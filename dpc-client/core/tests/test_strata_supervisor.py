"""The Strata supervisor (ADR-044): the start config DPC owns, the command, fail-closed death.

No engine runs here. A fake Strata install in tmp_path carries a base run
config shaped like the real `strata-iq3_xxs-lowmem-park.json`; the process,
`/metrics` and the VRAM reader are faked at the supervisor's own seams.
"""

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers import strata_supervisor as mod
from dpc_client_core.managers.llama_server_supervisor import LlamaServerError
from dpc_client_core.managers.strata_supervisor import StrataSupervisor, exit_code_words

BASE_ARGS = [
    "--pack", "D:/packs/iq3_xxs",
    "--native", "D:/models/flash-next-00001-of-00002.gguf",
    "--max-context", "261120",
    "--kv", "int8",
    "--vision",
    "--vram-reserve-mib", "1536",
    "--control-vector-scaled", "D:/cv/refusal.gguf:1.0",
    "--resident-budget-gib", "24",
    "--control-vector-layer-range", "4", "44",
    "--cvec-mode", "project",
    "--cvec-dir", "per-layer",
    "--conversation-cache-mib", "4096",
]


def _install(tmp_path: Path, **base_overrides) -> Path:
    root = tmp_path / "Strata"
    (root / "serve").mkdir(parents=True)
    (root / "serve" / "server.py").write_text("# launcher\n", encoding="utf-8")
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    tok = root / "tokenizer"
    tok.mkdir()
    (tok / "chat_template.jinja").write_text(
        "{%- if reasoning_effort not in ('xhigh', 'medium', 'low') %}{{ raise_exception('x') }}{% endif %}"
        "{%- set e = reasoning_effort | default('xhigh') %}",
        encoding="utf-8",
    )
    base = {
        "exe": str(root / "engine" / "strata.exe"),
        "args": list(BASE_ARGS),
        "cwd": str(root),
        "tokenizer": str(tok),
        "model_name": "qwen3.8-flash-next-iq3_xxs",
        "log": str(root / "strata-base.log"),
        "port": 8080,
        "sampling": {"top_k": 20},
        "vision": {"exe": str(root / "engine" / "strata-vision.exe"), "mmproj": "D:/mmproj.gguf"},
    }
    base.update(base_overrides)
    (root / "strata-base.json").write_text(json.dumps(base, indent=1), encoding="utf-8")
    return root


@pytest.fixture
def home(tmp_path, monkeypatch):
    dpc = tmp_path / "dpc-home"
    dpc.mkdir()
    monkeypatch.setattr(base_mod, "DPC_HOME", dpc)
    return dpc


def _sup(root: Path, **overrides) -> StrataSupervisor:
    config = {
        "strata_install_dir": str(root),
        "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240,
        "engine_watch_interval_s": 0.01,
        "vram_release_timeout_s": 0.05,
    }
    config.update(overrides)
    return StrataSupervisor("flash next", config)


def _value(args, flag, n=1):
    i = args.index(flag)
    return args[i + 1] if n == 1 else args[i + 1:i + 1 + n]


class _FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.calls = []

    def send_signal(self, sig):
        self.calls.append(("signal", sig))

    def kill(self):
        self.calls.append(("kill",))

    async def wait(self):
        self.returncode = 0
        return 0


async def _ret(value):
    return value


