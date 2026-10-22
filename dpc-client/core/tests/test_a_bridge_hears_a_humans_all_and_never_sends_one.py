"""A bridge hears a human's @all, and never sends one.

The backend wakes every registered external tag on a human's `@all` and wakes
nobody on an agent's (service.py `mention_all = "all" in mentions and not
is_agent_sender`, group_handler.py the same for a peer's post). The bridge reads
history.json, not the wake events, and matched only its own tags, so the `@all`
that woke it left no [MENTION] line and no remembered depth. And a bridge post
is an agent post, so nothing it writes can broadcast @all on the backend today -
the send side refuses it anyway, so a later change to that rule cannot turn one
stray `@all` into a cascade (Mike's call, 2026-10-22).

Driven through the real CLI entry, `main(argv)`, with the harness of
test_a_bridge_carries_the_depth_it_showed_to_its_next_send.
"""

import json

import pytest

from tests.test_a_bridge_answers_to_the_tag_it_registered import (  # noqa: F401
    GROUP, NODE, OTHER, bridge,
)
from tests.test_a_bridge_carries_the_depth_it_showed_to_its_next_send import (  # noqa: F401
    cli, _WS, _posted,
)

TAGS = ["CC_mike"]


def _msg(text, sender="Mike", sender_type="human", **extra):
    return {"content": text, "sender_name": sender, "sender_type": sender_type, **extra}


def _marked(bridge, text, **kw):
    return [i for i, _ in bridge.find_mentions([_msg(text, **kw)], names=TAGS)] == [0]


# --- read side ---------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "@all please look", "@All please look", "@ALL please look",
    "ping @all.", "hey (@all)", "line one\n@all line two",
])
def test_a_humans_all_is_a_mention_of_this_bridge(bridge, text):
    assert _marked(bridge, text)


@pytest.mark.parametrize("text", [
    "@allowed_mentions is a field", "@allahverdiyev wrote", "mail mike@all.com",
    "@all_hands", "@alls", "all of you", "@ all",
])
def test_a_word_that_only_starts_with_all_is_not_a_mention(bridge, text):
    assert not _marked(bridge, text)


def test_all_inside_code_is_not_a_mention(bridge):
    assert not _marked(bridge, "the tag `@all` wakes everyone")
    assert not _marked(bridge, "```\n@all\n```")
    assert not _marked(bridge, "```text\nrun @all now\n```\nthen nothing")
    assert _marked(bridge, "`code` and then @all outside")


@pytest.mark.parametrize("extra", [
    {"sender_type": "agent"},
    {"sender_type": "agent", "agent_owner": OTHER},
    {"sender_type": None, "is_agent": True},          # a record from before sender_type
])
def test_an_agents_all_is_not_a_mention(bridge, extra):
    assert not _marked(bridge, "@all report", sender="CC_linux", **extra)


def test_all_from_one_of_this_bridges_own_tags_is_not_a_mention(bridge):
    assert not _marked(bridge, "@all note to self", sender="CC_mike")
    assert not _marked(bridge, "@all note to self", sender="CC_mike", sender_type="agent")


def test_an_explicit_tag_still_marks_whoever_sent_it(bridge):
    assert _marked(bridge, "@CC_mike look", sender="CC_linux", sender_type="agent")
    assert _marked(bridge, "@cc_MIKE look")
    assert not _marked(bridge, "@CC_mike2 look")


def test_a_humans_all_names_every_tag_of_this_bridge(bridge):
    names = ["CC_mike", "CC_night"]
    assert bridge._tags_mentioned(_msg("@all hi"), names) == names
    assert bridge._tags_mentioned(_msg("@CC_night hi"), names) == ["CC_night"]
    assert bridge._tags_mentioned(_msg("@all hi", sender_type="agent"), names) == []
    assert bridge._tags_mentioned(_msg("`@all` hi"), names) == []


def test_a_humans_all_leaves_no_depth_because_a_human_post_starts_at_zero(bridge):
    msg = _msg("@all hi", mention_depth=5)
    assert bridge._reply_mention_depth(msg, NODE) == 0
    bridge._remember_history_line(GROUP, msg, TAGS, NODE)
    assert bridge._remembered_depth(GROUP, "CC_mike") == 0


def _history(bridge, *records):
    gdir = bridge.DPC_HOME / "conversations" / f"{GROUP}-1234"
    (gdir / "history.json").write_text(json.dumps({"messages": list(records)}), encoding="utf-8")


def test_mentions_lists_a_humans_all_through_the_cli(bridge, cli):
    _history(bridge, _msg("@all roll call"), _msg("@all roll call", "CC_linux", "agent"),
             _msg("nothing here"))
    code, out, _ = cli("--group", GROUP, "--mentions")
    assert code == 0
    assert "=== 1 mention(s) of @CC_mike ===" in out
    assert "roll call" in out


def test_the_cli_read_marks_an_agent_all_as_no_mention(bridge, cli):
    _history(bridge, _msg("@all roll call", "CC_linux", "agent"))
    code, out, _ = cli("--group", GROUP, "--mentions")
    assert code == 0
    assert "No mentions of @CC_mike found." in out


# --- send side: the cascade guard --------------------------------------------

@pytest.mark.parametrize("text", [
    "@all wake up", "@All wake up", "@ALL wake up", "done, cc @all.", "a\n@all\nb",
    "`code` first, then @all", "unclosed ``` fence then @all",
])
def test_send_refuses_all_outside_code(cli, text):
    code, out, sent = cli("--group", GROUP, "--send", text)
    assert code == 1
    assert "[ERROR]" in out and "@all" in out
    assert sent == [], "a refused text still reached the socket"


def test_send_file_refuses_all_outside_code(cli, tmp_path):
    f = tmp_path / "body.md"
    f.write_text("status\n@all please read\n", encoding="utf-8")
    code, out, sent = cli("--group", GROUP, "--send-file", str(f))
    assert code == 1
    assert "[ERROR]" in out
    assert sent == []


@pytest.mark.parametrize("text", [
    "the tag `@all` is a human's word",
    "```\n@all\n```",
    "see:\n```text\n@all hello\n```\nend",
    "@allowed_mentions is a field", "mail mike@all.com", "@CC_linux over to you",
])
def test_send_allows_all_in_code_and_words_that_only_start_with_it(cli, text):
    code, out, sent = cli("--group", GROUP, "--send", text)
    assert code == 0, out
    (cmd,) = sent
    assert cmd["payload"]["text"] == text


def test_send_file_allows_all_in_a_fenced_block(cli, tmp_path):
    f = tmp_path / "body.md"
    f.write_text("```\n@all\n```\n", encoding="utf-8")
    code, out, sent = cli("--group", GROUP, "--send-file", str(f))
    assert code == 0, out
    assert len(sent) == 1


def test_a_refused_send_keeps_the_remembered_depth_for_the_retry(bridge, cli):
    bridge._remember_reply_depth(GROUP, "CC_mike", 1)
    code, _, sent = cli("--group", GROUP, "--send", "@all hi")
    assert code == 1 and sent == []
    assert bridge._remembered_depth(GROUP, "CC_mike") == 1


# --- one rule, two copies ------------------------------------------------------

def test_the_code_pattern_is_the_backends(bridge):
    from dpc_client_core.service import CODE_BLOCK_RE, mentions_in
    assert bridge._CODE_RE.pattern == CODE_BLOCK_RE.pattern
    for text in ("`@all` x", "```\n@all\n```", "@all x", "a `b` @all"):
        assert bridge._has_all(text) == ("all" in mentions_in(text)), text
