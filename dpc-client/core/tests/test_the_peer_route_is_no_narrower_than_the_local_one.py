"""A guest on the peer route gets what a local caller gets: turns, tools, stream.

Board entry THE-PEER-WIRE-CARRIES-ONE-PROMPT-SO-A-GUEST-GETS-NO-TOOLS-NO-STREAM-
AND-NO-CONVERSATION. `remote:<node>:<alias>` through the ADR-041 gateway used to
flatten the conversation into one prompt, refuse tools by name and answer in one
chunk, while the same node's local route carried all three. DPTP v1.7 put
`messages`, `system`, `tools` and `stream` on REMOTE_INFERENCE_REQUEST,
`tool_calls` and `finish_reason` on the response, and REMOTE_INFERENCE_CHUNK on
the wire between them.

Pinned here: tools cross and come back rendered in both HTTP shapes; a stream is
several deltas whose sum is the final text, with the usage of the final event
equal to the ledger row; `prompt` still equals `flatten_messages(messages,
system)` over the very turns that travel beside it, which is the whole of the
old-host compatibility story; a menu row that claims no tool path is refused
before the round trip; a request too large for one frame is 413 by name; and the
chunk handler drops what it cannot place.

The peer is the fake `request_inference_from_peer` the peer-route test file
builds, scripted with chunks. Cross-platform: pure asyncio.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.llm_manager import flatten_messages
from dpc_client_core.message_handlers.inference_handler import RemoteInferenceChunkHandler
from tests.test_the_gateway_carries_tool_calls_and_streams_in_both_forms import (
    CALL,
    OPENAI_READ_FILE,
    READ_FILE,
    SYSTEM,
    _chat_body,
    _data_lines,
    _messages_body,
    _post_chat,
    _stream_text,
)
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER,
    PEER_ANSWER,
    REMOTE_ALIAS,
    REMOTE_MODEL,
    WIRE_ID,
    _peer_service,
    _priced_result,
    _rows,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import _key, _running
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _post_messages,
    _sse_events,
)

TURNS = [{"role": "user", "content": "read a.txt"}]
PIECES = ["hello from ", "the peer's ", "card"]


def _tool_serving_peer(tmp_path, **kwargs):
    """A peer whose menu row says its alias calls tools natively (DPTP §3.5)."""
    service = _peer_service(tmp_path, **kwargs)
    for row in service.peer_metadata[PEER]["providers"]:
        row["supports_tools"] = True
    return service


# --- (1) tools cross the wire and come back rendered in both shapes -------------------


@pytest.mark.asyncio
async def test_tools_reach_the_host_and_a_returned_call_is_a_tool_use_block(tmp_path):
    service = _tool_serving_peer(tmp_path, result=_priced_result(
        response="reading", tool_calls=[CALL], finish_reason="tool_calls",
    ))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(
            server, _messages_body(REMOTE_MODEL, TURNS, tools=[READ_FILE]), key=_key(tmp_path))
        assert status == 200, text
        answer = json.loads(text)
        assert answer["content"] == [{"type": "text", "text": "reading"}, CALL]
        assert answer["stop_reason"] == "tool_use"

        (call,) = service.peer_calls
        assert call["tools"] == [READ_FILE], "the tool schema did not reach the wire as sent"
        assert call["messages"] == TURNS
        assert call["system"] == SYSTEM
        assert call["on_chunk"] is None, "nobody asked to be streamed to"
        (row,) = _rows(ledger)
        assert row["route"] == "peer" and row["request_id"] == WIRE_ID


@pytest.mark.asyncio
async def test_the_openai_shape_converts_the_same_call_at_this_edge(tmp_path):
    """`arguments` is a JSON string on that wire and an object inside — the one
    conversion this door makes, and it makes it for a peer's call too."""
    service = _tool_serving_peer(tmp_path, result=_priced_result(
        response="reading", tool_calls=[CALL], finish_reason="tool_calls",
    ))
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_chat(
            server, _chat_body(REMOTE_MODEL, TURNS, tools=[OPENAI_READ_FILE]), key=_key(tmp_path))
        assert status == 200, text
        choice = json.loads(text)["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        (rendered,) = choice["message"]["tool_calls"]
        assert rendered["function"]["name"] == "read_file"
        assert json.loads(rendered["function"]["arguments"]) == CALL["input"]

        (call,) = service.peer_calls
        assert call["tools"] == [READ_FILE], "converted to the internal Anthropic shape once"


@pytest.mark.asyncio
async def test_a_host_that_called_nothing_still_answers_text(tmp_path):
    service = _tool_serving_peer(tmp_path)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(
            server, _messages_body(REMOTE_MODEL, TURNS, tools=[READ_FILE]), key=_key(tmp_path))
        assert status == 200, text
        answer = json.loads(text)
        assert answer["content"] == [{"type": "text", "text": PEER_ANSWER}]
        assert answer["stop_reason"] == "end_turn"


# --- (2) the stream: several deltas, and the record is the final frame -----------------


@pytest.mark.asyncio
async def test_a_peer_stream_is_several_deltas_whose_sum_is_the_answer(tmp_path):
    service = _tool_serving_peer(tmp_path, chunks=PIECES)
    async with _running(tmp_path, service) as (server, ledger):
        text = await _stream_text(server, "/v1/chat/completions",
                                  _chat_body(REMOTE_MODEL, TURNS, stream=True), key=_key(tmp_path))
        lines = _data_lines(text)
        assert lines[-1] == "[DONE]"
        chunks = [json.loads(line) for line in lines[:-1]]
        deltas = [c["choices"][0]["delta"].get("content", "") for c in chunks]
        assert "".join(deltas) == PEER_ANSWER
        assert len([d for d in deltas if d]) == len(PIECES), "one event per chunk from the host"

        (call,) = service.peer_calls
        assert call["on_chunk"] is not None, "asking for chunks is what sets stream on the wire"
        (row,) = _rows(ledger)
        usage = chunks[-1]["usage"]
        assert (usage["prompt_tokens"], usage["completion_tokens"]) == (
            row["prompt_tokens"], row["completion_tokens"]
        ), "the counts are the response's, never counted off the deltas"


@pytest.mark.asyncio
async def test_the_messages_form_streams_the_same_deltas_into_one_text_block(tmp_path):
    service = _tool_serving_peer(tmp_path, chunks=PIECES)
    async with _running(tmp_path, service) as (server, ledger):
        text = await _stream_text(server, "/v1/messages",
                                  _messages_body(REMOTE_MODEL, TURNS, stream=True), key=_key(tmp_path))
        events = _sse_events(text)
        deltas = [body["delta"]["text"] for name, body in events if name == "content_block_delta"]
        assert "".join(deltas) == PEER_ANSWER
        assert len(deltas) == len(PIECES)
        assert [name for name, _ in events][0] == "message_start"
        assert [name for name, _ in events][-1] == "message_stop"
        assert len(_rows(ledger)) == 1, "one call, one row, however many chunks it took"


@pytest.mark.asyncio
async def test_a_host_that_refuses_before_the_first_chunk_opens_no_stream_and_leaves_no_row(tmp_path):
    """A refused call emits no chunk on either side of the wire, so the stream
    never opens and the refusal is an ordinary status. The guest's row is a
    mirror of the response, and there is none."""
    service = _tool_serving_peer(tmp_path, fail=RuntimeError("compute sharing disabled"))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_chat(
            server, _chat_body(REMOTE_MODEL, TURNS, stream=True), key=_key(tmp_path))
        assert status == 502, text
        assert "compute sharing disabled" in text
        assert _rows(ledger) == []


# --- (3) the flatten invariant: one source for the prompt and the turns ----------------


@pytest.mark.asyncio
async def test_the_prompt_on_the_wire_is_the_flattening_of_the_turns_beside_it(tmp_path):
    """The whole of old-host compatibility: a host that ignores `messages` reads
    `prompt` and must find the same conversation there."""
    service = _tool_serving_peer(tmp_path)
    turns = [
        {"role": "user", "content": "read a.txt"},
        {"role": "assistant", "content": "reading"},
        {"role": "user", "content": "and b.txt"},
    ]
    async with _running(tmp_path, service) as (server, _):
        status, _ = await _post_messages(
            server, _messages_body(REMOTE_MODEL, turns), key=_key(tmp_path))
        assert status == 200
        status, _ = await _post_chat(server, _chat_body(REMOTE_MODEL, turns), key=_key(tmp_path))
        assert status == 200

    assert len(service.peer_calls) == 2
    for call in service.peer_calls:
        assert call["prompt"] == flatten_messages(call["messages"], call["system"])
        assert [turn["role"] for turn in call["messages"]] == [t["role"] for t in turns]
        assert "and b.txt" in call["prompt"] and "reading" in call["prompt"]


# --- (4) what the menu denies, and what does not fit one frame -------------------------


@pytest.mark.asyncio
async def test_a_menu_row_without_supports_tools_refuses_tools_before_the_round_trip(tmp_path):
    service = _peer_service(tmp_path)  # the default rows say nothing about tools
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_chat(
            server, _chat_body(REMOTE_MODEL, TURNS, tools=[OPENAI_READ_FILE]), key=_key(tmp_path))
        assert status == 400, text
        error = json.loads(text)["error"]
        assert error["code"] == "tools_unsupported"
        assert "supports_tools" in error["message"] and REMOTE_ALIAS in error["message"]
        assert service.peer_calls == [] and _rows(ledger) == []


@pytest.mark.asyncio
async def test_tools_beside_an_image_are_still_refused_on_the_peer_route(tmp_path):
    """The host's vision door takes no tools, and the wire carries the image
    beside the prompt rather than in its turn, so the combination is refused
    rather than answered without the tools. The local route now serves it
    (test_a_screenshot_crosses_the_gateway_beside_the_tools_in_its_own_turn.py);
    this route is the next step of the same card."""
    service = _tool_serving_peer(tmp_path)
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "iVBORw0KGgo="}}
    body = _messages_body(REMOTE_MODEL, [{"role": "user", "content": [image, {"type": "text", "text": "hi"}]}],
                          tools=[READ_FILE])
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(server, body, key=_key(tmp_path))
        assert status == 400, text
        assert "image" in text and "tool" in text
        assert service.peer_calls == [] and _rows(ledger) == []


