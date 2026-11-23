"""ADR-052 L13 point 2, Mike #17 (2026-11-23), Ark #20-1: the 120 s rule holds at the grant too.

A peer's transcription whose whole trip (its own `expected_s`) does not fit the guest's 120 s
(`PEER_TRIP_S`) is refused at once, also when the card is free: the guest has gone before the work
could end. One that fits is served at once. One whose `expected_s` is unknown cannot be shown to fit,
so since S3 follow-up 3 (Johnny #30-4) it is refused on a free card too, in the same class and words
as behind a blocked card, and also as a new use of a warm grant. Fake clients.
"""

import asyncio

import pytest

from dpc_client_core.managers.card_lease import PEER_TRIP_S, LeaseRefusedForPeer
from dpc_client_core.managers.llama_server_supervisor import serving_a_peer

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, whisper  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast")

NAMES = ("whisper:", "turbo", "small", "music:", "yue2", "agent", "chat", "flash")


async def _peer_acquire(lease, expected_s):
    with serving_a_peer("node-guest"):
        return await asyncio.wait_for(lease.acquire("peer node-guest", wait_s=None, expected_s=expected_s), BOUND)


@pytest.mark.asyncio
async def test_a_trip_over_120_s_is_refused_at_once_on_a_free_card():
    p = whisper("small")
    with pytest.raises(LeaseRefusedForPeer) as refused:
        await _peer_acquire(p.lease, 300)  # CPU twin at about 0.98x on 300 s of audio
    text = str(refused.value)
    assert "right now" in text
    for name in NAMES:
        assert name not in text
    assert holding() == [], "nothing was granted"
    await nothing_left()


@pytest.mark.asyncio
async def test_a_trip_that_fits_is_served_at_once_on_a_free_card():
    p = whisper("small")
    await _peer_acquire(p.lease, 30)
    assert holding() == ["whisper:small"]


@pytest.mark.asyncio
async def test_the_limit_itself_fits_and_a_second_over_does_not():
    p = whisper("small")
    await _peer_acquire(p.lease, PEER_TRIP_S)
    assert holding() == ["whisper:small"]
    p.lease.release("test")
    await nothing_left()
    with pytest.raises(LeaseRefusedForPeer):
        await _peer_acquire(p.lease, PEER_TRIP_S + 1)


@pytest.mark.asyncio
async def test_an_unknown_own_time_is_refused_on_a_free_card_in_the_blocked_cards_words():
    p = whisper("small")
    with pytest.raises(LeaseRefusedForPeer) as refused:
        await _peer_acquire(p.lease, None)
    assert str(refused.value) == "this node's GPU is busy with another job right now, and how long is not known"
    assert holding() == [], "nothing was granted"
    await nothing_left()


@pytest.mark.asyncio
async def test_an_unknown_own_time_is_refused_as_a_new_use_of_a_warm_grant_and_a_known_one_is_served():
    p = whisper("small")
    await asyncio.wait_for(p.lease.acquire("preload", wait_s=BOUND), BOUND)  # warm, idle, nothing open
    with pytest.raises(LeaseRefusedForPeer):
        with serving_a_peer("node-guest"):
            async with p.lease.use("peer node-guest", wait_s=None, expected_s=None):
                pass
    with serving_a_peer("node-guest"):
        async with p.lease.use("peer node-guest", wait_s=None, expected_s=30):
            assert p.lease._uses
    assert holding() == ["whisper:small"]
