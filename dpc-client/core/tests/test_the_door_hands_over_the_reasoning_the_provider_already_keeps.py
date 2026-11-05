"""The door hands over the reasoning its usage row counts.

ADR-041 D4, amendment 2026-11-04; board entry THE-DOOR-COUNTS-THE-REASONING-AND-
NEVER-HANDS-IT-OVER-SO-A-CLIENT-WATCHES-AN-EMPTY-THINKING-LINE. On the OpenAI form
a stream carries `reasoning_content` deltas as the model makes them, before the
content they precede, and a whole message carries a `reasoning_content` field: the
names DeepSeek, llama-server and the Strata launcher write (`serve/server.py`
v0.2.9, `chunk({"reasoning_content": ...})` and `msg["reasoning_content"]`).

Reasoning travels by `on_reasoning`, a parameter only the door passes, so the agent
loop, the UI and a peer's chunk sender keep receiving what they received, and that
is pinned on the real provider classes. The stack under the door is real, as in the
file `_Engine` comes from.

Beside tools `llamacpp_server` and `strata` stream too (amendment 2026-11-05).
"""

import ast
import asyncio
import inspect
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from openai import AsyncOpenAI

from dpc_client_core import gateway as gateway_module
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.gateway import _openai_messages
from dpc_client_core.llm_manager import PROVIDER_MAP, LLMManager
from dpc_client_core.managers.llama_server_supervisor import _CARD_HOLDS
from dpc_client_core.providers import AIProvider
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.neuraldeep_provider import NeuralDeepProvider
from dpc_client_core.providers.strata_provider import StrataProvider
from dpc_client_core.providers.zai_provider import ZaiProvider
from tests.test_a_client_that_leaves_the_door_ends_the_call_and_nothing_is_generated_again import (
    ALIAS,
    CHAT_PATH,
    MESSAGES_PATH,
    TOOLS_ANTHROPIC,
    TOOLS_OPENAI,
    _chat,
    _Client,
    _Engine,
    _forget,
    _host,
    _local_provider,
    _messages,
    _service,
    _settled,
    _until,
    fast_backoff,  # noqa: F401  (a fixture)
    no_handler_cancellation,  # noqa: F401  (a fixture)
)
from tests.test_strata_supervisor import _install
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER_ANSWER,
    REMOTE_MODEL,
    _peer_service,
    _priced_result,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import _key, _running

REASONING = ("Let me ", "think ", "this over.")
CONTENT = ("the ", "answer")
ONE_PIXEL = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
ENGINE_CALL = {"id": "call_1", "type": "function",
               "function": {"name": "read_file", "arguments": "{\"path\": \"a.txt\"}"}}


async def _post(server, tmp_path, path, body):
    url = f"http://127.0.0.1:{server.port}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers={"x-api-key": _key(tmp_path)}) as response:
            return response.status, await response.text()


def _events(text):
    return [json.loads(line[len("data: "):]) for line in text.split("\n")
            if line.startswith("data: {")]


def _deltas(text):
    return [event["choices"][0]["delta"] for event in _events(text) if event.get("choices")]


def _field(deltas, name):
    return [delta[name] for delta in deltas if delta.get(name)]


async def _door_call(tmp_path, engine_kwargs, body, *, path=CHAT_PATH, provider_kwargs=None, prepare=None,
                     engine_class=_Engine):
    """One call through the whole stack. Returns (status, text, engine, rows)."""
    async with engine_class(**engine_kwargs) as engine:
        provider = _local_provider(engine, **(provider_kwargs or {}))
        if prepare is not None:
            prepare(provider)
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, ledger):
                status, text = await _post(server, tmp_path, path, body)
                return status, text, engine, list(ledger.rows())
        finally:
            _forget(provider)


# --- (1) a stream, as the model makes it ----------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_deltas_arrive_piece_by_piece_before_the_content_they_precede(tmp_path):
    status, text, engine, _ = await _door_call(
        tmp_path, {"reasoning": REASONING, "content": CONTENT}, _chat(stream=True),
    )
    assert status == 200
    deltas = _deltas(text)
    assert _field(deltas, "reasoning_content") == list(REASONING), "the pieces the engine sent, not one lump"
    assert _field(deltas, "content") == list(CONTENT)
    keys = [next(k for k in delta if k != "role") for delta in deltas if delta]
    assert keys == ["reasoning_content"] * 3 + ["content"] * 2, "reasoning after the content it precedes"
    assert deltas[0]["role"] == "assistant", "the first delta names the role, whichever field it carries"
    assert [delta.get("role") for delta in deltas[1:]] == [None] * (len(deltas) - 1), "one role per response"
    assert _events(text)[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_a_reasoning_delta_leaves_while_the_model_is_still_thinking(tmp_path):
    """A door that collected the reasoning and wrote it before the first content
    would pass every order check; the client would still watch an empty line."""
    gate = asyncio.Event()
    async with _Engine(reasoning=REASONING, content=CONTENT, content_gate=gate) as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, _):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(stream=True))
                await client.send()
                seen = await client.read_until(REASONING[-1].encode("utf-8"))
                assert b'"content"' not in seen and not engine.finished
                gate.set()
                rest = await client.read_until(b"[DONE]")
                await client.leave()
                assert b"answer" in rest
        finally:
            gate.set()
            _forget(provider)


@pytest.mark.asyncio
async def test_a_stream_on_a_strata_alias_carries_it_too(tmp_path):
    """The class the owner runs: it inherits the entry points, and this is what says so."""
    def strata(engine):
        return _local_provider(
            engine, alias="flash", provider_class=StrataProvider,
            model="qwen3.8-flash-next-iq3_xxs", strata_install_dir=str(_install(tmp_path)),
            strata_base_config="strata-base.json", conversation_cache_mib=10240,
        )

    async with _Engine(reasoning=REASONING, content=CONTENT) as engine:
        provider = strata(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, _):
                _, text = await _post(server, tmp_path, CHAT_PATH, _chat("flash", stream=True))
        finally:
            _forget(provider)
    assert _field(_deltas(text), "reasoning_content") == list(REASONING)


# --- (2) the tools path on a door's stream: deltas as the engine makes them, a call whole ---


CALL_IN_PIECES = {"id": "call_85f3c07168754206a762f97c", "name": "read_file",
                  "pieces": ('{"path"', ': "a', '.txt"}')}
