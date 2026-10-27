"""ADR-046 point 8: `list_processes`, the one answer to "what runs now, and who started it".

What is asserted: the command's shape through the real local API dispatch; the
kept entries, open and finished, as the state file holds them; the daily history
read for `since`/`until`; `commit_mb` read at the request for a live entry (a fake
psutil, so no real process is walked) and None for a finished one and off
Windows; `free_commit` on Windows and elsewhere. The card's VRAM fields are
asserted in test_the_agent_map_reads_the_cards_vram_at_most_every_30_seconds.py.

Nothing here touches `~/.dpc`: the default registry is in memory and the
persisted ones live in `tmp_path`. No process is started.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dpc_client_core import process_registry as pr
from dpc_client_core.managers import commit_memory as cm
from dpc_client_core.managers import llama_server_supervisor as lss
from dpc_client_core.process_registry import ProcessRegistry, ProcessSample, SpawnOrigin, TreeSample

START = 1_000.0  # the creation time every fake process of these tests has
KEYS = {"avail_page_file_mib", "total_page_file_mib", "avail_phys_mib", "total_phys_mib", "source"}


def _utc(*parts) -> float:
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def _same(pid, start):
    return "same"


def _tool(registry, pid, agent="agent_a", **kwargs):
    return registry.register(pid, launcher="run_shell", launcher_kind="tool", command=f"cmd {pid}",
                             origin=SpawnOrigin(agent_id=agent, conversation_id="chat-1", tool="run_shell", round=3),
                             **kwargs)


class _FakeWS:
    """Feeds frames to the handler and records what it answers — the fake of
    tests/test_local_api.py."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.remote_address = ("127.0.0.1", 0)
        self.sent = []

    async def recv(self):
        return self._frames.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self, *args, **kwargs):
        pass


class TestTheCommand:
    @pytest.mark.asyncio
    async def test_the_command_is_allowed_and_answers_with_entries_the_backend_and_the_system_part(self, monkeypatch):
        from dpc_client_core.local_api import ALLOWED_COMMANDS, LocalApiServer
        from dpc_client_core.service import CoreService

        assert "list_processes" in ALLOWED_COMMANDS and hasattr(CoreService, "list_processes")
        reading = {"avail_page_file_mib": 1, "total_page_file_mib": 2, "avail_phys_mib": 3,
                   "total_phys_mib": 4, "source": cm.SOURCE}
        monkeypatch.setattr(cm, "read_free_commit_or_physical", lambda: dict(reading))
        monkeypatch.setattr(lss, "read_vram_mib", lambda: (11, 22))
        registry = pr.get_registry()
        _tool(registry, 7_960_001)
        registry.register(7_960_002, launcher="strata", launcher_kind="subsystem", command="launcher")
        registry.end(7_960_002, 0)

        class FakeCore:
            list_processes = CoreService.list_processes

        server = LocalApiServer(FakeCore(), port=0)
        server._auth_token = "token"
        ws = _FakeWS([
            json.dumps({"command": "auth", "token": "token"}),
            json.dumps({"id": "ok", "command": "list_processes", "payload": {}}),
            json.dumps({"id": "bad", "command": "list_processes", "payload": {"since": "yesterday"}}),
        ])
        await server._handler(ws)

        ok = next(m for m in ws.sent if m.get("id") == "ok")["payload"]
        assert set(ok) == {"status", "processes", "backend_pid", "backend_start_time", "free_commit",
                          "vram_free_mib", "vram_total_mib"}
        assert ok["status"] == "success" and ok["backend_pid"] == os.getpid()
        assert ok["backend_start_time"] == pr.create_time_of(os.getpid())
        assert ok["free_commit"] == reading
        assert (ok["vram_free_mib"], ok["vram_total_mib"]) == (11, 22)
        by_pid = {row["pid"]: row for row in ok["processes"]}
        assert set(by_pid) == {7_960_001, 7_960_002}
        live, ended = by_pid[7_960_001], by_pid[7_960_002]
        assert set(live) == set(pr.ProcessEntry.__dataclass_fields__) | {"commit_mb"}
        assert (live["agent_id"], live["conversation_id"], live["tool"], live["round"]) == (
            "agent_a", "chat-1", "run_shell", 3)
        assert live["parent_pid"] == os.getpid() and live["ended_at"] is None
        assert (ended["exit_reason"], ended["launcher_kind"], ended["commit_mb"]) == ("exited", "subsystem", None)

        bad = next(m for m in ws.sent if m.get("id") == "bad")["payload"]
        assert bad["status"] == "error" and "yesterday" in bad["message"]

    @pytest.mark.asyncio
    async def test_a_failure_answers_as_an_error_not_an_exception(self, monkeypatch):
        from dpc_client_core.service import CoreService

        def broken(*args, **kwargs):
            raise RuntimeError("the list could not be read")

        monkeypatch.setattr(pr.ProcessRegistry, "list_processes", broken)
        monkeypatch.setattr(lss, "read_vram_mib", lambda: (None, None))
        out = await CoreService.list_processes(SimpleNamespace())
        assert out == {"status": "error", "message": "the list could not be read"}


