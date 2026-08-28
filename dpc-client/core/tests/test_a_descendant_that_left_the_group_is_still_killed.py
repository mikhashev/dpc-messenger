"""`killpg` cannot reach a descendant that gave itself a new session.

Measured 2026-08-27 in a Linux container against the real `run_supervised`: a
grandchild started with `start_new_session=True` outlived the group kill, held
the inherited pipes, and made the call take 10 s against a 5 s timeout — our own
log naming it, «pipes still open 5s after killing pid 11 — a descendant escaped
the kill». With the parent-link walk it dies with everything else and the call
takes 5 s.

The same probe on Windows found no gap: `taskkill /T` walks parent links, which
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` does not break. The entry that
prompted this named `CREATE_BREAKAWAY_FROM_JOB`, which concerns Job objects and
defeats neither.
"""
import platform
import subprocess
import sys
import time
import types

import pytest

from dpc_client_core.dpc_agent.tools import process as proc

IS_WINDOWS = platform.system() == "Windows"


# --- the walk itself, on any platform ---


def test_the_walk_finds_a_grandchild_and_not_only_a_child(monkeypatch):
    """A child that called setsid keeps its parent link — that is the whole point."""
    monkeypatch.setattr(proc, "_parent_map_from_proc",
                        lambda: {10: [11, 12], 11: [13], 13: [14], 99: [100]})
    assert sorted(proc._descendants(10)) == [11, 12, 13, 14]


def test_a_process_with_no_children_walks_to_nothing(monkeypatch):
    monkeypatch.setattr(proc, "_parent_map_from_proc", lambda: {10: [11]})
    assert proc._descendants(11) == []


def test_the_walk_falls_back_to_ps_where_there_is_no_proc(monkeypatch):
    """macOS has no /proc and always has ps; a slim container is the reverse."""
    monkeypatch.setattr(proc, "_parent_map_from_proc", lambda: {})
    monkeypatch.setattr(proc, "_parent_map_from_ps", lambda pid: {7: [8]})
    assert proc._descendants(7) == [8]


def test_neither_source_is_an_empty_answer_rather_than_a_crash(monkeypatch):
    monkeypatch.setattr(proc, "_parent_map_from_proc", lambda: {})
    monkeypatch.setattr(proc, "_parent_map_from_ps", lambda pid: {})
    assert proc._descendants(7) == []


def test_ps_missing_is_survivable(monkeypatch):
    """`python:3.12-slim` carries no ps at all — measured, not assumed."""
    def _no_ps(*a, **k):
        raise FileNotFoundError("ps")

    monkeypatch.setattr(subprocess, "run", _no_ps)
    assert proc._parent_map_from_ps(1) == {}


def test_a_torn_proc_entry_does_not_lose_the_rest(monkeypatch, tmp_path):
    """A process can end between listing /proc and reading its status."""
    (tmp_path / "10").mkdir()
    (tmp_path / "10" / "status").write_text("Name:\tsh\nPPid:\t1\n", encoding="utf-8")
    (tmp_path / "11").mkdir()  # no status file: it ended while we looked
    (tmp_path / "self").mkdir()

    import os as _os
    monkeypatch.setattr(_os.path, "isdir", lambda p: True)
    monkeypatch.setattr(_os, "listdir", lambda p: ["10", "11", "self"])
    real_open = open

    def _open(path, *a, **k):
        return real_open(str(path).replace("/proc", str(tmp_path)).replace("\\proc", str(tmp_path)), *a, **k)

    monkeypatch.setattr("builtins.open", _open)
    assert proc._parent_map_from_proc() == {1: [10]}


# --- and the thing itself, where the platform allows it ---


@pytest.mark.skipif(IS_WINDOWS, reason="killpg and setsid are POSIX; Windows has no gap here")
def test_a_setsid_grandchild_is_killed_with_the_command(tmp_path):
    marker = "SETSID_GRANDCHILD_PROBE"
    spawner = tmp_path / "spawner.py"
    spawner.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c',\n"
        f"                  'import time; time.sleep(30)  # {marker}'],\n"
        "                 start_new_session=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    def alive():
        out = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True, text=True)
        return [l for l in out.stdout.splitlines() if marker in l and "ps -Ao" not in l]

    assert alive() == [], "control: nothing carrying the marker before the run"
    started = time.monotonic()
    run = proc.run_supervised(f"{sys.executable} {spawner}",
                              launcher="test", timeout=5, shell=True)
    elapsed = time.monotonic() - started
    time.sleep(1)

    assert alive() == [], "a descendant in its own session outlived the kill"
    assert elapsed < 8, f"the drain waited on an escaped descendant ({elapsed:.1f}s)"
    assert "killed" in (run.killed or "")


@pytest.mark.skipif(not IS_WINDOWS, reason="the /proc reader is a POSIX path")
def test_there_is_no_proc_on_windows_and_that_is_not_an_error():
    assert proc._parent_map_from_proc() == {}
