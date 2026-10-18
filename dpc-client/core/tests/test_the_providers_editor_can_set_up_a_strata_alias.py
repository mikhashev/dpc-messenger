"""What the providers editor needs for a `strata` alias (ADR-044 step 3).

The model-file overrides over the base config, the second-shard suggestion,
the four local-API commands (inspect, status, stop, reset) and the save-time
validation. No engine runs: the install is a fake in tmp_path and a running
launcher is a fake process with a faked `/metrics`.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.strata_supervisor import (
    STRATA_MODEL_PARAMS,
    LlamaServerError,
    model_param_error,
    strata_config_errors,
    suggest_ple_gguf,
)
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.service import CoreService

from tests.test_strata_supervisor import BASE_ARGS, _FakeProc, _install, _sup, _value

NATIVE = "D:/models/flash-next-00001-of-00002.gguf"
UI_FIELDS = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "utils" / "strataFields.ts"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    dpc = tmp_path / "dpc-home"
    dpc.mkdir()
    monkeypatch.setattr(base_mod, "DPC_HOME", dpc)
    _ACTIVE_SUPERVISORS.clear()
    yield dpc
    _ACTIVE_SUPERVISORS.clear()


def _base_with(**extra):
    """The fixture's base args with a --ple-gguf and an --mtp and --expert-profile."""
    return BASE_ARGS + [
        "--ple-gguf", "D:/models/flash-next-00002-of-00002.gguf",
        "--mtp", "D:/mtp/rt",
        "--expert-profile", "D:/data/expert-profile.bin",
    ]


class TestModelFileOverrides:
    @pytest.mark.parametrize("key,flag,value", [
        ("native_gguf", "--native", "E:/m/other-00001-of-00003.gguf"),
        ("ple_gguf", "--ple-gguf", "E:/m/other-00002-of-00003.gguf"),
        ("pack_dir", "--pack", "E:/packs/iq2"),
        ("mtp_dir", "--mtp", "E:/mtp/new"),
        ("expert_profile", "--expert-profile", "E:/data/profile2.bin"),
    ])
    def test_each_file_key_replaces_its_flag_once(self, tmp_path, key, flag, value):
        root = _install(tmp_path, args=_base_with())
        args = _sup(root, **{key: value}).build_start_config(1)["args"]
        assert _value(args, flag) == value
        assert args.count(flag) == 1

    def test_unset_keys_keep_every_base_value(self, tmp_path):
        root = _install(tmp_path, args=_base_with())
        cfg = _sup(root).build_start_config(1)
        args = cfg["args"]
        assert _value(args, "--native") == NATIVE
        assert _value(args, "--ple-gguf") == "D:/models/flash-next-00002-of-00002.gguf"
        assert _value(args, "--pack") == "D:/packs/iq3_xxs"
        assert _value(args, "--mtp") == "D:/mtp/rt"
        assert _value(args, "--expert-profile") == "D:/data/expert-profile.bin"
        assert _value(args, "--control-vector-scaled") == "D:/cv/refusal.gguf:1.0"
        assert cfg["vision"]["mmproj"] == "D:/mmproj.gguf"

    def test_a_flag_the_base_lacks_is_appended(self, tmp_path):
        args = _sup(_install(tmp_path), mtp_dir="E:/mtp").build_start_config(1)["args"]
        assert _value(args, "--mtp") == "E:/mtp"

    def test_mmproj_goes_into_the_vision_block(self, tmp_path):
        cfg = _sup(_install(tmp_path), mmproj="E:/mmproj-2.gguf").build_start_config(1)
        assert cfg["vision"]["mmproj"] == "E:/mmproj-2.gguf"
        assert cfg["vision"]["exe"].endswith("strata-vision.exe")

    def test_the_vision_model_follows_the_first_shard_where_it_named_it(self, tmp_path):
        root = _install(tmp_path, vision={"exe": "v.exe", "mmproj": "m.gguf", "model": NATIVE})
        cfg = _sup(root, native_gguf="E:/m/x-00001-of-00002.gguf").build_start_config(1)
        assert cfg["vision"]["model"] == "E:/m/x-00001-of-00002.gguf"

    def test_a_vision_model_naming_something_else_is_left_alone(self, tmp_path):
        root = _install(tmp_path, vision={"exe": "v.exe", "mmproj": "m.gguf", "model": "D:/own.gguf"})
        cfg = _sup(root, native_gguf="E:/m/x-00001-of-00002.gguf").build_start_config(1)
        assert cfg["vision"]["model"] == "D:/own.gguf"

    def test_mmproj_without_a_vision_block_is_refused(self, tmp_path):
        with pytest.raises(LlamaServerError, match="mmproj is set"):
            _sup(_install(tmp_path, vision=None), mmproj="E:/m.gguf").build_start_config(1)

    def test_the_control_vector_file_is_replaced_and_the_scale_kept(self, tmp_path):
        root = _install(tmp_path, args=[a.replace("refusal.gguf:1.0", "refusal.gguf:0.75") for a in BASE_ARGS])
        args = _sup(root, control_vector_file="C:\\cv\\other.gguf").build_start_config(1)["args"]
        assert _value(args, "--control-vector-scaled") == "C:\\cv\\other.gguf:0.75"
        assert _value(args, "--cvec-mode") == "project"

    def test_a_control_vector_file_with_no_base_flag_is_refused(self, tmp_path):
        root = _install(tmp_path, args=["--native", NATIVE])
        with pytest.raises(LlamaServerError, match="no --control-vector-scaled"):
            _sup(root, control_vector_file="E:/cv.gguf").build_start_config(1)

    def test_control_vector_off_wins_over_a_file(self, tmp_path):
        args = _sup(_install(tmp_path), control_vector=False,
                    control_vector_file="E:/cv.gguf").build_start_config(1)["args"]
        assert "--control-vector-scaled" not in args and "E:/cv.gguf:1.0" not in args

    def test_an_override_key_changes_the_restart_signature(self, tmp_path):
        root = _install(tmp_path)
        cfg = {"strata_install_dir": str(root), "strata_base_config": "strata-base.json",
               "conversation_cache_mib": 1024}
        assert (StrataProvider._flag_signature(cfg)
                != StrataProvider._flag_signature({**cfg, "pack_dir": "E:/p"}))


