"""The config-parity feature keys that belong to their feature views (card
THE-MUSIC-FILE-TRANSFER-VISION-AND-KNOWLEDGE-KEYS-HAVE-NO-CONTROL-IN-THE-UI, order 7):
[vision] max_image_size_mb at the composer's image chip, the two [knowledge] keys at
the Extract Knowledge action, [history] reject_unsigned beside the history surfaces,
and [hf] offline_mode (three states) beside the background-model keys.

The same shape as the background-model form's test: one small get/save service, the
settings writer validating before writing anything. What is asserted:

- the eight new commands are allowed and are CoreService methods, delegated to
  FeatureSettingsService;
- a save of each form's payload lands in config.ini under its section, and the getters
  the readers read answer the new value;
- an invalid value is refused with a reason naming `section.key` and writes nothing,
  not even the valid neighbouring key sent in the same command; the neighbour alone
  still passes;
- [knowledge] cold_fallback_provider refuses an alias no loaded provider answers to
  (the reader, knowledge_routing.cold_fallback, refuses it at extraction time), and
  empty (auto) is written back;
- [hf] offline_mode is written in exactly run_service's accepted spelling for each of
  the three states — `true` for on, `false` for off, and the key removed for auto —
  and the getter mirrors run_service's parse (a stored `1` reads on, a stored
  unparseable word reads auto, an absent key reads auto);
- a fresh config.ini still does not write [hf] offline_mode (absence is the third
  answer, the config passport's LEFT_OUT rule);
- a key left out keeps its stored value, and each save broadcasts the stored shape.

Every home is under tmp_path; `DPC_HOME` points there, never at `~/.dpc`.
"""

from __future__ import annotations

import configparser
from types import SimpleNamespace

import pytest

from dpc_client_core.feature_settings_service import FeatureSettingsService
from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings


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
    """A CoreService-shaped host whose feature settings service is real, with a
    recording API and two loaded providers."""
    api = RecordingApi()
    service = FeatureSettingsService(
        settings=Settings(home),
        local_api=api,
        llm_manager=SimpleNamespace(providers={"local_one": object(), "local_two": object()}),
    )
    return SimpleNamespace(feature_settings_service=service)


def _file(home) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    parser.read(home / "config.ini", encoding="utf-8")
    return parser


class TestTheCommands:
    def test_the_new_commands_are_allowed_and_are_service_methods(self):
        for name in ("get_vision_config", "save_vision_config",
                     "get_knowledge_extraction_config", "save_knowledge_extraction_config",
                     "get_history_sync_config", "save_history_sync_config",
                     "get_hf_config", "save_hf_config"):
            assert name in ALLOWED_COMMANDS, name
            assert callable(getattr(CoreService, name, None)), name

    @pytest.mark.asyncio
    async def test_the_get_commands_answer_the_readers_defaults(self, home, host):
        vision = await CoreService.get_vision_config(host)
        knowledge = await CoreService.get_knowledge_extraction_config(host)
        history = await CoreService.get_history_sync_config(host)
        hf = await CoreService.get_hf_config(host)
        assert vision == {"status": "success", "max_image_size_mb": 5}
        assert knowledge == {"status": "success", "cultural_perspectives_enabled": False,
                             "cold_fallback_provider": ""}
        assert history == {"status": "success", "reject_unsigned": False}
        assert hf == {"status": "success", "offline_mode": "auto"}


class TestAVisionSave:
    @pytest.mark.asyncio
    async def test_the_forms_payload_lands_in_config_ini(self, home, host):
        out = await CoreService.save_vision_config(host, config={"max_image_size_mb": 12})
        assert out["status"] == "success"
        assert _file(home).get("vision", "max_image_size_mb") == "12"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [0, -3, 2.5, "big", True])
    async def test_a_figure_the_reader_would_not_apply_is_refused(self, home, host, value):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_vision_config(host, config={"max_image_size_mb": value})
        assert out["status"] == "error" and "vision.max_image_size_mb" in out["message"]
        assert (home / "config.ini").read_bytes() == before, "nothing was written"
        # The reader's own getter still answers the stored value.
        assert Settings(home).get_vision_max_image_size_mb() == 5

    @pytest.mark.asyncio
    async def test_an_unknown_key_is_refused(self, home, host):
        out = await CoreService.save_vision_config(host, config={"max_video_size_mb": 12})
        assert out["status"] == "error" and "max_video_size_mb" in out["message"]


class TestAKnowledgeSave:
    @pytest.mark.asyncio
    async def test_the_forms_payload_lands_in_config_ini(self, home, host):
        out = await CoreService.save_knowledge_extraction_config(host, config={
            "cultural_perspectives_enabled": True, "cold_fallback_provider": "local_one"})
        assert out["status"] == "success"
        file = _file(home)
        assert file.get("knowledge", "cultural_perspectives_enabled") == "true"
        assert file.get("knowledge", "cold_fallback_provider") == "local_one"

    @pytest.mark.asyncio
    async def test_the_readers_getters_answer_the_new_value(self, home, host):
        await CoreService.save_knowledge_extraction_config(host, config={
            "cultural_perspectives_enabled": True, "cold_fallback_provider": "local_two"})
        file = Settings(home)
        assert file.get_cultural_perspectives_enabled() is True
        assert file.get_knowledge_cold_fallback_provider() == "local_two"

    @pytest.mark.asyncio
    async def test_an_alias_no_loaded_provider_answers_to_is_refused(self, home, host):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_knowledge_extraction_config(host, config={
            "cold_fallback_provider": "paid_cloud", "cultural_perspectives_enabled": True})
        assert out["status"] == "error" and "knowledge.cold_fallback_provider" in out["message"]
        assert (home / "config.ini").read_bytes() == before, \
            "the valid neighbouring key was not written either"
        # The neighbour alone passes; empty (auto) is written back and clears the key.
        out = await CoreService.save_knowledge_extraction_config(
            host, config={"cultural_perspectives_enabled": True})
        assert out["status"] == "success"
        out = await CoreService.save_knowledge_extraction_config(
            host, config={"cold_fallback_provider": ""})
        assert out["status"] == "success"
        assert _file(home).get("knowledge", "cold_fallback_provider") == ""

    @pytest.mark.asyncio
    async def test_a_key_left_out_keeps_its_stored_value(self, home, host):
        await CoreService.save_knowledge_extraction_config(
            host, config={"cultural_perspectives_enabled": True})
        out = await CoreService.get_knowledge_extraction_config(host)
        assert out["cultural_perspectives_enabled"] is True
        assert out["cold_fallback_provider"] == "", "untouched"

    @pytest.mark.asyncio
    async def test_a_non_bool_switch_is_refused(self, home, host):
        out = await CoreService.save_knowledge_extraction_config(
            host, config={"cultural_perspectives_enabled": "yes"})
        assert out["status"] == "error" and "knowledge.cultural_perspectives_enabled" in out["message"]


