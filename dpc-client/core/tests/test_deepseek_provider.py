"""Tests for DeepSeekProvider — DeepSeek over the OpenAI-compatible endpoint.

Focus: the DeepSeek-specific behaviours that differ from ZaiProvider —
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
    # The vendor maps xhigh to *high*; sending "max" upgraded whoever asked for it.
    assert _make({"reasoning_effort": "xhigh"})._build_extra_body()["reasoning_effort"] == "high"
    # invalid effort → omitted
    assert "reasoning_effort" not in _make({"reasoning_effort": "bogus"})._build_extra_body()
    # effort ignored when thinking disabled
    p_off = _make({"thinking": {"enabled": False}, "reasoning_effort": "high"})
    assert "reasoning_effort" not in p_off._build_extra_body()


def test_reasoning_effort_per_call_override():
    """A per-call reasoning_effort (e.g. a UI toggle) wins over the provider-config
    default; None/invalid falls back to config so callers passing nothing keep it.
    Regression guard: chat()'s old 'medium' default must not silently downgrade max."""
    p = _make({"reasoning_effort": "max"})
    # No override / None -> config value survives.
    assert p._build_extra_body()["reasoning_effort"] == "max"
    assert p._build_extra_body(None)["reasoning_effort"] == "max"
    # Explicit override wins.
    assert p._build_extra_body("high")["reasoning_effort"] == "high"
    assert p._build_extra_body("xhigh")["reasoning_effort"] == "high"
    # Invalid / "auto" override -> fall back to config (graceful, no downgrade).
    assert p._build_extra_body("auto")["reasoning_effort"] == "max"
    assert p._build_extra_body("bogus")["reasoning_effort"] == "max"
    # No config effort: omit unless overridden per-call.
    p2 = _make()
    assert "reasoning_effort" not in p2._build_extra_body()
    assert p2._build_extra_body("high")["reasoning_effort"] == "high"
    # Per-call effort still suppressed when thinking disabled.
    p_off = _make({"thinking": {"enabled": False}})
    assert "reasoning_effort" not in p_off._build_extra_body("max")


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
    # No native cache fields on the response → hit=0, miss=prompt_tokens (conservative).
    # No completion_tokens_details → reasoning=0, content=completion.
    assert result["usage"] == {
        "prompt_tokens": 100, "completion_tokens": 20,
        "reasoning_tokens": 0, "content_tokens": 20,
        "total_tokens": 120,
        "cache_read_input_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 100,
    }
    assert len(result["tool_calls_raw"]) == 1
    tc = result["tool_calls_raw"][0]
    assert tc.id == "call_42"
    assert tc.name == "list_dir"
    assert tc.input == {"path": "/tmp"}

    # thinking on by default → extra_body carries enabled, and the config
    # temperature is *withheld*: the API ignores it while reasoning (measured
    # 2026-08-15), and a number on the wire that changes no answer only makes
    # the editor's field look like a control.
    _, kwargs = p.client.chat.completions.create.call_args
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "temperature" not in kwargs
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


@pytest.mark.asyncio
async def test_generate_with_tools_captures_reasoning_tokens():
    """completion_tokens_details.reasoning_tokens splits completion into
    reasoning vs content (observability for effort efficiency)."""
    p = _make()
    fake_msg = SimpleNamespace(content="answer", reasoning_content="long cot", tool_calls=[])
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=fake_msg)],
        usage=SimpleNamespace(
            prompt_tokens=100, completion_tokens=900, total_tokens=1000,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=850),
        ),
    )
    p.client.chat.completions.create = AsyncMock(return_value=fake_resp)
    result = await p.generate_with_tools(messages=[{"role": "user", "content": "hi"}], tools=[])
    u = result["usage"]
    assert u["reasoning_tokens"] == 850
    assert u["content_tokens"] == 50  # 900 - 850


