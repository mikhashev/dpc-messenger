"""An agent's Voice model lives in its configuration, not in a chat header
(THE-CHAT-MODEL-DROPDOWN-CALLS-CLOUD-MODELS-LOCAL-AND-OFFERS-AGENT-CHATS-A-CHOICE-THE-AGENT-
CONFIG-ALREADY-MADE, 2026-11-25 addendum; follow-up 13).

Agent Configuration carries `voice_provider_alias` (who transcribes the person's voice for
this agent; runs on this node; '' is «Default (global)», None leaves the field alone).
There is no per-agent Vision model (Mike, 2026-11-25): where an image goes is his
2026-09-18 rule, pinned below: the model the agent runs, when it sees, else the global
vision_provider.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dpc_client_core import provider_alias_refs as refs
from dpc_client_core.agent_service import AgentService
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.dpc_agent.tools.transcribe import _pick_provider
from dpc_client_core.dpc_agent.tools.vision import describe_image
from dpc_client_core.managers import agent_manager as am


class _Sees:
    def __init__(self, sees, kind="ollama"):
        self._sees = sees
        self.config = {"type": kind}
        self.model_name = kind

    def supports_vision(self):
        return self._sees


PROVIDERS = {
    "text-only": _Sees(False),
    "qwen-vl": _Sees(True),
    "whisper-a": _Sees(False, "local_whisper"),
    "whisper-b": _Sees(False, "local_whisper"),
}


class _Registry:
    def __init__(self, agent_id):
        self._id = agent_id

    def get_agent(self, agent_id):
        return {"agent_id": agent_id} if agent_id == self._id else None

    def update_agent(self, agent_id, patch):
        pass


@pytest.fixture
def saver(tmp_path, monkeypatch):
    from dpc_client_core.dpc_agent import utils as agent_utils
    agent_id = "agent_test"
    cfg_path = tmp_path / agent_id / "config.json"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(json.dumps({"provider_alias": "text-only"}), encoding="utf-8")
    monkeypatch.setattr(agent_utils, "get_agent_config_path", lambda aid: tmp_path / aid / "config.json")
    monkeypatch.setattr(agent_utils, "AgentRegistry", lambda *a, **k: _Registry(agent_id))
    service = AgentService.__new__(AgentService)
    service.peer_metadata = {}
    service.llm_manager = SimpleNamespace(providers=PROVIDERS, vision_provider="qwen-vl",
                                          voice_provider="whisper-a")
    service._own_addresses_getter = None

    async def _no_refresh(_aid, _config):
        return None

    monkeypatch.setattr(service, "_refresh_live_agent_manager", _no_refresh, raising=False)

    async def _providers():
        return {"providers": [], "default_provider": ""}

    async def save(**kwargs):
        return await AgentService.save_agent_model_config(service, agent_id, **kwargs)

    async def get():
        return await AgentService.get_agent_model_config(service, agent_id, _providers)

    def read():
        return json.loads(cfg_path.read_text(encoding="utf-8"))

    return SimpleNamespace(save=save, get=get, read=read)


@pytest.mark.asyncio
async def test_the_dialog_saves_and_reads_back_voice(saver):
    result = await saver.save(voice_provider_alias="whisper-b")
    assert result["status"] == "ok"
    assert saver.read()["voice_provider_alias"] == "whisper-b"
    got = await saver.get()
    assert got["voice_provider_alias"] == "whisper-b"
    # What «Default (global)» means, for the dialog to say.
    assert got["global_voice_provider"] == "whisper-a"
    # There is no per-agent Vision model any more.
    assert "vision_provider_alias" not in got and "global_vision_provider" not in got


@pytest.mark.asyncio
async def test_an_empty_choice_clears_and_an_omitted_one_is_left_alone(saver):
    await saver.save(voice_provider_alias="whisper-b")
    await saver.save(reasoning_effort="high")
    assert saver.read()["voice_provider_alias"] == "whisper-b"
    await saver.save(voice_provider_alias="")
    assert saver.read()["voice_provider_alias"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, word", [
    ({"voice_provider_alias": "nowhere"}, "not a provider on this node"),
    ({"voice_provider_alias": "qwen-vl"}, "does not transcribe"),
])
async def test_a_model_that_cannot_do_the_job_is_refused_and_nothing_is_written(saver, kwargs, word):
    result = await saver.save(reasoning_effort="high", **kwargs)
    assert result["status"] == "error" and word in result["message"]
    assert saver.read() == {"provider_alias": "text-only"}


def test_voice_is_a_listed_role_so_a_rename_and_a_delete_follow_it():
    assert refs.AGENT_ROLE_NAMES["voice_provider_alias"] == "voice"
    assert "voice_provider_alias" in refs.AGENT_CONFIG_KEYS
    assert "vision_provider_alias" not in refs.AGENT_CONFIG_KEYS
    assert "vision_provider_alias" not in refs.AGENT_ROLE_NAMES


def test_the_manager_hands_the_configured_voice_model_to_the_agent_about_to_run():
    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager.config = {"voice_provider_alias": "whisper-b"}
    agent = SimpleNamespace(llm=SimpleNamespace(), voice_provider_alias=None)
    manager._apply_media_aliases(agent)
    assert agent.voice_provider_alias == "whisper-b"
    manager.config = {"voice_provider_alias": ""}
    manager._apply_media_aliases(agent)
    assert agent.voice_provider_alias is None


def test_a_stored_vision_provider_alias_is_ignored():
    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager.config = {"vision_provider_alias": "qwen-vl"}
    agent = SimpleNamespace(llm=SimpleNamespace(), voice_provider_alias=None)
    manager._apply_media_aliases(agent)
    assert vars(agent.llm) == {}


class _LLM:
    def __init__(self):
        self.calls = []
        self.providers = PROVIDERS

    async def query(self, prompt, provider_alias=None, return_metadata=False, images=None, **kw):
        self.calls.append(provider_alias)
        return {"response": "a cat", "model": "m"} if return_metadata else "a cat"


def _vision_ctx(tmp_path, main_alias):
    root = tmp_path / "agent_001"
    root.mkdir()
    (root / "render.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    llm = _LLM()
    ctx = ToolContext(agent_root=root, dpc_service=SimpleNamespace(llm_manager=llm))
    adapter = DpcLlmAdapter(llm, provider_alias=main_alias)
    ctx._agent = SimpleNamespace(llm=adapter)
    return ctx, llm


@pytest.mark.asyncio
async def test_an_agent_whose_main_model_cannot_see_sends_the_image_to_the_global_vision_provider(tmp_path):
    ctx, llm = _vision_ctx(tmp_path, "text-only")
    await describe_image(ctx, "render.png")
    assert llm.calls == [None]  # None = the node's global vision_provider


@pytest.mark.asyncio
async def test_an_agent_whose_main_model_sees_gets_the_image_itself(tmp_path):
    ctx, llm = _vision_ctx(tmp_path, "qwen-vl")
    await describe_image(ctx, "render.png")
    assert llm.calls == ["qwen-vl"]


def test_transcribe_audio_file_defaults_to_the_agents_voice_model():
    ctx = ToolContext(agent_root=None, dpc_service=SimpleNamespace(
        llm_manager=SimpleNamespace(providers=PROVIDERS)))
    ctx._agent = SimpleNamespace(voice_provider_alias="whisper-b")
    provider, _ = _pick_provider(ctx, None)
    assert provider is PROVIDERS["whisper-b"]
    ctx._agent = SimpleNamespace(voice_provider_alias=None)
    provider, _ = _pick_provider(ctx, None)
    assert provider is PROVIDERS["whisper-a"]
