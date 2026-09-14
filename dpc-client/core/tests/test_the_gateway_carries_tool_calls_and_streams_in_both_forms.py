"""A tool call crosses the gateway, and the answer arrives as it is produced.

ADR-041; the two board entries A-TOOL-CALL-CANNOT-CROSS-THE-GATEWAY-SO-CLAUDE-CODE-
TALKS-AND-CANNOT-EDIT and THE-ANSWER-ARRIVES-WHOLE-SO-AN-EDITOR-SHOWS-A-BLANK-BOX-
UNTIL-IT-DOES. Both HTTP shapes now carry `tools` to `LLMManager.query_messages`
and render a returned call back — a `tool_use` block under `stop_reason:
tool_use` in the Messages form, a `tool_calls` entry under `finish_reason:
tool_calls` in the OpenAI form — and a `tool_result` / `role: tool` turn on the
next request reaches the provider layer as the Anthropic block the providers
already convert. `stream: true` on the local route writes each chunk the door
hands back as it arrives; the peer route's own half of this — tools over the
wire and a real stream — is pinned in
test_the_peer_route_is_no_narrower_than_the_local_one.py.

The internal shape is the Anthropic Messages one, because it is the only
shape the provider layer takes un-flattened (`generate_with_tools`). The
OpenAI form converts on the way in and on the way out, and the edge that
matters is tested both ways: `arguments` is a JSON *string* on the OpenAI
wire and an *object* inside.

The stand-in service scripts what the door returns — a tool call, a run of
chunks, a failure after the first chunk — and records what it was given; the
listener is a real `aiohttp` `TCPSite` on port 0. Cross-platform: pure asyncio.
"""

import json
import types

import aiohttp
import pytest

from dpc_client_core.firewall import ContextFirewall
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    REMOTE_MODEL,
    _peer_service,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    ANSWER,
    BOTH_LISTS,
    INFERENCE_TIMEOUT_S,
    LOCAL,
    MAX_IMAGE_MB,
    NODE_ID,
    VENDOR,
    _Provider,
    _key,
    _request,
    _running,
    _write_rules,
)
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _anthropic_error,
    _post_messages,
    _sse_events,
)

BARE = "prompt_only"  # a served alias whose provider has no native tool path
SYSTEM = "be brief"
READ_FILE = {
    "name": "read_file",
    "description": "read a file",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}
