"""A Strata alias can choose its drafter and when its picture reader starts (Mike's call, 2026-11-06).

Two typed alias fields, each a dropdown with an explicit "auto" that stores nothing:

* `drafting` (`mtp` / `dflash2`): Strata's own run-config key, written at the ROOT of the
  start config. It is not a `model_params` key (Strata's page does not edit it) and `off` is
  not offered: that is `model_params.mtp = false`.
* `vision_elastic` (true / false): `elastic` INSIDE the start config's "vision" block, and
  only where the start has one.

Neither is an engine flag, so neither reaches `args`. No engine runs: the install is the
fake of test_strata_supervisor.
"""

import json
from types import SimpleNamespace

import pytest

from dpc_client_core.managers.llama_server_supervisor import LlamaServerError
from dpc_client_core.managers.strata_supervisor import (
    DRAFTING_CHOICES,
    STRATA_DEFAULTS,
    drafting_error,
    inspect_base_config,
    strata_config_errors,
    vision_elastic_error,
)
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers.strata_provider import _STRATA_FLAG_KEYS, metrics_summary
from dpc_client_core.service import CoreService

from tests.test_strata_supervisor import home, _install, _sup  # noqa: F401


def _args_text(cfg) -> str:
    return " ".join(str(a) for a in cfg["args"])


def _without_vision_block(root) -> None:
    path = root / "strata-base.json"
    base = json.loads(path.read_text(encoding="utf-8"))
    base.pop("vision")
    path.write_text(json.dumps(base), encoding="utf-8")


class TestTheDrafter:
    @pytest.mark.parametrize("value", ["mtp", "dflash2"])
    def test_a_choice_is_a_root_key_of_the_start_config_and_never_an_engine_flag(self, tmp_path, home, value):
        cfg = _sup(_install(tmp_path), drafting=value).build_start_config(5000)
        assert cfg["drafting"] == value
        assert "elastic" not in cfg["vision"]
        assert "drafting" not in _args_text(cfg) and value not in cfg["args"]

    @pytest.mark.parametrize("value", [None, "", "auto"])
    def test_auto_writes_no_key(self, tmp_path, home, value):
        cfg = _sup(_install(tmp_path), drafting=value).build_start_config(5000)
        assert "drafting" not in cfg
        assert "drafting" not in json.dumps(cfg["args"])

    def test_an_alias_without_the_field_writes_no_key(self, tmp_path, home):
        assert "drafting" not in _sup(_install(tmp_path)).build_start_config(5000)

    def test_auto_leaves_the_base_configs_own_key_and_a_choice_replaces_it(self, tmp_path, home):
        root = _install(tmp_path, drafting="off")
        assert _sup(root).build_start_config(5000)["drafting"] == "off"
        assert _sup(root, drafting="auto").build_start_config(5000)["drafting"] == "off"
        assert _sup(root, drafting="dflash2").build_start_config(5000)["drafting"] == "dflash2"
        assert inspect_base_config("strata-base.json", root)["values"]["drafting"] == "off"

    def test_the_written_file_carries_it_at_the_root(self, tmp_path, home):
        path = _sup(_install(tmp_path), drafting="dflash2").write_start_config(5000)
        assert json.loads(path.read_text(encoding="utf-8"))["drafting"] == "dflash2"

    @pytest.mark.parametrize("bad", ["off", "OFF", "dflash", "Mtp", 1, True, ["mtp"]])
    def test_an_unknown_value_is_refused_at_build_and_at_save(self, tmp_path, home, bad):
        with pytest.raises(LlamaServerError, match="drafting"):
            _sup(_install(tmp_path), drafting=bad).build_start_config(5000)
        config = {"strata_install_dir": "Z:/none", "strata_base_config": "x.json",
                  "conversation_cache_mib": 1, "drafting": bad}
        errors = strata_config_errors(config)
        assert len(errors) == 1 and errors[0].startswith("drafting: "), errors

    def test_off_is_refused_with_the_field_that_turns_the_layer_off(self):
        assert drafting_error("off") == (
            'drafting: "off" is not a choice here; turn the draft layer off with '
            "model_params mtp = false, which is the same thing")
        assert drafting_error("nope") == "drafting: expected auto, mtp or dflash2, not 'nope'"

    def test_the_validator_takes_auto_in_its_three_spellings(self):
        assert drafting_error(None) is None and drafting_error("") is None and drafting_error("auto") is None
        assert all(drafting_error(v) is None for v in DRAFTING_CHOICES)

    def test_drafting_is_still_not_a_model_params_key(self, tmp_path, home):
        with pytest.raises(LlamaServerError, match="not a key Strata's page edits"):
            _sup(_install(tmp_path), model_params={"drafting": "mtp"}).build_start_config(5000)


