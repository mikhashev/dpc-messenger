"""ADR-052 L13, Mike's call of 2026-11-24 (Zcode's review of follow-up 8, point 1): a peer may have an idle
engine stopped for it only when the engine has been idle for `PEER_SWAP_IDLE_S` (60 s) since the end of the
owner's last request on it.

`caller_idle_s` covers a gateway client and a peer; an owner's chat or agent turn holds an
`accepted_request` for the length of the request and leaves no trace, so the engine was "idle" from the
first second after the owner's turn. `_owner_request_ended_at` is that trace, set when a hold leaves its
alias, success or failure, and `why_a_peer_may_not_swap` reads it.

Each case drives a real peer request through BOTH lines that read the predicate, with an injected clock:
the lease line (a peer's Whisper lease behind an idle Strata: `_swaps_engines`) and the engine swap line (a
peer's start of another local alias: `LlamaServerSupervisor._why_a_peer_may_not_swap`). Owner turn ended 2 s
ago: the peer is refused on both and nothing is stopped; 61 s ago: allowed on both. An alias that never
served the owner counts as idle.
"""

import asyncio

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseRefusedForPeer
from dpc_client_core.managers.llama_server_supervisor import CardQueueRefusal, card_held_for, serving_a_peer

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, whisper  # noqa: F401
from tests.test_an_owners_start_swaps_out_an_idle_local_engine_that_holds_the_card import _running_pair
from tests.test_music_swaps_out_an_idle_supervisor_when_nothing_waits import quick, running_strata  # noqa: F401
from tests.test_one_local_engine_holds_the_card import card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick")

PEER = "dpc-node-" + "b" * 32


class Clock:
    """The card's one clock (`_swap_clock`), set by the test."""

    def __init__(self, now: float = 10_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(base_mod, "_swap_clock", c)
    return c


async def owner_turn_on(alias):
    """One owner's chat or agent turn on `alias`: accepted, served, ended."""
    async with card_held_for(alias, "accepted_request", agent_id="agent", conversation_id="chat"):
        await asyncio.sleep(0)


async def lease_line_peer(tmp_path, clock, gap_s, *, turn=True):
    """A peer's Whisper (a lease that waits for engines) behind an idle Strata whose owner turn ended
    `gap_s` ago. Returns (strata, outcome): the lease line's answer, an exception or None."""
    strata, events = await running_strata(tmp_path)
    if turn:
        await owner_turn_on("flash")
    clock.now += gap_s
    peer = whisper("small", waits_for_engines=True)
    outcome = None
    try:
        with serving_a_peer(PEER):
            await asyncio.wait_for(peer.lease.acquire("peer node-guest", wait_s=None, expected_s=30), BOUND)
    except LeaseRefusedForPeer as refused:
        outcome = refused
    return strata, events, peer, outcome


async def engine_line_peer(tmp_path, clock, gap_s, *, turn=True):
    """A peer's start of the other local alias, beside an idle Strata whose owner turn ended `gap_s` ago."""
    holder, new = await _running_pair(tmp_path, "strata")
    if turn:
        await owner_turn_on(holder.alias)
    clock.now += gap_s
    outcome = None
    try:
        with serving_a_peer(PEER):
            await asyncio.wait_for(new.ensure_running(), BOUND)
    except CardQueueRefusal as refused:
        outcome = refused
    return holder, new, outcome


@pytest.mark.asyncio
async def test_two_seconds_after_the_owners_turn_the_lease_line_refuses_the_peer_and_stops_nothing(tmp_path, clock):
    strata, events, peer, outcome = await lease_line_peer(tmp_path, clock, 2)
    assert isinstance(outcome, LeaseRefusedForPeer), "the peer was refused"
    assert events == [] and strata._proc is not None and holding() == ["flash"]
    # The reason is said in a peer's words, with no name in it.
    assert "flash" not in str(outcome) and "owner" not in str(outcome)
    await nothing_left()


@pytest.mark.asyncio
async def test_sixty_one_seconds_after_the_owners_turn_the_lease_line_serves_the_peer(tmp_path, clock):
    strata, events, peer, outcome = await lease_line_peer(tmp_path, clock, 61)
    assert outcome is None and events[-1] == "stopped"
    assert strata._proc is None and holding() == ["whisper:small"]
    peer.lease.release("peer served")
    await nothing_left()


@pytest.mark.asyncio
async def test_two_seconds_after_the_owners_turn_the_engine_line_refuses_the_peer_and_stops_nothing(tmp_path, clock):
    holder, new, outcome = await engine_line_peer(tmp_path, clock, 2)
    assert isinstance(outcome, CardQueueRefusal) and "served this node's owner" in str(outcome)
    assert holder._proc is not None and new._proc is None and holder._draining is False
    assert [h["alias"] for h in base_mod.card_holders()] == [holder.alias]


@pytest.mark.asyncio
async def test_sixty_one_seconds_after_the_owners_turn_the_engine_line_swaps_for_the_peer(tmp_path, clock):
    holder, new, outcome = await engine_line_peer(tmp_path, clock, 61)
    assert outcome is None
    assert holder._proc is None and new._proc is not None
    assert [h["alias"] for h in base_mod.card_holders()] == [new.alias]


@pytest.mark.asyncio
async def test_an_engine_that_never_served_the_owner_counts_as_idle_on_both_lines(tmp_path, clock):
    _strata, _events, peer, outcome = await lease_line_peer(tmp_path, clock, 0, turn=False)
    assert outcome is None and holding() == ["whisper:small"]
    peer.lease.release("peer served")
    await nothing_left()
    base_mod._CARD_CLAIMS.clear()
    holder, new, outcome = await engine_line_peer(tmp_path / "second", clock, 0, turn=False)
    assert outcome is None and holder._proc is None and new._proc is not None


@pytest.mark.asyncio
async def test_a_failed_turn_leaves_the_mark_too(tmp_path, clock):
    holder, new = await _running_pair(tmp_path, "strata")
    with pytest.raises(RuntimeError):
        async with card_held_for(holder.alias, "accepted_request", agent_id="agent", conversation_id="chat"):
            raise RuntimeError("the turn failed")
    clock.now += 2
    assert "served this node's owner" in base_mod.why_a_peer_may_not_swap([(holder, "running")])
    clock.now += 59
    assert base_mod.why_a_peer_may_not_swap([(holder, "running")]) is None
