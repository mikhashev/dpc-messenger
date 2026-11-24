"""ADR-052, Confirmation test 8 (slice S1): a peer's lease waits only when its estimate fits its deadline.

L13 (Mike #70, #85, #11), on ADR-044's amendment of 2026-11-18: a request served to a peer
(`serving_a_peer`) is granted at once when it can be; otherwise it takes a place only when its whole
trip is known and fits: the wait estimate (L10) plus its own `expected_s` within the guest's 120 s
(`PEER_TRIP_S`, L13 point 2) and within the bound its door gave, and is refused at once when the trip
is over it or any part of it is unknown (its own work too). A busy supervisor holding the card, an
idle one that the owner's terms do not let a peer stop (Mike, 2026-11-24: swap off, no stop figure,
a lease that swaps no engine out), and any owner's place in the card queue leave the estimate
unknown: refused at once. A waiting peer stands behind every owner's waiter, and an owner's
request that arrives later goes ahead of it. It asks an idle holder to let go only on the owner's
terms: the swap on, nobody of the owner's waiting. Every refusal to a peer says "right now", gives
the estimate when known, and names no alias, agent or conversation. Fake clients; the door (S3)
that computes a peer's own `expected_s` is not here.
"""

import asyncio
import time

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import PEER_TRIP_S, LeaseRefusedForPeer
from dpc_client_core.managers.llama_server_supervisor import card_held_for, serving_a_peer

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
    FakeClient,
    fast,
    hold_open,
    holding,
    music,
    nothing_left,
    swap,
    until,
    waiting,
    whisper,
)

pytestmark = pytest.mark.usefixtures("fast")

NAMES = ("whisper:", "turbo", "small", "music:", "yue2", "agent", "chat", "flash")


def _peer_whisper():
    return whisper("small")


async def _as_peer(coro_fn):
    with serving_a_peer("node-guest"):
        return await coro_fn()


async def _refused_at_once(lease, wait_s, expected_s=1):
    started = time.monotonic()
    with pytest.raises(LeaseRefusedForPeer) as refused:
        await asyncio.wait_for(
            _as_peer(lambda: lease.acquire("peer node-guest", wait_s=wait_s, expected_s=expected_s)), BOUND)
    assert time.monotonic() - started < 0.5, "refused at once, not at a deadline"
    assert lease.alias not in waiting(), "a refused peer takes no place"
    return refused.value


def _names_nothing(refusal, *, about_the_length=False):
    text = str(refusal)
    # An unknown length is a property of the request, not of the moment (S3 follow-up 5, Ark #44-1).
    assert ("how long this audio is" if about_the_length else "right now") in text
    for name in NAMES:
        assert name not in text, f"a peer was told {name!r}: {text}"
    assert refusal.holder_alias == "" and refusal.holder_engine == ""


class TestGrantedAtOnce:
    @pytest.mark.asyncio
    async def test_on_a_free_card_a_peer_is_served_at_once(self):
        p = _peer_whisper()
        await asyncio.wait_for(_as_peer(lambda: p.lease.acquire("peer node-guest", expected_s=1, wait_s=60)), BOUND)
        assert holding() == ["whisper:small"]