class TestTheModelParameters:
    def test_a_key_strata_does_not_edit_is_refused_at_build(self, tmp_path):
        with pytest.raises(LlamaServerError, match="not a key Strata's page edits"):
            _sup(_install(tmp_path), model_params={"kv": "int4"}).build_start_config(1)

    def test_values_follow_strata_rules(self):
        assert model_param_error("sampling.top_k", 20) is None
        assert "at most 64" in model_param_error("sampling.top_k", 65)
        assert "more than 0" in model_param_error("sampling.top_p", 0)
        assert model_param_error("anthropic_thinking", "on_request") is None
        assert "expected one of" in model_param_error("effort_position", "middle")
        assert "true or false" in model_param_error("lazy_load", 1)
        assert model_param_error("aliases", "a, b") is None
        assert model_param_error("vram_reserve_mib", None) is None
        assert "always false" in model_param_error("open_browser", True)

    def test_lazy_load_with_vision_is_refused(self, tmp_path):
        with pytest.raises(LlamaServerError, match="lazy_load is text-only"):
            _sup(_install(tmp_path), model_params={"lazy_load": True}).build_start_config(1)

    def test_the_ui_list_names_the_same_keys(self):
        text = UI_FIELDS.read_text(encoding="utf-8")
        block = text.split("STRATA_MODEL_PARAMS", 1)[1]
        ui_keys = re.findall(r"key: '([^']+)'", block.split("];", 1)[0])
        assert ui_keys == list(STRATA_MODEL_PARAMS)


