"""ADR-046 points 3, 4, 6 and 7: one list of the backend's children, one word for each end.

What is asserted, in the order the ADR gives it: the registry itself (entries,
the closed vocabulary, persistence), the spawn origin reaching it through the real
tool pool, `run_supervised` and the two supervisors ending their children with the
right word, the startup sweep, and the shutdown order. Real processes where the
point is a real exit (a signal, a timeout, a memory ceiling, a sweep); fakes where
the point is which code path sent the stop.

Nothing here touches `~/.dpc`: the default registry is in memory and the persisted
ones live in `tmp_path`. A test never kills a process it did not start; the sweep
tests that must not kill (own pid, an ancestor) pass a recording `kill`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dpc_client_core import process_registry as pr
from dpc_client_core.dpc_agent.tools import process as process_mod
from dpc_client_core.dpc_agent.tools.process import run_supervised
from dpc_client_core.process_registry import (
    EXIT_REASONS,
    SPAWN_ORIGIN,
    STOP_FIELD,
    STOP_VALUES,
    ProcessEntry,
    ProcessRegistry,
    ProcessRegistryClosed,
    SpawnOrigin,
)

WINDOWS = sys.platform == "win32"
SLEEP = [sys.executable, "-c", "import time; time.sleep(600)"]


def _stop_of(entry):
    return getattr(entry, STOP_FIELD)


def _wait(predicate, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def registry():
    return pr.get_registry()


@pytest.fixture
def started():
    """Processes a test starts by hand, killed at the end whatever happens."""
    procs = []
    yield procs
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()


# =========================================================== the registry itself

class TestTheEntry:
    def test_a_spawn_is_recorded_with_its_identity_parent_and_command(self, registry, caplog):
        origin = SpawnOrigin(agent_id="agent_007", conversation_id="conv-1", tool="run_shell", round=3)
        with caplog.at_level(logging.INFO, logger="dpc_client_core.process_registry"):
            entry = registry.register(
                7_000_001, launcher="run_shell", launcher_kind="tool",
                command="git   status\n--short", origin=origin, detail="timeout 60s, ceiling 8192 MB",
            )
        assert (entry.pid, entry.launcher, entry.launcher_kind) == (7_000_001, "run_shell", "tool")
        assert (entry.agent_id, entry.conversation_id, entry.tool, entry.round) == ("agent_007", "conv-1", "run_shell", 3)
        assert entry.command == "git status --short", "a command is one bounded line"
        assert entry.parent_pid == os.getpid(), "the parent of a child is the backend"
        assert entry.parent_start_time == pr.create_time_of(os.getpid())
        assert entry.started_at and entry.ended_at is None and entry.exit_reason is None
        # Present, and None until the sampler exists.
        for field in ("peak_rss_mb", "peak_commit_mb", "peak_vram_mib", "cpu_seconds",
                      "io_read_bytes", "io_write_bytes", "last_output_at"):
            assert getattr(entry, field) is None
        assert caplog.messages == [
            "spawned pid 7000001 for run_shell (tool; agent_id=agent_007 conversation_id=conv-1 "
            "tool=run_shell round=3; timeout 60s, ceiling 8192 MB): git status --short"
        ]

    def test_a_command_is_bounded(self, registry):
        entry = registry.register(7_000_002, launcher="run_shell", launcher_kind="tool",
                                  command="x" * 5000, origin=SpawnOrigin())
        assert len(entry.command) == pr.COMMAND_MAX_CHARS

    def test_a_subsystem_names_its_parent_and_a_made_up_one_is_refused(self, registry, caplog):
        with caplog.at_level(logging.INFO, logger="dpc_client_core.process_registry"):
            entry = registry.register(7_000_003, launcher="strata", launcher_kind="subsystem", command=["a", "b"])
        assert entry.agent_id is None and entry.parent_pid == os.getpid()
        assert caplog.messages[0].startswith(f"spawned pid 7000003 for strata (subsystem; parent pid {os.getpid()}")
        with pytest.raises(ValueError):
            registry.register(7_000_004, launcher="device_probe", launcher_kind="subsystem", command="x")
        with pytest.raises(ValueError):
            registry.register(7_000_005, launcher="x", launcher_kind="daemon", command="x")

    def test_the_backends_own_pid_is_not_one_of_its_children(self, registry):
        with pytest.raises(ValueError):
            registry.register(os.getpid(), launcher="run_shell", launcher_kind="tool", command="x", origin=SpawnOrigin())

    def test_a_tool_child_without_an_origin_is_a_lost_attribution(self, registry, caplog):
        with caplog.at_level(logging.INFO, logger="dpc_client_core.process_registry"):
            registry.register(7_000_006, launcher="run_shell", launcher_kind="tool", command="x")
            registry.register(7_000_007, launcher="run_shell", launcher_kind="tool", command="x",
                              origin=SpawnOrigin(agent_id="a"))
        lost = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(lost) == 1 and "lost attribution" in lost[0].message and "pid 7000006" in lost[0].message

    def test_the_origin_is_read_from_the_context_variable(self, registry):
        with pr.spawn_origin(SpawnOrigin(agent_id="a1", conversation_id="c1", tool="git_tool(sandbox)", round=2)):
            entry = registry.register(7_000_008, launcher="git_tool(sandbox)", launcher_kind="tool", command="git")
        assert (entry.agent_id, entry.conversation_id, entry.round) == ("a1", "c1", 2)
        assert SPAWN_ORIGIN.get() is None, "the origin is restored when the block ends"


class TestTheClosedVocabulary:
    @pytest.mark.parametrize("reason", [r for r in EXIT_REASONS if r != "stopped"])
    def test_every_exit_reason_can_end_a_process(self, registry, reason):
        registry.register(7_100_000, launcher="strata", launcher_kind="subsystem", command="x")
        entry = registry.end(7_100_000, 0, reason=reason)
        assert entry.exit_reason == reason and _stop_of(entry) is None and entry.ended_at

    @pytest.mark.parametrize("stop", STOP_VALUES)
    def test_every_stop_kind_goes_with_stopped(self, registry, stop):
        registry.register(7_100_001, launcher="strata", launcher_kind="subsystem", command="x")
        entry = registry.end(7_100_001, 0, reason="stopped", stop=stop)
        assert (entry.exit_reason, _stop_of(entry)) == ("stopped", stop)

    def test_a_word_outside_the_list_is_refused(self, registry):
        registry.register(7_100_002, launcher="strata", launcher_kind="subsystem", command="x")
        with pytest.raises(ValueError):
            registry.end(7_100_002, 0, reason="killed")
        with pytest.raises(ValueError):
            registry.end(7_100_002, 0, reason="stopped")                  # no stop reason
        with pytest.raises(ValueError):
            registry.end(7_100_002, 0, reason="exited", stop="ui_stop")  # a stop reason only with stopped
        with pytest.raises(ValueError):
            registry.note_stop_sent(7_100_002, "stopped", "because")
        assert registry.open_entries(), "a refused word closes nothing"

    def test_the_stop_field_is_named_once(self):
        """The constant and the dataclass field are the two places that name it."""
        from dataclasses import fields

        assert STOP_FIELD in {f.name for f in fields(ProcessEntry)}
        assert STOP_FIELD in ProcessEntry(1, None, None, None, "x", "tool").to_dict()

    def test_the_field_is_stop_kind_so_as_not_to_collide_with_the_gateways_wire_field(self):
        written = ProcessEntry(1, None, None, None, "x", "tool").to_dict()
        assert STOP_FIELD == "stop_kind" and "stop_kind" in written
        assert "stop_reason" not in written, "stop_reason is the Anthropic field the gateway writes"

    def test_the_list_holds_what_something_produces_and_engine_switch_is_not_in_it(self, registry):
        assert set(STOP_VALUES) == {"idle_unload", "ui_stop", "engine_died", "failed_start", "browser_closed"}
        registry.register(7_100_003, launcher="strata", launcher_kind="subsystem", command="x")
        with pytest.raises(ValueError):
            registry.end(7_100_003, 0, reason="stopped", stop="engine_switch")
        with pytest.raises(ValueError):
            registry.note_stop_sent(7_100_003, "stopped", "engine_switch")
        assert registry.open_entries(), "a refused word closes nothing"


class TestHowAnEndGetsItsWord:
    def test_an_ordinary_code_is_exited_whatever_its_value(self, registry):
        registry.register(7_200_001, launcher="strata", launcher_kind="subsystem", command="x")
        registry.register(7_200_002, launcher="strata", launcher_kind="subsystem", command="x")
        assert registry.end(7_200_001, 0).exit_reason == "exited"
        nonzero = registry.end(7_200_002, 3)
        assert (nonzero.exit_reason, nonzero.exit_code) == ("exited", 3)

    @pytest.mark.parametrize("platform,code,abnormal", [
        ("win32", 0xC0000005, True),         # access violation
        ("win32", 3221225477, True),         # the same, as an unsigned int
        ("win32", -1073741819, True),        # the same, as Python's signed view
        ("win32", 0xC0000409, True),
        ("win32", 0xC000013A, True),         # control-C exit, when nobody sent a stop
        ("win32", 1, False),
        ("win32", -1, False),                # exit(-1): a code a program chose
        ("win32", 0xFFFFFFFE, False),
        ("win32", 0, False),
        ("linux", -9, True),
        ("linux", -11, True),
        ("linux", 1, False),
        ("linux", 0, False),
        ("darwin", -6, True),
    ])
    def test_what_counts_as_a_death(self, platform, code, abnormal):
        assert pr.is_abnormal_exit(code, platform) is abnormal

    def test_a_death_nobody_sent_is_crashed_and_stores_code_and_signal(self):
        posix = ProcessRegistry(platform="linux")
        posix.register(7_200_010, launcher="llamacpp_server", launcher_kind="subsystem", command="x")
        died = posix.end(7_200_010, -9)
        assert (died.exit_reason, died.exit_code, died.signal) == ("crashed", -9, "SIGKILL")
        windows = ProcessRegistry(platform="win32")
        windows.register(7_200_011, launcher="strata", launcher_kind="subsystem", command="x")
        died = windows.end(7_200_011, -1073741819)
        assert (died.exit_reason, died.exit_code, died.signal) == ("crashed", 0xC0000005, None)

    def test_a_stop_that_was_sent_is_never_read_as_a_crash(self):
        """A SIGKILL from a timeout and the CTRL_BREAK a supervisor sends both look like deaths."""
        posix = ProcessRegistry(platform="linux")
        posix.register(7_200_020, launcher="run_shell", launcher_kind="tool", command="x", origin=SpawnOrigin())
        posix.note_stop_sent(7_200_020, "timeout")
        assert posix.end(7_200_020, -9).exit_reason == "timeout"
        windows = ProcessRegistry(platform="win32")
        windows.register(7_200_021, launcher="strata", launcher_kind="subsystem", command="x")
        windows.note_stop_sent(7_200_021, "stopped", "ui_stop")
        sent = windows.end(7_200_021, 0xC000013A)
        assert (sent.exit_reason, _stop_of(sent)) == ("stopped", "ui_stop")

    def test_the_first_end_stands_and_a_later_one_only_adds_a_missing_code(self, registry):
        registry.register(7_200_030, launcher="run_shell", launcher_kind="tool", command="x", origin=SpawnOrigin())
        registry.note_stop_sent(7_200_030, "memory_ceiling")
        registry.note_stop_sent(7_200_030, "timeout")                        # the second announcement is ignored
        first = registry.end(7_200_030, None, reason="memory_ceiling")
        assert first.exit_code is None, "an end this backend did not see carries no code"
        again = registry.end(7_200_030, 1, reason="exited")
        assert again is first and again.exit_reason == "memory_ceiling" and again.exit_code == 1

    def test_a_pid_the_registry_never_saw_is_not_an_error(self, registry):
        assert registry.end(7_299_999, 0) is None


class TestPrecedence:
    @pytest.mark.parametrize("stop_sent", [("stopped", "ui_stop"), ("cancelled_by_agent", None)])
    def test_a_stop_announced_after_the_orderly_stop_began_is_backend_stop_whoever_signalled(self, registry, stop_sent):
        registry.register(7_300_001, launcher="strata", launcher_kind="subsystem", command="x")
        registry.begin_shutdown()
        registry.note_stop_sent(7_300_001, *stop_sent)
        ended = registry.end(7_300_001, 0)
        assert (ended.exit_reason, _stop_of(ended)) == ("backend_stop", None)

    def test_a_failed_start_still_being_stopped_when_the_backends_stop_began_stays_failed_start(self, registry):
        registry.register(7_300_005, launcher="strata", launcher_kind="subsystem", command="x")
        registry.note_stop_sent(7_300_005, "stopped", "failed_start")
        registry.begin_shutdown()
        ended = registry.end(7_300_005, 0)
        assert (ended.exit_reason, _stop_of(ended)) == ("stopped", "failed_start")

    @pytest.mark.parametrize("stop_sent", [("stopped", "ui_stop"), ("cancelled_by_agent", None)])
    def test_every_word_a_sender_may_give_keeps_its_announcement_made_before_the_stop(self, registry, stop_sent):
        registry.register(7_300_006, launcher="run_shell", launcher_kind="tool", command="x", origin=SpawnOrigin())
        registry.note_stop_sent(7_300_006, *stop_sent)
        registry.begin_shutdown()
        ended = registry.end(7_300_006, 0)
        assert (ended.exit_reason, _stop_of(ended)) == (stop_sent[0], stop_sent[1])

    def test_the_first_announcement_decides_which_side_of_the_window_a_stop_is_on(self, registry):
        registry.register(7_300_007, launcher="strata", launcher_kind="subsystem", command="x")
        registry.begin_shutdown()
        registry.note_stop_sent(7_300_007, "stopped", "ui_stop")
        registry.note_stop_sent(7_300_007, "stopped", "failed_start")  # ignored: the first stands
        ended = registry.end(7_300_007, 0)
        assert (ended.exit_reason, _stop_of(ended)) == ("backend_stop", None)

    def test_while_the_backend_runs_it_is_stopped(self, registry):
        registry.register(7_300_002, launcher="strata", launcher_kind="subsystem", command="x")
        registry.note_stop_sent(7_300_002, "stopped", "ui_stop")
        assert registry.end(7_300_002, 0).exit_reason == "stopped"

    def test_a_timeout_keeps_its_own_word_during_the_stop(self, registry):
        registry.register(7_300_003, launcher="run_shell", launcher_kind="tool", command="x", origin=SpawnOrigin())
        registry.note_stop_sent(7_300_003, "timeout")
        registry.begin_shutdown()
        assert registry.end(7_300_003, 1).exit_reason == "timeout"

    def test_nothing_is_registered_once_the_stop_has_begun(self, registry):
        registry.begin_shutdown()
        with pytest.raises(ProcessRegistryClosed):
            registry.register(7_300_004, launcher="strata", launcher_kind="subsystem", command="x")
        with pytest.raises(ProcessRegistryClosed):
            registry.check_open()


class TestTheLogLines:
    def test_the_end_line_names_pid_launcher_reason_stop_kind_code_and_signal(self, caplog):
        registry = ProcessRegistry(platform="linux")
        with caplog.at_level(logging.INFO, logger="dpc_client_core.process_registry"):
            registry.register(7_400_001, launcher="llamacpp_server", launcher_kind="subsystem", command="x")
            registry.note_stop_sent(7_400_001, "stopped", "ui_stop")
            registry.end(7_400_001, -2)
            registry.register(7_400_002, launcher="strata", launcher_kind="subsystem", command="x")
            registry.end(7_400_002, -9)
            registry.register(7_400_003, launcher="strata", launcher_kind="subsystem", command="x")
            registry.end(7_400_003, None, reason="backend_stop")
        ends = [r for r in caplog.records if r.message.startswith("process ended")]
        assert [r.message.split(" after ")[0] for r in ends] == [
            "process ended: pid 7400001 launcher llamacpp_server reason stopped stop_kind ui_stop exit_code -2 signal SIGINT",
            "process ended: pid 7400002 launcher strata reason crashed stop_kind none exit_code -9 signal SIGKILL",
            "process ended: pid 7400003 launcher strata reason backend_stop stop_kind none exit_code unseen signal none",
        ]
        assert [r.levelno for r in ends] == [logging.INFO, logging.WARNING, logging.INFO], "a crash is a WARNING"


class TestPersistence:
    def test_every_spawn_and_every_end_is_written_and_reloads(self, tmp_path):
        path = tmp_path / "process_registry.json"
        first = ProcessRegistry(path)
        first.register(7_500_001, launcher="run_shell", launcher_kind="tool", command="a", origin=SpawnOrigin(agent_id="x"))
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert [e["pid"] for e in on_disk["entries"]] == [7_500_001] and on_disk["entries"][0]["ended_at"] is None
        first.end(7_500_001, 0)
        assert json.loads(path.read_text(encoding="utf-8"))["entries"][0]["exit_reason"] == "exited"
        first.register(7_500_002, launcher="strata", launcher_kind="subsystem", command="b")
        second = ProcessRegistry(path)
        second.load()
        assert [(e.pid, e.exit_reason) for e in second.entries()] == [(7_500_001, "exited"), (7_500_002, None)]

    def test_a_write_that_fails_before_the_replace_leaves_the_old_list(self, tmp_path, monkeypatch):
        """Never a truncating open of the file itself: the list is written beside it and swapped in."""
        path = tmp_path / "process_registry.json"
        registry = ProcessRegistry(path)
        registry.register(7_500_010, launcher="strata", launcher_kind="subsystem", command="a")
        before = path.read_bytes()

        def refuse(src, dst):
            raise PermissionError("the swap was refused")

        monkeypatch.setattr(pr.os, "replace", refuse)
        monkeypatch.setattr(pr.time, "sleep", lambda s: None)
        registry.register(7_500_011, launcher="strata", launcher_kind="subsystem", command="b")
        assert path.read_bytes() == before, "the live file was touched before the swap"
        assert [p.name for p in tmp_path.iterdir()] == [path.name], "a failed write leaves no temporary file"
        assert {e.pid for e in registry.open_entries()} == {7_500_010, 7_500_011}, "the list is kept in memory"

    def test_the_finished_entries_are_bounded_by_count_and_by_age_and_open_ones_never(self, tmp_path):
        now = [1_000_000_000.0]
        registry = ProcessRegistry(tmp_path / "r.json", clock=lambda: now[0], keep_finished_max=3, keep_finished_days=7)
        registry.register(7_600_000, launcher="strata", launcher_kind="subsystem", command="still open")
        for pid in range(7_600_001, 7_600_008):
            registry.register(pid, launcher="llamacpp_server", launcher_kind="subsystem", command="x")
            registry.end(pid, 0)
            now[0] += 60
        kept = [e.pid for e in registry.entries()]
        assert kept == [7_600_000, 7_600_005, 7_600_006, 7_600_007], "the newest three finished, and the open one"
        now[0] += 8 * 86400
        registry.register(7_600_100, launcher="strata", launcher_kind="subsystem", command="new")
        assert [e.pid for e in registry.entries()] == [7_600_000, 7_600_100], "older than 7 days is dropped, open is not"

    def test_a_memory_only_registry_never_writes_and_stays_bounded(self):
        registry = ProcessRegistry(keep_finished_max=5)
        for pid in range(7_650_000, 7_650_020):
            registry.register(pid, launcher="strata", launcher_kind="subsystem", command="x")
            registry.end(pid, 0)
        assert registry.path is None and len(registry.entries()) == 5

    def test_a_file_that_is_not_a_registry_is_set_aside_not_trusted(self, tmp_path):
        path = tmp_path / "process_registry.json"
        path.write_text("{ not json", encoding="utf-8")
        registry = ProcessRegistry(path)
        registry.load()
        outcome = registry.sweep_orphans()
        assert registry.entries() == []
        assert (tmp_path / "process_registry.json.corrupt").read_text(encoding="utf-8") == "{ not json"
        assert any("set aside" in m for _l, m in outcome.records)

    def test_the_default_registry_is_the_one_in_the_dpc_home_when_started(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DPC_HOME", str(tmp_path))
        assert pr.registry_path() == tmp_path / "process_registry.json"
        assert pr.get_registry().path is None, "tests and probes never write to ~/.dpc"
        outcome = pr.start_persistent()
        assert pr.get_registry().path == tmp_path / "process_registry.json" and (tmp_path / "process_registry.json").exists()
        assert outcome.records == []


def _utc(*args):
    from datetime import datetime, timezone

    return datetime(*args, tzinfo=timezone.utc).timestamp()


def _history_lines(directory):
    """Every parsed line of every daily file, oldest file first, with the file's name."""
    found = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            found.append((path.name, json.loads(line)))
    return found


