"""ADR-052, slice S1 follow-up (review #54-#57): the values and contracts the first S1 tests left unpinned.

- `seconds_from_ratio`: the ratio is audio seconds per wall second, so the time is a division
  (30 s at 20x is 1.5 s; a multiplication would say 600 s). Ark's falsifier of test 37, pinned where
  the orientation lives.
- `_LEASE_WAITERS()` carries `for_a_peer`, which L5a's supervisor side needs (owners before peers).
- The busy test with `but_for`: a use opened outside a request counts as busy.
- The cheap idle holder's estimate is 1.0 s, an engine's is unknown; `mark_starting` and
  `mark_running` move the holder in and out of busy.
- `let_go_by_hand` answers False when its bound runs out, as its docstring says, and the yield goes on.
- A deadline refusal for a waiter with no bound of its own does not raise.
"""

import asyncio
import math

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseWaitedTooLong, seconds_from_ratio
from dpc_client_core.managers.llama_server_supervisor import serving_a_peer

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
    fast,
    hold_open,
    holding,
    music,
    nothing_left,
    until,
    waiting,
    whisper,
)

pytestmark = pytest.mark.usefixtures("fast")


def _row(alias):
    return next(row for row in base_mod.card_queue() if row["alias"] == alias)


class TestTheRatioIsAnOrientationNotJustANumber:
    def test_thirty_seconds_at_twenty_times_real_time_is_one_and_a_half_seconds(self):
        assert seconds_from_ratio(30, 20) == 1.5
        assert seconds_from_ratio(30, 20) != 30 * 20, "a multiplication would give 600"
        assert seconds_from_ratio(118, 27.4) == pytest.approx(118 / 27.4)
        assert seconds_from_ratio(60, 0.5) == 120, "a slower than real time device takes longer"

    @pytest.mark.parametrize("audio, ratio", [
        (None, 20), (30, None), (0, 20), (30, 0), (-30, 20), (30, -20), (math.nan, 20), (30, math.nan),
        (math.inf, 20), (30, math.inf), ("long", 20), (30, "fast"),
    ])
    def test_anything_unknown_is_unknown_never_zero(self, audio, ratio):
        assert seconds_from_ratio(audio, ratio) is None


class TestTheWaiterHookCarriesForAPeer:
    @pytest.mark.asyncio
    async def test_an_owner_is_false_a_peer_is_true_and_the_owner_is_listed_first(self):
        w, owner, peer = whisper(eta=10), music(), whisper("small")
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])

        async def as_peer():
            with serving_a_peer("node-guest"):
                await peer.lease.acquire("peer node-guest", wait_s=60)

        guest = asyncio.create_task(as_peer())
        await until(lambda: waiting() == ["whisper:small"])
        job = asyncio.create_task(owner.lease.acquire("music job J", wait_s=BOUND, expected_s=5))
        await until(lambda: waiting() == ["music:yue2", "whisper:small"])

        [(owner_arrival, owner_lease, owner_for_a_peer), (peer_arrival, peer_lease, peer_for_a_peer)] = \
            base_mod._LEASE_WAITERS()
        assert owner_lease is owner.lease and owner_for_a_peer is False
        assert peer_lease is peer.lease and peer_for_a_peer is True
        assert owner_arrival > peer_arrival, "the owner came later and still stands first (rule 7)"

        for task in (job, guest):
            task.cancel()
        await asyncio.gather(job, guest, return_exceptions=True)
        release.set()
        await asyncio.wait_for(dictation, BOUND)
        await nothing_left()


class TestTheBusyTestWithButFor:
    @pytest.mark.asyncio
    async def test_a_use_outside_a_request_is_busy_unless_it_is_the_askers_own(self):
        w = whisper()
        await w.lease.acquire("preload", wait_s=BOUND)
        assert w.lease._busy_for_the_card("running") is False, "a warm grant with no use is idle"

        outside = w.lease._open("dictation", arrival=1, hold=None)
        mine, other = object(), object()
        assert w.lease._busy_for_the_card("running") is True
        assert w.lease._busy_for_the_card("running", but_for=None) is True
        assert w.lease._busy_for_the_card("running", but_for=mine) is True, \
            "a use opened outside any request (hold None) is not the asker's own"
        w.lease._close_now(outside)

        w.lease._open("agent turn", arrival=2, hold=mine)
        assert w.lease._busy_for_the_card("running") is True
        assert w.lease._busy_for_the_card("running", but_for=mine) is False, "the asker's own use does not count"
        assert w.lease._busy_for_the_card("running", but_for=other) is True

    @pytest.mark.asyncio
    async def test_before_leaves_out_a_use_that_arrived_after_the_acting_waiter(self):
        w = whisper()
        await w.lease.acquire("preload", wait_s=BOUND)
        w.lease._open("dictation", arrival=10, hold=None)
        assert w.lease._busy_for_the_card("running", before=11) is True
        assert w.lease._busy_for_the_card("running", before=10) is False
        assert w.lease._busy_for_the_card("running", before=5) is False