def test_the_second_shard_is_suggested_beside_the_first():
    assert suggest_ple_gguf("D:\\m\\Flash-IQ3_XXS-00001-of-00002.gguf") == "D:\\m\\Flash-IQ3_XXS-00002-of-00002.gguf"
    assert suggest_ple_gguf("/m/x-00001-of-00004.GGUF") == "/m/x-00002-of-00004.GGUF"
    assert suggest_ple_gguf("/m/x-00002-of-00002.gguf") is None
    assert suggest_ple_gguf("/m/x-00001-of-00001.gguf") is None
    assert suggest_ple_gguf("/m/single.gguf") is None
    assert suggest_ple_gguf(None) is None


class TestInspectCommand:
    @pytest.mark.asyncio
    async def test_it_reads_every_field_the_editor_shows(self, tmp_path):
        root = _install(tmp_path, args=_base_with(), sampling={"top_k": 20, "temperature": 0.6},
                        idle_unload_s=0, model_name="flash")
        out = await CoreService.strata_inspect_base_config(
            SimpleNamespace(), path="strata-base.json", install_dir=str(root))
        assert out["status"] == "success"
        assert out["path"] == str(root / "strata-base.json")
        assert out["model_name"] == "flash"
        v = out["values"]
        assert v["native_gguf"] == NATIVE and v["pack_dir"] == "D:/packs/iq3_xxs"
        assert v["ple_gguf"] == "D:/models/flash-next-00002-of-00002.gguf"
        assert v["mtp_dir"] == "D:/mtp/rt" and v["expert_profile"] == "D:/data/expert-profile.bin"
        assert v["max_context"] == 261120 and v["conversation_cache_mib"] == 4096
        assert v["conversation_cache_slots"] is None
        assert v["vision"] is True and v["mmproj"] == "D:/mmproj.gguf"
        assert v["control_vector"] is True and v["control_vector_file"] == "D:/cv/refusal.gguf"
        assert out["control_vector_scale"] == "1.0"
        assert v["resident_budget_gib"] == 24 and v["vram_reserve_mib"] == 1536
        assert out["model_params"] == {"sampling.temperature": 0.6, "sampling.top_k": 20,
                                       "idle_unload_s": 0, "vram_reserve_mib": 1536}
        assert out["suggested_ple_gguf"] == "D:/models/flash-next-00002-of-00002.gguf"
        assert NATIVE in out["missing_files"]

    @pytest.mark.asyncio
    async def test_a_file_that_is_not_a_run_config_is_a_clear_error(self, tmp_path):
        bad = tmp_path / "x.json"
        bad.write_text('{"model": "y"}', encoding="utf-8")
        out = await CoreService.strata_inspect_base_config(SimpleNamespace(), path=str(bad))
        assert out["status"] == "error" and "Strata run config" in out["message"]
        out = await CoreService.strata_inspect_base_config(SimpleNamespace(), path=str(tmp_path / "none.json"))
        assert out["status"] == "error" and "cannot read" in out["message"]


def _provider(tmp_path, alias="flash", **extra):
    return StrataProvider(alias, {
        "type": "strata", "model": "flash", "strata_install_dir": str(_install(tmp_path)),
        "strata_base_config": "strata-base.json", "conversation_cache_mib": 10240,
        "tested_version": "0.1.46", "tested_version_date": "2026-10-17", **extra,
    })


def _never_start(sup):
    calls = []

    async def refuse(*a, **k):
        calls.append("start")
        raise AssertionError("the status read started an engine")

    sup.ensure_running = refuse
    sup._launch = refuse
    sup._spawn = refuse
    return calls


