"""ADR-052 L13, Mike's decision of 2026-11-24: a peer MAY stop an idle engine, on the owner's terms.

ADR-044's amendment of 2026-11-06 lets an inference peer swap out an idle supervisor when the owner's
own conditions hold (`why_a_peer_may_not_swap`: the swap switch on, nothing in the card queue and no
owner's lease waiter, no holder busy or stopping). The lease line now reads the same predicate for a
peer's transcription behind an idle resident supervisor: when the terms hold, the supervisor's stop
counts its own figure (`_expected_stop_s()`), and the whole trip (that stop, Whisper's load and work,
the peer's `expected_s`) must fit the guest's 120 s (`PEER_TRIP_S`); then the peer waits, the
supervisor is stopped by the owner-start mechanism (`swap_out_for_a_lease`) and the peer is served.
An unknown stop figure leaves the trip unknown: refused at once (L10). A busy supervisor still
refuses the peer at once. Every refusal to a peer names no alias, engine, agent or chat (L3, 044
point 7).

Real Strata faked at its seams (`test_one_local_engine_holds_the_card.py`); fake lease clients.
"""

import asyncio
import time

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseRefusedForPeer
from dpc_client_core.managers.llama_server_supervisor import card_held_for, serving_a_peer

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
    fast,
    holding,
    nothing_left,
    swap,
    until,
    waiting,
    whisper,
)
from tests.test_music_swaps_out_an_idle_supervisor_when_nothing_waits import quick, running_strata  # noqa: F401
from tests.test_one_local_engine_holds_the_card import card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick")

NAMES = ("whisper:", "small", "turbo", "flash", "strata", "Strata", "agent", "chat")


def _peer_whisper():
    """A peer's Whisper lease: the real one waits for engines (item 1a, since S3b)."""
    return whisper("small", waits_for_engines=True)


async def _as_peer(coro_fn):
    with serving_a_peer("node-guest"):
        return await coro_fn()


async def _refused_at_once(lease, expected_s, wait_s=None):
    started = time.monotonic()
    with pytest.raises(LeaseRefusedForPeer) as refused:
        await asyncio.wait_for(
            _as_peer(lambda: lease.acquire("peer node-guest", wait_s=wait_s, expected_s=expected_s)), BOUND)
    assert time.monotonic() - started < 0.5, "refused at once, not at a deadline"
    assert lease.alias not in waiting(), "a refused peer takes no place"
    text = str(refused.value)
    assert "right now" in text
    for name in NAMES:
        assert name not in text, f"a peer was told {name!r}: {text}"
    assert refused.value.holder_alias == "" and refused.value.holder_engine == ""
    return refused.value


@pytest.mark.asyncio
async def test_idle_with_the_swap_on_and_a_trip_that_fits_the_engine_is_stopped_and_the_peer_served(tmp_path):
    strata, events = await running_strata(tmp_path)
    assert strata._expected_stop_s() == pytest.approx(20.05)
    p = _peer_whisper()
    await asyncio.wait_for(_as_peer(lambda: p.lease.acquire("peer node-guest", wait_s=None, expected_s=30)), BOUND)
    assert events == ["parked read", "live saved", "stopped"]
    assert strata._proc is None and holding() == ["whisper:small"]
    last = base_mod.last_swap()
    assert (last["from"], last["to"]) == ("flash", "whisper:small")
    p.lease.release("peer served")
    await nothing_left()


@pytest.mark.asyncio
async def test_the_peers_estimate_counts_the_stop_figure(tmp_path):
    strata, _events = await running_strata(tmp_path)
    strata._kv_loaded_stop_s = 7.5
    p = _peer_whisper()
    view = await asyncio.wait_for(_as_peer(lambda: card_lease.route_view(p.lease, expected_s=30)), BOUND)
    assert view["known"] is False and view["engine_stays"] is True, "L12's view is item 1a's, unchanged"
    probe = card_lease._LeaseWaiter(p.lease, "probe", None, True, 30)
    assert probe.for_a_peer is False  # built outside the peer mark: the owner's reading, no swap priced
    with serving_a_peer("node-guest"):
        peer_probe = card_lease._LeaseWaiter(p.lease, "probe", None, True, 30)
    estimate = card_lease._estimate(peer_probe)
    assert estimate["known"] is True and estimate["wait_s"] == pytest.approx(7.5)
    assert estimate["where"] == "yielding"


@pytest.mark.asyncio
async def test_with_the_swap_off_the_peer_is_refused_at_once_and_nothing_is_stopped(tmp_path):
    swap(False)
    strata, events = await running_strata(tmp_path)
    refusal = await _refused_at_once(_peer_whisper().lease, 30)
    assert refusal.wait["known"] is False
    assert events == [] and strata._proc is not None and holding() == ["flash"]
    await nothing_left()


