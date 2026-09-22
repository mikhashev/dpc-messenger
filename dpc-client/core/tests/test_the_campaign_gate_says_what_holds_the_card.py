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
               "32848, C:\\Users\\mikha\\.dpc\\bin\\llama.cpp\\<pin>\\llama-server.exe\n"
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


# --- A queue that spends itself on a configuration error ------------------
# 2026-08-30: an HF token that was the placeholder from a chat message. Four
# runs, exit 1 each, 25 seconds, and the screen read `-> None/None = None`
# four times. The operator went to bed believing a campaign was running.


def _fake_run_one(records):
    """Stand in for run_one, handing back prepared records in order."""
    it = iter(records)

    # `*_settings`: since 2026-09-23 the campaign passes the alias and limits on.
    def run_one(cfg, deadline, stamp, *_settings):
        return next(it)

    return run_one


class _Preflight:
    """The checks pass, so these tests reach the queue they are about."""

    def __init__(self, ok=True):
        self.ok = ok

    def run_checks(self, *a, **k):
        return [("OK" if self.ok else "FAIL", "alias", "stub")]

    def print_checks(self, checks):
        for status, name, detail in checks:
            print(f"  {status} {name} {detail}")

    def all_ok(self, checks):
        return self.ok


def _record(name, exit_code, minutes):
    return {"name": name, "temperature": 0.0, "reasoning_effort": "xhigh",
            "minutes": minutes, "exit_code": exit_code,
            "json": f"{name}.json"}


def _run_main(monkeypatch, tmp_path, records, argv=("--hours", "24"), preflight_ok=True):
    monkeypatch.setattr(campaign, "RESULTS", tmp_path)
    monkeypatch.setattr(campaign, "wait_for_gpu", lambda *a, **k: True)
    monkeypatch.setattr(campaign, "run_one", _fake_run_one(records))
    monkeypatch.setitem(sys.modules, "preflight", _Preflight(preflight_ok))
    monkeypatch.setattr(sys, "argv", ["campaign.py", *argv])
    return campaign.main()


def test_a_fast_failure_stops_the_queue(monkeypatch, tmp_path, capsys):
    records = [_record(cfg["name"], 1, 0.1) for cfg in campaign.QUEUE]

    _run_main(monkeypatch, tmp_path, records)

    out = capsys.readouterr().out
    assert "stopping the queue" in out, out
    assert out.count("FAILED (exit 1)") <= 2, "only the first run should have been started"


def test_a_slow_failure_does_not_stop_the_queue(monkeypatch, tmp_path, capsys):
    """A run that dies at minute 90 is a bad run, not a bad configuration."""
    records = [_record(campaign.QUEUE[0]["name"], 1, 90.0)] + [
        _record(cfg["name"], 0, 100.0) for cfg in campaign.QUEUE[1:]
    ]

    _run_main(monkeypatch, tmp_path, records)

    out = capsys.readouterr().out
    assert "stopping the queue" not in out, out
    assert out.count("FAILED") >= 1


def test_a_failure_says_so_on_screen_rather_than_none(monkeypatch, tmp_path, capsys):
    records = [_record(cfg["name"], 1, 0.1) for cfg in campaign.QUEUE]

    _run_main(monkeypatch, tmp_path, records)

    out = capsys.readouterr().out
    assert "FAILED (exit 1)" in out
    assert "None/None = None" not in out, "a failed run must not read as a score"


# --- one command, unattended, 2026-09-23 --------------------------------------
# The queue named `qwen3.8 27b Mythos`, an alias the providers file no longer
# held, so every run would have died after the dataset download. The alias is
# now an argument, checked before anything starts, and the night ends with one
# status a caller can read.


def test_the_runs_use_the_alias_the_campaign_was_given(tmp_path):
    cmd = campaign.runner_command(campaign.QUEUE[0], tmp_path / "r.json",
                                  {"alias": "some alias", "limit": 3})
    assert cmd[cmd.index("--provider-alias") + 1] == "some alias"
    assert cmd[cmd.index("--limit") + 1] == "3"
    assert "Mythos" not in " ".join(cmd)
    assert campaign.DEFAULT_ALIAS == "qwen3.8 27b"


def test_a_refused_preflight_starts_nothing(monkeypatch, tmp_path, capsys):
    code = _run_main(monkeypatch, tmp_path, [], preflight_ok=False)

    assert code == campaign.PREFLIGHT_EXIT
    assert "not started" in capsys.readouterr().out


def test_a_dry_run_starts_nothing_even_when_the_checks_pass(monkeypatch, tmp_path, capsys):
    code = _run_main(monkeypatch, tmp_path, [], argv=("--dry-run",))

    assert code == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "campaign until" not in out


@pytest.mark.parametrize("codes, never_free, expected", [
    ([0, 0], False, 0),
    ([0, 3], False, 3),
    ([0, 1], False, 1),
    ([3, -9], False, 1),
    ([], True, 4),
    ([], False, 4),
    ([0], True, 4),
])
def test_the_night_ends_with_one_status_worst_first(codes, never_free, expected):
    done = [{"exit_code": c} for c in codes]
    assert campaign.campaign_exit(done, never_free) == expected


def test_the_summary_line_carries_the_exit(monkeypatch, tmp_path, capsys):
    records = [_record(cfg["name"], 0, 100.0) for cfg in campaign.QUEUE]

    code = _run_main(monkeypatch, tmp_path, records)

    assert code == 0
    assert "campaign exit 0" in capsys.readouterr().out


def test_a_run_that_never_returns_is_killed_with_its_tree(monkeypatch, tmp_path, capsys):
    """A hung run held the night; now it is stopped, children included."""
    killed = []

    class _Hung:
        pid = 4242

        def __init__(self, *a, **k):
            pass

        def wait(self, timeout=None):
            if not killed:
                raise subprocess.TimeoutExpired("uv", timeout)
            return -1

    monkeypatch.setattr(campaign, "RESULTS", tmp_path)
    monkeypatch.setattr(campaign.subprocess, "Popen", _Hung)
    monkeypatch.setattr(campaign, "_kill_tree", lambda proc: killed.append(proc.pid))

    record = campaign.run_one(campaign.QUEUE[0], datetime.now(), "stamp",
                              {"alias": "a", "run_timeout_minutes": 0.01})

    assert killed == [4242]
    assert record["exit_code"] == campaign.TIMED_OUT
    assert "timed out" in capsys.readouterr().out


def test_the_wait_line_says_to_stop_the_service(clock, monkeypatch, capsys):
    _card(monkeypatch, free_mib=3000, holders=["llama-server.exe (pid 1)"])

    campaign.wait_for_gpu(clock.now() + timedelta(hours=8), budget_minutes=5)

    assert "Stop the DPC service" in capsys.readouterr().out
