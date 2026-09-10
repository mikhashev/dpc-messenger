"""The gateway speaks the Anthropic Messages form over the same door.

`POST /v1/messages` is a second HTTP shape over the same `Gateway` as
`/v1/chat/completions` (ADR-041): one listener, one key, one Host check, the
same two serving lists, the same vendor quota and the same ledger row. What
differs is only the wire: the key may arrive as `x-api-key` as well as
`Authorization: Bearer`, the body is the Messages request, the answer is a
`message` object, the stream is the six Messages events, and an error is the
Anthropic envelope `{"type": "error", "error": {"type", "message"}}`.

The conversation reaches the provider through the one converter the
providers already use (`OllamaProvider._anthropic_to_openai_messages`) and
the adapter's `messages_to_prompt`, so the flattened prompt is the one the
OpenAI route would have produced for the same turns. Tools are accepted and
ignored (ADR-041 M1): the answer is always a text block with `end_turn`.

The stand-in service, the running listener and the key are the ones the
OpenAI-shape test file builds; the listener is a real `aiohttp` `TCPSite` on
port 0. Cross-platform: pure asyncio.
"""

import json
from datetime import datetime, timezone

import aiohttp
import pytest

from dpc_client_core.node_ledger import NodeLedger
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    ANSWER,
    BOTH_LISTS,
    LOCAL,
    NODE_ID,
    UNLISTED,
    VENDOR,
    _chat,
    _key,
    _request,
    _running,
    _service,
    _spend,
)

SYSTEM = "be brief"
OPENAI_PROMPT = f"[SYSTEM]\n{SYSTEM}\n\n[USER]\nhi"


def _messages(model, text="hi", *, system=SYSTEM, **extra):
    body = {"model": model, "max_tokens": 256, "messages": [{"role": "user", "content": text}]}
    if system is not None:
        body["system"] = system
    body.update(extra)
    return body


async def _post_messages(server, body, *, key=None, header="x-api-key", headers=None):
    """POST /v1/messages; the key goes in `x-api-key` unless `header="bearer"`."""
    hdrs = dict(headers or {})
    if key is not None:
        if header == "bearer":
            hdrs["Authorization"] = f"Bearer {key}"
        else:
            hdrs["x-api-key"] = key
    url = f"http://127.0.0.1:{server.port}/v1/messages"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=hdrs) as resp:
            return resp.status, await resp.text()


def _anthropic_error(text):
    body = json.loads(text)
    assert body["type"] == "error", body
    assert set(body["error"]) == {"type", "message"}, body
    return body["error"]


def _sse_events(text):
    """[(event name, parsed data)] in wire order."""
    events = []
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line]
        if not lines:
            continue
        assert lines[0].startswith("event: ") and lines[1].startswith("data: "), block
        events.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return events


# --- (1) a local completion, Messages-shaped, one row, the same prompt --------------


@pytest.mark.asyncio
async def test_a_messages_request_on_a_local_alias_is_messages_shaped_and_leaves_one_gateway_row(tmp_path):
    service = _service(tmp_path, BOTH_LISTS)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(server, _messages(LOCAL), key=_key(tmp_path))
        assert status == 200, text
        body = json.loads(text)
        assert (body["type"], body["role"], body["model"]) == ("message", "assistant", LOCAL)
        assert body["content"] == [{"type": "text", "text": ANSWER}]
        assert (body["stop_reason"], body["stop_sequence"]) == ("end_turn", None)
        assert body["usage"] == {"input_tokens": 12, "output_tokens": 5}

        rows = list(ledger.rows())
        assert len(rows) == 1
        row = rows[0]
        assert (row["caller"], row["caller_kind"], row["route"]) == (NODE_ID, "gateway", "local")
        assert (row["alias"], row["model"]) == (LOCAL, "qwen3:8b")
        assert (row["prompt_tokens"], row["completion_tokens"]) == (12, 5)
        assert body["id"] == "msg_" + row["request_id"]

        # The same conversation through the OpenAI route flattens to the same prompt.
        assert service.calls[0]["alias"] == LOCAL
        assert service.calls[0]["prompt"] == OPENAI_PROMPT
        status, _ = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200
        assert service.calls[1]["prompt"] == service.calls[0]["prompt"]


# --- (2) two header forms, one key -------------------------------------------------


