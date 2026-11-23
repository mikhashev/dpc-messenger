"""ADR-052, Confirmation test 37, the parts of slice S3b (L13; Mike #70, #85, #11, #17).

Through the real Whisper provider: with the card free a peer's transcription is served at once,
under `serving_a_peer`. Behind a busy Whisper use whose whole trip fits in 120 s (its ETA plus the
peer's own `expected_s`) it waits in the lease line and is served; a trip over 120 s, or an unknown
one (a running music job), is refused at once (< 0.5 s) in words with "right now" and no alias,
agent or chat. The one cheap guard (L13 point 3): the handler re-checks that the guest is still
connected while it waits and at the grant; a guest that left is dropped, nothing loads and no answer
is sent ("transcription answer dropped, request gone").

The lease line's own peer rules (an owner arriving later goes first, the swap switch) are pinned by
`test_a_peer_lease_waits_only_when_its_estimate_fits_its_deadline.py`.
"""

import asyncio
import base64
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.managers.card_lease import LeaseRefusedForPeer
from dpc_client_core.managers.llama_server_supervisor import peer_being_served, serving_a_peer
from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.providers import whisper_provider

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
    fast,
    hold_open,
    music,
    nothing_left,
    until,
    waiting,
    whisper,
)
from tests._whisper_lease_helpers import gpu_whisper, lease_of

pytestmark = pytest.mark.usefixtures("fast")


async def _busy(client):
    over = asyncio.Event()
    task = asyncio.create_task(hold_open(client.lease, "dictation", over))
    await until(lambda: client.lease.granted)
    return over, task


async def _peer(p, expected_s):
    with serving_a_peer("node-guest"):
        return await p.transcribe("peer", purpose="peer node-guest", expected_s=expected_s)


@pytest.mark.asyncio
async def test_with_the_card_free_a_peer_is_served_at_once_as_a_peer(monkeypatch):
    p = gpu_whisper(monkeypatch)
    seen = []

    async def run(path, **kw):
        seen.append(peer_being_served())
        return {"text": path}

    monkeypatch.setattr(p, "_transcribe_loaded", run)
    assert (await asyncio.wait_for(_peer(p, 3.0), BOUND)) == {"text": "peer"}
    assert seen == ["node-guest"] and lease_of(p).granted


@pytest.mark.asyncio
async def test_behind_a_busy_whisper_whose_trip_fits_it_waits_and_is_served(monkeypatch):
    other = whisper("other", eta=3.0)
    over, task = await _busy(other)
    p = gpu_whisper(monkeypatch)
    call = asyncio.create_task(_peer(p, 2.0))
    await until(lambda: waiting() == ["whisper:turbo"])
    over.set()
    await asyncio.wait_for(task, BOUND)
    assert (await asyncio.wait_for(call, BOUND)) == {"text": "peer"}
    await nothing_left()


@pytest.mark.asyncio
@pytest.mark.parametrize("holder", ["over_120", "unknown"])
async def test_a_trip_over_120_s_or_unknown_is_refused_at_once(monkeypatch, holder):
    blocker = whisper("other", eta=110.0) if holder == "over_120" else music()
    over, task = await _busy(blocker)
    p = gpu_whisper(monkeypatch)
    started = time.monotonic()
    with pytest.raises(LeaseRefusedForPeer) as refused:
        await asyncio.wait_for(_peer(p, 19.0), BOUND)
    assert time.monotonic() - started < 0.5
    words = str(refused.value)
    assert "right now" in words and "whisper:other" not in words and "yue2" not in words and "dictation" not in words
    assert p.loads == 0 and waiting() == []
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


def _coordinator(provider, peers):
    service = MagicMock()
    service.p2p_manager.send_message_to_peer = AsyncMock()
    service.p2p_manager.peers = peers
    service.llm_manager.providers = {"w": provider}
    service.firewall.can_request_transcription.return_value = True
    return P2PCoordinator(service), service


_AUDIO = base64.b64encode(b"RIFF....WAVEfmt fake").decode()


@pytest.mark.asyncio
async def test_a_guest_that_left_before_the_grant_is_dropped_and_nothing_loads(monkeypatch, caplog):
    p = gpu_whisper(monkeypatch)
    coordinator, service = _coordinator(p, peers={})
    with caplog.at_level("INFO"):
        await asyncio.wait_for(
            coordinator.handle_transcription_request("node-guest", "req-1", _AUDIO, "audio/wav", provider="w"),
            BOUND)
    assert p.loads == 0 and not lease_of(p).granted
    service.p2p_manager.send_message_to_peer.assert_not_called()
    assert coordinator.transcription_answers_dropped == 1
    assert any("Transcription answer dropped, request gone" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_guest_that_leaves_during_the_wait_leaves_the_line(monkeypatch):
    # 10 s of audio: its own trip is known (load + 10 s at the warm ratio + the first call's penalty).
    monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: 10.0)
    other = whisper("other", eta=3.0)
    over, task = await _busy(other)
    p = gpu_whisper(monkeypatch)
    peers = {"node-guest": object()}
    coordinator, service = _coordinator(p, peers)
    handling = asyncio.create_task(
        coordinator.handle_transcription_request("node-guest", "req-1", _AUDIO, "audio/wav", provider="w"))
    await until(lambda: waiting() == ["whisper:turbo"])
    peers.clear()
    await asyncio.wait_for(handling, BOUND)
    assert waiting() == [] and p.loads == 0
    service.p2p_manager.send_message_to_peer.assert_not_called()
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_guest_still_there_is_answered(monkeypatch):
    p = gpu_whisper(monkeypatch)
    coordinator, service = _coordinator(p, peers={"node-guest": object()})
    await asyncio.wait_for(
        coordinator.handle_transcription_request("node-guest", "req-1", _AUDIO, "audio/wav", provider="w"),
        BOUND)
    assert p.loads == 1 and coordinator.transcription_answers_dropped == 0
    service.p2p_manager.send_message_to_peer.assert_awaited_once()