class TestThePictureReader:
    @pytest.mark.parametrize("value", [True, False])
    def test_a_choice_is_elastic_inside_the_vision_block_and_not_at_the_root(self, tmp_path, home, value):
        cfg = _sup(_install(tmp_path), vision_elastic=value).build_start_config(5000)
        assert cfg["vision"]["elastic"] is value
        assert "elastic" not in cfg and "vision_elastic" not in cfg
        assert "elastic" not in _args_text(cfg)
        assert cfg["vision"]["mmproj"] == "D:/mmproj.gguf", "the rest of the block is kept"

    @pytest.mark.parametrize("value", [None, "", "auto"])
    def test_auto_writes_nothing(self, tmp_path, home, value):
        cfg = _sup(_install(tmp_path), vision_elastic=value).build_start_config(5000)
        assert "elastic" not in cfg["vision"] and "elastic" not in cfg

    def test_auto_keeps_the_base_blocks_own_elastic(self, tmp_path, home):
        base_vision = {"exe": "x/strata-vision.exe", "mmproj": "D:/mmproj.gguf", "elastic": True}
        root = _install(tmp_path, vision=base_vision)
        assert _sup(root).build_start_config(5000)["vision"]["elastic"] is True
        assert _sup(root, vision_elastic=False).build_start_config(5000)["vision"]["elastic"] is False
        values = inspect_base_config("strata-base.json", root)["values"]
        assert values["vision_elastic"] is True and values["vision_block"] is True

    def test_the_value_is_written_into_a_copy_not_the_bases_block(self, tmp_path, home):
        root = _install(tmp_path)
        _sup(root, vision_elastic=True).build_start_config(5000)
        on_disk = json.loads((root / "strata-base.json").read_text(encoding="utf-8"))
        assert "elastic" not in on_disk["vision"]

    def test_a_set_value_on_a_start_with_no_vision_block_is_refused_at_build(self, tmp_path, home):
        root = _install(tmp_path)
        _without_vision_block(root)
        with pytest.raises(LlamaServerError, match="vision_elastic is set but the start has no .vision. block"):
            _sup(root, vision_elastic=True).build_start_config(5000)
        # auto asks for nothing, so there is nothing to refuse
        assert "vision" not in _sup(root, vision_elastic="auto").build_start_config(5000)

    def test_a_set_value_beside_vision_off_is_refused_at_build(self, tmp_path, home):
        with pytest.raises(LlamaServerError, match="vision_elastic is set but the start has no"):
            _sup(_install(tmp_path), vision=False, vision_elastic=False).build_start_config(5000)

    @pytest.mark.parametrize("bad", ["yes", "true", 1, 0, []])
    def test_an_unknown_value_is_refused_at_build_and_at_save(self, tmp_path, home, bad):
        with pytest.raises(LlamaServerError, match="vision_elastic"):
            _sup(_install(tmp_path), vision_elastic=bad).build_start_config(5000)
        config = {"strata_install_dir": "Z:/none", "strata_base_config": "x.json",
                  "conversation_cache_mib": 1, "vision_elastic": bad}
        errors = strata_config_errors(config)
        assert len(errors) == 1 and errors[0].startswith("vision_elastic: "), errors

    def test_the_validator_says_what_it_takes(self):
        assert vision_elastic_error("yes") == "vision_elastic: expected auto, true or false, not 'yes'"
        assert all(vision_elastic_error(v) is None for v in (None, "", "auto", True, False))