class TestTheCheapIdleEstimate:
    def test_a_cheap_holder_is_one_second_and_an_engine_is_unknown(self):
        assert card_lease._CHEAP_YIELD_S == 1.0
        assert card_lease._yield_estimate(whisper().lease) == 1.0
        assert card_lease._yield_estimate(music().lease) is None

    @pytest.mark.asyncio
    async def test_an_idle_whisper_in_the_way_of_music_reads_as_one_second(self):
        gate = asyncio.Event()
        w, m = whisper(gate=gate), music()
        await w.lease.acquire("preload", wait_s=BOUND)
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: w.yields)

        assert _row("music:yue2")["wait"] == {
            "wait_s": 1.0, "known": True, "behind": ["Whisper (turbo)"], "where": "yielding"}
        gate.set()
        await asyncio.wait_for(contender, BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_an_idle_music_engine_in_the_way_of_whisper_is_unknown_not_one_second(self):
        gate = asyncio.Event()
        m, w = music(gate=gate), whisper()
        await m.lease.acquire("preload", wait_s=BOUND)
        contender = asyncio.create_task(w.lease.acquire("dictation", wait_s=BOUND))
        await until(lambda: m.yields)

        wait = _row("whisper:turbo")["wait"]
        assert wait["wait_s"] is None and wait["known"] is False and wait["where"] == "yielding"
        gate.set()
        await asyncio.wait_for(contender, BOUND)
        await nothing_left()


class TestStartingAndRunning:
    @pytest.mark.asyncio
    async def test_a_loading_holder_is_busy_and_is_not_asked_until_it_says_it_is_running(self):
        w, m = whisper(), music()
        await w.lease.acquire("preload", wait_s=BOUND)
        w.lease.mark_starting()
        assert w.lease.state == "starting"
        assert w.lease._busy_for_the_card(w.lease.state) is True
        assert w.lease.who_uses() == "loading"
        assert next(r for r in base_mod.card_holders() if r["alias"] == "whisper:turbo")["busy"] is True

        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])
        await asyncio.sleep(0.05)
        assert w.yields == [], "a holder that is loading is busy: waited for, never asked"
        assert _row("music:yue2")["wait"]["where"] == "busy_holder"

        w.lease.mark_running()
        assert w.lease._busy_for_the_card("running") is False
        await asyncio.wait_for(contender, BOUND)
        assert [state for _why, state in w.yields] == ["draining"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_marking_a_lease_that_is_not_granted_does_nothing(self):
        w = whisper()
        w.lease.mark_starting()
        assert w.lease.state == "free" and w.lease._busy_for_the_card("free") is False
        w.lease.mark_running()
        assert w.lease.state == "free"


class TestAPersonsStopWithABound:
    @pytest.mark.asyncio
    async def test_a_bound_that_runs_out_answers_false_and_the_yield_goes_on(self):
        gate = asyncio.Event()
        w = whisper(gate=gate)
        await w.lease.acquire("preload", wait_s=BOUND)

        assert await asyncio.wait_for(w.lease.let_go_by_hand("Stop by hand", 0.05), BOUND) is False
        assert w.lease.state == "draining" and w.lease.granted, "the yield was not cut in half"
        assert len(w.yields) == 1

        gate.set()
        await until(lambda: not w.lease.granted)
        assert w.loaded is False
        await nothing_left()


class TestADeadlineRefusalWithNoBoundOfItsOwn:
    @pytest.mark.asyncio
    async def test_it_words_the_refusal_instead_of_raising_a_type_error(self):
        w, m = whisper(), music()
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])
        waiter = card_lease._LeaseWaiter(m.lease, "music job J", None, False, None)
        assert waiter.wait_s is None and math.isinf(waiter.give_up_at)

        refusal = card_lease._refusal_at_the_deadline(waiter, card_lease._blockers(waiter))

        assert isinstance(refusal, LeaseWaitedTooLong)
        assert "waited for the card and was not granted it" in str(refusal)
        assert "Whisper (turbo) (busy: dictation)" in str(refusal)
        release.set()
        await asyncio.wait_for(holder, BOUND)
        await nothing_left()
