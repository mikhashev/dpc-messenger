"""Tests for NeuralDeepProvider — the NeuralDeep gateway over the OpenAI SDK.

The response shapes here are the ones the 2026-09-26 probe recorded against
api.neuraldeep.ru: reasoning in `reasoning_content`, usage on the last stream
chunk, `reasoning_tokens` sometimes null and once above `completion_tokens`,
an answer that starts with "\\n\\n", and thinking that only stops on the
`-noreason` model. No network: the price list and /limits are stubbed."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from dpc_client_core.llm_manager import PROVIDER_MAP
from dpc_client_core.node_ledger import OUTPUT_INCLUDES_THINKING
from dpc_client_core.providers.base import reasoning_word_for
from dpc_client_core.providers import neuraldeep_prices
from dpc_client_core.providers.neuraldeep_prices import NeuralDeepPriceSource
from dpc_client_core.providers.neuraldeep_provider import (
    COST_BASIS_CHARGED,
    COST_BASIS_LIST_PRICE_REFERENCE,
    COST_BASIS_UNKNOWN,
    NEURALDEEP_DEFAULT_BASE_URL,
    NeuralDeepProvider,
)

_REAL_GET_BALANCE = NeuralDeepProvider.get_balance
LIST_AT = datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc)
PRICES = {"prices": [
    {"model": "qwen3.8-27b", "billing": "token", "in_rub_1m": 24.48,
     "out_rub_1m": 122.4, "cached_in_rub_1m": 2.448},
    {"model": "qwen3.8-27b-noreason", "billing": "token", "in_rub_1m": 24.48,
     "out_rub_1m": 122.4, "cached_in_rub_1m": 2.448},
    {"model": "qwen3.6-35b-a3b", "billing": "token", "in_rub_1m": 7.14,
     "out_rub_1m": 40.8, "cached_in_rub_1m": 0.714},
    {"model": "whisper-1", "billing": "minute", "in_rub_1m": 0.0,
     "out_rub_1m": 0.0, "cached_in_rub_1m": None, "rub_per_min": 102.0},
]}


class _Fetch:
    def __init__(self, result=PRICES):
        self.result, self.calls = result, 0

    async def __call__(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    """A price list served from a stub and a wallet key, per test."""
    monkeypatch.setenv("DPC_HOME", str(tmp_path))
    fetch = _Fetch()
    neuraldeep_prices.reset_price_source(NeuralDeepPriceSource(fetch=fetch, clock=lambda: LIST_AT))
    balance = AsyncMock(return_value={"billing_mode": "wallet"})
    monkeypatch.setattr(NeuralDeepProvider, "get_balance", balance)
    yield SimpleNamespace(fetch=fetch, balance=balance)
    neuraldeep_prices.reset_price_source()


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
    assert usage["cost_price_list_at"] == LIST_AT.isoformat()
    assert usage["cost_basis"] == COST_BASIS_CHARGED
    assert "cost" not in usage


@pytest.mark.asyncio
async def test_the_noreason_twin_is_priced_by_its_own_row():
    p = _make()
    _mock_create(p, _response(usage=_usage(prompt=0, completion=1_000_000, reasoning=0)))
    await p.generate_response("ping", reasoning_effort="off")
    assert p.get_last_usage()["cost_amount"] == pytest.approx(122.4)


@pytest.mark.asyncio
async def test_a_model_missing_from_the_list_is_unknown_with_the_reason():
    p = _make({"model": "some-new-model"})
    _mock_create(p, _response())
    await p.generate_response("ping")
    usage = p.get_last_usage()
    assert usage["cost_amount"] is None and "cost_currency" not in usage
    assert "not in the price list" in usage["cost_unpriced_reason"]
    assert usage["cost_price_list_at"] == LIST_AT.isoformat()


@pytest.mark.asyncio
async def test_a_row_without_token_prices_is_unknown_not_free():
    p = _make({"model": "whisper-1"})
    _mock_create(p, _response())
    await p.generate_response("ping")
    usage = p.get_last_usage()
    assert usage["cost_amount"] is None
    assert "billing='minute'" in usage["cost_unpriced_reason"]


@pytest.mark.asyncio
async def test_no_list_and_no_cache_is_unknown_and_the_call_still_answers(_offline):
    _offline.fetch.result = httpx.ConnectError("offline")
    p = _make()
    _mock_create(p, _response())
    assert await p.generate_response("ping") == "Pong"
    usage = p.get_last_usage()
    assert usage["cost_amount"] is None and "cost_currency" not in usage
    assert "fetch failed and nothing cached" in usage["cost_unpriced_reason"]


@pytest.mark.asyncio
async def test_a_pricing_crash_never_fails_the_call(monkeypatch):
    async def boom(self):
        raise RuntimeError("bug")
    monkeypatch.setattr(NeuralDeepPriceSource, "current", boom)
    p = _make()
    create = _mock_create(p, _response())
    assert await p.generate_response("ping") == "Pong"
    assert create.await_count == 1  # not retried as if the vendor had failed
    assert p.get_last_usage()["cost_unpriced_reason"] == "pricing failed: RuntimeError"


@pytest.mark.asyncio
async def test_a_subscription_key_records_a_list_price_reference(_offline):
    _offline.balance.return_value = {"billing_mode": "subscription"}
    p = _make()
    _mock_create(p, _response())
    await p.generate_response("ping")
    await p.generate_response("ping")
    usage = p.get_last_usage()
    assert usage["cost_basis"] == COST_BASIS_LIST_PRICE_REFERENCE
    assert usage["cost_amount"] > 0 and usage["cost_currency"] == "RUB"
    assert _offline.balance.await_count == 1  # /limits once a day, not per call
    assert _offline.fetch.calls == 1


@pytest.mark.asyncio
async def test_an_unreadable_limits_leaves_the_basis_unknown(_offline):
    _offline.balance.side_effect = httpx.ConnectError("offline")
    p = _make()
    _mock_create(p, _response())
    await p.generate_response("ping")
    await p.generate_response("ping")
    assert p.get_last_usage()["cost_basis"] == COST_BASIS_UNKNOWN
    assert _offline.balance.await_count == 1


# --- regression, 1a2f0ad8: get_balance() failing no longer raises, so a
# failed read must be read off `error`, not off an exception, or the last
# known billing_mode is lost for a full day (REFRESH_INTERVAL) on one bad read.


@pytest.mark.asyncio
async def test_a_transient_limits_failure_keeps_the_known_billing_mode(monkeypatch):
    monkeypatch.setattr(NeuralDeepProvider, "get_balance", _REAL_GET_BALANCE)
    p = _make()
    p._billing_mode = "subscription"
    p._billing_mode_read_at = datetime.now(timezone.utc) - timedelta(hours=25)  # past REFRESH_INTERVAL

    def handler(request):
        return httpx.Response(503, json={"detail": "busy"})  # no Retry-After, no cache to fall back on

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    mode = await p._billing_mode_now()
    assert mode == "subscription"
    assert p._billing_mode_failed_at is not None

    _mock_create(p, _response())
    await p.generate_response("ping")
    assert p.get_last_usage()["cost_basis"] == COST_BASIS_LIST_PRICE_REFERENCE


@pytest.mark.asyncio
async def test_a_plain_failure_after_a_403_clears_the_key_blocked_kind(monkeypatch):
    """A 403 sets `_limits_backoff_kind` to ERROR_KEY_BLOCKED. Once that
    backoff has expired, a later non-2xx failure that is NOT a 403 — here a
    plain 500 with no Retry-After header — must clear the stale kind too, or
    the next in-backoff raise (line ~504) mislabels an ordinary limits
    failure as "key blocked" forever, since nothing else resets it absent a
    Retry-After header."""
    from dpc_client_core.providers.neuraldeep_provider import NeuralDeepKeyBlocked

    monkeypatch.setattr(NeuralDeepProvider, "get_balance", _REAL_GET_BALANCE)
    p = _make()

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(
                            transport=httpx.MockTransport(lambda request: httpx.Response(403)), **kw))
    with pytest.raises(NeuralDeepKeyBlocked):
        await p._fetch_limits()
    assert p._limits_backoff_kind == "key_blocked"

    # The 403 backoff has not naturally expired — force past it, the way a
    # later call after the window would find it.
    p._limits_backoff_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(
                            transport=httpx.MockTransport(lambda request: httpx.Response(500)), **kw))
    with pytest.raises(Exception):
        await p._fetch_limits()

    assert p._limits_backoff_kind is None


@pytest.mark.asyncio
async def test_an_invalid_schema_read_also_keeps_the_known_billing_mode(monkeypatch):
    monkeypatch.setattr(NeuralDeepProvider, "get_balance", _REAL_GET_BALANCE)
    p = _make()
    p._billing_mode = "subscription"
    p._billing_mode_read_at = datetime.now(timezone.utc) - timedelta(hours=25)

    fixture_path = Path(__file__).parent / "fixtures" / "nd_limits_2026-09-28.json"
    payload = {**json.loads(fixture_path.read_text(encoding="utf-8")), "schema": 2}

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(
                            lambda request: httpx.Response(200, json=payload)), **kw))

    mode = await p._billing_mode_now()
    assert mode == "subscription"
    assert p._billing_mode_failed_at is not None

    _mock_create(p, _response())
    await p.generate_response("ping")
    assert p.get_last_usage()["cost_basis"] == COST_BASIS_LIST_PRICE_REFERENCE


# --- balance -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_balance_reads_the_wallet_from_limits(monkeypatch):
    import httpx

    payload = {"schema": 1, "tier": "free", "observed_at": "2026-09-28T10:41:49Z",
               "key": {"status": "ok", "billing_mode": "subscription"},
               "decision": {"can_request": True}, "chat": {},
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
    balance = await _REAL_GET_BALANCE(p)
    assert seen["url"] == "https://api.neuraldeep.ru/v1/limits"
    assert seen["auth"] == "Bearer test-key"
    assert balance["balance_infos"] == [
        {"currency": "RUB", "total_balance": "500.00", "spent_30d": "0.00"},
    ]
    assert balance["is_available"] is True
    assert balance["billing_mode"] == "subscription"
    assert balance["quota"]["billing_mode"] == "subscription"
    assert balance["quota"]["can_request"] is True
    assert balance["quota"]["windows"] == []  # payload carries no `chat` block


@pytest.mark.asyncio
async def test_balance_normalizes_the_real_limits_payload_into_a_quota_block(monkeypatch):
    """A-VENDOR-KEYS-QUOTA-WINDOWS-ARE-READ-AND-NEVER-SHOWN: the fixture is a
    real /v1/limits response (captured 2026-09-28, no secrets)."""
    fixture_path = Path(__file__).parent / "fixtures" / "nd_limits_2026-09-28.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    def handler(request):
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    p = _make()
    balance = await _REAL_GET_BALANCE(p)

    quota = balance["quota"]
    assert quota["tier"] == "free"
    assert quota["billing_mode"] == "subscription"
    assert quota["can_request"] is True
    assert quota["blockers"] == []
    assert quota["parallel_limit"] == 3
    # P1b: the vendor spells the week window "iso-week"; normalized to "week"
    # here (guest_vendor_quota's `_WINDOW_LENGTHS` only knows "3h"/"week"), the
    # raw wire word kept beside it as `vendor_window`.
    assert quota["windows"] == [
        {"name": "3h", "unit": "requests", "used": 14, "limit": 400,
         "remaining": 386, "resets_at": "2026-09-28T11:59:59Z", "reset_in_sec": 4690,
         "vendor_window": "3h"},
        {"name": "week", "unit": "requests", "used": 14, "limit": 2000,
         "remaining": 1986, "resets_at": "2026-10-05T00:00:00Z", "reset_in_sec": 566291,
         "vendor_window": "iso-week"},
        {"name": "minute", "unit": "requests", "used": 0, "limit": 20,
         "remaining": 20, "resets_at": None, "reset_in_sec": 11, "vendor_window": None},
    ]
    assert quota["observed_at"] == "2026-09-28T10:41:49Z"
    assert quota["daily_capacity"] == {
        "pct_used": 0.1, "exhausted": False, "resets_at": "2026-09-29T00:00:00+00:00",
    }
    assert quota["night"] == {
        "active": False, "capacity_factor": 2, "window_start_msk": 0, "window_end_msk": 6,
    }
    assert balance["balance_infos"] == [
        {"currency": "RUB", "total_balance": "500.00", "spent_30d": "0.00"},
    ]


@pytest.mark.asyncio
async def test_balance_quota_daily_capacity_and_night_are_none_safe(monkeypatch):
    """Absent or malformed daily_capacity/night blocks must not crash the
    quota normalization — they just read as None."""
    payload = {"schema": 1, "tier": "free",
               "key": {"status": "ok", "billing_mode": "subscription"},
               "decision": {"can_request": True}, "chat": {},
               "daily_capacity": "not-a-dict", "night": None}

    def handler(request):
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    p = _make()
    balance = await _REAL_GET_BALANCE(p)
    assert balance["quota"]["daily_capacity"] is None
    assert balance["quota"]["night"] is None


@pytest.mark.asyncio
async def test_balance_quota_survives_a_payload_with_no_chat_block(monkeypatch):
    """A schema-1 payload whose `chat` block is empty (a key with no volume
    windows configured) must not crash the normalization — windows come back
    empty, the rest of the decision still reads through."""
    payload = {"schema": 1, "tier": "free", "observed_at": "2026-09-28T10:41:49Z",
               "key": {"status": "ok", "billing_mode": "subscription"},
               "decision": {"can_request": False, "blockers": ["out_of_quota"]},
               "chat": {}}

    def handler(request):
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    p = _make()
    balance = await _REAL_GET_BALANCE(p)

    assert balance["quota"]["windows"] == []
    assert balance["quota"]["can_request"] is False
    assert balance["quota"]["blockers"] == ["out_of_quota"]
    assert balance["quota"]["parallel_limit"] is None
    assert balance["balance_infos"] == []


# --- /v1/limits reading policy: TTL, floor, schema validation, 401/403, backoff --


def _fixture_payload():
    fixture_path = Path(__file__).parent / "fixtures" / "nd_limits_2026-09-28.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def _patch_httpx(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.asyncio
async def test_two_reads_inside_the_ttl_make_one_http_request(monkeypatch):
    payload = _fixture_payload()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=payload)

    _patch_httpx(monkeypatch, handler)
    p = _make()
    first = await _REAL_GET_BALANCE(p)
    second = await _REAL_GET_BALANCE(p)  # a moment later, well within the 20s TTL
    assert calls["n"] == 1
    assert first["quota"]["tier"] == second["quota"]["tier"] == "free"


@pytest.mark.asyncio
async def test_a_read_past_the_ttl_but_inside_the_floor_still_serves_the_cache(monkeypatch):
    payload = _fixture_payload()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=payload)

    _patch_httpx(monkeypatch, handler)
    p = _make()
    await _REAL_GET_BALANCE(p)
    assert calls["n"] == 1
    # The cache looks stale (past the 20s TTL) but the one attempt so far is
    # still inside the 15s floor: the floor wins, no second request.
    p._limits_cache_at -= timedelta(seconds=25)
    balance = await _REAL_GET_BALANCE(p)
    assert calls["n"] == 1
    assert balance["quota"]["tier"] == "free"


@pytest.mark.asyncio
async def test_past_both_ttl_and_floor_fetches_again(monkeypatch):
    payload = _fixture_payload()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=payload)

    _patch_httpx(monkeypatch, handler)
    p = _make()
    await _REAL_GET_BALANCE(p)
    p._limits_cache_at -= timedelta(seconds=25)
    p._limits_last_attempt_at -= timedelta(seconds=25)
    await _REAL_GET_BALANCE(p)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_a_dropped_key_billing_mode_is_reported_as_invalid(monkeypatch):
    """A5: a vendor payload missing `key.billing_mode` must read as invalid,
    not silently erase the last known billing_mode (e273cb0b guarded the
    other side of this: a failed read keeps it; this guards the payload
    itself from looking like a valid read with nothing there)."""
    payload = {**_fixture_payload(), "key": {"status": "ok"}}
    _patch_httpx(monkeypatch, lambda request: httpx.Response(200, json=payload))
    p = _make()
    balance = await _REAL_GET_BALANCE(p)
    assert balance["error"] == "invalid"


@pytest.mark.asyncio
async def test_a_non_list_blocked_models_is_reported_as_invalid(monkeypatch):
    payload = {**_fixture_payload(), "blocked_models": {"not": "a list"}}
    _patch_httpx(monkeypatch, lambda request: httpx.Response(200, json=payload))
    p = _make()
    balance = await _REAL_GET_BALANCE(p)
    assert balance["error"] == "invalid"


@pytest.mark.asyncio
async def test_schema_mismatch_is_reported_as_invalid_without_crashing(monkeypatch):
    payload = {**_fixture_payload(), "schema": 2}
    _patch_httpx(monkeypatch, lambda request: httpx.Response(200, json=payload))
    p = _make()
    balance = await _REAL_GET_BALANCE(p)
    assert balance["error"] == "invalid"
    assert balance["quota"] is None
    assert balance["is_available"] is False


@pytest.mark.asyncio
async def test_a_401_rejects_the_key_and_the_next_read_makes_no_request(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"detail": "invalid api key"})

    _patch_httpx(monkeypatch, handler)
    p = _make()
    first = await _REAL_GET_BALANCE(p)
    assert first["error"] == "key_rejected"
    assert calls["n"] == 1
    second = await _REAL_GET_BALANCE(p)
    assert second["error"] == "key_rejected"
    assert calls["n"] == 1  # sticky: no second HTTP request


@pytest.mark.asyncio
async def test_a_403_is_reported_as_key_blocked_not_a_generic_error(monkeypatch):
    _patch_httpx(monkeypatch, lambda request: httpx.Response(403, json={"detail": "blocked"}))
    p = _make()
    balance = await _REAL_GET_BALANCE(p)
    assert balance["error"] == "key_blocked"


@pytest.mark.asyncio
async def test_a_403_backoff_with_no_cache_still_reports_key_blocked_on_a_second_read(monkeypatch):
    """A second get_balance() landing inside the 403 backoff window, with no
    cached payload to fall back on, must not collapse to the generic
    "unavailable" — it should still say key_blocked, the same kind the first
    call reported."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(403, json={"detail": "blocked"})

    _patch_httpx(monkeypatch, handler)
    p = _make()
    first = await _REAL_GET_BALANCE(p)
    assert first["error"] == "key_blocked"
    assert calls["n"] == 1
    # Still inside the 5-minute backoff: no second HTTP request, but the
    # error kind must still be key_blocked, not unavailable.
    second = await _REAL_GET_BALANCE(p)
    assert calls["n"] == 1
    assert second["error"] == "key_blocked"


