"""The process registry's files have a size limit and rotate (ADR-046 point 3).

The daily files were bounded by age alone and an entry by nothing but the command's
cut, so a short-lived process respawned every few seconds, or one oversized field,
could fill the disk. Measured 2026-11-03: daily files of 151-361 KB, so the limits
here must never bite in normal use and must hold in a loop.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from datetime import datetime, timezone

import pytest

from dpc_client_core import process_registry as pr
from dpc_client_core.process_registry import ProcessEntry, ProcessRegistry, SpawnOrigin

LOGGER = "dpc_client_core.process_registry"
NOON = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc).timestamp()
DAY = "2026-11-03"
SINCE = "2026-10-01T00:00:00Z"
FILE_MAX = 2_000
TOTAL_MAX = 6_000


def _registry(tmp_path, *, clock=lambda: NOON, file_max=FILE_MAX, total_max=TOTAL_MAX, **kwargs):
    """History only (no state file, no fsync) unless a `path` is given; nothing kept
    in memory, so `list_processes` answers from the daily files alone."""
    kwargs.setdefault("history_dir", tmp_path / "process_registry")
    kwargs.setdefault("keep_finished_max", 0)
    return ProcessRegistry(clock=clock, history_file_max_bytes=file_max, history_total_max_bytes=total_max, **kwargs)


def _spawn_and_end(registry, pid, command="x"):
    registry.register(pid, launcher="strata", launcher_kind="subsystem", command=command)
    return registry.end(pid, 0)


def _names(folder):
    return sorted(p.name for p in folder.iterdir())


def _pids(registry):
    return [row["pid"] for row in registry.list_processes(since=SINCE)]


def _row(pid, day=DAY):
    return json.dumps({"pid": pid, "start_time": None, "parent_pid": 1, "parent_start_time": None,
                       "launcher": "strata", "launcher_kind": "subsystem", "started_at": f"{day}T01:00:00.000+00:00",
                       "ended_at": f"{day}T01:00:01.000+00:00", "exit_reason": "exited"}) + "\n"


def _said(caplog, *words):
    return [r for r in caplog.records if all(w in r.getMessage() for w in words)]


class TestALimitPerDailyFile:
    def test_a_spawn_loop_stays_under_both_limits_and_the_day_goes_on(self, tmp_path):
        registry = _registry(tmp_path)
        for pid in range(7_980_001, 7_980_301):
            _spawn_and_end(registry, pid)
            sizes = {p.name: p.stat().st_size for p in registry.history_dir.iterdir()}
            assert max(sizes.values()) <= FILE_MAX
            assert sum(sizes.values()) <= TOTAL_MAX
        assert f"{DAY}.jsonl" in _names(registry.history_dir), "the day continues in its own file"
        assert any(n != f"{DAY}.jsonl" for n in _names(registry.history_dir)), "nothing rotated"
        assert _pids(registry)[-1] == 7_980_300

    def test_an_ordinary_day_is_one_file_under_the_real_limits(self, tmp_path):
        """465 rows was the fullest day measured; the real limits never reach it."""
        assert pr.HISTORY_FILE_MAX_BYTES == 10 * 1024 * 1024
        assert pr.HISTORY_TOTAL_MAX_BYTES == 50 * 1024 * 1024
        registry = ProcessRegistry(history_dir=tmp_path / "h", clock=lambda: NOON, keep_finished_max=0)
        for pid in range(7_981_001, 7_981_466):
            _spawn_and_end(registry, pid, "c" * 120)
        assert _names(registry.history_dir) == [f"{DAY}.jsonl"]
        assert len(_pids(registry)) == 465

    def test_a_rotated_file_is_numbered_from_one_and_never_renamed_again(self, tmp_path):
        registry = _registry(tmp_path, total_max=10 ** 9)
        folder = registry.history_dir
        folder.mkdir()
        first = None
        for pid in range(7_982_001, 7_982_101):  # far more rows than two rotations take
            _spawn_and_end(registry, pid)
            if first is None and f"{DAY}.1.jsonl" in _names(folder):
                first = (folder / f"{DAY}.1.jsonl").read_bytes()
            if f"{DAY}.2.jsonl" in _names(folder):
                break
        assert first is not None and f"{DAY}.2.jsonl" in _names(folder)
        assert (folder / f"{DAY}.1.jsonl").read_bytes() == first

    def test_a_second_backend_continues_the_numbering_and_loses_no_row(self, tmp_path):
        one, two = _registry(tmp_path, total_max=10 ** 9), _registry(tmp_path, total_max=10 ** 9)
        ended = []
        for index, pid in enumerate(range(7_983_001, 7_983_101)):
            _spawn_and_end(one if index % 2 else two, pid)
            ended.append(pid)
        assert sorted(_pids(one)) == ended
        assert sorted(_pids(two)) == ended


class TestRotatedRowsReadBack:
    def test_rows_of_one_instant_come_back_in_the_order_they_were_written(self, tmp_path):
        """The same clock for every row, so the sort by end keeps the files' order, and
        over ten rotations `.10` must follow `.2`, which a sort by name would not do."""
        registry = _registry(tmp_path, total_max=10 ** 9)
        written = list(range(7_984_001, 7_984_081))
        for pid in written:
            _spawn_and_end(registry, pid)
        assert f"{DAY}.10.jsonl" in _names(registry.history_dir)
        assert _pids(registry) == written

    def test_a_window_reads_the_rotated_files_of_its_days_only(self, tmp_path):
        folder = tmp_path / "process_registry"
        folder.mkdir()
        (folder / "2026-11-01.1.jsonl").write_text(_row(1, "2026-11-01"), encoding="utf-8")
        (folder / "2026-11-02.1.jsonl").write_text(_row(2, "2026-11-02"), encoding="utf-8")
        (folder / "2026-11-02.jsonl").write_text(_row(3, "2026-11-02"), encoding="utf-8")
        registry = _registry(tmp_path)
        rows = registry.list_processes(since="2026-11-02T00:00:00Z", until="2026-11-02T23:59:59Z")
        assert [r["pid"] for r in rows] == [2, 3]

    @pytest.mark.parametrize("torn", [b'{"pid": 7985', b'{"pid": 7985, "command": "\xd0'])
    def test_a_torn_last_line_in_a_rotated_file_costs_that_line_only(self, tmp_path, torn):
        """The second form ends inside a two-byte character: a whole-file decode of it
        raised out of `list_processes` and took every other row with it."""
        folder = tmp_path / "process_registry"
        folder.mkdir()
        (folder / f"{DAY}.1.jsonl").write_bytes(_row(1).encode() + torn)
        (folder / f"{DAY}.jsonl").write_bytes(_row(2).encode() + torn)
        assert _pids(_registry(tmp_path)) == [1, 2]

    def test_a_torn_file_rotates_and_the_new_row_is_whole(self, tmp_path):
        registry = _registry(tmp_path)
        folder = registry.history_dir
        folder.mkdir()
        (folder / f"{DAY}.jsonl").write_bytes(b"x" * FILE_MAX)
        _spawn_and_end(registry, 7_985_101)
        assert (folder / f"{DAY}.1.jsonl").read_bytes() == b"x" * FILE_MAX
        assert _pids(registry) == [7_985_101]


class TestAReadDuringARotation:
    def _rotate_when(self, monkeypatch, registry, trigger_name, *, new_row=True):
        """Rotate the day's file the first time the reader asks for `trigger_name`,
        before that read happens, as another thread would."""
        real = pathlib.Path.read_bytes
        fired = []

        def read_bytes(path):
            if path.name == trigger_name and not fired:
                fired.append(True)
                os.rename(registry.history_dir / f"{DAY}.jsonl", registry.history_dir / f"{DAY}.2.jsonl")
                if new_row:
                    (registry.history_dir / f"{DAY}.jsonl").write_text(_row(3), encoding="utf-8")
            return real(path)

        monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)
        return fired

    def _two_files(self, tmp_path):
        folder = tmp_path / "process_registry"
        folder.mkdir()
        (folder / f"{DAY}.1.jsonl").write_text(_row(1), encoding="utf-8")
        (folder / f"{DAY}.jsonl").write_text(_row(2), encoding="utf-8")
        return _registry(tmp_path)

    def test_the_file_moved_and_a_fresh_one_took_its_name(self, tmp_path, monkeypatch):
        """The listed name still opens, on the new file: only a second listing shows
        that the rows the reader wanted went to `.2`."""
        registry = self._two_files(tmp_path)
        fired = self._rotate_when(monkeypatch, registry, f"{DAY}.jsonl")
        assert sorted(_pids(registry)) == [1, 2, 3] and fired

    def test_the_file_moved_and_nothing_has_its_name_yet(self, tmp_path, monkeypatch, caplog):
        registry = self._two_files(tmp_path)
        fired = self._rotate_when(monkeypatch, registry, f"{DAY}.jsonl", new_row=False)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert sorted(_pids(registry)) == [1, 2] and fired
        assert not _said(caplog, "could not read")

    def test_no_row_is_read_twice_when_the_read_starts_over(self, tmp_path, monkeypatch):
        registry = self._two_files(tmp_path)
        self._rotate_when(monkeypatch, registry, f"{DAY}.1.jsonl")
        assert sorted(_pids(registry)) == [1, 2, 3]

    def test_a_file_that_cannot_be_read_is_said_and_the_rest_still_answers(self, tmp_path, monkeypatch, caplog):
        registry = self._two_files(tmp_path)
        real = pathlib.Path.read_bytes

        def read_bytes(path):
            if path.name == f"{DAY}.1.jsonl":
                raise PermissionError("held")
            return real(path)

        monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)
        monkeypatch.setattr(pr, "HISTORY_READ_RETRY_S", 0.0)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert _pids(registry) == [2]
        assert len(_said(caplog, "could not read")) == 1


class TestAgeOut:
    def test_a_rotated_file_ages_out_by_its_day(self, tmp_path):
        registry = _registry(tmp_path, clock=lambda: NOON, total_max=10 ** 9)
        folder = registry.history_dir
        folder.mkdir()
        for name in ("2026-10-26.1.jsonl", "2026-10-26.jsonl", "2026-10-27.3.jsonl", "2026-10-27.jsonl"):
            (folder / name).write_text("{}\n", encoding="utf-8")
        removed = registry.prune_history()
        assert [p.name for p in removed] == ["2026-10-26.1.jsonl", "2026-10-26.jsonl"]
        assert _names(folder) == ["2026-10-27.3.jsonl", "2026-10-27.jsonl"]

    def test_a_backend_that_stays_up_ages_files_out_when_a_new_day_begins(self, tmp_path):
        now = [NOON]
        registry = _registry(tmp_path, clock=lambda: now[0], total_max=10 ** 9)
        folder = registry.history_dir
        folder.mkdir()
        (folder / "2026-10-27.jsonl").write_text("{}\n", encoding="utf-8")
        (folder / f"{DAY}.jsonl").write_text("{}\n", encoding="utf-8")
        _spawn_and_end(registry, 7_986_001)
        assert "2026-10-27.jsonl" in _names(folder), "the same day: nothing to sweep"
        now[0] += 86_400
        _spawn_and_end(registry, 7_986_002)
        assert "2026-10-27.jsonl" not in _names(folder)


class TestALimitOverAllHistory:
    def _files(self, folder, sizes):
        folder.mkdir(exist_ok=True)
        for name, size in sizes.items():
            (folder / name).write_bytes(b"\n" * size)

    def test_the_oldest_go_first_and_the_removal_is_logged_once_with_what_went(self, tmp_path, caplog):
        registry = _registry(tmp_path)
        self._files(registry.history_dir, {
            "2026-11-01.1.jsonl": 1_500, "2026-11-01.jsonl": 1_500, "2026-11-02.jsonl": 1_500,
            f"{DAY}.1.jsonl": 1_500, f"{DAY}.jsonl": 100,
        })
        with caplog.at_level(logging.INFO, logger=LOGGER):
            removed = registry.prune_history()
        # 6000 - room for a whole current file (2000) leaves 4000 for the rest.
        assert [p.name for p in removed] == ["2026-11-01.1.jsonl", "2026-11-01.jsonl"]
        assert _names(registry.history_dir) == ["2026-11-02.jsonl", f"{DAY}.1.jsonl", f"{DAY}.jsonl"]
        (line,) = _said(caplog, "2026-11-01.1.jsonl")
        assert line.levelno == logging.INFO and "2026-11-01.jsonl" in line.getMessage()

    def test_today_s_current_file_is_never_removed(self, tmp_path):
        registry = _registry(tmp_path, total_max=FILE_MAX)
        self._files(registry.history_dir, {f"{DAY}.1.jsonl": 500, f"{DAY}.2.jsonl": 500, f"{DAY}.jsonl": 500})
        registry.prune_history()
        assert _names(registry.history_dir) == [f"{DAY}.jsonl"]

    def test_a_rotation_sweeps_and_says_so(self, tmp_path, caplog):
        registry = _registry(tmp_path)
        self._files(registry.history_dir, {"2026-11-01.jsonl": 3_900, f"{DAY}.jsonl": FILE_MAX - 10})
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _spawn_and_end(registry, 7_987_001)
        assert "2026-11-01.jsonl" not in _names(registry.history_dir)
        assert len(_said(caplog, "2026-11-01.jsonl")) == 1

    def test_a_file_that_cannot_be_removed_is_said_and_the_next_goes_instead(self, tmp_path, monkeypatch, caplog):
        registry = _registry(tmp_path)
        self._files(registry.history_dir, {"2026-11-01.jsonl": 3_000, "2026-11-02.jsonl": 3_000, f"{DAY}.jsonl": 10})
        real = pathlib.Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == "2026-11-01.jsonl":
                raise PermissionError("held")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "unlink", unlink)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            removed = registry.prune_history()
        assert [p.name for p in removed] == ["2026-11-02.jsonl"]
        assert _said(caplog, "could not remove", "2026-11-01.jsonl")

    def test_what_the_start_removes_reaches_the_log_after_logging_exists(self, tmp_path, monkeypatch):
        """The start runs before logging is configured, so the line has to travel in
        the outcome `run_service.py` writes later, like the sweep's. The real limits."""
        monkeypatch.setenv("DPC_HOME", str(tmp_path))
        folder = tmp_path / "process_registry"
        folder.mkdir()
        today = datetime.now(timezone.utc).date().isoformat()
        with (folder / f"{today}.1.jsonl").open("wb") as handle:
            handle.truncate(pr.HISTORY_TOTAL_MAX_BYTES)  # sparse, not 50 MiB of writes
        (folder / "2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")
        outcome = pr.start_persistent()
        said = [m for level, m in outcome.records if level == logging.INFO]
        assert any(f"{today}.1.jsonl" in m for m in said), said
        assert any("2020-01-01.jsonl" in m for m in said), said
        assert not (folder / f"{today}.1.jsonl").exists()


