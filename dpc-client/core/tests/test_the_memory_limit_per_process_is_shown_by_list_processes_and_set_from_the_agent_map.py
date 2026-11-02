"""The memory limit per process: shown by `list_processes`, changed by `set_shell_memory_limit`.

The key `[system] shell_memory_limit_mb` was edited by hand or by an environment
variable only. The Agent map now shows the limit in force and where it comes from
(`shell_memory_limit_mb`, `shell_memory_limit_source`: `env`, `config`, `default`)
and lets the owner change it (`set_shell_memory_limit`, param `mb`, 0 = no limit).
Team chat, 2026-11-02. What is asserted:

- `read_memory_limit` returns (MB, source) for either environment name, config.ini
  and the default, and a bad value falls back to 8192 (a bad config value reports
  `default`, a bad environment value reports `env`, because the variable still
  shadows the file); `_read_memory_ceiling` is its first element, never another reading;
- `list_processes` carries both fields beside `free_commit`;
- the command writes the key through Settings, refuses a negative or non-integer
  value and any change while an environment variable decides, and the next command
  sees the new value with no restart, even for a same-length value written within
  the clock tick the reader's cache is keyed on.

Every home is under tmp_path; `DPC_HOME` points there, never at `~/.dpc`. No process of
size is started: the end-to-end check runs `python -c pass`.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.tools import process as process_mod
from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.managers import commit_memory as cm
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

ENV_OLD = "DPC_SHELL_MEMORY_LIMIT_MB"
ENV_GENERIC = "DPC_SYSTEM_SHELL_MEMORY_LIMIT_MB"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A DPC home of its own, no environment override, and the reader's caches empty."""
    monkeypatch.delenv(ENV_OLD, raising=False)
    monkeypatch.delenv(ENV_GENERIC, raising=False)
    monkeypatch.setenv("DPC_HOME", str(tmp_path))
    monkeypatch.setattr(process_mod, "_bad_ceilings_announced", set(), raising=False)
    monkeypatch.setattr(process_mod, "_config_cache", (None, None), raising=False)
    return tmp_path


_edits = [0]


def write_config(home, value=None, body=None):
    """Write config.ini as a later edit: its mtime moves even within one tick."""
    path = home / "config.ini"
    if body is None:
        body = "[hub]\nurl = http://localhost:8000\n\n[system]\nauto_collect_device_info = true\n"
        if value is not None:
            body += f"shell_memory_limit_mb = {value}\n"
    path.write_text(body)
    _edits[0] += 1
    stamp = 1_700_000_000_000_000_000 + _edits[0] * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))
    return path


def host(home):
    return SimpleNamespace(settings=Settings(home))


# ---------------------------------------------------------------- the reader


