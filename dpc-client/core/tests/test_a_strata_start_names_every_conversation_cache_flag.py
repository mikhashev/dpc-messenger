"""Every conversation cache flag is DPC's and named (Mike's call, 2026-10-19: everything transparent).

Strata decides what its conversation cache parks by five flags. DPC used to
write two of them (budget, slots); the RAM floor, the shortest parked
conversation and the park gain were Strata defaults nobody on our side saw.
Each is now a typed alias field, written into every start config — Strata's
default when the alias sets none — and named in the start line and the status.
No engine runs: the install is the fake of test_strata_supervisor.
"""

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import strata_supervisor as mod
from dpc_client_core.managers.llama_server_supervisor import LlamaServerError
from dpc_client_core.managers.strata_supervisor import (
    CACHE_ADMISSION_FLAGS,
    inspect_base_config,
    strata_config_errors,
)
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers.strata_provider import metrics_summary, strata_status
from dpc_client_core.service import CoreService

from tests.test_strata_supervisor import BASE_ARGS, home, _install, _sup, _value  # noqa: F401

UI_FIELDS = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "utils" / "strataFields.ts"

# Strata 0.1.45 (src/program/generate.cpp, its help text and Options struct), read 2026-10-19.
STRATA_DOCUMENTED = {
    "conversation_cache_min_free_mib": ("--conversation-cache-min-free-mib", 2560),
    "conversation_cache_min_tokens": ("--conversation-cache-min-tokens", 0),
    "conversation_cache_park_gain": ("--conversation-cache-park-gain", 0),
}


def test_the_three_flags_and_their_defaults_are_strata_documented_ones():
    assert {k: v[:2] for k, v in CACHE_ADMISSION_FLAGS.items()} == STRATA_DOCUMENTED
    for key, (_flag, default) in STRATA_DOCUMENTED.items():
        assert mod.STRATA_DEFAULTS[key] == default


class TestTheStartConfig:
    def test_unset_writes_strata_defaults_explicitly_once_each(self, tmp_path, home):
        args = _sup(_install(tmp_path)).build_start_config(5000)["args"]
        for key, (flag, default) in STRATA_DOCUMENTED.items():
            assert args.count(flag) == 1, flag
            assert _value(args, flag) == str(default), flag

    def test_an_explicit_null_is_an_absence_too(self, tmp_path, home):
        args = _sup(_install(tmp_path), conversation_cache_min_free_mib=None).build_start_config(5000)["args"]
        assert _value(args, "--conversation-cache-min-free-mib") == "2560"

    def test_set_values_are_written(self, tmp_path, home):
        args = _sup(_install(tmp_path), conversation_cache_min_free_mib=4096,
                    conversation_cache_min_tokens=8000,
                    conversation_cache_park_gain=2048).build_start_config(5000)["args"]
        assert _value(args, "--conversation-cache-min-free-mib") == "4096"
        assert _value(args, "--conversation-cache-min-tokens") == "8000"
        assert _value(args, "--conversation-cache-park-gain") == "2048"

    def test_the_base_configs_value_is_replaced_not_kept(self, tmp_path, home):
        base = BASE_ARGS + ["--conversation-cache-min-free-mib", "1024", "--conversation-cache-park-gain", "512"]
        root = _install(tmp_path, args=base)
        args = _sup(root).build_start_config(5000)["args"]
        assert args.count("--conversation-cache-min-free-mib") == 1
        assert _value(args, "--conversation-cache-min-free-mib") == "2560"
        assert _value(args, "--conversation-cache-park-gain") == "0"
        out = inspect_base_config("strata-base.json", root)["values"]
        assert out["conversation_cache_min_free_mib"] == 1024
        assert out["conversation_cache_park_gain"] == 512
        assert out["conversation_cache_min_tokens"] is None

    @pytest.mark.parametrize("key", list(STRATA_DOCUMENTED))
    @pytest.mark.parametrize("bad", [-1, 1.5, "2560", True])
    def test_an_invalid_value_is_refused_at_build_and_at_save(self, tmp_path, home, key, bad):
        with pytest.raises(LlamaServerError, match=key):
            _sup(_install(tmp_path), **{key: bad}).build_start_config(5000)
        config = {"strata_install_dir": "Z:/none", "strata_base_config": "x.json",
                  "conversation_cache_mib": 1, key: bad}
        errors = strata_config_errors(config)
        assert len(errors) == 1 and errors[0].startswith(f"{key}: expected a whole number from 0 to "), errors
        with pytest.raises(ValueError, match=key):
            StrataProvider("flash", {"type": "strata", "model": "flash", **config})

    def test_strata_upper_bounds_hold(self):
        config = {"strata_install_dir": "Z:/none", "strata_base_config": "x.json", "conversation_cache_mib": 1}
        assert strata_config_errors({**config, "conversation_cache_min_tokens": 2**31 - 1,
                                     "conversation_cache_park_gain": 0,
                                     "conversation_cache_min_free_mib": 0}) == []
        assert strata_config_errors({**config, "conversation_cache_min_tokens": 2**31})
        assert strata_config_errors({**config, "conversation_cache_min_free_mib": 2**43})

    def test_the_save_validation_refuses_them_with_the_provider_prefix(self, tmp_path):
        root = _install(tmp_path)
        provider = {"alias": "flash", "type": "strata", "model": "flash", "strata_install_dir": str(root),
                    "strata_base_config": "strata-base.json", "conversation_cache_mib": 10240,
                    "conversation_cache_park_gain": -5}
        errors = CoreService._validate_providers_config(
            SimpleNamespace(), {"default_provider": "flash", "providers": [provider]})
        assert errors == ["Provider 1: strata conversation_cache_park_gain: expected a whole number "
                          "from 0 to 2147483647, not -5"]

    def test_a_change_restarts_the_launcher_and_an_explicit_default_does_not(self, tmp_path):
        cfg = {"strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
               "conversation_cache_mib": 1024}
        sig = StrataProvider._flag_signature
        assert sig(cfg) != sig({**cfg, "conversation_cache_min_tokens": 4000})
        assert sig(cfg) == sig({**cfg, "conversation_cache_min_free_mib": 2560})