class TestTheCommand:
    def test_windows_runs_the_venv_python_with_the_strata_engine_and_an_explicit_port(
        self, tmp_path, home, monkeypatch
    ):
        root = _install(tmp_path)
        monkeypatch.setattr(sys, "platform", "win32")
        sup = _sup(root)
        python = sup.python_path()
        assert python == root / ".venv" / "Scripts" / "python.exe"
        cmd = sup.build_command(python, 51234)
        assert cmd[0] == str(python)
        assert cmd[1] == str(root / "serve" / "server.py")
        # The launcher defaults to the mock engine and to its own port.
        assert _value(cmd, "--engine") == "strata"
        assert _value(cmd, "--port") == "51234"
        assert _value(cmd, "--host") == "127.0.0.1"
        assert _value(cmd, "--config") == str(home / "strata" / "flash_next.json")

    def test_off_windows_the_venv_python_is_bin_python(self, tmp_path, home, monkeypatch):
        root = _install(tmp_path)
        monkeypatch.setattr(sys, "platform", "linux")
        assert _sup(root).python_path() == root / ".venv" / "bin" / "python"

    @pytest.mark.asyncio
    async def test_the_child_runs_in_the_install_dir(self, tmp_path, home, monkeypatch):
        root = _install(tmp_path)
        sup = _sup(root)
        seen = {}

        async def fake_exec(*cmd, **kw):
            seen.update(kw)
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        await sup._spawn(["python"], {})
        assert seen["cwd"] == str(root)
        assert seen["creationflags"] == base_mod._CREATION_NEW_PROCESS_GROUP


