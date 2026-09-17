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

Images beside tools (THREE-PROVIDER-HANDLES-NEVER-GROW-TOGETHER, third step):
image blocks in the turns reach `query_messages` with the tools and the system
on an alias that sees and calls tools, and are refused `tools_unsupported` on
one that does not, by the predicate the menu row states; the flat `images`
field beside tools is refused rather than answered without them, and without
tools it keeps `query` as it always did. The log lines say how many pictures
and tools a call carried, never what they were.
"""

import logging
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
async def test_the_images_field_without_tools_keeps_the_prompt_door_that_owns_vision(tmp_path):
    """The flat `images` field is what every older guest sends, and `query`'s
    vision entry point answers it from the prompt, a conversation beside it or
    not — what the local route does without tools too."""
    coord, svc = _host(tmp_path)
    images = [{"base64": "aGk=", "mime_type": "image/png"}]

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", images=images, messages=MESSAGES,
    )

    svc.llm_manager.query_messages.assert_not_awaited()
    assert svc.llm_manager.query.await_args.kwargs["images"] == images
    assert _answer(svc)["status"] == "success"


# --- (2b) images beside tools: in the turns, by the predicate the menu row states -----

SHOT = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}}
WIRE_SHOT = {"base64": "aGk=", "mime_type": "image/png"}
SYSTEM = "you are a coding agent"
# The incident's two forms: the screenshot in the turn being asked, and the
# screenshot only in the history with a text turn asked after it.
SHOT_IN_THE_LAST_TURN = [
    {"role": "user", "content": [{"type": "text", "text": "what is wrong here?"}, SHOT]},
]
SHOT_IN_THE_HISTORY = [
    {"role": "user", "content": [{"type": "text", "text": "what is wrong on this screen?"}, SHOT]},
    {"role": "assistant", "content": [{"type": "text", "text": "the button is cut off"}]},
    {"role": "user", "content": [{"type": "text", "text": "fix it"}]},
]


class _Provider:
    """A serving alias whose two capabilities a test sets: eyes, and a tools path."""

    def __init__(self, *, vision, tools=True):
        self.config = {"type": "llamacpp_server"}
        self.model = "qwen3.8-27b"
        self._vision = vision
        if tools:
            self.generate_with_tools = self._generate_with_tools

    def supports_vision(self):
        return self._vision

    async def _generate_with_tools(self, *args, **kwargs):
        raise AssertionError("the host speaks to the manager, never to the provider")


def _seeing_host(tmp_path, **provider):
    coord, svc = _host(tmp_path)
    svc.llm_manager.providers = {"ollama_local": _Provider(**provider)}
    return coord, svc


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", [SHOT_IN_THE_LAST_TURN, SHOT_IN_THE_HISTORY],
                         ids=["screenshot asked about", "screenshot in the history"])
async def test_image_blocks_in_the_turns_beside_tools_reach_query_messages_with_the_system_and_the_tools(
        tmp_path, turns):
    coord, svc = _seeing_host(tmp_path, vision=True)

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", messages=turns, system=SYSTEM, tools=TOOLS,
    )

    svc.llm_manager.query.assert_not_awaited()
    call = svc.llm_manager.query_messages.await_args
    assert call.args[0] == turns, "the turns did not reach the door with their image blocks"
    assert call.kwargs["tools"] == TOOLS and call.kwargs["system"] == SYSTEM
    assert _answer(svc)["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [{"vision": False}, {"vision": True, "tools": False}],
                         ids=["calls tools but cannot see", "sees but calls no tools"])
async def test_image_blocks_beside_tools_on_an_alias_that_cannot_take_both_are_refused_and_nothing_runs(
        tmp_path, provider):
    coord, svc = _seeing_host(tmp_path, **provider)

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", messages=SHOT_IN_THE_HISTORY, system=SYSTEM, tools=TOOLS,
    )

    payload = _answer(svc)
    assert payload["status"] == "error" and payload["code"] == "tools_unsupported"
    assert "1 image(s)" in payload["error"] and "serves_images_with_tools" in payload["error"]
    svc.llm_manager.query_messages.assert_not_awaited()
    svc.llm_manager.query.assert_not_awaited()
    assert list(coord._ledger.rows()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("messages", [SHOT_IN_THE_HISTORY, None], ids=["beside turns", "beside a prompt"])
async def test_the_images_field_beside_tools_is_refused_rather_than_answered_without_the_tools(tmp_path, messages):
    """`query` holds no tools. The host used to take this request there and
    answer from the prompt, the tools and the history gone without a word."""
    coord, svc = _seeing_host(tmp_path, vision=True)

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", images=[WIRE_SHOT], messages=messages, tools=TOOLS,
    )

    payload = _answer(svc)
    assert payload["status"] == "error" and payload["code"] == "tools_unsupported"
    assert "must travel in the turns" in payload["error"]
    svc.llm_manager.query.assert_not_awaited()
    svc.llm_manager.query_messages.assert_not_awaited()
    assert list(coord._ledger.rows()) == []


# --- (2c) the log lines count what a call carried, and quote none of it ---------------


def _lines(caplog, prefix):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


@pytest.mark.asyncio
async def test_both_host_lines_count_the_images_in_the_turns_and_inside_a_tool_result_and_the_tools(
        tmp_path, caplog):
    coord, svc = _seeing_host(tmp_path, vision=True)
    turns = [
        *SHOT_IN_THE_HISTORY[:2],
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_01", "name": "get_weather",
                                           "input": {"city": "Paris"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01",
                                      "content": [{"type": "text", "text": "the page"}, SHOT]}]},
    ]
    two_tools = TOOLS + [dict(TOOLS[0], name="read_file")]

    with caplog.at_level(logging.DEBUG, logger="dpc_client_core.p2p_coordinator"):
        await coord.handle_inference_request(
            "peer-1", "req-1", "flattened", messages=turns, system=SYSTEM, tools=two_tools,
        )

    (handling,) = _lines(caplog, "Handling inference request")
    (served,) = _lines(caplog, "Peer inference served")
    for line in (handling, served):
        assert "images=2 tools=2" in line, line
        assert "aGk=" not in line and "the page" not in line, "counted, never quoted"
    assert "images: " not in handling


@pytest.mark.asyncio
async def test_the_images_field_counts_on_the_lines_of_a_request_that_carries_no_tools(tmp_path, caplog):
    coord, svc = _host(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="dpc_client_core.p2p_coordinator"):
        await coord.handle_inference_request("peer-1", "req-1", "flattened", images=[WIRE_SHOT, WIRE_SHOT])

    (handling,) = _lines(caplog, "Handling inference request")
    (served,) = _lines(caplog, "Peer inference served")
    assert "images=2 tools=0" in handling and "images=2 tools=0" in served


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
