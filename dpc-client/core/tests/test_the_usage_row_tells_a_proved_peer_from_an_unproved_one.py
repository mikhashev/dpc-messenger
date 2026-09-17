"""A usage row says whether the node at the other end of it was proved.

ADR-041 D2 draws one line — peer compute is served over a connection on which
the peer's key has been proved — and the gateway keeps it
(`PROVED_CONNECTION_TYPES`). The ledger did not: `caller_kind="peer"` covered a
proved caller and an unproved one alike, so a quota, a cap or a probe keyed on
`caller` had no way to tell a name the transport proved from a name the Hub
asserted, our own intention supplied or an envelope carried
([[A-REFUSED-IDENTITY-ON-THE-DIRECT-TIER-IS-A-DEMOTION-TO-THREE-TIERS-THAT-NEVER-ASK-FOR-ONE]]).

`peer_proved` and `peer_connection_type` are that distinction, written by
whoever knows the connection the call travelled over: the host's row for a
peer's call, the gateway's requester row for its own peer route, and the
agent's requester row for a peer-routed round. This file is about the columns;
the host's refusal — D2's other half, which is why an unproved peer leaves no
host row at all any more — lives in
`test_peer_inference_is_served_only_over_a_proved_connection.py`. A requester
row still carries every tier, because this node may ask over any of them.

A row with no peer in it — an agent or a gateway client on this machine — says
None to both: there is no far end to prove. A row written before the columns
reads the same way.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.node_ledger import NodeLedger, usage_row
from dpc_client_core.p2p_manager import PROVED_CONNECTION_TYPES, peer_proof
from tests.test_p2p_coordinator import make_coordinator
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER,
    REMOTE_MODEL,
    WIRE_ID,
    _peer_service,
    _rows,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    _chat,
    _key,
    _request,
    _running,
)

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
HOST_PEER = "peer-1"


def _row(**overrides):
    fields = dict(
        request_id="req-1", caller="agent_001", caller_kind="agent",
        alias="qwen", model="qwen3:8b", route="local",
        prompt_tokens=8, completion_tokens=1, thinking_tokens=None,
        counts_source="ours", started_at=NOW, duration_s=1.0,
        billing="subscription", cost_usd=0.0,
    )
    fields.update(overrides)
    return usage_row(**fields)


class _Connection:
    """What `p2p_manager.peers[peer_id]` is: a wrapper naming its own tier."""

    def __init__(self, node_id, connection_type="direct_tls"):
        self.node_id = node_id
        self.connection_type = connection_type


# --- the columns ------------------------------------------------------------------


def test_a_row_with_no_peer_in_it_claims_nothing_about_one():
    row = _row()
    assert row["peer_proved"] is None and row["peer_connection_type"] is None


@pytest.mark.parametrize("proved,tier", [(True, "direct_tls"), (False, "webrtc"), (False, "udp_dtls")])
def test_the_marker_is_written_as_given(proved, tier):
    row = _row(caller_kind="peer", peer_proved=proved, peer_connection_type=tier)
    assert (row["peer_proved"], row["peer_connection_type"]) == (proved, tier)


@pytest.mark.parametrize("bad", [1, 0, "yes", "true"])
def test_a_marker_that_is_not_a_bool_is_refused_not_written(bad):
    """`peer_proved=1` would read as proved to Python and as a count to a human."""
    with pytest.raises(ValueError, match="peer_proved"):
        _row(peer_proved=bad)


def test_a_tier_that_is_not_a_word_is_refused_not_written():
    with pytest.raises(ValueError, match="peer_connection_type"):
        _row(peer_connection_type=6)


def test_a_row_written_before_the_columns_reads_as_none(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    old = {k: v for k, v in _row().items()
           if k not in ("peer_proved", "peer_connection_type")}
    ledger.append(old)

    (row,) = ledger.rows()
    assert row["peer_proved"] is None and row["peer_connection_type"] is None


# --- the reader of the connection -------------------------------------------------


def test_only_direct_tls_is_proved_and_a_peer_with_no_connection_says_nothing():
    assert PROVED_CONNECTION_TYPES == ("direct_tls",)
    assert peer_proof({HOST_PEER: _Connection(HOST_PEER)}, HOST_PEER) == (True, "direct_tls")
    assert peer_proof({HOST_PEER: _Connection(HOST_PEER, "webrtc")}, HOST_PEER) == (False, "webrtc")
    assert peer_proof({}, HOST_PEER) == (None, None)
    assert peer_proof(None, HOST_PEER) == (None, None)
    # A wrapper that names no tier is unknown, and unknown is not proved.
    assert peer_proof({HOST_PEER: SimpleNamespace()}, HOST_PEER) == (False, "unknown")


# --- the host's row: the caller is the peer, and the transport says whether ---------


def _host(tmp_path, connection=None):
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.providers = {"ollama_local": SimpleNamespace(config={})}
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "pong", "model": "qwen3:8b", "provider": "ollama_local",
        "prompt_tokens": 8, "response_tokens": 1, "tokens_used": 9,
    })
    svc.p2p_manager.peers = {HOST_PEER: connection} if connection is not None else {}
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


@pytest.mark.asyncio
async def test_a_call_served_over_direct_tls_leaves_a_row_that_says_proved(tmp_path):
    coord, _ = _host(tmp_path, _Connection(HOST_PEER))

    await coord.handle_inference_request(HOST_PEER, "req-1", "ping")

    (row,) = coord._ledger.rows()
    assert (row["caller"], row["caller_kind"]) == (HOST_PEER, "peer")
    assert (row["peer_proved"], row["peer_connection_type"]) == (True, "direct_tls")


@pytest.mark.asyncio
async def test_a_call_over_another_tier_is_refused_and_leaves_no_row(tmp_path):
    """WebRTC takes the name from the Hub's signal. The row used to say so and
    the call still ran; since D2 the call does not run, so there is nothing to
    write down — a refused call is not a call."""
    coord, svc = _host(tmp_path, _Connection(HOST_PEER, "webrtc"))

    await coord.handle_inference_request(HOST_PEER, "req-1", "ping")

    svc.llm_manager.query.assert_not_called()
    assert list(coord._ledger.rows()) == []


@pytest.mark.asyncio
async def test_a_caller_with_no_connection_of_record_is_refused_and_leaves_no_row(tmp_path):
    coord, svc = _host(tmp_path)

    await coord.handle_inference_request(HOST_PEER, "req-1", "ping")

    svc.llm_manager.query.assert_not_called()
    assert list(coord._ledger.rows()) == []


# --- the gateway's requester row: proved by the gate it passed ----------------------


@pytest.mark.asyncio
async def test_the_gateways_requester_row_says_the_connection_it_gated_on(tmp_path):
    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text

        (row,) = _rows(ledger)
        assert (row["request_id"], row["route"]) == (WIRE_ID, "peer")
        assert (row["peer_proved"], row["peer_connection_type"]) == (True, "direct_tls")


@pytest.mark.asyncio
async def test_a_gateway_row_for_a_local_call_has_no_peer_to_prove(tmp_path):
    from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import LOCAL

    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200, text

        (row,) = _rows(ledger)
        assert row["route"] == "local"
        assert (row["peer_proved"], row["peer_connection_type"]) == (None, None)


# --- the agent's requester row: the connection the round went over ------------------


class _SilentProvider:
    alias = "qwen"
    model = "qwen3:8b"

    async def generate_response(self, prompt, **kwargs):
        return "unused"

    def get_last_usage(self):
        return {}


def _requester(tmp_path, connection):
    ledger = NodeLedger(tmp_path / "ledger")
    manager = SimpleNamespace(
        token_count_manager=None, providers={"qwen": _SilentProvider()},
        agent_provider=None, default_provider="qwen",
    )
    adapter = DpcLlmAdapter(manager, provider_alias="qwen", caller="agent_test",
                            ledger=ledger, compute_host=PEER)
    service = SimpleNamespace(
        _request_inference_from_peer=AsyncMock(return_value={
            "request_id": WIRE_ID, "response": "from afar", "model": "qwen-on-the-peer",
            "prompt_tokens": 40, "response_tokens": 1, "tokens_used": 41,
        }),
        p2p_manager=SimpleNamespace(peers={PEER: connection} if connection else {}),
    )
    adapter._llm_manager.providers["dpc_agent"] = SimpleNamespace(
        peer_id=None, remote_model=None, timeout=5, _service=service,
    )
    return adapter, ledger


@pytest.mark.parametrize("connection,expected", [
    (_Connection(PEER), (True, "direct_tls")),
    (_Connection(PEER, "relay"), (False, "relay")),
    (None, (None, None)),
], ids=["direct TLS", "relayed", "gone"])
@pytest.mark.asyncio
async def test_the_agents_peer_route_row_says_what_the_connection_was(tmp_path, connection, expected):
    adapter, ledger = _requester(tmp_path, connection)

    await adapter.chat([{"role": "user", "content": "x"}])

    (row,) = ledger.rows()
    assert row["route"] == "peer"
    assert (row["peer_proved"], row["peer_connection_type"]) == expected


@pytest.mark.asyncio
async def test_an_agents_local_row_has_no_peer_to_prove(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    manager = SimpleNamespace(
        token_count_manager=None, providers={"qwen": _SilentProvider()},
        agent_provider=None, default_provider="qwen",
    )
    adapter = DpcLlmAdapter(manager, provider_alias="qwen", caller="agent_test", ledger=ledger)

    await adapter.chat([{"role": "user", "content": "x"}])

    (row,) = ledger.rows()
    assert row["route"] == "local"
    assert (row["peer_proved"], row["peer_connection_type"]) == (None, None)
