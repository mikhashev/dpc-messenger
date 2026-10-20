"""A bridge carries the depth it showed to its next send.

3706cf47 printed `[--mention-depth N]` on a [MENTION] line and on a history
line, and `--send` without `--mention-depth` still sent 0: a forgotten flag
reset the cross-node count and reopened the loop two bridges can run
(specs/dptp_v1.md §4.3; Zcode's and Ark's review, 2026-10-20). Now the bridge
remembers, per group and tag, the largest depth it printed for a mention of its
own tag, and the next send that goes out without the flag carries it and
consumes it. [SENT] names the depth sent, 0 included.

Driven through the real CLI entry, `main(argv)`, against a fake DPC_HOME and a
fake `websockets` (the harness of test_a_bridge_answers_to_the_tag_it_registered).
"""

import json
import sys
import time
import types

import pytest

from tests.test_a_bridge_answers_to_the_tag_it_registered import (  # noqa: F401
    GROUP, NODE, OTHER, bridge,
)

OTHER_GROUP = "group-ffffffffffff"


class _WS:
    """Answers auth, then plays `frames`; a callable frame gets the last command's id."""

    def __init__(self, sent, frames):
        self.sent = sent
        self._frames = [json.dumps({"status": "OK"}), *frames]

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        item = self._frames.pop(0)
        return item(self.sent[-1]["id"]) if callable(item) else item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _posted(cid, payload="0123456789abcdef"):
    return json.dumps({"id": cid, "command": "send_group_agent_message",
                       "status": "OK", "payload": payload})


REFUSED = {"status": "error", "message": "agent name not registered"}


@pytest.fixture
def cli(bridge, monkeypatch, capsys):
    """run(*argv, frames=[...]) -> (exit code, stdout, commands sent)."""
    (bridge.DPC_HOME / ".ws_token").write_text("tok", encoding="utf-8")

    def run(*argv, frames=None):
        sent = []
        fake = types.ModuleType("websockets")
        fake.connect = lambda url: _WS(sent, frames if frames is not None else [_posted])
        monkeypatch.setitem(sys.modules, "websockets", fake)
        code = 0
        try:
            bridge.main(list(argv))
        except SystemExit as e:
            code = e.code
        captured = capsys.readouterr()
        run.err = captured.err
        return code, captured.out, [c for c in sent if c.get("command") != "auth"]

    return run


def _mention(depth, tag="cc_mike", group=GROUP):
    return json.dumps({"event": "cc_group_mention", "payload": {
        "group_id": group, "agent_tag": tag, "sender_name": "CC_linux",
        "text": f"@{tag} look", "mention_depth": depth}})


def _listen_once(cli, depth, *extra):
    code, out, _ = cli("--group", GROUP, "--listen", "--once", *extra, frames=[_mention(depth)])
    assert code == 0
    return out


def _send(cli, *extra, group=GROUP):
    code, out, sent = cli("--group", group, "--send", "hi", *extra)
    assert code == 0, out
    (cmd,) = sent
    return cmd["payload"].get("mention_depth", 0), out


# (a) a printed [MENTION] at depth 1 is carried by a send without the flag

def test_a_send_without_the_flag_carries_the_depth_the_mention_line_showed(cli):
    assert "[--mention-depth 1]" in _listen_once(cli, 1)
    depth, out = _send(cli)
    assert depth == 1, "the reply reset the count the [MENTION] line showed"
    assert "[SENT] 2 chars → group group-0a52389f2bb6: OK (mention_depth=1)" in out
    assert "[INFO] mention_depth 1 remembered" in out


# (b) one-shot

def test_the_remembered_depth_is_spent_by_the_send_that_carried_it(cli):
    _listen_once(cli, 1)
    assert _send(cli)[0] == 1
    depth, out = _send(cli)
    assert depth == 0, "a second send reused a depth the first one had spent"
    assert "OK (mention_depth=0)" in out


# (c) an explicit flag wins, 0 included, and consumes too

def test_an_explicit_zero_overrides_a_remembered_depth_and_spends_it(cli):
    _listen_once(cli, 1)
    depth, out = _send(cli, "--mention-depth", "0")
    assert depth == 0 and "OK (mention_depth=0)" in out
    assert "remembered" not in out
    assert _send(cli)[0] == 0, "the explicit send left the remembered depth behind"


def test_an_explicit_depth_is_sent_as_given(cli):
    _listen_once(cli, 1)
    assert _send(cli, "--mention-depth", "2")[0] == 2


# (d) several hints before a send: the largest

@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_of_two_mentions_shown_the_deeper_depth_is_carried(cli, order):
    for d in order:
        _listen_once(cli, d)
    assert _send(cli)[0] == 1


