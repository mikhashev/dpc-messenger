"""ADR-046 amendment of 2026-11-06: the full command of a RUNNING process is read from
the OS on request and stored nowhere.

What is asserted: a real child with a long argument answers with that argument whole
while the registry's entry keeps its 120 characters; a wrong `started_at`, a finished
entry, a pid that changed hands, an entry with no start time and an unreadable command
line are each refused with their reason; the local API command answers `status`
success or error and is allowed; and the state file and the history are byte for byte
what they were before the request.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from dpc_client_core import process_registry as pr
from dpc_client_core.process_registry import COMMAND_MAX_CHARS, ProcessRegistry

LONG_ARGUMENT = "needle-" + "x" * 300 + "-end"


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def child():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", LONG_ARGUMENT],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture
def registry(tmp_path):
    return ProcessRegistry(tmp_path / "process_registry.json")


def _register(registry, proc):
    return registry.register(proc.pid, launcher="run_shell", launcher_kind="tool",
                             command=f"python -c sleep {LONG_ARGUMENT}", handle=proc)


class TestTheFullCommand:
    def test_a_live_child_answers_with_its_whole_command_line_and_the_entry_stays_cut(self, registry, child):
        entry = _register(registry, child)
        assert len(entry.command) == COMMAND_MAX_CHARS and LONG_ARGUMENT not in entry.command

        answer = registry.full_command_of(child.pid, entry.started_at)

        assert answer["ok"] is True and answer["truncated"] is False
        assert LONG_ARGUMENT in answer["command"]
        assert "time.sleep(30)" in answer["command"]
        assert len(answer["command"]) > COMMAND_MAX_CHARS
        (kept,) = registry.entries()
        assert len(kept.command) == COMMAND_MAX_CHARS, "reading it did not widen what is kept"

    def test_a_started_at_that_is_not_the_entrys_finds_no_entry(self, registry, child):
        entry = _register(registry, child)
        for wrong in ("2001-01-01T00:00:00.000+00:00", "", None, 5):
            answer = registry.full_command_of(child.pid, wrong)
            assert answer["ok"] is False and answer["reason"] == "no_entry", wrong
        assert registry.full_command_of(child.pid + 1, entry.started_at)["reason"] == "no_entry"
        assert registry.full_command_of(True, entry.started_at)["reason"] == "no_entry"
        assert registry.full_command_of("12", entry.started_at)["reason"] == "no_entry"

    def test_an_entry_that_ended_is_refused_with_the_ended_answer_and_the_os_is_not_read(self, registry, child):
        entry = _register(registry, child)
        registry.end(child.pid, 0)

        def must_not_read(pid):
            raise AssertionError("the OS was read for a finished entry")

        answer = registry.full_command_of(child.pid, entry.started_at, read_cmdline=must_not_read)
        assert answer["ok"] is False and answer["reason"] == "ended"
        assert str(COMMAND_MAX_CHARS) in answer["message"]

    def test_a_pid_that_changed_hands_is_refused_and_the_os_is_not_read(self, registry, child):
        entry = _register(registry, child)

        def must_not_read(pid):
            raise AssertionError("the OS was read for another process")

        answer = registry.full_command_of(child.pid, entry.started_at, probe=lambda pid, start: "other",
                                          read_cmdline=must_not_read)
        assert answer["ok"] is False and answer["reason"] == "other_process"

    def test_a_process_that_is_gone_and_an_entry_with_no_start_time_are_refused(self, registry, child):
        entry = _register(registry, child)
        gone = registry.full_command_of(child.pid, entry.started_at, probe=lambda pid, start: "gone")
        assert (gone["ok"], gone["reason"]) == (False, "not_running")
        unverifiable = registry.full_command_of(child.pid, entry.started_at, probe=lambda pid, start: "unverifiable")
        assert (unverifiable["ok"], unverifiable["reason"]) == (False, "unverifiable")

    def test_the_real_probe_refuses_an_entry_whose_recorded_start_time_no_longer_matches(self, tmp_path, child):
        registry = ProcessRegistry(tmp_path / "process_registry.json", create_time=lambda pid: 1_000.0)
        entry = _register(registry, child)
        assert entry.start_time == 1_000.0, "the entry carries a start time the live process does not have"
        answer = registry.full_command_of(child.pid, entry.started_at)
        assert answer["ok"] is False and answer["reason"] == "other_process"

    def test_an_unreadable_command_line_is_a_refusal_not_an_exception(self, registry, child):
        entry = _register(registry, child)

        def denied(pid):
            raise PermissionError("access denied")

        answer = registry.full_command_of(child.pid, entry.started_at, read_cmdline=denied)
        assert answer["ok"] is False and answer["reason"] == "unreadable" and "access denied" in answer["message"]
        empty = registry.full_command_of(child.pid, entry.started_at, read_cmdline=lambda pid: [])
        assert empty["ok"] is False and empty["reason"] == "unreadable"

    def test_a_command_line_beyond_the_cap_is_cut_and_says_so(self, registry, child, monkeypatch):
        entry = _register(registry, child)
        monkeypatch.setattr(pr, "FULL_COMMAND_MAX_CHARS", 50)
        answer = registry.full_command_of(child.pid, entry.started_at, read_cmdline=lambda pid: ["a" * 80])
        assert answer == {"ok": True, "command": "a" * 50, "truncated": True}

    @pytest.mark.parametrize("platform, expected", [("win32", 'prog "a b" c'), ("linux", "prog 'a b' c")])
    def test_the_line_is_joined_with_the_quoting_of_the_platform(self, tmp_path, child, platform, expected):
        registry = ProcessRegistry(tmp_path / "process_registry.json", platform=platform)
        entry = _register(registry, child)
        answer = registry.full_command_of(child.pid, entry.started_at, read_cmdline=lambda pid: ["prog", "a b", "c"])
        assert answer["command"] == expected

    def test_nothing_is_written_to_the_state_file_or_the_history(self, tmp_path, registry, child):
        entry = _register(registry, child)
        registry.end(child.pid, 0)
        second = ProcessRegistry(tmp_path / "other" / "process_registry.json")
        live = _register(second, child)
        before = _snapshot(tmp_path)
        assert before, "there is a state file to compare"

        registry.full_command_of(child.pid, entry.started_at)
        assert second.full_command_of(child.pid, live.started_at)["ok"] is True
        second.full_command_of(child.pid, "2001-01-01T00:00:00.000+00:00")

        assert _snapshot(tmp_path) == before


class TestTheCommand:
    def test_the_command_is_allowed_and_has_a_method(self):
        from dpc_client_core.local_api import ALLOWED_COMMANDS
        from dpc_client_core.service import CoreService

        assert "get_process_command" in ALLOWED_COMMANDS and hasattr(CoreService, "get_process_command")

    @pytest.mark.asyncio
    async def test_the_service_answers_success_with_the_command_and_error_with_a_reason(self, registry, child, monkeypatch):
        from types import SimpleNamespace

        from dpc_client_core.service import CoreService

        monkeypatch.setattr(pr, "get_registry", lambda: registry)
        entry = _register(registry, child)

        ok = await CoreService.get_process_command(SimpleNamespace(), child.pid, entry.started_at)
        assert set(ok) == {"status", "pid", "started_at", "command", "truncated"}
        assert ok["status"] == "success" and LONG_ARGUMENT in ok["command"]
        assert (ok["pid"], ok["started_at"], ok["truncated"]) == (child.pid, entry.started_at, False)

        refused = await CoreService.get_process_command(SimpleNamespace(), child.pid, "2001-01-01T00:00:00.000+00:00")
        assert set(refused) == {"status", "reason", "message"}
        assert refused["status"] == "error" and refused["reason"] == "no_entry"

    @pytest.mark.asyncio
    async def test_a_failure_answers_as_an_error_not_an_exception(self, monkeypatch):
        from types import SimpleNamespace

        from dpc_client_core.service import CoreService

        def broken(*args, **kwargs):
            raise RuntimeError("the registry could not be read")

        monkeypatch.setattr(ProcessRegistry, "full_command_of", broken)
        out = await CoreService.get_process_command(SimpleNamespace(), 1, "x")
        assert out == {"status": "error", "reason": "unreadable", "message": "the registry could not be read"}