CALL_ON_THE_WIRE = {"index": 0, "id": CALL_IN_PIECES["id"], "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})}}
# The `timings` of the live call, whole.
LAUNCHER_TIMINGS = {"cache_n": 0, "prompt_n": 285, "prompt_ms": 1240.5, "prompt_per_token_ms": 4.353,
                    "prompt_per_second": 229.7, "predicted_n": 52, "predicted_ms": 457.4,
                    "predicted_per_token_ms": 8.796, "predicted_per_second": 113.7,
                    "draft_n": 48, "draft_n_accepted": 36}


class _Launcher(_Engine):
    """`openai_chunks` on a stream, `openai_collect` on a whole reply: a role chunk, a
    keep-alive comment, reasoning, content, a call as a name and then pieces of its
    arguments, and a last chunk with the finish word, usage, `timings` and, when a call
    was refused, `strata_rejected_tool_calls`. `usage_apart` is llama-server: the reason
    on one chunk and the usage on a chunk with no choices, and a whole reply that keeps a
    call the output ended inside. `breaks` says per request where a stream goes wrong and,
    in a pair, how: `error` is the launcher's frame after the headers went (`EngineDied`,
    `ValueError`), `drop` the connection aborting, `clean` an orderly end with no finish word."""

    def __init__(self, *, calls=(), finish="stop", rejected=None, usage_apart=False, breaks=(), timings=LAUNCHER_TIMINGS,
                 after_last=False, with_usage=True, **kwargs):
        super().__init__(**kwargs)
        self.calls, self.finish, self.rejected, self.timings = calls, finish, rejected, timings
        self.usage_apart, self.after_last, self.with_usage = usage_apart, after_last, with_usage
        self.breaks = list(breaks)
        self.raw = []

    async def _chat(self, request):
        self.raw.append(await request.read())
        return await super()._chat(request)

    def _usage(self):
        return {"prompt_tokens": 285, "completion_tokens": 52, "total_tokens": 337,
                "prompt_tokens_details": {"cached_tokens": 0}}

    def _last_fields(self):
        fields = {"usage": self._usage()} if self.with_usage else {}
        if self.timings:
            fields["timings"] = self.timings
        if self.rejected:
            fields["strata_rejected_tool_calls"] = self.rejected
        return fields

    async def _whole(self):
        await asyncio.sleep(self.delay)
        reasoning, content = "".join(self.reasoning), "".join(self.content)
        if reasoning.strip() and reasoning.strip() == content.strip():
            reasoning = ""
        calls = [{"id": c["id"], "type": "function",
                  "function": {"name": c["name"], "arguments": "".join(c["pieces"])}} for c in self.calls]
        if self.finish != "tool_calls" and not self.usage_apart:
            calls = [c for c in calls if _parses(c["function"]["arguments"])]
        message = {"role": "assistant", "content": content or None}
        if reasoning:
            message["reasoning_content"] = reasoning
        if calls:
            message["tool_calls"] = calls
        return web.json_response({
            "id": "chatcmpl-engine", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": message, "finish_reason": self.finish}],
            **self._last_fields(),
        })

    async def _stream(self, request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        breaks_at = self.breaks.pop(0) if self.breaks else None
        breaks_at, how = breaks_at if isinstance(breaks_at, tuple) else (breaks_at, "error")

        async def send(payload):
            await response.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
            await asyncio.sleep(self.delay)

        async def end():
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        async def broke(where):
            if breaks_at != where:
                return False
            if how == "drop":
                request.transport.abort()
            elif how == "error":
                await send({"error": {"type": "server_error", "message": "connection reset by the engine"}})
            elif how == "refusal":
                await send({"error": {"type": "server_error", "message": "the engine refused this request"}})
            return True

        await send(self._chunk({"role": "assistant", "content": None if self.usage_apart else ""}))
        await response.write(b": keep-alive\n\n")
        if await broke("role"):
            return await end()
        for piece in self.reasoning:
            await send(self._chunk({"reasoning_content": piece}))
            if await broke("reasoning"):
                return await end()
        if self.content_gate is not None:
            await self.content_gate.wait()
        for piece in self.content:
            await send(self._chunk({"content": piece}))
            if await broke("content"):
                return await end()
        for index, call in enumerate(self.calls):
            await send(self._chunk({"tool_calls": [{"index": index, "id": call["id"], "type": "function",
                                                    "function": {"name": call["name"], "arguments": ""}}]}))
            for piece in call["pieces"]:
                await send(self._chunk({"tool_calls": [{"index": index, "function": {"arguments": piece}}]}))
                if await broke("call"):
                    return await end()
        if self.usage_apart:
            await send(self._chunk({}, self.finish))
            await send({**self._chunk({}), "choices": [], **self._last_fields()})
        else:
            await send({**self._chunk({}, self.finish), **self._last_fields()})
        if self.after_last:
            await send({**self._chunk({}), "choices": []})
        return await end()


def _parses(text):
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _called(*calls, **kwargs):
    """What a double is given for a turn that ends on calls."""
    return {"calls": calls or (CALL_IN_PIECES,), "finish": "tool_calls", **kwargs}


@pytest.fixture(params=["llamacpp_server", "strata"])
def local(request, tmp_path):
    """Both classes, each with its engine's shape: (double's kwargs, provider's kwargs)."""
    if request.param == "strata":
        return {}, _strata_kwargs(tmp_path)
    return {"usage_apart": True}, {}


def _strata_kwargs(tmp_path):
    return {"provider_class": StrataProvider, "model": "qwen3.8-flash-next-iq3_xxs",
            "strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
            "conversation_cache_mib": 10240}


@pytest.mark.parametrize("content", [CONTENT, ()], ids=["with-prose", "calls-alone"])
@pytest.mark.asyncio
async def test_a_stream_with_tools_gives_the_deltas_as_made_and_the_call_whole_at_the_end(tmp_path, local, content):
    engine_kwargs, provider_kwargs = local
    status, text, engine, _ = await _door_call(
        tmp_path, {**engine_kwargs, **_called(reasoning=REASONING, content=content)},
        _chat(stream=True, tools=TOOLS_OPENAI), engine_class=_Launcher, provider_kwargs=provider_kwargs,
    )
    assert status == 200, text
    assert engine.requests[0]["stream"] is True
    deltas = [delta for delta in _deltas(text) if delta]
    assert deltas[0] == {"role": "assistant", "reasoning_content": REASONING[0]}
    assert _field(deltas, "reasoning_content") == list(REASONING), "the pieces, and once: nothing again at the end"
    assert _field(deltas, "content") == list(content)
    assert deltas[-1] == {"tool_calls": [CALL_ON_THE_WIRE]}, "a call in one piece, parsed"
    assert len(_field(deltas, "tool_calls")) == 1
    keys = [next(k for k in delta if k != "role") for delta in deltas]
    assert keys == ["reasoning_content"] * 3 + ["content"] * len(content) + ["tool_calls"]
    assert "```tool_call" not in text, "the plain path's folded call reached a client as text"
    assert _events(text)[-1]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_a_reasoning_delta_beside_tools_leaves_while_the_model_is_still_thinking(tmp_path, local):
    engine_kwargs, provider_kwargs = local
    gate = asyncio.Event()
    async with _Launcher(**engine_kwargs, **_called(reasoning=REASONING, content=CONTENT, content_gate=gate)) as engine:
        provider = _local_provider(engine, **provider_kwargs)
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, _):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(stream=True, tools=TOOLS_OPENAI))
                await client.send()
                seen = await client.read_until(REASONING[-1].encode("utf-8"))
                assert b'"content"' not in seen and not engine.finished
                assert provider.supervisor._in_flight == 1, "the stream runs outside the call slot"
                assert _CARD_HOLDS.get(ALIAS), "the stream runs with no hold on the card"
                gate.set()
                rest = await client.read_until(b"[DONE]")
                await client.leave()
                assert b"answer" in rest and b"read_file" in rest
        finally:
            gate.set()
            _forget(provider)


@pytest.mark.asyncio
async def test_the_messages_form_beside_tools_streams_its_text_and_ends_on_a_whole_call(tmp_path, local):
    engine_kwargs, provider_kwargs = local
    status, text, _, _ = await _door_call(
        tmp_path, {**engine_kwargs, **_called(reasoning=REASONING, content=CONTENT)},
        _messages(stream=True, tools=TOOLS_ANTHROPIC), path=MESSAGES_PATH,
        engine_class=_Launcher, provider_kwargs=provider_kwargs,
    )
    assert status == 200, text
    events = _events(text)
    assert [e["delta"]["text"] for e in events if e.get("delta", {}).get("type") == "text_delta"] == list(CONTENT)
    (start,) = [e["content_block"] for e in events if e.get("content_block", {}).get("type") == "tool_use"]
    assert start["name"] == "read_file"
    (piece,) = [e["delta"]["partial_json"] for e in events if e.get("delta", {}).get("type") == "input_json_delta"]
    assert json.loads(piece) == {"path": "a.txt"}
    assert "think " not in text, "this form hands over no reasoning"


@pytest.mark.asyncio
async def test_the_request_on_a_stream_is_the_request_on_the_whole_path_and_two_keys(tmp_path, local):
    engine_kwargs, provider_kwargs = local
    async with _Launcher(**engine_kwargs, **_called(content=CONTENT)) as engine:
        provider = _local_provider(engine, **provider_kwargs)
        try:
            turns = [{"role": "user", "content": "hi"}]
            await provider.generate_with_tools(turns, TOOLS_ANTHROPIC, reasoning_effort="high", tool_choice="none")
            trace = _Trace()
            await provider.generate_with_tools(turns, TOOLS_ANTHROPIC, reasoning_effort="high", tool_choice="none",
                                               on_chunk=trace.chunk, on_reasoning=trace.reasoning)
        finally:
            _forget(provider)
    whole, streamed = engine.requests
    assert streamed.pop("stream_options") == {"include_usage": True} and streamed.pop("stream") is True
    assert streamed == whole


