"""The llamacpp_server provider: the template's dictionary, the budget, the child.

ADR-040 route b2, step 3. These tests pin the thinking dialect without an
engine: the effort words travel verbatim from the model's own dictionary
(`low/medium/xhigh`), the fleet's `high`/`max` are translated onto `xhigh`
loudly, `off` becomes `enable_thinking: false`, and `reasoning_budget_tokens`
rides per-request on top of the alias config — capping the template's default
thinking too, not only the named efforts. The lifecycle half: the client is
lazy behind the supervisor, a reload adopts a live child with unchanged flags,
and close() drains.
"""

import json
from types import SimpleNamespace

import pytest

from dpc_client_core.llm_manager import PROVIDER_MAP
from dpc_client_core.providers import LlamaServerProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

GGUF = "D:/models/qwen3.8-27b-Q4_K_M.gguf"


def _provider(**overrides):
    config = {"type": "llamacpp_server", "gguf_path": GGUF}
    config.update(overrides)
    return LlamaServerProvider("local_qwen38", config)


class _FakeSupervisor:
    """The three things the provider asks of a supervisor, counted."""

    def __init__(self, port=8123):
        self.port = port
        self.props = {"total_slots": 4}
        self.ensure_calls = 0
        self.slot_enters = 0
        self.drained_with = None

    async def ensure_running(self):
        self.ensure_calls += 1
        return self.props

    def call_slot(self):
        self.slot_enters += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def drain(self, timeout=0.0):
        self.drained_with = timeout


class _FakeCompletions:
    def __init__(self, resp):
        self.resp = resp
        self.bodies = []

    async def create(self, **params):
        self.bodies.append(params)
        return self.resp


def _fake_client(resp):
    completions = _FakeCompletions(resp)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