class TestTheEntries:
    def test_open_and_finished_entries_are_the_state_files_set(self, tmp_path):
        path = tmp_path / "process_registry.json"
        registry = ProcessRegistry(path, keep_finished_max=2, create_time=lambda pid: START)
        for pid in (7_961_001, 7_961_002, 7_961_003, 7_961_004):
            _tool(registry, pid)
        for pid in (7_961_001, 7_961_002, 7_961_003):
            registry.end(pid, 0)
        rows = registry.list_processes(probe=_same)
        on_disk = json.loads(path.read_text(encoding="utf-8"))["entries"]
        assert [r["pid"] for r in rows] == [e["pid"] for e in on_disk] == [7_961_002, 7_961_003, 7_961_004]
        assert [{k: v for k, v in r.items() if k != "commit_mb"} for r in rows] == on_disk
        assert all("commit_mb" not in e for e in on_disk), "commit_mb is never stored"

    def test_since_and_until_add_what_the_history_holds_in_the_window_and_nothing_twice(self, tmp_path):
        now = [_utc(2026, 10, 24, 10, 0, 0)]
        registry = ProcessRegistry(tmp_path / "process_registry.json", clock=lambda: now[0],
                                   keep_finished_max=1, create_time=lambda pid: START)
        for pid, day in ((7_962_001, 24), (7_962_002, 25), (7_962_003, 26)):
            now[0] = _utc(2026, 10, day, 10, 0, 0)
            _tool(registry, pid)
            now[0] = _utc(2026, 10, day, 10, 0, 5)
            registry.end(pid, 0)
        assert [r["pid"] for r in registry.list_processes()] == [7_962_003], "the state file keeps one"

        rows = registry.list_processes("2026-10-25T00:00:00Z", "2026-10-26T23:59:59Z")
        assert [r["pid"] for r in rows] == [7_962_002, 7_962_003], "the 25th from the history, the 26th kept once"
        assert rows[0]["exit_reason"] == "exited" and rows[0]["commit_mb"] is None

        rows = registry.list_processes(since="2026-10-24T00:00:00+00:00")
        assert [r["pid"] for r in rows] == [7_962_001, 7_962_002, 7_962_003]
        rows = registry.list_processes(until="2026-10-24T23:00:00")
        assert [r["pid"] for r in rows] == [7_962_001, 7_962_003], "a naive bound is UTC"

    def test_a_torn_history_line_is_skipped_and_a_malformed_bound_is_refused(self, tmp_path):
        registry = ProcessRegistry(tmp_path / "process_registry.json", keep_finished_max=0,
                                   clock=lambda: _utc(2026, 10, 26, 12, 0, 0))
        _tool(registry, 7_963_001)
        registry.end(7_963_001, 0)
        (registry.history_dir / "2026-10-26.jsonl").open("a", encoding="utf-8").write('{"pid": 7963')
        assert [r["pid"] for r in registry.list_processes(since="2026-10-26T00:00:00Z")] == [7_963_001]
        with pytest.raises(ValueError, match="not an ISO-8601"):
            registry.list_processes(since="yesterday")

    def test_a_memory_only_registry_has_no_history_to_add(self):
        registry = pr.get_registry()
        _tool(registry, 7_964_001)
        assert [r["pid"] for r in registry.list_processes(since="2026-01-01T00:00:00Z", probe=_same)] == [7_964_001]


class _FakeProcess:
    def __init__(self, pid, tree):
        self.pid = pid
        self._tree = tree

    def create_time(self):
        return START

    def status(self):
        return "running"

    def children(self, recursive=False):
        return [_FakeProcess(p, self._tree) for p in self._tree.get(self.pid, {}).get("children", [])]

    def oneshot(self):
        import contextlib

        return contextlib.nullcontext()

    def memory_info(self):
        private = self._tree[self.pid]["private_mb"] * pr._MB
        return SimpleNamespace(rss=private, private=private, peak_wset=private, peak_pagefile=private)

    def cpu_times(self):
        return SimpleNamespace(user=0.0, system=0.0)

    def io_counters(self):
        return SimpleNamespace(read_bytes=0, write_bytes=0)


def _fake_psutil(tree):
    class NoSuchProcess(Exception):
        pass

    def process(pid=None):
        if pid not in tree:
            raise NoSuchProcess(pid)
        return _FakeProcess(pid, tree)

    return SimpleNamespace(Process=process, NoSuchProcess=NoSuchProcess, STATUS_ZOMBIE="zombie")