# --- (2a) the result is the one the whole path gives ---------------------------------------


def _comparable(result):
    """The result without what is our clock's: the engine's words on the call all stay."""
    usage = dict(result["usage"])
    speed = {k: v for k, v in usage.pop("speed").items() if k not in ("elapsed_s", "total_tok_s")}
    return {**result, "usage": {**usage, "speed": speed},
            "tool_calls_raw": [vars(call) for call in result["tool_calls_raw"]]}


CUT_CALL = {"id": "call_cut", "name": "read_file", "pieces": ('{"path"', ': "a.t')}
TWO_CALLS = (CALL_IN_PIECES, {"id": "call_2", "name": "read_file", "pieces": ('{"path": "b.txt"}',)})
SAME_RESULT = {
    "call": _called(reasoning=REASONING, content=CONTENT),
    "calls-alone": _called(reasoning=REASONING, content=()),
    "two-calls": _called(*TWO_CALLS, content=CONTENT),
    "no-reasoning": _called(content=CONTENT),
    "text-only": {"reasoning": REASONING, "content": CONTENT},
    "refused-call": {"reasoning": REASONING, "content": CONTENT,
                     "rejected": [{"name": "ghost", "reason": "undeclared"}]},
    "cut-in-a-call": {"calls": (CALL_IN_PIECES, CUT_CALL), "finish": "length", "content": CONTENT},
    "cut-in-prose": {"reasoning": REASONING, "content": CONTENT, "finish": "length"},
    "no-think-end": {"reasoning": ("the same text",), "content": ("the same text",)},
    "unparsed-under-tool-calls": _called(CUT_CALL, content=CONTENT),
    "a-chunk-after-the-last": {"content": CONTENT, "after_last": True,
                               "rejected": [{"name": "ghost", "reason": "undeclared"}]},
}


async def _both_paths(engine_kwargs, provider_kwargs):
    """(the whole path's result, the stream's, the stream's trace)."""
    turns = [{"role": "user", "content": "hi"}]
    async with _Launcher(**engine_kwargs) as engine:
        provider = _local_provider(engine, **{"max_tokens": 64, **provider_kwargs})
        try:
            whole = await provider.generate_with_tools(turns, TOOLS_ANTHROPIC)
            thinking_after_whole = provider.get_last_thinking()
            trace = _Trace()
            streamed = await provider.generate_with_tools(
                turns, TOOLS_ANTHROPIC, on_chunk=trace.chunk, on_reasoning=trace.reasoning,
            )
            assert provider.get_last_thinking() == thinking_after_whole
            assert (provider.get_last_usage() or {}) == streamed["usage"]
        finally:
            _forget(provider)
    return whole, streamed, trace


@pytest.mark.parametrize("scenario", SAME_RESULT, ids=str)
@pytest.mark.asyncio
async def test_the_stream_returns_the_result_the_whole_path_returns(tmp_path, local, scenario):
    engine_kwargs, provider_kwargs = local
    whole, streamed, trace = await _both_paths({**engine_kwargs, **SAME_RESULT[scenario]}, provider_kwargs)
    assert _comparable(streamed) == _comparable(whole)
    assert "".join(trace.of("content")) == whole["content"], "what was written is the text the result holds"


@pytest.mark.asyncio
async def test_the_engines_words_on_the_call_arrive_from_either_shape_of_stream(tmp_path, local):
    engine_kwargs, provider_kwargs = local
    _, streamed, _ = await _both_paths({**engine_kwargs, **_called(reasoning=REASONING, content=CONTENT)},
                                       provider_kwargs)
    usage = streamed["usage"]
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (285, 52)
    assert (usage["finish_reason"], usage["max_tokens"]) == ("tool_calls", 64)
    assert (usage["prefilled_tokens"], usage["cached_tokens"]) == (285, 0)
    assert (usage["prompt_ms"], usage["predicted_ms"]) == (1240.5, 457.4)
    assert usage["speed"]["draft_acceptance"] == 0.75
    assert usage["speed"]["speed_source"] == "engine"
    assert usage["reasoning_tokens"] > 0 and usage["thinking_source"] == "estimated"


@pytest.mark.asyncio
async def test_with_no_engine_timings_the_stream_splits_the_speed_at_its_first_chunk(tmp_path, local):
    engine_kwargs, provider_kwargs = local
    whole, streamed, _ = await _both_paths({**engine_kwargs, **_called(content=CONTENT, timings=None)}, provider_kwargs)
    assert "prefill_tok_s" not in whole["usage"]["speed"], "a call with no first chunk has no such boundary"
    assert streamed["usage"]["speed"]["prefill_tok_s"] > 0
    assert "speed_source" not in streamed["usage"]["speed"]


@pytest.mark.asyncio
async def test_the_callbacks_are_told_the_conversation_and_the_text_is_kept_without_a_chunk_callback(tmp_path, local):
    engine_kwargs, provider_kwargs = local
    async with _Launcher(**engine_kwargs, **_called(reasoning=REASONING, content=CONTENT)) as engine:
        provider = _local_provider(engine, **provider_kwargs)
        try:
            turns = [{"role": "user", "content": "hi"}]
            reasoning, chunk = AsyncMock(), AsyncMock()
            await provider.generate_with_tools(turns, TOOLS_ANTHROPIC, conversation_id="conv",
                                               on_chunk=chunk, on_reasoning=reasoning)
            assert reasoning.await_args_list[0].args == (REASONING[0], "conv")
            assert chunk.await_args_list[0].args == (CONTENT[0], "conv")
            result = await provider.generate_with_tools(turns, TOOLS_ANTHROPIC, on_reasoning=reasoning)
            assert result["content"] == "".join(CONTENT)
        finally:
            _forget(provider)


@pytest.mark.asyncio
async def test_a_call_the_launcher_refused_is_named_from_the_last_chunk_and_absent_otherwise(tmp_path, local, caplog):
    engine_kwargs, provider_kwargs = local
    refused = [{"name": "ghost", "reason": "undeclared"}]
    with caplog.at_level("WARNING", logger="dpc_client_core.providers.llamacpp_server_provider"):
        _, streamed, _ = await _both_paths({**engine_kwargs, "content": CONTENT, "rejected": refused}, provider_kwargs)
    assert streamed["rejected_tool_calls"] == ["ghost"]
    assert caplog.text.count("not a call of a declared tool: ghost") == 2, "said on each path"
    _, streamed, _ = await _both_paths({**engine_kwargs, "content": CONTENT}, provider_kwargs)
    assert "rejected_tool_calls" not in streamed


@pytest.mark.asyncio
async def test_a_call_cut_at_the_limit_is_marked_on_llama_server_and_left_out_by_the_launcher(tmp_path):
    cut = {"calls": (CUT_CALL,), "finish": "length", "content": CONTENT}
    _, llama, _ = await _both_paths({**cut, "usage_apart": True}, {})
    (call,) = llama["tool_calls_raw"]
    assert call.input == {} and call.unreadable_arguments == '{"path": "a.t'
    assert llama["usage"]["finish_reason"] == "length"
    _, strata, _ = await _both_paths(cut, _strata_kwargs(tmp_path))
    assert strata["tool_calls_raw"] == [] and strata["usage"]["finish_reason"] == "length"


