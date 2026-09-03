"""The group bridge can be woken by the backend's push instead of polling history.json.

`--listen` authenticates on the local WebSocket and prints one line per
cc_group_mention event addressed to this bridge's tag. The frame filter is a
pure function; the loop is driven here with a scripted fake socket (same
harness as test_a_bridge_answers_to_the_tag_it_registered: fake DPC_HOME, fake
`websockets` in sys.modules).
"""

import json
import re
import sys
import types

import pytest

from tests.test_a_bridge_answers_to_the_tag_it_registered import (  # noqa: F401
    GROUP, OTHER, bridge,
)


def _frame(**over):
    payload = {
        "group_id": GROUP,
        "text": "@CC_mike look\nsecond line",
        "sender_name": "Mike",
        "sender_node_id": OTHER,
        "agent_tag": "cc_mike",
    }
    payload.update(over)
    return {"event": "cc_group_mention", "payload": payload}


# (a) the frame filter is pure


def test_filter_accepts_a_mention_for_own_group_and_tag(bridge):
    assert bridge._mention_for_me(_frame(), GROUP, ["cc_mike"]) == _frame()["payload"]


def test_filter_rejects_another_group(bridge):
    assert bridge._mention_for_me(_frame(group_id="group-ffff"), GROUP, ["cc_mike"]) is None


def test_filter_rejects_another_tag(bridge):
    assert bridge._mention_for_me(_frame(agent_tag="cc_linux"), GROUP, ["cc_mike"]) is None


def test_filter_rejects_a_non_mention_event(bridge):
    frame = {"event": "peer_connected", "payload": _frame()["payload"]}
    assert bridge._mention_for_me(frame, GROUP, ["cc_mike"]) is None


def test_filter_rejects_a_malformed_frame(bridge):
    assert bridge._mention_for_me({"event": "cc_group_mention"}, GROUP, ["cc_mike"]) is None
    assert bridge._mention_for_me({"event": "cc_group_mention", "payload": "x"}, GROUP, ["cc_mike"]) is None
    assert bridge._mention_for_me("not a frame", GROUP, ["cc_mike"]) is None


def test_filter_accepts_any_tag_when_asked(bridge):
    frame = _frame(agent_tag="cc_linux")
    assert bridge._mention_for_me(frame, GROUP, ["cc_mike"], all_tags=True) == frame["payload"]


def test_filter_compares_the_tag_case_insensitively(bridge):
    frame = _frame(agent_tag="CC_Mike")
    assert bridge._mention_for_me(frame, GROUP, ["cc_mike"]) == frame["payload"]


# (b) the listen loop on a scripted socket


class _Closed(Exception):
    """Stands in for websockets' ConnectionClosed."""


class _ScriptedWS:
    def __init__(self, script, sent):
        self.script = script
        self.sent = sent

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def scripted(bridge, monkeypatch):
    """sessions: one recv script per connect (an exception there fails the connect itself)."""
    sessions, calls, sent = [], [], []
    fake = types.ModuleType("websockets")

    def connect(url):
        calls.append(url)
        session = sessions.pop(0)
        if isinstance(session, BaseException):
            raise session
        return _ScriptedWS(session, sent)

    fake.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", fake)
    (bridge.DPC_HOME / ".ws_token").write_text("tok", encoding="utf-8")

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr(bridge.asyncio, "sleep", no_sleep)
    return sessions, calls, sent


OK = json.dumps({"status": "OK"})
UNRELATED = json.dumps({"event": "peer_connected", "payload": {"node_id": OTHER}})
MENTION_LINE = r"\[MENTION\] \d\d:\d\d:\d\d Mike \(@cc_mike\) in group-0a52389f2bb6: @CC_mike look"