@pytest.mark.asyncio
async def test_retry_after_is_honored_before_the_next_automatic_read(monkeypatch):
    payload = _fixture_payload()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "30"}, json={"detail": "busy"})
        return httpx.Response(200, json=payload)

    _patch_httpx(monkeypatch, handler)
    p = _make()
    first = await _REAL_GET_BALANCE(p)
    assert first["error"] == "unavailable"
    assert calls["n"] == 1
    # Still inside the 30s pause: no second request, still the same error.
    second = await _REAL_GET_BALANCE(p)
    assert calls["n"] == 1
    assert second["error"] == "unavailable"
    # Force the pause and the fetch floor to have both passed: the next
    # read fetches again.
    p._limits_backoff_until -= timedelta(seconds=31)
    p._limits_last_attempt_at -= timedelta(seconds=16)
    third = await _REAL_GET_BALANCE(p)
    assert calls["n"] == 2
    assert third["quota"]["tier"] == "free"


@pytest.mark.asyncio
async def test_retry_after_is_capped_at_five_minutes(monkeypatch):
    _patch_httpx(monkeypatch, lambda request: httpx.Response(
        503, headers={"Retry-After": "3600"}, json={"detail": "busy"}))
    p = _make()
    await _REAL_GET_BALANCE(p)
    # The attempt is stamped before the request and the backoff after the
    # response, so the gap is the cap plus the fetch time — a second covers it
    # while a 3600 s Retry-After left uncapped would still fail.
    assert p._limits_backoff_until - p._limits_last_attempt_at <= timedelta(minutes=5, seconds=1)


