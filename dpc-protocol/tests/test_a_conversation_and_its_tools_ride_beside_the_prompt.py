"""The v1.7 request half rides beside the prompt, and is invisible when unused.

DPTP §3.4: REMOTE_INFERENCE_REQUEST grew `messages`, `system`, `tools` and
`stream`, and REMOTE_INFERENCE_RESPONSE grew `tool_calls` and `finish_reason`.
The compatibility rule the whole design rests on is that none of it shows when
nobody asked for it: a guest that sends only a prompt must build the same bytes
it built before, so an older host reads exactly what it always read. The new
REMOTE_INFERENCE_CHUNK frame is checked here for its three fields.
"""

import json

import pytest

from dpc_protocol.protocol import (
    create_remote_inference_chunk,
    create_remote_inference_request,
    create_remote_inference_response,
)

MESSAGES = [
    {"role": "user", "content": "what is the weather in Paris?"},
    {"role": "assistant", "content": [{"type": "text", "text": "let me check"}]},
]
TOOLS = [{"name": "get_weather", "description": "the weather",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]
TOOL_USE = [{"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {"city": "Paris"}}]


# --- the prompt-only shapes are untouched -------------------------------------------


def test_a_request_that_names_none_of_the_new_fields_is_the_shape_it_always_was():
    """Byte-identical, not merely equivalent: an older host parses these bytes."""
    built = create_remote_inference_request("req-1", "hello", model="m", provider="p")
    assert json.dumps(built) == json.dumps({
        "command": "REMOTE_INFERENCE_REQUEST",
        "payload": {"request_id": "req-1", "prompt": "hello", "model": "m", "provider": "p"},
    })


@pytest.mark.parametrize("absent", ["messages", "system", "tools", "stream"])
def test_each_new_request_field_is_absent_rather_than_null_when_unused(absent):
    payload = create_remote_inference_request("req-1", "hello")["payload"]
    assert absent not in payload


def test_stream_false_is_the_same_frame_as_no_stream_at_all():
    assert (create_remote_inference_request("req-1", "hi", stream=False)
            == create_remote_inference_request("req-1", "hi"))


def test_a_response_that_ran_no_tools_carries_neither_new_field():
    payload = create_remote_inference_response("req-1", response="hi")["payload"]
    assert "tool_calls" not in payload and "finish_reason" not in payload


def test_an_empty_tool_call_list_is_not_sent():
    """Nothing was called, and the wire says nothing rather than saying `[]`."""
    payload = create_remote_inference_response("req-1", response="hi", tool_calls=[])["payload"]
    assert "tool_calls" not in payload


# --- and they ride when they carry something ----------------------------------------


def test_the_conversation_its_system_prompt_and_its_tools_travel_beside_the_prompt():
    payload = create_remote_inference_request(
        "req-1", "flattened turns", messages=MESSAGES, system="be brief",
        tools=TOOLS, stream=True,
    )["payload"]
    assert payload["prompt"] == "flattened turns", "the prompt stays required beside them"
    assert payload["messages"] == MESSAGES
    assert payload["system"] == "be brief"
    assert payload["tools"] == TOOLS
    assert payload["stream"] is True


def test_a_system_prompt_may_be_a_block_list():
    payload = create_remote_inference_request(
        "req-1", "p", messages=MESSAGES, system=[{"type": "text", "text": "be brief"}],
    )["payload"]
    assert payload["system"] == [{"type": "text", "text": "be brief"}]


def test_the_calls_and_the_stop_word_come_back_on_the_response():
    payload = create_remote_inference_response(
        "req-1", response="", tool_calls=TOOL_USE, finish_reason="tool_calls",
    )["payload"]
    assert payload["tool_calls"] == TOOL_USE
    assert payload["finish_reason"] == "tool_calls"
    assert payload["status"] == "success"


def test_an_error_response_carries_no_calls_and_no_stop_word():
    payload = create_remote_inference_response(
        "req-1", error="refused", tool_calls=TOOL_USE, finish_reason="stop",
    )["payload"]
    assert payload == {"request_id": "req-1", "error": "refused", "status": "error"}


# --- the chunk frame ----------------------------------------------------------------


def test_a_chunk_carries_the_request_id_its_place_and_its_text_and_nothing_else():
    frame = create_remote_inference_chunk("req-1", 0, "The capital")
    assert frame["command"] == "REMOTE_INFERENCE_CHUNK"
    assert frame["payload"] == {"request_id": "req-1", "seq": 0, "delta": "The capital"}


def test_the_deltas_of_a_stream_concatenate_in_seq_order_into_the_answer():
    """The falsifier for the whole streaming design: chunks are transport, and
    the response's text is what they add up to."""
    answer = "The capital of France is Paris."
    pieces = ["The capital ", "of France ", "is Paris."]
    frames = [create_remote_inference_chunk("req-1", i, piece) for i, piece in enumerate(pieces)]
    final = create_remote_inference_response("req-1", response=answer)

    ordered = sorted(frames, key=lambda f: f["payload"]["seq"])
    assert "".join(f["payload"]["delta"] for f in ordered) == final["payload"]["response"]