@pytest.mark.asyncio
async def test_generate_with_tools_prefers_native_cache_split():
    """DeepSeek-native prompt_cache_hit/miss_tokens win over the OpenAI-compat
    prompt_tokens_details.cached_tokens (which DeepSeek leaves at 0)."""
    p = _make()
    fake_msg = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=[])
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=fake_msg)],
        usage=SimpleNamespace(
            prompt_tokens=1000, completion_tokens=50, total_tokens=1050,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            prompt_cache_hit_tokens=320, prompt_cache_miss_tokens=680,
        ),
    )
    p.client.chat.completions.create = AsyncMock(return_value=fake_resp)
    result = await p.generate_with_tools(messages=[{"role": "user", "content": "hi"}], tools=[])
    u = result["usage"]
    assert u["prompt_cache_hit_tokens"] == 320
    assert u["prompt_cache_miss_tokens"] == 680
    assert u["cache_read_input_tokens"] == 320  # mirrors hit for back-compat


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
    toggle), unlike ZaiProvider which omits extra_body when disabled."""
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


# --- balance endpoint (Phase 2: balance-poll) ---

def test_supports_balance():
    assert _make().supports_balance() is True


@pytest.mark.asyncio
async def test_get_balance_calls_user_balance_endpoint(monkeypatch):
    """get_balance() GETs {base_url}/user/balance with a bearer token and returns
    the raw DeepSeek payload."""
    p = _make()
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "is_available": True,
                "balance_infos": [{
                    "currency": "USD", "total_balance": "7.52",
                    "granted_balance": "0.00", "topped_up_balance": "7.52",
                }],
            }

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    result = await p.get_balance()
    assert result["is_available"] is True
    assert result["balance_infos"][0]["total_balance"] == "7.52"
    assert captured["url"] == "https://api.deepseek.com/user/balance"
    assert captured["headers"]["Authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_get_balance_strips_v1_from_base_url(monkeypatch):
    """A /v1 base_url (valid for chat) must not leak into the balance URL —
    /user/balance lives at the API root, not under /v1."""
    p = _make({"base_url": "https://api.deepseek.com/v1"})
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"is_available": True, "balance_infos": []}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            captured["url"] = url
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await p.get_balance()
    assert captured["url"] == "https://api.deepseek.com/user/balance"


# --- thinking-CoT replay (Phase 3) ---

@pytest.mark.asyncio
async def test_cot_replay_restores_real_reasoning_not_placeholder():
    """A tool-call round caches its reasoning_content by tool_call id, and the next
    round replays the REAL CoT instead of the ' ' placeholder."""
    p = _make()

    # Round 1: model returns a tool call + reasoning_content.
    tc = SimpleNamespace(id="call_X", function=SimpleNamespace(name="ls", arguments="{}"))
    msg1 = SimpleNamespace(content="", reasoning_content="my real CoT", tool_calls=[tc])
    resp1 = SimpleNamespace(
        choices=[SimpleNamespace(message=msg1)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    p.client.chat.completions.create = AsyncMock(return_value=resp1)
    await p.generate_with_tools(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "ls", "description": "", "input_schema": {"type": "object"}}],
    )
    assert p._cot_cache.get("call_X") == "my real CoT"

    # Round 2: replay carries the round-1 assistant tool_use(call_X) + a tool result.
    captured = {}

    async def _capture(**kwargs):
        captured["messages"] = kwargs["messages"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="done", reasoning_content=None, tool_calls=[]))],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=3, total_tokens=23),
        )

    p.client.chat.completions.create = _capture
    replay = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_X", "name": "ls", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_X", "content": "ok"}]},
    ]
    await p.generate_with_tools(messages=replay, tools=[])
    asst = [m for m in captured["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
    assert asst, "expected an assistant tool-call message in the replayed request"
    assert asst[0]["reasoning_content"] == "my real CoT"  # real CoT, not " "


@pytest.mark.asyncio
async def test_cot_replay_falls_back_to_placeholder_when_uncached():
    """When the CoT for a replayed tool call is not cached, reasoning_content stays
    the ' ' placeholder (avoids the 400, no crash)."""
    p = _make()
    captured = {}

    async def _capture(**kwargs):
        captured["messages"] = kwargs["messages"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="done", reasoning_content=None, tool_calls=[]))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7),
        )

    p.client.chat.completions.create = _capture
    replay = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "unknown_id", "name": "ls", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "unknown_id", "content": "ok"}]},
    ]
    await p.generate_with_tools(messages=replay, tools=[])
    asst = [m for m in captured["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
    assert asst[0]["reasoning_content"] == " "


# --- sampling fields while thinking is on (measured inert 2026-08-15) ---

def test_no_temperature_or_top_p_while_thinking():
    """The API ignores both while reasoning, so the request stops carrying them.

    Measured, not assumed: with thinking off, temperature 0.0 returned the same
    word 5/5 and 2.0 returned five different ones; with thinking on, 0.0 returned
    four different words out of five.
    """
    p = _make({"thinking": {"enabled": True}, "temperature": 0.6, "top_p": 0.9})
    assert p._sampling_params() == {}


def test_sampling_is_sent_when_thinking_is_off():
    p = _make({"thinking": {"enabled": False}, "temperature": 0.6, "top_p": 0.9})
    assert p._sampling_params() == {"temperature": 0.6, "top_p": 0.9}


def test_top_p_stays_absent_when_unset():
    """None means "the API's default", which is said by silence, not by a null."""
    p = _make({"thinking": {"enabled": False}, "temperature": 0.6})
    assert p._sampling_params() == {"temperature": 0.6}


def test_a_per_call_temperature_is_honoured_when_thinking_is_off():
    p = _make({"thinking": {"enabled": False}, "temperature": 0.6})
    assert p._sampling_params(0.1)["temperature"] == 0.1


def test_the_withheld_number_is_named_once(caplog):
    """Once per provider: the reason belongs to the alias, not to the call."""
    p = _make({"thinking": {"enabled": True}, "temperature": 0.6})
    with caplog.at_level("INFO"):
        p._sampling_params()
        p._sampling_params()
    lines = [r.getMessage() for r in caplog.records if "not sent" in r.getMessage()]
    assert len(lines) == 1
    assert "0.6" in lines[0]


def test_nothing_is_said_when_the_field_is_actually_used(caplog):
    p = _make({"thinking": {"enabled": False}, "temperature": 0.6})
    with caplog.at_level("INFO"):
        p._sampling_params()
    assert not [r for r in caplog.records if "not sent" in r.getMessage()]


# --- Off in the chat header: the alias-level toggle was the only one there was ---

def test_off_disables_thinking_for_this_call_only():
    """The header outranks the alias, and `off` is the direction it could not
    express before: DeepSeek's own `none` effort leaves thinking running."""
    p = _make({"thinking": {"enabled": True}, "reasoning_effort": "max"})
    assert p._build_extra_body("off") == {"thinking": {"type": "disabled"}}
    # the alias is untouched — the next call without an override still thinks
    assert p._build_extra_body()["thinking"] == {"type": "enabled"}