class TestStatusCommand:
    @pytest.mark.asyncio
    async def test_a_stopped_alias_is_reported_and_nothing_starts(self, tmp_path):
        p = _provider(tmp_path)
        calls = _never_start(p.supervisor)

        async def no_metrics(path):
            calls.append(path)
            raise AssertionError("read /metrics of a launcher that is not running")

        p.supervisor._get = no_metrics
        out = await CoreService.local_engine_status(SimpleNamespace())
        assert out["status"] == "success" and out["card_holder"] is None
        [row] = out["strata"]
        assert row["alias"] == "flash" and row["state"] == "stopped"
        assert row["metrics"] is None and row["tested_version"] == "0.1.46"
        assert calls == []
        assert p.supervisor._proc is None

    @pytest.mark.asyncio
    async def test_a_running_launcher_shows_its_metrics_summary(self, tmp_path):
        p = _provider(tmp_path)
        sup = p.supervisor
        calls = _never_start(sup)
        sup._proc, sup.props, sup.port, sup.engine_version = _FakeProc(), {"x": 1}, 5123, "0.1.47"

        async def metrics(path):
            assert path == "/metrics"
            return {
                "engine": {"max_context": 261120, "images": True, "version": "0.1.47"},
                "live": {"state": "idle"},
                "conversation_cache": {"enabled": True, "budget_mib": 10240, "slots": 4, "parked": 2,
                                       "bytes": 123, "parks": 5, "restores": 3, "evictions": 0,
                                       "requests": 9},
                "hardware": {"gpu_mem_total": 32 << 30, "gpu_mem_used": 30 << 30},
            }

        sup._get = metrics
        [row] = (await CoreService.local_engine_status(SimpleNamespace()))["strata"]
        assert row["state"] == "running" and row["port"] == 5123
        assert row["version_matches"] is False
        m = row["metrics"]
        assert m["max_context"] == 261120 and m["images"] is True and m["vram_free_mib"] == 2048
        assert m["conversation_cache"] == {"enabled": True, "budget_mib": 10240, "slots": 4, "parked": 2,
                                           "bytes": 123, "parks": 5, "restores": 3, "evictions": 0}
        assert calls == []

    @pytest.mark.asyncio
    async def test_an_unreadable_metrics_is_reported_not_raised(self, tmp_path):
        p = _provider(tmp_path)
        sup = p.supervisor
        sup._proc, sup.props = _FakeProc(), {"x": 1}

        async def broken(path):
            raise ConnectionError("refused")

        sup._get = broken
        [row] = (await CoreService.local_engine_status(SimpleNamespace()))["strata"]
        assert row["metrics"] is None and "refused" in row["metrics_error"]

    @pytest.mark.asyncio
    async def test_a_failure_is_shown_with_its_reason(self, tmp_path):
        p = _provider(tmp_path)
        p.supervisor.failure = {"reason": "exit code 0xC0000409", "exit_code": "exit code 0xC0000409",
                                "at": 1.0, "log_tail": []}
        [row] = (await CoreService.local_engine_status(SimpleNamespace()))["strata"]
        assert row["state"] == "failed" and row["failure"]["exit_code"] == "exit code 0xC0000409"


class TestStopAndReset:
    @pytest.mark.asyncio
    async def test_stop_wraps_the_manual_switch(self, tmp_path, monkeypatch):
        seen = []

        async def fake_stop(alias, timeout=None):
            seen.append(alias)
            return alias == "flash"

        from dpc_client_core.providers import llamacpp_server_provider as lsp
        monkeypatch.setattr(lsp, "stop_local_engine", fake_stop)
        assert (await CoreService.stop_local_engine(SimpleNamespace(), alias="flash"))["status"] == "success"
        out = await CoreService.stop_local_engine(SimpleNamespace(), alias="nobody")
        assert out["status"] == "error" and "nobody" in out["message"]
        assert (await CoreService.stop_local_engine(SimpleNamespace()))["status"] == "error"
        assert seen == ["flash", "nobody"]

    @pytest.mark.asyncio
    async def test_stop_really_stops_a_registered_child(self, tmp_path):
        p = _provider(tmp_path, vram_release_timeout_s=0.01)
        p.supervisor._proc = _FakeProc()
        out = await CoreService.stop_local_engine(SimpleNamespace(), alias="flash")
        assert out == {"status": "success", "alias": "flash"}
        assert p.supervisor._proc is None

    @pytest.mark.asyncio
    async def test_reset_clears_the_failure(self, tmp_path):
        p = _provider(tmp_path)
        p.supervisor.failure = {"reason": "dead", "log_tail": ["x"]}
        out = await CoreService.strata_reset_failure(SimpleNamespace(), alias="flash")
        assert out == {"status": "success", "alias": "flash", "was_failed": True,
                       "previous": {"reason": "dead"}}
        assert p.supervisor.failure is None
        out = await CoreService.strata_reset_failure(SimpleNamespace(), alias="ghost")
        assert out["status"] == "error"

    def test_the_four_commands_are_allowed(self):
        for cmd in ("strata_inspect_base_config", "local_engine_status", "stop_local_engine",
                    "strata_reset_failure"):
            assert cmd in ALLOWED_COMMANDS and hasattr(CoreService, cmd)