class TestTheReaderSaysWhereTheLimitComesFrom:
    def test_the_environment_is_source_env_and_the_old_name_beats_the_generic_one(self, home, monkeypatch):
        write_config(home, 1000)
        monkeypatch.setenv(ENV_GENERIC, "3000")
        assert process_mod.read_memory_limit() == (3000, "env")
        monkeypatch.setenv(ENV_OLD, "2000")
        assert process_mod.read_memory_limit() == (2000, "env")
        assert process_mod.memory_limit_env_name() == ENV_OLD

    def test_config_is_source_config_and_the_environment_name_is_none(self, home):
        write_config(home, 1000)
        assert process_mod.read_memory_limit() == (1000, "config")
        assert process_mod.memory_limit_env_name() is None

    def test_a_missing_key_or_a_missing_file_is_source_default(self, home):
        assert process_mod.read_memory_limit() == (8192, "default"), "no config.ini at all"
        write_config(home)
        assert process_mod.read_memory_limit() == (8192, "default"), "config.ini without the key"

    def test_zero_is_no_limit_from_either_source(self, home, monkeypatch):
        write_config(home, 0)
        assert process_mod.read_memory_limit() == (0, "config")
        monkeypatch.setenv(ENV_OLD, "0")
        assert process_mod.read_memory_limit() == (0, "env")

    def test_a_bad_config_value_runs_the_default_and_reports_default(self, home):
        """What runs is 8192, and a new value in the file fixes it: not `config`."""
        write_config(home, "lots")
        assert process_mod.read_memory_limit() == (8192, "default")

    def test_a_bad_environment_value_runs_the_default_but_still_reports_env(self, home, monkeypatch):
        """The variable shadows the file even when it is nonsense (as before), so an edit
        of config.ini would change nothing and the source must say so."""
        write_config(home, 1000)
        for bad in ("lots", ""):
            monkeypatch.setenv(ENV_OLD, bad)
            assert process_mod.read_memory_limit() == (8192, "env"), repr(bad)
            assert process_mod.memory_limit_env_name() == ENV_OLD

    @pytest.mark.parametrize("scenario", ["env_old", "env_generic", "config", "absent", "zero", "bad_config", "bad_env"])
    def test_the_ceiling_a_command_is_held_to_is_the_readers_number(self, home, monkeypatch, scenario):
        if scenario == "env_old":
            monkeypatch.setenv(ENV_OLD, "2000")
        elif scenario == "env_generic":
            monkeypatch.setenv(ENV_GENERIC, "3000")
        elif scenario == "config":
            write_config(home, 1000)
        elif scenario == "absent":
            write_config(home)
        elif scenario == "zero":
            write_config(home, 0)
        elif scenario == "bad_config":
            write_config(home, "lots")
        elif scenario == "bad_env":
            monkeypatch.setenv(ENV_OLD, "lots")
        assert process_mod._read_memory_ceiling() == process_mod.read_memory_limit()[0]

    def test_the_hint_a_refusal_carries_points_at_the_agent_map_field(self):
        assert "Memory limit per process" in process_mod.CEILING_HINT
        assert "shell_memory_limit_mb" in process_mod.CEILING_HINT
        assert ENV_OLD in process_mod.CEILING_HINT


# ------------------------------------------------------------- list_processes


class TestListProcessesCarriesBothFields:
    @pytest.fixture(autouse=True)
    def _quiet_system(self, monkeypatch):
        monkeypatch.setattr(cm, "read_free_commit_or_physical", lambda: None)
        monkeypatch.setattr(cm, "read_vram_mib", lambda: (None, None))

    @pytest.mark.asyncio
    async def test_a_value_in_config(self, home):
        write_config(home, 32768)
        out = await CoreService.list_processes(SimpleNamespace())
        assert out["status"] == "success"
        assert (out["shell_memory_limit_mb"], out["shell_memory_limit_source"]) == (32768, "config")
        assert "free_commit" in out, "beside free_commit, not instead of it"

    @pytest.mark.asyncio
    async def test_the_environment_and_the_default_and_zero(self, home, monkeypatch):
        write_config(home)
        out = await CoreService.list_processes(SimpleNamespace())
        assert (out["shell_memory_limit_mb"], out["shell_memory_limit_source"]) == (8192, "default")
        monkeypatch.setenv(ENV_GENERIC, "4096")
        out = await CoreService.list_processes(SimpleNamespace())
        assert (out["shell_memory_limit_mb"], out["shell_memory_limit_source"]) == (4096, "env")
        monkeypatch.setenv(ENV_OLD, "0")
        out = await CoreService.list_processes(SimpleNamespace())
        assert (out["shell_memory_limit_mb"], out["shell_memory_limit_source"]) == (0, "env")

    @pytest.mark.asyncio
    async def test_the_answer_is_what_the_next_command_will_be_held_to(self, home):
        write_config(home, 1500)
        out = await CoreService.list_processes(SimpleNamespace())
        run = process_mod.run_supervised([sys.executable, "-c", "pass"], launcher="test", timeout=30)
        assert run.ceiling_mb == out["shell_memory_limit_mb"] == 1500


# ------------------------------------------------------------------ the command