@pytest.mark.parametrize("usage_apart, delta", [
    (True, [{**CALL_ON_THE_WIRE, "id": "call_cut", "function": {"name": "read_file", "arguments": "{}"}}]), (False, None),
], ids=["llamacpp_server", "strata"])
@pytest.mark.asyncio
async def test_at_the_door_a_cut_call_is_empty_on_llama_server_and_absent_on_the_launcher_under_length(
    tmp_path, usage_apart, delta,
):
    _, text, _, _ = await _door_call(
        tmp_path, {"calls": (CUT_CALL,), "finish": "length", "content": CONTENT, "usage_apart": usage_apart},
        _chat(stream=True, tools=TOOLS_OPENAI), engine_class=_Launcher,
        provider_kwargs={} if usage_apart else _strata_kwargs(tmp_path),
    )
    assert (_field(_deltas(text), "tool_calls") or [None])[0] == delta
    assert _events(text)[-1]["choices"][0]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_a_callback_that_raises_closes_the_stream_to_the_engine(local):
    engine_kwargs, provider_kwargs = local
    many = tuple(f"thought{i} " for i in range(60))
    async with _Launcher(**engine_kwargs, reasoning=many, content=CONTENT, delay=0.02) as engine:
        provider = _local_provider(engine, **provider_kwargs)
        try:
            with pytest.raises(RuntimeError):
                await provider.generate_with_tools([{"role": "user", "content": "hi"}], TOOLS_ANTHROPIC,
                                                   on_reasoning=AsyncMock(side_effect=ValueError("no")))
            await _until(lambda: engine.left or engine.finished)
            assert engine.left == 1 and engine.finished == 0, "the engine went on for nobody"
        finally:
            _forget(provider)


@pytest.mark.asyncio
async def test_a_turn_that_closed_a_whole_call_at_the_limit_keeps_it_on_the_launcher(tmp_path):
    whole, streamed, _ = await _both_paths({"calls": (CALL_IN_PIECES,), "finish": "length"}, _strata_kwargs(tmp_path))
    assert [c.input for c in streamed["tool_calls_raw"]] == [{"path": "a.txt"}] == [
        c.input for c in whole["tool_calls_raw"]]


@pytest.mark.asyncio
async def test_the_row_of_a_streamed_call_with_tools_has_the_engines_word(tmp_path, local):
    engine_kwargs, provider_kwargs = local
    _, text, _, rows = await _door_call(
        tmp_path, {**engine_kwargs, **_called(reasoning=REASONING, content=CONTENT)},
        _chat(stream=True, tools=TOOLS_OPENAI, stream_options={"include_usage": True}),
        engine_class=_Launcher, provider_kwargs={"max_tokens": 64, **provider_kwargs},
    )
    (row,) = rows
    assert row["counts_source"] == "engine"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (285, 52)
    assert (row["finish_reason"], row["max_tokens"]) == ("tool_calls", 64)
    assert (row["prefilled_tokens"], row["cached_tokens"]) == (285, 0)
    assert row["thinking_tokens"] > 0 and row["output_includes_thinking"] == "includes"
    assert _events(text)[-1]["usage"]["completion_tokens"] == 52


