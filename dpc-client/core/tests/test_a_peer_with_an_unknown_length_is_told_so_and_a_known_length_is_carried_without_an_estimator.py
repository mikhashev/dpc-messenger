"""ADR-052 S3 follow-up 5 (Ark #44-1, #44-2).

1. A peer whose audio length is unknown is refused in words that name the length, on a free card and
   behind a blocked one alike (the length is what is unknown, not the wait). A known length behind a
   blocked card keeps the card's own words.
2. `card_ask` carries `audio_s` whenever the length is known (probe or declared), also for a provider
   that keeps no estimator; nothing is carried when it is unknown.
"""

import asyncio

import pytest

from dpc_client_core.managers.card_lease import LeaseRefusedForPeer
from dpc_client_core.managers.llama_server_supervisor import serving_a_peer
from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import card_ask

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, whisper  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast")

LENGTH_WORDS = ("this node cannot tell how long this audio is, so it cannot promise to finish it "
                "within the time the request is given")


async def _peer_acquire(lease, expected_s):
    with serving_a_peer("node-guest"):
        return await asyncio.wait_for(lease.acquire("peer node-guest", wait_s=None, expected_s=expected_s), BOUND)


@pytest.mark.asyncio
async def test_an_unknown_length_on_a_free_card_is_not_called_a_busy_card():
    p = whisper("small")
    with pytest.raises(LeaseRefusedForPeer) as refused:
        await _peer_acquire(p.lease, None)
    assert str(refused.value) == LENGTH_WORDS
    assert holding() == []
    await nothing_left()


@pytest.mark.asyncio
async def test_an_unknown_length_on_a_blocked_card_gets_the_same_words():
    p = whisper("small")
    release = asyncio.Event()

    async def hold():
        async with p.lease.use("dictation", wait_s=BOUND):
            await release.wait()

    holder = asyncio.create_task(hold())
    for _ in range(200):
        if p.lease._uses:
            break
        await asyncio.sleep(0.01)
    try:
        with pytest.raises(LeaseRefusedForPeer) as refused:
            await _peer_acquire(p.lease, None)
        assert str(refused.value) == LENGTH_WORDS
    finally:
        release.set()
        await asyncio.wait_for(holder, BOUND)
    await nothing_left()


class _NoEstimator:
    config = {"type": "local_whisper"}


def test_a_provider_without_an_estimator_still_carries_the_probed_length(monkeypatch):
    monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: 42.5)
    ask = card_ask(_NoEstimator(), "dictation", "a.webm")
    assert ask["audio_s"] == 42.5 and ask["expected_s"] is None


def test_a_provider_without_an_estimator_carries_the_declared_length_when_nothing_is_probed(monkeypatch):
    monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: None)
    ask = card_ask(_NoEstimator(), "dictation", "a.webm", declared_s=12)
    assert ask["audio_s"] == 12.0


def test_no_known_length_carries_none(monkeypatch):
    monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: None)
    assert "audio_s" not in card_ask(_NoEstimator(), "dictation", "a.webm")
