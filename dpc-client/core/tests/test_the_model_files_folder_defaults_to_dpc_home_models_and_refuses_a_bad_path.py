"""The model files folder: `[local_engines] model_files_dir`, step 1 of the flow.

Empty means `<DPC home>/models`. A chosen path is checked before anything is written
(absolute, not a root or a system folder, created if missing, proved writable), and a
refusal leaves config.ini as it was. `local_engine_status` reports the resolved folder;
reading it creates nothing. Every home here is under tmp_path, never `~/.dpc`.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core import settings as settings_mod
from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings


@pytest.fixture
def home(tmp_path):
    return tmp_path / "dpc-home"


@pytest.fixture
def settings(home):
    return Settings(home)


def _no_mkdir(monkeypatch):
    """A refusal must come before the folder is touched."""
    def boom(self, *a, **k):
        raise AssertionError(f"mkdir reached for {self}")

    monkeypatch.setattr(Path, "mkdir", boom)
    monkeypatch.setattr(settings_mod, "_probe_writable",
                        lambda folder: (_ for _ in ()).throw(AssertionError("probe reached")))


def _host(settings):
    return SimpleNamespace(settings=settings)


SYSTEM_FOLDER = r"C:\Windows\models" if sys.platform == "win32" else "/usr/local/models"
PROGRAM_FILES = r"C:\Program Files\DPC models" if sys.platform == "win32" else "/etc/dpc-models"


class TestDefault:
    def test_empty_resolves_to_models_under_the_dpc_home(self, settings, home):
        got = settings.get_model_files_dir()
        assert got == home / "models"
        assert got.is_absolute()

    def test_a_config_without_the_key_or_the_section_reads_the_same_default(self, home):
        home.mkdir()
        (home / "config.ini").write_text("[hub]\nurl = http://localhost:8000\n", encoding="utf-8")
        assert Settings(home).get_model_files_dir() == home / "models"

    def test_reading_the_default_creates_no_folder(self, settings, home):
        settings.get_model_files_dir()
        assert not (home / "models").exists()

    def test_another_home_gives_another_default(self, tmp_path):
        a, b = Settings(tmp_path / "a"), Settings(tmp_path / "b")
        assert a.get_model_files_dir() == tmp_path / "a" / "models"
        assert b.get_model_files_dir() == tmp_path / "b" / "models"

    def test_a_hand_edited_relative_value_is_read_as_the_default(self, home):
        home.mkdir()
        (home / "config.ini").write_text("[local_engines]\nmodel_files_dir = models-here\n", encoding="utf-8")
        assert Settings(home).get_model_files_dir() == home / "models"


class TestSet:
    def test_a_chosen_folder_round_trips_and_survives_a_reload(self, settings, home, tmp_path):
        chosen = tmp_path / "big disk" / "gguf"
        assert settings.set_model_files_dir(str(chosen)) == chosen
        assert settings.get_model_files_dir() == chosen
        assert Settings(home).get_model_files_dir() == chosen

    def test_an_empty_string_resets_to_the_default(self, settings, home, tmp_path):
        settings.set_model_files_dir(str(tmp_path / "elsewhere"))
        assert settings.set_model_files_dir("") == home / "models"
        assert settings.get_model_files_dir() == home / "models"
        assert Settings(home).get_model_files_dir() == home / "models"
        assert not (home / "models").exists(), "a reset creates nothing either"

    def test_a_missing_folder_is_created_with_its_parents(self, settings, tmp_path):
        chosen = tmp_path / "a" / "b" / "c"
        assert not chosen.exists()
        settings.set_model_files_dir(str(chosen))
        assert chosen.is_dir()

    def test_the_write_probe_leaves_nothing_behind_and_existing_files_stay(self, settings, tmp_path):
        chosen = tmp_path / "mf"
        chosen.mkdir()
        (chosen / "keep.gguf").write_bytes(b"x")
        settings.set_model_files_dir(str(chosen))
        assert [p.name for p in chosen.iterdir()] == ["keep.gguf"]

    def test_a_tilde_is_expanded(self, settings, tmp_path, monkeypatch):
        fake_home = tmp_path / "user"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        got = settings.set_model_files_dir("~/gguf")
        assert got == fake_home / "gguf"
        assert got.is_dir()

    def test_a_percent_sign_in_the_path_survives_the_config_file(self, settings, home, tmp_path):
        chosen = tmp_path / "100%" / "models"
        settings.set_model_files_dir(str(chosen))
        assert settings.get_model_files_dir() == chosen
        assert Settings(home).get_model_files_dir() == chosen

    def test_dot_dot_segments_are_collapsed(self, settings, tmp_path):
        got = settings.set_model_files_dir(str(tmp_path / "x" / ".." / "y"))
        assert got == tmp_path / "y"


class TestRefusals:
    def _refused(self, settings, home, raw, *named):
        before = (home / "config.ini").read_bytes()
        with pytest.raises(ValueError) as e:
            settings.set_model_files_dir(raw)
        assert (home / "config.ini").read_bytes() == before, "a refusal must not touch config.ini"
        assert settings.get_model_files_dir() == home / "models"
        msg = str(e.value)
        assert msg.endswith(".") and "\n" not in msg
        for word in named:
            assert word in msg
        return msg

    def test_a_relative_path_is_refused_naming_it(self, settings, home, monkeypatch):
        _no_mkdir(monkeypatch)
        self._refused(settings, home, "models/here", "models/here", "absolute")

    def test_a_filesystem_root_is_refused(self, settings, home, tmp_path, monkeypatch):
        _no_mkdir(monkeypatch)
        root = Path(tmp_path.anchor)
        self._refused(settings, home, str(root), str(root), "root")

    @pytest.mark.parametrize("raw", [SYSTEM_FOLDER, PROGRAM_FILES])
    def test_a_system_folder_is_refused(self, settings, home, monkeypatch, raw):
        _no_mkdir(monkeypatch)
        self._refused(settings, home, raw, raw, "system folder")

    def test_a_dot_dot_route_into_a_system_folder_is_refused(self, settings, home, tmp_path, monkeypatch):
        _no_mkdir(monkeypatch)
        sneaky = str(Path(SYSTEM_FOLDER).parent / "x" / ".." / "models")
        self._refused(settings, home, sneaky, "system folder")

    def test_an_unwritable_folder_is_refused_and_nothing_is_written(self, settings, home, tmp_path, monkeypatch):
        def deny(folder):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(settings_mod, "_probe_writable", deny)
        chosen = tmp_path / "ro"
        msg = self._refused(settings, home, str(chosen), str(chosen), "not writable")
        assert "Permission denied" in msg

    def test_a_folder_that_cannot_be_created_is_refused(self, settings, home, tmp_path):
        blocker = tmp_path / "a-file"
        blocker.write_text("x", encoding="utf-8")
        self._refused(settings, home, str(blocker / "inside"), str(blocker / "inside"), "cannot be created")

    def test_a_file_where_the_folder_should_be_is_refused(self, settings, home, tmp_path):
        blocker = tmp_path / "a-file"
        blocker.write_text("x", encoding="utf-8")
        self._refused(settings, home, str(blocker), str(blocker), "cannot be created")


class TestCommandAndStatus:
    def test_the_command_is_allowed_and_is_a_service_method(self):
        assert "set_model_files_dir" in ALLOWED_COMMANDS
        assert callable(getattr(CoreService, "set_model_files_dir", None))

    @pytest.mark.asyncio
    async def test_the_command_answers_with_the_resolved_folder(self, settings, tmp_path):
        chosen = tmp_path / "gguf"
        out = await CoreService.set_model_files_dir(_host(settings), path=str(chosen))
        assert out == {"status": "success", "model_files_dir": str(chosen)}
        assert chosen.is_dir()
        out = await CoreService.set_model_files_dir(_host(settings), path="")
        assert out == {"status": "success", "model_files_dir": str(settings.dpc_home_dir / "models")}

    @pytest.mark.asyncio
    async def test_the_command_answers_a_refusal_as_an_error_and_writes_nothing(self, settings, home):
        before = (home / "config.ini").read_bytes()
        out = await CoreService.set_model_files_dir(_host(settings), path="not/absolute")
        assert out["status"] == "error" and "not/absolute" in out["message"]
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    async def test_the_status_carries_the_resolved_default_and_creates_no_folder(self, settings, home):
        out = await CoreService.local_engine_status(_host(settings))
        assert out["status"] == "success"
        assert out["model_files_dir"] == str(home / "models")
        assert Path(out["model_files_dir"]).is_absolute()
        assert not (home / "models").exists()
        assert not any(p.name == "models" for p in home.rglob("*"))

    @pytest.mark.asyncio
    async def test_the_status_follows_a_chosen_folder(self, settings, tmp_path):
        chosen = tmp_path / "gguf"
        await CoreService.set_model_files_dir(_host(settings), path=str(chosen))
        out = await CoreService.local_engine_status(_host(settings))
        assert out["model_files_dir"] == str(chosen)
