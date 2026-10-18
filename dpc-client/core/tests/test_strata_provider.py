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
    def test_stopped_answers_from_the_start_config_when_the_base_reads_images(self, tmp_path):
        # The base carries --vision and a "vision" block: an image routed to a
        # stopped alias has to reach generate_with_vision, which starts it.
        p = StrataProvider("flash", _config(_install(tmp_path)))
        assert p.supervisor._proc is None
        assert p.supports_vision() is True

    def test_stopped_says_no_when_the_start_would_not_read_images(self, tmp_path):
        assert StrataProvider("flash", _config(_install(tmp_path), vision=False)).supports_vision() is False
        root = _install(tmp_path / "novision", vision=None)
        assert StrataProvider("flash", _config(root)).supports_vision() is False

    def test_an_mmproj_override_alone_does_not_turn_vision_on(self, tmp_path):
        # build_start_config adds --vision only for vision=True; the answer is its own.
        args = [a for a in json.loads((_install(tmp_path) / "strata-base.json").read_text())["args"]
                if a != "--vision"]
        root = _install(tmp_path / "noflag", args=args)
        assert StrataProvider("flash", _config(root, mmproj="D:/other-mmproj.gguf")).supports_vision() is False
        assert StrataProvider("flash", _config(root, vision=True)).supports_vision() is True

    def test_the_stopped_answer_follows_an_edit_of_the_base_config(self, tmp_path):
        root = _install(tmp_path)
        p = StrataProvider("flash", _config(root))
        assert p.supports_vision() is True
        base = root / "strata-base.json"
        cfg = json.loads(base.read_text(encoding="utf-8"))
        cfg["args"] = [a for a in cfg["args"] if a != "--vision"]
        base.write_text(json.dumps(cfg), encoding="utf-8")
        assert p.supports_vision() is False

    def test_the_stopped_answer_is_cached_while_the_base_is_unchanged(self, tmp_path, monkeypatch):
        p = StrataProvider("flash", _config(_install(tmp_path)))
        builds = []
        real = p.supervisor.build_start_config
        monkeypatch.setattr(p.supervisor, "build_start_config", lambda port: builds.append(port) or real(port))
        assert p.supports_vision() is True
        assert p.supports_vision() is True
        assert len(builds) == 1

    def test_a_stopped_alias_with_no_start_config_says_no(self, tmp_path):
        root = _install(tmp_path)
        p = StrataProvider("flash", _config(root, vision=True))
        cfg = json.loads((root / "strata-base.json").read_text(encoding="utf-8"))
        cfg.pop("vision")
        (root / "strata-base.json").write_text(json.dumps(cfg), encoding="utf-8")
        # vision=True with no "vision" block: the start would refuse, so no.
        assert p.supports_vision() is False
        (root / "strata-base.json").unlink()
        assert p.supports_vision() is False

    def test_the_running_launchers_props_decide(self, tmp_path):
        p = StrataProvider("flash", _config(_install(tmp_path)))
        p.supervisor._proc = _FakeProc()
        p.supervisor.props = {"modalities": {"vision": True}}
        assert p.supports_vision() is True
        p.supervisor.props = {"modalities": {"vision": False}}
        assert p.supports_vision() is False

    def test_the_running_launchers_no_wins_over_the_start_config(self, tmp_path):
        p = StrataProvider("flash", _config(_install(tmp_path)))
        assert p.supervisor.start_reads_images() is True
        p.supervisor._proc = _FakeProc()
        p.supervisor.props = {"modalities": {}}
        assert p.supports_vision() is False

    def test_a_dead_launchers_stale_props_are_not_read(self, tmp_path):
        p = StrataProvider("flash", _config(_install(tmp_path), vision=False))
        p.supervisor._proc = _FakeProc(returncode=1)
        p.supervisor.props = {"modalities": {"vision": True}}
        assert p.supports_vision() is False

    @pytest.mark.asyncio
    async def test_an_image_to_a_stopped_alias_starts_the_launcher(self, tmp_path, monkeypatch):
        from dpc_client_core.llm_manager import LLMManager

        p = StrataProvider("flash", _config(_install(tmp_path)))
        started = []

        async def fake_ensure_running():
            started.append(True)
            raise RuntimeError("start attempted")

        monkeypatch.setattr(p.supervisor, "ensure_running", fake_ensure_running)
        monkeypatch.setattr(p, "_is_retryable", lambda e: False)
        manager = LLMManager.__new__(LLMManager)
        manager.providers = {"flash": p}
        monkeypatch.setattr(manager, "_alias_for_query", lambda alias, images: "flash", raising=False)
        image = {"mime_type": "image/png", "base64": "iVBORw0KGgo="}
        with pytest.raises(Exception, match="start attempted"):
            await manager.query("what is this?", provider_alias="flash", images=[image])
        assert started == [True]


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

    @pytest.mark.parametrize("key,value", [("resident_budget_gib", 30), ("vram_reserve_mib", 2048)])
    def test_a_changed_resident_budget_or_reserve_does_not_adopt(self, tmp_path, key, value):
        root = _install(tmp_path)
        first = StrataProvider("flash", _config(root))
        first.supervisor.props = {"x": 1}
        second = StrataProvider("flash", _config(root, **{key: value}))
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