@pytest.mark.asyncio
async def test_x_api_key_and_bearer_both_open_the_door_and_a_wrong_key_is_401_in_the_anthropic_envelope(tmp_path):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        key = _key(tmp_path)
        assert (await _post_messages(server, _messages(LOCAL), key=key))[0] == 200
        assert (await _post_messages(server, _messages(LOCAL), key=key, header="bearer"))[0] == 200

        for header in ("x-api-key", "bearer"):
            status, text = await _post_messages(server, _messages(LOCAL), key="not-" + key, header=header)
            assert status == 401, header
            error = _anthropic_error(text)
            assert error["type"] == "authentication_error"
            assert ".gateway_key" in error["message"]
        status, text = await _post_messages(server, _messages(LOCAL))
        assert status == 401 and _anthropic_error(text)["type"] == "authentication_error"

        # The OpenAI route still takes Bearer, and now x-api-key too; its errors keep its envelope.
        assert (await _request(server, "GET", "/v1/models", key=key))[0] == 200
        assert (await _request(server, "GET", "/v1/models", headers={"x-api-key": key}))[0] == 200
        status, text = await _request(server, "GET", "/v1/models", key="not-" + key)
        assert status == 401
        body = json.loads(text)
        assert "type" not in body and body["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_a_foreign_host_on_the_messages_route_is_400_in_the_anthropic_envelope(tmp_path):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        status, text = await _post_messages(server, _messages(LOCAL), key=_key(tmp_path),
                                            headers={"Host": "gateway.example:9997"})
        assert status == 400
        error = _anthropic_error(text)
        assert error["type"] == "invalid_request_error" and "gateway.example:9997" in error["message"]


# --- (3) stream: true, the six events in order ---------------------------------------


@pytest.mark.asyncio
async def test_stream_true_yields_the_six_message_events_with_the_whole_text_in_one_delta(tmp_path):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, ledger):
        url = f"http://127.0.0.1:{server.port}/v1/messages"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=_messages(LOCAL, stream=True),
                                    headers={"x-api-key": _key(tmp_path)}) as resp:
                assert resp.status == 200
                assert resp.content_type == "text/event-stream"
                text = await resp.text()
        events = _sse_events(text)
        assert [name for name, _ in events] == [
            "message_start", "content_block_start", "content_block_delta",
            "content_block_stop", "message_delta", "message_stop",
        ]
        assert all(data["type"] == name for name, data in events)
        (row,) = ledger.rows()

        start = events[0][1]["message"]
        assert start["id"] == "msg_" + row["request_id"]
        assert (start["type"], start["role"], start["model"]) == ("message", "assistant", LOCAL)
        assert (start["content"], start["stop_reason"], start["stop_sequence"]) == ([], None, None)
        assert start["usage"] == {"input_tokens": 12, "output_tokens": 0}
        assert events[1][1] == {"type": "content_block_start", "index": 0,
                                "content_block": {"type": "text", "text": ""}}
        assert events[2][1] == {"type": "content_block_delta", "index": 0,
                                "delta": {"type": "text_delta", "text": ANSWER}}
        assert events[3][1] == {"type": "content_block_stop", "index": 0}
        assert events[4][1] == {"type": "message_delta",
                                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                "usage": {"output_tokens": row["completion_tokens"]}}
        assert events[5][1] == {"type": "message_stop"}


# --- (4) refusals in the Anthropic envelope, reaching neither provider nor ledger -----


@pytest.mark.asyncio
async def test_an_alias_outside_both_lists_is_404_not_found_error(tmp_path):
    service = _service(tmp_path, BOTH_LISTS)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(server, _messages(UNLISTED), key=_key(tmp_path))
        assert status == 404
        error = _anthropic_error(text)
        assert error["type"] == "not_found_error" and UNLISTED in error["message"]
        assert service.calls == [] and list(ledger.rows()) == []


@pytest.mark.asyncio
async def test_a_vendor_alias_over_its_quota_is_429_rate_limit_error(tmp_path):
    quota = 0.01
    service = _service(tmp_path, {"serving_vendor": [VENDOR], "vendor_quotas": {VENDOR: quota}})
    ledger = NodeLedger(tmp_path / "ledger")
    _spend(ledger, cost=quota, started_at=datetime.now(timezone.utc), request_id="the-whole-day")
    async with _running(tmp_path, service, ledger=ledger) as (server, _):
        status, text = await _post_messages(server, _messages(VENDOR), key=_key(tmp_path))
        assert status == 429
        error = _anthropic_error(text)
        assert error["type"] == "rate_limit_error"
        assert VENDOR in error["message"] and "0.01" in error["message"]
        assert service.calls == []
        assert [r["request_id"] for r in ledger.rows()] == ["the-whole-day"]