class TestTheStartConfig:
    def test_typed_fields_replace_existing_flags_and_add_missing_ones(self, tmp_path, home):
        root = _install(tmp_path)
        cfg = _sup(root, conversation_cache_slots=4, max_context=131072).build_start_config(5000)
        args = cfg["args"]
        assert _value(args, "--conversation-cache-mib") == "10240"
        assert args.count("--conversation-cache-mib") == 1
        assert _value(args, "--max-context") == "131072"
        # Absent from the base, so appended.
        assert _value(args, "--conversation-cache-slots") == "4"
        assert _value(args, "--pack") == "D:/packs/iq3_xxs"

    def test_vision_off_drops_the_flag_and_the_vision_block(self, tmp_path, home):
        cfg = _sup(_install(tmp_path), vision=False).build_start_config(5000)
        assert "--vision" not in cfg["args"]
        assert "vision" not in cfg

    def test_vision_on_needs_a_vision_block(self, tmp_path, home):
        root = _install(tmp_path, vision=None)
        with pytest.raises(LlamaServerError, match="no \"vision\" block"):
            _sup(root, vision=True).build_start_config(5000)

    def test_control_vector_off_drops_every_cvec_flag_and_its_values_only(self, tmp_path, home):
        args = _sup(_install(tmp_path), control_vector=False).build_start_config(5000)["args"]
        assert not [a for a in args if a.startswith("--control-vector") or a.startswith("--cvec-")]
        for value in ("D:/cv/refusal.gguf:1.0", "4", "44", "project", "per-layer"):
            assert value not in args
        # The flag between two cvec flags survives with its value.
        assert _value(args, "--resident-budget-gib") == "24"

    def test_unset_vision_and_control_vector_keep_the_base(self, tmp_path, home):
        cfg = _sup(_install(tmp_path)).build_start_config(5000)
        assert "--vision" in cfg["args"] and "vision" in cfg
        assert _value(cfg["args"], "--cvec-mode") == "project"

    def test_model_params_land_where_strata_runconfig_keeps_them(self, tmp_path, home):
        cfg = _sup(_install(tmp_path), model_params={
            "sampling.temperature": 0.6,
            "sampling.top_k": None,
            "reasoning_budget_tokens": 4096,
            "vram_reserve_mib": 900,
            "idle_unload_s": 0,
        }).build_start_config(5000)
        assert cfg["sampling"] == {"temperature": 0.6}
        assert cfg["reasoning_budget_tokens"] == 4096
        assert _value(cfg["args"], "--vram-reserve-mib") == "900"
        assert cfg["args"].count("--vram-reserve-mib") == 1
        assert cfg["idle_unload_s"] == 0

    def test_model_params_cannot_reach_the_structural_keys(self, tmp_path, home):
        with pytest.raises(LlamaServerError, match="'exe'"):
            _sup(_install(tmp_path), model_params={"exe": "evil.exe"}).build_start_config(1)

    def test_extra_args_come_last(self, tmp_path, home):
        args = _sup(_install(tmp_path), extra_args=["--spec", "4"]).build_start_config(1)["args"]
        assert args[-2:] == ["--spec", "4"]

    def test_there_is_no_default_conversation_cache_budget(self, tmp_path, home):
        root = _install(tmp_path)
        sup = _sup(root, conversation_cache_mib=None)
        with pytest.raises(LlamaServerError, match="conversation_cache_mib is not set"):
            sup.write_start_config(5000)
        assert not (home / "strata").exists()

    def test_the_file_is_written_under_dpc_home_and_the_install_is_untouched(self, tmp_path, home):
        root = _install(tmp_path)
        before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        sup = _sup(root)
        path = sup.write_start_config(5000)
        assert path == home / "strata" / "flash_next.json"
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["port"] == 5000 and written["host"] == "127.0.0.1"
        # The base config's log sits in the install; DPC's start logs to its own.
        assert Path(written["log"]).parent == home / "logs"
        after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        assert after == before
        assert sup.start_config_used == path

    def test_a_dpc_home_inside_the_install_is_refused(self, tmp_path, monkeypatch):
        root = _install(tmp_path)
        monkeypatch.setattr(base_mod, "DPC_HOME", root / "dpc")
        with pytest.raises(LlamaServerError, match="inside the Strata install"):
            _sup(root).write_start_config(5000)


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_a_recorded_death_refuses_a_start_until_reset(self, tmp_path, home, monkeypatch):
        sup = _sup(_install(tmp_path))
        launches = []

        async def fake_launch(binary, cache_type=None):
            launches.append(binary)
            sup._proc = _FakeProc()
            sup.props = {"modalities": {"vision": True}}
            return sup.props

        monkeypatch.setattr(sup, "_launch", fake_launch)
        sup.failure = {"reason": "the launcher reports the engine unloaded; exit code 0xC0000409",
                       "log_tail": []}
        with pytest.raises(LlamaServerError, match="will not start it again") as excinfo:
            await sup.ensure_running()
        assert "0xC0000409" in str(excinfo.value)
        assert launches == []

        assert sup.reset_failure()["reason"].startswith("the launcher")
        assert await sup.ensure_running() == {"modalities": {"vision": True}}
        assert len(launches) == 1

    @pytest.mark.asyncio
    async def test_a_launcher_that_exited_on_its_own_is_not_relaunched(self, tmp_path, home, monkeypatch):
        sup = _sup(_install(tmp_path))
        monkeypatch.setattr(sup, "_launch", lambda *a, **k: pytest.fail("relaunched"))
        sup._proc = _FakeProc(returncode=3221226505)
        with pytest.raises(LlamaServerError, match="will not start it again"):
            await sup.ensure_running()
        assert "3221226505" in sup.failure["reason"]

    @pytest.mark.asyncio
    async def test_the_watcher_stops_the_launcher_on_an_unexplained_unload(self, tmp_path, home, monkeypatch):
        sup = _sup(_install(tmp_path))
        sup.write_start_config(5000)
        proc = _FakeProc()
        sup._proc, sup.props = proc, {"x": 1}
        log = home / "logs"
        log.mkdir()
        (log / "strata-flash_next.log").write_text(
            "[strata] the engine stopped unexpectedly (exit code 3221226505)\n", encoding="utf-8")
        monkeypatch.setattr(sup, "_get", lambda path: _ret({"live": {"state": "unloaded"}}))
        sup._start_watch()
        await asyncio.wait_for(sup._watch_task, timeout=5)
        assert sup.failure is not None
        assert "0xC0000409" in sup.failure["reason"]
        assert "commit limit" in sup.failure["reason"]
        expected = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
        assert ("signal", expected) in proc.calls
        assert sup._proc is None and sup.props is None
        with pytest.raises(LlamaServerError, match="will not start it again"):
            await sup.ensure_running()

    @pytest.mark.asyncio
    async def test_an_unload_the_alias_configured_is_not_a_death(self, tmp_path, home, monkeypatch):
        sup = _sup(_install(tmp_path), model_params={"idle_unload_s": 600})
        sup.write_start_config(5000)
        sup._proc, sup.props = _FakeProc(), {"x": 1}
        polls = []

        async def fake_get(path):
            polls.append(path)
            return {"live": {"state": "unloaded"}}

        monkeypatch.setattr(sup, "_get", fake_get)
        sup._start_watch()
        while len(polls) < 5:
            await asyncio.sleep(0.01)
        assert sup.failure is None
        await sup.stop()
        assert sup._watch_task is None

    @pytest.mark.asyncio
    async def test_a_503_naming_the_exit_code_fails_closed_through_the_slot(self, tmp_path, home):
        sup = _sup(_install(tmp_path))
        proc = _FakeProc()
        sup._proc, sup.props = proc, {"x": 1}

        class _Status(Exception):
            status_code = 503

        with pytest.raises(_Status):
            async with sup.call_slot():
                raise _Status("Error code: 503 - {'error': {'message': 'the engine stopped "
                              "unexpectedly (exit code 3221226505); the next request restarts it'}}")
        assert sup._in_flight == 0
        assert sup.failure["exit_code"] == "exit code 3221226505 (0xC0000409)"
        assert sup._proc is None

    @pytest.mark.asyncio
    async def test_a_503_while_starting_is_not_a_death(self, tmp_path, home):
        sup = _sup(_install(tmp_path))
        sup._proc, sup.props = _FakeProc(), {"x": 1}

        class _Status(Exception):
            status_code = 503

        with pytest.raises(_Status):
            async with sup.call_slot():
                raise _Status("Error code: 503 - the engine is (re)starting and has not said READY yet")
        assert sup.failure is None and sup._proc is not None


