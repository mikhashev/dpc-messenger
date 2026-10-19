"""Free commit by GlobalMemoryStatusEx, named where the Strata supervisor talks about commit (ADR-044).

The reference is free commit, `ullAvailPageFile` of `ullTotalPageFile` (Mike's
call, 2026-10-19); `ullAvailPhys` is printed after it. The kernel32 call is
faked at `commit_memory._global_memory_status_ex`, the OS at `sys.platform`,
so every case runs on Windows, Linux and macOS alike.
"""

import ctypes
import logging
import sys
import time
from pathlib import Path

import pytest

from dpc_client_core.managers import commit_memory as cm
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers import strata_supervisor as mod
from dpc_client_core.managers.strata_supervisor import StrataSupervisor
from dpc_client_core.providers import strata_provider

MIB = 1 << 20
READING = {
    "avail_page_file_mib": 3686,
    "total_page_file_mib": 108236,
    "avail_phys_mib": 11305,
    "total_phys_mib": 63168,
    "source": "GlobalMemoryStatusEx",
}


def _fake_call(seen):
    def call(status):
        seen.append(status.dwLength)
        status.ullAvailPageFile = 3686 * MIB + 5
        status.ullTotalPageFile = 108236 * MIB
        status.ullAvailPhys = 11305 * MIB
        status.ullTotalPhys = 63168 * MIB
        return True
    return call


@pytest.fixture
def home(tmp_path, monkeypatch):
    dpc = tmp_path / "dpc-home"
    dpc.mkdir()
    monkeypatch.setattr(base_mod, "DPC_HOME", dpc)
    return dpc


def _sup(tmp_path: Path) -> StrataSupervisor:
    return StrataSupervisor("flash", {
        "strata_install_dir": str(tmp_path / "Strata"),
        "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 15360,
    })


class TestTheReader:
    def test_off_windows_it_returns_none_and_says_not_measured(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(cm, "_global_memory_status_ex", lambda s: pytest.fail("called off Windows"))
        assert cm.read_free_commit() is None
        assert "not measured on this OS" in cm.describe_free_commit(None)

    def test_on_windows_it_reads_both_halves_in_mib_with_dwlength_set(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        seen = []
        monkeypatch.setattr(cm, "_global_memory_status_ex", _fake_call(seen))
        assert cm.read_free_commit() == READING
        assert seen == [ctypes.sizeof(cm.MEMORYSTATUSEX)] == [64]

    @pytest.mark.parametrize("call", [lambda s: False, lambda s: 1 / 0])
    def test_a_failed_call_is_none_never_an_exception(self, monkeypatch, call):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(cm, "_global_memory_status_ex", call)
        assert cm.read_free_commit() is None
        assert "unread" in cm.describe_free_commit(None)

    def test_the_text_leads_with_free_commit_and_names_each_field(self):
        text = cm.describe_free_commit(READING)
        assert text.index("ullAvailPageFile") < text.index("ullAvailPhys")
        assert "free commit ullAvailPageFile 3686 of ullTotalPageFile 108236 MiB" in text
        assert "ullAvailPhys 11305 of ullTotalPhys 63168 MiB" in text
        assert text.endswith("(GlobalMemoryStatusEx)")


class TestTheSupervisor:
    def test_the_start_line_names_free_commit_beside_the_cache_budget(self, tmp_path, home, monkeypatch, caplog):
        monkeypatch.setattr(mod, "read_free_commit", lambda: dict(READING))
        sup = _sup(tmp_path)
        with caplog.at_level(logging.INFO, logger=mod.__name__):
            sup.log_start(None, {}, binary=Path("python.exe"))
        [line] = [r.getMessage() for r in caplog.records if "starting on" in r.getMessage()]
        assert "conversation_cache_mib=15360 beside free commit ullAvailPageFile 3686" in line
        assert "GlobalMemoryStatusEx" in line
        assert sup.last_free_commit["avail_page_file_mib"] == 3686

    def test_off_windows_the_start_line_says_not_measured(self, tmp_path, home, monkeypatch, caplog):
        monkeypatch.setattr(sys, "platform", "darwin")
        sup = _sup(tmp_path)
        with caplog.at_level(logging.INFO, logger=mod.__name__):
            sup.log_start(None, {}, binary=Path("python"))
        assert any("not measured on this OS" in r.getMessage() for r in caplog.records)
        assert sup.last_free_commit is None

    @pytest.mark.asyncio
    async def test_a_death_carries_the_free_commit_read_before_and_after_it(self, tmp_path, home, monkeypatch):
        sup = _sup(tmp_path)
        sup.last_free_commit = {**READING, "at": time.monotonic() - 4}
        monkeypatch.setattr(mod, "read_free_commit", lambda: {**READING, "avail_page_file_mib": 40000})
        await sup._fail_closed("the launcher reports the engine unloaded", stop=False)
        reason = sup.failure["reason"]
        assert "commit limit" in reason and "possibly" not in reason
        assert "free commit ullAvailPageFile 3686 of ullTotalPageFile 108236 MiB" in reason
        assert "4 s before the death" in reason
        assert "free commit ullAvailPageFile 40000" in reason and "after it" in reason
        assert sup.failure["free_commit"]["before"]["avail_page_file_mib"] == 3686
        assert sup.failure["free_commit"]["after"]["avail_page_file_mib"] == 40000

    @pytest.mark.asyncio
    async def test_off_windows_a_death_says_nothing_was_measured(self, tmp_path, home, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        sup = _sup(tmp_path)
        await sup._fail_closed("the launcher exited on its own with 1", stop=False)
        reason = sup.failure["reason"]
        assert "possibly the Windows commit limit" in reason
        assert "not measured on this OS" in reason
        assert sup.failure["free_commit"] == {"before": None, "after": None}

    @pytest.mark.asyncio
    async def test_a_refusal_that_is_not_a_death_carries_no_reading(self, tmp_path, home, monkeypatch):
        monkeypatch.setattr(mod, "read_free_commit", lambda: pytest.fail("read for a refusal"))
        sup = _sup(tmp_path)
        await sup._fail_closed(mod.NO_ENGINE_VERSION, note="mock engine", stop=False)
        assert "free commit" not in sup.failure["reason"]
        assert sup.failure["free_commit"] is None


class TestTheStatus:
    @pytest.mark.asyncio
    async def test_the_engine_status_carries_the_reading_for_the_hint(self, monkeypatch):
        monkeypatch.setattr(strata_provider, "read_free_commit", lambda: dict(READING))
        out = await strata_provider.local_engine_status()
        assert out["free_commit"] == READING
