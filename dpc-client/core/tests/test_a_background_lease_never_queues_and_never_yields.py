"""ADR-052, Confirmation test 7 (slice S1): a background lease never queues and never yields.

The rule-10 analog: `background=True` (the Whisper preload, the embedder's steady state) is granted
only when the card is free and nobody waits in either line; otherwise it meets `LeaseSkipped` at
once and the client stays on the CPU. It never takes a place, never asks a holder to let go, and a
granted lease's background use is admitted only where a foreground use would be (L5). Fake clients
stand for the preload and the embedder.
"""

import asyncio
import time

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseSkipped
from dpc_client_core.managers.llama_server_supervisor import card_queue_refusal_in

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


async def _skipped(lease, what, **kw):
    started = time.monotonic()
    with pytest.raises(LeaseSkipped) as skipped:
        await asyncio.wait_for(lease.acquire(what, wait_s=BOUND, background=True, **kw), BOUND)
    assert time.monotonic() - started < 0.5, "a skip never waits"
    assert waiting() == [], "a skip never takes a place"
    return skipped.value


class TestOnAFreeCard:
    @pytest.mark.asyncio
    async def test_the_preload_is_granted(self):
        w = whisper()
        await asyncio.wait_for(w.lease.acquire("preload", wait_s=BOUND, background=True), BOUND)
        assert holding() == ["whisper:turbo"]


class TestWhileAnythingHoldsTheCard:
    @pytest.mark.asyncio
    async def test_an_idle_holder_is_not_asked_and_the_preload_is_skipped(self):
        w, m = whisper(), music()
        await m.lease.acquire("engine start", wait_s=BOUND)

        refusal = await _skipped(w.lease, "preload")

        assert m.yields == [] and m.lease.state == "running"
        assert refusal.holder_alias == "music:yue2" and refusal.holder_engine == "yue2.cpp"
        assert "the card is not free right now" in str(refusal) and "yue2.cpp" in str(refusal)
        assert card_queue_refusal_in(refusal) is refusal
        assert not w.lease.granted
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_busy_holder_skips_the_embedders_steady_state(self):
        w, e = whisper(), embedder()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)

        async def steady_state():
            async with e.lease.use("memory search", wait_s=BOUND, background=True):
                pass

        with pytest.raises(LeaseSkipped):
            await asyncio.wait_for(steady_state(), BOUND)
        assert w.yields == [] and waiting() == [] and not e.lease.granted
        release.set()
        await asyncio.wait_for(dictation, BOUND)

    @pytest.mark.asyncio
    async def test_a_supervisor_holder_skips_it(self):
        class Supervisor:
            alias, ENGINE, port = "flash", "strata", None
            _proc, props, _draining = None, None, False

        base_mod._CARD_CLAIMS[Supervisor()] = "claimed"
        refusal = await _skipped(whisper().lease, "preload")
        assert refusal.holder_alias == "flash"


class TestWhileSomeoneWaits:
    @pytest.mark.asyncio
    async def test_a_waiting_card_queue_place_skips_it_on_a_free_card(self):
        base_mod._CARD_QUEUE.append(base_mod._CardPlace("flash"))
        await _skipped(whisper().lease, "preload")
        base_mod._CARD_QUEUE.clear()

    @pytest.mark.asyncio
    async def test_a_waiting_lease_skips_it(self):
        w, m, e = whisper(), music(), embedder()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])

        with pytest.raises(LeaseSkipped):
            await asyncio.wait_for(e.lease.acquire("memory search", wait_s=BOUND, background=True), BOUND)
        assert waiting() == ["music:yue2"]
        release.set()
        await asyncio.wait_for(asyncio.gather(dictation, job), BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_granted_lease_admits_a_background_use_only_while_nobody_waits_for_it(self):
        e, m = embedder(), music()
        await e.lease.acquire("index rebuild", wait_s=BOUND)

        async def steady_state():
            async with e.lease.use("memory search", wait_s=BOUND, background=True):
                pass

        await asyncio.wait_for(steady_state(), BOUND)  # re-entrant on its own grant

        release = asyncio.Event()
        rebuild = asyncio.create_task(hold_open(e.lease, "index rebuild", release))
        await until(lambda: e.lease._uses)
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])
        with pytest.raises(LeaseSkipped):
            await asyncio.wait_for(steady_state(), BOUND)
        assert waiting() == ["music:yue2"] and [u.what for u in e.lease._uses] == ["index rebuild"]
        release.set()
        await asyncio.wait_for(asyncio.gather(rebuild, job), BOUND)
        assert holding() == ["music:yue2"]
        await nothing_left()
