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


class _ToolCalling(FakeProvider):
    """A provider with a native tools path."""

    async def generate_with_tools(self, messages, tools, system="", **kwargs):
        raise AssertionError("a menu row calls nothing")


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
            "supports_tools": False,
            "serves_images_with_tools": False,
            "context_window": 204800,
        }

    def test_unknown_model_context_window_is_none(self):
        provider = FakeProvider("totally-custom-model-xyz")
        lm = _llm_manager_with({"p": provider})
        stub = self._service_stub(lm)
        info = CoreService.build_p2p_provider_info(stub, "p", provider)
        assert info["context_window"] is None

    # `serves_images_with_tools` is what the route serves, asked of the one
    # predicate the host's gate and `query_messages` ask — not a product of the
    # two `supports_*` flags beside it.

    def _row(self, provider):
        stub = self._service_stub(_llm_manager_with({"p": provider}))
        return CoreService.build_p2p_provider_info(stub, "p", provider)

    def test_a_provider_that_sees_and_calls_tools_serves_images_with_tools(self):
        provider = _ToolCalling("qwen3.8-vl", ptype="llamacpp_server", vision=True)
        row = self._row(provider)
        assert row["supports_vision"] is True and row["supports_tools"] is True
        assert row["serves_images_with_tools"] is True

    def test_a_provider_that_calls_tools_but_cannot_see_does_not(self):
        row = self._row(_ToolCalling("qwen3:8b", vision=False))
        assert row["supports_tools"] is True
        assert row["serves_images_with_tools"] is False

    def test_a_provider_that_sees_but_calls_no_tools_does_not(self):
        row = self._row(FakeProvider("llava:13b", vision=True))
        assert row["supports_vision"] is True
        assert row["serves_images_with_tools"] is False

    def test_a_tools_attribute_that_is_none_is_no_path_though_the_two_flags_say_yes(self):
        """Where the two flags and the predicate part: `supports_tools` asks
        `hasattr`, and a class that switches its tools path off by setting it to
        None still has the attribute. `entry_point_for` asks for a callable, and
        so does `query_messages` — a row multiplying the flags would promise
        this alias a call the door refuses."""
        provider = FakeProvider("switched-off", vision=True)
        provider.generate_with_tools = None
        row = self._row(provider)
        assert row["supports_vision"] is True and row["supports_tools"] is True
        assert row["serves_images_with_tools"] is False

    def test_the_field_is_the_answer_of_entry_point_for_asked_about_images_beside_tools(self):
        """The row follows the predicate whichever way it answers, and asks it
        about the request shape the field names."""
        asked = []

        def predicate(provider, *, tools, streaming, images=False):
            asked.append({"tools": tools, "streaming": streaming, "images": images})
            return "generate_with_tools", answer

        provider = _ToolCalling("qwen3.8-vl", vision=True)
        with patch("dpc_client_core.service.entry_point_for", side_effect=predicate):
            answer = None
            assert self._row(provider)["serves_images_with_tools"] is False
            answer = object()
            assert self._row(FakeProvider("no-tools-no-eyes"))["serves_images_with_tools"] is True
        assert asked == [{"tools": True, "streaming": False, "images": True}] * 2


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
        self.compute_host = "unset"

    def set_provider_alias(self, alias):
        self.provider_alias = alias

    def set_compute_host(self, compute_host):
        self.compute_host = compute_host


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