@pytest.mark.asyncio
async def test_a_picture_beside_tools_stays_in_the_turn_and_the_call_streams(tmp_path, local, caplog):
    engine_kwargs, provider_kwargs = local

    def sees(provider):
        provider.supports_vision = lambda: True

    picture = [{"type": "text", "text": "what is this"},
               {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ONE_PIXEL}"}}]
    body = {"model": ALIAS, "stream": True, "tools": TOOLS_OPENAI, "messages": [{"role": "user", "content": picture}]}
    with caplog.at_level("INFO", logger="dpc_client_core.providers.llamacpp_server_provider"):
        status, text, engine, _ = await _door_call(
            tmp_path, {**engine_kwargs, **_called(reasoning=REASONING, content=CONTENT)}, body,
            prepare=sees, engine_class=_Launcher, provider_kwargs=provider_kwargs,
        )
    assert status == 200, text
    assert _field(_deltas(text), "reasoning_content") == list(REASONING)
    (sent,) = engine.requests
    assert sent["stream"] is True and "image_url" in json.dumps(sent["messages"])
    assert "images=1 tools=1, tool_calls=1" in caplog.text and "path=tools-stream" in caplog.text


# --- (2b) a stream that breaks, and a client that leaves ------------------------------------


@pytest.mark.asyncio
async def test_a_stream_that_breaks_before_any_delta_is_retried_and_says_everything_once(
    tmp_path, local, fast_backoff,  # noqa: F811
):
    engine_kwargs, provider_kwargs = local
    status, text, engine, rows = await _door_call(
        tmp_path, {**engine_kwargs, **_called(reasoning=REASONING, content=CONTENT, breaks=["role"])},
        _chat(stream=True, tools=TOOLS_OPENAI), engine_class=_Launcher, provider_kwargs=provider_kwargs,
    )
    assert status == 200, text
    assert len(engine.requests) == 2, "nothing was written, so asking again repeats nothing"
    deltas = _deltas(text)
    assert _field(deltas, "reasoning_content") == list(REASONING)
    assert _field(deltas, "content") == list(CONTENT)
    assert len(rows) == 1


@pytest.mark.parametrize("breaks_at", ["reasoning", "content"])
@pytest.mark.asyncio
async def test_a_stream_that_breaks_after_a_delta_is_not_asked_again_and_the_client_is_told(
    tmp_path, local, fast_backoff, breaks_at,  # noqa: F811
):
    engine_kwargs, provider_kwargs = local
    lock = asyncio.Semaphore(1)
    async with _Launcher(**engine_kwargs, **_called(reasoning=REASONING, content=CONTENT, breaks=[breaks_at]),
                         ) as engine:
        provider = _local_provider(engine, **provider_kwargs)
        try:
            async with _running(tmp_path, _service(tmp_path, provider), inference_lock=lock) as (server, _):
                status, text = await _post(server, tmp_path, CHAT_PATH, _chat(stream=True, tools=TOOLS_OPENAI))
            assert await _settled(engine) == 1, "a second generation would write its text behind the first's"
            assert not lock.locked() and provider.supervisor._in_flight == 0
        finally:
            _forget(provider)
    assert status == 200
    (error,) = [event["error"] for event in _events(text) if "error" in event]
    assert error["code"] == "upstream_error"
    assert text.count(REASONING[0]) == 1
    assert "tool_calls" not in text


@pytest.mark.asyncio
async def test_a_retry_that_writes_and_then_breaks_is_not_asked_again(tmp_path, local, fast_backoff):  # noqa: F811
    engine_kwargs, provider_kwargs = local
    _, text, engine, _ = await _door_call(
        tmp_path, {**engine_kwargs, **_called(content=CONTENT, breaks=["role", "content"])},
        _chat(stream=True, tools=TOOLS_OPENAI), engine_class=_Launcher, provider_kwargs=provider_kwargs,
    )
    assert len(engine.requests) == 2
    assert text.count(f'"content": "{CONTENT[0]}"') == 1


@pytest.mark.parametrize("breaks_at, content", [
    (("role", "drop"), CONTENT), (("role", "clean"), CONTENT), (("call", "drop"), ()), (("call", "clean"), ()),
], ids=["drop-in-prefill", "no-finish-in-prefill", "drop-in-a-call", "no-finish-in-a-call"])
@pytest.mark.asyncio
async def test_a_stream_gone_wrong_with_nothing_written_is_asked_again_and_says_it_once(
    tmp_path, local, fast_backoff, breaks_at, content,  # noqa: F811
):
    engine_kwargs, provider_kwargs = local
    status, text, engine, rows = await _door_call(
        tmp_path, {**engine_kwargs, **_called(content=content, breaks=[breaks_at])},
        _chat(stream=True, tools=TOOLS_OPENAI), engine_class=_Launcher, provider_kwargs=provider_kwargs,
    )
    assert status == 200, text
    assert len(engine.requests) == 2
    deltas = _deltas(text)
    assert _field(deltas, "content") == list(content)
    assert _field(deltas, "tool_calls") == [[CALL_ON_THE_WIRE]], "the half-written call was not lost"
    assert _events(text)[-1]["choices"][0]["finish_reason"] == "tool_calls"
    (row,) = rows
    assert row["counts_source"] == "engine"


@pytest.mark.parametrize("path, body", [
    (CHAT_PATH, _chat(stream=True, tools=TOOLS_OPENAI)), (MESSAGES_PATH, _messages(stream=True, tools=TOOLS_ANTHROPIC)),
], ids=["openai", "messages"])
@pytest.mark.parametrize("breaks_at", [("content", "drop"), ("content", "clean"), ("call", "clean")],
                         ids=["drop", "no-finish", "no-finish-in-a-call"])
@pytest.mark.asyncio
async def test_a_stream_gone_wrong_after_a_delta_is_not_asked_again_and_is_no_answer(
    tmp_path, local, fast_backoff, breaks_at, path, body,  # noqa: F811
):
    engine_kwargs, provider_kwargs = local
    status, text, engine, rows = await _door_call(
        tmp_path, {**engine_kwargs, **_called(content=CONTENT, breaks=[breaks_at])}, body, path=path,
        engine_class=_Launcher, provider_kwargs=provider_kwargs,
    )
    assert status == 200, text
    assert len(engine.requests) == 1
    assert [event for event in _events(text) if "error" in event], "the client was not told"
    assert "read_file" not in text
    assert rows == [], "a broken stream is not a served call"


@pytest.mark.parametrize("how", ["drop", "clean"])
@pytest.mark.asyncio
async def test_a_retry_that_writes_and_then_goes_wrong_is_not_asked_again(tmp_path, local, fast_backoff, how):  # noqa: F811
    engine_kwargs, provider_kwargs = local
    _, text, engine, _ = await _door_call(
        tmp_path, {**engine_kwargs, **_called(content=CONTENT, breaks=[("role", how), ("content", how)])},
        _chat(stream=True, tools=TOOLS_OPENAI), engine_class=_Launcher, provider_kwargs=provider_kwargs,
    )
    assert len(engine.requests) == 2
    assert text.count(f'"content": "{CONTENT[0]}"') == 1


@pytest.mark.asyncio
async def test_an_engine_that_never_finishes_a_stream_is_a_failure_and_no_row(tmp_path, local, fast_backoff):  # noqa: F811
    engine_kwargs, provider_kwargs = local
    status, text, engine, rows = await _door_call(
        tmp_path, {**engine_kwargs, **_called(content=CONTENT, breaks=[("role", "clean")] * 5000)},
        _chat(stream=True, tools=TOOLS_OPENAI), engine_class=_Launcher, provider_kwargs=provider_kwargs,
    )
    assert status == 502, text
    assert len(engine.requests) > 1
    assert rows == []


@pytest.mark.asyncio
async def test_an_engines_refusal_on_a_stream_is_not_asked_again(tmp_path, local, fast_backoff):  # noqa: F811
    """Only a broken transport joins the retried set: this frame says the same thing twice."""
    engine_kwargs, provider_kwargs = local
    status, text, engine, _ = await _door_call(
        tmp_path, {**engine_kwargs, **_called(content=CONTENT, breaks=[("role", "refusal")])},
        _chat(stream=True, tools=TOOLS_OPENAI), engine_class=_Launcher, provider_kwargs=provider_kwargs,
    )
    assert status == 502, text
    assert len(engine.requests) == 1


@pytest.mark.asyncio
async def test_a_finish_word_with_no_usage_is_an_answer_and_is_not_asked_again(tmp_path, local):
    """The whole path's rule for a reply with no usage: the engine's last chunk is the
    finish word, so only its absence reads as broken."""
    engine_kwargs, provider_kwargs = local
    whole, streamed, _ = await _both_paths({**engine_kwargs, **_called(content=CONTENT, with_usage=False)}, provider_kwargs)
    assert streamed["usage"] == whole["usage"] == {}
    assert streamed["content"] == "".join(CONTENT)
    assert [c.input for c in streamed["tool_calls_raw"]] == [{"path": "a.txt"}]


@pytest.mark.asyncio
async def test_a_call_served_to_a_peer_on_strata_is_not_asked_again_after_a_drop(tmp_path, fast_backoff):  # noqa: F811
    """ADR-044 point 9: a peer told so comes back on its own."""
    async with _Launcher(**_called(content=CONTENT, breaks=[("role", "drop")])) as engine:
        provider = _local_provider(engine, **_strata_kwargs(tmp_path))
        coordinator, wire = _host(tmp_path, provider)
        frames = []

        async def send(_peer_id, frame):
            frames.append(frame)

        wire.p2p_manager.send_message_to_peer = send
        try:
            await asyncio.create_task(coordinator.handle_inference_request(
                "peer-1", "req-1", "hi", provider=provider.alias,
                messages=[{"role": "user", "content": "hi"}], tools=TOOLS_ANTHROPIC, stream=True,
            ))
        finally:
            _forget(provider)
    assert len(engine.requests) == 1
    (answer,) = [f["payload"] for f in frames if f["command"] == "REMOTE_INFERENCE_RESPONSE"]
    assert answer.get("error") or answer.get("status") == "error"


@pytest.mark.parametrize("handler_cancellation", [True, False], ids=["connection-lost", "failing-write"])
@pytest.mark.asyncio
async def test_a_client_that_leaves_a_stream_with_tools_stops_the_engine_and_lets_go(
    tmp_path, local, monkeypatch, fast_backoff, handler_cancellation,  # noqa: F811
):
    if not handler_cancellation:
        runner = gateway_module.web.AppRunner
        monkeypatch.setattr(gateway_module.web, "AppRunner",
                            lambda app, **kw: runner(app, **{**kw, "handler_cancellation": False}))
    engine_kwargs, provider_kwargs = local
    many = tuple(f"thought{i} " for i in range(60))
    lock = asyncio.Semaphore(1)
    async with _Launcher(**engine_kwargs, **_called(reasoning=many, content=CONTENT, delay=0.02)) as engine:
        provider = _local_provider(engine, **provider_kwargs)
        try:
            async with _running(tmp_path, _service(tmp_path, provider), inference_lock=lock) as (server, ledger):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(stream=True, tools=TOOLS_OPENAI))
                await client.send()
                await client.read_until(b"thought1 ")
                await client.leave()
                await _until(lambda: not lock.locked())
                assert await _settled(engine) == 1, "the answer was generated again for nobody"
                assert engine.left == 1 and engine.finished == 0, "the engine went on for nobody"
                assert provider.supervisor._in_flight == 0, "the call slot is still held"
                assert not _CARD_HOLDS.get(ALIAS), "the card hold outlived the call"
                (row,) = list(ledger.rows())
                assert row["counts_source"] == "ours" and row["thinking_tokens"] > 0
        finally:
            _forget(provider)


# --- (2c) a host serving a guest's stream ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_host_sends_a_guests_text_as_it_is_made_and_the_calls_and_notes_on_the_last_frame(tmp_path, local):
    """The P2P door passes the callback too, on a stream a guest asked for."""
    engine_kwargs, provider_kwargs = local
    async with _Launcher(**engine_kwargs, **_called(reasoning=REASONING, content=CONTENT)) as engine:
        provider = _local_provider(engine, **provider_kwargs)
        coordinator, wire = _host(tmp_path, provider)
        frames = []

        async def send(_peer_id, frame):
            frames.append(frame)

        wire.p2p_manager.send_message_to_peer = send
        try:
            await asyncio.create_task(coordinator.handle_inference_request(
                "peer-1", "req-1", "hi", provider=provider.alias,
                messages=[{"role": "user", "content": "hi"}], tools=TOOLS_ANTHROPIC, stream=True,
            ))
        finally:
            _forget(provider)
    assert [f["payload"]["delta"] for f in frames if f["command"] == "REMOTE_INFERENCE_CHUNK"] == list(CONTENT)
    (answer,) = [f["payload"] for f in frames if f["command"] == "REMOTE_INFERENCE_RESPONSE"]
    assert answer["thinking"] == "".join(REASONING)
    assert [c["input"] for c in answer["tool_calls"]] == [{"path": "a.txt"}]
    if provider_kwargs:
        (sent,) = engine.requests
        assert sent["strata_checkpoint"] is False, "a peer's call parks nothing, on a stream too"


