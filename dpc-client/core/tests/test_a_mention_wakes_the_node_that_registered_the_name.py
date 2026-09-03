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

There are two mention paths — one for a message this node sends, one for a
message arriving from a peer. Both call `external_agents_to_wake`, and so do
these tests: an earlier version modelled the decision instead of calling it, and
stayed green while one of the paths went ungated.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.agent_service import _name_refusal
from dpc_client_core.service import (
    EXTERNAL_AGENT_PREFIX,
    MENTIONABLE_NAME_RE,
    external_agents_to_wake,
)


class TestTheTagRuleIsEnforcedWhereItIsTyped:
    """Mention routing parses `@(\\w+)\\b`, so a tag is addressable only as far as
    its first non-word character. `CC-lnx` is delivered to every `CC`, while the
    chat still shows the text as typed — so the mistake looks like it worked,
    which is why it is refused at the input rather than explained in a document."""

    @pytest.mark.parametrize("tag", ["CC", "CC_lnx", "CC2", "agent_1", "агент_1"])
    def test_a_reachable_tag_is_accepted(self, tag):
        assert MENTIONABLE_NAME_RE.fullmatch(tag), (
            f"«{tag}» survives mention routing whole and must be allowed"
        )

    @pytest.mark.parametrize("tag", ["CC-lnx", "Fifth Agent", "CC.win", "", "  "])
    def test_a_tag_that_routing_would_cut_is_refused(self, tag):
        assert not MENTIONABLE_NAME_RE.fullmatch(tag), (
            f"«{tag}» is cut by mention routing and would address every agent "
            "sharing its first segment"
        )

    def test_the_prefix_cannot_collide_with_a_folder_id(self):
        """Embedded agents are `agent_001` and the like; the prefix keeps the two
        kinds apart, and is what lets the gate ask «any external registered?»."""
        assert EXTERNAL_AGENT_PREFIX == "ext:"
        assert not "agent_001".startswith(EXTERNAL_AGENT_PREFIX)


class TestWhoIsWoken:
    def test_nothing_registered_anywhere_keeps_todays_behaviour(self):
        """The transition. On the first run after the change no node has
        registered anything, and the old behaviour has to survive that."""
        woken, warned = external_agents_to_wake({"agent_001"}, {"cc"}, "CC")
        assert woken == {"cc"}, "the change silenced a node that answered yesterday"
        assert warned, "it answered by name alone and said nothing about it"

    def test_a_registered_node_is_woken_by_its_own_tag(self):
        woken, warned = external_agents_to_wake(
            {"agent_001", "ext:CC_win"}, {"cc_win"}, "CC")
        assert woken == {"cc_win"}
        assert not warned, "registration decided, so there is nothing to warn about"

    def test_a_registered_node_is_not_woken_by_someone_elses_tag(self):
        """The whole point: the Linux machine's tag no longer wakes this one."""
        woken, warned = external_agents_to_wake({"ext:CC_win"}, {"cc_lnx"}, "CC")
        assert woken == set()
        assert not warned

    def test_registration_replaces_the_configured_name(self):
        """Once a node has registered anything, its config name stops being a way
        in — otherwise the collision survives the fix for anyone who registers."""
        woken, _ = external_agents_to_wake({"ext:CC_win"}, {"cc"}, "CC")
        assert woken == set(), "the bare display name still reached a registered node"

    def test_several_external_agents_on_one_node(self):
        """The config holds one name; the group list holds as many as were typed."""
        woken, _ = external_agents_to_wake(
            {"ext:CC_win", "ext:reviewer", "agent_001"},
            {"cc_win", "reviewer"}, "CC")
        assert woken == {"cc_win", "reviewer"}


def test_both_mention_paths_ask_the_same_function():
    core = Path(__file__).resolve().parents[1] / "dpc_client_core"
    handler = (core / "message_handlers" / "group_handler.py").read_text(encoding="utf-8")
    service = (core / "service.py").read_text(encoding="utf-8")

    assert "external_agents_to_wake(" in handler, "the peer path stopped asking"
    assert service.count("external_agents_to_wake(") >= 2, "the send path stopped asking"
    for src, who in ((handler, "peer path"), (service, "send path")):
        assert '"agent_tag": tag' in src, f"{who} stopped naming the matched tag"


class TestAnEmbeddedAgentIsNamedTheSameWay:
    """A display name is how a mention reaches an embedded agent, so it is under
    the same rule as an external tag — and was not, until Mike asked for it."""

    def test_a_hyphen_is_refused(self):
        """The case the rule exists for: `CC-lnx` is reached by `@CC`."""
        refusal = _name_refusal("CC-lnx")
        assert refusal, "an agent named this answers to @CC and shares it"
        assert "@CC»" in refusal, "the refusal should name what it would answer to"

    def test_a_space_is_refused(self):
        assert "@Fifth»" in _name_refusal("Fifth Agent")

    def test_a_reachable_name_is_accepted(self):
        for name in ("Ark", "CC_win", "agent_1", "агент_1"):
            assert _name_refusal(name) == "", f"«{name}» survives routing whole"

    def test_an_empty_name_is_refused_as_empty(self):
        """Refused *for being empty*. Both branches return a string, so
        asserting truthiness passed with the empty check deleted."""
        for blank in ("", "   ", None):
            assert _name_refusal(blank) == "An agent needs a name."

    def test_creation_and_rename_both_ask(self):
        """The wiring, not the helper — and counted, not matched.

        Testing the helper alone stayed green with the call deleted from
        `create_agent`. Then `"_name_refusal(name)" in src` stayed green too,
        because `def _name_refusal(name):` contains it.
        """
        src = (Path(__file__).resolve().parents[1] / "dpc_client_core"
               / "agent_service.py").read_text(encoding="utf-8")
        assert src.count("_name_refusal(") == 3, (
            "one definition and two call sites — creation or rename stopped asking"
        )
        assert '_name_refusal(updates["name"])' in src, "rename stopped asking"

    def test_every_agent_on_this_machine_would_pass(self):
        """The rule was chosen to break nothing that exists: all nine agents
        configured here are already addressable whole."""
        import json
        import os
        root = Path.home() / ".dpc" / "agents"
        if not root.exists():
            return
        for d in sorted(os.listdir(root)):
            cfg = root / d / "config.json"
            if not cfg.exists():
                continue
            try:
                name = json.loads(cfg.read_text(encoding="utf-8")).get("name") or ""
            except Exception:
                continue
            if name:
                assert _name_refusal(name) == "", (
                    f"«{name}» is configured and the new rule would refuse it"
                )