def _chat_resp(content="ok", reasoning=None, tool_calls=None, usage=None):
    msg = SimpleNamespace(
        content=content, reasoning_content=reasoning, tool_calls=tool_calls
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=usage or SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


class TestConstruction:
    def test_a_gguf_path_is_required_and_a_missing_one_is_a_valueerror(self):
        # The manager catches ValueError/KeyError on load, so a misconfigured
        # alias is skipped with a log line, not a dead backend.
        with pytest.raises(ValueError, match="gguf_path"):
            LlamaServerProvider("broken", {"type": "llamacpp_server"})

    def test_no_api_key_is_required(self):
        _provider()

    def test_the_type_is_in_the_provider_map(self):
        assert PROVIDER_MAP["llamacpp_server"] is LlamaServerProvider

    def test_the_provider_registers_its_supervisor_by_alias(self):
        p = _provider()
        assert _ACTIVE_SUPERVISORS["local_qwen38"] is p.supervisor

    def test_a_reload_with_unchanged_flags_adopts_the_live_child(self):
        first = _provider(n_ctx=131072)
        # Adoption is for a child that is actually serving; a supervisor that
        # never started has nothing to adopt and is rebuilt freely.
        first.supervisor.props = {"total_slots": 1}
        second = _provider(n_ctx=131072)
        assert second.supervisor is first.supervisor

    def test_a_reload_with_changed_flags_does_not_adopt(self):
        first = _provider(n_ctx=131072)
        first.supervisor.props = {"total_slots": 1}
        second = _provider(n_ctx=262144)
        assert second.supervisor is not first.supervisor
        assert _ACTIVE_SUPERVISORS["local_qwen38"] is second.supervisor


class TestTheTemplateDictionary:
    """The words travel as the template spelled them, or not at all."""

    def test_the_templates_own_words_go_verbatim(self):
        for word in ("low", "medium", "xhigh"):
            body = _provider()._build_extra_body(word)
            assert body == {"chat_template_kwargs": {"reasoning_effort": word}}

    def test_xhigh_survives_the_alias_config_untouched(self):
        # The shared normalizer folds xhigh into high for the other providers;
        # here that fold would delete the top of the model's ladder.
        p = _provider(reasoning_effort="xhigh")
        assert p._build_extra_body() == {"chat_template_kwargs": {"reasoning_effort": "xhigh"}}

    def test_the_fleet_words_land_on_xhigh(self):
        # Three levels on the model, five in our header: there is no
        # second-from-top to preserve, so both upper fleet words map up.
        p = _provider()
        for word in ("high", "max"):
            assert p._build_extra_body(word) == {"chat_template_kwargs": {"reasoning_effort": "xhigh"}}

    def test_off_becomes_the_enable_thinking_switch(self):
        # Design-pinned until the cheap ADR-040 probe runs: the template's off
        # path is enable_thinking, not an effort word.
        p = _provider()
        assert p._build_extra_body("off") == {"chat_template_kwargs": {"enable_thinking": False}}

    def test_an_unknown_word_sends_nothing_and_the_template_default_applies(self):
        p = _provider()
        assert p._build_extra_body("ultra") == {}

    def test_nobody_saying_anything_sends_nothing(self):
        assert _provider()._build_extra_body() == {}

    def test_a_per_call_word_beats_the_alias_config(self):
        p = _provider(reasoning_effort="xhigh")
        assert p._build_extra_body("low") == {"chat_template_kwargs": {"reasoning_effort": "low"}}


class TestTheBudget:
    """reasoning_budget_tokens: per-request, over the alias config, capping
    the default thinking too — the 233K empty-answer failure mode."""

    def test_an_alias_budget_rides_top_level_alongside_the_effort(self):
        p = _provider(reasoning_budget_tokens=8000)
        assert p._build_extra_body("medium") == {
            "chat_template_kwargs": {"reasoning_effort": "medium"},
            "reasoning_budget_tokens": 8000,
        }

    def test_a_per_request_budget_beats_the_alias_config(self):
        p = _provider(reasoning_budget_tokens=8000)
        assert p._build_extra_body("medium", reasoning_budget_tokens=4000)["reasoning_budget_tokens"] == 4000

    def test_the_budget_caps_the_template_default_too(self):
        # No effort word anywhere: the template still thinks at its own
        # default (xhigh), and that is the strongest path — it must be the
        # capped one, not the exempt one.
        p = _provider(reasoning_budget_tokens=8000)
        assert p._build_extra_body() == {"reasoning_budget_tokens": 8000}

    def test_off_carries_no_budget(self):
        p = _provider(reasoning_budget_tokens=8000)
        assert p._build_extra_body("off") == {"chat_template_kwargs": {"enable_thinking": False}}


class TestSamplingAndLabels:
    def test_temperature_is_sent_even_while_thinking(self):
        # Unlike DeepSeek's inert-while-thinking dial, this server honours it
        # (every 2026-08-19 probe ran temp 0 with thinking on).
        body = _provider(temperature=0.0)._build_extra_body("xhigh")
        params = _provider(temperature=0.0)._sampling_params()
        assert "temperature" in params
        assert body["chat_template_kwargs"]["reasoning_effort"] == "xhigh"

    def test_top_p_only_when_configured(self):
        assert "top_p" not in _provider()._sampling_params()
        assert _provider(top_p=0.95)._sampling_params()["top_p"] == 0.95

    def test_the_effort_label_reads_the_built_body(self):
        p = _provider()
        assert p._effort_label("off", p._build_extra_body("off")) == "off"
        assert p._effort_label("high", p._build_extra_body("high")) == "xhigh"
        assert p._effort_label(None, p._build_extra_body()) == "server-default"


class TestRetryClassification:
    def test_a_template_refusal_is_not_retryable(self):
        # HTTP 500 with a deterministic jinja raise: retrying burns the whole
        # backoff budget to hear the same refusal.
        err = Exception("500 Internal Server Error: raise_exception Invalid reasoning effort")
        assert not LlamaServerProvider._is_retryable(err)

    def test_connection_class_failures_stay_retryable(self):
        assert LlamaServerProvider._is_retryable(Exception("connection error while calling localhost"))
        assert LlamaServerProvider._is_retryable(Exception("request timed out"))


class TestTheCallPath:
    @pytest.mark.asyncio
    async def test_generate_response_ensures_the_server_and_records_usage(self):
        p = _provider(reasoning_effort="high")
        sup = _FakeSupervisor()
        p.supervisor = sup
        client, completions = _fake_client(_chat_resp(content="hi", reasoning="thought"))
        async def _ensure():
            return client
        p._ensure = _ensure

        out = await p.generate_response("hello", conversation_id="c1")

        assert out == "hi"
        assert sup.ensure_calls == 1 or sup.slot_enters == 1
        body = completions.bodies[0]
        assert body["extra_body"] == {"chat_template_kwargs": {"reasoning_effort": "xhigh"}}
        assert body["model"].endswith("Q4_K_M")
        assert p.get_last_thinking() == "thought"
        usage = p.get_last_usage()
        assert usage["prompt_tokens"] == 11 and usage["completion_tokens"] == 7

    @pytest.mark.asyncio
    async def test_generate_response_carries_a_per_request_budget(self):
        p = _provider()
        p.supervisor = _FakeSupervisor()
        client, completions = _fake_client(_chat_resp())
        async def _ensure():
            return client
        p._ensure = _ensure

        await p.generate_response("hello", reasoning_budget_tokens=2048)
        assert completions.bodies[0]["extra_body"]["reasoning_budget_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_generate_with_tools_parses_tool_calls_and_skips_cot_padding(self):
        p = _provider()
        p.supervisor = _FakeSupervisor()
        tc = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="read_file", arguments=json.dumps({"path": "a.md"})),
        )
        client, completions = _fake_client(_chat_resp(content="", tool_calls=[tc]))
        async def _ensure():
            return client
        p._ensure = _ensure

        result = await p.generate_with_tools(
            [{"role": "user", "content": [{"type": "text", "text": "read it"}]}],
            [{"name": "read_file", "description": "", "input_schema": {"type": "object"}}],
            system="be brief",
        )

        assert result["tool_calls_raw"][0].name == "read_file"
        assert result["tool_calls_raw"][0].input == {"path": "a.md"}
        sent = completions.bodies[0]["messages"]
        # The qwen template needs no reasoning_content replay — unlike
        # DeepSeek's HTTP-400 rule, an assistant tool-call message goes out
        # bare.
        assert all("reasoning_content" not in m for m in sent)
        assert sent[0]["role"] == "system" and sent[0]["content"] == "be brief"

    @pytest.mark.asyncio
    async def test_close_drains_the_child(self):
        p = _provider()
        sup = _FakeSupervisor()
        p.supervisor = sup
        # The registry must point at the supervisor being closed for the
        # deregistration to fire — as it does when nothing swapped it since
        # construction.
        _ACTIVE_SUPERVISORS["local_qwen38"] = sup
        await p.close()
        assert sup.drained_with is not None
        assert "local_qwen38" not in _ACTIVE_SUPERVISORS
