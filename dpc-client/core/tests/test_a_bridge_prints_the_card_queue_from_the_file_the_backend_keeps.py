"""`cc_group_chat_bridge.py --card` (Mike, 2026-11-25: CC must be able to see the card's queue).

It prints `<DPC_HOME>/state/card_lease.json` in a readable form, without calling the backend: who
holds the card, who waits in order with the lease's estimate or "wait unknown", the last grants and
releases, and a warning when the file is stale (the backend that wrote it is gone, stopped, or has
not refreshed it). A missing file is said, with exit 1.
"""

import json
import os

import pytest

from tests.test_a_bridge_answers_to_the_tag_it_registered import bridge  # noqa: F401

NOW = 1_800_000_000.0

SAMPLE = {
    "schema": "card_lease/1",
    "updated_at": NOW - 3,
    "updated_at_iso": "2027-01-15T08:00:00Z",
    "backend_pid": 4242,
    "free": False,
    "closed": None,
    "holder": None,
    "holders": [{
        "client": "whisper:turbo", "display": "Whisper (turbo)", "kind": "transcription",
        "engine": "whisper", "state": "running", "busy": True, "purpose": "dictation",
        "uses": ["dictation"], "agent_id": "agent_ark", "conversation_id": "group-a",
        "since": NOW - 68, "eta_s": 20, "beside": None, "vram_mib": 3000,
    }],
    "waiters": [
        {"position": 1, "client": "music:yue2", "display": "yue2.cpp", "kind": "music",
         "purpose": "music job J1", "agent_id": None, "conversation_id": None, "since": NOW - 41,
         "give_up_at": NOW + 100, "until_cancelled": False, "wait_s": 20.0, "wait_known": True,
         "wait_upper_bound": False, "waits_for": ["Whisper (turbo) (dictation)"], "where": "busy_holder",
         "for_a_peer": False},
        {"position": 2, "client": "qwen27", "display": "qwen27", "kind": "inference",
         "purpose": "agent agent_x in chat group-b", "agent_id": "agent_x", "conversation_id": "group-b",
         "since": NOW - 5, "give_up_at": None, "until_cancelled": True, "wait_s": None,
         "wait_known": False, "wait_upper_bound": False,
         "waits_for": ["Whisper (turbo)", "yue2.cpp"], "where": "card_queue", "for_a_peer": False},
    ],
    "recent": [
        {"at": NOW - 200, "event": "granted", "client": "whisper:turbo", "display": "Whisper (turbo)",
         "purpose": "dictation", "waited_s": 3.2},
        {"at": NOW - 120, "event": "released", "client": "whisper:turbo", "display": "Whisper (turbo)",
         "purpose": "dictation", "why": "yield for music:yue2", "held_s": 80.0},
        {"at": NOW - 90, "event": "left_the_line", "client": "music:yue2", "display": "yue2.cpp",
         "purpose": "music job J0", "why": "cancelled", "waited_s": 30.0},
    ],
}


def _write(bridge, data=SAMPLE):
    path = bridge._card_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_the_file_is_state_card_lease_json_under_the_dpc_home(bridge):  # noqa: F811
    assert bridge._card_file_path() == bridge.DPC_HOME / "state" / "card_lease.json"


def test_the_holder_the_waiters_in_order_and_the_record_are_printed(bridge):  # noqa: F811
    text = bridge.format_card(SAMPLE, now=NOW, pid_alive=True)

    lines = text.splitlines()
    assert lines[0] == "[CARD] held by 1:"
    assert lines[1] == ("  Whisper (turbo): dictation (agent agent_ark, chat group-a), running/busy "
                        f"since {bridge._clock_of(NOW - 68)} (1 min 08 s), about 20 s left")
    assert lines[2] == "[CARD] queue: 2 waiting, first served first:"
    assert lines[3] == ("  1. yue2.cpp: music job J1, waiting 41 s, wait about 20 s"
                        "; waits for Whisper (turbo) (dictation)")
    assert lines[4] == ("  2. qwen27: agent agent_x in chat group-b (agent agent_x, chat group-b), waiting 5 s, "
                        "wait unknown, until cancelled; waits for Whisper (turbo), yue2.cpp")
    assert lines[5] == "[CARD] recent (3, oldest first):"
    assert lines[6].endswith("granted: Whisper (turbo) - dictation (waited 3 s)")
    assert lines[7].endswith("released: Whisper (turbo) - dictation (held 1 min 20 s; yield for music:yue2)")
    assert lines[8].endswith("left the line: yue2.cpp - music job J0 (waited 30 s; cancelled)")
    assert lines[-1] == f"[CARD] file updated {bridge._clock_of(NOW - 3)} (3 s ago), backend pid 4242 (running)"
    assert not any("WARNING" in line for line in lines)