@pytest.mark.asyncio
async def test_a_provider_failure_is_502_api_error_and_an_unloaded_provider_503_api_error(tmp_path):
    service = _service(tmp_path, BOTH_LISTS, fail=RuntimeError("Ollama is not running"))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(server, _messages(LOCAL), key=_key(tmp_path))
        assert status == 502
        error = _anthropic_error(text)
        assert error["type"] == "api_error" and "Ollama is not running" in error["message"]
        assert list(ledger.rows()) == []

    listed_but_unloaded = _service(tmp_path, {"serving_local": ["gone"]}, providers={})
    async with _running(tmp_path, listed_but_unloaded) as (server, ledger):
        status, text = await _post_messages(server, _messages("gone"), key=_key(tmp_path))
        assert status == 503
        error = _anthropic_error(text)
        assert error["type"] == "api_error" and "gone" in error["message"]
        assert listed_but_unloaded.calls == [] and list(ledger.rows()) == []


# --- (5) tools are accepted and ignored: text and end_turn, nothing raises ------------


@pytest.mark.asyncio
async def test_tools_and_a_tool_result_in_the_history_are_answered_with_text_and_end_turn(tmp_path):
    service = _service(tmp_path, BOTH_LISTS)
    body = {
        "model": LOCAL, "max_tokens": 256, "system": SYSTEM,
        "tools": [{"name": "read_file", "description": "read", "input_schema": {"type": "object", "properties": {}}}],
        "tool_choice": {"type": "auto"},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "read a.txt"},
                                         {"type": "image", "source": {"type": "base64", "data": "AA=="}}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "reading"},
                {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "the file says hi"}]},
            ]},
        ],
    }
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(server, body, key=_key(tmp_path))
        assert status == 200, text
        answer = json.loads(text)
        assert answer["content"] == [{"type": "text", "text": ANSWER}]
        assert answer["stop_reason"] == "end_turn"
        assert len(list(ledger.rows())) == 1

        # The provider saw the converter's rendering of every block, tool result included.
        (call,) = service.calls
        prompt = call["prompt"]
        assert prompt.startswith(f"[SYSTEM]\n{SYSTEM}\n\n[USER]\nread a.txt\n\n[ASSISTANT]\nreading\n")
        assert "read_file" in prompt
        assert "[TOOL RESULT:" in prompt and "the file says hi" in prompt


# --- (6) malformed bodies are 400 invalid_request_error --------------------------------


@pytest.mark.asyncio
async def test_a_body_without_model_or_messages_or_text_is_400_invalid_request_error(tmp_path):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, ledger):
        key = _key(tmp_path)
        bodies = (
            {"max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            {"model": LOCAL, "max_tokens": 1},
            {"model": LOCAL, "max_tokens": 1, "messages": []},
            {"model": LOCAL, "max_tokens": 1, "messages": "hi"},
            {"model": LOCAL, "max_tokens": 1, "messages": ["hi"]},
            {"model": LOCAL, "max_tokens": 1, "messages": [{"role": "system", "content": "hi"}]},
            {"model": LOCAL, "max_tokens": 1, "messages": [{"role": "user", "content": ""}]},
            {"model": LOCAL, "max_tokens": 1, "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "data": "AA=="}}]}]},
            {"model": LOCAL, "max_tokens": 1, "system": {"text": "x"}, "messages": [{"role": "user", "content": "hi"}]},
        )
        for body in bodies:
            status, text = await _post_messages(server, body, key=key)
            assert status == 400, (body, text)
            assert _anthropic_error(text)["type"] == "invalid_request_error", body
        assert list(ledger.rows()) == []

        url = f"http://127.0.0.1:{server.port}/v1/messages"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=b"not json", headers={"x-api-key": key}) as resp:
                assert resp.status == 400
                assert _anthropic_error(await resp.text())["type"] == "invalid_request_error"


# --- (7) system as a string and as blocks flatten alike --------------------------------


@pytest.mark.asyncio
async def test_a_block_list_system_and_a_string_system_flatten_to_the_same_prompt(tmp_path):
    service = _service(tmp_path, BOTH_LISTS)
    async with _running(tmp_path, service) as (server, _):
        key = _key(tmp_path)
        blocks = [{"type": "text", "text": "be ", "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": "brief"}]
        assert (await _post_messages(server, _messages(LOCAL, system=SYSTEM), key=key))[0] == 200
        assert (await _post_messages(server, _messages(LOCAL, system=blocks), key=key))[0] == 200
        assert (await _post_messages(server, _messages(LOCAL, system=None), key=key))[0] == 200
        prompts = [c["prompt"] for c in service.calls]
        assert prompts[0] == prompts[1] == OPENAI_PROMPT
        assert prompts[2] == "[USER]\nhi"