class TestTheEffortIsAlwaysOnTheWire:
    """Every DPC call reaches Strata through /v1/chat/completions, and Strata
    fills an effort-less request from settings DPC does not own, so each call
    kind names its effort (or turns thinking off) itself."""

    @staticmethod
    def _wired(tmp_path, **overrides):
        from tests.test_llamacpp_server_provider import _FakeSupervisor, _chat_resp, _fake_client

        p = StrataProvider("flash", _config(_install(tmp_path), **overrides))
        p.supervisor = _FakeSupervisor()
        tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="t", arguments="{}"))
        client, completions = _fake_client(_chat_resp(tool_calls=[tc]))

        async def _ensure():
            return client

        p._ensure = _ensure
        return p, completions

    @staticmethod
    async def _every_kind(p):
        await p.generate_response("hi")
        await p.generate_with_tools(
            [{"role": "user", "content": [{"type": "text", "text": "x"}]}],
            [{"name": "t", "description": "", "input_schema": {"type": "object"}}],
        )
        await p.generate_with_vision("what is this?", [{"mime_type": "image/png", "base64": "iVBORw0KGgo="}])

    @pytest.mark.asyncio
    async def test_no_effort_anywhere_sends_the_template_default(self, tmp_path):
        p, completions = self._wired(tmp_path)
        await self._every_kind(p)
        kwargs = [b["extra_body"].get("chat_template_kwargs") for b in completions.bodies]
        # plain and tools name the template's default; vision with nothing asked is off.
        assert kwargs == [{"reasoning_effort": "xhigh"}, {"reasoning_effort": "xhigh"},
                          {"enable_thinking": False}]
        assert p.get_last_usage() is not None

    @pytest.mark.asyncio
    async def test_an_unknown_alias_word_still_sends_one(self, tmp_path):
        p, completions = self._wired(tmp_path, reasoning_effort="bogus")
        await self._every_kind(p)
        assert [b["extra_body"]["chat_template_kwargs"] for b in completions.bodies] == \
            [{"reasoning_effort": "xhigh"}] * 3

    @pytest.mark.asyncio
    async def test_off_and_a_named_word_pass_through(self, tmp_path):
        p, completions = self._wired(tmp_path, reasoning_effort="off")
        await self._every_kind(p)
        assert [b["extra_body"]["chat_template_kwargs"] for b in completions.bodies] == \
            [{"enable_thinking": False}] * 3
        p, completions = self._wired(tmp_path / "low", reasoning_effort="low")
        await self._every_kind(p)
        assert [b["extra_body"]["chat_template_kwargs"] for b in completions.bodies] == \
            [{"reasoning_effort": "low"}] * 3

    @pytest.mark.asyncio
    async def test_the_stream_names_it_too(self, tmp_path):
        p, completions = self._wired(tmp_path)
        bodies = []

        async def chunks():
            delta = SimpleNamespace(content="ok", reasoning_content=None)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")], usage=None)

        async def create(**params):
            bodies.append(params)
            return chunks()

        completions.create = create
        assert await p.generate_response_stream("hi") == "ok"
        assert bodies[0]["stream"] is True
        assert bodies[0]["extra_body"]["chat_template_kwargs"] == {"reasoning_effort": "xhigh"}

    def test_no_word_to_send_warns_once_per_provider(self, tmp_path, caplog):
        # The hole the override closes stays open when neither the template's
        # default nor xhigh is among the words: say so, once, not per call.
        import logging

        p = StrataProvider("flash", _config(_install(tmp_path)))
        p._template_efforts, p._template_default = ("low", "medium"), None
        with caplog.at_level(logging.WARNING, logger="dpc_client_core.providers.strata_provider"):
            bodies = [p._build_extra_body() for _ in range(3)]
        assert all("chat_template_kwargs" not in b for b in bodies)
        warned = [r for r in caplog.records if "no effort word to send" in r.getMessage()]
        assert len(warned) == 1 and warned[0].levelno == logging.WARNING
        q = StrataProvider("flash2", _config(_install(tmp_path / "q")))
        q._template_efforts, q._template_default = ("low", "medium"), None
        with caplog.at_level(logging.WARNING, logger="dpc_client_core.providers.strata_provider"):
            q._build_extra_body()
        assert len([r for r in caplog.records if "no effort word to send" in r.getMessage()]) == 2