def test_off_never_reaches_the_wire_as_an_effort():
    """`off` is not one of DeepSeek's seven words; sent as one it would 400.

    This pins an invariant, not a branch: it holds because `off` is what turns
    thinking off, and no effort is sent then. Neutralising an explicit filter
    for it left this green — which is what proved the filter was decoration.
    """
    body = _make({"reasoning_effort": "high"})._build_extra_body("off")
    assert "reasoning_effort" not in body


def test_off_gives_the_temperature_back():
    """Sampling is inert only while reasoning. Turn reasoning off for a call and
    the number is live again, so withholding it there would remove a control
    exactly where it works."""
    p = _make({"thinking": {"enabled": True}, "temperature": 0.6, "top_p": 0.9})
    assert p._sampling_params() == {}
    assert p._sampling_params(reasoning_effort="off") == {"temperature": 0.6, "top_p": 0.9}


def test_an_alias_with_thinking_off_is_not_switched_on_by_a_level():
    """Config says no; a level says how much. The nearer scope wins on the
    effort, but it cannot manufacture a thinking mode the alias disabled."""
    p = _make({"thinking": {"enabled": False}})
    assert p._build_extra_body("high") == {"thinking": {"type": "disabled"}}

async def _no_sleep(_seconds):
    """`_retry_with_backoff` waits 3s before its first retry; the wait is not
    the behaviour any test here is about."""
    return None



# --- the retried stream used to deliver the answer twice ----------------------

class _StreamOf:
    """An async iterator over prepared chunks — what the SDK hands back."""

    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


