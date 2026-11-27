"""Peer transcription is served only where the peer's key was proved (ADR-041 D2).

`handle_transcription_request` receives `REMOTE_TRANSCRIPTION_REQUEST` over
whatever transport connected the two nodes, and `peer_id` is what
`firewall.can_request_transcription` admits on and what Whisper's card lease
labels (`serving_a_peer`, `peer <id>`). Only `direct_tls` proves the peer's
key; WebRTC, hole-punched UDP/DTLS, relay wrappers, and gossip do not.

An unproved request is refused BEFORE `firewall.can_request_transcription` is
queried, before audio is decoded or probed, and before Whisper runs, returning
a `REMOTE_TRANSCRIPTION_RESPONSE` carrying `code=REFUSAL_IDENTITY_UNPROVED`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.message_handlers.transcription_handler import RemoteTranscriptionResponseHandler
from dpc_client_core.p2p_manager import peer_proof
from dpc_protocol.protocol import (
    REFUSAL_IDENTITY_UNPROVED,
    PeerRefused,
    create_remote_transcription_response,
)
from tests.test_p2p_coordinator import ProvedConnection, make_coordinator

PEER = "peer-1"
_AUDIO = base64.b64encode(b"RIFF....WAVEfmt fake").decode()


class _Connection:
    """Fake peer connection carrying an explicit transport tier name."""

    def __init__(self, node_id: str, connection_type: str):
        self.node_id = node_id
        self.connection_type = connection_type


class _FakeWhisper:
    def __init__(self):
        self.config = {"type": "local_whisper"}
        self.calls = 0

    async def transcribe(self, path, **kwargs):
        self.calls += 1
        return {
            "text": "transcribed words",
            "language": "en",
            "duration": 2.5,
            "provider": "local_whisper",
        }


def _host(connection=None):
    """A coordinator configured to allow and run transcription, so only the transport decides."""
    peers = {PEER: connection} if connection is not None else {}
    coord, svc = make_coordinator(peers=peers)
    svc.firewall.can_request_transcription.return_value = True
    whisper = _FakeWhisper()
    svc.llm_manager.providers = {"whisper_local": whisper}
    return coord, svc, whisper


def _response_sent(svc) -> dict:
    msg = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert msg["command"] == "REMOTE_TRANSCRIPTION_RESPONSE"
    return msg["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection", "expected_tier"),
    [
        (_Connection(PEER, "webrtc"), "webrtc"),
        (_Connection(PEER, "udp_dtls"), "udp_dtls"),
        (SimpleNamespace(node_id=PEER), "unknown"),
        (None, "no connection of record"),
    ],
)
async def test_unproved_connection_is_refused_before_firewall_and_whisper(
    connection, expected_tier, monkeypatch, caplog
):
    from dpc_client_core.providers import whisper_provider

    coord, svc, whisper = _host(connection)
    card_ask_spy = MagicMock(side_effect=AssertionError("card_ask must not be called"))
    transcribe_route_spy = AsyncMock(side_effect=AssertionError("transcribe_with_route must not be called"))
    monkeypatch.setattr(whisper_provider, "card_ask", card_ask_spy)
    monkeypatch.setattr(whisper_provider, "transcribe_with_route", transcribe_route_spy)

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.p2p_coordinator"):
        await coord.handle_transcription_request(
            PEER, "req-t-1", _AUDIO, "audio/wav", model="openai/whisper-large-v3", provider="whisper_local"
        )

    svc.firewall.can_request_transcription.assert_not_called()
    assert whisper.calls == 0
    card_ask_spy.assert_not_called()
    transcribe_route_spy.assert_not_called()

    payload = _response_sent(svc)
    assert payload["request_id"] == "req-t-1"
    assert payload["status"] == "error"
    assert payload["code"] == REFUSAL_IDENTITY_UNPROVED
    assert expected_tier in payload["error"]
    assert "ADR-041 D2" in payload["error"]
    assert "direct TLS" in payload["error"]

    warned = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "ADR-041 D2" in r.getMessage()
    ]
    assert len(warned) == 1


@pytest.mark.asyncio
async def test_relayed_peer_connection_is_refused_before_firewall(monkeypatch):
    from dpc_client_core.transports.relayed_connection import RelayedPeerConnection

    relay_node = SimpleNamespace(node_id="dpc-node-relay")
    relayed = RelayedPeerConnection(
        peer_id=PEER,
        relay_node=relay_node,
        relay_connection=MagicMock(),
        session_id="sess-1",
        own_node_id="dpc-node-self",
    )
    assert peer_proof({PEER: relayed}, PEER) == (False, "unknown")

    coord, svc, whisper = _host(relayed)
    await coord.handle_transcription_request(
        PEER, "req-t-relay", _AUDIO, "audio/wav", provider="whisper_local"
    )

    svc.firewall.can_request_transcription.assert_not_called()
    assert whisper.calls == 0
    payload = _response_sent(svc)
    assert payload["status"] == "error"
    assert payload["code"] == REFUSAL_IDENTITY_UNPROVED


@pytest.mark.asyncio
async def test_proved_direct_tls_connection_proceeds_through_firewall_and_whisper(monkeypatch):
    from dpc_client_core.providers import whisper_provider

    monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: 2.5)
    coord, svc, whisper = _host(ProvedConnection(PEER))

    await coord.handle_transcription_request(
        PEER, "req-t-ok", _AUDIO, "audio/wav", model="openai/whisper-large-v3", provider="whisper_local"
    )

    svc.firewall.can_request_transcription.assert_called_once_with(PEER, "openai/whisper-large-v3")
    assert whisper.calls == 1
    payload = _response_sent(svc)
    assert payload["request_id"] == "req-t-ok"
    assert payload["status"] == "success"
    assert payload["text"] == "transcribed words"
    assert "code" not in payload


@pytest.mark.asyncio
async def test_proved_direct_tls_connection_still_respects_firewall_denial():
    coord, svc, whisper = _host(ProvedConnection(PEER))
    svc.firewall.can_request_transcription.return_value = False

    await coord.handle_transcription_request(
        PEER, "req-t-denied", _AUDIO, "audio/wav", model="openai/whisper-large-v3", provider="whisper_local"
    )

    svc.firewall.can_request_transcription.assert_called_once_with(PEER, "openai/whisper-large-v3")
    assert whisper.calls == 0
    payload = _response_sent(svc)
    assert payload["status"] == "error"
    assert payload.get("code") != REFUSAL_IDENTITY_UNPROVED
    assert "Access denied" in payload["error"]


def test_create_remote_transcription_response_carries_code_only_on_error():
    err = create_remote_transcription_response(
        request_id="req-1",
        error="unproved",
        code=REFUSAL_IDENTITY_UNPROVED,
    )["payload"]
    assert err == {
        "request_id": "req-1",
        "error": "unproved",
        "status": "error",
        "code": REFUSAL_IDENTITY_UNPROVED,
    }

    err_no_code = create_remote_transcription_response(request_id="req-2", error="boom")["payload"]
    assert "code" not in err_no_code

    ok = create_remote_transcription_response(
        request_id="req-3", text="hello", code=REFUSAL_IDENTITY_UNPROVED
    )["payload"]
    assert ok["status"] == "success"
    assert "code" not in ok


@pytest.mark.asyncio
async def test_remote_transcription_response_handler_preserves_refusal_code():
    service = MagicMock()
    fut = asyncio.get_running_loop().create_future()
    service._pending_transcription_requests = {"req-1": fut}
    handler = RemoteTranscriptionResponseHandler(service)

    payload = create_remote_transcription_response(
        request_id="req-1",
        error="direct TLS required",
        code=REFUSAL_IDENTITY_UNPROVED,
    )["payload"]
    await handler.handle(PEER, payload)

    with pytest.raises(PeerRefused) as raised:
        await fut
    assert isinstance(raised.value, RuntimeError)
    assert raised.value.code == REFUSAL_IDENTITY_UNPROVED
    assert "direct TLS required" in str(raised.value)
