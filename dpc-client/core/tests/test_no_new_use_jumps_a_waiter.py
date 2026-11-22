"""ADR-052, Confirmation test 3 (slice S1): no new use jumps a waiter (L5), one order across two lines (L5a).

(a) A dictation stream cannot starve a waiting music lease: while it waits for the Whisper lease,
new Whisper uses are not admitted; they wait behind it with their own arrivals, and when the busy
use ends the music lease is granted first.

(b), the lease's side: every lease waiter draws its arrival from the card queue's one clock, so a
lease waiter waits behind a card-queue place that came before it, even with the card free, and is
not held up by one that came after it; later lease contenders do not overtake an earlier one; and
`_LEASE_WAITERS()` hands the card queue those arrivals for S2. The supervisor's side of (b), a series
of llama-server and Strata starts that arrive after a waiting music lease and do not take the card
before it, and (c), a waiting music lease that swaps an idle supervisor itself (Mike #70), need the
supervisor to honour leases: slice S2, which writes them in this file.
"""

import asyncio

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
    embedder,
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


class TestADictationStreamCannotStarveMusic:
    @pytest.mark.asyncio
    async def test_new_whisper_uses_wait_behind_a_waiting_music_lease_and_music_goes_first(self):
        w, m = whisper(), music()
        first = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation 1", first))
        await until(lambda: holding() == ["whisper:turbo"])
        music_done = asyncio.Event()
        job = asyncio.create_task(hold_open(m.lease, "music job J", music_done))
        await until(lambda: waiting() == ["music:yue2"])

        granted = []

        async def later_dictation(i):
            async with w.lease.use(f"dictation {i}", wait_s=BOUND):
                granted.append(f"dictation {i}")

        stream = [asyncio.create_task(later_dictation(i)) for i in range(2, 5)]
        await until(lambda: len(waiting()) == 4)
        assert waiting() == ["music:yue2"] + ["whisper:turbo"] * 3, "each new use waits behind music"
        assert [u.what for u in w.lease._uses] == ["dictation 1"]
        arrivals = [arrival for arrival, _lease, _for_a_peer in base_mod._LEASE_WAITERS()]
        assert arrivals == sorted(arrivals)

        first.set()
        await until(lambda: m.lease._uses)
        assert holding() == ["music:yue2"] and granted == [], "music was granted before any later dictation"
        assert waiting() == ["whisper:turbo"] * 3, "the dictations now wait for the busy music job"
        await asyncio.sleep(0.05)
        assert granted == [] and m.yields == []

        music_done.set()
        await asyncio.wait_for(asyncio.gather(dictation, job, *stream), BOUND)
        assert sorted(granted) == ["dictation 2", "dictation 3", "dictation 4"]
        assert holding() == ["whisper:turbo"]
        await nothing_left()


class TestOneOrderAcrossBothLines:
    @pytest.mark.asyncio
    async def test_a_lease_waits_behind_an_earlier_card_queue_place_even_with_the_card_free(self):
        place = base_mod._CardPlace("flash")
        base_mod._CARD_QUEUE.append(place)
        w = whisper()
        acquire = asyncio.create_task(w.lease.acquire("dictation", wait_s=BOUND))
        await until(lambda: waiting() == ["whisper:turbo"])
        [(arrival, lease, for_a_peer)] = base_mod._LEASE_WAITERS()
        assert lease is w.lease and lease.alias == "whisper:turbo" and for_a_peer is False
        assert arrival > place.arrival, "one clock: the place came first"
        row = base_mod.card_queue()[-1]
        assert row["wait"]["where"] == "card_queue" and row["wait"]["behind"] == ["local engine 'flash'"]
        assert row["wait"]["wait_s"] is None
        await asyncio.sleep(0.05)
        assert not acquire.done()

        base_mod._CARD_QUEUE.remove(place)
        await asyncio.wait_for(acquire, BOUND)
        assert holding() == ["whisper:turbo"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_place_under_the_leases_own_alias_is_no_exception_to_the_order(self):
        """Johnny small 2 (review #56): `_blockers` does not compare aliases. Every lease excludes
        every supervisor, so an earlier place blocks an ungranted lease whatever alias it stands
        under, the lease's own name included; the grant is not at once."""
        w = whisper()
        place = base_mod._CardPlace(w.lease.alias)
        base_mod._CARD_QUEUE.append(place)
        try:
            acquire = asyncio.create_task(w.lease.acquire("dictation", wait_s=BOUND))
            await until(lambda: waiting() == ["whisper:turbo"])
            await asyncio.sleep(0.05)
            assert not acquire.done() and not w.lease.granted
            assert [kind for kind, _what, _state in card_lease._blockers(
                next(iter(card_lease._LEASE_LINE)))] == ["place"]
        finally:
            base_mod._CARD_QUEUE.remove(place)
        await asyncio.wait_for(acquire, BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_in_s1_a_card_queue_place_does_not_hold_up_a_use_of_a_granted_lease(self):
        """Intended for S1 (the comment in `_blockers`): a granted lease admits a new use unless it is
        draining or an earlier lease waiter waits for it. A supervisor's place waiting for a lease
        holder is S2's (`_blockers` gains it then); this pins today's behaviour so S2 changes it on
        purpose."""
        w = whisper()
        await w.lease.acquire("preload", wait_s=BOUND)
        place = base_mod._CardPlace("flash")
        base_mod._CARD_QUEUE.append(place)
        try:
            async with w.lease.use("dictation", wait_s=BOUND):
                assert [use.what for use in w.lease._uses] == ["dictation"]
        finally:
            base_mod._CARD_QUEUE.remove(place)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_card_queue_place_that_came_later_does_not_hold_a_lease_waiter_up(self):
        w, m = whisper(), music()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])
        later = base_mod._CardPlace("flash")
        base_mod._CARD_QUEUE.append(later)
        assert later.arrival > base_mod._LEASE_WAITERS()[0][0]

        release.set()
        await asyncio.wait_for(job, BOUND)
        await asyncio.wait_for(dictation, BOUND)
        assert holding() == ["music:yue2"]
        base_mod._CARD_QUEUE.remove(later)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_later_lease_contenders_do_not_overtake_an_earlier_one(self):
        w, m, e = whisper(), music(), embedder()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])
        music_done = asyncio.Event()
        job = asyncio.create_task(hold_open(m.lease, "music job J", music_done))
        await until(lambda: waiting() == ["music:yue2"])
        rebuild = asyncio.create_task(e.lease.acquire("index rebuild", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2", "embedder:BAAI/bge-m3"])
        row = base_mod.card_queue()[-1]
        assert row["wait"]["where"] == "busy_holder"
        assert row["wait"]["behind"] == ["Whisper (turbo) (dictation)", "yue2.cpp (music job J)"]

        release.set()
        await until(lambda: m.lease._uses)
        assert holding() == ["music:yue2"] and waiting() == ["embedder:BAAI/bge-m3"]
        assert not rebuild.done()

        music_done.set()
        await asyncio.wait_for(asyncio.gather(dictation, job, rebuild), BOUND)
        assert holding() == ["embedder:BAAI/bge-m3"]
        await nothing_left()