class TestAHistorySave:
    @pytest.mark.asyncio
    async def test_the_switch_lands_in_config_ini_and_the_getter_answers_it(self, home, host):
        out = await CoreService.save_history_sync_config(host, config={"reject_unsigned": True})
        assert out["status"] == "success"
        assert _file(home).get("history", "reject_unsigned") == "true"
        assert Settings(home).get_reject_unsigned_history() is True

    @pytest.mark.asyncio
    async def test_a_non_bool_is_refused_and_nothing_is_written(self, home, host):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_history_sync_config(host, config={"reject_unsigned": 1})
        assert out["status"] == "error" and "history.reject_unsigned" in out["message"]
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    async def test_off_is_written_back(self, home, host):
        await CoreService.save_history_sync_config(host, config={"reject_unsigned": True})
        out = await CoreService.save_history_sync_config(host, config={"reject_unsigned": False})
        assert out["status"] == "success"
        assert _file(home).get("history", "reject_unsigned") == "false"


class TestTheHfOfflineMode:
    @pytest.mark.asyncio
    async def test_on_is_written_in_run_services_accepted_spelling(self, home, host):
        out = await CoreService.save_hf_config(host, config={"offline_mode": "on"})
        assert out["status"] == "success"
        assert _file(home).get("hf", "offline_mode") == "true"
        # run_service's own parse of that spelling:
        parser = configparser.ConfigParser()
        parser.read(home / "config.ini")
        assert parser.getboolean("hf", "offline_mode") is True

    @pytest.mark.asyncio
    async def test_off_is_written_in_run_services_accepted_spelling(self, home, host):
        out = await CoreService.save_hf_config(host, config={"offline_mode": "off"})
        assert out["status"] == "success"
        assert _file(home).get("hf", "offline_mode") == "false"
        parser = configparser.ConfigParser()
        parser.read(home / "config.ini")
        assert parser.getboolean("hf", "offline_mode") is False

    @pytest.mark.asyncio
    async def test_auto_removes_the_key(self, home, host):
        await CoreService.save_hf_config(host, config={"offline_mode": "on"})
        out = await CoreService.save_hf_config(host, config={"offline_mode": "auto"})
        assert out["status"] == "success"
        assert not _file(home).has_option("hf", "offline_mode"), \
            "with the key absent, run_service's own cache detection decides"

    @pytest.mark.asyncio
    async def test_a_state_run_service_does_not_take_is_refused(self, home, host):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_hf_config(host, config={"offline_mode": "maybe"})
        assert out["status"] == "error" and "hf.offline_mode" in out["message"]
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    async def test_the_getter_mirrors_run_services_parse(self, home, host):
        # Through the service's own settings object, the one its getter reads.
        file = host.feature_settings_service.settings
        file.set("hf", "offline_mode", "1")            # a spelling getboolean takes
        assert (await CoreService.get_hf_config(host))["offline_mode"] == "on"
        file.set("hf", "offline_mode", "maybe")        # a spelling it does not: unset
        assert (await CoreService.get_hf_config(host))["offline_mode"] == "auto"
        file.set("hf", "offline_mode", "0")
        assert (await CoreService.get_hf_config(host))["offline_mode"] == "off"

    @pytest.mark.asyncio
    async def test_a_fresh_config_ini_still_leaves_the_key_absent(self, home):
        assert not _file(home).has_option("hf", "offline_mode"), \
            "absence is the third answer (the config passport's LEFT_OUT rule)"

    @pytest.mark.asyncio
    async def test_a_save_broadcasts_the_stored_shape(self, home, host):
        await CoreService.save_hf_config(host, config={"offline_mode": "on"})
        (name, payload) = host.feature_settings_service.local_api.events[-1]
        assert name == "hf_config_updated"
        assert payload == {"offline_mode": "on"}


class TestTheSavesBroadcast:
    @pytest.mark.asyncio
    async def test_each_save_broadcasts_its_stored_shape(self, home, host):
        await CoreService.save_vision_config(host, config={"max_image_size_mb": 9})
        await CoreService.save_knowledge_extraction_config(
            host, config={"cultural_perspectives_enabled": True})
        await CoreService.save_history_sync_config(host, config={"reject_unsigned": True})
        events = dict(host.feature_settings_service.local_api.events)
        assert events["vision_config_updated"] == {"max_image_size_mb": 9}
        assert events["knowledge_extraction_config_updated"] == {
            "cultural_perspectives_enabled": True, "cold_fallback_provider": ""}
        assert events["history_sync_config_updated"] == {"reject_unsigned": True}