class _FlakyCompletions:
    """Retryable on the first call, a real stream on the second.

    Counts its calls so the test can assert the retry actually happened rather
    than inferring it from the output it is also asserting on.
    """

    def __init__(self, chunks):
        self._chunks = chunks
        self.calls = 0

    async def create(self, **params):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("HTTP 429 rate limit")
        return _StreamOf(self._chunks)


def _token(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(
            content=text, reasoning_content=None), finish_reason=None)],
        usage=None,
    )


def _usage_chunk():
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2, total_tokens=9,
                              prompt_tokens_details=None),
    )


@pytest.mark.asyncio
async def test_a_retried_stream_delivers_each_token_exactly_once(monkeypatch):
    """`_call` is the whole stream: the retry re-runs it and it delivers every
    token through `on_chunk` on its way to returning `full_text`. Sending that
    return value to `on_chunk` as well put the answer on the wire a second time.

    It is not a double bill — `on_chunk` reaches `agent_manager.emit_stream_chunk`,
    which appends and broadcasts and touches no usage. It is worse in a slower
    way: `_raw` becomes the answer twice, `_streaming_raw` stops matching
    `response` (`agent_manager.py:1168`) where it is normally None, and the
    doubled text is persisted to conversation history and read back as context by
    every later turn.

    Unreachable on a mock that never fails, which is why it survived — found by
    Ark in `zai_provider.py` and by the Orbit graph in the other two, 2026-08-23.
    """
    from dpc_client_core.providers import deepseek_provider as mod

    p = _make()
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)  # 3s of backoff, not behaviour
    completions = _FlakyCompletions([_token("he"), _token("llo"), _usage_chunk()])
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    seen = []

    async def on_chunk(text, conv_id):
        seen.append(text)

    out = await p.generate_response_stream("hello", on_chunk, "conv-1")

    assert completions.calls == 2, "the retry has to have happened for this to mean anything"
    assert out == "hello"
    assert seen == ["he", "llo"], "the full text must not follow the tokens"
    assert "".join(seen) == out


# --- an effort reaches the wire on the paths without tools --------------------

@pytest.mark.asyncio
async def test_the_plain_path_sends_the_callers_effort_and_not_the_alias_ceiling():
    """`_build_extra_body()` was called with no argument here while the tools path
    passed one, so sleep, knowledge extraction and Local AI Chat could only ever
    run at the alias default — max on both live aliases when it was measured, over
    118 plain calls. The label was read from the same body and was therefore
    honest, which is why nothing looked wrong."""
    p = _make({"reasoning_effort": "max"})  # the alias ceiling
    fake_msg = SimpleNamespace(content="ok", reasoning_content=None)
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=fake_msg)],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )
    p.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=AsyncMock(return_value=fake_resp))))

    await p.generate_response("hi", reasoning_effort="low")

    _, kwargs = p.client.chat.completions.create.call_args
    assert kwargs["extra_body"]["reasoning_effort"] == "low", "the caller's word, not the alias's"


@pytest.mark.asyncio
async def test_off_on_the_plain_path_turns_thinking_off_rather_than_lowering_it():
    """`off` is not a level: it disables the thinking block, which is the only
    thing the API listens to. A path that could not carry the word could not
    express it either."""
    p = _make({"reasoning_effort": "max"})
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", reasoning_content=None))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )
    p.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=AsyncMock(return_value=fake_resp))))

    await p.generate_response("hi", reasoning_effort="off")

    _, kwargs = p.client.chat.completions.create.call_args
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in kwargs["extra_body"]


@pytest.mark.asyncio
async def test_the_stream_has_somewhere_to_put_an_effort_and_sends_it():
    """Half of this defect was the signature: `generate_response_stream` had no
    parameter for an effort, so a caller could not have lowered it if it wanted."""
    p = _make({"reasoning_effort": "max"})
    p.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=AsyncMock(return_value=_StreamOf(
            [_token("hi"), _usage_chunk()])))))

    async def on_chunk(text, conv_id):
        pass

    await p.generate_response_stream("hi", on_chunk, "conv-1", reasoning_effort="medium")

    _, kwargs = p.client.chat.completions.create.call_args
    assert kwargs["extra_body"]["reasoning_effort"] == "medium"