def test_listen_once_prints_one_mention_and_returns_0_without_reconnecting(bridge, scripted, capsys):
    sessions, calls, sent = scripted
    script = [OK, UNRELATED, json.dumps(_frame()), _Closed("gone")]
    sessions.append(script)
    assert bridge.listen_sync(GROUP, ["CC_mike"], once=True) == 0
    out, err = capsys.readouterr()
    mention_lines = [line for line in out.splitlines() if line.startswith("[MENTION]")]
    assert len(mention_lines) == 1
    assert re.fullmatch(MENTION_LINE, mention_lines[0])
    assert "[LISTEN] listening for @CC_mike in group-0a52389f2bb6" in err
    assert "reconnecting" not in err
    assert calls == ["ws://127.0.0.1:9999"]
    assert script == [_Closed("gone")] or isinstance(script[0], _Closed)   # never consumed
    assert sent == [{"id": "group-bridge-auth", "command": "auth", "token": "tok"}]


def test_listen_skips_a_mention_for_another_tag(bridge, scripted, capsys):
    sessions, _, _ = scripted
    sessions.append([OK, json.dumps(_frame(agent_tag="cc_linux")), json.dumps(_frame()), _Closed()])
    assert bridge.listen_sync(GROUP, ["CC_mike"], once=True) == 0
    out = capsys.readouterr().out
    assert out.count("[MENTION]") == 1
    assert "(@cc_mike)" in out and "(@cc_linux)" not in out


def test_listen_reconnects_with_doubling_backoff(bridge, scripted, capsys):
    sessions, calls, _ = scripted
    sessions += [[OK, _Closed("gone")], ConnectionRefusedError("refused"), [OK, json.dumps(_frame())]]
    assert bridge.listen_sync(GROUP, ["CC_mike"], once=True) == 0
    err = capsys.readouterr().err
    assert "[LISTEN] reconnecting in 1s" in err
    assert "[LISTEN] reconnecting in 2s" in err
    assert len(calls) == 3


def test_listen_stops_with_exit_0_on_keyboard_interrupt(bridge, scripted, capsys):
    sessions, _, _ = scripted
    sessions.append([OK, KeyboardInterrupt()])
    assert bridge.listen_sync(GROUP, ["CC_mike"]) == 0
    err = capsys.readouterr().err
    assert "[LISTEN] stopped" in err
    assert "reconnecting" not in err


def test_listen_exits_1_when_auth_is_rejected(bridge, scripted, capsys):
    sessions, calls, _ = scripted
    sessions.append([json.dumps({"status": "ERROR", "message": "invalid token"})])
    assert bridge.listen_sync(GROUP, ["CC_mike"]) == 1
    assert "[ERROR] Auth rejected" in capsys.readouterr().err
    assert len(calls) == 1


def test_listen_all_tags_banner_and_match(bridge, scripted, capsys):
    sessions, _, _ = scripted
    sessions.append([OK, json.dumps(_frame(agent_tag="cc_linux"))])
    assert bridge.listen_sync(GROUP, ["CC_mike"], all_tags=True, once=True) == 0
    out, err = capsys.readouterr()
    assert "[LISTEN] listening for any tag in group-0a52389f2bb6" in err
    assert "(@cc_linux)" in out


# (c) --json


def test_listen_json_prints_the_payload_as_one_json_line(bridge, scripted, capsys):
    sessions, _, _ = scripted
    sessions.append([OK, UNRELATED, json.dumps(_frame())])
    assert bridge.listen_sync(GROUP, ["CC_mike"], as_json=True, once=True) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == _frame()["payload"]
    assert set(json.loads(lines[0])) == {"group_id", "text", "sender_name", "sender_node_id", "agent_tag"}


def test_format_mention_keeps_the_first_line_cut_at_200(bridge):
    line = bridge._format_mention(_frame(text="x" * 300 + "\nmore")["payload"], ts="12:00:00")
    assert line == "[MENTION] 12:00:00 Mike (@cc_mike) in group-0a52389f2bb6: " + "x" * 200
    assert bridge._format_mention(_frame(text="")["payload"], ts="12:00:00").endswith(": ")