class TestCommitNow:
    def test_a_live_entrys_commit_is_its_trees_private_read_at_the_request_and_never_stored(self, tmp_path, monkeypatch):
        tree = {7_965_001: {"private_mb": 100.0, "children": [7_965_002]}, 7_965_002: {"private_mb": 50.25}}
        monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(tree))
        path = tmp_path / "process_registry.json"
        registry = ProcessRegistry(path, platform="win32", create_time=lambda pid: START)
        _tool(registry, 7_965_001)
        (row,) = registry.list_processes()
        assert row["commit_mb"] == 150.2, "the sum over the tree, as the sampler sums it"
        tree[7_965_002]["private_mb"] = 500.0
        (row,) = registry.list_processes()
        assert row["commit_mb"] == 600.0, "read again at the next request"
        assert all("commit_mb" not in e for e in json.loads(path.read_text(encoding="utf-8"))["entries"])
        assert registry.entries()[0].peak_commit_mb is None, "a request is not a sample"

    def test_a_finished_entry_and_a_pid_that_changed_hands_have_none(self, monkeypatch):
        tree = {7_965_011: {"private_mb": 10.0}, 7_965_012: {"private_mb": 20.0}}
        monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(tree))
        registry = ProcessRegistry(platform="win32", create_time=lambda pid: START)
        _tool(registry, 7_965_011)
        _tool(registry, 7_965_012)
        registry.end(7_965_011, 0)
        rows = {r["pid"]: r for r in registry.list_processes(probe=_same)}
        assert rows[7_965_011]["commit_mb"] is None, "a finished entry is not read, even when its pid answers"
        assert rows[7_965_012]["commit_mb"] == 20.0
        rows = {r["pid"]: r for r in registry.list_processes(probe=lambda pid, start: "other")}
        assert rows[7_965_012]["commit_mb"] is None, "a stranger on the pid is not read"

    def test_off_windows_commit_is_none_and_nothing_is_walked(self, monkeypatch):
        registry = ProcessRegistry(platform="linux", create_time=lambda pid: START)
        _tool(registry, 7_965_021)

        def walked(pid):
            raise AssertionError("the tree was walked off Windows")

        (row,) = registry.list_processes(read_tree=walked, probe=_same)
        assert row["commit_mb"] is None

    def test_an_injected_reader_is_summed_and_a_tree_with_no_commit_is_none(self):
        registry = ProcessRegistry(platform="win32", create_time=lambda pid: START)
        _tool(registry, 7_965_031)
        reading = TreeSample(START, [ProcessSample(7_965_031, START, rss_mb=5.0, commit_mb=None)])
        (row,) = registry.list_processes(read_tree=lambda pid: reading, probe=_same)
        assert row["commit_mb"] is None


class TestFreeCommit:
    def test_on_windows_it_is_the_globalmemorystatusex_reading_unchanged(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        reading = {"avail_page_file_mib": 3686, "total_page_file_mib": 108236, "avail_phys_mib": 11305,
                   "total_phys_mib": 63168, "source": "GlobalMemoryStatusEx"}
        monkeypatch.setattr(cm, "read_free_commit", lambda: dict(reading))
        assert cm.read_free_commit_or_physical() == reading

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_elsewhere_it_is_physical_memory_by_psutil_with_commit_null(self, monkeypatch, platform):
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(cm, "read_free_commit", lambda: pytest.fail("GlobalMemoryStatusEx read off Windows"))
        fake = SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=11305 << 20, total=63168 << 20))
        monkeypatch.setitem(sys.modules, "psutil", fake)
        reading = cm.read_free_commit_or_physical()
        assert set(reading) == KEYS
        assert reading == {"avail_page_file_mib": None, "total_page_file_mib": None, "avail_phys_mib": 11305,
                           "total_phys_mib": 63168, "source": "psutil"}

    def test_elsewhere_an_unreadable_psutil_is_none_never_an_exception(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

        def broken():
            raise OSError("no /proc")

        monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(virtual_memory=broken))
        assert cm.read_free_commit_or_physical() is None

    def test_the_strata_reader_stays_windows_only(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert cm.read_free_commit() is None, "local_engine_status and the Strata supervisor keep None off Windows"

    @pytest.mark.asyncio
    async def test_the_command_carries_the_reading_off_windows_too(self, monkeypatch):
        from dpc_client_core.service import CoreService

        reading = {"avail_page_file_mib": None, "total_page_file_mib": None, "avail_phys_mib": 1,
                   "total_phys_mib": 2, "source": "psutil"}
        monkeypatch.setattr(cm, "read_free_commit_or_physical", lambda: dict(reading))
        monkeypatch.setattr(lss, "read_vram_mib", lambda: (None, None))
        out = await CoreService.list_processes(SimpleNamespace())
        assert out["status"] == "success" and out["free_commit"] == reading