OPENAI_READ_FILE = {
    "type": "function",
    "function": {"name": "read_file", "description": "read a file", "parameters": READ_FILE["input_schema"]},
}
CALL = {"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {"path": "a.txt", "lines": [1, 2]}}


class _ToolProvider(_Provider):
    """A provider with a native tool path; the gateway only asks whether it exists."""

    async def generate_with_tools(self, *args, **kwargs):
        raise AssertionError("the gateway speaks to the manager, never to the provider")


class _BareProvider:
    """A provider with a prompt and nothing else: no `generate_with_tools`."""

    def __init__(self, type_, model):
        self.config = {"type": type_, "model": model}
        self.model = model


def _tool_service(tmp_path, compute=BOTH_LISTS, *, text=ANSWER, tool_calls=(), chunks=None,
                  finish_reason=None, thinking_tokens=None, fail_after_chunks=None):
    """A stand-in with both doors and a scripted `query_messages`: it hands
    `chunks` (or the whole text) to `on_chunk` when one is given, then returns
    the manager's dict with the scripted tool calls and stop reason."""
    rules = _write_rules(tmp_path, compute)
    providers = {
        LOCAL: _ToolProvider("ollama", "qwen3:8b"),
        VENDOR: _ToolProvider("deepseek", "deepseek-v4-flash"),
        BARE: _BareProvider("ollama", "phi3"),
    }
    calls = []

    async def query(prompt, provider_alias=None, return_metadata=False, **kwargs):
        raise AssertionError("both HTTP shapes go through query_messages now")

    async def query_messages(messages, *, system="", tools=None, on_chunk=None,
                             provider_alias=None, return_metadata=False, **kwargs):
        calls.append({"messages": messages, "system": system, "tools": tools,
                      "alias": provider_alias, "streamed": on_chunk is not None})
        if on_chunk is not None:
            for chunk in (chunks if chunks is not None else [text]):
                await on_chunk(chunk, None)
            if fail_after_chunks is not None:
                raise fail_after_chunks
        return {
            "response": text, "provider": provider_alias, "model": providers[provider_alias].model,
            "tokens_used": 17, "prompt_tokens": 12, "response_tokens": 5, "model_max_tokens": 4096,
            "vision_used": False, "thinking": None, "thinking_tokens": thinking_tokens,
            "output_includes_thinking": "excludes",
            "streamed": on_chunk is not None, "flattened": False, "tools_used": bool(tools),
            "tool_calls": list(tool_calls) if tools else [], "finish_reason": finish_reason,
        }

    return types.SimpleNamespace(
        firewall=ContextFirewall(rules),
        llm_manager=types.SimpleNamespace(providers=providers, query=query, query_messages=query_messages),
        p2p_manager=types.SimpleNamespace(node_id=NODE_ID),
        settings=types.SimpleNamespace(
            get_remote_inference_timeout=lambda: INFERENCE_TIMEOUT_S,
            get_vision_max_image_size_mb=lambda: MAX_IMAGE_MB,
        ),
        calls=calls,
    )


THREE_LISTS = {"serving_local": [LOCAL, BARE], "serving_vendor": [VENDOR], "vendor_quotas": {VENDOR: 2.0}}


def _messages_body(model, messages, **extra):
    body = {"model": model, "max_tokens": 256, "system": SYSTEM, "messages": messages}
    body.update(extra)
    return body


def _chat_body(model, messages, **extra):
    body = {"model": model, "messages": [{"role": "system", "content": SYSTEM}, *messages]}
    body.update(extra)
    return body


async def _post_chat(server, body, *, key):
    return await _request(server, "POST", "/v1/chat/completions", key=key, body=body)


async def _stream_text(server, path, body, *, key):
    url = f"http://127.0.0.1:{server.port}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers={"x-api-key": key}) as resp:
            assert resp.status == 200, await resp.text()
            assert resp.content_type == "text/event-stream"
            return await resp.text()


def _data_lines(text):
    return [line[len("data: "):] for line in text.split("\n") if line.startswith("data: ")]


# --- (1) the Messages form: tools in, a tool_use block out, a tool_result back in ----


@pytest.mark.asyncio
async def test_a_messages_request_with_tools_gets_a_tool_use_block_and_the_tool_result_completes_the_round_trip(tmp_path):
    service = _tool_service(tmp_path, text="reading", tool_calls=[CALL])
    first = _messages_body(LOCAL, [{"role": "user", "content": "read a.txt"}],
                           tools=[READ_FILE], tool_choice={"type": "auto"})
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(server, first, key=_key(tmp_path))
        assert status == 200, text
        answer = json.loads(text)
        assert answer["content"] == [{"type": "text", "text": "reading"}, CALL]
        assert answer["stop_reason"] == "tool_use"
        assert answer["usage"] == {"input_tokens": 12, "output_tokens": 5}
        (call,) = service.calls
        assert call["tools"] == [READ_FILE], "the tool schema did not reach the door as sent"
        assert call["messages"] == first["messages"] and call["system"] == SYSTEM
        (row,) = ledger.rows()
        assert (row["caller_kind"], row["route"], row["alias"]) == ("gateway", "local", LOCAL)
        assert (row["prompt_tokens"], row["completion_tokens"]) == (12, 5)
        assert row["output_includes_thinking"] == "excludes"
        assert answer["id"] == "msg_" + row["request_id"]

        # The client runs the tool and comes back with the result: the turn reaches
        # the door as the Anthropic block, which is what the providers convert.
        service.calls.clear()
        second = _messages_body(LOCAL, [
            {"role": "user", "content": "read a.txt"},
            {"role": "assistant", "content": answer["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "the file says hi"}]},
        ], tools=[READ_FILE])
        service_text_only = service  # the scripted door returns the same call again; the shape is what is tested
        status, text = await _post_messages(server, second, key=_key(tmp_path))
        assert status == 200, text
        (call,) = service_text_only.calls
        assert call["messages"][1]["content"][1] == CALL
        assert call["messages"][2]["content"][0]["type"] == "tool_result"
        assert len(list(ledger.rows())) == 2


@pytest.mark.asyncio
async def test_a_call_with_no_text_is_one_tool_use_block_and_the_stop_reason_is_tool_use_even_when_the_provider_named_none(tmp_path):
    service = _tool_service(tmp_path, text="", tool_calls=[CALL], finish_reason=None)
    body = _messages_body(LOCAL, [{"role": "user", "content": "read a.txt"}], tools=[READ_FILE])
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(server, body, key=_key(tmp_path))
        assert status == 200, text
        answer = json.loads(text)
        assert answer["content"] == [CALL], "an empty text block was put beside the call"
        assert answer["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_tool_choice_none_keeps_the_tools_from_the_door_and_any_tool_or_no_parallel_is_refused(tmp_path):
    service = _tool_service(tmp_path, tool_calls=[CALL])
    key_message = [{"role": "user", "content": "hi"}]
    async with _running(tmp_path, service) as (server, ledger):
        key = _key(tmp_path)
        status, text = await _post_messages(
            server, _messages_body(LOCAL, key_message, tools=[READ_FILE], tool_choice={"type": "none"}), key=key)
        assert status == 200, text
        assert json.loads(text)["content"] == [{"type": "text", "text": ANSWER}]
        assert service.calls[0]["tools"] is None, "tool_choice none still handed the tools to the door"

        for choice in ({"type": "any"}, {"type": "tool", "name": "read_file"},
                       {"type": "auto", "disable_parallel_tool_use": True}):
            status, text = await _post_messages(
                server, _messages_body(LOCAL, key_message, tools=[READ_FILE], tool_choice=choice), key=key)
            assert status == 400, (choice, text)
            error = _anthropic_error(text)
            assert error["type"] == "invalid_request_error" and "tool_choice" in error["message"], choice
        assert len(service.calls) == 1 and len(list(ledger.rows())) == 1


@pytest.mark.asyncio
async def test_tools_on_an_alias_whose_provider_has_no_native_tool_path_are_refused_by_name_in_both_envelopes(tmp_path):
    service = _tool_service(tmp_path, THREE_LISTS)
    async with _running(tmp_path, service) as (server, ledger):
        key = _key(tmp_path)
        status, text = await _post_messages(
            server, _messages_body(BARE, [{"role": "user", "content": "hi"}], tools=[READ_FILE]), key=key)
        assert status == 400, text
        error = _anthropic_error(text)
        assert error["type"] == "invalid_request_error" and BARE in error["message"]

        status, text = await _post_chat(
            server, _chat_body(BARE, [{"role": "user", "content": "hi"}], tools=[OPENAI_READ_FILE]), key=key)
        assert status == 400, text
        assert BARE in json.loads(text)["error"]["message"]
        assert service.calls == [] and list(ledger.rows()) == []

        # Without tools the same alias is served as before.
        status, _ = await _post_messages(server, _messages_body(BARE, [{"role": "user", "content": "hi"}]), key=key)
        assert status == 200


@pytest.mark.asyncio
async def test_a_server_tool_type_in_the_tools_list_is_refused_rather_than_dropped(tmp_path):
    service = _tool_service(tmp_path)
    body = _messages_body(LOCAL, [{"role": "user", "content": "hi"}],
                          tools=[{"type": "web_search_20260209", "name": "web_search"}])
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(server, body, key=_key(tmp_path))
        assert status == 400, text
        assert "web_search" in _anthropic_error(text)["message"]
        assert service.calls == []


# --- (2) the OpenAI form: both directions of the conversion -------------------------------


@pytest.mark.asyncio
async def test_an_openai_request_with_tools_is_converted_in_and_the_call_is_rendered_out_with_arguments_as_a_json_string(tmp_path):
    service = _tool_service(tmp_path, text="", tool_calls=[CALL])
    body = _chat_body(LOCAL, [{"role": "user", "content": "read a.txt"}], tools=[OPENAI_READ_FILE], tool_choice="auto")
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_chat(server, body, key=_key(tmp_path))
        assert status == 200, text
        answer = json.loads(text)
        (choice,) = answer["choices"]
        assert choice["finish_reason"] == "tool_calls"
        message = choice["message"]
        assert message["role"] == "assistant" and message["content"] is None
        (tool_call,) = message["tool_calls"]
        assert (tool_call["id"], tool_call["type"], tool_call["function"]["name"]) == ("toolu_01", "function", "read_file")
        arguments = tool_call["function"]["arguments"]
        assert isinstance(arguments, str), "arguments left the gateway as an object; the OpenAI wire wants a JSON string"
        assert json.loads(arguments) == CALL["input"]
        assert answer["usage"]["completion_tokens"] == 5

        # In: the OpenAI tool schema became the Anthropic one the providers take.
        (call,) = service.calls
        assert call["tools"] == [READ_FILE]
        assert call["system"] == SYSTEM
        assert call["messages"] == [{"role": "user", "content": "read a.txt"}]
        assert len(list(ledger.rows())) == 1


@pytest.mark.asyncio
async def test_an_openai_history_with_tool_calls_and_tool_turns_becomes_tool_use_and_batched_tool_result_blocks(tmp_path):
    service = _tool_service(tmp_path)
    history = [
        {"role": "user", "content": "read both"},
        {"role": "assistant", "content": "reading", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})}},
            {"id": "call_2", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"b.txt\"}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "A"},
        {"role": "tool", "tool_call_id": "call_2", "content": "B"},
        {"role": "user", "content": [{"type": "text", "text": "and now?"}]},
    ]
    body = _chat_body(LOCAL, history, tools=[OPENAI_READ_FILE])
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_chat(server, body, key=_key(tmp_path))
        assert status == 200, text
        (call,) = service.calls
        assert call["messages"] == [
            {"role": "user", "content": "read both"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "reading"},
                {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.txt"}},
                {"type": "tool_use", "id": "call_2", "name": "read_file", "input": {"path": "b.txt"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "A"},
                {"type": "tool_result", "tool_use_id": "call_2", "content": "B"},
            ]},
            {"role": "user", "content": [{"type": "text", "text": "and now?"}]},
        ]
        # The answer with no tool call is the plain shape, unchanged.
        choice = json.loads(text)["choices"][0]
        assert choice["message"] == {"role": "assistant", "content": ANSWER} and choice["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_openai_tool_fields_that_cannot_be_honoured_are_400_and_none_keeps_the_tools_from_the_door(tmp_path):
    service = _tool_service(tmp_path, tool_calls=[CALL])
    user = [{"role": "user", "content": "hi"}]
    async with _running(tmp_path, service) as (server, ledger):
        key = _key(tmp_path)
        status, text = await _post_chat(server, _chat_body(LOCAL, user, tools=[OPENAI_READ_FILE], tool_choice="none"), key=key)
        assert status == 200, text
        assert service.calls[0]["tools"] is None
        assert json.loads(text)["choices"][0]["message"]["content"] == ANSWER

        refused = (
            dict(tools=[OPENAI_READ_FILE], tool_choice="required"),
            dict(tools=[OPENAI_READ_FILE], tool_choice={"type": "function", "function": {"name": "read_file"}}),
            dict(tools=[OPENAI_READ_FILE], parallel_tool_calls=False),
            dict(functions=[OPENAI_READ_FILE["function"]]),
            dict(tools=[{"type": "function", "function": {"description": "no name"}}]),
        )
        for extra in refused:
            status, text = await _post_chat(server, _chat_body(LOCAL, user, **extra), key=key)
            assert status == 400, (extra, text)
            assert json.loads(text)["error"]["type"] == "invalid_request_error", extra

        # A tool call in the history whose arguments are not JSON is the client's error, said so.
        bad = [{"role": "user", "content": "x"},
               {"role": "assistant", "content": None, "tool_calls": [
                   {"id": "c", "type": "function", "function": {"name": "read_file", "arguments": "{not json"}}]},
               {"role": "tool", "tool_call_id": "c", "content": "?"}]
        status, text = await _post_chat(server, _chat_body(LOCAL, bad, tools=[OPENAI_READ_FILE]), key=key)
        assert status == 400, text
        assert "arguments" in json.loads(text)["error"]["message"]
        assert len(service.calls) == 1 and len(list(ledger.rows())) == 1


# --- (3) real streaming on the local route, both forms -------------------------------------


@pytest.mark.asyncio
async def test_an_openai_stream_writes_each_chunk_as_it_arrives_then_the_stop_word_and_the_usage(tmp_path):
    service = _tool_service(tmp_path, text="hello", chunks=["hel", "lo"], finish_reason="stop")
    async with _running(tmp_path, service) as (server, ledger):
        body = _chat_body(LOCAL, [{"role": "user", "content": "hi"}], stream=True)
        text = await _stream_text(server, "/v1/chat/completions", body, key=_key(tmp_path))
        lines = _data_lines(text)
        assert lines[-1] == "[DONE]"
        chunks = [json.loads(line) for line in lines[:-1]]
        assert service.calls[0]["streamed"], "the door was not given a chunk callback"
        assert [c["choices"][0]["delta"] for c in chunks] == [
            {"role": "assistant", "content": "hel"}, {"content": "lo"}, {},
        ]
        assert [c["choices"][0]["finish_reason"] for c in chunks] == [None, None, "stop"]
        assert all(c["object"] == "chat.completion.chunk" and c["model"] == LOCAL for c in chunks)
        (row,) = ledger.rows()
        assert {c["id"] for c in chunks} == {"chatcmpl-" + row["request_id"]}
        # Without stream_options the usage rides on the stop chunk, as it did before.
        assert chunks[0]["usage"] is None and chunks[1]["usage"] is None
        assert chunks[2]["usage"]["completion_tokens"] == row["completion_tokens"] == 5


@pytest.mark.asyncio
async def test_include_usage_moves_the_usage_to_a_final_chunk_with_no_choices_whose_counts_are_the_rows(tmp_path):
    service = _tool_service(tmp_path, text="hello", chunks=["hel", "lo"], thinking_tokens=3)
    async with _running(tmp_path, service) as (server, ledger):
        body = _chat_body(LOCAL, [{"role": "user", "content": "hi"}], stream=True,
                          stream_options={"include_usage": True})
        text = await _stream_text(server, "/v1/chat/completions", body, key=_key(tmp_path))
        chunks = [json.loads(line) for line in _data_lines(text)[:-1]]
        assert [len(c["choices"]) for c in chunks] == [1, 1, 1, 0]
        assert all(c["usage"] is None for c in chunks[:-1])
        (row,) = ledger.rows()
        usage = chunks[-1]["usage"]
        # The M1 / board falsifier: the envelope's output count is the row's
        # completion plus thinking, for the same request_id.
        assert chunks[-1]["id"] == "chatcmpl-" + row["request_id"]
        assert usage["completion_tokens"] == row["completion_tokens"] + row["thinking_tokens"] == 8
        assert usage["completion_tokens_details"] == {"reasoning_tokens": 3}
        assert usage["prompt_tokens"] == row["prompt_tokens"] == 12
        assert usage["total_tokens"] == 20


@pytest.mark.asyncio
async def test_an_openai_stream_with_a_tool_call_streams_the_text_then_the_call_as_one_chunk_then_tool_calls(tmp_path):
    service = _tool_service(tmp_path, text="reading", chunks=["read", "ing"], tool_calls=[CALL])
    async with _running(tmp_path, service) as (server, _):
        body = _chat_body(LOCAL, [{"role": "user", "content": "read a.txt"}], stream=True, tools=[OPENAI_READ_FILE])
        text = await _stream_text(server, "/v1/chat/completions", body, key=_key(tmp_path))
        chunks = [json.loads(line) for line in _data_lines(text)[:-1]]
        deltas = [c["choices"][0]["delta"] for c in chunks]
        assert deltas[:2] == [{"role": "assistant", "content": "read"}, {"content": "ing"}]
        (tool_call,) = deltas[2]["tool_calls"]
        assert (tool_call["index"], tool_call["id"], tool_call["type"]) == (0, "toolu_01", "function")
        assert tool_call["function"]["name"] == "read_file"
        assert json.loads(tool_call["function"]["arguments"]) == CALL["input"]
        assert deltas[3] == {}
        assert [c["choices"][0]["finish_reason"] for c in chunks] == [None, None, None, "tool_calls"]


@pytest.mark.asyncio
async def test_a_messages_stream_writes_a_text_delta_per_chunk_and_the_counts_in_message_delta(tmp_path):
    service = _tool_service(tmp_path, text="hello", chunks=["hel", "lo"], thinking_tokens=3)
    async with _running(tmp_path, service) as (server, ledger):
        body = _messages_body(LOCAL, [{"role": "user", "content": "hi"}], stream=True)
        text = await _stream_text(server, "/v1/messages", body, key=_key(tmp_path))
        events = _sse_events(text)
        assert [name for name, _ in events] == [
            "message_start", "content_block_start", "content_block_delta", "content_block_delta",
            "content_block_stop", "message_delta", "message_stop",
        ]
        (row,) = ledger.rows()
        start = events[0][1]["message"]
        assert start["id"] == "msg_" + row["request_id"] and start["content"] == []
        # The head leaves before the door has counted anything: the cumulative
        # usage on message_delta is where the counts stand (the wire allows it).
        assert start["usage"] == {"input_tokens": 0, "output_tokens": 0}
        assert [e["delta"] for _, e in events[2:4]] == [
            {"type": "text_delta", "text": "hel"}, {"type": "text_delta", "text": "lo"},
        ]
        delta = events[5][1]
        assert delta["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
        assert delta["usage"] == {"input_tokens": 12, "output_tokens": 8, "output_tokens_details": {"thinking_tokens": 3}}
        assert delta["usage"]["output_tokens"] == row["completion_tokens"] + row["thinking_tokens"]


@pytest.mark.asyncio
async def test_a_messages_stream_with_a_tool_call_closes_the_text_block_and_emits_the_call_as_one_tool_use_block(tmp_path):
    service = _tool_service(tmp_path, text="reading", chunks=["reading"], tool_calls=[CALL])
    async with _running(tmp_path, service) as (server, _):
        body = _messages_body(LOCAL, [{"role": "user", "content": "read a.txt"}], stream=True, tools=[READ_FILE])
        text = await _stream_text(server, "/v1/messages", body, key=_key(tmp_path))
        events = _sse_events(text)
        assert [name for name, _ in events] == [
            "message_start", "content_block_start", "content_block_delta", "content_block_stop",
            "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop",
        ]
        assert events[4][1] == {"type": "content_block_start", "index": 1,
                                "content_block": {"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {}}}
        delta = events[5][1]
        assert delta["index"] == 1 and delta["delta"]["type"] == "input_json_delta"
        assert json.loads(delta["delta"]["partial_json"]) == CALL["input"]
        assert events[6][1] == {"type": "content_block_stop", "index": 1}
        assert events[7][1]["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_a_door_that_fails_after_the_first_byte_ends_the_stream_with_an_error_event_not_a_hang(tmp_path):
    service = _tool_service(tmp_path, chunks=["hel"], fail_after_chunks=RuntimeError("the card fell over"))
    async with _running(tmp_path, service) as (server, ledger):
        key = _key(tmp_path)
        text = await _stream_text(server, "/v1/messages", _messages_body(LOCAL, [{"role": "user", "content": "hi"}], stream=True), key=key)
        events = _sse_events(text)
        assert [name for name, _ in events][:3] == ["message_start", "content_block_start", "content_block_delta"]
        assert events[-1][0] == "error"
        assert events[-1][1]["error"]["type"] == "api_error" and "the card fell over" in events[-1][1]["error"]["message"]

        text = await _stream_text(server, "/v1/chat/completions", _chat_body(LOCAL, [{"role": "user", "content": "hi"}], stream=True), key=key)
        lines = _data_lines(text)
        assert lines[-1] == "[DONE]"
        assert "the card fell over" in json.loads(lines[-2])["error"]["message"]
        assert list(ledger.rows()) == [], "a call that produced no answer is not a row"


# --- (4) the peer route: a menu row that claims no tool path is refused off the menu ---------------


@pytest.mark.asyncio
async def test_tools_on_a_peer_alias_whose_menu_claims_no_tool_path_are_refused_before_the_round_trip(tmp_path):
    """The wire carries tools now, so what is refused here is the peer's own
    word: a menu row without `supports_tools` (DPTP §3.5) reads as no, and the
    guest says so rather than spending a round trip the host will refuse."""
    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        key = _key(tmp_path)
        status, text = await _post_messages(
            server, _messages_body(REMOTE_MODEL, [{"role": "user", "content": "hi"}], tools=[READ_FILE]), key=key)
        assert status == 400, text
        error = _anthropic_error(text)
        assert error["type"] == "invalid_request_error"
        assert "supports_tools" in error["message"] and "mythos" in error["message"]

        status, text = await _post_chat(
            server, _chat_body(REMOTE_MODEL, [{"role": "user", "content": "hi"}], tools=[OPENAI_READ_FILE]), key=key)
        assert status == 400, text
        assert json.loads(text)["error"]["code"] == "tools_unsupported"
        assert service.peer_calls == [] and list(ledger.rows()) == []