class TestTheStateFileAndTheDailyHistory:
    def test_a_file_written_before_the_rename_is_read_with_its_old_key_as_stop_kind(self, tmp_path):
        old = ProcessEntry(7_900_001, None, None, None, "strata", "subsystem", command="x",
                           started_at="2026-10-25T10:00:00.000+00:00", ended_at="2026-10-25T10:05:00.000+00:00",
                           exit_reason="stopped", exit_code=0).to_dict()
        del old["stop_kind"]
        old["stop_reason"] = "ui_stop"
        both = dict(old, pid=7_900_002, stop_kind="failed_start")
        path = tmp_path / "process_registry.json"
        path.write_text(json.dumps({"schema": 1, "entries": [old, both]}), encoding="utf-8")
        registry = ProcessRegistry(path)
        registry.load()
        first, second = registry.entries()
        assert (first.exit_reason, _stop_of(first)) == ("stopped", "ui_stop"), "yesterday's key was lost"
        assert _stop_of(second) == "failed_start", "when both keys are there the new one wins"
        registry.register(7_900_003, launcher="strata", launcher_kind="subsystem", command="y")
        on_disk = json.loads(path.read_text(encoding="utf-8"))["entries"]
        assert [e["stop_kind"] for e in on_disk[:2]] == ["ui_stop", "failed_start"]
        assert all("stop_reason" not in e for e in on_disk), "the old key is read, never written back"

    def test_the_state_file_is_synced_before_it_is_swapped_in(self, tmp_path, monkeypatch):
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            events.append("fsync")
            return real_fsync(fd)

        def replace(src, dst):
            events.append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr(pr.os, "fsync", fsync)
        monkeypatch.setattr(pr.os, "replace", replace)
        registry = ProcessRegistry(tmp_path / "process_registry.json")
        registry.register(7_900_010, launcher="strata", launcher_kind="subsystem", command="x")
        assert events == ["fsync", "replace"], "the swap must come after the sync"
        registry.end(7_900_010, 0)
        assert events == ["fsync", "replace"] * 2

    def test_a_finished_entry_is_appended_as_one_line_to_the_file_of_the_day_it_ended(self, tmp_path):
        now = [_utc(2026, 10, 25, 23, 59, 58)]
        path = tmp_path / "process_registry.json"
        registry = ProcessRegistry(path, clock=lambda: now[0])
        assert registry.history_dir == tmp_path / "process_registry"
        registry.register(7_910_001, launcher="run_shell", launcher_kind="tool", command="a",
                          origin=SpawnOrigin(agent_id="agent_1", conversation_id="c", tool="run_shell", round=2))
        registry.register(7_910_002, launcher="strata", launcher_kind="subsystem", command="b")
        assert not registry.history_dir.exists(), "an open entry is not history yet"
        registry.note_stop_sent(7_910_002, "stopped", "ui_stop")
        registry.end(7_910_001, 0)
        now[0] = _utc(2026, 10, 26, 0, 0, 3)
        registry.end(7_910_002, 0)
        lines = _history_lines(registry.history_dir)
        assert [name for name, _row in lines] == ["2026-10-25.jsonl", "2026-10-26.jsonl"]
        first, second = (row for _name, row in lines)
        assert first == registry.entries()[0].to_dict() and second == registry.entries()[1].to_dict()
        assert (first["pid"], first["agent_id"], first["round"], first["exit_reason"], first["stop_kind"]) == (
            7_910_001, "agent_1", 2, "exited", None)
        assert (second["exit_reason"], second["stop_kind"], second["ended_at"][:10]) == ("stopped", "ui_stop", "2026-10-26")
        raw = (registry.history_dir / "2026-10-25.jsonl").read_bytes()
        assert raw.endswith(b"\n") and b"\r\n" not in raw, "one LF-terminated line, on Windows too"

    def test_the_history_keeps_what_the_state_file_drops(self, tmp_path):
        now = [_utc(2026, 10, 25, 12, 0, 0)]
        registry = ProcessRegistry(tmp_path / "process_registry.json", clock=lambda: now[0], keep_finished_max=3)
        for pid in range(7_920_001, 7_920_011):
            registry.register(pid, launcher="strata", launcher_kind="subsystem", command="x")
            registry.end(pid, 0)
            now[0] += 1
        assert len(registry.entries()) == 3
        assert [row["pid"] for _n, row in _history_lines(registry.history_dir)] == list(range(7_920_001, 7_920_011))

    def test_a_sweep_closing_an_entry_of_an_earlier_run_is_history_too(self, tmp_path):
        path = _file_with_an_open_entry(tmp_path, 7_930_001, 1_700_000_000.0)
        registry = ProcessRegistry(path)
        registry.load()
        registry.sweep_orphans()
        ((_name, row),) = _history_lines(registry.history_dir)
        assert (row["pid"], row["exit_reason"], row["exit_code"]) == (7_930_001, "backend_stop", None)

    def test_files_dated_more_than_seven_days_ago_are_removed_and_nothing_else(self, tmp_path):
        now = _utc(2026, 10, 25, 3, 0, 0)
        registry = ProcessRegistry(tmp_path / "process_registry.json", clock=lambda: now)
        folder = registry.history_dir
        folder.mkdir()
        for name in ("2026-10-17.jsonl", "2026-10-18.jsonl", "2026-10-25.jsonl", "notes.jsonl", "2026-10-10.jsonl.lock"):
            (folder / name).write_text("{}\n", encoding="utf-8")
        removed = registry.prune_history()
        assert sorted(p.name for p in removed) == ["2026-10-17.jsonl"]
        assert sorted(p.name for p in folder.iterdir()) == [
            "2026-10-10.jsonl.lock", "2026-10-18.jsonl", "2026-10-25.jsonl", "notes.jsonl"]

    def test_the_start_of_a_persistent_registry_prunes_the_history(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DPC_HOME", str(tmp_path))
        folder = tmp_path / "process_registry"
        folder.mkdir()
        (folder / "2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")
        pr.start_persistent()
        assert not (folder / "2020-01-01.jsonl").exists()
        assert pr.get_registry().history_dir == folder

    def test_a_memory_only_registry_has_no_history_and_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        registry = ProcessRegistry()
        assert registry.history_dir is None and pr.get_registry().history_dir is None
        registry.register(7_940_001, launcher="strata", launcher_kind="subsystem", command="x")
        registry.end(7_940_001, 0)
        assert registry.prune_history() == []
        assert list(tmp_path.iterdir()) == []

    def test_a_failed_append_is_logged_once_and_takes_neither_a_spawn_nor_an_end_down(self, tmp_path, caplog):
        path = tmp_path / "process_registry.json"
        (tmp_path / "process_registry").write_text("a file where the folder should be", encoding="utf-8")
        registry = ProcessRegistry(path)
        with caplog.at_level(logging.WARNING, logger="dpc_client_core.process_registry"):
            for pid in (7_950_001, 7_950_002, 7_950_003):
                registry.register(pid, launcher="strata", launcher_kind="subsystem", command="x")
                ended = registry.end(pid, 0)
                assert ended.exit_reason == "exited"
        said = [r for r in caplog.records if "history" in r.getMessage()]
        assert len(said) == 1 and said[0].levelno == logging.WARNING
        assert [e["pid"] for e in json.loads(path.read_text(encoding="utf-8"))["entries"]] == [7_950_001, 7_950_002, 7_950_003]

    def test_a_torn_last_line_does_not_swallow_the_next_append(self, tmp_path):
        now = _utc(2026, 10, 25, 12, 0, 0)
        registry = ProcessRegistry(tmp_path / "process_registry.json", clock=lambda: now)
        registry.history_dir.mkdir()
        torn = registry.history_dir / "2026-10-25.jsonl"
        torn.write_bytes(b'{"pid": 1, "launc')
        registry.register(7_960_001, launcher="strata", launcher_kind="subsystem", command="x")
        registry.end(7_960_001, 0)
        parsed = []
        for line in torn.read_text(encoding="utf-8").splitlines():
            try:
                parsed.append(json.loads(line)["pid"])
            except ValueError:
                pass
        assert parsed == [7_960_001], "the new line was glued to the torn one"

    def test_a_stale_lock_is_cleared_and_no_lock_is_left_behind(self, tmp_path):
        now = _utc(2026, 10, 25, 12, 0, 0)
        registry = ProcessRegistry(tmp_path / "process_registry.json", clock=lambda: now)
        registry.history_dir.mkdir()
        lock = registry.history_dir / "2026-10-25.jsonl.lock"
        lock.write_text("", encoding="utf-8")
        old = time.time() - 60
        os.utime(lock, (old, old))
        registry.register(7_970_001, launcher="strata", launcher_kind="subsystem", command="x")
        registry.end(7_970_001, 0)
        assert len(_history_lines(registry.history_dir)) == 1 and not lock.exists()


class TestAKillNobodyInDpcSent:
    """The honest asymmetry: what the registry can say about a process somebody else killed."""

    @pytest.mark.skipif(not WINDOWS, reason="taskkill is the Windows way of a foreign hard kill")
    def test_on_windows_a_foreign_taskkill_leaves_code_1_and_reads_exited(self, registry, started):
        child = subprocess.Popen(SLEEP)
        started.append(child)
        registry.register(child.pid, launcher="strata", launcher_kind="subsystem", command=SLEEP, handle=child)
        done = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
        assert done.returncode == 0, done.stderr
        code = child.wait(timeout=20)
        entry = registry.end(child.pid, code)
        assert (entry.exit_reason, entry.exit_code, entry.signal) == ("exited", 1, None)

    @pytest.mark.skipif(WINDOWS, reason="a signal from outside is the POSIX way of a foreign kill")
    def test_on_posix_a_foreign_sigkill_reads_crashed_with_the_signal_by_name(self, registry, started):
        child = subprocess.Popen(SLEEP)
        started.append(child)
        registry.register(child.pid, launcher="strata", launcher_kind="subsystem", command=SLEEP, handle=child)
        os.kill(child.pid, signal.SIGKILL)
        code = child.wait(timeout=20)
        entry = registry.end(child.pid, code)
        assert (entry.exit_reason, entry.exit_code, entry.signal) == ("crashed", -9, "SIGKILL")


# ================================================== the origin through the real pool

class _TheTool:
    """A ToolRegistry stand-in: `execute` runs in the pool thread, like the real one."""

    def __init__(self, work):
        self.work = work

    def execute(self, name, args, ctx=None):
        return self.work()


def _call(name="run_shell", call_id="call-1"):
    return {"id": call_id, "function": {"name": name, "arguments": "{}"}}


class TestTheOriginReachesThePoolThread:
    @pytest.mark.asyncio
    async def test_a_tool_that_spawns_is_attributed_to_its_agent_chat_tool_and_round(self, tmp_path, registry):
        from dpc_client_core.dpc_agent.loop import _execute_with_timeout

        logs = tmp_path / "agent_042" / "logs"
        logs.mkdir(parents=True)
        seen = {}

        def work():
            seen["thread"] = threading.current_thread().name
            run = run_supervised([sys.executable, "-c", "pass"], launcher="run_shell", timeout=30)
            return run.exit_reason

        out = await _execute_with_timeout(
            _TheTool(work), _call("run_shell"), logs, 30, task_id="t", round_number=4,
            session_id="conv-77", agent_id="agent_042",
        )
        assert out["result"] == "exited"
        assert seen["thread"].startswith("dpc_agent_tool"), "it ran on the real pool, not the caller's thread"
        (entry,) = registry.entries()
        assert (entry.agent_id, entry.conversation_id, entry.tool, entry.round) == ("agent_042", "conv-77", "run_shell", 4)
        assert entry.launcher == "run_shell" and entry.launcher_kind == "tool"

    @pytest.mark.asyncio
    async def test_the_agent_defaults_to_the_folder_the_logs_sit_in(self, tmp_path, registry):
        from dpc_client_core.dpc_agent.loop import _execute_with_timeout

        logs = tmp_path / "agent_9" / "logs"
        logs.mkdir(parents=True)
        await _execute_with_timeout(
            _TheTool(lambda: str(run_supervised([sys.executable, "-c", "pass"], launcher="read_document", timeout=30).ok)),
            _call("read_document"), logs, 30,
        )
        assert registry.entries()[0].agent_id == "agent_9"

    @pytest.mark.asyncio
    async def test_a_worker_carries_nothing_from_one_call_to_the_next(self, tmp_path):
        from dpc_client_core.dpc_agent.loop import _execute_with_timeout, _get_shared_executor

        logs = tmp_path / "agent_1" / "logs"
        logs.mkdir(parents=True)
        for i in range(8):
            await _execute_with_timeout(_TheTool(lambda: "ok"), _call(call_id=f"c{i}"), logs, 30, agent_id="agent_1")
        loop = asyncio.get_running_loop()
        leftovers = await asyncio.gather(*[
            loop.run_in_executor(_get_shared_executor(), SPAWN_ORIGIN.get) for _ in range(8)
        ])
        assert leftovers == [None] * 8


# ============================================================ run_supervised's reasons

def _wait_for_entry(registry, seconds=20.0):
    assert _wait(lambda: registry.open_entries("tool"), seconds), "the child was never registered"
    return registry.open_entries("tool")[0]


class TestRunSupervisedEndsItsChildWithAWord:
    def test_exit_zero(self, registry):
        run = run_supervised([sys.executable, "-c", "pass"], launcher="test(zero)", timeout=30)
        (entry,) = registry.entries()
        assert (entry.exit_reason, entry.exit_code, entry.signal) == ("exited", 0, None)
        assert run.exit_reason == "exited" and entry.start_time is not None

    def test_a_nonzero_exit_nobody_sent_is_exited_not_crashed(self, registry):
        run = run_supervised([sys.executable, "-c", "import sys; sys.exit(3)"], launcher="test(three)", timeout=30)
        (entry,) = registry.entries()
        assert (entry.exit_reason, entry.exit_code) == ("exited", 3) and run.exit_reason == "exited"

    def test_a_timeout_kill_is_timeout_and_on_posix_not_crashed(self, registry):
        run = run_supervised(SLEEP, launcher="test(timeout)", timeout=1, ceiling_mb=0)
        (entry,) = registry.entries()
        assert run.timed_out and run.exit_reason == "timeout"
        assert entry.exit_reason == "timeout"
        if not WINDOWS:
            assert (entry.exit_code, entry.signal) == (-9, "SIGKILL"), "SIGKILL by our own kill must not read as a crash"

    def test_the_memory_ceiling_kill_is_memory_ceiling(self, registry, monkeypatch):
        monkeypatch.setattr(process_mod, "_MEMORY_POLL_SECONDS", 0.1)
        allocator = ("import time\nb = []\nwhile True:\n"
                     "    b.append(bytearray(20 * 1024 * 1024))\n    time.sleep(0.1)\n")
        run = run_supervised([sys.executable, "-c", allocator], launcher="test(ceiling)", timeout=60, ceiling_mb=150)
        (entry,) = registry.entries()
        assert run.exceeded_mb and not run.timed_out
        assert entry.exit_reason == "memory_ceiling" and run.exit_reason == "memory_ceiling"

    def test_a_self_inflicted_abnormal_death_is_crashed(self, registry):
        if WINDOWS:
            body = "import os; os._exit(-1073741819)"        # 0xC0000005, an access violation
        else:
            body = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
        run = run_supervised([sys.executable, "-c", body], launcher="test(crash)", timeout=30, ceiling_mb=0)
        (entry,) = registry.entries()
        assert entry.exit_reason == "crashed" and run.exit_reason == "crashed"
        if WINDOWS:
            assert entry.exit_code == 0xC0000005 and entry.signal is None
        else:
            assert (entry.exit_code, entry.signal) == (-9, "SIGKILL")

    def test_the_start_line_keeps_its_prefix_and_gains_the_kind_and_origin(self, registry, caplog):
        with caplog.at_level(logging.INFO, logger=process_mod.log.name):
            with pr.spawn_origin(SpawnOrigin(agent_id="agent_5", conversation_id="conv-5", tool="run_shell", round=1)):
                run_supervised([sys.executable, "-c", "pass"], launcher="run_shell", timeout=30, ceiling_mb=0)
        (start,) = [m for m in caplog.messages if m.startswith("spawned pid")]
        assert " for run_shell (tool; agent_id=agent_5 conversation_id=conv-5 tool=run_shell round=1; timeout 30s, ceiling off MB): " in start
        assert any(m.startswith("process ended: pid ") and "launcher run_shell reason exited" in m for m in caplog.messages)

    def test_a_spawn_after_the_stop_began_is_refused_before_a_process_exists(self, registry, monkeypatch):
        registry.begin_shutdown()
        calls = []
        real = subprocess.Popen
        monkeypatch.setattr(process_mod.subprocess, "Popen", lambda *a, **k: calls.append(a) or real(*a, **k))
        with pytest.raises(ProcessRegistryClosed):
            run_supervised([sys.executable, "-c", "pass"], launcher="test(late)", timeout=30)
        assert calls == [], "a process was born after the stop began"


# ============================================================= the supervisors, with fakes

class _FakeChild:
    """Enough of an asyncio subprocess: a pid, a code that appears on `wait`."""

    _next = 8_000_000

    def __init__(self, code_on_wait=0):
        _FakeChild._next += 1
        self.pid = _FakeChild._next
        self.returncode = None
        self.code_on_wait = code_on_wait
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.signals.append("kill")

    async def wait(self):
        if self.returncode is None:
            self.returncode = self.code_on_wait
        return self.returncode


DEATH = 0xC0000005 if WINDOWS else -9


def _llama(tmp_path, monkeypatch):
    from dpc_client_core.managers import llama_server_supervisor as base

    monkeypatch.setattr(base, "DPC_HOME", tmp_path)
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"")
    return base.LlamaServerSupervisor("alias1", {"gguf_path": str(gguf)})


