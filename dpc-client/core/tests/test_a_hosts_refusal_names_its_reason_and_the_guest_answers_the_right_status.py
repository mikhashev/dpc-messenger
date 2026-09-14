"""A host's refusal carries a code, and the guest's gateway answers with it.

`REMOTE_INFERENCE_RESPONSE`'s error form used to carry prose and nothing else
(DPTP §3.4), so on the guest `RemoteInferenceResponseHandler` settled the
pending future with a bare `RuntimeError` and `gateway._complete_via_peer`
turned every one of those into the same `502 peer_refused`. Six different
gates on the host — an unproved tier (ADR-041 D2), the firewall, an empty
serving list, onward sharing (D7 part 1), an effort word the alias has no rung
for, a tools request its provider cannot take — reached an IDE client as one
status, when two of them are the client's own 400 and three are a 403 it must
ask a person about.

This file checks the three hops of the fix:

* the **host** puts the matching code on each refusal it sends, and puts none
  on a failure it has no word for, nor on a served answer;
* the **guest's handler** carries the code on `PeerRefused`, which is still a
  `RuntimeError`, so a caller written before the code keeps what it had;
* the **guest's gateway** answers each code with the status its cause
  deserves, in both HTTP shapes, and keeps the 502 for a code it cannot place
  — and a refusal still writes no usage row.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_protocol.protocol import (
    REFUSAL_CODES,
    PeerRefused,
    create_remote_inference_response,
)
from dpc_client_core.message_handlers.inference_handler import (
    RemoteInferenceResponseHandler,
)
from dpc_client_core.node_ledger import NodeLedger
from tests.test_p2p_coordinator import make_coordinator
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (  # noqa: E501
    PEER,
    REMOTE_MODEL,
    _peer_service,
    _rows,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    _chat,
    _key,
    _request,
    _running,
)
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _anthropic_error,
    _messages,
    _post_messages,
)

HOST_PEER = "peer-1"


# --- the host: one code per gate ---------------------------------------------


def _refusal(svc) -> dict:
    """The error payload the door sent, as the guest will read it."""
    message = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert message["command"] == "REMOTE_INFERENCE_RESPONSE"
    payload = message["payload"]
    assert payload["status"] == "error"
    return payload


def _serving_host(tmp_path, **provider_attrs):
    """A door that would serve, so that only the gate under test refuses."""
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.providers = {
        "ollama_local": SimpleNamespace(config={"type": "ollama"}, **provider_attrs)
    }
    svc.llm_manager.query = AsyncMock(return_value={"response": "pong", "model": "m"})
    svc.llm_manager.query_messages = AsyncMock(return_value={"response": "pong", "model": "m"})
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


@pytest.mark.asyncio
async def test_a_request_over_an_unproved_tier_is_refused_as_identity_unproved(tmp_path):
    """D2: the transport could not prove the name every later gate keys on."""
    coord, svc = _serving_host(tmp_path)
    svc.p2p_manager.peers = {HOST_PEER: SimpleNamespace(node_id=HOST_PEER, connection_type="webrtc")}

    await coord.handle_inference_request(HOST_PEER, "req-1", "ping")

    payload = _refusal(svc)
    assert payload["code"] == "identity_unproved"
    assert "ADR-041 D2" in payload["error"], "the prose is unchanged beside the code"
    assert _rows(coord._ledger) == [], "a refused call is not a call"


@pytest.mark.asyncio
async def test_a_firewall_refusal_is_named_not_allowed(tmp_path):
    coord, svc = _serving_host(tmp_path)
    svc.firewall.can_request_inference.return_value = False

    await coord.handle_inference_request(HOST_PEER, "req-1", "ping")

    payload = _refusal(svc)
    assert payload["code"] == "not_allowed"
    assert "not authorized" in payload["error"]
    assert _rows(coord._ledger) == []


@pytest.mark.asyncio
async def test_a_node_that_designates_no_alias_is_named_model_not_found(tmp_path):
    """Nothing on the menu is a miss on the menu, not a broken host."""
    coord, svc = _serving_host(tmp_path)
    svc.firewall.compute_serving_alias = None

    await coord.handle_inference_request(HOST_PEER, "req-1", "ping")

    payload = _refusal(svc)
    assert payload["code"] == "model_not_found"
    assert "serving alias" in payload["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["remote_peer", "dpc_agent"])
async def test_an_onward_sharing_refusal_is_named_after_the_rule(tmp_path, provider_type):
    """D7 part 1: what is shared is not shared onward."""
    coord, svc = _serving_host(tmp_path)
    svc.firewall.compute_serving_alias = "relay"
    svc.llm_manager.providers = {"relay": SimpleNamespace(config={"type": provider_type})}

    await coord.handle_inference_request(HOST_PEER, "req-1", "ping")

    payload = _refusal(svc)
    assert payload["code"] == "onward_sharing_refused"
    assert "ADR-041 D7" in payload["error"]
    assert _rows(coord._ledger) == []


@pytest.mark.asyncio
async def test_an_effort_word_the_alias_has_no_rung_for_is_the_guests_own_invalid_value(tmp_path):
    """The refusal this card was filed for: the guest chose a word, so the
    guest's own door must answer 400 and list the words, not blame the host."""
    coord, svc = _serving_host(tmp_path)

    await coord.handle_inference_request(
        HOST_PEER, "req-1", "ping", reasoning_effort="enthusiastic",
    )

    payload = _refusal(svc)
    assert payload["code"] == "invalid_value"
    assert "enthusiastic" in payload["error"]
    assert "off, low, medium, high, max" in payload["error"], "the words it does know"
    svc.llm_manager.query.assert_not_called()
    assert _rows(coord._ledger) == []