# --- (2d) nobody else moved ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agents_call_through_the_real_adapter_stays_one_whole_request(tmp_path, local):
    engine_kwargs, provider_kwargs = local
    async with _Launcher(**engine_kwargs, **_called(reasoning=REASONING, content=CONTENT)) as engine:
        provider = _local_provider(engine, **provider_kwargs)
        try:
            adapter = DpcLlmAdapter(SimpleNamespace(token_count_manager=None, providers={}), provider_alias="test")
            chunks = AsyncMock()
            message, _ = await adapter._chat_native_tools(
                provider, [{"role": "user", "content": "hi"}],
                [{"function": {"name": "read_file", "description": "", "parameters": {}}}],
                on_stream_chunk=chunks, conversation_id="conv",
            )
        finally:
            _forget(provider)
    (sent,) = engine.requests
    assert "stream" not in sent and "stream_options" not in sent
    assert engine.finished == 1
    assert [call.args for call in chunks.await_args_list] == [("".join(CONTENT), "conv")], "one whole chunk"
    assert message["thinking"] == "".join(REASONING)
    assert [json.loads(c["function"]["arguments"]) for c in message["tool_calls"]] == [{"path": "a.txt"}]


@pytest.mark.parametrize("body, whole", [
    ({"tools": TOOLS_OPENAI}, True), ({"stream": True, "tools": TOOLS_OPENAI}, False),
], ids=["whole", "stream"])
@pytest.mark.asyncio
async def test_a_door_call_streams_from_the_engine_only_when_the_client_asked(tmp_path, local, body, whole):
    engine_kwargs, provider_kwargs = local
    _, _, engine, _ = await _door_call(tmp_path, {**engine_kwargs, **_called(content=CONTENT)}, _chat(**body),
                                       engine_class=_Launcher, provider_kwargs=provider_kwargs)
    (sent,) = engine.requests
    assert ("stream" not in sent) is whole


def test_only_the_two_doors_can_turn_the_stream_on():
    """Above the providers. `llm_manager` passes on what it is given; the agent package
    names the callback nowhere, so no agent's call reaches `_call_on_a_stream`."""
    root = Path(inspect.getfile(LLMManager)).parent
    naming = {path.relative_to(root).as_posix() for path in root.rglob("*.py")
              if "on_reasoning" in path.read_text(encoding="utf-8") and path.parent.name != "providers"}
    assert naming == {"gateway.py", "llm_manager.py", "p2p_coordinator.py"}
    assert "generate_with_tools" in (root / "dpc_agent" / "llm_adapter.py").read_text(encoding="utf-8")


# --- (3) a whole message --------------------------------------------------------------


@pytest.mark.parametrize("extra", [{}, {"tools": TOOLS_OPENAI}], ids=["plain", "tools"])
@pytest.mark.asyncio
async def test_a_whole_message_carries_the_field_beside_the_content_and_the_count_is_unchanged(tmp_path, extra):
    status, text, _, rows = await _door_call(
        tmp_path, {"reasoning": REASONING, "content": CONTENT}, _chat(**extra),
    )
    assert status == 200
    body = json.loads(text)
    message = body["choices"][0]["message"]
    assert message["reasoning_content"] == "".join(REASONING)
    assert message["content"] == "".join(CONTENT)
    (row,) = rows
    assert body["usage"]["completion_tokens"] == row["completion_tokens"]


@pytest.mark.asyncio
async def test_a_picture_without_tools_answers_whole_and_its_reasoning_still_comes_first(tmp_path):
    def sees(provider):
        provider.supports_vision = lambda: True

    picture = [{"type": "text", "text": "what is this"},
               {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ONE_PIXEL}"}}]
    body = {"model": ALIAS, "stream": True, "messages": [{"role": "user", "content": picture}]}
    status, text, _, _ = await _door_call(
        tmp_path, {"reasoning": REASONING, "content": CONTENT}, body, prepare=sees,
    )
    assert status == 200, text
    deltas = [delta for delta in _deltas(text) if delta]
    assert deltas[0] == {"role": "assistant", "reasoning_content": "".join(REASONING)}
    assert deltas[1] == {"content": "".join(CONTENT)}


# --- (4) a provider with none: as it was ----------------------------------------------


@pytest.mark.asyncio
async def test_a_stream_with_no_reasoning_is_the_stream_it_always_was(tmp_path):
    _, text, _, _ = await _door_call(tmp_path, {"content": CONTENT}, _chat(stream=True))
    assert _deltas(text) == [{"role": "assistant", "content": "the "}, {"content": "answer"}, {}]
    assert "reasoning_content" not in text


@pytest.mark.asyncio
async def test_a_whole_message_with_no_reasoning_has_no_such_key(tmp_path):
    _, text, _, _ = await _door_call(tmp_path, {"content": CONTENT}, _chat())
    assert set(json.loads(text)["choices"][0]["message"]) == {"role", "content"}


class _TagsInTheText(AIProvider):
    """A model that writes `<think>` into its answer, on a provider with no stream:
    the manager hands the raw text to `on_chunk`, then parses the tags out."""

    def __init__(self):
        super().__init__(ALIAS, {"type": "ollama", "model": "m"})

    def supports_thinking(self):
        return True

    async def generate_response(self, prompt, **kwargs):
        return "<think>tagged notes</think>the answer"


@pytest.mark.asyncio
async def test_tags_already_written_as_text_on_a_stream_are_not_written_again_as_reasoning(tmp_path):
    async with _running(tmp_path, _service(tmp_path, _TagsInTheText())) as (server, _):
        _, text = await _post(server, tmp_path, CHAT_PATH, _chat(stream=True))
    deltas = _deltas(text)
    assert not _field(deltas, "reasoning_content")
    assert "tagged notes" in "".join(_field(deltas, "content"))


@pytest.mark.asyncio
async def test_tags_in_a_whole_answer_become_the_field(tmp_path):
    async with _running(tmp_path, _service(tmp_path, _TagsInTheText())) as (server, _):
        _, text = await _post(server, tmp_path, CHAT_PATH, _chat())
    message = json.loads(text)["choices"][0]["message"]
    assert message["reasoning_content"] == "tagged notes"
    assert message["content"] == "the answer"


# --- (5) the peer route ---------------------------------------------------------------


async def _peer_call(tmp_path, body, **service):
    async with _running(tmp_path, _peer_service(tmp_path, **service)) as (server, _):
        return await _post(server, tmp_path, CHAT_PATH, body)


@pytest.mark.asyncio
async def test_a_peer_whole_message_carries_what_the_host_sent(tmp_path):
    status, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL), result=_priced_result(thinking="the host's notes"))
    assert status == 200
    assert json.loads(text)["choices"][0]["message"]["reasoning_content"] == "the host's notes"


@pytest.mark.asyncio
async def test_a_peer_that_sends_no_chunk_gets_its_reasoning_in_front_of_the_text(tmp_path):
    _, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL, stream=True),
                               result=_priced_result(thinking="the host's notes"))
    deltas = [delta for delta in _deltas(text) if delta]
    assert deltas[0] == {"role": "assistant", "reasoning_content": "the host's notes"}
    assert deltas[1] == {"content": PEER_ANSWER}


@pytest.mark.asyncio
async def test_a_peer_stream_hands_the_reasoning_over_when_the_answer_frame_brings_it_after_the_chunks(tmp_path):
    """A chunk carries text and the reasoning rides the final frame, so it cannot
    come first without a wire change. Late is what arrives; it is not dropped."""
    _, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL, stream=True),
                               chunks=["hello ", "there"], result=_priced_result(thinking="the host's notes"))
    deltas = _deltas(text)
    assert deltas[0] == {"role": "assistant", "content": "hello "}
    assert deltas[1] == {"content": "there"}
    assert deltas[2] == {"reasoning_content": "the host's notes"}
    assert _events(text)[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("thinking", [None, "", 7, {"a": 1}], ids=["none", "empty", "number", "object"])
@pytest.mark.asyncio
async def test_a_host_whose_reasoning_is_not_text_sends_none_through_the_door(tmp_path, thinking):
    """The wire can carry anything, and this field is passed on to a third party's parser."""
    _, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL), result=_priced_result(thinking=thinking))
    assert "reasoning_content" not in json.loads(text)["choices"][0]["message"]


# --- (6) raw on purpose ----------------------------------------------------------------


# The shape of the five stored reasoning texts that hold call markup, out of 8356
# read on 2026-11-04 from `~/.dpc`: each is the model talking about a call, none
# makes one. This one is written for the test; none of them is copied here.
TALKING_ABOUT_A_CALL = 'My last turn was "<tool_call><function=read_file>..." — malformed, so it never ran. '


