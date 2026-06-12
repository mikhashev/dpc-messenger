# tests/test_provider_metadata.py
"""P2P provider metadata: shared builder, strict context-window lookup,
peer context_window resolution (REMOTE-AGENT-CONTEXT-WINDOW)."""

from types import SimpleNamespace

from dpc_client_core.llm_manager import LLMManager, MODEL_CONTEXT_WINDOWS
from dpc_client_core.service import CoreService
from dpc_client_core.agent_service import AgentService


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
