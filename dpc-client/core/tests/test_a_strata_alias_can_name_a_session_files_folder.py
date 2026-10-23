"""A Strata alias can name the folder of Strata's session files (Mike's call, 2026-10-23).

Strata saves and restores a conversation to a file through `/slots/0` when its
config carries the top-level key `slot_save_path`. It is not an engine flag:
the first attempt, `--slot-save-path` in `extra_args`, made `strata.exe` exit
with `unknown argument` (live, 2026-10-23 17:43). The typed alias field of the
same name is written as a key of the start config and nowhere in `args`.
No engine runs: the install is the fake of test_strata_supervisor.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.managers.llama_server_supervisor import LlamaServerError
from dpc_client_core.managers.strata_supervisor import (
    inspect_base_config,
    slot_save_path_error,
    strata_config_errors,
)
from dpc_client_core.providers import StrataProvider
from dpc_client_core.service import CoreService

from tests.test_strata_supervisor import home, _install, _sup  # noqa: F401


def _folder(tmp_path) -> str:
    return str(tmp_path / "sessions")


class TestTheStartConfig:
    def test_a_set_folder_is_a_top_level_key_and_never_an_engine_flag(self, tmp_path, home):
        folder = _folder(tmp_path)
        cfg = _sup(_install(tmp_path), slot_save_path=folder).build_start_config(5000)
        assert cfg["slot_save_path"] == folder
        assert not any("slot" in str(a) and "save" in str(a) for a in cfg["args"]), cfg["args"]

    @pytest.mark.parametrize("value", [None, ""])
    def test_unset_or_empty_writes_no_key(self, tmp_path, home, value):
        cfg = _sup(_install(tmp_path), slot_save_path=value).build_start_config(5000)
        assert "slot_save_path" not in cfg

    def test_a_set_folder_replaces_the_base_configs_and_unset_keeps_it(self, tmp_path, home):
        root = _install(tmp_path, slot_save_path=str(tmp_path / "from-base"))
        assert _sup(root).build_start_config(5000)["slot_save_path"] == str(tmp_path / "from-base")
        folder = _folder(tmp_path)
        assert _sup(root, slot_save_path=folder).build_start_config(5000)["slot_save_path"] == folder
        assert inspect_base_config("strata-base.json", root)["values"]["slot_save_path"] == str(tmp_path / "from-base")

    def test_building_the_config_creates_nothing_but_writing_it_does(self, tmp_path, home):
        folder = Path(_folder(tmp_path))
        sup = _sup(_install(tmp_path), slot_save_path=str(folder))
        sup.build_start_config(5000)
        assert not folder.exists()
        path = sup.write_start_config(5000)
        assert folder.is_dir()
        assert json.loads(path.read_text(encoding="utf-8"))["slot_save_path"] == str(folder)

    def test_an_existing_folder_is_left_alone(self, tmp_path, home):
        folder = tmp_path / "sessions"
        folder.mkdir()
        (folder / "kept.bin").write_bytes(b"x")
        _sup(_install(tmp_path), slot_save_path=str(folder)).write_start_config(5000)
        assert (folder / "kept.bin").read_bytes() == b"x"

    def test_a_folder_that_cannot_be_made_is_refused_in_words(self, tmp_path, home):
        blocker = tmp_path / "a-file"
        blocker.write_text("x", encoding="utf-8")
        sup = _sup(_install(tmp_path), slot_save_path=str(blocker / "inside"))
        with pytest.raises(LlamaServerError, match="cannot create the session files folder"):
            sup.write_start_config(5000)

    @pytest.mark.parametrize("bad", ["sessions", "./sessions", "~/sessions", "C:", "bad\npath", "bad\x00path", "  ", 5, True])
    def test_a_bad_path_is_refused_at_build_and_at_save(self, tmp_path, home, bad):
        with pytest.raises(LlamaServerError, match="slot_save_path"):
            _sup(_install(tmp_path), slot_save_path=bad).build_start_config(5000)
        config = {"strata_install_dir": "Z:/none", "strata_base_config": "x.json",
                  "conversation_cache_mib": 1, "slot_save_path": bad}
        errors = strata_config_errors(config)
        assert len(errors) == 1 and errors[0].startswith("slot_save_path: "), errors

    def test_the_validator_takes_an_absolute_path_of_this_os(self, tmp_path):
        assert slot_save_path_error(str(tmp_path / "x")) is None
        assert slot_save_path_error(None) is None and slot_save_path_error("") is None
        assert "absolute" in slot_save_path_error("relative/dir")


class TestTheAlias:
    def test_a_change_restarts_the_launcher_and_an_unset_field_does_not(self, tmp_path):
        cfg = {"strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
               "conversation_cache_mib": 1024}
        sig = StrataProvider._flag_signature
        assert sig(cfg) != sig({**cfg, "slot_save_path": _folder(tmp_path)})
        assert sig({**cfg, "slot_save_path": _folder(tmp_path)}) != sig({**cfg, "slot_save_path": str(tmp_path / "other")})
        assert sig(cfg) == sig({**cfg, "slot_save_path": None})

    def test_the_provider_refuses_a_relative_folder_on_load(self, tmp_path):
        config = {"type": "strata", "model": "flash", "strata_install_dir": str(_install(tmp_path)),
                  "strata_base_config": "strata-base.json", "conversation_cache_mib": 1024,
                  "slot_save_path": "sessions"}
        with pytest.raises(ValueError, match="slot_save_path"):
            StrataProvider("flash", config)

    def test_the_save_refuses_a_path_that_is_a_file(self, tmp_path):
        root = _install(tmp_path)
        occupied = tmp_path / "a-file"
        occupied.write_text("x", encoding="utf-8")
        provider = {"alias": "flash", "type": "strata", "model": "flash", "strata_install_dir": str(root),
                    "strata_base_config": "strata-base.json", "conversation_cache_mib": 10240,
                    "slot_save_path": str(occupied)}
        errors = CoreService._validate_providers_config(
            SimpleNamespace(), {"default_provider": "flash", "providers": [provider]})
        assert errors == [f"Provider 1: strata slot_save_path: {occupied} exists and is not a folder"]
        provider["slot_save_path"] = _folder(tmp_path)
        assert CoreService._validate_providers_config(
            SimpleNamespace(), {"default_provider": "flash", "providers": [provider]}) == []
        assert not Path(_folder(tmp_path)).exists(), "saving must not create the folder"