@pytest.mark.asyncio
async def test_a_tools_request_an_alias_has_no_path_for_is_named_tools_unsupported(tmp_path):
    """`query_messages` refuses before a token is spent when the provider has no
    `generate_with_tools`; the code says which of the two things was wrong."""
    coord, svc = _serving_host(tmp_path)
    svc.llm_manager.query_messages = AsyncMock(side_effect=ValueError(
        "Provider 'ollama_local' (model: m) has no native tool-calling path, and 1 tool(s) "
        "were asked for."
    ))

    await coord.handle_inference_request(
        HOST_PEER, "req-1", "ping",
        messages=[{"role": "user", "content": "ping"}],
        tools=[{"name": "ping", "description": "", "input_schema": {}}],
    )

    payload = _refusal(svc)
    assert payload["code"] == "tools_unsupported"
    assert "tool-calling path" in payload["error"]
    assert _rows(coord._ledger) == []


@pytest.mark.asyncio
async def test_a_path_refusal_on_an_alias_that_does_call_tools_is_invalid_value(tmp_path):
    """The predicate is the one `entry_point_for` asks: with a tool path present
    the same `ValueError` is about the word, not about the tools."""
    coord, svc = _serving_host(tmp_path, generate_with_tools=lambda *a, **k: None)
    svc.llm_manager.query_messages = AsyncMock(side_effect=ValueError(
        "Provider 'ollama_local' (model: m) takes no reasoning effort on its "
        "generate_with_tools path, and 'high' was asked for."
    ))

    await coord.handle_inference_request(
        HOST_PEER, "req-1", "ping",
        messages=[{"role": "user", "content": "ping"}],
        tools=[{"name": "ping", "description": "", "input_schema": {}}],
    )

    assert _refusal(svc)["code"] == "invalid_value"


@pytest.mark.asyncio
async def test_a_failure_mid_call_carries_no_code_at_all(tmp_path):
    """A host that broke has no word for why, and says so by saying nothing:
    the guest then answers the 502 it always did."""
    coord, svc = _serving_host(tmp_path)
    svc.llm_manager.query = AsyncMock(side_effect=RuntimeError("the card fell over"))

    await coord.handle_inference_request(HOST_PEER, "req-1", "ping")

    payload = _refusal(svc)
    assert "code" not in payload
    assert "the card fell over" in payload["error"]


@pytest.mark.asyncio
async def test_a_served_answer_carries_no_code(tmp_path):
    """The code is the error form's field; a success never wears one."""
    coord, svc = _serving_host(tmp_path)

    await coord.handle_inference_request(HOST_PEER, "req-1", "ping")

    payload = svc.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]
    assert payload["status"] == "success" and "code" not in payload


# --- the guest's handler: the code rides on the exception ---------------------


class _Service:
    """Just enough of `CoreService` for the response handler."""

    def __init__(self):
        self._pending_inference_requests = {}
        self._pending_inference_chunks = {}


async def _settle(payload: dict):
    """Hand one REMOTE_INFERENCE_RESPONSE payload to the handler and return the
    exception it settled the pending future with."""
    service = _Service()
    future = asyncio.get_running_loop().create_future()
    service._pending_inference_requests["req-1"] = future
    await RemoteInferenceResponseHandler(service).handle("peer-1", payload)
    assert future.done()
    return future.exception()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", sorted(REFUSAL_CODES))
async def test_the_handler_carries_each_code_on_the_exception(code):
    error = await _settle(create_remote_inference_response(
        "req-1", error="no", code=code,
    )["payload"])

    assert isinstance(error, PeerRefused) and error.code == code
    assert str(error) == "no", "the host's words are the message, as before"


@pytest.mark.asyncio
async def test_a_refusal_from_a_host_that_predates_the_code_names_no_reason():
    error = await _settle(create_remote_inference_response("req-1", error="no")["payload"])

    assert isinstance(error, PeerRefused) and error.code == ""


@pytest.mark.asyncio
async def test_a_peer_refusal_is_still_a_runtime_error_for_every_older_caller():
    """`RemotePeerProvider`, the agent's round and anything else written against
    the old exception keep working: the class only adds a field."""
    error = await _settle(create_remote_inference_response(
        "req-1", error="no", code="not_allowed",
    )["payload"])

    assert isinstance(error, RuntimeError)


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [12, ["not_allowed"], {"code": "not_allowed"}, None])
async def test_a_code_that_is_not_a_string_is_dropped_not_carried(sent):
    """The wire can carry anything; only a word goes further."""
    error = await _settle({"request_id": "req-1", "status": "error", "error": "no", "code": sent})

    assert error.code == ""