@pytest.mark.asyncio
async def test_concurrent_get_balance_calls_coalesce_into_one_http_request(monkeypatch):
    """Coddy's own join of parallel readers (internal/session/provider_usage.go
    :275-281, `inflight`): a burst of callers on one provider instance must
    cost exactly one HTTP request, all reading the same result."""
    payload = _fixture_payload()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    p = _make()
    results = await asyncio.gather(*[_REAL_GET_BALANCE(p) for _ in range(8)])
    assert calls["n"] == 1
    for r in results:
        assert r["quota"]["tier"] == "free"
        assert r["balance_infos"] == [{"currency": "RUB", "total_balance": "500.00", "spent_30d": "0.00"}]


@pytest.mark.asyncio
async def test_a_failed_coalesced_fetch_reaches_every_joiner(monkeypatch):
    """The exception, not just the success, reaches every joined caller."""
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return httpx.Response(403, json={"detail": "blocked"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    p = _make()
    results = await asyncio.gather(*[_REAL_GET_BALANCE(p) for _ in range(4)])
    assert calls["n"] == 1
    for r in results:
        assert r["error"] == "key_blocked"


# --- blocked_models: a model-level gate, decision.can_request stays true -----


def _payload_with_blocked_model(model="qwen3.8-27b-noreason", blocker="model_cap_blocked"):
    payload = _fixture_payload()
    payload["blocked_models"] = [
        {"model": model, "blocker": blocker,
         "resets_at": "2026-09-28T12:00:00Z", "reset_in_sec": 900},
    ]
    return payload


@pytest.mark.asyncio
async def test_blocked_models_is_parsed_into_the_quota_block(monkeypatch):
    _patch_httpx(monkeypatch, lambda request: httpx.Response(200, json=_payload_with_blocked_model()))
    p = _make()
    balance = await _REAL_GET_BALANCE(p)
    assert balance["quota"]["can_request"] is True  # the account itself is fine
    assert balance["quota"]["blocked_models"] == [
        {"model": "qwen3.8-27b-noreason", "blocker": "model_cap_blocked",
         "resets_at": "2026-09-28T12:00:00Z", "reset_in_sec": 900},
    ]


@pytest.mark.asyncio
async def test_blocked_models_survives_a_malformed_entry(monkeypatch):
    payload = _fixture_payload()
    payload["blocked_models"] = [{"blocker": "x"}, "not-a-dict", {"model": "  "}]
    _patch_httpx(monkeypatch, lambda request: httpx.Response(200, json=payload))
    p = _make()
    balance = await _REAL_GET_BALANCE(p)
    assert balance["quota"]["blocked_models"] == []


@pytest.mark.asyncio
async def test_model_blocked_matches_the_exact_wire_name_only(monkeypatch):
    """A block on the `-noreason` twin matches only that wire name — coddy's
    own `modelBlocked` does no base/twin normalization on the block itself."""
    _patch_httpx(monkeypatch, lambda request: httpx.Response(
        200, json=_payload_with_blocked_model(model="qwen3.8-27b-noreason")))
    p = _make()  # model: qwen3.8-27b
    await _REAL_GET_BALANCE(p)  # populates p._limits_cache

    assert p.model_blocked("qwen3.8-27b") is None  # base is not blocked
    entry = p.model_blocked("qwen3.8-27b-noreason")
    assert entry is not None and entry["blocker"] == "model_cap_blocked"
    assert p.model_blocked("QWEN3.8-27B-NOREASON") is not None  # case-insensitive


@pytest.mark.asyncio
async def test_model_blocked_takes_the_callers_own_wire_model(monkeypatch):
    """`model` is required — a caller passes its own `_wire_model(effort)`,
    not this alias's default, since `effort=off` runs the `-noreason` twin."""
    _patch_httpx(monkeypatch, lambda request: httpx.Response(
        200, json=_payload_with_blocked_model(model="qwen3.8-27b")))
    p = _make()
    await _REAL_GET_BALANCE(p)
    assert p.model_blocked(p._wire_model()) is not None
    assert p.model_blocked(p.model) is not None


def test_model_blocked_is_read_only_with_no_cache_yet():
    p = _make()
    assert p.model_blocked("anything") is None


# --- A1: a cancelled leader must not hang a joiner ---------------------------


@pytest.mark.asyncio
async def test_a_cancelled_leader_still_lets_a_later_read_complete(monkeypatch):
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        await asyncio.sleep(1)
        return httpx.Response(200, json=_fixture_payload())

    _patch_httpx(monkeypatch, handler)
    p = _make()

    leader = asyncio.ensure_future(_REAL_GET_BALANCE(p))
    await asyncio.sleep(0.01)  # let the leader start the HTTP call and register
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    # A follower that had joined the same in-flight future must not hang.
    result = await asyncio.wait_for(_REAL_GET_BALANCE(p), timeout=1)
    assert result["error"] in ("unavailable",)
    assert p._limits_inflight is None


@pytest.mark.asyncio
async def test_a_joiner_started_before_the_cancel_also_completes(monkeypatch):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=_fixture_payload())

    _patch_httpx(monkeypatch, handler)
    p = _make()

    leader = asyncio.ensure_future(_REAL_GET_BALANCE(p))
    await asyncio.sleep(0.01)
    joiner = asyncio.ensure_future(_REAL_GET_BALANCE(p))
    await asyncio.sleep(0.01)
    leader.cancel()

    with pytest.raises(asyncio.CancelledError):
        await leader
    joined = await asyncio.wait_for(joiner, timeout=1)
    assert joined["error"] == "unavailable"


# --- A2: null session/week/rpm blocks must not crash -------------------------


@pytest.mark.asyncio
async def test_null_session_week_rpm_blocks_do_not_crash(monkeypatch):
    payload = {**_fixture_payload(),
               "chat": {"session": None, "week": None, "rpm": None}}
    _patch_httpx(monkeypatch, lambda request: httpx.Response(200, json=payload))
    p = _make()
    balance = await _REAL_GET_BALANCE(p)
    assert balance["quota"]["windows"] == []


# --- A3: the fetch floor holds after a failed fetch with no cache -----------


@pytest.mark.asyncio
async def test_the_floor_holds_after_a_failed_fetch_with_no_cache(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={"detail": "busy"})  # no Retry-After

    _patch_httpx(monkeypatch, handler)
    p = _make()
    first = await _REAL_GET_BALANCE(p)
    assert first["error"] == "unavailable"
    assert calls["n"] == 1
    # A moment later, still well inside the 15s floor: no second request.
    second = await _REAL_GET_BALANCE(p)
    assert calls["n"] == 1
    assert second["error"] == "unavailable"


# --- A4: the TTL clock starts at response arrival, not at fetch start --------


@pytest.mark.asyncio
async def test_the_cache_timestamp_is_the_arrival_time_not_the_start_time(monkeypatch):
    payload = _fixture_payload()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        await asyncio.sleep(0.2)  # a slow response
        return httpx.Response(200, json=payload)

    _patch_httpx(monkeypatch, handler)
    p = _make()
    before = datetime.now(timezone.utc)
    await _REAL_GET_BALANCE(p)
    after = datetime.now(timezone.utc)
    assert p._limits_cache_at is not None
    assert before <= p._limits_cache_at <= after
    assert p._limits_cache_at - before >= timedelta(seconds=0.15)


# --- A6: 403 backs off instead of being polled on every automatic read ------


@pytest.mark.asyncio
async def test_403_backs_off_like_other_non_2xx_answers(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(403, json={"detail": "blocked"})

    _patch_httpx(monkeypatch, handler)
    p = _make()
    first = await _REAL_GET_BALANCE(p)
    assert first["error"] == "key_blocked"
    assert calls["n"] == 1
    assert p._limits_backoff_until is not None
    # Still inside the backoff: the next automatic read makes no request,
    # and still reports key_blocked — the same kind the first 403 gave,
    # not the generic "unavailable" (S-neuraldeep-403-backoff-kind).
    second = await _REAL_GET_BALANCE(p)
    assert calls["n"] == 1
    assert second["error"] == "key_blocked"