class TestSaveValidation:
    def _errors(self, provider):
        return CoreService._validate_providers_config(
            SimpleNamespace(), {"default_provider": provider["alias"], "providers": [provider]})

    def _ok(self, tmp_path, **extra):
        root = _install(tmp_path)
        return {"alias": "flash", "type": "strata", "model": "flash", "strata_install_dir": str(root),
                "strata_base_config": "strata-base.json", "conversation_cache_mib": 10240, **extra}

    def test_a_complete_alias_passes(self, tmp_path):
        assert self._errors(self._ok(tmp_path)) == []

    def test_the_three_required_keys(self, tmp_path):
        errors = self._errors({"alias": "flash", "type": "strata", "model": "flash"})
        joined = " ".join(errors)
        assert "strata_install_dir and strata_base_config" in joined
        assert "conversation_cache_mib" in joined

    def test_the_budget_must_be_a_positive_whole_number(self, tmp_path):
        ok = self._ok(tmp_path)
        for bad in (0, "4g", True):
            assert any("positive whole number" in e
                       for e in self._errors({**ok, "conversation_cache_mib": bad})), bad

    def test_the_budget_and_reserve_are_checked(self, tmp_path):
        ok = self._ok(tmp_path)
        assert self._errors({**ok, "resident_budget_gib": 24.5, "vram_reserve_mib": 0}) == []
        for bad in (0, -1, "24", True):
            errors = self._errors({**ok, "resident_budget_gib": bad})
            assert len(errors) == 1 and "resident_budget_gib: expected a number more than 0" in errors[0], bad
        for bad in (-1, 1.5, "700", False):
            errors = self._errors({**ok, "vram_reserve_mib": bad})
            assert len(errors) == 1 and "vram_reserve_mib: expected a whole number, at least 0" in errors[0], bad

    def test_a_set_path_must_exist(self, tmp_path):
        ok = self._ok(tmp_path)
        errors = self._errors({**ok, "native_gguf": str(tmp_path / "missing.gguf")})
        assert errors == [f"Provider 1: strata native_gguf: {tmp_path / 'missing.gguf'} does not exist"]
        assert self._errors({**ok, "native_gguf": str(tmp_path / "Strata" / "serve" / "server.py")}) == []
        errors = self._errors({**ok, "strata_base_config": "nope.json"})
        assert len(errors) == 1 and "strata_base_config" in errors[0]

    def test_model_params_strata_does_not_edit_are_refused(self, tmp_path):
        errors = self._errors(self._ok(tmp_path, model_params={"kv": "int4", "sampling.top_k": 20}))
        assert len(errors) == 1 and "'kv'" in errors[0]

    def test_the_provider_refuses_the_same_keys_without_touching_the_disk(self):
        with pytest.raises(ValueError, match="conversation_cache_mib"):
            StrataProvider("flash", {"type": "strata", "strata_install_dir": "Z:/none",
                                     "strata_base_config": "x.json"})
        assert strata_config_errors({"strata_install_dir": "Z:/none", "strata_base_config": "x.json",
                                     "conversation_cache_mib": 1}) == []
