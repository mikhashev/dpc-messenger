"""Peer inference is served only where the peer's key was proved (ADR-041 D2).

The host receives `REMOTE_INFERENCE_REQUEST` over whatever connected the two
nodes, and `sender_node_id` is what the firewall admits on, what the ledger
writes the row under and what a quota counts against. On the direct tier that
name was proved — the nonce signature inbound, the key hash outbound. On the
three tiers below it is the Hub's assertion, our own intention, or a field in
an envelope ([[A-REFUSED-IDENTITY-ON-THE-DIRECT-TIER-IS-A-DEMOTION-TO-THREE-
TIERS-THAT-NEVER-ASK-FOR-ONE]]).

D2 draws the line where the proof is: peer inference — all of it, not a gateway
subset — is served over a connection whose key is proved, and WebRTC, relay and
gossip are excluded. The row half of that CRITICAL shipped in `4a9c8dab`
(`peer_proved`, `peer_connection_type`); this is the refusal half, on the host
side.

A refused call is not a call: nothing runs, no usage row is written, and the
peer is told which tier it arrived on and why that is not enough.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.p2p_manager import PROVED_CONNECTION_TYPES, peer_proof
from tests.test_p2p_coordinator import ProvedConnection, make_coordinator

PEER = "peer-1"


class _Connection:
    """What `p2p_manager.peers[peer_id]` is: a wrapper naming its own tier."""

    def __init__(self, node_id: str, connection_type: str = "direct_tls"):
        self.node_id = node_id
        self.connection_type = connection_type


def _host(tmp_path, connection=None):
    """A coordinator that would serve, so that only the tier decides."""
    coord, svc = make_coordinator(peers={PEER: connection} if connection is not None else {})
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.providers = {"ollama_local": SimpleNamespace(config={"type": "ollama"})}
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "pong", "model": "qwen3:8b", "provider": "ollama_local",
        "prompt_tokens": 8, "response_tokens": 1, "tokens_used": 9,
    })
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


def _error_sent(svc) -> str:
    msg = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert msg["command"] == "REMOTE_INFERENCE_RESPONSE"
    return msg["payload"]["error"]


# --- the refusal ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_that_arrived_over_webrtc_is_refused_and_nothing_runs(tmp_path, caplog):
    """WebRTC takes the sender's name from the Hub's signal; the host does not
    spend a token on a name it cannot check."""
    coord, svc = _host(tmp_path, _Connection(PEER, "webrtc"))

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.p2p_coordinator"):
        await coord.handle_inference_request(PEER, "req-1", "ping")

    svc.llm_manager.query.assert_not_called()
    assert list(coord._ledger.rows()) == []

    error = _error_sent(svc)
    assert "webrtc" in error
    assert "ADR-041 D2" in error
    assert "direct TLS" in error

    warned = [r for r in caplog.records
              if r.levelno == logging.WARNING and "ADR-041 D2" in r.getMessage()]
    assert len(warned) == 1


@pytest.mark.asyncio
async def test_a_request_with_no_connection_of_record_is_refused(tmp_path, caplog):
    """Gossip hands `route_message` the envelope's `source` and puts nothing in
    `peers` (gossip_manager.py:727), so the host has no connection to read at
    all. Nothing to read is not a proof."""
    coord, svc = _host(tmp_path)

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.p2p_coordinator"):
        await coord.handle_inference_request(PEER, "req-1", "ping")

    svc.llm_manager.query.assert_not_called()
    assert list(coord._ledger.rows()) == []

    error = _error_sent(svc)
    assert "no connection of record" in error
    assert "ADR-041 D2" in error and "direct TLS" in error

    warned = [r for r in caplog.records
              if r.levelno == logging.WARNING and "ADR-041 D2" in r.getMessage()]
    assert len(warned) == 1


@pytest.mark.asyncio
async def test_an_unproved_sender_learns_nothing_about_the_alias_list(tmp_path):
    """Identity is asked before the alias gates (D7 part 1 and D4-0): a sender
    the transport could not prove must not learn which alias this node serves,
    nor draw an answer out of the firewall."""
    coord, svc = _host(tmp_path, _Connection(PEER, "webrtc"))
    svc.firewall.compute_serving_alias = "ollama_local"

    await coord.handle_inference_request(PEER, "req-1", "ping", model="gemma3:27b")

    svc.firewall.can_request_inference.assert_not_called()
    assert "ollama_local" not in _error_sent(svc)


@pytest.mark.asyncio
async def test_a_request_over_direct_tls_is_still_served_and_written_down(tmp_path):
    """The regression guard: the tier the proof lives on is untouched."""
    coord, svc = _host(tmp_path, _Connection(PEER, "direct_tls"))

    await coord.handle_inference_request(PEER, "req-1", "ping")

    svc.llm_manager.query.assert_called_once()
    msg = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert msg["payload"].get("error") is None and msg["payload"]["response"] == "pong"
    (row,) = list(coord._ledger.rows())
    assert (row["caller"], row["caller_kind"]) == (PEER, "peer")
    assert (row["peer_proved"], row["peer_connection_type"]) == (True, "direct_tls")


# --- the tiers name themselves ----------------------------------------------------


def test_the_webrtc_wrapper_names_its_tier_and_the_tier_is_not_proved():
    """It named none, so `peer_proof` read it as `"unknown"` — a word that says
    nothing about which tier the row or the refusal is talking about."""
    from dpc_client_core.webrtc_peer import WebRTCPeerConnection

    with patch("dpc_client_core.webrtc_peer.RTCPeerConnection", MagicMock()), \
         patch("dpc_client_core.webrtc_peer.Settings", MagicMock()):
        peer = WebRTCPeerConnection(node_id=PEER)

    assert peer.connection_type == "webrtc"
    assert peer_proof({PEER: peer}, PEER) == (False, "webrtc")
    assert "webrtc" not in PROVED_CONNECTION_TYPES


def test_the_udp_dtls_transport_already_names_its_tier_and_it_is_not_proved():
    """`udp_dtls` compares the CN alone and not the key hash
    (transports/dtls_connection.py:175), which is the shape `p2p_manager.py`
    warns about in its own comment — so naming the tier is all it earns. It is
    never placed in `p2p_manager.peers` today (the only assignment sites are
    p2p_manager.py:650, :941 and :1559), so this call cannot arrive over it."""
    from dpc_client_core.transports import UDPPeerConnection

    connection = UDPPeerConnection(node_id=PEER, dtls_conn=SimpleNamespace(is_connected=True))

    assert connection.connection_type == "udp_dtls"
    assert peer_proof({PEER: connection}, PEER) == (False, "udp_dtls")


def test_the_direct_wrapper_is_the_only_proved_tier():
    assert PROVED_CONNECTION_TYPES == ("direct_tls",)
    assert peer_proof({PEER: ProvedConnection(PEER)}, PEER) == (True, "direct_tls")
