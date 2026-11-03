"""A failed Strata start names the engine's arena warning and the field that answers it.

The engine only warns that the expert arena does not fit, loads on, and may exit later, so
the warning is usually outside the launcher's last 12 lines, the only ones a failed start
quoted. The lines below are copied from this machine's engine log of 2026-11-02 13:37.
The numbers in the note are the engine's, and the note says so. Spawn and health are
faked; nothing is started.
"""

import sys
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import commit_memory as cm
from dpc_client_core.managers import strata_supervisor
from dpc_client_core.managers.llama_server_supervisor import LlamaServerError
from dpc_client_core.providers import strata_provider

from tests.test_strata_supervisor import (  # noqa: F401
    _FakeProc, _install, _started_through_the_real_launch, _sup, home,
)

ARENA_WARNING = (
    "strata generate: WARNING: the expert arena needs 46.84 GiB of RAM but 33.10 GiB is available "
    "(13.74 GiB short, after waiting): the load may swap or be stopped by the OS. Close other programs, "
    "or run with less RAM: --mmap-experts with --resident-budget-gib N"
)
ARENA_WAITING = (
    "strata generate: the expert arena needs 46.84 GiB of RAM and 32.88 GiB is available; waiting up to "
    "20 s for it (another engine still exiting?) - 0 s so far (STRATA_ARENA_RAM_WAIT_S changes this)"
)
THE_EXIT = "strata generate: the embedding buffer failed"
FIELD = "resident_budget_gib"
# The other line of `src/core/expert_source.cpp` that opens with these words: it is a refusal
# of a cgroup limit, not the shortfall the field answers. Not on this machine's logs.
ARENA_CGROUP = (
    "ArenaExpertSource: the expert arena needs 46.84 GiB of RAM but this process's memory limit "
    "(cgroup memory.max / memory.limit_in_bytes) is 32.00 GiB: it would be killed while loading."
)


class TestTheLineItLooksFor:
    @pytest.mark.parametrize("line", [
        pytest.param(ARENA_WARNING, id="the-line-of-2026-11-02"),
        pytest.param(ARENA_WARNING.replace(", after waiting", ""), id="no-wait-before-it"),
        pytest.param("WARNING: the expert arena needs 50 GB of RAM but 33 GB is available (17 GB short)",
                     id="another-unit-after-an-engine-update"),
    ])
    def test_the_warning_is_matched(self, line):
        assert strata_supervisor._ARENA_SHORT_RE.search(line)

    @pytest.mark.parametrize("line", [
        pytest.param(ARENA_WAITING, id="still-waiting"),
        pytest.param(ARENA_CGROUP, id="the-cgroup-refusal"),
        pytest.param(THE_EXIT, id="the-exit"),
    ])
    def test_nothing_else_is(self, line):
        assert not strata_supervisor._ARENA_SHORT_RE.search(line)


def _append(path, *lines):
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(f"{line}\n" for line in lines))


def _dies_before_healthy(sup, monkeypatch, *engine_lines):
    """A launcher whose engine writes `engine_lines` and which has exited with 1; its own log
    ends in 12 other lines, as the launcher's did on 2026-11-02."""
    _started_through_the_real_launch(sup, monkeypatch, metrics={})

    async def spawn(cmd, env):
        _append(sup._engine_log_path, *engine_lines)
        _append(sup._log_path, *[f"  strata generate: line {i}" for i in range(11)], f"  {THE_EXIT}")
        return _FakeProc(returncode=1)

    sup._spawn = spawn