class TestNeitherKeyLeaks:
    def test_both_set_leave_args_as_without_them(self, tmp_path, home):
        root = _install(tmp_path)
        plain = _sup(root).build_start_config(5000)
        both = _sup(root, drafting="dflash2", vision_elastic=True).build_start_config(5000)
        assert both["args"] == plain["args"]
        assert both["drafting"] == "dflash2" and both["vision"]["elastic"] is True
        assert "vision_elastic" not in both

    def test_the_alias_defaults_hold_both_as_absent(self):
        assert STRATA_DEFAULTS["drafting"] is None and STRATA_DEFAULTS["vision_elastic"] is None


class TestTheSave:
    def _provider(self, root, **extra):
        return {"alias": "flash", "type": "strata", "model": "flash", "strata_install_dir": str(root),
                "strata_base_config": "strata-base.json", "conversation_cache_mib": 10240, **extra}

    def _errors(self, provider):
        return CoreService._validate_providers_config(
            SimpleNamespace(), {"default_provider": "flash", "providers": [provider]})

    def test_the_choices_are_saved(self, tmp_path):
        root = _install(tmp_path)
        assert self._errors(self._provider(root, drafting="dflash2", vision_elastic=True)) == []
        assert self._errors(self._provider(root, drafting="auto", vision_elastic="auto")) == []

    def test_a_bad_value_is_named_with_its_provider(self, tmp_path):
        root = _install(tmp_path)
        assert self._errors(self._provider(root, drafting="off")) == [
            'Provider 1: strata drafting: "off" is not a choice here; turn the draft layer off with '
            "model_params mtp = false, which is the same thing"]
        assert self._errors(self._provider(root, vision_elastic="soon")) == [
            "Provider 1: strata vision_elastic: expected auto, true or false, not 'soon'"]

    def test_a_value_on_an_alias_whose_start_has_no_vision_block_is_refused(self, tmp_path):
        root = _install(tmp_path)
        _without_vision_block(root)
        assert self._errors(self._provider(root, vision_elastic=False)) == [
            'Provider 1: strata vision_elastic: the start has no "vision" block (encoder exe) to put it '
            "in; the base config has none, so leave it at auto"]
        assert self._errors(self._provider(root, vision_elastic="auto")) == []

    def test_vision_off_on_the_alias_is_named(self, tmp_path):
        root = _install(tmp_path)
        assert self._errors(self._provider(root, vision=False, vision_elastic=True)) == [
            'Provider 1: strata vision_elastic: the start has no "vision" block to put it in, because the '
            "alias turns vision off; leave it at auto or turn vision on"]


class TestTheAlias:
    def test_a_change_restarts_the_launcher_and_auto_does_not(self, tmp_path):
        cfg = {"strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
               "conversation_cache_mib": 1024}
        sig = StrataProvider._flag_signature
        assert "drafting" in _STRATA_FLAG_KEYS and "vision_elastic" in _STRATA_FLAG_KEYS
        assert sig(cfg) != sig({**cfg, "drafting": "mtp"})
        assert sig({**cfg, "drafting": "mtp"}) != sig({**cfg, "drafting": "dflash2"})
        assert sig(cfg) != sig({**cfg, "vision_elastic": False})
        assert sig(cfg) == sig({**cfg, "drafting": None, "vision_elastic": None})


class TestWhatTheEngineChose:
    """Strata puts the chosen drafter in `/metrics` under `engine` (`drafting`, `drafting_note`),
    from strata_control/drafting.py `engine_info`, merged into `engine.info` at the engine's READY."""

    def test_the_summary_carries_the_drafter_and_the_note(self):
        summary = metrics_summary({"engine": {
            "drafting": "mtp", "drafting_note": "Drafting: MTP (DFlash2 is not downloaded yet)"}})
        assert summary["drafter"] == "mtp"
        assert summary["drafting_note"] == "Drafting: MTP (DFlash2 is not downloaded yet)"

    def test_an_engine_that_says_nothing_gives_none(self):
        summary = metrics_summary({"engine": {}})
        assert summary["drafter"] is None and summary["drafting_note"] is None
