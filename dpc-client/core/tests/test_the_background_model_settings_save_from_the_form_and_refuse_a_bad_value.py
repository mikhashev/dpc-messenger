"""The node-level background model settings: saved from the form, refused when a value
is bad (A-NODE-HAS-NO-ONE-SWITCH-THAT-KEEPS-ITS-BACKGROUND-MODELS-OFF-THE-GPU).

The config.ini side landed first (07554a25): [memory] embedding_device and
[knowledge_graph] gliner_device, and the audit (audit/config_ui_parity_2026-11-27.md)
found no UI writer for them or for [knowledge_graph] backend — rows marked "?". The
background models form now covers the three keys through one command pair whose writer
validates every value before writing anything. What is asserted:

- the two new commands are allowed and are CoreService methods, delegated to
  BackgroundModelsService;
- a save of the form's payload lands in config.ini under [memory] and [knowledge_graph],
  and the getters the readers read (service start for the embedding device, each graph
  open for the two knowledge graph keys) answer the new value;
- an invalid device string is refused with a reason naming its key and writes nothing,
  not even the valid neighbouring key sent in the same command; the neighbour alone
  still passes — GLiNER's set has no mps, and the writer must say so;
- a key left out keeps its stored value.

Every home is under tmp_path; `DPC_HOME` points there, never at `~/.dpc`. No model is
loaded, no graph opened.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dpc_client_core.background_models_service import BackgroundModelsService
from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

# The form's payload: the three node-level keys, named as config.ini names them (the
# command writes what it is given, verbatim).
BACKGROUND_MODELS_FORM = {
    "embedding_device": "cuda",
    "gliner_device": "cpu",
    "backend": "grafeo",
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
    """A CoreService-shaped host whose background models service is real, with a
    recording API."""
    api = RecordingApi()
    service = BackgroundModelsService(
        settings=Settings(home),
        local_api=api,
    )
    return SimpleNamespace(background_models_service=service)


class TestTheCommands:
    def test_the_new_commands_are_allowed_and_are_service_methods(self):
        for name in ("get_background_model_config", "save_background_model_config"):
            assert name in ALLOWED_COMMANDS
            assert callable(getattr(CoreService, name, None))

    @pytest.mark.asyncio
    async def test_the_get_command_answers_the_three_keys_with_their_defaults(self, home, host):
        config = await CoreService.get_background_model_config(host)
        assert config["status"] == "success"
        assert config["embedding_device"] == "auto"
        assert config["gliner_device"] == "auto"
        assert config["backend"] == "sqlite"


class TestABackgroundModelSave:
    @pytest.mark.asyncio
    async def test_the_forms_payload_lands_in_config_ini(self, home, host):
        out = await CoreService.save_background_model_config(
            host, config=dict(BACKGROUND_MODELS_FORM))
        assert out == {"status": "success",
                       "message": "Background model settings saved successfully"}
        file = Settings(home)
        assert file.get("memory", "embedding_device") == "cuda"
        assert file.get("knowledge_graph", "gliner_device") == "cpu"
        assert file.get("knowledge_graph", "backend") == "grafeo"

    @pytest.mark.asyncio
    async def test_the_readers_getters_answer_the_new_value(self, home, host):
        await CoreService.save_background_model_config(
            host, config=dict(BACKGROUND_MODELS_FORM))
        file = Settings(home)
        # service.py hands get_embedding_device() to the memory module once at start;
        # KnowledgeGraph reads the two others at each open — all three through these.
        assert file.get_embedding_device() == "cuda"
        assert file.get_gliner_device() == "cpu"
        assert file.get_kg_backend() == "grafeo"

    @pytest.mark.asyncio
    async def test_a_key_left_out_keeps_its_stored_value(self, home, host):
        await CoreService.save_background_model_config(host, config={"gliner_device": "cpu"})
        assert Settings(home).get("memory", "embedding_device") == "auto", "untouched"
        assert Settings(home).get("knowledge_graph", "gliner_device") == "cpu"
        assert Settings(home).get("knowledge_graph", "backend") == "sqlite", "untouched"

    @pytest.mark.asyncio
    async def test_an_invalid_device_is_refused_and_the_valid_neighbour_still_passes(
            self, home, host):
        Settings(home)  # the seeds are written here, before the snapshot
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_background_model_config(
            host, config={"gliner_device": "mps", "backend": "grafeo"})
        assert out["status"] == "error" and "gliner_device" in out["message"]
        assert (home / "config.ini").read_bytes() == before, \
            "the valid neighbouring key was not written either"
        # The neighbour alone passes, and the refused key still holds its stored value.
        out = await CoreService.save_background_model_config(host, config={"backend": "grafeo"})
        assert out["status"] == "success"
        file = Settings(home)
        assert file.get("knowledge_graph", "backend") == "grafeo"
        assert file.get_gliner_device() == "auto", "the refused value"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("config, key", [
        ({"embedding_device": "gpu"}, "embedding_device"),
        ({"embedding_device": ""}, "embedding_device"),
        ({"embedding_device": 5}, "embedding_device"),
        # GLiNER's reader knows no mps — a value the embedding key takes is refused here.
        ({"gliner_device": "mps"}, "gliner_device"),
        ({"gliner_device": True}, "gliner_device"),
        ({"backend": "postgres"}, "backend"),
        ({"backend": ""}, "backend"),
    ])
    async def test_values_of_the_wrong_kind_are_refused_with_their_key(
            self, home, host, config, key):
        out = await CoreService.save_background_model_config(host, config=config)
        assert out["status"] == "error" and key in out["message"]

    @pytest.mark.asyncio
    async def test_an_unknown_key_is_refused(self, home, host):
        out = await CoreService.save_background_model_config(
            host, config={"model": "gtx-4090"})
        assert out["status"] == "error" and "model" in out["message"]
        assert not (home / "agents").exists(), "nothing but config.ini was touched"

    @pytest.mark.asyncio
    async def test_a_save_broadcasts_the_stored_values(self, home, host):
        await CoreService.save_background_model_config(host, config={"gliner_device": "cpu"})
        (name, payload) = host.background_models_service.local_api.events[-1]
        assert name == "background_model_config_updated"
        assert payload["gliner_device"] == "cpu" and payload["embedding_device"] == "auto"
        assert payload["backend"] == "sqlite"
