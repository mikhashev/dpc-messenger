"""The routes this door does not serve answer by name, not by a bare 404.

The gateway serves chat and the menu in front of it: `GET /v1/models`,
`POST /v1/chat/completions`, `POST /v1/messages`. An IDE client also indexes
(`/v1/embeddings`) and completes a line (`/v1/completions`, and Anthropic's
legacy `/v1/complete`), and until now each of those was aiohttp's own 404 with
no body — indistinguishable, from the client's side, from a wrong port, a
wrong host or a gateway that is down (board entry
THE-GATEWAY-SERVES-CHAT-ONLY-AND-EVERY-IDE-CLIENT-ALSO-INDEXES-AND-COMPLETES).

Each now answers a 404 carrying `endpoint_not_served` — this door's own HTTP
code word, in the family of `model_not_found`; no DPTP command learns it,
because nothing crosses the wire — and a sentence naming the route and the two
that are served. Nothing is implemented behind them: this node's only
embedding model belongs to an agent's memory index, is no provider alias and
stands in neither serving list, so the gateway has nothing to route an
embeddings call to (ADR-041 D1, D4, D7).
"""

import json

import pytest

from dpc_client_core.gateway import UNSERVED_ROUTES
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    BOTH_LISTS,
    _key,
    _request,
    _running,
    _service,
)
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _anthropic_error,
)

# The three, written out here rather than read from the module, so a route
# quietly dropped from `UNSERVED_ROUTES` fails a test instead of shrinking the
# loop that checks them.
EMBEDDINGS = "/v1/embeddings"
COMPLETIONS = "/v1/completions"
COMPLETE = "/v1/complete"


@pytest.mark.asyncio
async def test_each_unserved_route_is_404_naming_itself_and_the_routes_that_are_served(tmp_path):
    service = _service(tmp_path, BOTH_LISTS)
    async with _running(tmp_path, service) as (server, ledger):
        key = _key(tmp_path)
        for path in (EMBEDDINGS, COMPLETIONS):
            status, text = await _request(server, "POST", path, key=key, body={"input": "hi"})
            error = json.loads(text)["error"]
            assert status == 404, path
            assert error["code"] == "endpoint_not_served", path
            assert path in error["message"], path
            assert "/v1/chat/completions" in error["message"], path

        # The Anthropic dialect's own legacy route, in the Anthropic envelope:
        # the guard chooses the shape by path, and a client of that dialect
        # parses what it expects even where no handler ran.
        status, text = await _request(server, "POST", COMPLETE, key=key, body={"prompt": "hi"})
        error = _anthropic_error(text)
        assert status == 404 and error["type"] == "not_found_error"
        assert COMPLETE in error["message"] and "/v1/messages" in error["message"]

        # Named, and nothing behind it: no call, no row.
        assert service.calls == [] and list(ledger.rows()) == []


@pytest.mark.asyncio
async def test_the_embeddings_refusal_says_the_node_has_no_alias_to_route_one_to(tmp_path):
    """Not «not yet»: this node's embedding model is the agent memory index's,
    no provider alias names it, and neither serving list holds one."""
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        status, text = await _request(server, "POST", EMBEDDINGS, key=_key(tmp_path),
                                      body={"input": "hi", "model": "anything"})
        assert status == 404
        message = json.loads(text)["error"]["message"]
        assert "memory index" in message and "serving_local" in message


@pytest.mark.asyncio
async def test_a_wrong_method_on_an_unserved_route_reads_the_same_sentence(tmp_path):
    """Registered for any method: a GET must not become a 405 that says nothing
    about this door."""
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        status, text = await _request(server, "GET", EMBEDDINGS, key=_key(tmp_path))
        assert status == 404
        assert json.loads(text)["error"]["code"] == "endpoint_not_served"


@pytest.mark.asyncio
async def test_an_unserved_route_is_still_behind_the_key(tmp_path):
    """The door says what it is before it says what it serves: no key, 401 —
    the same answer every other route gives, so the port tells a stranger
    nothing about what runs behind it."""
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        for path in UNSERVED_ROUTES:
            status, _text = await _request(server, "POST", path, body={"input": "hi"})
            assert status == 401, path


@pytest.mark.asyncio
async def test_the_three_served_routes_still_answer(tmp_path):
    """The refusals are three new routes beside the door, not in front of it."""
    async with _running(tmp_path, _service(tmp_path, BOTH_LISTS)) as (server, _):
        status, _text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))
        assert status == 200
        assert set(UNSERVED_ROUTES) == {EMBEDDINGS, COMPLETIONS, COMPLETE}