@pytest.mark.asyncio
async def test_call_markup_in_the_reasoning_reaches_the_client_verbatim_and_calls_nothing(tmp_path):
    """Cut at the marker, as the answer's rule cuts, the sentence that diagnoses the
    failure would go, and everything after it. See the amendment for the argument."""
    pieces = (TALKING_ABOUT_A_CALL, "Next I will read it properly.")
    _, text, _, _ = await _door_call(tmp_path, {"reasoning": pieces, "content": CONTENT}, _chat(stream=True))
    deltas = _deltas(text)
    assert _field(deltas, "reasoning_content") == list(pieces)
    assert not _field(deltas, "tool_calls")
    assert _events(text)[-1]["choices"][0]["finish_reason"] == "stop"


# --- (7) a client that sends it back --------------------------------------------------


NOTES = "SENT-BACK-NOTES"


def test_a_reasoning_content_on_an_assistant_turn_is_read_by_nobody():
    system, turns, _ = _openai_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "reasoning_content": NOTES},
        {"role": "user", "content": "more"},
    ])
    assert NOTES not in json.dumps(turns) and NOTES not in system


@pytest.mark.parametrize("extra", [{}, {"tools": TOOLS_OPENAI}, {"stream": True}], ids=["plain", "tools", "stream"])
@pytest.mark.asyncio
async def test_a_client_that_sends_it_back_is_answered_and_the_model_never_sees_it(tmp_path, extra):
    turn = {"role": "assistant", "content": "hello", "reasoning_content": NOTES}
    if "tools" in extra:
        turn["tool_calls"] = [ENGINE_CALL]
    body = {"model": ALIAS, "messages": [
        {"role": "user", "content": "hi"}, turn,
        *([{"role": "tool", "tool_call_id": "call_1", "content": "ok"}] if "tools" in extra else []),
        {"role": "user", "content": "more"},
    ], **extra}
    status, _, engine, _ = await _door_call(
        tmp_path, {"content": CONTENT}, body, provider_kwargs={"preserve_reasoning": True},
    )
    assert status == 200
    (sent,) = engine.requests
    assert NOTES not in json.dumps(sent), "the client's copy reached the model, as text or as a replay"


@pytest.mark.asyncio
async def test_deepseeks_replay_rule_stands_when_a_client_sends_it_back(tmp_path, monkeypatch):
    """The vendor refuses a replayed call turn with no reasoning, so the provider pads
    or looks up its own copy; what a client sent is neither of those."""
    async with _Engine(content=CONTENT) as engine:
        provider = _sdk_provider(DeepSeekProvider, engine, monkeypatch, {"model": "deepseek-v4-flash"})
        _, turns, _ = _openai_messages([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "reasoning_content": NOTES, "tool_calls": [ENGINE_CALL]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ])
        await provider.generate_with_tools(turns, TOOLS_ANTHROPIC)
    (sent,) = engine.requests
    (replayed,) = [m for m in sent["messages"] if m["role"] == "assistant"]
    assert replayed["reasoning_content"] == " "


# --- (8) the Messages form: left as it was ---------------------------------------------


@pytest.mark.parametrize("body", [_messages(), _messages(stream=True), _messages(tools=TOOLS_ANTHROPIC, stream=True)],
                         ids=["whole", "stream", "tools-stream"])
@pytest.mark.asyncio
async def test_the_messages_form_hands_over_no_thinking_block_and_no_reasoning_text(tmp_path, body):
    """A real block carries a signature this door cannot make. Question for Mike."""
    status, text, _, _ = await _door_call(
        tmp_path, {"reasoning": REASONING, "content": CONTENT}, body, path=MESSAGES_PATH,
    )
    assert status == 200
    assert '"thinking_delta"' not in text and '"type": "thinking"' not in text
    assert "think " not in text and "reasoning_content" not in text
    assert "answer" in text


# --- (9) every other consumer sees what it saw -----------------------------------------


def _manager(tmp_path, provider):
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": []}), encoding="utf-8")
    manager = LLMManager(config_path=config)
    manager.providers = {provider.alias: provider}
    manager.default_provider = provider.alias
    return manager


class _Trace:
    """Both callbacks on one list, so their order is what is read."""

    def __init__(self):
        self.seen = []

    async def chunk(self, text, conversation_id=None):
        self.seen.append(("content", text))

    async def reasoning(self, text, conversation_id=None):
        self.seen.append(("reasoning", text))

    def of(self, kind):
        return [text for what, text in self.seen if what == kind]


def _sdk_provider(cls, engine, monkeypatch, config):
    """A vendor class, its client aimed at the double: the real SDK reads the real frames."""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "not-a-real-key")
    provider = cls("vendor", {"api_key_env": "TEST_PROVIDER_KEY", **config})
    provider.client = AsyncOpenAI(api_key="k", base_url=f"http://127.0.0.1:{engine.port}/v1")
    return provider


VENDOR_CLASSES = [
    (DeepSeekProvider, {"model": "deepseek-v4-flash"}),
    (ZaiProvider, {"model": "glm-4.7"}),
    (NeuralDeepProvider, {"model": "qwen3.8-27b"}),
]


@pytest.fixture
def _neuraldeep_is_quiet(monkeypatch):
    async def _no_balance(self):
        return {"billing_mode": "wallet"}

    monkeypatch.setattr(NeuralDeepProvider, "get_balance", _no_balance)


@pytest.mark.parametrize("cls, config", VENDOR_CLASSES, ids=lambda v: getattr(v, "__name__", None))
@pytest.mark.parametrize("tools", [None, TOOLS_ANTHROPIC], ids=["stream", "tools"])
@pytest.mark.asyncio
async def test_a_vendor_class_hands_it_to_the_door_and_to_nobody_else(
    tmp_path, monkeypatch, _neuraldeep_is_quiet, cls, config, tools,
):
    async with _Engine(reasoning=REASONING, content=CONTENT) as engine:
        provider = _sdk_provider(cls, engine, monkeypatch, config)
        manager = _manager(tmp_path, provider)
        turns = [{"role": "user", "content": "hi"}]

        door = _Trace()
        result = await manager.query_messages(
            turns, tools=tools, on_chunk=door.chunk, on_reasoning=door.reasoning, return_metadata=True,
        )
        assert result["thinking"] == "".join(REASONING)
        assert door.seen[0][0] == "reasoning", "the reasoning came after what it precedes"
        assert "".join(door.of("reasoning")) == "".join(REASONING)
        assert "".join(door.of("content")) == "".join(CONTENT)
        if tools is None:
            assert door.of("reasoning") == list(REASONING)

        agent = _Trace()
        result = await manager.query_messages(turns, tools=tools, on_chunk=agent.chunk, return_metadata=True)
        assert [what for what, _ in agent.seen] == ["content"] * len(agent.seen)
        assert "".join(agent.of("content")) == "".join(CONTENT)
        assert result["thinking"] == "".join(REASONING), "the record the agent reads did not move"


@pytest.mark.parametrize("tools", [None, TOOLS_ANTHROPIC], ids=["stream", "tools"])
@pytest.mark.asyncio
async def test_the_local_class_gives_the_agent_loop_what_it_gave(tmp_path, tools):
    async with _Engine(reasoning=REASONING, content=CONTENT) as engine:
        provider = _local_provider(engine)
        try:
            manager = _manager(tmp_path, provider)
            agent = _Trace()
            result = await manager.query_messages(
                [{"role": "user", "content": "hi"}], tools=tools, on_chunk=agent.chunk, return_metadata=True,
            )
            assert [what for what, _ in agent.seen] == ["content"] * len(agent.seen)
            assert "".join(agent.of("content")) == "".join(CONTENT)
            assert result["thinking"] == "".join(REASONING)
        finally:
            _forget(provider)