@pytest.mark.asyncio
async def test_a_request_too_large_for_one_frame_is_413_by_name(tmp_path):
    """`write_message` raises at the origin (DPTP §2), before a byte leaves;
    tools and a long conversation together can reach the cap."""
    service = _tool_serving_peer(tmp_path, fail=ValueError(
        "Message of 70000000 bytes exceeds the 67108864-byte frame cap"))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_chat(
            server, _chat_body(REMOTE_MODEL, TURNS, tools=[OPENAI_READ_FILE]), key=_key(tmp_path))
        assert status == 413, text
        message = json.loads(text)["error"]["message"]
        assert "frame cap" in message and PEER in message
        assert _rows(ledger) == [], "nothing ran, so nothing is recorded"


# --- (5) the chunk handler places a chunk or drops it ----------------------------------


def _chunk_service(callback=None):
    service = MagicMock()
    service._pending_inference_chunks = {WIRE_ID: callback} if callback is not None else {}
    return service


@pytest.mark.asyncio
async def test_a_chunk_reaches_the_callback_that_asked_for_it():
    seen = []

    async def on_chunk(delta):
        seen.append(delta)

    handler = RemoteInferenceChunkHandler(_chunk_service(on_chunk))
    for seq, piece in enumerate(PIECES):
        await handler.handle(PEER, {"request_id": WIRE_ID, "seq": seq, "delta": piece})

    assert seen == PIECES


