"""One `@CC` should wake one machine, and the change must not silence them all.

Filed 2026-08-05 by Mike: «in group chats several nodes may each have an external
non-DPC agent with the same name, and that can trigger someone else's agent». The
embedded-agent path asks whether the agent is registered to *this* node; the
external path asked nothing at all, so a name match woke every bridge carrying
that name — each with a different working tree and a different memory, and the
reply from the one that could not do the work looked exactly like the reply from
the one that could. It cost a round in the `work` group.

Gating on registration alone would have broken every existing install on the day
it shipped: nothing was registrable before the Group Settings field existed, so
`group.agents[node_id]` held no external agent anywhere, and `@CC` would have
woken nobody — with no error, no log line, and a bridge that still looked alive.
Mike chose the transitional behaviour over a migration (2026-09-03): registration
decides once this node has registered anything for this group, and until then the
old behaviour stands and says so in the log. The gate then arrives per group, on
the day somebody fills the field, rather than on a release date.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.service import EXTERNAL_AGENT_PREFIX, _EXTERNAL_TAG_RE


class TestTheTagRuleIsEnforcedWhereItIsTyped:
    """Mention routing parses `@(\\w+)\\b`, so a tag is addressable only as far as
    its first non-word character. `CC-lnx` looks unique in the interface — the
    message metadata even keeps it whole — and is delivered to every `CC`. The
    error looks like success, which is why it is refused rather than documented."""

    @pytest.mark.parametrize("tag", ["CC", "CC_lnx", "CC2", "agent_1", "агент_1"])
    def test_a_reachable_tag_is_accepted(self, tag):
        assert _EXTERNAL_TAG_RE.fullmatch(tag), (
            f"«{tag}» survives mention routing whole and must be allowed"
        )

    @pytest.mark.parametrize("tag", ["CC-lnx", "Fifth Agent", "CC.win", "", "  "])
    def test_a_tag_that_routing_would_cut_is_refused(self, tag):
        assert not _EXTERNAL_TAG_RE.fullmatch(tag), (
            f"«{tag}» is cut by mention routing and would address every agent "
            "sharing its first segment"
        )

    def test_the_prefix_cannot_collide_with_a_folder_id(self):
        """Embedded agents are `agent_001` and the like; the prefix keeps the two
        kinds apart, and is what lets the gate ask «any external registered?»."""
        assert EXTERNAL_AGENT_PREFIX == "ext:"
        assert not "agent_001".startswith(EXTERNAL_AGENT_PREFIX)


class _Recorder:
    """The narrowest stand-in that still exercises the branch: the handler only
    reads a display name and a group's agent map, and only writes an event."""

    def __init__(self, registered, cc_name="cc", node_id="node-A"):
        self.events = []
        self.warnings = []
        self._registered = registered
        self._cc = cc_name
        self.node_id = node_id

    def allowed(self):
        return set(self._registered)


def _decide(mention_names, allowed_agents, cc_name):
    """The branch under test, lifted from group_handler._handle_agent_mentions.

    Kept in step with the handler by test_the_handler_still_holds_this_shape
    below, which reads the source rather than trusting this copy.
    """
    registered = {a[len(EXTERNAL_AGENT_PREFIX):].lower()
                  for a in allowed_agents if a.startswith(EXTERNAL_AGENT_PREFIX)}
    warned = False
    if registered:
        woken = registered & mention_names
    elif cc_name in mention_names:
        woken = {cc_name}
        warned = True
    else:
        woken = set()
    return woken, warned


class TestWhoIsWoken:
    def test_nothing_registered_anywhere_keeps_todays_behaviour(self):
        """The transition. On the first run after the change no node has
        registered anything, and the old behaviour has to survive that."""
        woken, warned = _decide({"cc"}, {"agent_001"}, "cc")
        assert woken == {"cc"}, "the change silenced a node that answered yesterday"
        assert warned, "it answered by name alone and said nothing about it"

    def test_a_registered_node_is_woken_by_its_own_tag(self):
        woken, warned = _decide({"cc_win"}, {"agent_001", "ext:CC_win"}, "cc")
        assert woken == {"cc_win"}
        assert not warned, "registration decided, so there is nothing to warn about"

    def test_a_registered_node_is_not_woken_by_someone_elses_tag(self):
        """The whole point: the Linux machine's tag no longer wakes this one."""
        woken, warned = _decide({"cc_lnx"}, {"ext:CC_win"}, "cc")
        assert woken == set()
        assert not warned

    def test_registration_replaces_the_configured_name(self):
        """Once a node has registered anything, its config name stops being a way
        in — otherwise the collision survives the fix for anyone who registers."""
        woken, _ = _decide({"cc"}, {"ext:CC_win"}, "cc")
        assert woken == set(), "the bare display name still reached a registered node"

    def test_several_external_agents_on_one_node(self):
        """The config holds one name; the group list holds as many as were typed."""
        woken, _ = _decide({"cc_win", "reviewer"},
                           {"ext:CC_win", "ext:reviewer", "agent_001"}, "cc")
        assert woken == {"cc_win", "reviewer"}


def test_the_handler_still_holds_this_shape():
    """The copy above is a copy. This reads the handler and fails if the branch
    it models is gone — a green suite over a stale model is worth nothing."""
    src = (Path(__file__).resolve().parents[1] / "dpc_client_core" / "message_handlers"
           / "group_handler.py").read_text(encoding="utf-8")
    assert "EXTERNAL_AGENT_PREFIX" in src, "the handler no longer knows external agents"
    assert "registered & mention_names" in src, "the registration branch is gone"
    assert 'elif cc_name in mention_names:' in src, "the transitional branch is gone"
    assert '"agent_tag": tag' in src, "the event stopped saying which tag was matched"
