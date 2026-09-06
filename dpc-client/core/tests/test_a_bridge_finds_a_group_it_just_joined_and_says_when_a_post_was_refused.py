"""Two defects seen on the Linux node, 2026-09-06.

(a) A group just joined has metadata.json and no history.json yet; the bridge
    looked only for history.json, missed the slugged directory, and so missed
    the tag registered there and posted as the configured "CC".
(b) The local API wraps a handler's answer in {"status": "OK", "payload": ...};
    a refused post is {"status": "error", "message": ...} inside that envelope,
    and the bridge printed the outer "OK".

Same harness as test_a_bridge_answers_to_the_tag_it_registered: fake DPC_HOME,
fake `websockets` in sys.modules, the CLI run by file path.
"""

import json
import runpy
import sys
import types

import pytest

from tests.test_a_bridge_answers_to_the_tag_it_registered import (  # noqa: F401
    BRIDGE, GROUP, bridge,
)

REFUSED = {"status": "error",
           "message": "agent name not registered for this node in this group: CC_mike"}


def _envelope(payload, status="OK"):
    return {"id": "x", "command": "send_group_agent_message", "status": status, "payload": payload}


# (a) a group with metadata.json and no history.json is found by its metadata


def test_a_just_joined_group_is_found_and_its_tag_seen_without_history(bridge):
    gdir = bridge.DPC_HOME / "conversations" / f"{GROUP}-1234"
    (gdir / "history.json").unlink()
    assert bridge._find_group_dir(GROUP) == gdir
    assert bridge._registered_tags(GROUP) == ["CC_mike"]
    assert bridge._resolve_identity(GROUP, None) == "CC_mike"
    assert bridge.read_history(GROUP) == []


def test_a_slugged_dir_with_neither_file_is_still_skipped(bridge):
    gdir = bridge.DPC_HOME / "conversations" / f"{GROUP}-1234"
    (gdir / "history.json").unlink()
    (gdir / "metadata.json").unlink()
    assert bridge._find_group_dir(GROUP) == bridge.DPC_HOME / "conversations" / GROUP


# (b) the outcome of a send is read inside the envelope


def test_refusal_under_an_ok_envelope_is_an_error(bridge):
    assert bridge._send_outcome(_envelope(REFUSED)) == (False, "ERROR " + REFUSED["message"])


def test_a_posted_message_id_is_ok(bridge):
    assert bridge._send_outcome(_envelope("0123456789abcdef")) == (True, "OK")


def test_an_error_envelope_is_an_error_with_its_message(bridge):
    reply = _envelope({"message": "Unknown or non-async command: x"}, status="ERROR")
    assert bridge._send_outcome(reply) == (False, "ERROR Unknown or non-async command: x")
    assert bridge._send_outcome({"status": "error", "message": "auth rejected"}) == \
        (False, "ERROR auth rejected")
    assert bridge._send_outcome("nonsense")[0] is False


def test_exit_code_is_1_for_a_refusal_and_0_for_a_post_or_a_timeout(bridge):
    assert bridge._send_exit_code(_envelope(REFUSED)) == 1
    assert bridge._send_exit_code(_envelope("0123456789abcdef")) == 0
    assert bridge._send_exit_code({"status": "sent"}) == 0


# (c) the CLI send path prints the ERROR line and exits 1


class _AnsweringWS:
    def __init__(self, replies):
        self._replies = list(replies)

    async def send(self, raw):
        pass

    async def recv(self):
        return self._replies.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _run_send(bridge, monkeypatch, capsys, reply: dict, text="hi"):
    fake = types.ModuleType("websockets")
    fake.connect = lambda url: _AnsweringWS([json.dumps({"status": "OK"}), json.dumps(reply)])
    monkeypatch.setitem(sys.modules, "websockets", fake)
    (bridge.DPC_HOME / ".ws_token").write_text("tok", encoding="utf-8")
    monkeypatch.setenv("DPC_HOME", str(bridge.DPC_HOME))
    monkeypatch.setattr(sys, "argv", ["cc_group_chat_bridge.py", "--group", GROUP, "--send", text])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(BRIDGE), run_name="__main__")
    return exc.value.code, capsys.readouterr().out


def test_cli_send_exits_1_and_says_error_when_the_backend_refuses(bridge, monkeypatch, capsys):
    code, out = _run_send(bridge, monkeypatch, capsys, _envelope(REFUSED))
    assert code == 1
    assert ("[SENT] 2 chars → group group-0a52389f2bb6: ERROR "
            "agent name not registered for this node in this group: CC_mike") in out


def test_cli_send_exits_0_and_says_ok_when_the_backend_posts(bridge, monkeypatch, capsys):
    code, out = _run_send(bridge, monkeypatch, capsys, _envelope("0123456789abcdef"))
    assert code == 0
    assert "[SENT] 2 chars → group group-0a52389f2bb6: OK" in out