@pytest.mark.asyncio
async def test_a_chunk_for_a_request_nobody_is_waiting_for_is_dropped():
    handler = RemoteInferenceChunkHandler(_chunk_service())

    assert await handler.handle(PEER, {"request_id": "stranger", "seq": 0, "delta": "x"}) is None


@pytest.mark.asyncio
async def test_a_chunk_out_of_order_is_dropped_and_does_not_advance_the_count():
    """No reordering and no buffering: the answer is in the frame that ends the
    stream, so a gap here costs nothing but the live text."""
    seen = []

    async def on_chunk(delta):
        seen.append(delta)

    handler = RemoteInferenceChunkHandler(_chunk_service(on_chunk))
    await handler.handle(PEER, {"request_id": WIRE_ID, "seq": 0, "delta": "a"})
    await handler.handle(PEER, {"request_id": WIRE_ID, "seq": 5, "delta": "skipped"})
    await handler.handle(PEER, {"request_id": WIRE_ID, "seq": 1, "delta": "b"})

    assert seen == ["a", "b"]


@pytest.mark.asyncio
async def test_a_callback_that_raises_does_not_take_the_connection_down():
    async def on_chunk(delta):
        raise RuntimeError("the client hung up")

    handler = RemoteInferenceChunkHandler(_chunk_service(on_chunk))

    assert await handler.handle(PEER, {"request_id": WIRE_ID, "seq": 0, "delta": "a"}) is None


@pytest.mark.asyncio
async def test_the_seq_count_of_a_finished_request_does_not_outlive_it():
    seen = []

    async def on_chunk(delta):
        seen.append(delta)

    service = _chunk_service(on_chunk)
    handler = RemoteInferenceChunkHandler(service)
    await handler.handle(PEER, {"request_id": WIRE_ID, "seq": 0, "delta": "a"})
    # The coordinator drops the callback with the pending future, and a later
    # request reusing the id starts counting from zero again.
    service._pending_inference_chunks.clear()
    await handler.handle(PEER, {"request_id": WIRE_ID, "seq": 0, "delta": "gone"})
    assert handler._next_seq == {}

    service._pending_inference_chunks[WIRE_ID] = on_chunk
    await handler.handle(PEER, {"request_id": WIRE_ID, "seq": 0, "delta": "b"})
    assert seen == ["a", "b"]


# --- (6) the coordinator asks for a stream only when someone will read it --------------


@pytest.mark.asyncio
async def test_the_coordinator_sets_stream_only_when_a_callback_was_given(tmp_path):
    from tests.test_p2p_coordinator import make_coordinator

    coord, svc = make_coordinator()
    svc._pending_inference_chunks = {}
    svc._pending_inference_requests = {}

    async def answer(peer_id, message):
        request_id = message["payload"]["request_id"]
        svc._pending_inference_requests[request_id].set_result({"response": "ok"})

    svc.p2p_manager.send_message_to_peer = AsyncMock(side_effect=answer)

    await coord.request_inference_from_peer("peer-1", "flattened", messages=TURNS, system=SYSTEM)
    quiet = svc.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]
    assert "stream" not in quiet
    assert quiet["messages"] == TURNS and quiet["system"] == SYSTEM

    async def noop(delta):
        return None

    await coord.request_inference_from_peer("peer-1", "flattened", messages=TURNS, on_chunk=noop)
    streamed = svc.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]
    assert streamed["stream"] is True
    assert svc._pending_inference_chunks == {}, "the callback is dropped with the future"