@pytest.mark.asyncio
async def test_ollama_on_the_tools_path_hands_it_over_in_front_of_the_content(tmp_path, monkeypatch):
    from tests.test_ollama_says_what_the_call_cost import _answered, _make

    from dpc_client_core.providers import ollama_provider as op

    monkeypatch.setattr(op.ollama, "Client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no daemon")))
    provider = _make()
    provider.client.chat = _answered("the answer", "my notes", "stop")
    door = _Trace()
    await provider.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=[],
                                       on_chunk=door.chunk, on_reasoning=door.reasoning)
    assert door.seen == [("reasoning", "my notes"), ("content", "the answer")]


class _StoredThinking(AIProvider):
    """The shape of Anthropic's and Ollama's prompt door: no stream, the reasoning
    stored on the instance by `generate_response` and read back by the manager."""

    def __init__(self, thinking="stored notes", supports=True):
        super().__init__(ALIAS, {"type": "anthropic", "model": "m"})
        self.thinking, self.supports = thinking, supports

    def supports_thinking(self):
        return self.supports

    def get_last_thinking(self):
        return self.thinking

    async def generate_response(self, prompt, **kwargs):
        assert "on_reasoning" not in kwargs, "this entry point names no such parameter"
        return "the answer"


@pytest.mark.asyncio
async def test_a_provider_with_no_stream_hands_its_stored_reasoning_over_before_the_whole_answer(tmp_path):
    manager = _manager(tmp_path, _StoredThinking())
    door = _Trace()
    await manager.query_messages([{"role": "user", "content": "hi"}], on_chunk=door.chunk,
                                 on_reasoning=door.reasoning, return_metadata=True)
    assert door.seen == [("reasoning", "stored notes"), ("content", "the answer")]


@pytest.mark.parametrize("provider", [_StoredThinking(thinking=None), _StoredThinking(supports=False)],
                         ids=["nothing-stored", "does-not-think"])
@pytest.mark.asyncio
async def test_a_provider_with_none_to_give_sends_none(tmp_path, provider):
    manager = _manager(tmp_path, provider)
    door = _Trace()
    await manager.query_messages([{"role": "user", "content": "hi"}], on_chunk=door.chunk,
                                 on_reasoning=door.reasoning, return_metadata=True)
    assert door.seen == [("content", "the answer")]


class _StreamWithKwargs(AIProvider):
    """A stream whose `**kwargs` would swallow the callback and forward it to a vendor."""

    def __init__(self):
        super().__init__(ALIAS, {"type": "gemini", "model": "m"})
        self.kwargs = None

    async def generate_response_stream(self, prompt, on_chunk=None, conversation_id=None, **kwargs):
        self.kwargs = kwargs
        await on_chunk("the answer", conversation_id)
        return "the answer"


@pytest.mark.asyncio
async def test_an_entry_point_that_does_not_name_the_parameter_is_never_handed_it(tmp_path):
    provider = _StreamWithKwargs()
    door = _Trace()
    await _manager(tmp_path, provider).query_messages(
        [{"role": "user", "content": "hi"}], on_chunk=door.chunk, on_reasoning=door.reasoning, return_metadata=True,
    )
    assert provider.kwargs == {}
    assert door.seen == [("content", "the answer")]


def _reads_reasoning_inside(method) -> bool:
    """Whether the method's own body keeps a reasoning text: what makes a class one
    that has something to give on this path."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    return any(
        (isinstance(node, ast.Constant) and node.value in ("reasoning_content", "thinking"))
        or (isinstance(node, ast.Attribute) and node.attr == "_last_thinking")
        for node in ast.walk(tree)
    )


def test_every_class_that_keeps_reasoning_on_a_door_path_takes_the_callback_by_keyword():
    """A new provider cannot join the door's paths and stay silent unseen. By keyword
    only, so no positional caller of these signatures can be moved by it."""
    checked = []
    for cls in {c for c in PROVIDER_MAP.values() if isinstance(c, type)}:
        for name in ("generate_with_tools", "generate_response_stream"):
            method = getattr(cls, name, None)
            if method is None or not _reads_reasoning_inside(method):
                continue
            parameter = inspect.signature(method).parameters.get("on_reasoning")
            assert parameter is not None, f"{cls.__name__}.{name} keeps reasoning and has no way to hand it over"
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is None
            checked.append(f"{cls.__name__}.{name}")
    assert {"LlamaServerProvider.generate_response_stream", "LlamaServerProvider.generate_with_tools",
            "DeepSeekProvider.generate_response_stream", "DeepSeekProvider.generate_with_tools",
            "ZaiProvider.generate_response_stream", "ZaiProvider.generate_with_tools",
            "NeuralDeepProvider.generate_response_stream", "NeuralDeepProvider.generate_with_tools",
            "OllamaProvider.generate_with_tools"} <= set(checked)


# --- (10) with defect B: the reasoning is what now notices the client leaving ----------


@pytest.mark.asyncio
async def test_a_client_gone_during_the_reasoning_is_found_by_the_reasoning_write_and_nothing_runs_again(
    tmp_path, fast_backoff, no_handler_cancellation,  # noqa: F811
):
    many = tuple(f"thought{i} " for i in range(60))
    lock = asyncio.Semaphore(1)
    async with _Engine(reasoning=many, content=CONTENT, delay=0.02) as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider), inference_lock=lock) as (server, _):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(stream=True))
                await client.send()
                await client.read_until(b"thought1 ")
                await client.leave()
                await _until(lambda: not lock.locked())
                assert await _settled(engine) == 1, "the answer was generated again for nobody"
                assert engine.left == 1, "the engine went on reasoning for nobody"
        finally:
            _forget(provider)


# --- (11) an answer that is the reasoning is said once --------------------------------


def _ollama_at_the_door(monkeypatch, content, thinking):
    from tests.test_ollama_says_what_the_call_cost import _answered, _Resp

    from dpc_client_core.providers import ollama_provider as op

    monkeypatch.setattr(op.ollama, "Client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no daemon")))
    provider = op.OllamaProvider(ALIAS, {"type": "ollama", "model": "m", "host": "http://127.0.0.1:11434"})
    provider.supports_thinking = lambda: True
    provider.client.chat = _answered(content, thinking, "stop")
    assert _Resp
    return provider


NO_ANSWER_BODIES = [_chat(), _chat(stream=True), _chat(tools=TOOLS_OPENAI), _chat(stream=True, tools=TOOLS_OPENAI)]


@pytest.mark.parametrize("body", NO_ANSWER_BODIES, ids=["plain", "plain-stream", "tools", "tools-stream"])
@pytest.mark.asyncio
async def test_ollama_with_no_answer_promotes_its_reasoning_once_as_the_answer(tmp_path, monkeypatch, body):
    """`generate_response` and `generate_with_tools` return the reasoning as the content when
    the model answered nothing. It is the answer then, and it is not also reasoning."""
    provider = _ollama_at_the_door(monkeypatch, "", "only my notes")
    async with _running(tmp_path, _service(tmp_path, provider)) as (server, _):
        status, text = await _post(server, tmp_path, CHAT_PATH, body)
    assert status == 200, text
    assert "reasoning_content" not in text
    assert text.count("only my notes") == 1


@pytest.mark.parametrize("body", NO_ANSWER_BODIES, ids=["plain", "plain-stream", "tools", "tools-stream"])
@pytest.mark.asyncio
async def test_ollama_with_an_answer_hands_its_reasoning_over_apart_from_it(tmp_path, monkeypatch, body):
    provider = _ollama_at_the_door(monkeypatch, "the answer", "my notes")
    async with _running(tmp_path, _service(tmp_path, provider)) as (server, _):
        status, text = await _post(server, tmp_path, CHAT_PATH, body)
    assert status == 200, text
    assert text.count("my notes") == 1 and "reasoning_content" in text
    assert text.count("the answer") == 1


@pytest.mark.asyncio
async def test_a_host_that_promoted_its_reasoning_is_not_read_twice_at_the_guests_door(tmp_path):
    same = _priced_result(response="only my notes", thinking="only my notes")
    _, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL), result=same)
    message = json.loads(text)["choices"][0]["message"]
    assert message["content"] == "only my notes" and "reasoning_content" not in message
    _, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL, stream=True), chunks=["only my notes"], result=same)
    assert "reasoning_content" not in text