@pytest.mark.asyncio
async def test_a_word_this_node_has_no_meaning_for_is_carried_anyway():
    """A newer host may name a cause this one has no word for. The handler is
    not the reader: it carries the word and lets the gateway decide."""
    error = await _settle(
        {"request_id": "req-1", "status": "error", "error": "no", "code": "quota_exhausted"}
    )

    assert error.code == "quota_exhausted"


# --- the guest's gateway: one status per code, in both shapes ------------------

# The map under test, as the card states it: the client's own errors, a model
# it cannot have, and a door it must ask a person about.
STATUS_FOR = {
    "invalid_value": 400,
    "tools_unsupported": 400,
    "model_not_found": 404,
    "not_allowed": 403,
    "identity_unproved": 403,
    "onward_sharing_refused": 403,
}
OPENAI_TYPE_FOR = {400: "invalid_request_error", 403: "permission_error",
                   404: "invalid_request_error", 502: "server_error"}
ANTHROPIC_TYPE_FOR = {400: "invalid_request_error", 403: "permission_error",
                      404: "not_found_error", 502: "api_error"}


#: The word the map does not place yet: a spent vendor ceiling on the peer
#: door (ADR-041 D5). Its status is 429, one line in `_complete_via_peer`, and
#: the two tests below hold the gap open until that line lands.
NOT_PLACED_YET = {"insufficient_quota"}


def test_the_gateway_places_every_word_the_protocol_defines():
    """No word of the vocabulary falls through to the 502 the card was about,
    but one, which is named rather than forgotten."""
    assert set(STATUS_FOR) | NOT_PLACED_YET == set(REFUSAL_CODES)
    assert not set(STATUS_FOR) & NOT_PLACED_YET


@pytest.mark.asyncio
async def test_a_spent_ceiling_is_still_the_502_until_the_gateway_learns_the_word(tmp_path):
    """What a client sees today when a host refuses on its daily ceiling. This
    reddens when the mapping lands, which is when `insufficient_quota` moves
    into `STATUS_FOR` and out of `NOT_PLACED_YET`."""
    refusal = PeerRefused("your ceiling is spent", code="insufficient_quota")
    service = _peer_service(tmp_path, fail=refusal)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))

        assert status == 502
        assert json.loads(text)["error"]["code"] == "peer_refused"
        assert _rows(ledger) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", sorted(STATUS_FOR.items()))
async def test_the_openai_shape_answers_each_code_with_its_status(tmp_path, code, status):
    service = _peer_service(tmp_path, fail=PeerRefused("the host's own words", code=code))
    async with _running(tmp_path, service) as (server, ledger):
        got, text = await _request(server, "POST", "/v1/chat/completions",
                                   key=_key(tmp_path), body=_chat(REMOTE_MODEL))

        assert got == status, text
        error = json.loads(text)["error"]
        assert error["code"] == code, "the host's word, not a second vocabulary"
        assert error["type"] == OPENAI_TYPE_FOR[status]
        assert "the host's own words" in error["message"]
        assert PEER in error["message"]
        assert len(service.peer_calls) == 1, "the refusal is the host's, so the host was asked"
        assert _rows(ledger) == [], "a refusal writes no row"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", sorted(STATUS_FOR.items()))
async def test_the_messages_shape_answers_each_code_with_its_status(tmp_path, code, status):
    service = _peer_service(tmp_path, fail=PeerRefused("the host's own words", code=code))
    async with _running(tmp_path, service) as (server, ledger):
        got, text = await _post_messages(server, _messages(REMOTE_MODEL), key=_key(tmp_path))

        assert got == status, text
        error = _anthropic_error(text)
        assert error["type"] == ANTHROPIC_TYPE_FOR[status]
        assert "the host's own words" in error["message"]
        assert _rows(ledger) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    PeerRefused("compute sharing disabled"),
    PeerRefused("something newer", code="quota_exhausted"),
    RuntimeError("compute sharing disabled"),
])
async def test_a_refusal_with_no_code_this_node_can_place_is_the_502_it_always_was(tmp_path, failure):
    """The compatibility half: an older host, a newer host and any other caller
    still raising the bare error all keep today's answer."""
    service = _peer_service(tmp_path, fail=failure)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))

        assert status == 502
        error = json.loads(text)["error"]
        assert error["code"] == "peer_refused"
        assert str(failure) in error["message"]
        assert _rows(ledger) == []


@pytest.mark.asyncio
async def test_the_row_rule_holds_for_the_new_statuses_too(tmp_path):
    """Every status the map can answer with, one after another on one node:
    nothing that was refused is written down."""
    for code in sorted(REFUSAL_CODES):
        service = _peer_service(tmp_path, fail=PeerRefused("refused", code=code))
        async with _running(tmp_path, service) as (server, ledger):
            await _request(server, "POST", "/v1/chat/completions",
                           key=_key(tmp_path), body=_chat(REMOTE_MODEL))
            assert _rows(ledger) == [], code