class TestARenameThatFails:
    def _refuse(self, monkeypatch):
        def rename(src, dst, *args, **kwargs):
            raise PermissionError(13, "held by another handle", str(src))

        monkeypatch.setattr(pr.os, "rename", rename)

    def test_the_append_goes_on_and_the_failure_is_said_once(self, tmp_path, monkeypatch, caplog):
        registry = _registry(tmp_path, total_max=10 ** 9)
        self._refuse(monkeypatch)
        current = registry.history_dir / f"{DAY}.jsonl"
        pid = 7_988_001
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert _spawn_and_end(registry, pid).exit_reason == "exited"
            while current.stat().st_size <= FILE_MAX and pid < 7_988_100:
                pid += 1
                assert _spawn_and_end(registry, pid).exit_reason == "exited"
        assert current.stat().st_size > FILE_MAX
        assert _names(registry.history_dir) == [f"{DAY}.jsonl"]
        assert len(_said(caplog, "could not rotate")) == 1
        assert _pids(registry)[-1] == pid, "the row past the limit still landed"

    def test_the_file_stops_at_twice_its_limit_and_says_rows_are_lost(self, tmp_path, monkeypatch, caplog):
        registry = _registry(tmp_path, total_max=10 ** 9)
        self._refuse(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            for pid in range(7_988_101, 7_988_201):
                assert _spawn_and_end(registry, pid).exit_reason == "exited"
        size = (registry.history_dir / f"{DAY}.jsonl").stat().st_size
        assert FILE_MAX < size <= 2 * FILE_MAX
        assert len(_said(caplog, "could not rotate")) == 1
        assert len(_said(caplog, "dropped from pid")) == 1

    def test_when_the_rename_works_again_the_file_rotates_and_the_loss_is_counted(self, tmp_path, monkeypatch, caplog):
        registry = _registry(tmp_path, total_max=10 ** 9)
        with monkeypatch.context() as patch:
            self._refuse(patch)
            for pid in range(7_988_301, 7_988_401):
                _spawn_and_end(registry, pid)
        landed = len(_pids(registry))
        assert landed < 100
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _spawn_and_end(registry, 7_988_401)
        assert f"{DAY}.1.jsonl" in _names(registry.history_dir)
        assert _pids(registry)[-1] == 7_988_401
        assert _said(caplog, str(100 - landed), "dropped")


class TestTwoWritersAtOneLock:
    def _refuse_the_lock_once(self, monkeypatch):
        """What Windows answers while another process is deleting the lock file."""
        real, refused = os.open, []

        def open_(path, flags, *args):
            if str(path).endswith(".lock") and not refused:
                refused.append(True)
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, flags, *args)

        monkeypatch.setattr(pr.os, "open", open_)
        return refused

    def test_on_windows_a_lock_being_deleted_is_waited_for_and_the_row_lands(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pr.sys, "platform", "win32")
        refused = self._refuse_the_lock_once(monkeypatch)
        registry = _registry(tmp_path)
        _spawn_and_end(registry, 7_988_501)
        assert refused and _pids(registry) == [7_988_501]

    def test_elsewhere_that_error_is_a_failed_append_at_once_not_a_wait(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(pr.sys, "platform", "linux")
        refused = self._refuse_the_lock_once(monkeypatch)
        monkeypatch.setattr(pr, "LOCK_TIMEOUT_S", 60.0)
        registry = _registry(tmp_path)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert _spawn_and_end(registry, 7_988_502).exit_reason == "exited"
        assert refused and len(_said(caplog, "could not append")) == 1


class TestUnparsableNames:
    NAMES = ("2026-11-03.x.jsonl", "2026-11-03.0.jsonl", "2026-11-03.01.jsonl", "2020-01-01.1.2.jsonl",
             "2020-13-45.jsonl", "2020-13-45.1.jsonl", "notes.jsonl", "2020-01-01.1.jsonl.lock", "2020-01-01.1.txt")

    def test_a_name_neither_can_parse_is_left_alone_unread_and_uncounted(self, tmp_path):
        registry = _registry(tmp_path, total_max=FILE_MAX)
        folder = registry.history_dir
        folder.mkdir()
        for name in self.NAMES:
            (folder / name).write_bytes(_row(9).encode() + b"\n" * 5_000)
        assert registry.prune_history() == []
        assert _names(folder) == sorted(self.NAMES)
        assert _pids(registry) == []
        _spawn_and_end(registry, 7_989_001)
        assert _names(folder) == sorted(self.NAMES + (f"{DAY}.jsonl",))


class TestAnOversizedField:
    HUGE = "x" * 1_000_000

    def _origin(self):
        return SpawnOrigin(agent_id=self.HUGE, conversation_id=self.HUGE, tool=self.HUGE, round=1, turn_id=self.HUGE)

    def test_no_field_of_an_entry_passes_its_cut_in_either_file(self, tmp_path):
        path = tmp_path / "process_registry.json"
        registry = ProcessRegistry(path, clock=lambda: NOON)
        registry.register(7_990_001, launcher=self.HUGE, launcher_kind="tool", command=self.HUGE, origin=self._origin())
        registry.end(7_990_001, 0)
        assert path.stat().st_size < 3_000
        (line,) = (registry.history_dir / f"{DAY}.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(line) < 3_000
        row = json.loads(line)
        for name in ("launcher", "agent_id", "conversation_id", "tool", "turn_id"):
            assert len(row[name]) == pr.FIELD_MAX_CHARS
        assert len(row["command"]) == pr.COMMAND_MAX_CHARS

    def test_the_worst_entry_is_the_size_the_cuts_promise(self, tmp_path):
        """Four-byte characters: twelve bytes each as `\\ud83d\\ude00` in the state file,
        whose JSON is ASCII, four in the history's UTF-8."""
        wide = "\U0001F600" * 1_000
        path = tmp_path / "process_registry.json"
        registry = ProcessRegistry(path, clock=lambda: NOON)
        registry.register(7_990_101, launcher=wide, launcher_kind="tool", command=wide,
                          origin=SpawnOrigin(agent_id=wide, conversation_id=wide, tool=wide, round=1, turn_id=wide))
        registry.end(7_990_101, 0)
        chars = 5 * pr.FIELD_MAX_CHARS + pr.COMMAND_MAX_CHARS
        assert path.stat().st_size < 12 * chars + 1_500
        assert (registry.history_dir / f"{DAY}.jsonl").stat().st_size < 4 * chars + 1_000

    def test_an_oversized_entry_in_an_older_file_is_cut_when_it_is_read(self, tmp_path):
        path = tmp_path / "process_registry.json"
        path.write_text(json.dumps({"schema": 1, "entries": [{
            "pid": 7_990_201, "start_time": None, "parent_pid": 1, "parent_start_time": None,
            "launcher": "run_shell", "launcher_kind": "tool", "agent_id": self.HUGE, "command": self.HUGE,
        }]}), encoding="utf-8")
        registry = ProcessRegistry(path, clock=lambda: NOON)
        registry.load()
        (entry,) = registry.entries()
        assert (len(entry.agent_id), len(entry.command)) == (pr.FIELD_MAX_CHARS, pr.COMMAND_MAX_CHARS)

    def test_the_kill_of_a_turn_still_reaches_an_entry_whose_origin_was_cut(self):
        registry = ProcessRegistry(clock=lambda: NOON)
        origin = self._origin()
        registry.register(7_990_301, launcher="run_shell", launcher_kind="tool", command="c", origin=origin)
        killed = registry.stop_commands_of(origin, kill_tree=lambda target: "killed", wait_s=0.0)
        assert [e.pid for e in killed] == [7_990_301]

    def test_an_agent_with_an_id_past_the_cut_still_ends_its_own(self):
        registry = ProcessRegistry(clock=lambda: NOON)
        registry.register(7_990_401, launcher="run_shell", launcher_kind="tool", command="c", origin=self._origin())
        entry, said = registry.cancel_by_agent(
            7_990_401, self.HUGE, kill_tree=lambda target: "killed", probe=lambda pid, start: "same", wait_s=0.0)
        assert entry is not None and entry.exit_reason == "cancelled_by_agent", said

    def test_an_ordinary_entry_is_not_touched_by_the_cut(self):
        entry = ProcessEntry(pid=1, start_time=None, parent_pid=None, parent_start_time=None,
                             launcher="git_tool(external)", launcher_kind="tool",
                             agent_id="agent_johnny_f309700d", turn_id="20261102T092301Z-f3a35f8d")
        assert (entry.launcher, entry.agent_id, entry.turn_id, entry.tool) == (
            "git_tool(external)", "agent_johnny_f309700d", "20261102T092301Z-f3a35f8d", None)