class TestTheyAreNamed:
    def test_the_start_line_names_all_five(self, tmp_path, home, monkeypatch, caplog):
        monkeypatch.setattr(mod, "read_free_commit", lambda: None)
        sup = _sup(_install(tmp_path), conversation_cache_slots=4, conversation_cache_park_gain=2048)
        with caplog.at_level(logging.INFO, logger=mod.__name__):
            sup.log_start(None, {}, binary=Path("python.exe"))
        [line] = [r.getMessage() for r in caplog.records if "starting on" in r.getMessage()]
        assert "conversation_cache_mib=10240 beside" in line
        assert "conversation_cache_slots=4" in line
        assert "conversation_cache_min_free_mib=2560 (physical RAM floor)" in line
        assert "conversation_cache_min_tokens=0" in line
        assert "conversation_cache_park_gain=2048" in line

    @pytest.mark.asyncio
    async def test_the_status_reads_them_from_the_start_config_used(self, tmp_path, home):
        sup = _sup(_install(tmp_path), conversation_cache_min_tokens=6000)
        assert sup.start_cache_flags() is None
        sup.write_start_config(5000)
        row = await strata_status(sup)
        assert row["start_cache_flags"] == {
            "conversation_cache_mib": 10240, "conversation_cache_slots": None,
            "conversation_cache_min_free_mib": 2560, "conversation_cache_min_tokens": 6000,
            "conversation_cache_park_gain": 0,
        }
        written = json.loads(Path(sup.start_config_used).read_text(encoding="utf-8"))["args"]
        assert _value(written, "--conversation-cache-min-tokens") == "6000"

    def test_metrics_carry_what_the_engine_reports(self):
        summary = metrics_summary({"engine": {"conversation_cache_min_free_mib": 2560,
                                              "conversation_cache_min_tokens": 0}})
        assert summary["cache_admission"] == {"min_free_mib": 2560, "min_tokens": 0, "park_gain": None}


def test_the_ui_lists_the_same_flags_and_defaults():
    text = UI_FIELDS.read_text(encoding="utf-8")
    block = text.split("export const STRATA_CACHE_ADMISSION", 1)[1].split("];", 1)[0]
    found = re.findall(r"key: '([^']+)', flag: '([^']+)', strataDefault: (\d+),\s*max: (\d+)", block)
    assert [(k, f, int(d), int(m)) for k, f, d, m in found] == [
        (k, flag, default, hi) for k, (flag, default, hi) in CACHE_ADMISSION_FLAGS.items()
    ]
