"""A `/v1/models` row carries `context_window`, and says nothing when nobody knows.

The host already put the number on the wire — `CoreService.build_p2p_provider_info`
writes `lookup_context_window(provider.model)` into every `PROVIDERS_RESPONSE`
row (DPTP §3.5) — and the door dropped it: `_peer_row_extras` published `tariff`
and `settings` and nothing else, so an IDE client that sizes its own history had
to guess. Both halves of the menu now carry it, a peer's row off the host's own
statement and this node's own off the same `lookup_context_window` the alias
config resolves through.

Which number: `context_window` is what one conversation may occupy, and `n_ctx`
is the pool every slot shares. `llama_server_supervisor.window_outgrows_pool`
says nothing derives one from the other, so they can disagree, and the one a
caller is bounded by is `context_window`.

`None` means «not stated», and a row then carries no key at all: an absent key
and a guessed number are not the same thing, and a client believes a number.
"""

import copy
import json

import pytest

from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER,
    REMOTE_ALIAS,
    REMOTE_MODEL,
    _peer_service,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    BOTH_LISTS,
    LOCAL,
    LOCAL_WINDOW,
    VENDOR,
    _key,
    _request,
    _running,
    _service,
)


async def _rows(server, tmp_path):
    status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))
    assert status == 200, text
    return {row["id"]: row for row in json.loads(text)["data"]}


# --- this node's own rows -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_local_row_carries_the_window_its_own_lookup_answers(tmp_path):
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        rows = await _rows(server, tmp_path)
        assert rows[LOCAL]["context_window"] == LOCAL_WINDOW


@pytest.mark.asyncio
async def test_a_local_row_whose_window_is_unknown_carries_no_such_key(tmp_path):
    """The vendor model is one `lookup_context_window` answers None for."""
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        rows = await _rows(server, tmp_path)
        assert "context_window" not in rows[VENDOR], rows[VENDOR]


# --- a peer's rows --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_row_carries_the_window_the_host_stated(tmp_path):
    service = _peer_service(tmp_path)
    stated = service.peer_metadata[PEER]["providers"][0]["context_window"]
    async with _running(tmp_path, service) as (server, _):
        rows = await _rows(server, tmp_path)
        assert rows[REMOTE_MODEL]["context_window"] == stated


@pytest.mark.asyncio
async def test_a_peer_row_carries_no_window_where_the_host_stated_none(tmp_path):
    """Both silences a host can send: the key absent, and the key set to None —
    `build_p2p_provider_info` writes the second for a model it does not know."""
    for stated in ({}, {"context_window": None}):
        service = _peer_service(tmp_path)
        rows_in = copy.deepcopy(service.peer_metadata[PEER]["providers"])
        for row in rows_in:
            row.pop("context_window", None)
            row.update(stated)
        service.peer_metadata[PEER]["providers"] = rows_in
        async with _running(tmp_path, service) as (server, _):
            rows = await _rows(server, tmp_path)
            assert "context_window" not in rows[REMOTE_MODEL], (stated, rows[REMOTE_MODEL])
            assert rows[REMOTE_MODEL]["id"].endswith(REMOTE_ALIAS), "the row under test is the peer's"
