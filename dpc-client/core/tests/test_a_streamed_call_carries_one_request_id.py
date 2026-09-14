"""One call, one id: from the first SSE event to the usage rows of both nodes.

Observed on the two-node pair on 2026-09-14: a streamed call on
`remote:<node>:<alias>` wore two ids. The OpenAI content chunks carried the id
the door minted before the call, while the finish chunk, the usage chunk and
both ledger rows carried a second id the coordinator minted for the wire; the
Messages form did the same across `message_start`. That breaks the OpenAI
stream contract, which is one id per response, and it breaks the double entry
of ADR-041 D3, whose two rows join on `request_id` — the id the client held
stood on no row.

Pinned here: the door mints once and that id travels — to the host on the wire,
back into the guest's row, and onto every event the client reads — on the peer
route and on the local one, streamed and whole, in both HTTP shapes. The
coordinator mints only for a caller that supplies no id.

The peer is the fake `request_inference_from_peer` of the peer-route test file,
which records the id it was sent and echoes it as a host does. Cross-platform:
pure asyncio.
"""

import json
from unittest.mock import AsyncMock

import pytest

from tests.test_p2p_coordinator import make_coordinator
from tests.test_the_gateway_carries_tool_calls_and_streams_in_both_forms import (
    _chat_body,
    _data_lines,
    _messages_body,
    _post_chat,
    _stream_text,
    _tool_service,
)
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    REMOTE_MODEL,
    WIRE_ID,
    _rows,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import LOCAL, _key, _running
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _post_messages,
    _sse_events,
)
from tests.test_the_peer_route_is_no_narrower_than_the_local_one import PIECES, TURNS, _tool_serving_peer


def _openai_ids(text):
    """Every id an OpenAI stream showed its client, `[DONE]` aside."""
    return [json.loads(line)["id"] for line in _data_lines(text) if line != "[DONE]"]


def _anthropic_ids(text):
    """The Messages stream names the message once, on `message_start`."""
    return [body["message"]["id"] for name, body in _sse_events(text) if name == "message_start"]


def _streamed_chat(model):
    return _chat_body(model, TURNS, stream=True, stream_options={"include_usage": True})


# --- (1) the peer route: the id on the wire is the id the client was shown ----------


@pytest.mark.asyncio
async def test_a_streamed_peer_call_shows_one_openai_id_and_it_is_the_id_on_the_wire(tmp_path):
    service = _tool_serving_peer(tmp_path, chunks=PIECES)
    async with _running(tmp_path, service) as (server, ledger):
        text = await _stream_text(server, "/v1/chat/completions",
                                  _streamed_chat(REMOTE_MODEL), key=_key(tmp_path))
        ids = _openai_ids(text)
        assert len(ids) == len(PIECES) + 2, "a chunk per delta, the finish chunk, the usage chunk"
        assert len(set(ids)) == 1, f"the stream wore {len(set(ids))} ids: {sorted(set(ids))}"

        (call,) = service.peer_calls
        assert call["request_id"], "the door's id has to reach the wire for the rows to join"
        assert ids[0] == "chatcmpl-" + call["request_id"]
        (row,) = _rows(ledger)
        assert row["request_id"] == call["request_id"], "the guest's row joins the host's on this"


@pytest.mark.asyncio
async def test_a_streamed_peer_call_names_the_message_once_in_the_anthropic_form(tmp_path):
    service = _tool_serving_peer(tmp_path, chunks=PIECES)
    async with _running(tmp_path, service) as (server, ledger):
        text = await _stream_text(server, "/v1/messages",
                                  _messages_body(REMOTE_MODEL, TURNS, stream=True), key=_key(tmp_path))
        (message_id,) = _anthropic_ids(text)

        (call,) = service.peer_calls
        assert message_id == "msg_" + call["request_id"]
        (row,) = _rows(ledger)
        assert row["request_id"] == call["request_id"]


@pytest.mark.asyncio
async def test_a_peer_that_sent_no_chunk_is_named_by_the_same_id_as_one_that_did(tmp_path):
    """The head leaves after the answer here, and carries the door's id all the same."""
    service = _tool_serving_peer(tmp_path)  # a host that streams nothing
    async with _running(tmp_path, service) as (server, ledger):
        text = await _stream_text(server, "/v1/messages",
                                  _messages_body(REMOTE_MODEL, TURNS, stream=True), key=_key(tmp_path))
        (message_id,) = _anthropic_ids(text)

        (call,) = service.peer_calls
        assert message_id == "msg_" + call["request_id"]
        assert _rows(ledger)[0]["request_id"] == call["request_id"]