class TestAStartThatDiedBeforeHealthy:
    @pytest.mark.asyncio
    async def test_the_error_quotes_the_engines_line_says_whose_it_is_and_names_the_field(
        self, tmp_path, home, monkeypatch,
    ):
        sup = _sup(_install(tmp_path))
        _dies_before_healthy(sup, monkeypatch, ARENA_WAITING, ARENA_WARNING, THE_EXIT)
        with pytest.raises(LlamaServerError) as excinfo:
            await sup.ensure_running()
        text = str(excinfo.value)
        assert ARENA_WARNING in text
        assert "engine's numbers" in text and "not the Add Provider form's reading" in text
        assert FIELD in text and "--resident-budget-gib" in text and "'flash next'" in text
        # The launcher's own lines stay, and the note comes ahead of them.
        assert THE_EXIT in text
        assert text.index(FIELD) < text.index("  | ")
        assert not any("expert arena" in line for line in excinfo.value.log_lines), (
            "the launcher's tail alone misses the warning, which is why the engine log is read"
        )

    @pytest.mark.asyncio
    async def test_it_stays_the_builds_fault_so_a_new_pin_still_rolls_back(self, tmp_path, home, monkeypatch):
        sup = _sup(_install(tmp_path))
        _dies_before_healthy(sup, monkeypatch, ARENA_WARNING)
        with pytest.raises(LlamaServerError) as excinfo:
            await sup.ensure_running()
        assert excinfo.value._build_fault is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine_lines", [
        pytest.param((THE_EXIT,), id="another-failure"),
        pytest.param((ARENA_WAITING, THE_EXIT), id="only-the-waiting-line"),
    ])
    async def test_no_note_without_the_warning(self, tmp_path, home, monkeypatch, engine_lines):
        sup = _sup(_install(tmp_path))
        _dies_before_healthy(sup, monkeypatch, *engine_lines)
        with pytest.raises(LlamaServerError) as excinfo:
            await sup.ensure_running()
        assert FIELD not in str(excinfo.value)
        assert str(excinfo.value).startswith("strata[flash next] exited with 1 before becoming healthy\n")

    @pytest.mark.asyncio
    async def test_an_earlier_starts_warning_is_not_this_ones(self, tmp_path, home, monkeypatch):
        sup = _sup(_install(tmp_path))
        sup._engine_log_path.parent.mkdir(parents=True, exist_ok=True)
        _append(sup._engine_log_path, ARENA_WARNING)
        _dies_before_healthy(sup, monkeypatch, THE_EXIT)
        with pytest.raises(LlamaServerError) as excinfo:
            await sup.ensure_running()
        assert FIELD not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_log_that_shrank_is_read_from_its_start(self, tmp_path, home, monkeypatch):
        sup = _sup(_install(tmp_path))
        sup._engine_log_path.parent.mkdir(parents=True, exist_ok=True)
        _append(sup._engine_log_path, "x" * 5000)
        sup.prime_restore_scan()
        sup._engine_log_path.write_text(f"{ARENA_WARNING}\n", encoding="utf-8")
        assert ARENA_WARNING in sup._arena_short_note()


class TestARefusedStartThatLatches:
    @pytest.mark.asyncio
    async def test_the_reason_carries_the_note_after_a_smoke_refused(self, tmp_path, home, monkeypatch):
        sup = _sup(_install(tmp_path))
        spawned = _started_through_the_real_launch(
            sup, monkeypatch, metrics={"engine": {"version": "0.1.46"}}, smoke={"choices": []})
        real_spawn = sup._spawn

        async def spawn(cmd, env):
            _append(sup._engine_log_path, ARENA_WARNING)
            return await real_spawn(cmd, env)

        sup._spawn = spawn
        with pytest.raises(LlamaServerError, match="will not start it again"):
            await sup.ensure_running()
        assert len(spawned) == 1
        assert ARENA_WARNING in sup.failure["reason"] and FIELD in sup.failure["reason"]

    @pytest.mark.asyncio
    async def test_an_engine_that_was_up_and_died_gets_no_note(self, tmp_path, home, monkeypatch):
        sup = _sup(_install(tmp_path))
        sup._engine_log_path.parent.mkdir(parents=True, exist_ok=True)
        sup.prime_restore_scan()
        _append(sup._engine_log_path, ARENA_WARNING)
        await sup._fail_closed("the launcher exited on its own with 1", stop=False)
        assert FIELD not in sup.failure["reason"]


class TestTheStatusGivesTheFormItsReading:
    @pytest.mark.asyncio
    async def test_off_windows_it_is_physical_memory_with_commit_null(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(cm, "read_free_commit", lambda: pytest.fail("GlobalMemoryStatusEx read off Windows"))
        monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
            virtual_memory=lambda: SimpleNamespace(available=33894 << 20, total=63168 << 20)))
        out = await strata_provider.local_engine_status()
        assert out["free_commit"] == {"avail_page_file_mib": None, "total_page_file_mib": None,
                                      "avail_phys_mib": 33894, "total_phys_mib": 63168, "source": "psutil"}
