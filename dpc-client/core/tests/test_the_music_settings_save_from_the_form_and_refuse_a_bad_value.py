"""The [music] settings of the Create panel's engine form and the player's settings
form: saved, and refused when a value is bad (config-parity card
THE-MUSIC-FILE-TRANSFER-VISION-AND-KNOWLEDGE-KEYS-HAVE-NO-CONTROL-IN-THE-UI, order 7).

The audit (audit/config_ui_parity_2026-11-27.md) found no UI writer for any of these
keys — rows marked "?". One command pair now covers them, in the shape of
`set_music_default_engine`: the settings writer validates every value before writing
anything, and two checks only the command layer can do run first — `describe_alias`
against the loaded providers, `yue2_instrumental_adapter` against the adapters folders
the reader scans. What is asserted:

- the two new commands are allowed and are CoreService methods (the music commands mixin);
- a save of the form's payload lands in config.ini under [music], and the getters the
  readers read answer the new values;
- an invalid value is refused with a reason naming `music.<key>` and writes nothing, not
  even the valid neighbouring key sent in the same command; the neighbour alone still
  passes;
- `export_dir` applies `_music_export_root`'s two checks at save (a relative path and a
  filesystem root are refused), and a fresh config.ini seeds the key empty (empty = the
  default export folder);
- `describe_alias` refuses an alias no loaded provider answers to, and `none` passes;
- `yue2_instrumental_adapter` refuses a name the adapters folders do not hold, and
  `none` passes;
- a key left out keeps its stored value.

Every home is under tmp_path; `DPC_HOME` points there, never at `~/.dpc`. No engine is
started, no folder watched.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.music.commands import MusicCommandsMixin
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

# The engine form's payload: a value for every START-PER-ENGINE key of yue2.cpp, plus
# one PER-JOB key — named as config.ini names them (the command writes what it is
# given, verbatim).
ENGINE_FORM = {
    "yue2_binary": "C:/engines/yue2.cpp/build/yue-server.exe",
    "yue2_models_dir": "C:/models/yue2",
    "yue2_backbone": "rs-tw",
    "yue2_port": 8500,
    "yue2_idle_stop_s": 120,
    "yue2_adapters_dir": "C:/models/yue2/loras/ruach-voices/voices",
    "yue2_instrumental_adapter": "none",
    "yue2_device": "CUDA0",
    "yue2_max_seq": 8192,
    "agent_queue_max": 3,
}

# The player form's payload: the two LIVE keys and the two START-ONLY ones.
PLAYER_FORM = {
    "export_dir": "D:/music-exports",
    "stream_ranges_linux": True,
    "stream_port": 9996,
    "poll_interval_s": 30,
}


class _LlmManager(SimpleNamespace):
    """Just the providers dict `_music_describe_alias` reads."""


class _Host(MusicCommandsMixin):
    """A CoreService-shaped host whose music commands mixin is real, with a settings
    object on a throwaway home and a provider registry of two aliases."""

    def __init__(self, home):
        self.settings = Settings(home)
        self.llm_manager = _LlmManager(providers={"music_flamingo": object(), "other": object()})


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A DPC home of its own, so no test reads or writes ~/.dpc."""
    monkeypatch.setenv("DPC_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def host(home):
    return _Host(home)


class TestTheCommands:
    def test_the_new_commands_are_allowed_and_are_service_methods(self):
        for name in ("get_music_settings", "save_music_settings"):
            assert name in ALLOWED_COMMANDS
            assert callable(getattr(CoreService, name, None))

    @pytest.mark.asyncio
    async def test_the_get_command_answers_the_readers_defaults(self, home, host):
        config = await MusicCommandsMixin.get_music_settings(host)
        assert config["status"] == "ok"
        assert config["acestep_port"] == 9995 and config["acestep_idle_stop_s"] == 300.0
        assert config["yue2_port"] == 9994 and config["yue2_backbone"] == "stock"
        assert config["yue2_max_seq"] == 0
        assert config["agent_queue_max"] == 2 and config["turn_jobs_max"] == 1
        assert config["describe_alias"] == "auto"
        assert config["auto_align_lyrics"] is True and config["lrclib_enabled"] is True
        assert config["export_dir"] == "" and config["stream_port"] == 9996
        assert config["stream_ranges_linux"] is False and config["poll_interval_s"] == 60.0


class TestAnEngineFormSave:
    @pytest.mark.asyncio
    async def test_the_forms_payload_lands_in_config_ini(self, home, host):
        out = await MusicCommandsMixin.save_music_settings(host, config=dict(ENGINE_FORM))
        assert out["status"] == "ok"
        file = Settings(home)
        assert file.get("music", "yue2_binary") == "C:/engines/yue2.cpp/build/yue-server.exe"
        assert file.get("music", "yue2_backbone") == "rs-tw"
        assert file.get("music", "yue2_port") == "8500"
        assert file.get("music", "yue2_instrumental_adapter") == "none"

    @pytest.mark.asyncio
    async def test_the_readers_getters_answer_the_new_value(self, home, host):
        await MusicCommandsMixin.save_music_settings(host, config=dict(ENGINE_FORM))
        stored = await MusicCommandsMixin.get_music_settings(host)
        assert stored["yue2_port"] == 8500
        assert stored["yue2_backbone"] == "rs-tw"
        assert stored["yue2_idle_stop_s"] == 120.0
        assert stored["yue2_max_seq"] == 8192
        assert stored["agent_queue_max"] == 3

    @pytest.mark.asyncio
    async def test_the_player_forms_payload_lands_in_config_ini(self, home, host):
        out = await MusicCommandsMixin.save_music_settings(host, config=dict(PLAYER_FORM))
        assert out["status"] == "ok"
        file = Settings(home)
        # `_music_export_root` reads the folder through `os.path.normpath`, so the writer
        # stores the normalised spelling (`D:\music-exports` on Windows).
        assert file.get("music", "export_dir") == os.path.normpath("D:/music-exports")
        assert file.get("music", "stream_ranges_linux") == "true"
        assert file.get("music", "poll_interval_s") == "30"

    @pytest.mark.asyncio
    async def test_a_key_left_out_keeps_its_stored_value(self, home, host):
        await MusicCommandsMixin.save_music_settings(host, config={"lrclib_enabled": False})
        stored = await MusicCommandsMixin.get_music_settings(host)
        assert stored["lrclib_enabled"] is False
        assert stored["auto_align_lyrics"] is True, "untouched"
        assert stored["yue2_port"] == 9994, "untouched"

    @pytest.mark.asyncio
    async def test_an_invalid_port_is_refused_and_the_valid_neighbour_still_passes(self, home, host):
        Settings(home)  # the fresh file is written here, before the snapshot
        before = (home / "config.ini").read_bytes()
        out = await MusicCommandsMixin.save_music_settings(
            host, config={"yue2_port": 70000, "agent_queue_max": 4})
        assert out["status"] == "error" and "music.yue2_port" in out["message"]
        assert (home / "config.ini").read_bytes() == before, \
            "the valid neighbouring key was not written either"
        out = await MusicCommandsMixin.save_music_settings(host, config={"agent_queue_max": 4})
        assert out["status"] == "ok"
        stored = await MusicCommandsMixin.get_music_settings(host)
        assert stored["agent_queue_max"] == 4
        assert stored["yue2_port"] == 9994, "the refused value"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["yue2_port", "acestep_port", "stream_port"])
    async def test_port_zero_is_a_free_port_at_each_start_and_is_written(self, home, host, key):
        """The three readers take 0 (engine_base.py's port(); `_music_stream_port`), the
        stream server binds 0 and then keeps the port it was given, and the template's own
        comment says so. A person whose fixed port is taken needs that value from the form
        as much as from config.ini. Read from the parser, not through Settings.get: that
        path takes a DPC_MUSIC_<KEY> variable first, and the file is what is asserted."""
        out = await MusicCommandsMixin.save_music_settings(host, config={key: 0})
        assert out["status"] == "ok", out
        assert Settings(home)._config.get("music", key) == "0"
        stored = await MusicCommandsMixin.get_music_settings(host)
        assert stored[key] == 0, "a written 0 reads back as 0, not as the default"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("config, key", [
        ({"yue2_port": 70000}, "music.yue2_port"),
        ({"acestep_port": "many"}, "music.acestep_port"),
        ({"stream_port": 70000}, "music.stream_port"),
        ({"yue2_backbone": "turbo"}, "music.yue2_backbone"),
        ({"yue2_idle_stop_s": -5}, "music.yue2_idle_stop_s"),
        ({"acestep_idle_stop_s": "soon"}, "music.acestep_idle_stop_s"),
        ({"poll_interval_s": 0}, "music.poll_interval_s"),
        ({"poll_interval_s": -1}, "music.poll_interval_s"),
        ({"yue2_device": "cuda"}, "music.yue2_device"),      # the documented set is CUDA0/…
        ({"acestep_device": "mps"}, "music.acestep_device"),
        ({"yue2_max_seq": -2}, "music.yue2_max_seq"),
        # `_music_positive_int` replaces a figure below 1 with the default, so the writer
        # refuses it rather than letting a written 0 silently mean 2.
        ({"agent_queue_max": 0}, "music.agent_queue_max"),
        ({"turn_jobs_max": -1}, "music.turn_jobs_max"),
        ({"auto_align_lyrics": "yes"}, "music.auto_align_lyrics"),
        ({"lrclib_enabled": 1}, "music.lrclib_enabled"),
        ({"export_dir": "music/exports"}, "music.export_dir"),
        ({"export_dir": "/"}, "music.export_dir"),
        ({"export_dir": "C:/"}, "music.export_dir"),
        ({"yue2_binary": 5}, "music.yue2_binary"),
    ])
    async def test_values_of_the_wrong_kind_are_refused_with_their_key(
            self, home, host, config, key):
        out = await MusicCommandsMixin.save_music_settings(host, config=config)
        assert out["status"] == "error" and key in out["message"], out

    @pytest.mark.asyncio
    async def test_a_refusal_leaves_config_ini_byte_identical(self, home, host):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        await MusicCommandsMixin.save_music_settings(host, config={"yue2_backbone": "turbo"})
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    async def test_an_unknown_key_is_refused(self, home, host):
        out = await MusicCommandsMixin.save_music_settings(host, config={"default_engine": "acestep"})
        assert out["status"] == "error" and "default_engine" in out["message"], \
            "the default engine keeps its own command"
        out = await MusicCommandsMixin.save_music_settings(host, config={"chunk_size": 65536})
        assert out["status"] == "error" and "chunk_size" in out["message"], \
            "[file_transfer] belongs to the Network form's card"

    @pytest.mark.asyncio
    async def test_a_non_object_config_is_refused(self, home, host):
        out = await MusicCommandsMixin.save_music_settings(host, config=[("yue2_port", 1)])
        assert out["status"] == "error"


