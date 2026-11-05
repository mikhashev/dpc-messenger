"""The usage a door returns carries the cache hit its ledger row already holds.

Board: THE-DOOR-KEEPS-THE-CACHE-HITS-IT-MEASURED-SO-A-CLIENT-SHOWS-ZERO-CACHE.
A client that reads `usage` (dsh reads `prompt_tokens_details.cached_tokens`)
showed 0 cached for prompts the engine had served 90% from its cache. OpenAI
form: `prompt_tokens_details.cached_tokens`, inside `prompt_tokens`. Messages
form: Anthropic's `input_tokens` is the prompt read fresh and
`cache_read_input_tokens` the hit, summed by the client — so with a measured hit
`input_tokens` is prompt minus hit and the prompt is never counted twice.
A hit nobody measured is no field, never 0: the peer wire carries none, and a
provider with no split gives none. A real aiohttp door, a double shaped like the
manager's dict.
"""

import json

import pytest

from tests.test_the_gateway_carries_tool_calls_and_streams_in_both_forms import (
    CALL,
    READ_FILE,
    _chat_body,
    _data_lines,
    _messages_body,
    _peer_service,
    _post_chat,
    _stream_text,
    _tool_service,
)
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    REMOTE_MODEL,
    _priced_result,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import LOCAL, _key, _running
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _post_messages,
    _sse_events,
)

HIT = 9  # of the double's 12 prompt tokens
USER = [{"role": "user", "content": "hi"}]


def _with_cache(service, **extra):
    """The same double, its manager dict grown by what the provider's usage held."""
    inner = service.llm_manager.query_messages

    async def query_messages(*args, **kwargs):
        return dict(await inner(*args, **kwargs), **extra)

    service.llm_manager.query_messages = query_messages
    return service


@pytest.mark.asyncio
async def test_an_openai_reply_carries_the_hit_inside_the_prompt_and_it_is_the_rows(tmp_path):
    service = _with_cache(_tool_service(tmp_path), cached_tokens=HIT, prefilled_tokens=3)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_chat(server, _chat_body(LOCAL, USER), key=_key(tmp_path))
        assert status == 200, text
        usage = json.loads(text)["usage"]
        (row,) = ledger.rows()
        assert usage["prompt_tokens_details"] == {"cached_tokens": HIT} and row["cached_tokens"] == HIT
        assert usage["prompt_tokens"] == row["prompt_tokens"] == 12  # the hit is inside it


@pytest.mark.asyncio
@pytest.mark.parametrize("include_usage", [False, True])
async def test_an_openai_stream_carries_the_hit_on_the_usage_it_writes(tmp_path, include_usage):
    service = _with_cache(_tool_service(tmp_path, text="hello", chunks=["hel", "lo"]), cached_tokens=HIT)
    async with _running(tmp_path, service) as (server, ledger):
        extra = {"stream_options": {"include_usage": True}} if include_usage else {}
        body = _chat_body(LOCAL, USER, stream=True, **extra)
        text = await _stream_text(server, "/v1/chat/completions", body, key=_key(tmp_path))
        chunks = [json.loads(line) for line in _data_lines(text)[:-1]]
        carrying = [c["usage"] for c in chunks if c["usage"]]
        assert len(carrying) == 1
        assert carrying[0]["prompt_tokens_details"] == {"cached_tokens": HIT}
        assert include_usage == (chunks[-1]["choices"] == [])


@pytest.mark.asyncio
async def test_a_messages_reply_splits_the_prompt_so_a_client_summing_the_two_gets_it_once(tmp_path):
    service = _with_cache(_tool_service(tmp_path), cached_tokens=HIT)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(server, _messages_body(LOCAL, USER), key=_key(tmp_path))
        assert status == 200, text
        usage = json.loads(text)["usage"]
        (row,) = ledger.rows()
        assert usage["cache_read_input_tokens"] == row["cached_tokens"] == HIT
        assert usage["input_tokens"] == 3
        assert usage["input_tokens"] + usage["cache_read_input_tokens"] == row["prompt_tokens"] == 12


@pytest.mark.asyncio
async def test_a_messages_stream_carries_the_split_on_message_delta(tmp_path):
    service = _with_cache(_tool_service(tmp_path, text="hello", chunks=["hel", "lo"]), cached_tokens=HIT)
    async with _running(tmp_path, service) as (server, _):
        text = await _stream_text(server, "/v1/messages", _messages_body(LOCAL, USER, stream=True),
                                  key=_key(tmp_path))
        events = dict(_sse_events(text))
        usage = events["message_delta"]["usage"]
        assert (usage["input_tokens"], usage["cache_read_input_tokens"]) == (3, HIT)


@pytest.mark.asyncio
async def test_a_provider_with_no_split_gives_no_field_in_either_form_not_a_zero(tmp_path):
    service = _tool_service(tmp_path)  # the double's dict holds no cached_tokens
    async with _running(tmp_path, service) as (server, ledger):
        key = _key(tmp_path)
        _, text = await _post_chat(server, _chat_body(LOCAL, USER), key=key)
        assert "prompt_tokens_details" not in json.loads(text)["usage"]
        _, text = await _post_messages(server, _messages_body(LOCAL, USER), key=key)
        usage = json.loads(text)["usage"]
        assert "cache_read_input_tokens" not in usage and usage["input_tokens"] == 12
        assert {r["cached_tokens"] for r in ledger.rows()} == {None}


@pytest.mark.asyncio
async def test_a_messages_head_written_after_the_answer_carries_the_split_too(tmp_path):
    # A calls-only turn streams no text, so the head leaves once the door has counted.
    service = _with_cache(_tool_service(tmp_path, text="", chunks=[], tool_calls=[CALL]), cached_tokens=HIT)
    async with _running(tmp_path, service) as (server, _):
        body = _messages_body(LOCAL, USER, stream=True, tools=[READ_FILE])
        text = await _stream_text(server, "/v1/messages", body, key=_key(tmp_path))
        head = dict(_sse_events(text))["message_start"]["message"]["usage"]
        assert (head["input_tokens"], head["cache_read_input_tokens"]) == (3, HIT)


@pytest.mark.asyncio
@pytest.mark.parametrize("hit", [99, -1, True])
async def test_a_hit_that_is_not_a_count_inside_the_prompt_is_not_printed(tmp_path, hit):
    service = _with_cache(_tool_service(tmp_path), cached_tokens=hit)
    async with _running(tmp_path, service) as (server, _):
        key = _key(tmp_path)
        _, text = await _post_chat(server, _chat_body(LOCAL, USER), key=key)
        assert "prompt_tokens_details" not in json.loads(text)["usage"]
        _, text = await _post_messages(server, _messages_body(LOCAL, USER), key=key)
        usage = json.loads(text)["usage"]
        assert "cache_read_input_tokens" not in usage and usage["input_tokens"] == 12


@pytest.mark.asyncio
async def test_a_peer_alias_gives_no_field_because_the_wire_carries_no_hit(tmp_path):
    service = _peer_service(tmp_path, result=_priced_result(cached_tokens=HIT))
    async with _running(tmp_path, service) as (server, _):
        key = _key(tmp_path)
        _, text = await _post_chat(server, _chat_body(REMOTE_MODEL, USER), key=key)
        assert "prompt_tokens_details" not in json.loads(text)["usage"]
        _, text = await _post_messages(server, _messages_body(REMOTE_MODEL, USER), key=key)
        assert "cache_read_input_tokens" not in json.loads(text)["usage"]
