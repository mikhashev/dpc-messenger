"""Tests for DeepSeekProvider — DeepSeek over the OpenAI-compatible endpoint.

Focus: the DeepSeek-specific behaviours that differ from ZaiCodingProvider —
(1) reasoning_content echo on assistant tool-call messages (the make-or-break for
multi-round agent tool use), (2) explicit thinking enabled/disabled toggle,
(3) reasoning_effort mapping, (4) no 1313 special-case. Plus the shared
Anthropic<->OpenAI conversion + tool_calls_raw contract. No network."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.providers.deepseek_provider import (
    DeepSeekProvider,
    DEEPSEEK_DEFAULT_BASE_URL,
)


def _make(config=None):
    cfg = {"api_key": "test-key", "model": "deepseek-v4-flash", "temperature": 0.7}
    if config:
        cfg.update(config)
    return DeepSeekProvider("deepseek_test", cfg)


def test_default_base_url_and_registration_shape():
    p = _make()
    assert DEEPSEEK_DEFAULT_BASE_URL == "https://api.deepseek.com"
    assert p.supports_thinking() is True
    assert p.supports_vision() is False  # DeepSeek V4 text models are not multimodal
    assert str(p.client.base_url).rstrip("/") == DEEPSEEK_DEFAULT_BASE_URL


def test_transient_errors_are_retryable():
    assert DeepSeekProvider._is_retryable(Exception("HTTP 429 rate limit")) is True
    assert DeepSeekProvider._is_retryable(Exception("503 service unavailable")) is True
    assert DeepSeekProvider._is_retryable(Exception("connection reset")) is True


def test_client_errors_are_not_retryable():
    """No 1313 special-case needed (DeepSeek never emits it); plain 4xx fail fast."""
    assert DeepSeekProvider._is_retryable(Exception("400 invalid request body")) is False
    assert DeepSeekProvider._is_retryable(Exception("401 unauthorized")) is False


# --- thinking toggle (DeepSeek-specific: must send {type: disabled} to override default-on) ---

def test_build_extra_body_thinking_toggle():
    p_on = _make({"thinking": {"enabled": True}})
    assert p_on._build_extra_body() == {"thinking": {"type": "enabled"}}
    # default (no thinking key) is enabled
    assert _make()._build_extra_body() == {"thinking": {"type": "enabled"}}
    # disabled must be sent explicitly (DeepSeek thinking is default-on server-side)
    p_off = _make({"thinking": {"enabled": False}})
    assert p_off._build_extra_body() == {"thinking": {"type": "disabled"}}


def test_reasoning_effort_mapping():
    assert _make({"reasoning_effort": "high"})._build_extra_body() == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "high",
    }
    assert _make({"reasoning_effort": "xhigh"})._build_extra_body()["reasoning_effort"] == "max"
    # invalid effort → omitted
    assert "reasoning_effort" not in _make({"reasoning_effort": "bogus"})._build_extra_body()
    # effort ignored when thinking disabled
    p_off = _make({"thinking": {"enabled": False}, "reasoning_effort": "high"})
    assert "reasoning_effort" not in p_off._build_extra_body()


# --- reasoning_content echo (THE critical DeepSeek quirk) ---

def test_reasoning_content_echo_pads_placeholder_on_tool_calls():
    """With thinking on, an assistant message carrying tool_calls MUST get
    reasoning_content on replay; the adapter drops thinking blocks, so we pad
    with a single space (V4 Pro rejects "")."""
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.txt"}},
        ]},
    ]
    out = DeepSeekProvider._anthropic_to_openai_messages("", messages, reasoning_echo=True)
    assert out[0]["reasoning_content"] == " "
    assert out[0]["tool_calls"][0]["id"] == "tu_1"


def test_reasoning_content_echo_uses_real_thinking_when_present():
    messages = [
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "let me reason"},
            {"type": "tool_use", "id": "tu_2", "name": "x", "input": {}},
        ]},
    ]
    out = DeepSeekProvider._anthropic_to_openai_messages("", messages, reasoning_echo=True)
    assert out[0]["reasoning_content"] == "let me reason"


def test_no_reasoning_content_when_echo_disabled():
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.txt"}},
        ]},
    ]
    out = DeepSeekProvider._anthropic_to_openai_messages("", messages, reasoning_echo=False)
    assert "reasoning_content" not in out[0]


def test_no_reasoning_content_on_assistant_without_tool_calls():
    """Only assistant messages that carry tool_calls need the echo."""
    messages = [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
    out = DeepSeekProvider._anthropic_to_openai_messages("", messages, reasoning_echo=True)
    assert "reasoning_content" not in out[0]


# --- shared Anthropic<->OpenAI conversion ---

def test_tools_anthropic_to_openai():
    anthropic_tools = [
        {"name": "read_file", "description": "Read a file",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    ]
    out = DeepSeekProvider._anthropic_to_openai_tools(anthropic_tools)
    assert out == [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]


def test_messages_tool_use_and_result():
    messages = [
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.txt"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "file body"},
        ]},
    ]
    out = DeepSeekProvider._anthropic_to_openai_messages("", messages)
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "let me check"
    assert out[0]["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"path": "a.txt"}
    assert out[1] == {"role": "tool", "tool_call_id": "tu_1", "content": "file body"}


def test_messages_tool_result_content_as_list():
    messages = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_9",
             "content": [{"type": "text", "text": "part1 "}, {"type": "text", "text": "part2"}]},
        ]},
    ]
    out = DeepSeekProvider._anthropic_to_openai_messages("", messages)
    assert out == [{"role": "tool", "tool_call_id": "tu_9", "content": "part1 part2"}]


# --- generate_with_tools contract ---

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
    assert tc.input == {"path": "/tmp"}

    # thinking on by default → extra_body carries enabled; config temperature respected
    _, kwargs = p.client.chat.completions.create.call_args
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert kwargs["temperature"] == 0.7
    assert kwargs["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_generate_with_tools_captures_cached_tokens():
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


def test_effective_temperature_resolution():
    p = _make({"temperature": 0.5})
    assert p._effective_temperature() == 0.5
    assert p._effective_temperature(0.2) == 0.2

    p_no_temp = DeepSeekProvider("ds_no_temp", {"api_key": "k", "model": "deepseek-v4-flash"})
    assert p_no_temp._temperature_explicit is None
    assert p_no_temp._effective_temperature() == 1.0


@pytest.mark.asyncio
async def test_generate_response_sends_thinking_disabled_when_off():
    """DeepSeek-specific: thinking off must send {type: disabled} (always send the
    toggle), unlike ZaiCodingProvider which omits extra_body when disabled."""
    p = _make({"thinking": {"enabled": False}})
    fake_msg = SimpleNamespace(content="hello", reasoning_content=None)
    fake_resp = SimpleNamespace(choices=[SimpleNamespace(message=fake_msg)])
    p.client.chat.completions.create = AsyncMock(return_value=fake_resp)

    out = await p.generate_response("hi")
    assert out == "hello"
    _, kwargs = p.client.chat.completions.create.call_args
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs["temperature"] == 0.7


@pytest.mark.asyncio
async def test_generate_with_vision_builds_image_url():
    p = _make()
    fake_msg = SimpleNamespace(content="a cat", reasoning_content=None)
    fake_resp = SimpleNamespace(choices=[SimpleNamespace(message=fake_msg)])
    p.client.chat.completions.create = AsyncMock(return_value=fake_resp)

    out = await p.generate_with_vision("describe", [{"base64": "AAAA", "mime_type": "image/png"}])
    assert out == "a cat"
    _, kwargs = p.client.chat.completions.create.call_args
    img_block = kwargs["messages"][0]["content"][1]
    assert img_block["type"] == "image_url"
    assert img_block["image_url"]["url"].startswith("data:image/png;base64,")
