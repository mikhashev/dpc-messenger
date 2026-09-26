"""Tests for NeuralDeepProvider — the NeuralDeep gateway over the OpenAI SDK.

The response shapes here are the ones the 2026-09-26 probe recorded against
api.neuraldeep.ru: reasoning in `reasoning_content`, usage on the last stream
chunk, `reasoning_tokens` sometimes null and once above `completion_tokens`,
an answer that starts with "\\n\\n", and thinking that only stops on the
`-noreason` model. No network."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.llm_manager import PROVIDER_MAP
from dpc_client_core.node_ledger import OUTPUT_INCLUDES_THINKING
from dpc_client_core.providers.base import reasoning_word_for
from dpc_client_core.providers.neuraldeep_provider import (
    NEURALDEEP_DEFAULT_BASE_URL,
    NeuralDeepProvider,
)


def _make(config=None):
    cfg = {"api_key": "test-key", "model": "qwen3.8-27b"}
    if config:
        cfg.update(config)
    return NeuralDeepProvider("nd_test", cfg)


def _usage(prompt=40, completion=63, reasoning=57, cached=0, details=True):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning) if details else None,
    )


def _response(content="\n\nPong", reasoning="thinking...", tool_calls=None, usage=None):
    msg = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage or _usage())


def _mock_create(provider, result):
    provider.client.chat.completions.create = AsyncMock(return_value=result)
    return provider.client.chat.completions.create


# --- registration and construction -------------------------------------------


def test_registered_with_default_endpoint_and_key_env(monkeypatch):
    assert PROVIDER_MAP["neuraldeep"] is NeuralDeepProvider
    monkeypatch.setenv("NEURALDEEP_API_KEY", "from-env")
    p = NeuralDeepProvider("nd", {"model": "qwen3.8-27b"})
    assert str(p.client.base_url).rstrip("/") == NEURALDEEP_DEFAULT_BASE_URL
    assert p._api_key == "from-env"


def test_missing_key_is_refused(monkeypatch):
    monkeypatch.delenv("NEURALDEEP_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key not found"):
        NeuralDeepProvider("nd", {"model": "qwen3.8-27b"})


def test_vision_and_thinking_follow_the_model_id():
    assert _make().supports_vision() and _make().supports_thinking()
    assert _make({"model": "qwen3.6-35b-a3b-noreason"}).supports_vision()
    assert not _make({"model": "qwen3.6-35b-a3b-noreason"}).supports_thinking()
    assert not _make({"model": "gpt-oss-120b"}).supports_vision()
    assert _make({"model": "gpt-oss-120b"}).supports_thinking()
    assert not _make({"model": "e5-large"}).supports_thinking()


def test_the_declared_convention_is_a_ledger_word():
    assert NeuralDeepProvider.DECLARED_OUTPUT_INCLUDES_THINKING == "includes"
    assert NeuralDeepProvider.DECLARED_OUTPUT_INCLUDES_THINKING in OUTPUT_INCLUDES_THINKING


# --- thinking off is a model switch ----------------------------------------


def test_off_switches_to_the_noreason_twin():
    p = _make()
    assert p._request_params("off")["model"] == "qwen3.8-27b-noreason"
    assert p._request_params(None)["model"] == "qwen3.8-27b"
    assert "chat_template_kwargs" not in json.dumps(p._request_params("off"))


def test_an_alias_configured_not_to_think_uses_the_twin():
    p = _make({"model": "qwen3.6-fp8", "thinking": {"enabled": False}})
    assert p._request_params()["model"] == "qwen3.6-fp8-noreason"
    assert p._effort_word() == "off"


def test_off_is_not_offered_where_no_twin_exists():
    p = _make({"model": "gpt-oss-120b"})
    assert "off" not in p.reasoning_words_served()
    assert reasoning_word_for(p, "off") is None
    assert p._request_params("off")["model"] == "gpt-oss-120b"


def test_effort_words_map_onto_each_models_own_scale():
    qwen = _make()
    # qwen3.8-27b does not know `high` and would fall back to `low`.
    assert qwen._request_params("high")["extra_body"] == {"reasoning_effort": "xhigh"}
    assert qwen._request_params("medium")["extra_body"] == {"reasoning_effort": "medium"}
    oss = _make({"model": "gpt-oss-120b"})
    assert oss._request_params("max")["extra_body"] == {"reasoning_effort": "high"}
    # qwen3.6 has no effort levels: nothing is sent, only `off` is served.
    q36 = _make({"model": "qwen3.6-35b-a3b"})
    assert "extra_body" not in q36._request_params("high")
    assert q36.reasoning_words_served() == ["off"]
    assert _make({"model": "qwen3.8-27b-noreason"}).reasoning_words_served() == []


# --- plain path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_strips_the_leading_newlines_and_keeps_the_reasoning():
    p = _make()
    create = _mock_create(p, _response())
    assert await p.generate_response("ping") == "Pong"
    assert p.get_last_thinking() == "thinking..."
    usage = p.get_last_usage()
    assert usage["reasoning_tokens"] == 57 and usage["content_tokens"] == 6
    assert usage["output_includes_thinking"] == "includes"
    assert create.call_args.kwargs["model"] == "qwen3.8-27b"


@pytest.mark.asyncio
async def test_null_reasoning_tokens_are_absent_not_zero():
    p = _make({"model": "qwen3.6-35b-a3b"})
    _mock_create(p, _response(usage=_usage(reasoning=None)))
    await p.generate_response("ping")
    usage = p.get_last_usage()
    assert "reasoning_tokens" not in usage and "thinking_source" not in usage
    assert usage["completion_tokens"] == 63


@pytest.mark.asyncio
async def test_reasoning_above_completion_is_clamped():
    p = _make({"model": "qwen3.6-35b-a3b"})
    _mock_create(p, _response(usage=_usage(completion=296, reasoning=297)))
    await p.generate_response("ping")
    usage = p.get_last_usage()
    assert usage["reasoning_tokens"] == 296 and usage["content_tokens"] == 0


@pytest.mark.asyncio
async def test_plain_off_reaches_the_wire_as_the_twin_and_is_recorded_as_off():
    p = _make()
    create = _mock_create(p, _response(reasoning=None, usage=_usage(completion=5, reasoning=0)))
    await p.generate_response("ping", reasoning_effort="off")
    assert create.call_args.kwargs["model"] == "qwen3.8-27b-noreason"
    assert p.get_last_usage()["served_effort"] == "off"


# --- stream ------------------------------------------------------------------


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _delta(content=None, reasoning=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, reasoning_content=reasoning))],
        usage=None,
    )


@pytest.mark.asyncio
async def test_stream_collects_reasoning_strips_the_answer_and_reads_the_last_chunk():
    p = _make()
    chunks = [
        _delta(reasoning="Let me "), _delta(reasoning="think."),
        _delta(content="\n\n"), _delta(content="Po"), _delta(content="ng"),
        SimpleNamespace(choices=[], usage=_usage(prompt=12, completion=20, reasoning=16)),
    ]
    create = _mock_create(p, _Stream(chunks))
    seen = []

    async def on_chunk(text, conv):
        seen.append(text)

    assert await p.generate_response_stream("ping", on_chunk, "c1") == "Pong"
    assert seen == ["Po", "ng"]
    assert p.get_last_thinking() == "Let me think."
    assert p.get_last_usage()["reasoning_tokens"] == 16
    assert create.call_args.kwargs["stream_options"] == {"include_usage": True}


# --- tools ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_round_trip_converts_both_ways():
    p = _make()
    call = SimpleNamespace(id="chatcmpl-tool-1",
                           function=SimpleNamespace(name="get_weather", arguments='{"city": "Moscow"}'))
    create = _mock_create(p, _response(content="\n\n", tool_calls=[call]))
    messages = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t0", "name": "get_weather", "input": {"city": "Omsk"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t0", "content": "cold"}]},
    ]
    tools = [{"name": "get_weather", "description": "Weather",
              "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]
    out = await p.generate_with_tools(messages, tools, system="be brief")

    sent = create.call_args.kwargs
    assert sent["tools"][0]["function"]["name"] == "get_weather"
    assert sent["tool_choice"] == "auto"
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert sent["messages"][2]["tool_calls"][0]["id"] == "t0"
    assert sent["messages"][3]["tool_call_id"] == "t0"

    assert out["content"] == ""
    raw = out["tool_calls_raw"][0]
    assert (raw.id, raw.name, raw.input) == ("chatcmpl-tool-1", "get_weather", {"city": "Moscow"})
    assert out["usage"]["cost_currency"] == "RUB"


# --- vision ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vision_sends_one_data_url():
    p = _make()
    create = _mock_create(p, _response(content="\n\nDPC 42", reasoning=None,
                                       usage=_usage(reasoning=None)))
    text = await p.generate_with_vision("read it", [{"base64": "QUJD", "mime_type": "image/png"}])
    assert text == "DPC 42"
    parts = create.call_args.kwargs["messages"][0]["content"]
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}


@pytest.mark.asyncio
async def test_vision_refuses_more_than_one_image_and_non_vision_models():
    with pytest.raises(ValueError, match="one image"):
        await _make().generate_with_vision("x", [{"base64": "QQ=="}, {"base64": "QQ=="}])
    with pytest.raises(ValueError, match="no vision path"):
        await _make({"model": "gpt-oss-120b"}).generate_with_vision("x", [{"base64": "QQ=="}])


# --- cost in roubles, never in dollars -------------------------------------


@pytest.mark.asyncio
async def test_usage_carries_roubles_with_their_currency_and_no_dollar_cost():
    p = _make()
    _mock_create(p, _response(usage=_usage(prompt=1_000_000, completion=1_000_000,
                                           reasoning=10, cached=500_000)))
    await p.generate_response("ping")
    usage = p.get_last_usage()
    assert usage["cost_currency"] == "RUB"
    # 500k cached * 2.448 + 500k uncached * 24.48 + 1M out * 122.4, per 1M
    assert usage["cost_amount"] == pytest.approx(1.224 + 12.24 + 122.4)
    assert "cost" not in usage


@pytest.mark.asyncio
async def test_an_unpriced_model_records_no_amount():
    p = _make({"model": "some-new-model"})
    _mock_create(p, _response())
    await p.generate_response("ping")
    usage = p.get_last_usage()
    assert "cost_amount" not in usage and "cost_currency" not in usage


# --- balance -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_balance_reads_the_wallet_from_limits(monkeypatch):
    import httpx

    payload = {"key": {"status": "ok", "billing_mode": "subscription"},
               "decision": {"can_request": True},
               "wallet": {"balance_rub": 500.0, "spent_rub_30d": 0.0}}
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    p = _make()
    assert p.supports_balance()
    balance = await p.get_balance()
    assert seen["url"] == "https://api.neuraldeep.ru/v1/limits"
    assert seen["auth"] == "Bearer test-key"
    assert balance["balance_infos"] == [{"currency": "RUB", "total_balance": "500.00"}]
    assert balance["is_available"] is True
    assert balance["billing_mode"] == "subscription"