class TestTheAliasChecks:
    @pytest.mark.asyncio
    async def test_a_describe_alias_no_loaded_provider_answers_to_is_refused(self, home, host):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await MusicCommandsMixin.save_music_settings(
            host, config={"describe_alias": "ghost", "turn_jobs_max": 2})
        assert out["status"] == "error" and "music.describe_alias" in out["message"]
        assert (home / "config.ini").read_bytes() == before
        # The neighbour alone passes; auto and none name no alias and pass too.
        out = await MusicCommandsMixin.save_music_settings(host, config={"turn_jobs_max": 2})
        assert out["status"] == "ok"
        for word in ("auto", "none"):
            out = await MusicCommandsMixin.save_music_settings(host, config={"describe_alias": word})
            assert out["status"] == "ok"
            assert Settings(home).get("music", "describe_alias") == word

    @pytest.mark.asyncio
    async def test_a_loaded_provider_alias_is_written_verbatim(self, home, host):
        out = await MusicCommandsMixin.save_music_settings(host, config={"describe_alias": "other"})
        assert out["status"] == "ok"
        assert Settings(home).get("music", "describe_alias") == "other"

    @pytest.mark.asyncio
    async def test_an_instrumental_adapter_the_folders_do_not_hold_is_refused(self, home, host):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await MusicCommandsMixin.save_music_settings(
            host, config={"yue2_instrumental_adapter": "ghost", "yue2_port": 8501})
        assert out["status"] == "error" and "music.yue2_instrumental_adapter" in out["message"]
        assert (home / "config.ini").read_bytes() == before
        # none (no adapter) and empty (the default adapter) pass.
        out = await MusicCommandsMixin.save_music_settings(
            host, config={"yue2_instrumental_adapter": "none"})
        assert out["status"] == "ok"
        assert Settings(home).get("music", "yue2_instrumental_adapter") == "none"


class TestTheExportFolder:
    @pytest.mark.asyncio
    async def test_a_fresh_config_ini_seeds_the_key_empty(self, home):
        Settings(home)  # the fresh file is written here
        text = (home / "config.ini").read_text(encoding="utf-8")
        assert "export_dir" in text, "the key a person can discover"
        assert Settings(home).get("music", "export_dir") == "", "empty = the default folder"

    @pytest.mark.asyncio
    async def test_an_absolute_folder_is_written_and_the_default_comes_back(self, home, host):
        target = home / "exports" / "mine"
        out = await MusicCommandsMixin.save_music_settings(host, config={"export_dir": str(target)})
        assert out["status"] == "ok"
        assert Settings(home).get("music", "export_dir") == str(target)
        # Empty writes back to the default folder.
        out = await MusicCommandsMixin.save_music_settings(host, config={"export_dir": ""})
        assert out["status"] == "ok"
        assert Settings(home).get("music", "export_dir") == ""