class TestAnEstimateThatFitsWaits:
    @pytest.mark.asyncio
    async def test_it_waits_and_is_granted(self):
        w, p = whisper(eta=10), _peer_whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)

        peer = asyncio.create_task(_as_peer(lambda: p.lease.acquire("peer node-guest", expected_s=1, wait_s=60)))
        await until(lambda: waiting() == ["whisper:small"])
        row = base_mod.card_queue()[0]
        assert row["wait"]["wait_s"] == 10 and row["until_cancelled"] is False

        release.set()
        await asyncio.wait_for(peer, BOUND)
        await asyncio.wait_for(dictation, BOUND)
        assert holding() == ["whisper:small"]
        assert [state for _why, state in w.yields] == ["draining"], "an idle holder lets go on the owner's terms"
        await nothing_left()

    @pytest.mark.asyncio
    async def test_it_stands_behind_every_owners_waiter_and_a_later_owner_goes_ahead(self):
        w, p, m = whisper(eta=0.3), _peer_whisper(), music(outcome="refused")
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        granted = []

        async def peer():
            await _as_peer(lambda: p.lease.acquire("peer node-guest", expected_s=1, wait_s=0.6))
            granted.append("peer")

        async def owner():
            await m.lease.acquire("music job J", wait_s=BOUND, expected_s=2)
            granted.append("owner")

        peer_task = asyncio.create_task(peer())
        await until(lambda: waiting() == ["whisper:small"])
        owner_task = asyncio.create_task(owner())
        await until(lambda: len(waiting()) == 2)
        assert waiting() == ["music:yue2", "whisper:small"], "the owner's later request stands first"

        release.set()
        await asyncio.wait_for(owner_task, BOUND)
        assert granted == ["owner"]
        with pytest.raises(LeaseRefusedForPeer) as refused:
            await asyncio.wait_for(peer_task, BOUND)
        _names_nothing(refused.value)
        await asyncio.wait_for(dictation, BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_an_owners_use_arriving_later_is_admitted_while_a_peer_waits(self):
        w, p = whisper(eta=5), _peer_whisper()
        first = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", first))
        await until(lambda: w.lease._uses)
        peer = asyncio.create_task(_as_peer(lambda: p.lease.acquire("peer node-guest", expected_s=1, wait_s=30)))
        await until(lambda: waiting() == ["whisper:small"])

        second = asyncio.Event()
        voice = asyncio.create_task(hold_open(w.lease, "voice message", second))
        await until(lambda: len(w.lease._uses) == 2)
        assert waiting() == ["whisper:small"], "a waiting peer does not hold an owner's use back (L5)"

        first.set()
        second.set()
        await asyncio.wait_for(asyncio.gather(dictation, voice, peer), BOUND)
        assert holding() == ["whisper:small"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_the_waiters_ahead_count_their_expected_time(self):
        w, m, p = whisper(eta=10), music(), _peer_whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND, expected_s=20))
        await until(lambda: waiting() == ["music:yue2"])

        assert (await _refused_at_once(p.lease, 25)).wait["wait_s"] == 30
        peer = asyncio.create_task(_as_peer(lambda: p.lease.acquire("peer node-guest", expected_s=1, wait_s=35)))
        await until(lambda: waiting() == ["music:yue2", "whisper:small"])
        peer.cancel()
        await asyncio.gather(peer, return_exceptions=True)
        release.set()
        await asyncio.wait_for(asyncio.gather(dictation, job), BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("door_bound", [None, 300], ids=["no bound from its door", "a door bound over 120 s"])
    async def test_a_peer_waits_no_longer_than_its_whole_trip_less_its_own_work(self, door_bound):
        # The inference peers' bound (`_a_peers_slot_wait_s`, 300 s here) plays no part.
        swap(True, wait_s=300, remote_timeout=1200)
        w, p = whisper(eta=10), _peer_whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        peer = asyncio.create_task(
            _as_peer(lambda: p.lease.acquire("peer node-guest", expected_s=19, wait_s=door_bound)))
        await until(lambda: waiting() == ["whisper:small"])
        [row] = base_mod.card_queue()
        assert row["until_cancelled"] is False
        assert row["give_up_at"] - row["queued_at"] <= PEER_TRIP_S
        [waiter] = card_lease._LEASE_LINE
        assert waiter.wait_s == PEER_TRIP_S - 19, "give_up_at - arrival = 120 - expected_s"
        assert row["give_up_at"] - row["queued_at"] == pytest.approx(PEER_TRIP_S - 19, abs=1)
        release.set()
        await asyncio.wait_for(asyncio.gather(dictation, peer), BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_shorter_door_bound_stays_the_bound(self):
        w, p = whisper(eta=10), _peer_whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        peer = asyncio.create_task(_as_peer(lambda: p.lease.acquire("peer node-guest", expected_s=19, wait_s=30)))
        await until(lambda: waiting() == ["whisper:small"])
        assert card_lease._LEASE_LINE[0].wait_s == 30
        release.set()
        await asyncio.wait_for(asyncio.gather(dictation, peer), BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_wait_and_its_own_work_that_together_fit_in_120_s_wait(self):
        # 100 s of others' work and 19 s of its own: 119 s.
        w, p = whisper(eta=100), _peer_whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        peer = asyncio.create_task(_as_peer(lambda: p.lease.acquire("peer node-guest", expected_s=19, wait_s=None)))
        await until(lambda: waiting() == ["whisper:small"])
        release.set()
        await asyncio.wait_for(asyncio.gather(dictation, peer), BOUND)
        await nothing_left()


class TestAnEstimateOverItOrUnknownIsRefusedAtOnce:
    @pytest.mark.asyncio
    async def test_a_wait_that_fits_alone_but_not_with_its_own_work_is_refused(self):
        # 110 s of others' work and 19 s of its own: 129 s, over the guest's 120 s.
        swap(True, wait_s=300, remote_timeout=1200)
        w, p = whisper(eta=110), _peer_whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)

        refusal = await _refused_at_once(p.lease, None, expected_s=19)

        _names_nothing(refusal)
        assert refusal.wait["known"] is True and refusal.wait["wait_s"] == 110
        release.set()
        await asyncio.wait_for(dictation, BOUND)

    @pytest.mark.asyncio
    async def test_its_own_work_unknown_is_refused_even_when_the_wait_fits(self):
        w, p = whisper(eta=5), _peer_whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)

        refusal = await _refused_at_once(p.lease, 60, expected_s=None)

        _names_nothing(refusal, about_the_length=True)
        assert refusal.wait["known"] is True and refusal.wait["wait_s"] == 5
        release.set()
        await asyncio.wait_for(dictation, BOUND)

    @pytest.mark.asyncio
    async def test_over_the_deadline(self):
        w, p = whisper(eta=200), _peer_whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)

        refusal = await _refused_at_once(p.lease, 60)

        _names_nothing(refusal)
        assert "for about 3 min" in str(refusal)
        assert refusal.wait["known"] is True and refusal.wait["wait_s"] == 200
        release.set()
        await asyncio.wait_for(dictation, BOUND)

    @pytest.mark.asyncio
    async def test_unknown(self):
        w, p = whisper(), _peer_whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)

        refusal = await _refused_at_once(p.lease, 60)

        _names_nothing(refusal)
        assert "how long is not known" in str(refusal)
        assert refusal.wait["wait_s"] is None
        release.set()
        await asyncio.wait_for(dictation, BOUND)

    @pytest.mark.asyncio
    async def test_a_supervisor_holder_has_no_estimate(self):
        class Supervisor:
            alias, ENGINE, port = "flash", "strata", None
            _proc, props, _draining = None, None, False

        base_mod._CARD_CLAIMS[Supervisor()] = "claimed"
        _names_nothing(await _refused_at_once(_peer_whisper().lease, 60))

    @staticmethod
    def _claim(busy: bool, stop_s=None):
        class Supervisor:
            alias, ENGINE, port = "flash", "strata", None
            _proc, props, _draining = type("Proc", (), {"returncode": None})(), {"total_slots": 1}, False

            def _busy_for_the_card(self, state, but_for=None, before=None):
                return busy

            def _expected_stop_s(self):
                return stop_s

        held = Supervisor()
        base_mod._CARD_CLAIMS[held] = "claimed"
        assert base_mod._live_claims() == [(held, "running")]
        return held

    @pytest.mark.asyncio
    @pytest.mark.parametrize("swap_on", [True, False], ids=["swap on", "swap off"])
    async def test_a_busy_running_supervisor_refuses_a_peer_at_once(self, swap_on):
        # A busy supervisor has no ETA (L10), and a peer waits for no busy holder (L13).
        swap(swap_on)
        self._claim(busy=True, stop_s=1.0)
        refusal = await _refused_at_once(whisper("small", waits_for_engines=True).lease, 60, expected_s=1)
        _names_nothing(refusal)
        assert refusal.wait["known"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ["swap off", "no stop figure", "a lease that waits for no engine"])
    async def test_an_idle_running_supervisor_refuses_a_peer_at_once_unless_it_may_be_stopped(self, case):
        # Mike, 2026-11-24: a peer may have an idle engine stopped on the owner's terms, its stop
        # figure counted in the trip (served: `test_a_peer_may_have_an_idle_engine_swapped_out_on_
        # the_owners_terms.py`). Without the swap, without a figure, or for a lease that never swaps
        # an engine out, nothing is stopped for it and its trip is unknown: refused at once.
        swap(case != "swap off")
        self._claim(busy=False, stop_s=None if case == "no stop figure" else 1.0)
        lease = whisper("small", waits_for_engines=case != "a lease that waits for no engine").lease
        refusal = await _refused_at_once(lease, 60, expected_s=1)
        _names_nothing(refusal)
        assert refusal.wait["known"] is False

    @pytest.mark.asyncio
    async def test_any_owners_place_in_the_card_queue_is_ahead_of_a_peer(self):
        p = _peer_whisper()
        base_mod._CARD_QUEUE.append(base_mod._CardPlace("flash"))
        refusal = await _refused_at_once(p.lease, 60)
        _names_nothing(refusal)
        base_mod._CARD_QUEUE.clear()

    @pytest.mark.asyncio
    async def test_with_the_swap_off_an_idle_holder_is_not_asked_for_a_peer(self):
        swap(False)
        w, p = whisper(), _peer_whisper()
        await w.lease.acquire("preload", wait_s=BOUND)

        _names_nothing(await _refused_at_once(p.lease, 60))
        assert w.yields == [] and w.lease.state == "running"

    @pytest.mark.asyncio
    async def test_with_the_swap_on_an_idle_cheap_holder_lets_go_for_a_peer(self):
        w, p = whisper(), _peer_whisper()
        await w.lease.acquire("preload", wait_s=BOUND)
        await asyncio.wait_for(_as_peer(lambda: p.lease.acquire("peer node-guest", expected_s=1, wait_s=60)), BOUND)
        assert holding() == ["whisper:small"] and len(w.yields) == 1
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_peer_still_waiting_at_its_deadline_is_refused_in_a_peers_words(self):
        w, p = whisper(eta=0.2), _peer_whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)

        with pytest.raises(LeaseRefusedForPeer) as refused:
            await asyncio.wait_for(_as_peer(lambda: p.lease.acquire("peer node-guest", expected_s=1, wait_s=0.3)), BOUND)

        _names_nothing(refused.value)
        assert waiting() == []
        release.set()
        await asyncio.wait_for(dictation, BOUND)

    @pytest.mark.asyncio
    async def test_inside_an_owners_turn_the_peer_words_still_name_nothing(self):
        w, p = whisper(eta=200), _peer_whisper()
        release = asyncio.Event()

        async def owners_turn():
            async with card_held_for(None, "accepted_request", agent_id="agent", conversation_id="chat"):
                await hold_open(w.lease, "agent 'agent' tool transcribe_audio_file", release)

        turn = asyncio.create_task(owners_turn())
        await until(lambda: w.lease._uses)
        _names_nothing(await _refused_at_once(p.lease, 60))
        release.set()
        await asyncio.wait_for(turn, BOUND)