def test_a_free_card_and_an_empty_queue_say_so(bridge):  # noqa: F811
    free = {"updated_at": NOW - 1, "backend_pid": 7, "holders": [], "waiters": [], "recent": []}

    lines = bridge.format_card(free, now=NOW, pid_alive=True).splitlines()

    assert lines[:2] == ["[CARD] free: nobody holds the GPU card", "[CARD] queue: nobody waits"]


def test_an_estimate_that_is_a_bound_says_at_most(bridge):  # noqa: F811
    waiter = dict(SAMPLE["waiters"][0], wait_upper_bound=True)

    assert bridge._estimate_words(waiter) == "wait at most 20 s"
    assert bridge._estimate_words(dict(waiter, wait_known=False, wait_s=None)) == "wait unknown"


def test_a_file_of_a_backend_that_is_gone_is_marked_stale(bridge):  # noqa: F811
    text = bridge.format_card(SAMPLE, now=NOW, pid_alive=False)

    assert text.splitlines()[0] == "[CARD] WARNING: stale: the backend that wrote this file (pid 4242) is not running"
    assert text.splitlines()[-1].endswith("backend pid 4242 (not running)")


def test_a_file_nobody_refreshes_is_marked_possibly_stale(bridge):  # noqa: F811
    text = bridge.format_card(dict(SAMPLE, updated_at=NOW - 600), now=NOW, pid_alive=True)

    assert text.splitlines()[0] == ("[CARD] WARNING: possibly stale: not refreshed for 10 min 00 s "
                                    "(it refreshes every 15 s)")


def test_a_stopped_backend_is_said(bridge):  # noqa: F811
    text = bridge.format_card(dict(SAMPLE, backend_stopped=True), now=NOW, pid_alive=False)

    assert "has stopped; DPC holds no card now" in text.splitlines()[0]


def test_a_pid_check_never_signals_the_process_on_windows(bridge, monkeypatch):  # noqa: F811
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert bridge._pid_alive(os.getpid()) is True
    assert bridge._pid_alive(0) is None and bridge._pid_alive(None) is None and bridge._pid_alive(True) is None
    if os.name == "nt":
        assert killed == [], "os.kill terminates a process on Windows"


def test_a_pid_nobody_has_is_not_alive(bridge):  # noqa: F811
    assert bridge._pid_alive(2 ** 22 + 12345) is False


def test_the_cli_prints_the_file_and_exits_0_without_a_group_or_the_backend(bridge, capsys, monkeypatch):  # noqa: F811
    _write(bridge, dict(SAMPLE, updated_at=__import__("time").time(), backend_pid=os.getpid()))

    def no_backend(*a, **kw):
        raise AssertionError("--card calls no backend")

    for name in ("_fetch_chat_replies", "send_group_message_sync", "_backend_commands", "_get_ws_url"):
        monkeypatch.setattr(bridge, name, no_backend)

    with pytest.raises(SystemExit) as stop:
        bridge.main(["--card"])

    out = capsys.readouterr().out
    assert stop.value.code == 0
    assert out.startswith("[CARD] held by 1:") and "music job J1" in out
    assert "(running)" in out.splitlines()[-1]


def test_a_missing_file_is_said_with_exit_1(bridge, capsys):  # noqa: F811
    with pytest.raises(SystemExit) as stop:
        bridge.main(["--card"])

    assert stop.value.code == 1
    assert capsys.readouterr().out.startswith("[CARD] unknown: no card file at ")


def test_an_unreadable_file_is_said_with_exit_1(bridge, capsys):  # noqa: F811
    path = _write(bridge)
    path.write_text("{ torn", encoding="utf-8")

    with pytest.raises(SystemExit) as stop:
        bridge.main(["--card"])

    assert stop.value.code == 1
    assert "unreadable" in capsys.readouterr().out


def test_the_flag_is_in_the_help(bridge):  # noqa: F811
    assert "--card" in bridge._build_parser().format_help()