def test_a_history_line_for_my_tag_leaves_its_depth_and_one_for_another_tag_does_not(bridge, cli):
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    history = {"messages": [
        {"sender_name": "CC_linux", "sender_type": "agent", "sender_node_id": OTHER,
         "content": "@CC_linux note to self", "timestamp": now},
        {"sender_name": "Mike", "sender_type": "human", "content": "@CC_mike hi", "timestamp": now},
    ]}
    (bridge._find_group_dir(GROUP) / "history.json").write_text(json.dumps(history), encoding="utf-8")
    cli("--group", GROUP, "--last", "10")
    assert _send(cli)[0] == 0, "a line that does not mention this bridge's tag left a depth"

    history["messages"].append({"sender_name": "CC_linux", "sender_type": "agent",
                                "sender_node_id": OTHER, "content": "@CC_mike over to you",
                                "timestamp": now})
    (bridge._find_group_dir(GROUP) / "history.json").write_text(json.dumps(history), encoding="utf-8")
    code, out, _ = cli("--group", GROUP, "--last", "10")
    assert "CC_linux [--mention-depth 1]: @CC_mike over to you" in out
    assert _send(cli)[0] == 1, "the history line's depth did not reach the send"


def test_a_mention_of_another_tag_printed_under_all_tags_leaves_nothing(bridge, cli):
    code, out, _ = cli("--group", GROUP, "--listen", "--once", "--all-tags",
                       frames=[_mention(1, tag="cc_linux")])
    assert code == 0 and "[--mention-depth 1]" in out
    assert not (bridge.DPC_HOME / bridge.MENTION_DEPTH_STATE_FILE).exists(), (
        "a mention of a tag this bridge does not answer to left a depth")
    assert _send(cli)[0] == 0


# (e) stale entries are harmless

def test_a_remembered_depth_older_than_the_ttl_is_ignored(bridge, cli):
    old = time.time() - bridge.MENTION_DEPTH_TTL_S - 5
    (bridge.DPC_HOME / bridge.MENTION_DEPTH_STATE_FILE).write_text(
        json.dumps({GROUP: {"cc_mike": {"depth": 1, "at": old}}}), encoding="utf-8")
    assert _send(cli)[0] == 0


def test_an_old_history_mention_is_not_remembered_however_often_it_is_reread(bridge, cli):
    old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                        time.gmtime(time.time() - bridge.MENTION_DEPTH_TTL_S - 60))
    history = {"messages": [{"sender_name": "CC_linux", "sender_type": "agent",
                             "sender_node_id": OTHER, "content": "@CC_mike yesterday",
                             "timestamp": old}]}
    (bridge._find_group_dir(GROUP) / "history.json").write_text(json.dumps(history), encoding="utf-8")
    for _ in range(3):
        cli("--group", GROUP, "--mentions")
    assert _send(cli)[0] == 0


@pytest.mark.parametrize("entry", [{"depth": 1.5, "at": "now"}, {"depth": True},
                                   "garbage", {"depth": -1}])
def test_a_malformed_entry_is_ignored(bridge, cli, entry):
    if isinstance(entry, dict) and entry.get("at") == "now":
        entry = dict(entry, at=time.time())
    (bridge.DPC_HOME / bridge.MENTION_DEPTH_STATE_FILE).write_text(
        json.dumps({GROUP: {"cc_mike": entry}}), encoding="utf-8")
    assert _send(cli)[0] == 0


def test_an_unreadable_state_file_is_no_state(bridge, cli):
    (bridge.DPC_HOME / bridge.MENTION_DEPTH_STATE_FILE).write_text("{not json", encoding="utf-8")
    assert _send(cli)[0] == 0
    _listen_once(cli, 1)
    assert _send(cli)[0] == 1


# (f) other groups are not affected

def test_a_depth_remembered_in_one_group_does_not_reach_another(cli):
    _listen_once(cli, 1)
    assert _send(cli, "--as", "CC_mike", group=OTHER_GROUP)[0] == 0
    assert _send(cli)[0] == 1, "the other group's send spent this group's depth"


# a refused post keeps it for the retry

def test_a_refused_post_keeps_the_remembered_depth_for_the_retry(cli):
    _listen_once(cli, 1)
    code, out, sent = cli("--group", GROUP, "--send", "hi",
                          frames=[lambda cid: _posted(cid, REFUSED)])
    assert code == 1 and sent[0]["payload"]["mention_depth"] == 1
    assert "(mention_depth=1)" in out
    assert _send(cli)[0] == 1


# (g) a depth that is not a whole number is refused, not truncated

@pytest.mark.parametrize("bad", ["1.5", "1_0", "+1", " 1", "-1", "one", "١"])
def test_the_cli_refuses_a_depth_that_is_not_a_whole_number(cli, bad):
    code, _, sent = cli("--group", GROUP, "--send", "hi", "--mention-depth", bad)
    assert code == 2 and sent == []
    assert "--mention-depth" in cli.err


def test_the_state_file_sits_beside_the_ws_token(bridge, cli):
    _listen_once(cli, 1)
    path = bridge.DPC_HOME / bridge.MENTION_DEPTH_STATE_FILE
    assert path.parent == (bridge.DPC_HOME / ".ws_token").parent
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state[GROUP]["cc_mike"]["depth"] == 1
    assert [p.name for p in bridge.DPC_HOME.iterdir() if p.name.endswith(".tmp")] == []
