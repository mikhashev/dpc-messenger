"""Tests for ZaiCodingProvider — Z.AI GLM Coding Plan over the OpenAI-compatible
coding/paas/v4 endpoint. Focus: Anthropic<->OpenAI conversion, tool_calls_raw
shape contract, and 1313 Fair-Usage non-retry policy. No network."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.providers.zai_coding_provider import (
    ZaiCodingProvider,
    ZAI_CODING_DEFAULT_BASE_URL,
)


def _make(config=None):
    cfg = {"api_key": "test-key", "model": "glm-5.2", "temperature": 0.7}
    if config:
        cfg.update(config)
    return ZaiCodingProvider("zai_coding_test", cfg)


def test_default_base_url_and_registration_shape():
    p = _make()
    assert ZAI_CODING_DEFAULT_BASE_URL == "https://api.z.ai/api/coding/paas/v4"
    assert p.supports_thinking() is True
    assert str(p.client.base_url).rstrip("/") == ZAI_CODING_DEFAULT_BASE_URL


def test_1313_is_not_retryable():
    """Fair-Usage 1313 is an account penalty — retrying spams it, must fail fast."""
    err = Exception(
        "APIStatusError: code '1313' usage pattern does not comply with the Fair Usage Policy"
    )
    assert ZaiCodingProvider._is_retryable(err) is False


def test_transient_errors_are_retryable():
    assert ZaiCodingProvider._is_retryable(Exception("HTTP 429 rate limit")) is True
    assert ZaiCodingProvider._is_retryable(Exception("503 service unavailable")) is True
    assert ZaiCodingProvider._is_retryable(Exception("connection reset")) is True


def test_tools_anthropic_to_openai():
    anthropic_tools = [
        {"name": "read_file", "description": "Read a file",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    ]
    out = ZaiCodingProvider._anthropic_to_openai_tools(anthropic_tools)
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
    assert ZaiCodingProvider._anthropic_to_openai_tools(openai_tools) == openai_tools


def test_messages_anthropic_to_openai_text_and_system():
    system = "You are helpful."
    messages = [{"role": "user", "content": "hi"}]
    out = ZaiCodingProvider._anthropic_to_openai_messages(system, messages)
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
    out = ZaiCodingProvider._anthropic_to_openai_messages("", messages)

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
    out = ZaiCodingProvider._anthropic_to_openai_messages("", messages)
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

    p_no_temp = ZaiCodingProvider(
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
