"""A queue that waits for a resource must say what is holding it, and stop.

The 2026-08-25 campaign log carries 598 identical `waiting for the GPU (28 848
MiB in use)` lines, then the deadline, then an empty summary — no run at all.
On 2026-08-27 the same gate waited from 06:19 to 12:03 on the server its own
run had just left, and started one of four.

Two things follow. The number the gate asks for has to be the one that answers
"can my model load" — free VRAM, not whether the card looks idle, which a
resident server can never satisfy while doing its job. And a wait has to end.
"""
import subprocess
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "eval" / "gaia"))

import campaign  # noqa: E402


class _Clock:
    """A clock the test advances, so a half-hour budget costs no half-hour."""

    def __init__(self):
        self.t = datetime(2026, 8, 27, 4, 33, 0)

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(campaign, "datetime", types.SimpleNamespace(now=c.now))
    monkeypatch.setattr(campaign.time, "sleep", lambda s: c.advance(s))
    return c


def _card(monkeypatch, free_mib, holders=()):
    monkeypatch.setattr(campaign, "gpu_free_mib", lambda: free_mib)
    monkeypatch.setattr(campaign, "gpu_holder_candidates", lambda: list(holders))


# --- the gate opens on the question that matters ---


def test_the_gate_opens_when_there_is_room_for_the_model(clock, monkeypatch):
    _card(monkeypatch, free_mib=30000)
    assert campaign.wait_for_gpu(clock.now() + timedelta(hours=8)) is True


def test_a_resident_server_does_not_open_the_gate(clock, monkeypatch):
    """23 GB held leaves 9 GB free, and the model needs 26."""
    _card(monkeypatch, free_mib=9000, holders=["llama-server.exe (pid 32848)"])
    assert campaign.wait_for_gpu(clock.now() + timedelta(hours=8), budget_minutes=5) is False


# --- it says what is holding the card ---


def test_the_waiting_line_names_the_holder_and_not_only_a_number(clock, monkeypatch, capsys):
    _card(monkeypatch, free_mib=2803, holders=["llama-server.exe (pid 32848)"])

    campaign.wait_for_gpu(clock.now() + timedelta(hours=8), budget_minutes=5)

    out = capsys.readouterr().out
    assert "llama-server.exe (pid 32848)" in out, "the log said only how many MiB"
    assert "2803 MiB free" in out
    assert "26000 needed" in out


def test_it_says_so_when_nothing_names_itself(clock, monkeypatch, capsys):
    """Windows does not attribute VRAM per process, so an empty list is normal."""
    _card(monkeypatch, free_mib=100, holders=[])

    campaign.wait_for_gpu(clock.now() + timedelta(hours=8), budget_minutes=5)

    assert "no compute process named it" in capsys.readouterr().out


def test_the_waiting_line_is_not_printed_once_a_minute(clock, monkeypatch, capsys):
    """598 identical lines is what this replaces."""
    _card(monkeypatch, free_mib=100, holders=["llama-server.exe (pid 1)"])

    campaign.wait_for_gpu(clock.now() + timedelta(hours=8), budget_minutes=30)

    waiting_lines = [l for l in capsys.readouterr().out.splitlines() if "waiting for the GPU" in l]
    assert 1 <= len(waiting_lines) <= 8, f"30 polls printed {len(waiting_lines)} lines"


# --- and the wait ends ---


def test_the_wait_is_bounded_and_says_it_gave_up(clock, monkeypatch, capsys):
    _card(monkeypatch, free_mib=100, holders=["llama-server.exe (pid 1)"])
    started = clock.now()

    assert campaign.wait_for_gpu(started + timedelta(hours=8), budget_minutes=30) is False

    assert "giving up on the GPU" in capsys.readouterr().out
    assert clock.now() - started < timedelta(minutes=40), "it waited past its budget"


def test_the_deadline_still_wins_when_it_comes_first(clock, monkeypatch, capsys):
    _card(monkeypatch, free_mib=100)
    started = clock.now()

    assert campaign.wait_for_gpu(started + timedelta(minutes=5), budget_minutes=120) is False

    assert "giving up on the GPU" not in capsys.readouterr().out
    assert clock.now() - started < timedelta(minutes=10)


# --- unknown is not plenty ---


def test_a_card_it_cannot_read_is_treated_as_full(monkeypatch):
    def _boom(*a, **k):
        raise OSError("the driver said no")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert campaign.gpu_free_mib() == 0
    assert campaign.gpu_holder_candidates() == []


def test_unparsable_output_is_also_treated_as_full(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="\n"))
    assert campaign.gpu_free_mib() == 0


# --- a machine with no NVIDIA card at all is a third answer ---


def test_no_nvidia_smi_is_not_the_same_as_a_full_card(monkeypatch):
    """A Mac or an AMD box has no VRAM to contend for; the gate must not wall it."""
    def _missing(*a, **k):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(subprocess, "run", _missing)
    assert campaign.gpu_free_mib() is None


def test_the_gate_opens_where_there_is_no_such_card(clock, monkeypatch, capsys):
    monkeypatch.setattr(campaign, "gpu_free_mib", lambda: None)
    monkeypatch.setattr(campaign, "gpu_holder_candidates", lambda: [])

    assert campaign.wait_for_gpu(clock.now() + timedelta(hours=8)) is True
    assert "the VRAM gate does not apply" in capsys.readouterr().out


def test_only_compute_processes_are_named(monkeypatch):
    """The card lists every windowed app; naming explorer.exe helps nobody."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout="14248, C:\\Windows\\explorer.exe\n"
               "32848, C:\\Users\\mikha\\.dpc\\bin\\llama.cpp\\b10566\\llama-server.exe\n"
               "45312, C:\\Python312\\python.exe\n"))

    named = campaign.gpu_holder_candidates()
    assert named == ["llama-server.exe (pid 32848)", "python.exe (pid 45312)"]


def test_a_posix_path_is_trimmed_the_same_way(monkeypatch):
    """The first version split on a backslash only, which is a Windows answer."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout="7040, /usr/local/bin/llama-server\n"
               "9001, /usr/bin/python3.12\n"
               "4242, /usr/lib/firefox/firefox\n"))

    assert campaign.gpu_holder_candidates() == [
        "llama-server (pid 7040)", "python3.12 (pid 9001)"]
