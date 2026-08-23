"""Tests for ZaiProvider — Z.AI GLM over the prepaid pay-per-token platform API.

Most of this file is the Coding Plan provider's test suite carried over: the
Anthropic<->OpenAI conversion, the tool_calls_raw shape contract and the
temperature resolution are the same code under a new name, and dropping their
coverage while renaming would have been the expensive kind of tidy.

What is new is what the consolidation added: the endpoint guard that refuses a
subscription base URL at construction, and usage on the paths that used to lose
it. 1313 keeps its non-retry policy and gains a second meaning — on the prepaid
API that code should be unreachable, so it is a canary rather than a hiccup.

No network."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.providers.zai_provider import (
    ZaiProvider,
    ZAI_DEFAULT_BASE_URL,
)


def _make(config=None):
    cfg = {"api_key": "test-key", "model": "glm-5.2", "temperature": 0.7}
    if config:
        cfg.update(config)
    return ZaiProvider("zai_test", cfg)


def test_default_base_url_and_registration_shape():
    p = _make()
    assert ZAI_DEFAULT_BASE_URL == "https://api.z.ai/api/paas/v4"
    assert p.supports_thinking() is True
    assert str(p.client.base_url).rstrip("/") == ZAI_DEFAULT_BASE_URL


def test_1313_is_not_retryable():
    """Fair-Usage 1313 is an account penalty — retrying spams it, must fail fast."""
    err = Exception(
        "APIStatusError: code '1313' usage pattern does not comply with the Fair Usage Policy"
    )
    assert ZaiProvider._is_retryable(err) is False


def test_transient_errors_are_retryable():
    assert ZaiProvider._is_retryable(Exception("HTTP 429 rate limit")) is True
    assert ZaiProvider._is_retryable(Exception("503 service unavailable")) is True
    assert ZaiProvider._is_retryable(Exception("connection reset")) is True


def test_tools_anthropic_to_openai():
    anthropic_tools = [
        {"name": "read_file", "description": "Read a file",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    ]
    out = ZaiProvider._anthropic_to_openai_tools(anthropic_tools)
    assert out == [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]


def test_tools_openai_passthrough():
    """Already-OpenAI tools (with a 'function' key) pass through untouched."""
    openai_tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    assert ZaiProvider._anthropic_to_openai_tools(openai_tools) == openai_tools


def test_messages_anthropic_to_openai_text_and_system():
    system = "You are helpful."
    messages = [{"role": "user", "content": "hi"}]
    out = ZaiProvider._anthropic_to_openai_messages(system, messages)
    assert out == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]


def test_messages_anthropic_to_openai_tool_use_and_result():
    """assistant tool_use block -> OpenAI tool_calls; user tool_result -> role:tool."""
    messages = [
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.txt"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "file body"},
        ]},
    ]
    out = ZaiProvider._anthropic_to_openai_messages("", messages)

    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "let me check"
    assert out[0]["tool_calls"][0]["id"] == "tu_1"
    assert out[0]["tool_calls"][0]["type"] == "function"
    assert out[0]["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"path": "a.txt"}

    assert out[1] == {"role": "tool", "tool_call_id": "tu_1", "content": "file body"}


def test_messages_tool_result_content_as_list():
    """Anthropic tool_result.content may be a list of blocks — flatten to text (Ark review note 1)."""
    messages = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_9",
             "content": [{"type": "text", "text": "part1 "}, {"type": "text", "text": "part2"}]},
        ]},
    ]
    out = ZaiProvider._anthropic_to_openai_messages("", messages)
    assert out == [{"role": "tool", "tool_call_id": "tu_9", "content": "part1 part2"}]


@pytest.mark.asyncio
async def test_generate_with_tools_maps_response_to_contract():
    """Response must expose tool_calls_raw items with .id/.name/.input (dict),
    plus thinking and usage — exactly what llm_adapter._chat_native_tools consumes."""
    p = _make()

    fake_tool_call = SimpleNamespace(
        id="call_42",
        function=SimpleNamespace(name="list_dir", arguments='{"path": "/tmp"}'),
    )
    fake_msg = SimpleNamespace(
        content="working on it",
        reasoning_content="thinking about dirs",
        tool_calls=[fake_tool_call],
    )
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=fake_msg)],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )
    p.client.chat.completions.create = AsyncMock(return_value=fake_resp)

    result = await p.generate_with_tools(
        messages=[{"role": "user", "content": "list /tmp"}],
        tools=[{"name": "list_dir", "description": "", "input_schema": {"type": "object"}}],
        system="be terse",
    )

    assert result["content"] == "working on it"
    assert result["thinking"] == "thinking about dirs"
    assert result["usage"] == {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "cache_read_input_tokens": 0,
    }

    assert len(result["tool_calls_raw"]) == 1
    tc = result["tool_calls_raw"][0]
    assert tc.id == "call_42"
    assert tc.name == "list_dir"
    assert tc.input == {"path": "/tmp"}  # arguments JSON string parsed to dict

    # thinking enabled by default -> extra_body carries the thinking flag; explicit
    # config temperature (0.7) is respected (override > config > z.ai 1.0 default)
    _, kwargs = p.client.chat.completions.create.call_args
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert kwargs["temperature"] == 0.7
    assert kwargs["tool_choice"] == "auto"


def test_effective_temperature_resolution():
    """override > explicit config temperature > z.ai 1.0 (thinking) > base default."""
    p = _make({"temperature": 0.5})
    assert p._effective_temperature() == 0.5
    assert p._effective_temperature(0.2) == 0.2

    p_no_temp = ZaiProvider(
        "zai_no_temp",
        {"api_key": "k", "model": "glm-5.2", "thinking": {"enabled": True}},
    )
    assert p_no_temp._temperature_explicit is None
    assert p_no_temp._effective_temperature() == 1.0


@pytest.mark.asyncio
async def test_generate_with_tools_captures_cached_tokens():
    """usage.prompt_tokens_details.cached_tokens → cache_read_input_tokens (Ark review note 3)."""
    p = _make()
    fake_msg = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=[])
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=fake_msg)],
        usage=SimpleNamespace(
            prompt_tokens=200, completion_tokens=10, total_tokens=210,
            prompt_tokens_details=SimpleNamespace(cached_tokens=64),
        ),
    )
    p.client.chat.completions.create = AsyncMock(return_value=fake_resp)
    result = await p.generate_with_tools(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert result["usage"]["cache_read_input_tokens"] == 64


@pytest.mark.asyncio
async def test_generate_with_vision_builds_image_url():
    """Vision uses OpenAI image_url data-URL format and returns content (Ark review note 2 wraps retry)."""
    p = _make({"thinking": {"enabled": False}})
    fake_msg = SimpleNamespace(content="a cat", reasoning_content=None)
    fake_resp = SimpleNamespace(choices=[SimpleNamespace(message=fake_msg)])
    p.client.chat.completions.create = AsyncMock(return_value=fake_resp)

    out = await p.generate_with_vision("describe", [{"base64": "AAAA", "mime_type": "image/png"}])
    assert out == "a cat"
    _, kwargs = p.client.chat.completions.create.call_args
    img_block = kwargs["messages"][0]["content"][1]
    assert img_block["type"] == "image_url"
    assert img_block["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_generate_response_captures_reasoning():
    p = _make({"thinking": {"enabled": False}})
    fake_msg = SimpleNamespace(content="hello", reasoning_content=None)
    fake_resp = SimpleNamespace(choices=[SimpleNamespace(message=fake_msg)])
    p.client.chat.completions.create = AsyncMock(return_value=fake_resp)

    out = await p.generate_response("hi")
    assert out == "hello"
    # thinking disabled -> no extra_body, temperature uses config value
    _, kwargs = p.client.chat.completions.create.call_args
    assert "extra_body" not in kwargs
    assert kwargs["temperature"] == 0.7


# --- the endpoint guard: the reason this provider exists ----------------------

@pytest.mark.parametrize("bad_url", [
    "https://api.z.ai/api/anthropic",
    "https://api.z.ai/api/coding/paas/v4",
    "https://api.z.ai/api/v1",
    "https://api.z.ai/api/coding/paas/v4/",       # trailing slash
    "HTTPS://API.Z.AI/API/CODING/PAAS/V4",        # case
])
def test_a_coding_plan_endpoint_is_refused_at_construction(bad_url):
    """All three are the GLM Coding Plan, which this product may not use: the
    vendor limits subscription benefits to a published list of tools that does
    not include D-PC, and this account has already been banned for a month once
    for exactly this. A warning would be a line in a log nobody reads while the
    violations accumulate, so it raises."""
    with pytest.raises(ValueError) as exc:
        _make({"base_url": bad_url})
    assert "Coding Plan" in str(exc.value)
    assert ZAI_DEFAULT_BASE_URL in str(exc.value), "the error has to name the way out"


def test_the_prepaid_endpoint_is_accepted():
    p = _make({"base_url": ZAI_DEFAULT_BASE_URL})
    assert str(p.client.base_url).rstrip("/") == ZAI_DEFAULT_BASE_URL


def test_fair_usage_1313_is_still_not_retryable():
    """It was never a transient error. On the prepaid API it should also be
    unreachable, which is why the provider logs it at ERROR."""
    assert ZaiProvider._is_retryable(Exception("error code 1313 fair usage")) is False


# --- usage: a prepaid call whose tokens go unrecorded is invisible money ------

@pytest.mark.asyncio
async def test_the_plain_path_records_usage():
    """Not only the tools path. This project already paid for this hole once, on
    the DeepSeek plain path, and the fix did not travel."""
    p = _make()
    fake_msg = SimpleNamespace(content="hi", reasoning_content=None)
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=fake_msg)],
        usage=SimpleNamespace(
            prompt_tokens=11, completion_tokens=3, total_tokens=14,
            prompt_tokens_details=SimpleNamespace(cached_tokens=4),
        ),
    )
    p.client.chat.completions.create = AsyncMock(return_value=fake_resp)

    assert await p.generate_response("hello") == "hi"
    assert p.get_last_usage()["prompt_tokens"] == 11
    assert p.get_last_usage()["cache_read_input_tokens"] == 4


@pytest.mark.asyncio
async def test_the_stream_asks_for_usage_and_reads_the_chunk_that_carries_it():
    """Two failure modes in one test. An OpenAI-compatible stream reports no
    usage at all unless stream_options.include_usage is sent; and the chunk that
    carries it has an empty `choices`, so a loop that tests for choices first
    drops the very chunk it asked for."""
    p = _make()

    class _Stream:
        def __aiter__(self):
            async def gen():
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(
                        content="he", reasoning_content=None))],
                    usage=None,
                )
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(
                        content="llo", reasoning_content=None))],
                    usage=None,
                )
                # the usage-only chunk: no choices at all
                yield SimpleNamespace(
                    choices=[],
                    usage=SimpleNamespace(
                        prompt_tokens=7, completion_tokens=2, total_tokens=9,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=1),
                    ),
                )
            return gen()

    p.client.chat.completions.create = AsyncMock(return_value=_Stream())

    seen = []
    async def on_chunk(text, conv_id):
        seen.append(text)

    out = await p.generate_response_stream("hello", on_chunk, "conv-1")

    assert out == "hello" and seen == ["he", "llo"]
    _, kwargs = p.client.chat.completions.create.call_args
    assert kwargs["stream_options"] == {"include_usage": True}
    assert p.get_last_usage()["prompt_tokens"] == 7
    assert p.get_last_usage()["completion_tokens"] == 2


@pytest.mark.asyncio
async def test_the_vision_path_records_usage():
    p = _make({"thinking": {"enabled": False}})
    fake_msg = SimpleNamespace(content="a cat", reasoning_content=None)
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=fake_msg)],
        usage=SimpleNamespace(
            prompt_tokens=500, completion_tokens=6, total_tokens=506,
            prompt_tokens_details=None,
        ),
    )
    p.client.chat.completions.create = AsyncMock(return_value=fake_resp)

    assert await p.generate_with_vision("describe", [
        {"base64": "AAAA", "mime_type": "image/png"}
    ]) == "a cat"
    assert p.get_last_usage()["prompt_tokens"] == 500


# --- Ark's code review, 2026-08-23 -------------------------------------------

def _two_token_stream():
    """A stream that yields 'he' + 'llo' and then the usage-only chunk."""
    class _Stream:
        def __aiter__(self):
            async def gen():
                for piece in ("he", "llo"):
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(
                            content=piece, reasoning_content=None))],
                        usage=None,
                    )
                yield SimpleNamespace(
                    choices=[],
                    usage=SimpleNamespace(
                        prompt_tokens=7, completion_tokens=2, total_tokens=9,
                        prompt_tokens_details=None,
                    ),
                )
            return gen()
    return _Stream()


@pytest.mark.asyncio
async def test_a_retried_stream_delivers_each_token_exactly_once(monkeypatch):
    """The retry branch used to send the whole answer again after the retried
    call had already streamed it token by token: the reader saw the text twice
    and we paid for the bytes twice. Found by Ark reading the diff, 2026-08-23 —
    unreachable until a real network produces a retryable error, which it will."""
    from dpc_client_core.providers import zai_provider as mod

    p = _make()
    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock())
    p.client.chat.completions.create = AsyncMock(
        side_effect=[Exception("HTTP 429 rate limit"), _two_token_stream()]
    )

    seen = []
    async def on_chunk(text, conv_id):
        seen.append(text)

    out = await p.generate_response_stream("hello", on_chunk, "conv-1")

    assert out == "hello"
    assert seen == ["he", "llo"], "the full text must not follow the tokens"
    assert "".join(seen) == out


@pytest.mark.parametrize("model, vision", [
    ("glm-4.7", False),
    ("glm-5.3", False),
    ("glm-4.7-flashx", False),
    ("glm-4-32b-0414-128k", False),
    ("glm-4.6v", True),
    ("glm-4.6v-flash", True),
    ("glm-5v-turbo", True),
    ("glm-4.5v", True),
    ("glm-ocr", True),
    ("GLM-4.6V", True),
])
def test_only_the_v_models_claim_vision(model, vision):
    """`supports_vision` returned an unconditional True while its own docstring
    named the V models. `llm_manager` auto-selects the first provider that says
    yes when an image query has no configured vision provider, so a glm-4.7
    alias would volunteer for image work and fail at the API."""
    assert _make({"model": model}).supports_vision() is vision