def _strata(tmp_path, monkeypatch):
    from dpc_client_core.managers import llama_server_supervisor as base
    from tests.test_strata_supervisor import _install, _sup

    monkeypatch.setattr(base, "DPC_HOME", tmp_path / "home")
    return _sup(_install(tmp_path))


def _exec_returning(monkeypatch, child):
    async def fake_exec(*cmd, **kw):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


class TestTheSupervisorsRegisterAndEndTheirChild:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("make,launcher", [(_llama, "llamacpp_server"), (_strata, "strata")])
    async def test_a_spawn_registers_a_subsystem_child_of_the_backend(self, tmp_path, monkeypatch, registry, make, launcher):
        sup = make(tmp_path, monkeypatch)
        child = _FakeChild()
        _exec_returning(monkeypatch, child)
        await sup._spawn(["engine", "--port", "1"], {})
        (entry,) = registry.open_entries("subsystem")
        assert (entry.pid, entry.launcher, entry.parent_pid) == (child.pid, launcher, os.getpid())
        assert entry.command == "engine --port 1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("make", [_llama, _strata])
    async def test_a_stop_nobody_qualified_is_a_ui_stop(self, tmp_path, monkeypatch, registry, make):
        sup = make(tmp_path, monkeypatch)
        _exec_returning(monkeypatch, _FakeChild())
        sup._proc = await sup._spawn(["e"], {})
        await sup.stop()
        (entry,) = registry.entries()
        assert (entry.exit_reason, _stop_of(entry)) == ("stopped", "ui_stop")
        assert entry.exit_code == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("make", [_llama, _strata])
    async def test_a_start_that_failed_stops_its_child_as_failed_start(self, tmp_path, monkeypatch, registry, make):
        sup = make(tmp_path, monkeypatch)
        _exec_returning(monkeypatch, _FakeChild())
        proc = sup._proc = await sup._spawn(["e"], {})
        await sup._stop_the_spawned(proc)
        (entry,) = registry.entries()
        assert (entry.exit_reason, _stop_of(entry)) == ("stopped", "failed_start")

    @pytest.mark.asyncio
    async def test_a_strata_engine_that_died_after_the_start_succeeded_is_stopped_as_engine_died(self, tmp_path, monkeypatch, registry):
        """The watcher's unloaded state and the 503 path both reach `_fail_closed` with no `refused_start`."""
        sup = _strata(tmp_path, monkeypatch)
        _exec_returning(monkeypatch, _FakeChild())
        sup._proc = await sup._spawn(["e"], {})
        await sup._fail_closed("the launcher reports the engine unloaded")
        (entry,) = registry.entries()
        assert (entry.exit_reason, _stop_of(entry)) == ("stopped", "engine_died")

    @pytest.mark.asyncio
    async def test_a_503_naming_the_exit_code_stops_the_launcher_as_engine_died(self, tmp_path, monkeypatch, registry):
        sup = _strata(tmp_path, monkeypatch)
        _exec_returning(monkeypatch, _FakeChild())
        sup._proc = await sup._spawn(["e"], {})
        failure = OSError("the engine stopped unexpectedly (exit code 3221226505)")
        failure.status_code = 503
        with pytest.raises(OSError):
            async with sup.call_slot():
                raise failure
        (entry,) = registry.entries()
        assert (entry.exit_reason, _stop_of(entry)) == ("stopped", "engine_died")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reply", [
        None,  # a launcher that names no engine version: the version check refuses
        {"choices": []},  # a version, and a smoke that gets no tool call back
    ], ids=["no-version", "smoke-fails"])
    async def test_a_start_the_launcher_refuses_stays_failed_start_through_the_real_launch(
        self, tmp_path, monkeypatch, registry, reply,
    ):
        from tests.test_strata_supervisor import _started_through_the_real_launch

        sup = _strata(tmp_path, monkeypatch)
        metrics = {"engine": {"version": "0.1.46"}} if reply else {}
        _started_through_the_real_launch(sup, monkeypatch, metrics=metrics, smoke=reply)
        child = _FakeChild()
        _exec_returning(monkeypatch, child)
        real_spawn = type(sup)._spawn

        async def spawn(cmd, env):
            return await real_spawn(sup, cmd, env)

        sup._spawn = spawn
        from dpc_client_core.managers.llama_server_supervisor import LlamaServerError

        with pytest.raises(LlamaServerError):
            await sup.ensure_running()
        (entry,) = registry.entries()
        assert entry.pid == child.pid
        assert (entry.exit_reason, _stop_of(entry)) == ("stopped", "failed_start")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("make", [_llama, _strata])
    async def test_the_orderly_stop_is_backend_stop_even_though_the_supervisor_sent_the_signal(
        self, tmp_path, monkeypatch, registry, make
    ):
        sup = make(tmp_path, monkeypatch)
        child = _FakeChild()
        _exec_returning(monkeypatch, child)
        sup._proc = await sup._spawn(["e"], {})
        registry.begin_shutdown()
        sup._stop_why = "ui_stop"
        await sup.stop()
        (entry,) = registry.entries()
        assert child.signals, "the supervisor did send the stop"
        assert (entry.exit_reason, _stop_of(entry)) == ("backend_stop", None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("make", [_llama, _strata])
    async def test_a_child_that_died_on_its_own_is_crashed_or_exited_never_stopped(self, tmp_path, monkeypatch, registry, make):
        sup = make(tmp_path, monkeypatch)
        died, quit_ = _FakeChild(), _FakeChild()
        _exec_returning(monkeypatch, died)
        sup._proc = await sup._spawn(["e"], {})
        died.returncode = DEATH
        await sup._stop_child()
        _exec_returning(monkeypatch, quit_)
        sup._proc = await sup._spawn(["e"], {})
        quit_.returncode = 3
        await sup._stop_child()
        first, second = registry.entries()
        assert first.exit_reason == "crashed" and second.exit_reason == "exited"
        assert not died.signals and not quit_.signals, "a dead child is not signalled"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("make", [_llama, _strata])
    async def test_no_child_is_born_once_the_stop_has_begun(self, tmp_path, monkeypatch, registry, make):
        from dpc_client_core.managers.llama_server_supervisor import LlamaServerError

        sup = make(tmp_path, monkeypatch)
        spawned = []

        async def fake_exec(*cmd, **kw):
            spawned.append(cmd)
            return _FakeChild()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        registry.begin_shutdown()
        with pytest.raises(LlamaServerError, match="stopping"):
            await sup._spawn(["e"], {})
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_stand_in_with_no_pid_is_not_recorded(self, tmp_path, monkeypatch, registry):
        sup = _llama(tmp_path, monkeypatch)

        class NoPid:
            returncode = None

        async def fake_exec(*cmd, **kw):
            return NoPid()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        await sup._spawn(["e"], {})
        assert registry.entries() == []


# ================================================================== the startup sweep

def _file_with_an_open_entry(tmp_path, pid, start_time):
    """A registry file as a run that died would have left it: one open entry.
    Written by hand, because `register` itself refuses the backend's own pid."""
    path = tmp_path / "process_registry.json"
    entry = ProcessEntry(
        pid, start_time, os.getpid(), None, "run_shell", "tool", agent_id="a", conversation_id="c",
        tool="run_shell", round=1, command="the earlier run's child", started_at="2026-10-25T10:00:00.000+00:00",
    )
    path.write_text(json.dumps({"schema": 1, "entries": [entry.to_dict()]}), encoding="utf-8")
    return path


class TestTheStartupSweep:
    def test_a_process_that_is_alive_with_the_same_start_time_is_killed_and_closed_orphan_swept(self, tmp_path, started):
        child = subprocess.Popen(SLEEP)
        started.append(child)
        path = _file_with_an_open_entry(tmp_path, child.pid, pr.create_time_of(child.pid))
        registry = ProcessRegistry(path)
        registry.load()
        outcome = registry.sweep_orphans()
        assert child.wait(timeout=20) is not None, "the orphan was left running"
        (entry,) = registry.entries()
        assert entry.exit_reason == "orphan_swept" and entry.ended_at
        assert outcome.killed and not outcome.closed
        assert any("killed" in m and f"pid {child.pid}" in m and "orphan_swept" in m for _l, m in outcome.records)
        assert json.loads(path.read_text(encoding="utf-8"))["entries"][0]["exit_reason"] == "orphan_swept"

    def test_a_process_alive_with_a_different_start_time_is_a_stranger_and_left_alone(self, tmp_path, started):
        child = subprocess.Popen(SLEEP)
        started.append(child)
        path = _file_with_an_open_entry(tmp_path, child.pid, pr.create_time_of(child.pid) - 5000.0)
        registry = ProcessRegistry(path)
        registry.load()
        outcome = registry.sweep_orphans()
        assert child.poll() is None, "a stranger that reused the pid was killed"
        (entry,) = registry.entries()
        assert (entry.exit_reason, entry.exit_code) == ("backend_stop", None)
        assert outcome.closed and not outcome.killed

    def test_a_process_that_is_gone_is_closed_backend_stop_with_no_code_and_says_so(self, tmp_path, caplog):
        done = subprocess.run([sys.executable, "-c", "pass"])
        assert done.returncode == 0
        path = _file_with_an_open_entry(tmp_path, 7_700_001, 1_700_000_000.0)
        registry = ProcessRegistry(path)
        registry.load()
        outcome = registry.sweep_orphans()
        (entry,) = registry.entries()
        assert (entry.exit_reason, entry.exit_code, entry.signal) == ("backend_stop", None, None)
        (level, message), = outcome.records
        assert "found closed at start" in message and "pid 7700001" in message
        with caplog.at_level(logging.INFO, logger="dpc_client_core.process_registry"):
            outcome.log()
        assert any("found closed at start" in m for m in caplog.messages)

    @pytest.mark.parametrize("which", ["own", "parent"])
    def test_never_its_own_pid_and_never_an_ancestors(self, tmp_path, which):
        pid = os.getpid() if which == "own" else os.getppid()
        path = _file_with_an_open_entry(tmp_path, pid, pr.create_time_of(pid))
        registry = ProcessRegistry(path)
        registry.load()
        killed = []
        outcome = registry.sweep_orphans(kill=lambda p: killed.append(p) or "recorded")
        assert killed == [], f"the sweep aimed a kill at {which}"
        assert any(l == logging.ERROR for l, _m in outcome.records)
        assert registry.entries()[0].exit_reason == "backend_stop"

    def test_a_missing_creation_time_is_not_enough_to_kill(self, tmp_path, started):
        child = subprocess.Popen(SLEEP)
        started.append(child)
        path = _file_with_an_open_entry(tmp_path, child.pid, None)
        registry = ProcessRegistry(path)
        registry.load()
        registry.sweep_orphans()
        assert child.poll() is None

    def test_a_kill_that_does_not_work_leaves_the_entry_open(self, tmp_path, started):
        child = subprocess.Popen(SLEEP)
        started.append(child)
        path = _file_with_an_open_entry(tmp_path, child.pid, pr.create_time_of(child.pid))
        registry = ProcessRegistry(path)
        registry.load()
        outcome = registry.sweep_orphans(kill=lambda p: "pretended")
        assert child.poll() is None
        assert registry.entries()[0].exit_reason is None
        assert any(l == logging.ERROR and "stays open" in m for l, m in outcome.records)

    def test_the_backend_sweeps_after_the_job_and_before_its_first_spawn(self):
        import ast

        source = (Path(__file__).resolve().parents[1] / "run_service.py").read_text(encoding="utf-8")
        main = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
        order = [
            n.func.id for n in ast.walk(main)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in ("enter_kill_on_close_job", "start_process_registry", "dependency_setup", "setup_logging")
        ]
        assert order == ["enter_kill_on_close_job", "start_process_registry", "dependency_setup", "setup_logging"]
        assert "sweep_outcome.log()" in source, "the sweep's lines are written once logging exists"


class TestAZombieIsGone:
    """On POSIX a killed child stays a zombie until its parent reaps it, and psutil
    still gives its creation time. CI on Linux saw the sweep leave such an entry
    open, because the probe kept answering "same" after the kill."""

    def test_a_zombie_with_the_recorded_start_time_is_gone_and_the_sweep_closes_it(self, tmp_path, monkeypatch):
        import psutil

        killed = set()

        class Process:  # what psutil reports for a pid that was killed and not yet reaped
            def __init__(self, pid):
                self.pid = pid

            def create_time(self):
                return 1_700_000_000.0

            def status(self):
                return psutil.STATUS_ZOMBIE if self.pid in killed else psutil.STATUS_SLEEPING

        monkeypatch.setattr(psutil, "Process", Process)
        assert pr.probe_identity(7_700_002, 1_700_000_000.0) == "same"
        path = _file_with_an_open_entry(tmp_path, 7_700_002, 1_700_000_000.0)
        registry = ProcessRegistry(path)
        registry.load()
        outcome = registry.sweep_orphans(kill=lambda p: killed.add(p) or "killed", protected=lambda: {os.getpid()})
        assert pr.probe_identity(7_700_002, 1_700_000_000.0) == "gone"
        (entry,) = registry.entries()
        assert entry.exit_reason == "orphan_swept"
        assert outcome.killed and not outcome.closed

    @pytest.mark.skipif(sys.platform == "win32", reason="a Windows process leaves no zombie")
    def test_a_real_child_killed_and_not_reaped_is_gone(self, started):
        child = subprocess.Popen(SLEEP)
        started.append(child)
        start = pr.create_time_of(child.pid)
        os.kill(child.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5.0
        while pr.probe_identity(child.pid, start) == "same" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pr.probe_identity(child.pid, start) == "gone"
        assert child.poll() is not None


# =========================================================================== shutdown

class TestTheOrderlyStop:
    @pytest.mark.asyncio
    async def test_the_order_is_refuse_then_commands_then_histories_then_engines_then_verify(self, monkeypatch):
        from unittest.mock import AsyncMock, Mock

        from dpc_client_core.dpc_agent import index_writer
        from dpc_client_core.service import CoreService

        monkeypatch.setattr(index_writer, "begin_shutdown", lambda: None)
        events = []

        class Recording(ProcessRegistry):
            def begin_shutdown(self):
                events.append("refuse new entries")
                return super().begin_shutdown()

            def stop_commands(self, **kw):
                events.append("kill commands")
                return super().stop_commands(**kw)

            def finish_shutdown(self, **kw):
                events.append("verify and log")
                return super().finish_shutdown(**kw)

        recording = Recording()
        pr.set_registry(recording)

        async def a_background_task():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                events.append("background tasks cancelled")
                raise

        class Monitor:
            _history_dirty = True

            def save_history(self):
                events.append("group history saved")
                return True

        class LLM:
            providers = {}

            async def shutdown(self):
                events.append("engines stop")
                get = pr.get_registry()
                assert get.stopping

        svc = CoreService.__new__(CoreService)
        task = asyncio.ensure_future(a_background_task())
        await asyncio.sleep(0)
        svc._background_tasks = {task}
        svc.conversation_monitors = {"group-1": Monitor()}
        svc.llm_manager = LLM()
        svc.p2p_manager, svc.local_api, svc.hub_client = AsyncMock(), AsyncMock(), AsyncMock()
        svc.p2p_manager.shutdown_all.side_effect = lambda: events.append("core components stop")
        svc.file_server = Mock()
        svc.gateway = None
        await svc.shutdown()

        assert events == [
            "refuse new entries",
            "background tasks cancelled",
            "kill commands",
            "group history saved",
            "engines stop",
            "verify and log",
            "core components stop",
        ]
        with pytest.raises(ProcessRegistryClosed):
            recording.register(7_800_001, launcher="strata", launcher_kind="subsystem", command="x")

    @pytest.mark.asyncio
    async def test_a_running_command_is_killed_by_the_stop_and_ends_backend_stop(self, monkeypatch, registry):
        from unittest.mock import AsyncMock, Mock

        from dpc_client_core.dpc_agent import index_writer
        from dpc_client_core.service import CoreService

        monkeypatch.setattr(index_writer, "begin_shutdown", lambda: None)
        result = {}

        def command():
            with pr.spawn_origin(SpawnOrigin(agent_id="a", conversation_id="c", tool="run_shell", round=1)):
                result["run"] = run_supervised(SLEEP, launcher="run_shell", timeout=120, ceiling_mb=0)

        thread = threading.Thread(target=command, daemon=True)
        thread.start()
        entry = _wait_for_entry(registry)
        pid = entry.pid

        class LLM:
            providers = {}

            async def shutdown(self):
                pass

        svc = CoreService.__new__(CoreService)
        svc._background_tasks = set()
        svc.conversation_monitors = {}
        svc.llm_manager = LLM()
        svc.p2p_manager, svc.local_api, svc.hub_client = AsyncMock(), AsyncMock(), AsyncMock()
        svc.file_server = Mock()
        svc.gateway = None
        await svc.shutdown()
        thread.join(timeout=20)
        assert not thread.is_alive(), "the thread running the command did not come back"
        assert pr.probe_identity(pid, entry.start_time) == "gone", "the command outlived the stop"
        (done,) = registry.entries()
        assert done.exit_reason == "backend_stop" and result["run"].exit_reason == "backend_stop"
        assert not registry.open_entries()

    def test_an_entry_left_open_is_found_killed_and_closed_by_the_verify_step(self, tmp_path, started, caplog):
        registry = ProcessRegistry()
        child = subprocess.Popen(SLEEP)
        started.append(child)
        registry.register(child.pid, launcher="strata", launcher_kind="subsystem", command="x")
        registry.begin_shutdown()
        with caplog.at_level(logging.INFO, logger="dpc_client_core.process_registry"):
            done = registry.finish_shutdown(kill=lambda p: child.kill() or "killed")
        assert child.wait(timeout=20) is not None
        assert [e.exit_reason for e in done] == ["backend_stop"]
        assert any("still running after the stop" in m for m in caplog.messages)
        assert any(m.startswith("backend stop: pid ") and "ended backend_stop" in m for m in caplog.messages)

    def test_the_thread_that_sees_the_kill_first_reads_the_same_word(self, registry):
        """The command's own thread learns of the death before the stop has recorded it."""
        registry.register(7_800_010, launcher="run_shell", launcher_kind="tool", command="x", origin=SpawnOrigin())
        seen = {}

        def kill_tree(target):
            seen["by_the_thread"] = registry.end(7_800_010, DEATH)
            return "killed"

        registry.stop_commands(kill_tree=kill_tree)
        assert seen["by_the_thread"].exit_reason == "backend_stop", "a kill the stop sent read as a crash"

    def test_the_stop_never_aims_at_the_backends_own_pid(self, registry):
        """A row naming the backend is a defect; it is skipped and logged, not killed."""
        entry = ProcessEntry(os.getpid(), None, None, None, "run_shell", "tool", command="x", started_at="2026-10-25T00:00:00+00:00")
        registry._open[entry.pid] = entry
        registry._entries.append(entry)
        aimed = []
        registry.stop_commands(kill_tree=lambda target: aimed.append(target) or "recorded")
        assert aimed == []
