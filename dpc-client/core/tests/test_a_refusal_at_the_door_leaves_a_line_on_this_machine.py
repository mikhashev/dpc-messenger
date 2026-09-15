"""A refusal at the gateway writes one line here, and it is not a transcript.

On 2026-09-16 the Claude Code VS Code extension was refused with a 400 naming
the roles it may send, and the machine that produced the refusal recorded
nothing: the diagnosis had to be read off the client's screen. Every refusal
now leaves one line — the method and path, the status and the code, and the
sentence `GatewayError` already words — at WARNING for a 4xx and ERROR for a
5xx, with the response id where the route has minted one.

One line, not a transcript: the key, the prompt, the messages and the system
prompt stay out of the log, which is why the requests below carry marker
strings and the assertions look for them.

The refusal reaches the log from three places — the middleware, and each
streaming route for the failure that arrives after the first byte, where the
status can no longer be the response. A stream that has not opened yet
re-raises to the middleware, so that path must not log twice.
"""

import json
import logging

import pytest

from tests.test_the_gateway_carries_tool_calls_and_streams_in_both_forms import (
    _chat_body,
    _data_lines,
    _messages_body,
    _stream_text,
    _tool_service,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    BOTH_LISTS,
    LOCAL,
    _key,
    _request,
    _running,
    _service,
)

GATEWAY_LOGGER = "dpc_client_core.gateway"
PROMPT = "SECRET-PROMPT-marker"
SYSTEM = "SECRET-SYSTEM-marker"
BAD_ROLE = "sidekick"


def _refusals(caplog):
    return [r for r in caplog.records
            if r.name == GATEWAY_LOGGER and r.getMessage().startswith("gateway refused")]


def _no_secrets(line, key):
    assert PROMPT not in line and SYSTEM not in line, line
    assert key not in line, line


def _bad_role_chat(**extra):
    """A body refused only at the third turn, so the first two are parsed and
    a logger that echoed content would have something to echo."""
    body = {"model": LOCAL, "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": PROMPT},
        {"role": BAD_ROLE, "content": PROMPT},
    ]}
    body.update(extra)
    return body


# --- (1) the middleware: the refusal that is the whole response ------------------


@pytest.mark.asyncio
async def test_a_refused_request_writes_one_warning_naming_the_status_the_path_and_the_reason(tmp_path, caplog):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        key = _key(tmp_path)
        with caplog.at_level(logging.DEBUG, logger=GATEWAY_LOGGER):
            status, text = await _request(server, "POST", "/v1/chat/completions",
                                          key=key, body=_bad_role_chat())
    assert status == 400, text
    (record,) = _refusals(caplog)
    line = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "400" in line and "/v1/chat/completions" in line and "POST" in line
    assert "invalid_request_error" in line
    assert BAD_ROLE in line, "the reason the GatewayError words is what tells one refusal from another"
    _no_secrets(line, key)


@pytest.mark.asyncio
async def test_a_failure_the_door_could_not_serve_is_logged_at_error(tmp_path, caplog):
    service = _service(tmp_path, BOTH_LISTS, fail=RuntimeError("the card fell over"))
    async with _running(tmp_path, service) as (server, _):
        key = _key(tmp_path)
        body = {"model": LOCAL, "messages": [{"role": "user", "content": PROMPT}]}
        with caplog.at_level(logging.DEBUG, logger=GATEWAY_LOGGER):
            status, text = await _request(server, "POST", "/v1/chat/completions", key=key, body=body)
    assert status == 502, text
    (record,) = _refusals(caplog)
    assert record.levelno == logging.ERROR
    assert "502" in record.getMessage()
    _no_secrets(record.getMessage(), key)


@pytest.mark.asyncio
async def test_a_refusal_in_the_anthropic_envelope_is_logged_the_same_once(tmp_path, caplog):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        key = _key(tmp_path)
        body = {"model": LOCAL, "max_tokens": 16, "system": SYSTEM,
                "messages": [{"role": BAD_ROLE, "content": PROMPT}]}
        with caplog.at_level(logging.DEBUG, logger=GATEWAY_LOGGER):
            status, text = await _request(server, "POST", "/v1/messages", key=key, body=body)
    assert status == 400, text
    (record,) = _refusals(caplog)
    assert "/v1/messages" in record.getMessage() and record.levelno == logging.WARNING
    _no_secrets(record.getMessage(), key)


# --- (2) the streaming routes: the failure that arrives after the first byte ------


@pytest.mark.asyncio
async def test_a_failure_inside_an_open_openai_stream_writes_one_line_carrying_the_response_id(tmp_path, caplog):
    service = _tool_service(tmp_path, chunks=["hel"], fail_after_chunks=RuntimeError("the card fell over"))
    async with _running(tmp_path, service) as (server, _):
        key = _key(tmp_path)
        body = _chat_body(LOCAL, [{"role": "user", "content": PROMPT}], stream=True)
        with caplog.at_level(logging.DEBUG, logger=GATEWAY_LOGGER):
            text = await _stream_text(server, "/v1/chat/completions", body, key=key)
    lines = _data_lines(text)
    # The client sees `chatcmpl-<id>`; the id itself is what the ledger row and
    # this line are both filed under, so the log joins the two.
    minted = json.loads(lines[0])["id"].removeprefix("chatcmpl-")
    assert "error" in json.loads(lines[-2]), text
    (record,) = _refusals(caplog)
    line = record.getMessage()
    assert record.levelno == logging.ERROR and "502" in line
    assert "/v1/chat/completions" in line
    assert minted in line, "the id the client saw is the one this line is filed under"
    _no_secrets(line, key)


@pytest.mark.asyncio
async def test_a_failure_inside_an_open_messages_stream_writes_one_line_carrying_the_response_id(tmp_path, caplog):
    service = _tool_service(tmp_path, chunks=["hel"], fail_after_chunks=RuntimeError("the card fell over"))
    async with _running(tmp_path, service) as (server, _):
        key = _key(tmp_path)
        body = _messages_body(LOCAL, [{"role": "user", "content": PROMPT}], stream=True)
        with caplog.at_level(logging.DEBUG, logger=GATEWAY_LOGGER):
            text = await _stream_text(server, "/v1/messages", body, key=key)
    head = json.loads([line[len("data: "):] for line in text.split("\n") if line.startswith("data: ")][0])
    minted = head["message"]["id"].removeprefix("msg_")
    (record,) = _refusals(caplog)
    line = record.getMessage()
    assert record.levelno == logging.ERROR and "/v1/messages" in line
    assert minted in line
    _no_secrets(line, key)


@pytest.mark.asyncio
async def test_a_stream_that_failed_before_its_first_byte_is_logged_once_not_twice(tmp_path, caplog):
    """`stream: true` and a door that fails having sent no chunk: the route
    re-raises because the status can still be the response, and the middleware
    is then the one that writes — once between the two of them."""
    service = _tool_service(tmp_path, chunks=[], fail_after_chunks=RuntimeError("the card fell over"))
    async with _running(tmp_path, service) as (server, _):
        key = _key(tmp_path)
        body = _chat_body(LOCAL, [{"role": "user", "content": PROMPT}], stream=True)
        with caplog.at_level(logging.DEBUG, logger=GATEWAY_LOGGER):
            status, text = await _request(server, "POST", "/v1/chat/completions", key=key, body=body)
    assert status == 502, text
    (record,) = _refusals(caplog)
    assert record.levelno == logging.ERROR and "502" in record.getMessage()