@pytest.mark.asyncio
async def test_an_owners_waiter_in_the_line_refuses_the_peer_at_once(tmp_path):
    strata, events = await running_strata(tmp_path)
    owner = whisper("turbo")  # waits for no engine: it stands in the line and swaps nothing out
    dictation = asyncio.create_task(owner.lease.acquire("dictation", wait_s=None, expected_s=1))
    await until(lambda: waiting() == ["whisper:turbo"])
    refusal = await _refused_at_once(_peer_whisper().lease, 30)
    assert refusal.wait["known"] is False
    await asyncio.sleep(0.05)
    assert events == [] and strata._proc is not None and holding() == ["flash"]
    dictation.cancel()
    await asyncio.gather(dictation, return_exceptions=True)
    await nothing_left()


@pytest.mark.asyncio
async def test_an_owners_place_in_the_card_queue_refuses_the_peer_at_once(tmp_path):
    strata, events = await running_strata(tmp_path)
    base_mod._CARD_QUEUE.append(base_mod._CardPlace("qwen27"))
    try:
        await _refused_at_once(_peer_whisper().lease, 30)
    finally:
        base_mod._CARD_QUEUE.clear()
    assert events == [] and strata._proc is not None
    await nothing_left()


@pytest.mark.asyncio
async def test_a_stop_and_work_over_120_s_is_refused_at_once_and_nothing_is_stopped(tmp_path):
    strata, events = await running_strata(tmp_path)
    strata._kv_loaded_stop_s = 100.0
    refusal = await _refused_at_once(_peer_whisper().lease, 30)
    assert refusal.wait["known"] is True and refusal.wait["wait_s"] == pytest.approx(100.0)
    await asyncio.sleep(0.05)
    assert events == [] and strata._proc is not None and strata._draining is False
    await nothing_left()


@pytest.mark.asyncio
async def test_an_unknown_stop_figure_leaves_the_trip_unknown_and_refuses_at_once(tmp_path, monkeypatch):
    strata, events = await running_strata(tmp_path)
    monkeypatch.setattr(strata, "_expected_stop_s", lambda: None)
    refusal = await _refused_at_once(_peer_whisper().lease, 30)
    assert refusal.wait["known"] is False
    assert events == [] and strata._proc is not None
    await nothing_left()


@pytest.mark.asyncio
async def test_a_busy_engine_still_refuses_the_peer_at_once(tmp_path):
    strata, events = await running_strata(tmp_path)
    over = asyncio.Event()

    async def turn():
        async with card_held_for("flash", "accepted_request", agent_id="agent", conversation_id="chat"):
            await over.wait()

    on_strata = asyncio.create_task(turn())
    await until(lambda: base_mod._CARD_HOLDS.get("flash"))
    refusal = await _refused_at_once(_peer_whisper().lease, 30)
    assert refusal.wait["known"] is False
    assert events == [] and strata._draining is False
    over.set()
    await asyncio.wait_for(on_strata, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_lease_that_waits_for_no_engine_never_stops_one_for_a_peer(tmp_path):
    strata, events = await running_strata(tmp_path)
    await _refused_at_once(whisper("small").lease, 30)
    assert events == [] and strata._proc is not None
    await nothing_left()


@pytest.mark.asyncio
async def test_the_owners_terms_are_one_predicate_for_both_lines(tmp_path):
    """The module predicate `why_a_peer_may_not_swap` answers each of the owner's terms. This pins the
    predicate only: it calls neither `_why_a_peer_may_not_swap` nor `_swaps_engines`, so it would pass if
    one of the two lines stopped reading it. That both lines read it is pinned, with a real peer request
    through each, in `test_a_peer_may_swap_an_engine_only_a_minute_after_the_owners_last_request.py`."""
    strata, _events = await running_strata(tmp_path)
    holders = [(strata, "running")]
    assert base_mod.why_a_peer_may_not_swap(holders) is None
    swap(False)
    assert "not turned on swapping" in base_mod.why_a_peer_may_not_swap(holders)
    swap(True)
    strata._in_flight = 1
    try:
        assert "busy" in base_mod.why_a_peer_may_not_swap(holders)
    finally:
        strata._in_flight = 0
    base_mod._CARD_QUEUE.append(base_mod._CardPlace("qwen27"))
    try:
        assert "waiting for the card" in base_mod.why_a_peer_may_not_swap(holders)
    finally:
        base_mod._CARD_QUEUE.clear()
