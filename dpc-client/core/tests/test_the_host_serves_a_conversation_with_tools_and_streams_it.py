"""The host's half of DPTP v1.7: a conversation, tools, and a real stream.

Until now `handle_inference_request` had one shape to serve — a flat prompt on
`llm_manager.query` — so a guest reaching this node through the ADR-041 gateway
got no tool calling, no un-flattened turns and no streaming, while the same
node's own local route got all three. The request now carries `messages`,
`system`, `tools` and `stream`, and the host branches on them *after* every
gate: D2's proof of the connection, `can_request_inference`, the serving alias,
the onward-sharing refusal and the effort clamp all run first, and a refused
call emits no chunk.

What is pinned here: the prompt-only request is served exactly as before (the
compatibility rule the whole design rests on); a request with messages reaches
`query_messages` with its tools and comes back carrying `tool_calls` and
`finish_reason`; `stream: true` emits REMOTE_INFERENCE_CHUNK frames whose
deltas concatenate into the final response; a refusal emits none; and a field
from a newer guest that this node has never heard of is ignored rather than
refused.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.message_handlers.inference_handler import RemoteInferenceRequestHandler
from dpc_client_core.node_ledger import NodeLedger
from tests.test_p2p_coordinator import make_coordinator

MESSAGES = [{"role": "user", "content": "what is the weather in Paris?"}]
TOOLS = [{"name": "get_weather", "description": "the weather",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]
TOOL_USE = [{"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {"city": "Paris"}}]
ANSWER = "The weather in Paris is fine."


def _result(**extra):
    result = {
        "response": ANSWER, "model": "qwen3:8b", "provider": "ollama_local",
        "prompt_tokens": 8, "response_tokens": 7, "tokens_used": 15,
    }
    result.update(extra)
    return result


def _host(tmp_path, *, messages_result=None, chunks=()):
    """A coordinator whose serving alias is loaded and whose two query doors
    are fakes; `chunks` is what `query_messages` hands to `on_chunk`.

    The provider carries `generate_with_tools`, because a host that answers a
    tools request must have the path: the door asks `entry_point_for` before the
    router, and a provider without it is refused there rather than served."""
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.providers = {
        "ollama_local": SimpleNamespace(config={}, generate_with_tools=lambda *a, **k: None),
    }
    svc.llm_manager.query = AsyncMock(return_value=_result())

    async def query_messages(messages, **kwargs):
        on_chunk = kwargs.get("on_chunk")
        if on_chunk is not None:
            for piece in chunks:
                await on_chunk(piece)
        return messages_result if messages_result is not None else _result()

    svc.llm_manager.query_messages = AsyncMock(side_effect=query_messages)
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


def _sent(svc):
    """Every frame this host wrote to the peer, in order."""
    return [call[0][1] for call in svc.p2p_manager.send_message_to_peer.call_args_list]


def _chunks(svc):
    return [f["payload"] for f in _sent(svc) if f["command"] == "REMOTE_INFERENCE_CHUNK"]


def _answer(svc):
    (final,) = [f for f in _sent(svc) if f["command"] == "REMOTE_INFERENCE_RESPONSE"]
    return final["payload"]


# --- (1) the prompt-only request is served as it always was --------------------------


@pytest.mark.asyncio
async def test_a_request_carrying_only_a_prompt_still_takes_the_query_door(tmp_path):
    coord, svc = _host(tmp_path)

    await coord.handle_inference_request("peer-1", "req-1", "ping")

    svc.llm_manager.query.assert_awaited_once()
    svc.llm_manager.query_messages.assert_not_awaited()
    payload = _answer(svc)
    assert payload["status"] == "success" and payload["response"] == ANSWER
    assert "tool_calls" not in payload and "finish_reason" not in payload
    assert _chunks(svc) == [], "nobody asked to be streamed to"


# --- (2) a conversation with tools ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_conversation_with_tools_reaches_query_messages_and_the_calls_come_back(tmp_path):
    coord, svc = _host(tmp_path, messages_result=_result(
        response="", tool_calls=TOOL_USE, finish_reason="tool_calls",
    ))

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", messages=MESSAGES, system="be brief",
        tools=TOOLS,
    )

    svc.llm_manager.query.assert_not_awaited()
    call = svc.llm_manager.query_messages.await_args
    assert call.args[0] == MESSAGES
    assert call.kwargs["system"] == "be brief"
    assert call.kwargs["tools"] == TOOLS
    assert call.kwargs["provider_alias"] == "ollama_local"
    assert call.kwargs["on_chunk"] is None, "stream was not asked for"

    payload = _answer(svc)
    assert payload["tool_calls"] == TOOL_USE
    assert payload["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_a_conversation_that_called_nothing_carries_neither_new_field(tmp_path):
    coord, svc = _host(tmp_path)

    await coord.handle_inference_request("peer-1", "req-1", "flattened", messages=MESSAGES)

    payload = _answer(svc)
    assert payload["response"] == ANSWER
    assert "tool_calls" not in payload and "finish_reason" not in payload


@pytest.mark.asyncio
async def test_a_request_carrying_images_keeps_the_prompt_door_that_owns_vision(tmp_path):
    """`query` holds the only vision entry point, so images and a conversation
    together are answered from the prompt — what the local route does too."""
    coord, svc = _host(tmp_path)
    images = [{"base64": "aGk=", "mime_type": "image/png"}]

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", images=images, messages=MESSAGES,
    )

    svc.llm_manager.query_messages.assert_not_awaited()
    assert svc.llm_manager.query.await_args.kwargs["images"] == images


# --- (3) the stream ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_true_emits_chunks_whose_deltas_are_the_answer(tmp_path):
    pieces = ["The weather ", "in Paris ", "is fine."]
    coord, svc = _host(tmp_path, chunks=pieces,
                       messages_result=_result(response="".join(pieces)))

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", messages=MESSAGES, stream=True,
    )

    assert svc.llm_manager.query_messages.await_args.kwargs["on_chunk"] is not None
    chunks = _chunks(svc)
    assert [c["seq"] for c in chunks] == [0, 1, 2], "zero-based and in order"
    assert all(c["request_id"] == "req-1" for c in chunks)
    payload = _answer(svc)
    assert "".join(c["delta"] for c in chunks) == payload["response"]
    assert payload["prompt_tokens"] == 8 and payload["response_tokens"] == 7, (
        "the record is the final frame: no count is born in a chunk"
    )


@pytest.mark.asyncio
async def test_the_response_is_the_last_frame_and_carries_the_whole_answer(tmp_path):
    coord, svc = _host(tmp_path, chunks=["a", "b"], messages_result=_result(response="ab"))

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", messages=MESSAGES, stream=True,
    )

    assert [f["command"] for f in _sent(svc)] == [
        "REMOTE_INFERENCE_CHUNK", "REMOTE_INFERENCE_CHUNK", "REMOTE_INFERENCE_RESPONSE",
    ]
    (row,) = coord._ledger.rows()
    assert row["request_id"] == "req-1" and row["completion_tokens"] == 7


@pytest.mark.asyncio
async def test_a_request_that_did_not_ask_to_stream_is_handed_no_chunk_sender(tmp_path):
    coord, svc = _host(tmp_path, chunks=["a", "b"])

    await coord.handle_inference_request("peer-1", "req-1", "flattened", messages=MESSAGES)

    assert svc.llm_manager.query_messages.await_args.kwargs["on_chunk"] is None
    assert _chunks(svc) == []


# --- (4) a refused call emits no chunk ------------------------------------------------


@pytest.mark.asyncio
async def test_a_firewall_refusal_emits_no_chunk_and_never_reaches_a_provider(tmp_path):
    coord, svc = _host(tmp_path, chunks=["a", "b"])
    svc.firewall.can_request_inference.return_value = False

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", messages=MESSAGES, tools=TOOLS, stream=True,
    )

    assert _chunks(svc) == []
    assert _answer(svc)["status"] == "error"
    svc.llm_manager.query_messages.assert_not_awaited()
    svc.llm_manager.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_request_over_an_unproved_connection_emits_no_chunk(tmp_path):
    """D2 stands in front of every new field, as it stands in front of the old ones."""
    coord, svc = _host(tmp_path, chunks=["a", "b"])
    svc.p2p_manager.peers = {"peer-1": SimpleNamespace(connection_type="webrtc")}

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", messages=MESSAGES, stream=True,
    )

    assert _chunks(svc) == []
    assert "proved" in _answer(svc)["error"]
    svc.llm_manager.query_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_host_with_no_serving_alias_emits_no_chunk(tmp_path):
    coord, svc = _host(tmp_path, chunks=["a", "b"])
    svc.firewall.compute_serving_alias = None

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", messages=MESSAGES, stream=True,
    )

    assert _chunks(svc) == []
    assert _answer(svc)["status"] == "error"


# --- (5) the handler: the four fields are read, and a fifth is ignored ------------------


@pytest.mark.asyncio
async def test_the_handler_hands_the_four_new_fields_on():
    service = MagicMock()
    service._handle_inference_request = AsyncMock()

    await RemoteInferenceRequestHandler(service).handle("peer-1", {
        "request_id": "req-1", "prompt": "flattened", "messages": MESSAGES,
        "system": "be brief", "tools": TOOLS, "stream": True,
    })

    kwargs = service._handle_inference_request.await_args.kwargs
    assert kwargs == {"messages": MESSAGES, "system": "be brief", "tools": TOOLS, "stream": True}


@pytest.mark.asyncio
async def test_a_field_from_a_newer_guest_is_ignored_rather_than_refused():
    """An older host must still answer a guest that grew a field it never heard of."""
    service = MagicMock()
    service._handle_inference_request = AsyncMock()

    await RemoteInferenceRequestHandler(service).handle("peer-1", {
        "request_id": "req-1", "prompt": "flattened",
        "tool_choice": {"type": "any"}, "top_k": 40,
    })

    args = service._handle_inference_request.await_args.args
    assert args[:3] == ("peer-1", "req-1", "flattened")
    assert service._handle_inference_request.await_args.kwargs["tools"] is None
