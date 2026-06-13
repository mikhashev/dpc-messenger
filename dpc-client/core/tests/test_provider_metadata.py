# tests/test_provider_metadata.py
"""P2P provider metadata: shared builder, strict context-window lookup,
peer context_window resolution (REMOTE-AGENT-CONTEXT-WINDOW)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

from dpc_client_core.llm_manager import LLMManager, MODEL_CONTEXT_WINDOWS
from dpc_client_core.service import CoreService
from dpc_client_core.agent_service import AgentService
from dpc_client_core.managers.agent_manager import DpcAgentManager


def _llm_manager_with(providers: dict) -> LLMManager:
    lm = object.__new__(LLMManager)
    lm.providers = providers
    return lm


class FakeProvider:
    def __init__(self, model: str, ptype: str = "ollama", context_window=None, vision=False):
        self.model = model
        self.config = {"type": ptype}
        if context_window is not None:
            self.config["context_window"] = context_window
        self._vision = vision

    def supports_vision(self) -> bool:
        return self._vision


class TestLookupContextWindow:
    def test_known_model_returns_value(self):
        lm = _llm_manager_with({})
        known_model = next(k for k in MODEL_CONTEXT_WINDOWS if k != "default")
        assert lm.lookup_context_window(known_model) == MODEL_CONTEXT_WINDOWS[known_model]

    def test_unknown_model_returns_none(self):
        lm = _llm_manager_with({})
        assert lm.lookup_context_window("totally-custom-model-xyz") is None

    def test_provider_config_override_wins(self):
        lm = _llm_manager_with({"zai": FakeProvider("custom-glm", context_window=204800)})
        assert lm.lookup_context_window("custom-glm") == 204800

    def test_get_context_window_unknown_falls_back_to_default(self):
        lm = _llm_manager_with({})
        assert lm.get_context_window("totally-custom-model-xyz") == MODEL_CONTEXT_WINDOWS["default"]


class TestBuildP2PProviderInfo:
    def _service_stub(self, lm):
        return SimpleNamespace(
            llm_manager=lm,
            _provider_supports_voice=lambda provider: False,
        )

    def test_all_fields_present(self):
        provider = FakeProvider("custom-glm", ptype="zai", context_window=204800, vision=True)
        lm = _llm_manager_with({"zai_glm": provider})
        stub = self._service_stub(lm)
        info = CoreService.build_p2p_provider_info(stub, "zai_glm", provider)
        assert info == {
            "alias": "zai_glm",
            "model": "custom-glm",
            "type": "zai",
            "supports_vision": True,
            "supports_voice": False,
            "context_window": 204800,
        }

    def test_unknown_model_context_window_is_none(self):
        provider = FakeProvider("totally-custom-model-xyz")
        lm = _llm_manager_with({"p": provider})
        stub = self._service_stub(lm)
        info = CoreService.build_p2p_provider_info(stub, "p", provider)
        assert info["context_window"] is None


class TestPeerProviderContextWindow:
    def _agent_service_stub(self, peer_metadata):
        return SimpleNamespace(peer_metadata=peer_metadata)

    def test_resolves_from_peer_metadata(self):
        stub = self._agent_service_stub({
            "dpc-node-peer1": {"providers": [
                {"alias": "zai_glm", "model": "glm-5.1", "context_window": 204800},
            ]}
        })
        cw = AgentService._peer_provider_context_window(stub, "dpc-node-peer1", "zai_glm")
        assert cw == 204800

    def test_missing_context_window_returns_none(self):
        stub = self._agent_service_stub({
            "dpc-node-peer1": {"providers": [{"alias": "zai_glm", "model": "glm-5.1"}]}
        })
        assert AgentService._peer_provider_context_window(stub, "dpc-node-peer1", "zai_glm") is None

    def test_unknown_alias_returns_none(self):
        stub = self._agent_service_stub({
            "dpc-node-peer1": {"providers": [{"alias": "other", "context_window": 8192}]}
        })
        assert AgentService._peer_provider_context_window(stub, "dpc-node-peer1", "zai_glm") is None

    def test_unknown_peer_returns_none(self):
        stub = self._agent_service_stub({})
        assert AgentService._peer_provider_context_window(stub, "dpc-node-ghost", "zai_glm") is None


class TestSaveAgentModelConfigComputeHost:
    def _run(self, peer_metadata, provider_alias, start_config):
        stub = SimpleNamespace(peer_metadata=peer_metadata)
        stub._peer_provider_context_window = (
            lambda host, alias: AgentService._peer_provider_context_window(stub, host, alias)
        )
        stub._refresh_live_agent_manager = AsyncMock(return_value=None)
        saved = {}
        reg = MagicMock()
        reg.get_agent.return_value = {"id": "agent_x"}

        async def _providers():
            return {"providers": [], "default_provider": ""}

        with patch("dpc_client_core.dpc_agent.utils.load_agent_config", return_value=dict(start_config)), \
             patch("dpc_client_core.dpc_agent.utils.save_agent_config", side_effect=lambda aid, cfg: saved.update(cfg)), \
             patch("dpc_client_core.dpc_agent.utils.AgentRegistry", return_value=reg):
            asyncio.run(AgentService.save_agent_model_config(
                stub, "agent_x", provider_alias=provider_alias, providers_getter=_providers,
            ))
        return saved, reg

    def test_remote_main_sets_compute_host_and_window(self):
        peer_meta = {"dpc-node-peer1": {"providers": [
            {"alias": "zai_glm", "model": "glm-5.1", "context_window": 204800},
        ]}}
        saved, reg = self._run(peer_meta, "zai_glm", {"compute_host": ""})
        assert saved["compute_host"] == "dpc-node-peer1"
        assert saved["context_window"] == 204800
        reg.update_agent.assert_any_call("agent_x", {"compute_host": "dpc-node-peer1"})

    def test_local_main_clears_compute_host(self):
        peer_meta = {"dpc-node-peer1": {"providers": [
            {"alias": "zai_glm", "model": "glm-5.1", "context_window": 204800},
        ]}}
        saved, reg = self._run(
            peer_meta, "ollama_local",
            {"compute_host": "dpc-node-peer1", "context_window": 204800},
        )
        assert saved["compute_host"] == ""
        assert "context_window" not in saved
        reg.update_agent.assert_any_call("agent_x", {"compute_host": ""})

    def test_unknown_alias_falls_back_to_local(self):
        saved, reg = self._run(
            {}, "typo_or_deleted",
            {"compute_host": "dpc-node-peer1", "context_window": 204800},
        )
        assert saved["compute_host"] == ""
        assert "context_window" not in saved


class FakeMonitor:
    def __init__(self):
        self.limit = None

    def set_token_limit(self, n):
        self.limit = n


class _FakeAgent:
    def __init__(self):
        self.provider_alias = "unset"

    def set_provider_alias(self, alias):
        self.provider_alias = alias


class TestResolveAndApplyModelConfig:
    def _manager(self, config, providers):
        mgr = object.__new__(DpcAgentManager)
        mgr.config = dict(config)
        mgr.service = SimpleNamespace(llm_manager=_llm_manager_with(providers), peer_metadata={})
        mgr._agent_monitors = {}
        mgr._agent = None
        return mgr

    def test_resolve_uses_provider_override(self):
        mgr = self._manager(
            {"provider_alias": "glm52"},
            {"glm52": FakeProvider("glm-5.2", ptype="zai", context_window=1000000)},
        )
        assert mgr._resolve_context_window() == 1000000

    def test_resolve_stored_window_wins(self):
        mgr = self._manager(
            {"provider_alias": "glm52", "context_window": 524288},
            {"glm52": FakeProvider("glm-5.2", context_window=1000000)},
        )
        assert mgr._resolve_context_window() == 524288

    def test_apply_model_config_refreshes_live_monitors(self):
        mgr = self._manager(
            {"provider_alias": "old"},
            {
                "old": FakeProvider("glm-5.1", context_window=204800),
                "new": FakeProvider("glm-5.2", context_window=1000000),
            },
        )
        stale = FakeMonitor()
        stale.set_token_limit(204800)
        mgr._agent_monitors["agent_001"] = stale
        window = mgr.apply_model_config({"provider_alias": "new"})
        assert window == 1000000
        assert stale.limit == 1000000
        assert mgr.config["provider_alias"] == "new"

    def test_apply_model_config_live_refreshes_group_overlay(self):
        mgr = object.__new__(DpcAgentManager)
        mgr.config = {"provider_alias": "old"}
        mgr.agent_id = "agent_001"
        mgr._agent_monitors = {}
        mgr._agent = None
        broadcasts = []

        async def _broadcast(gid):
            broadcasts.append(gid)

        mgr.service = SimpleNamespace(
            llm_manager=_llm_manager_with({"new": FakeProvider("glm-5.2", context_window=1000000)}),
            peer_metadata={},
            _group_agent_context={"group-1": {"agent_001": (50000, 204800, "ts", "Ark")}},
            broadcast_group_token_usage=_broadcast,
        )
        window = asyncio.run(mgr.apply_model_config_live({"provider_alias": "new"}))
        assert window == 1000000
        assert mgr.service._group_agent_context["group-1"]["agent_001"][1] == 1000000
        assert broadcasts == ["group-1"]

    def test_apply_model_config_refreshes_agent_provider(self):
        mgr = self._manager(
            {"provider_alias": "old"},
            {"new": FakeProvider("glm-5.2", context_window=1000000)},
        )
        agent = _FakeAgent()
        mgr._agent = agent
        mgr.apply_model_config({"provider_alias": "new"})
        assert agent.provider_alias == "new"


def test_adapter_set_provider_alias_resets_cached_model():
    from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
    adapter = object.__new__(DpcLlmAdapter)
    adapter._provider_alias = "old"
    adapter._default_model = "cached-glm-5.1"
    adapter.set_provider_alias("glm-5.2[1m]")
    assert adapter._provider_alias == "glm-5.2[1m]"
    assert adapter._default_model is None