class TestSetShellMemoryLimit:
    def test_the_command_is_allowed_and_is_a_service_method(self):
        assert "set_shell_memory_limit" in ALLOWED_COMMANDS
        assert callable(getattr(CoreService, "set_shell_memory_limit", None))

    @pytest.mark.asyncio
    async def test_it_writes_the_key_and_answers_with_the_effective_value_and_source(self, home):
        out = await CoreService.set_shell_memory_limit(host(home), mb=32768)
        assert out == {"status": "success", "shell_memory_limit_mb": 32768, "shell_memory_limit_source": "config"}
        assert Settings(home).get("system", "shell_memory_limit_mb") == "32768", "written through Settings"

    @pytest.mark.asyncio
    async def test_the_next_command_sees_it_without_a_restart(self, home):
        write_config(home, 1000)
        first = process_mod.run_supervised([sys.executable, "-c", "pass"], launcher="test", timeout=30)
        await CoreService.set_shell_memory_limit(host(home), mb=3000)
        second = process_mod.run_supervised([sys.executable, "-c", "pass"], launcher="test", timeout=30)
        assert (first.ceiling_mb, second.ceiling_mb) == (1000, 3000)
        assert process_mod._read_memory_ceiling() == 3000

    @pytest.mark.asyncio
    async def test_a_same_length_value_written_within_one_clock_tick_is_still_seen(self, home):
        """The reader's cache is keyed on (mtime_ns, size); 1000 -> 2000 changes neither
        when the clock has not moved, so the command must tell the reader itself."""
        settings = Settings(home)
        service = SimpleNamespace(settings=settings)
        await CoreService.set_shell_memory_limit(service, mb=1000)
        assert process_mod.read_memory_limit() == (1000, "config")
        pinned = process_mod._config_cache[0][1]
        original_set = settings.set

        def set_within_the_same_tick(section, key, value):
            original_set(section, key, value)
            os.utime(settings.config_file, ns=(pinned, pinned))

        settings.set = set_within_the_same_tick
        out = await CoreService.set_shell_memory_limit(service, mb=2000)
        assert out["shell_memory_limit_mb"] == 2000
        assert process_mod._config_cache[0][1] == pinned, "the premise: the stamp did not move"
        assert process_mod.read_memory_limit() == (2000, "config")

    @pytest.mark.asyncio
    async def test_zero_means_no_limit(self, home):
        out = await CoreService.set_shell_memory_limit(host(home), mb=0)
        assert out == {"status": "success", "shell_memory_limit_mb": 0, "shell_memory_limit_source": "config"}
        run = process_mod.run_supervised([sys.executable, "-c", "pass"], launcher="test", timeout=30)
        assert run.ceiling_mb == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [-1, -4096, "lots", "4096", 1.5, 4096.0, True, None])
    async def test_a_negative_or_non_integer_value_is_refused_with_a_reason_and_writes_nothing(self, home, bad):
        write_config(home, 1000)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.set_shell_memory_limit(host(home), mb=bad)
        assert out["status"] == "error" and out["message"]
        assert "mb" in out["message"]
        assert (home / "config.ini").read_bytes() == before
        assert process_mod.read_memory_limit() == (1000, "config")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("names", [(ENV_OLD,), (ENV_GENERIC,), (ENV_OLD, ENV_GENERIC)])
    async def test_while_an_environment_variable_decides_it_refuses_and_names_the_variable(self, home, monkeypatch, names):
        write_config(home, 1000)
        before = (home / "config.ini").read_bytes()
        for name in names:
            monkeypatch.setenv(name, "2000")
        out = await CoreService.set_shell_memory_limit(host(home), mb=4096)
        assert out["status"] == "error"
        assert names[0] in out["message"], "the variable that is in force, the old name first"
        assert (home / "config.ini").read_bytes() == before, "editing config would have changed nothing"
        assert process_mod.read_memory_limit() == (2000, "env")

    @pytest.mark.asyncio
    async def test_a_nonsense_environment_value_still_blocks_the_edit(self, home, monkeypatch):
        monkeypatch.setenv(ENV_OLD, "lots")
        out = await CoreService.set_shell_memory_limit(host(home), mb=4096)
        assert out["status"] == "error" and ENV_OLD in out["message"]

    @pytest.mark.asyncio
    async def test_a_bad_value_in_the_file_can_be_replaced_from_the_map(self, home):
        write_config(home, "lots")
        assert process_mod.read_memory_limit() == (8192, "default")
        out = await CoreService.set_shell_memory_limit(host(home), mb=2048)
        assert out == {"status": "success", "shell_memory_limit_mb": 2048, "shell_memory_limit_source": "config"}
