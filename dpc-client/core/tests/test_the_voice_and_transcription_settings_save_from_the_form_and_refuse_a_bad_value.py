"""The voice, local transcription and voice message settings: saved from the form, refused
when a value is bad (THE-VOICE-AND-TRANSCRIPTION-SETTINGS-ARE-EDITABLE-ONLY-BY-HAND-IN-CONFIG-INI).

The three sections' live keys were edited by hand in config.ini only;
`save_voice_transcription_config` existed but no component called it, and the live
`[local_transcription]` and `[voice_messages]` keys had no writer at all. The voice
settings form now covers them (audit/config_ui_parity_2026-11-27.md, the rows for the
three sections). What is asserted:

- the four new commands are allowed and are CoreService methods, delegated to VoiceService;
- a save of the form's payload lands in config.ini through Settings, and the readers the
  service handed over at start (the live source methods) see the new value without a
  restart — every covered key is live per the audit, none needs a restart;
- the form's payload for the existing `save_voice_transcription_config` writes all eight
  `[voice_transcription]` keys, and the get now answers `timeout_seconds` too;
- an invalid value is refused with a reason naming its key and writes nothing, not even
  the valid neighbouring key sent in the same command; the neighbour alone still passes;
- a key left out keeps its stored value.

Every home is under tmp_path; `DPC_HOME` points there, never at `~/.dpc`. No model is
loaded and no audio is transcribed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings
from dpc_client_core.voice_service import VoiceService

# The form's payload for [voice_transcription]: the eight keys of the card, named as
# config.ini names them (the command writes what it is given, verbatim).
VOICE_TRANSCRIPTION_FORM = {
    "enabled": True,
    "sender_transcribes": False,
    "recipient_delay_seconds": 5,
    "timeout_seconds": 120,
    "provider_priority": ["whisper-large-v3-turbo", "whisper-small", "openai"],
    "show_transcriber_name": True,
    "cache_transcriptions": False,
    "fallback_to_openai": False,
}

# The form's payload for the nine live [local_transcription] keys. The nine dead keys
# of the section (model, device, compile, flash, chunk, batch, language, task, lazy)
# are another card and are never sent.
LOCAL_TRANSCRIPTION_FORM = {
    "enabled": False,
    "fallback_to_openai": False,
    "max_file_size_mb": 40,
    "max_duration_seconds": 900,
    "idle_unload_s": 120,
    "queue_wait_s": 60,
    "cpu_max_s": 450,
    "cpu_max_audio_s": 240,
    "cpu_contention_factor": 1.5,
}

# The form's payload for the seven live [voice_messages] keys (`enabled` is dead: no
# reader, so the form has no field for it).
VOICE_MESSAGES_FORM = {
    "max_duration_seconds": 600,
    "max_size_mb": 25,
    "mime_types": ["audio/webm", "audio/ogg", "audio/wav"],
    "default_sample_rate": 16000,
    "default_channels": 2,
    "default_codec": "wav",
    "auto_transcribe": False,
}


class RecordingApi:
    """A local API that records the events a save broadcasts."""

    def __init__(self):
        self.events = []

    async def broadcast_event(self, event_name, payload):
        self.events.append((event_name, payload))


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A DPC home of its own, so no test reads or writes ~/.dpc."""
    monkeypatch.setenv("DPC_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def host(home):
    """A CoreService-shaped host whose voice service is real, with a recording API."""
    api = RecordingApi()
    voice = VoiceService(
        llm_manager=SimpleNamespace(providers={}),
        settings=Settings(home),
        local_api=api,
    )
    return SimpleNamespace(voice_service=voice)


class TestTheCommands:
    def test_the_new_commands_are_allowed_and_are_service_methods(self):
        for name in ("get_local_transcription_config", "save_local_transcription_config",
                     "get_voice_messages_config", "save_voice_messages_config"):
            assert name in ALLOWED_COMMANDS
            assert callable(getattr(CoreService, name, None))

    @pytest.mark.asyncio
    async def test_the_get_commands_answer_the_sections_with_their_defaults(self, home, host):
        local = await CoreService.get_local_transcription_config(host)
        assert local["status"] == "success"
        assert local["enabled"] is True and local["queue_wait_s"] == 600
        assert local["cpu_contention_factor"] == 1.0 and local["max_duration_seconds"] is None
        messages = await CoreService.get_voice_messages_config(host)
        assert messages["status"] == "success"
        assert messages["max_duration_seconds"] == 300.0 and messages["max_size_mb"] == 10
        assert messages["default_sample_rate"] == 48000 and messages["auto_transcribe"] is True

    @pytest.mark.asyncio
    async def test_the_voice_transcription_get_answers_all_eight_keys(self, home, host):
        config = await CoreService.get_voice_transcription_config(host)
        assert config["status"] == "success"
        for key in ("enabled", "sender_transcribes", "recipient_delay_seconds", "timeout_seconds",
                    "provider_priority", "show_transcriber_name", "cache_enabled",
                    "fallback_to_openai"):
            assert key in config, f"{key} was missing from the get answer"


class TestAVoiceTranscriptionSave:
    @pytest.mark.asyncio
    async def test_the_forms_payload_lands_in_config_ini(self, home, host):
        out = await CoreService.save_voice_transcription_config(host, config=dict(VOICE_TRANSCRIPTION_FORM))
        assert out["status"] == "success"
        file = Settings(home)
        assert file.get("voice_transcription", "enabled") == "True"
        assert file.get("voice_transcription", "timeout_seconds") == "120"
        assert file.get("voice_transcription", "provider_priority") == \
            "whisper-large-v3-turbo,whisper-small,openai"
        assert file.get("voice_transcription", "cache_transcriptions") == "False"
        # The readers' own getters parse the written words back.
        assert file.get_voice_transcription_timeout_seconds() == 120
        assert file.get_voice_transcription_provider_priority() == \
            ["whisper-large-v3-turbo", "whisper-small", "openai"]


class TestALocalTranscriptionSave:
    @pytest.mark.asyncio
    async def test_the_forms_payload_lands_in_config_ini(self, home, host):
        out = await CoreService.save_local_transcription_config(
            host, config=dict(LOCAL_TRANSCRIPTION_FORM))
        assert out == {"status": "success",
                       "message": "Local transcription settings saved successfully"}
        file = Settings(home)
        assert file.get("local_transcription", "enabled") == "false"
        assert file.get("local_transcription", "queue_wait_s") == "60"
        assert file.get("local_transcription", "cpu_contention_factor") == "1.5"
        assert file.get("local_transcription", "max_duration_seconds") == "900"

    @pytest.mark.asyncio
    async def test_the_live_source_methods_see_it_without_a_restart(self, home, host):
        await CoreService.save_local_transcription_config(
            host, config={"idle_unload_s": 90, "cpu_max_s": 0, "cpu_contention_factor": 2})
        live = host.voice_service.settings
        assert live.get_local_transcription_idle_unload_s() == 90
        # 0 turns the CPU route off (Mike, 2026-11-24), it is not the default 600.
        assert live.get_local_transcription_cpu_route() == {
            "cpu_max_s": 0.0, "cpu_max_audio_s": 300.0, "cpu_contention_factor": 2.0}

    @pytest.mark.asyncio
    async def test_a_key_left_out_keeps_its_stored_value(self, home, host):
        await CoreService.save_local_transcription_config(host, config={"queue_wait_s": 30})
        assert Settings(home).get("local_transcription", "idle_unload_s") == "300", "untouched"
        assert Settings(home).get("local_transcription", "queue_wait_s") == "30"

    @pytest.mark.asyncio
    async def test_an_empty_max_duration_clears_a_stored_number_back_to_the_legacy_bound(
            self, home, host):
        await CoreService.save_local_transcription_config(host, config={"max_duration_seconds": 900})
        assert Settings(home).get_local_transcription_max_duration_seconds() == 900.0
        out = await CoreService.save_local_transcription_config(
            host, config={"max_duration_seconds": ""})
        assert out["status"] == "success"
        assert Settings(home).get("local_transcription", "max_duration_seconds") == ""
        assert Settings(home).get_local_transcription_max_duration_seconds() is None

    @pytest.mark.asyncio
    async def test_an_invalid_value_is_refused_and_the_valid_neighbour_still_passes(self, home, host):
        Settings(home)  # the seeds are written here, before the snapshot
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_local_transcription_config(
            host, config={"queue_wait_s": -5, "enabled": False})
        assert out["status"] == "error" and "queue_wait_s" in out["message"]
        assert (home / "config.ini").read_bytes() == before, \
            "the valid neighbouring key was not written either"
        # The neighbour alone passes, and the refused key still holds its stored value.
        out = await CoreService.save_local_transcription_config(host, config={"enabled": False})
        assert out["status"] == "success"
        file = Settings(home)
        assert file.get("local_transcription", "enabled") == "false"
        assert file.get_local_transcription_queue_wait_s() == 600

    @pytest.mark.asyncio
    @pytest.mark.parametrize("config, key", [
        ({"enabled": "yes"}, "enabled"),
        ({"max_file_size_mb": "lots"}, "max_file_size_mb"),
        ({"max_duration_seconds": -1}, "max_duration_seconds"),
        ({"idle_unload_s": True}, "idle_unload_s"),
        ({"cpu_max_s": float("nan")}, "cpu_max_s"),
        ({"cpu_contention_factor": 0}, "cpu_contention_factor"),
        ({"queue_wait_s": 1.5}, "queue_wait_s"),
    ])
    async def test_values_of_the_wrong_kind_are_refused_with_their_key(
            self, home, host, config, key):
        out = await CoreService.save_local_transcription_config(host, config=config)
        assert out["status"] == "error" and key in out["message"]

    @pytest.mark.asyncio
    async def test_an_unknown_key_is_refused(self, home, host):
        out = await CoreService.save_local_transcription_config(
            host, config={"model": "openai/whisper-small"})
        assert out["status"] == "error" and "model" in out["message"]
        assert Settings(home).get("local_transcription", "model", "absent") == "absent", \
            "a dead key was not written through the back door"

    @pytest.mark.asyncio
    async def test_a_save_broadcasts_the_stored_values(self, home, host):
        await CoreService.save_local_transcription_config(host, config={"queue_wait_s": 45})
        (name, payload) = host.voice_service.local_api.events[-1]
        assert name == "local_transcription_config_updated"
        assert payload["queue_wait_s"] == 45 and payload["enabled"] is True


class TestAVoiceMessagesSave:
    @pytest.mark.asyncio
    async def test_the_forms_payload_lands_in_config_ini(self, home, host):
        out = await CoreService.save_voice_messages_config(host, config=dict(VOICE_MESSAGES_FORM))
        assert out == {"status": "success",
                       "message": "Voice message settings saved successfully"}
        file = Settings(home)
        assert file.get("voice_messages", "max_duration_seconds") == "600"
        assert file.get("voice_messages", "max_size_mb") == "25"
        assert file.get("voice_messages", "mime_types") == "audio/webm,audio/ogg,audio/wav"
        assert file.get("voice_messages", "default_sample_rate") == "16000"
        assert file.get("voice_messages", "default_channels") == "2"
        assert file.get("voice_messages", "default_codec") == "wav"
        assert file.get("voice_messages", "auto_transcribe") == "false"
        # The readers the recorders and senders use read the same words live.
        assert file.get_voice_messages_settings()["mime_types"] == \
            ["audio/webm", "audio/ogg", "audio/wav"]

    @pytest.mark.asyncio
    async def test_an_invalid_value_is_refused_and_the_valid_neighbour_still_passes(self, home, host):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_voice_messages_config(
            host, config={"max_size_mb": 0, "auto_transcribe": False})
        assert out["status"] == "error" and "max_size_mb" in out["message"]
        assert (home / "config.ini").read_bytes() == before, \
            "the valid neighbouring key was not written either"
        out = await CoreService.save_voice_messages_config(host, config={"auto_transcribe": False})
        assert out["status"] == "success"
        file = Settings(home)
        assert file.get("voice_messages", "auto_transcribe") == "false"
        assert int(file.get("voice_messages", "max_size_mb", "10")) == 10, "the refused value"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("config, key", [
        ({"max_duration_seconds": 0}, "max_duration_seconds"),
        ({"max_size_mb": "lots"}, "max_size_mb"),
        ({"mime_types": []}, "mime_types"),
        ({"default_sample_rate": 0}, "default_sample_rate"),
        ({"default_channels": 1.5}, "default_channels"),
        ({"default_codec": "  "}, "default_codec"),
        ({"auto_transcribe": "yes"}, "auto_transcribe"),
    ])
    async def test_values_of_the_wrong_kind_are_refused_with_their_key(
            self, home, host, config, key):
        out = await CoreService.save_voice_messages_config(host, config=config)
        assert out["status"] == "error" and key in out["message"]

    @pytest.mark.asyncio
    async def test_the_dead_enabled_key_is_not_one_of_the_command(self, home, host):
        out = await CoreService.save_voice_messages_config(host, config={"enabled": False})
        assert out["status"] == "error" and "enabled" in out["message"]