@pytest.mark.asyncio
async def test_a_host_answering_under_another_id_does_not_rename_the_stream(tmp_path):
    """The client was handed an id with its first chunk and the host was asked
    under that id, so that is what the row records; a response naming something
    else is the host's own confusion and does not move the join."""
    service = _tool_serving_peer(tmp_path, chunks=PIECES, echoes=False)
    async with _running(tmp_path, service) as (server, ledger):
        text = await _stream_text(server, "/v1/chat/completions",
                                  _streamed_chat(REMOTE_MODEL), key=_key(tmp_path))
        ids = _openai_ids(text)
        assert len(set(ids)) == 1, f"the stream wore {len(set(ids))} ids: {sorted(set(ids))}"

        (call,) = service.peer_calls
        assert ids[0] == "chatcmpl-" + call["request_id"]
        (row,) = _rows(ledger)
        assert row["request_id"] == call["request_id"] != WIRE_ID


# --- (2) the local route, which had no second minter and must stay that way ---------


@pytest.mark.asyncio
async def test_a_streamed_local_call_shows_one_id_and_the_row_carries_it(tmp_path):
    service = _tool_service(tmp_path, chunks=PIECES)
    async with _running(tmp_path, service) as (server, ledger):
        text = await _stream_text(server, "/v1/chat/completions",
                                  _streamed_chat(LOCAL), key=_key(tmp_path))
        ids = _openai_ids(text)
        assert len(set(ids)) == 1, f"the stream wore {len(set(ids))} ids: {sorted(set(ids))}"
        (row,) = _rows(ledger)
        assert ids[0] == "chatcmpl-" + row["request_id"]


@pytest.mark.asyncio
async def test_a_streamed_local_call_names_the_message_once_in_the_anthropic_form(tmp_path):
    service = _tool_service(tmp_path, chunks=PIECES)
    async with _running(tmp_path, service) as (server, ledger):
        text = await _stream_text(server, "/v1/messages",
                                  _messages_body(LOCAL, TURNS, stream=True), key=_key(tmp_path))
        (message_id,) = _anthropic_ids(text)
        (row,) = _rows(ledger)
        assert message_id == "msg_" + row["request_id"]


# --- (3) the whole answer, which was never split, on both routes --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [LOCAL, REMOTE_MODEL])
async def test_an_unstreamed_answer_is_named_by_the_id_of_its_row_in_both_forms(tmp_path, model):
    service = _tool_service(tmp_path) if model == LOCAL else _tool_serving_peer(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_chat(server, _chat_body(model, TURNS), key=_key(tmp_path))
        assert status == 200, text
        chat_id = json.loads(text)["id"]

        status, text = await _post_messages(server, _messages_body(model, TURNS), key=_key(tmp_path))
        assert status == 200, text
        message_id = json.loads(text)["id"]

    first, second = _rows(ledger)
    assert chat_id == "chatcmpl-" + first["request_id"]
    assert message_id == "msg_" + second["request_id"]


# --- (4) the coordinator mints for a caller that has no id of its own ---------------


@pytest.mark.asyncio
async def test_the_coordinator_sends_the_callers_id_and_mints_only_without_one():
    coord, svc = make_coordinator()
    svc._pending_inference_chunks = {}
    svc._pending_inference_requests = {}
    seen = {}

    async def answer(peer_id, message):
        request_id = message["payload"]["request_id"]
        seen["frame"] = request_id
        seen["waiting"] = request_id in svc._pending_inference_requests
        seen["chunks"] = list(svc._pending_inference_chunks)
        svc._pending_inference_requests[request_id].set_result({"response": "ok"})

    svc.p2p_manager.send_message_to_peer = AsyncMock(side_effect=answer)

    async def noop(delta):
        return None

    await coord.request_inference_from_peer("peer-1", "flattened", on_chunk=noop, request_id="the-doors-id")
    assert seen["frame"] == "the-doors-id", "the id the client saw is the id the host is asked under"
    assert seen["waiting"], "the future waits on it"
    assert seen["chunks"] == ["the-doors-id"], "and the chunks come back on it"

    await coord.request_inference_from_peer("peer-1", "flattened")
    assert seen["frame"] and seen["frame"] != "the-doors-id", "nobody supplied one, so one is minted"
