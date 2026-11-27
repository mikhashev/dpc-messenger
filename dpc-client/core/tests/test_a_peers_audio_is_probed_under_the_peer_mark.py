"""ADR-052 S3 follow-up 2, Zcode #22-4: a peer's transcription is the peer's from the probe on.

The peer door builds its card ask (`card_ask`, which probes the audio length with ffprobe) inside
`serving_a_peer`, so the time spent reading the length carries the peer mark as the call does; the
S3b re-check that the guest is still connected stays on the ask and runs at the grant.
"""

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.managers.llama_server_supervisor import peer_being_served
from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.providers import whisper_provider

from tests._card_lease_helpers import BOUND, fast  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper
from tests.test_p2p_coordinator import ProvedConnection

pytestmark = pytest.mark.usefixtures("fast")

_AUDIO = base64.b64encode(b"RIFF....WAVEfmt fake").decode()


def _coordinator(provider, peers):
    service = MagicMock()
    service.p2p_manager.send_message_to_peer = AsyncMock()
    service.p2p_manager.peers = peers
    service.llm_manager.providers = {"w": provider}
    service.firewall.can_request_transcription.return_value = True
    return P2PCoordinator(service), service


@pytest.mark.asyncio
async def test_the_length_probe_runs_under_the_peer_mark(monkeypatch):
    seen = []

    def probe(path):
        seen.append(peer_being_served())
        return 10.0

    monkeypatch.setattr(whisper_provider, "audio_seconds", probe)
    p = gpu_whisper(monkeypatch)
    coordinator, service = _coordinator(p, peers={"node-guest": ProvedConnection("node-guest")})
    await asyncio.wait_for(
        coordinator.handle_transcription_request("node-guest", "req-1", _AUDIO, "audio/wav", provider="w"),
        BOUND)
    assert seen == ["node-guest"]
    service.p2p_manager.send_message_to_peer.assert_called_once()


@pytest.mark.asyncio
async def test_the_ask_still_carries_the_guest_check(monkeypatch):
    monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: 10.0)
    p = gpu_whisper(monkeypatch)
    asks = []
    real = p.transcribe

    async def spy(path, *args, **kw):
        asks.append(kw)
        return await real(path, *args, **kw)

    monkeypatch.setattr(p, "transcribe", spy)
    coordinator, _ = _coordinator(p, peers={"node-guest": ProvedConnection("node-guest")})
    await asyncio.wait_for(
        coordinator.handle_transcription_request("node-guest", "req-1", _AUDIO, "audio/wav", provider="w"),
        BOUND)
    assert len(asks) == 1 and callable(asks[0]["check"])
    assert asks[0]["purpose"] == "peer node-guest" and asks[0]["expected_s"] is not None