def test_exit_code_words_reads_both_forms():
    assert exit_code_words("exit code 0xC0000409: stack buffer overrun / fast-fail") == \
        "exit code 0xC0000409: stack buffer overrun / fast-fail"
    assert exit_code_words("(exit code 1); the next") == "exit code 1"
    assert exit_code_words("nothing here") is None


class TestTheVersionCheck:
    @pytest.mark.asyncio
    async def test_a_mismatch_warns_and_is_stored(self, tmp_path, home, monkeypatch, caplog):
        sup = _sup(_install(tmp_path), tested_version="0.1.45", tested_version_date="2026-10-18")
        monkeypatch.setattr(sup, "_get", lambda p: _ret({"engine": {"version": "0.1.46"}}))
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            await sup._check_version()
        assert sup.engine_version == "0.1.46"
        assert any(r.levelno == logging.WARNING and "0.1.45" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_missing_version_is_an_error_naming_the_mock_engine(self, tmp_path, home, monkeypatch, caplog):
        sup = _sup(_install(tmp_path), tested_version="0.1.45")
        sup.props = {"modalities": {"vision": False}}
        monkeypatch.setattr(sup, "_get", lambda p: _ret({"engine": {"model": "x"}}))
        with caplog.at_level(logging.ERROR, logger=mod.__name__):
            await sup._check_version()
        assert sup.engine_version is None
        assert any(r.levelno == logging.ERROR and "mock engine" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_props_build_info_is_the_fallback(self, tmp_path, home, monkeypatch, caplog):
        sup = _sup(_install(tmp_path), tested_version="0.1.45")
        sup.props = {"build_info": "Strata 0.1.45"}

        async def no_metrics(path):
            raise OSError("down")

        monkeypatch.setattr(sup, "_get", no_metrics)
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            await sup._check_version()
        assert sup.engine_version == "0.1.45"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def _started_through_the_real_launch(sup, monkeypatch, metrics, props=None):
    """Spawn, /health, /props and /metrics faked at the supervisor's seams, so
    `ensure_running` and both `_launch`es run for real. Returns the spawned procs."""
    monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)
    monkeypatch.setattr(base_mod, "_free_vram_mib", lambda: 30000)
    spawned = []

    async def spawn(cmd, env):
        proc = _FakeProc()
        spawned.append(proc)
        return proc

    async def health():
        return True

    async def get(path):
        return metrics if path == "/metrics" else (props or {"total_slots": 1})

    sup._spawn, sup._health_ok, sup._get = spawn, health, get
    sup._start_watch = lambda: None
    return spawned


class TestNoVersionFailsClosed:
    """ADR-044 point 5: the smoke counts only after the version check, so a
    launcher that names no engine version is refused like an engine death."""

    @pytest.mark.asyncio
    async def test_no_version_after_health_stops_the_launcher_and_refuses_until_reset(
        self, tmp_path, home, monkeypatch,
    ):
        sup = _sup(_install(tmp_path), tested_version="0.1.45")
        spawned = _started_through_the_real_launch(
            sup, monkeypatch, metrics={"live": {"state": "loaded"}})

        with pytest.raises(LlamaServerError, match="will not start it again") as excinfo:
            await sup.ensure_running()
        assert mod.NO_ENGINE_VERSION in str(excinfo.value)
        assert "no engine version reported" in sup.failure["reason"]
        assert "mock engine or wrong --engine" in sup.failure["reason"]
        assert "commit limit" not in sup.failure["reason"], "not a death: no commit-limit story"
        assert sup.failure["exit_code"] is None
        assert len(spawned) == 1
        expected = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
        assert ("signal", expected) in spawned[0].calls, "the launcher was stopped"
        assert sup._proc is None and sup.props is None
        assert sup.engine_version is None

        with pytest.raises(LlamaServerError, match="will not start it again"):
            await sup.ensure_running()
        assert len(spawned) == 1, "refused like a death: no second start"

        sup.reset_failure()
        with pytest.raises(LlamaServerError, match="no engine version reported"):
            await sup.ensure_running()
        assert len(spawned) == 2, "after a reset a person's call starts it again"

    @pytest.mark.asyncio
    async def test_a_mismatch_with_the_tested_version_still_serves(self, tmp_path, home, monkeypatch, caplog):
        sup = _sup(_install(tmp_path), tested_version="0.1.45")
        spawned = _started_through_the_real_launch(
            sup, monkeypatch, metrics={"engine": {"version": "0.1.46"}})
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            props = await sup.ensure_running()
        assert props == {"total_slots": 1}
        assert sup.failure is None and sup._proc is spawned[0]
        assert sup.engine_version == "0.1.46"
        assert any(r.levelno == logging.WARNING and "0.1.45" in r.getMessage() for r in caplog.records)
        await sup.stop()

    @pytest.mark.asyncio
    async def test_props_build_info_alone_is_a_version_and_serves(self, tmp_path, home, monkeypatch):
        sup = _sup(_install(tmp_path))
        _started_through_the_real_launch(
            sup, monkeypatch, metrics={}, props={"build_info": "Strata 0.1.45"})
        await sup.ensure_running()
        assert sup.failure is None and sup.engine_version == "0.1.45"
        await sup.stop()


class TestStopChecksTheCard:
    @pytest.mark.asyncio
    async def test_vram_that_does_not_come_back_is_reported(self, tmp_path, home, monkeypatch, caplog):
        sup = _sup(_install(tmp_path))
        sup._proc, sup._vram_free_before = _FakeProc(), 30000
        monkeypatch.setattr(base_mod, "_free_vram_mib", lambda: 1500)
        with caplog.at_level(logging.ERROR, logger=mod.__name__):
            await sup.stop()
        assert sup.vram_released is False
        assert sup.last_stop_vram == {"before_mib": 30000, "after_mib": 1500, "released": False}
        assert any("strata.exe" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_vram_that_comes_back_is_released(self, tmp_path, home, monkeypatch):
        sup = _sup(_install(tmp_path))
        sup._proc, sup._vram_free_before = _FakeProc(), 30000
        monkeypatch.setattr(base_mod, "_free_vram_mib", lambda: 29500)
        await sup.stop()
        assert sup.vram_released is True
