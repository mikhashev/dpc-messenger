"""Tests for OllamaProvider native tool calling.

Focus: the Ollama-specific tool path — (1) Anthropic<->Ollama(OpenAI-shape)
conversion, (2) the tool_calls_raw .id/.name/.input contract consumed by
llm_adapter._chat_native_tools, (3) the thinking-surface fallback for
reasoning models that leave content empty on the final turn. No network."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.providers.ollama_provider import OllamaProvider, OLLAMA_SAMPLING_PARAMS


class _Resp(dict):
    """Mimic the ollama ChatResponse: supports both resp['message'] and resp.attr."""
    def __init__(self, message, **attrs):
        super().__init__(message=message)
        self.__dict__.update(attrs)


def _make(config=None):
    cfg = {"model": "lfm2.5:latest", "host": "http://127.0.0.1:11434", "temperature": 0.2}
    if config:
        cfg.update(config)
    return OllamaProvider("ollama_test", cfg)


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch):
    """This file promises no network, and capability detection now asks the
    daemon whenever it can reach one. So here it deliberately cannot: the
    two detection tests below are about the fallback name lists, and the
    daemon path is covered in test_ollama_asks_the_daemon.py."""
    from dpc_client_core.providers import ollama_provider as op
    op._MODEL_CAPABILITIES.clear()

    def _refuse(*a, **k):
        raise RuntimeError("no daemon in this file")

    monkeypatch.setattr(op.ollama, "Client", _refuse)
    yield
    op._MODEL_CAPABILITIES.clear()


def test_supports_thinking_detection():
    """The fallback list, not the truth. Measured 2026-08-13: the daemon
    says lfm2.5 *can* think, so with a daemon reachable this same model
    answers True — which is the defect the list was hiding."""
    assert _make({"model": "deepseek-r1:8b"}).supports_thinking() is True
    assert _make({"model": "lfm2.5:latest"}).supports_thinking() is False


def test_supports_vision_detection():
    assert _make({"model": "qwen3.6:27b"}).supports_vision() is True
    assert _make({"model": "qwen3.5:9b"}).supports_vision() is True
    assert _make({"model": "ornith:9b-q8_0"}).supports_vision() is False


def test_build_options_sampling_params_from_config():
    p = _make({
        "context_window": 131072,
        "presence_penalty": 0.0,
        "min_p": 0.05,
        "top_k": 20,
        "num_predict": 4096,
    })
    opts = p._build_options()
    assert opts == {
        "num_ctx": 131072,
        "temperature": 0.2,
        "presence_penalty": 0.0,
        "min_p": 0.05,
        "top_k": 20,
        "num_predict": 4096,
    }
    assert "repeat_penalty" not in opts
    assert "top_p" not in opts


def test_build_options_omits_unset_and_default_temperature():
    p = OllamaProvider("ollama_test", {"model": "lfm2.5:latest"})
    assert p._build_options() is None


def test_build_options_kwargs_temperature_override():
    p = _make()
    opts = p._build_options(temperature=0.9)
    assert opts == {"temperature": 0.9}


def test_build_options_stays_plain_dict():
    """min_p must reach the server: the SDK Options model drops it, only the
    plain-dict serialization path preserves it."""
    p = _make({"min_p": 0.1})
    assert type(p._build_options()) is dict


@pytest.mark.asyncio
async def test_generate_with_tools_passes_sampling_options():
    p = _make({"presence_penalty": 0.0, "top_p": 0.95})
    fake_msg = SimpleNamespace(content="ok", thinking=None, tool_calls=[])
    p.client.chat = AsyncMock(return_value=_Resp(fake_msg, prompt_eval_count=1, eval_count=1))

    await p.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=[])

    _, kwargs = p.client.chat.call_args
    assert kwargs["options"]["presence_penalty"] == 0.0
    assert kwargs["options"]["top_p"] == 0.95
    assert kwargs["options"]["temperature"] == 0.2


def test_sampling_params_whitelist_is_closed():
    """The loop pulls ONLY these keys from config — a typo key in providers.json
    can never reach the server (whitelist by construction, S96 review)."""
    p = _make({"presence_penality": 1.5})  # typo key
    opts = p._build_options()
    assert "presence_penality" not in opts
    assert set(OLLAMA_SAMPLING_PARAMS) == {"min_p", "presence_penalty", "repeat_penalty", "top_k", "top_p", "num_predict"}


def test_tools_anthropic_to_openai():
    out = OllamaProvider._anthropic_to_openai_tools([
        {"name": "read_file", "description": "Read a file",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    ])
    assert out == [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]


def test_messages_tool_use_and_result():
    """Ollama variant: assistant tool_calls keep arguments as a dict; the tool
    result message has no tool_call_id (Ollama matches by order, not id)."""
    messages = [
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.txt"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "file body"},
        ]},
    ]
    out = OllamaProvider._anthropic_to_openai_messages("", messages)
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "let me check"
    assert out[0]["tool_calls"][0]["function"]["name"] == "read_file"
    assert out[0]["tool_calls"][0]["function"]["arguments"] == {"path": "a.txt"}
    assert out[1] == {"role": "tool", "content": "file body"}


@pytest.mark.asyncio
async def test_generate_with_tools_maps_response_to_contract():
    """tool_calls_raw items expose .id/.name/.input (dict); content/thinking/usage
    populated — exactly what llm_adapter._chat_native_tools consumes. Ollama returns
    arguments as a dict (not a JSON string)."""
    p = _make()
    fake_tc = SimpleNamespace(id="call_42", function=SimpleNamespace(name="list_dir", arguments={"path": "/tmp"}))
    fake_msg = SimpleNamespace(content="working on it", thinking="thinking about dirs", tool_calls=[fake_tc])
    p.client.chat = AsyncMock(return_value=_Resp(fake_msg, prompt_eval_count=100, eval_count=20))

    result = await p.generate_with_tools(
        messages=[{"role": "user", "content": "list /tmp"}],
        tools=[{"name": "list_dir", "description": "", "input_schema": {"type": "object"}}],
        system="be terse",
    )

    assert result["content"] == "working on it"
    assert result["thinking"] == "thinking about dirs"
    assert result["usage"] == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    assert len(result["tool_calls_raw"]) == 1
    tc = result["tool_calls_raw"][0]
    assert tc.id == "call_42"
    assert tc.name == "list_dir"
    assert tc.input == {"path": "/tmp"}

    _, kwargs = p.client.chat.call_args
    assert kwargs["tools"][0]["function"]["name"] == "list_dir"


@pytest.mark.asyncio
async def test_generate_with_tools_surfaces_thinking_when_content_empty():
    """Reasoning models (LFM2.5) put the final answer in `thinking` with empty
    content on the no-tool-call turn → surface thinking as content."""
    p = _make()
    fake_msg = SimpleNamespace(content="", thinking="The answer is 4.39 days.", tool_calls=[])
    p.client.chat = AsyncMock(return_value=_Resp(fake_msg, prompt_eval_count=50, eval_count=10))

    result = await p.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=[])
    assert result["content"] == "The answer is 4.39 days."


@pytest.mark.asyncio
async def test_generate_with_tools_no_surface_when_tool_calls_present():
    """Thinking must NOT replace empty content while a tool call is pending —
    the loop has to continue, not treat the monologue as the final answer."""
    p = _make()
    fake_tc = SimpleNamespace(id=None, function=SimpleNamespace(name="get", arguments={}))
    fake_msg = SimpleNamespace(content="", thinking="let me call the tool", tool_calls=[fake_tc])
    p.client.chat = AsyncMock(return_value=_Resp(fake_msg, prompt_eval_count=5, eval_count=2))

    result = await p.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=[])
    assert result["content"] == ""
    assert len(result["tool_calls_raw"]) == 1
    # missing id is backfilled so the round-trip tool_call_id is never empty
    assert result["tool_calls_raw"][0].id.startswith("call_")


# ---------------------------------------------------------------------------
# Loop-aware client + vision keep_alive (OLLAMA-VISION-EVENT-LOOP, 2026-07-22)
# ---------------------------------------------------------------------------

import asyncio


class _Msg(dict):
    """Ollama message: supports both msg['content'] and msg.thinking."""
    def __init__(self, content, thinking=None):
        super().__init__(content=content)
        self.thinking = thinking


class TestLoopAwareClient:
    """Agent tools run each async call in a fresh event loop that is closed
    afterward (tools/registry.py), and an httpx pool belongs to the loop that
    opened it.

    The earlier answer — replace the cached client whenever the loop changed —
    left the old one unclosed, because its loop was already dead. Each such
    call leaked a client holding an open socket to Ollama with a read
    outstanding, and one of those sockets parked the process inside
    IocpProactor.close() at shutdown, where the wait is unbounded.

    So: the shared client is used only on its own loop; any other loop gets
    one of its own and closes it before returning."""

    def _use(self, provider, loop):
        async def call():
            async with provider._client() as client:
                return client

        return loop.run_until_complete(call())

    def test_same_loop_reuses_the_shared_client(self):
        p = _make()
        shared = p.client

        loop = asyncio.new_event_loop()
        try:
            c1 = self._use(p, loop)
            c2 = self._use(p, loop)
        finally:
            loop.close()

        assert c1 is shared
        assert c2 is shared

    def test_foreign_loop_gets_its_own_client(self):
        p = _make()
        shared = p.client

        loop_a = asyncio.new_event_loop()
        try:
            c_a = self._use(p, loop_a)  # first use adopts this loop
        finally:
            loop_a.close()

        loop_b = asyncio.new_event_loop()
        try:
            c_b = self._use(p, loop_b)
        finally:
            loop_b.close()

        assert c_a is shared
        assert c_b is not shared

    def test_foreign_loop_client_is_closed_before_returning(self):
        """The leak itself: what a per-call loop opens, it must also close —
        afterwards nobody can, since the pool's loop is gone."""
        p = _make()
        closed = []
        made = []

        class _FakeClient:
            def __init__(self, host=None):
                made.append(self)

            async def close(self):
                closed.append(self)

        loop_a = asyncio.new_event_loop()
        try:
            self._use(p, loop_a)  # adopt loop A
        finally:
            loop_a.close()

        import dpc_client_core.providers.ollama_provider as mod

        original = mod.ollama.AsyncClient
        mod.ollama.AsyncClient = _FakeClient
        try:
            loop_b = asyncio.new_event_loop()
            try:
                c_b = self._use(p, loop_b)
            finally:
                loop_b.close()
        finally:
            mod.ollama.AsyncClient = original

        assert made == [c_b]
        assert closed == [c_b], "a per-call client left open is the leak"

    def test_shared_client_is_never_replaced(self):
        """Replacing it was how the old client became unreachable — and
        unclosable, its loop already dead."""
        p = _make()
        shared = p.client

        loop_a = asyncio.new_event_loop()
        try:
            self._use(p, loop_a)
        finally:
            loop_a.close()

        loop_b = asyncio.new_event_loop()
        try:
            self._use(p, loop_b)
        finally:
            loop_b.close()

        assert p.client is shared


class TestVisionKeepAlive:
    """The VL model must stay resident briefly so back-to-back agent QC calls
    don't cold-start a reload (was keep_alive=0 = unload immediately)."""

    @pytest.mark.asyncio
    async def test_default_keep_alive_is_one_minute(self):
        p = _make({"model": "qwen3-vl:8b"})
        captured = {}

        async def fake_chat(**kwargs):
            captured.update(kwargs)
            return _Resp(_Msg("a description"))

        p.client = SimpleNamespace(chat=fake_chat)
        out = await p.generate_with_vision("what is this", [{"base64": "abc", "mime_type": "image/png"}])

        assert out == "a description"
        assert captured["keep_alive"] == "1m"

    @pytest.mark.asyncio
    async def test_keep_alive_config_override(self):
        p = _make({"model": "qwen3-vl:8b", "vision_keep_alive": 0})
        captured = {}

        async def fake_chat(**kwargs):
            captured.update(kwargs)
            return _Resp(_Msg("d"))

        p.client = SimpleNamespace(chat=fake_chat)
        await p.generate_with_vision("q", [{"base64": "x", "mime_type": "image/png"}])

        assert captured["keep_alive"] == 0
