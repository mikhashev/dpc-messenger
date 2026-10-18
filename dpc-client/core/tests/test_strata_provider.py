"""The `strata` provider type (ADR-044): registration, vision from the live start,
the class boundary on adoption, and local classification by purpose (point 10)."""

import json
from types import SimpleNamespace

import pytest

from dpc_client_core.llm_manager import PROVIDER_MAP
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import LlamaServerSupervisor
from dpc_client_core.managers.strata_supervisor import StrataSupervisor
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

from tests.test_strata_supervisor import _FakeProc, _install


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


def _config(root, **overrides):
    config = {
        "type": "strata", "alias": "flash", "model": "qwen3.8-flash-next-iq3_xxs",
        "strata_install_dir": str(root), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, "context_window": 200000,
    }
    config.update(overrides)
    return config


def test_the_type_is_registered_and_the_validator_accepts_it(tmp_path):
    from dpc_client_core.service import CoreService

    assert PROVIDER_MAP["strata"] is StrataProvider
    errors = CoreService._validate_providers_config(
        SimpleNamespace(), {"default_provider": "flash", "providers": [_config(_install(tmp_path))]}
    )
    assert errors == []


def test_the_provider_owns_a_strata_supervisor_and_needs_the_install(tmp_path):
    p = StrataProvider("flash", _config(_install(tmp_path)))
    assert type(p.supervisor) is StrataSupervisor
    with pytest.raises(ValueError, match="strata_install_dir"):
        StrataProvider("flash", {"type": "strata", "strata_base_config": "x.json"})


def test_context_window_stays_the_config_field(tmp_path):
    p = StrataProvider("flash", _config(_install(tmp_path), max_context=261120))
    assert p.config["context_window"] == 200000


def test_effort_words_come_from_the_tokenizer_template(tmp_path):
    p = StrataProvider("flash", _config(_install(tmp_path)))
    assert set(p._template_efforts) == {"xhigh", "medium", "low"}
    assert p._template_efforts_source == "model"


def test_no_budget_message_is_sent_by_default(tmp_path):
    p = StrataProvider("flash", _config(_install(tmp_path), reasoning_budget_tokens=4096))
    body = p._build_extra_body("low", effective_max_tokens=8192)
    assert body["reasoning_budget_tokens"] == 4096
    assert "reasoning_budget_message" not in body


class TestVisionFromTheLiveStart:
    def test_false_when_nothing_runs(self, tmp_path):
        p = StrataProvider("flash", _config(_install(tmp_path)))
        assert p.supports_vision() is False

    def test_the_running_launchers_props_decide(self, tmp_path):
        p = StrataProvider("flash", _config(_install(tmp_path)))
        p.supervisor._proc = _FakeProc()
        p.supervisor.props = {"modalities": {"vision": True}}
        assert p.supports_vision() is True
        p.supervisor.props = {"modalities": {"vision": False}}
        assert p.supports_vision() is False

    def test_a_dead_launcher_with_stale_props_says_no(self, tmp_path):
        p = StrataProvider("flash", _config(_install(tmp_path)))
        p.supervisor._proc = _FakeProc(returncode=1)
        p.supervisor.props = {"modalities": {"vision": True}}
        assert p.supports_vision() is False


class TestAdoption:
    def test_an_unchanged_reload_adopts_the_running_launcher(self, tmp_path):
        root = _install(tmp_path)
        first = StrataProvider("flash", _config(root))
        first.supervisor.props = {"x": 1}
        second = StrataProvider("flash", _config(root, start_timeout_s=900))
        assert second.supervisor is first.supervisor
        assert second.supervisor.config["start_timeout_s"] == 900

    def test_a_changed_budget_does_not_adopt(self, tmp_path):
        root = _install(tmp_path)
        first = StrataProvider("flash", _config(root))
        first.supervisor.props = {"x": 1}
        second = StrataProvider("flash", _config(root, conversation_cache_mib=8192))
        assert second.supervisor is not first.supervisor

    def test_a_child_of_the_other_engine_is_never_adopted_under_the_same_alias(self, tmp_path):
        llama = LlamaServerSupervisor("flash", {"gguf_path": "D:/m.gguf"})
        llama.props = {"x": 1}
        _ACTIVE_SUPERVISORS["flash"] = llama
        p = StrataProvider("flash", _config(_install(tmp_path)))
        assert type(p.supervisor) is StrataSupervisor
        assert _ACTIVE_SUPERVISORS["flash"] is p.supervisor


def test_reset_is_exposed_on_the_provider(tmp_path):
    p = StrataProvider("flash", _config(_install(tmp_path)))
    p.supervisor.failure = {"reason": "dead"}
    assert p.engine_failure() == {"reason": "dead"}
    assert p.reset_engine_failure() == {"reason": "dead"}
    assert p.engine_status()["failure"] is None


class TestLocalByPurpose:
    """ADR-044 point 10: each set answers its own question about `strata`."""

    def test_the_firewall_puts_it_on_the_card_list_and_not_the_money_list(self, tmp_path):
        from dpc_client_core.firewall import ContextFirewall

        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps({"compute": {"enabled": True, "serving_local": ["flash"]}}),
                         encoding="utf-8")
        lists = ContextFirewall(rules).classify_serving_lists({"flash": "strata"})
        assert lists.local == ("flash",)
        rules.write_text(json.dumps({"compute": {
            "enabled": True, "serving_vendor": ["flash"], "vendor_quotas": {"flash": 1.0},
        }}), encoding="utf-8")
        with pytest.raises(ValueError, match="belongs under"):
            ContextFirewall(rules).classify_serving_lists({"flash": "strata"})

    def test_pricing_books_it_as_a_local_call(self):
        from dpc_client_core.dpc_agent.pricing import price_call

        price = price_call(provider_type="strata", alias="flash", model="qwen3.8-flash-next",
                           prompt_tokens=200000, completion_tokens=500)
        assert price["cost_amount"] == 0.0 and price["cost_currency"] is None
        assert price["billing"] == "subscription"

    def test_provider_facts_call_it_self_hosted(self):
        from dpc_client_core.dpc_agent.provider_facts import provider_kind

        assert provider_kind("strata") == "self_hosted"

    def test_knowledge_extraction_may_fall_back_to_it(self):
        from dpc_client_core.knowledge_routing import first_local_provider

        providers = {
            "vendor": SimpleNamespace(config={"type": "deepseek"}),
            "flash": SimpleNamespace(config={"type": "strata"}),
        }
        assert first_local_provider(providers) == "flash"

    def test_it_is_a_chat_model_at_the_gateway_not_transcription_only(self):
        from dpc_client_core.gateway import serves_chat

        assert serves_chat("strata") is True
