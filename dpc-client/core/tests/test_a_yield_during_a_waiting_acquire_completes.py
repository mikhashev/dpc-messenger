"""ADR-052, Confirmation test 14 (slice S1, from the Risks): a yield during a waiting acquire completes.

A lease is never acquired while a client lock is held; the client's yield does take that lock. So a
contender waits, the holder's `on_yield` takes the client lock (here a Whisper-like client whose
transcription takes the lease first and its `_load_lock` second, and whose yield unloads under that
lock), the yield finishes and the contender is granted. A transcription that arrives meanwhile
waits in the line holding no lock, so it cannot stop the yield; a yield that has to wait for the
lock (an idle unload still running) finishes once it gets it; and a contender that gives up never
leaves a yield half done. Fake clients only.
"""

import asyncio

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers.card_lease import CardLease

from tests._card_lease_helpers import BOUND, fast, holding, music, nothing_left, until, waiting  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast")


class LockedClient:
    """The lock order of ADR-052 section 3.1: the lease use first, then the client's own lock."""

    def __init__(self, holder="whisper:turbo"):
        self.lock = asyncio.Lock()
        self.loaded = False
        self.events = []
        self.lease = CardLease(holder, kind="transcription", engine="whisper", display="Whisper (turbo)",
                               vram_mib=None, yield_cost="cheap", on_yield=self.yield_card)

    async def transcribe(self, what, **kw):
        kw.setdefault("wait_s", BOUND)
        async with self.lease.use(what, **kw):
            async with self.lock:
                self.loaded = True
                self.events.append(f"transcribed {what}")

    async def yield_card(self, why):
        async with self.lock:
            if not self.loaded:
                return "nothing_to_give"
            self.loaded = False
            self.events.append("unloaded")
            return "let_go"


class TestTheYieldCompletes:
    @pytest.mark.asyncio
    async def test_the_yield_takes_the_client_lock_and_the_contender_is_granted(self):
        w, m = LockedClient(), music()
        await w.transcribe("dictation 1")
        assert w.lease.granted and w.loaded and not w.lock.locked(), "warm, and no lock held"

        await asyncio.wait_for(m.lease.acquire("music job J", wait_s=BOUND), BOUND)

        assert w.events == ["transcribed dictation 1", "unloaded"]
        assert holding() == ["music:yue2"] and not w.lease.granted
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_transcription_arriving_meanwhile_waits_in_line_holding_no_lock(self):
        w, m = LockedClient(), music()
        await w.transcribe("dictation 1")
        unload_may_finish = asyncio.Event()
        original = w.yield_card

        async def slow_yield(why):
            await unload_may_finish.wait()
            return await original(why)

        w.lease.rebind(slow_yield)
        music_done = asyncio.Event()

        async def job():
            async with m.lease.use("music job J", wait_s=BOUND):
                await music_done.wait()

        job_task = asyncio.create_task(job())
        await until(lambda: w.lease.state == "draining")
        dictation = asyncio.create_task(w.transcribe("dictation 2"))
        await until(lambda: waiting() == ["music:yue2", "whisper:turbo"])
        assert not w.lock.locked(), "the waiting transcription holds no client lock"

        unload_may_finish.set()
        await until(lambda: m.lease._uses)
        assert w.events == ["transcribed dictation 1", "unloaded"]
        assert waiting() == ["whisper:turbo"], "dictation 2 now waits for the busy music job"

        music_done.set()
        await asyncio.wait_for(asyncio.gather(job_task, dictation), BOUND)
        assert w.events[-1] == "transcribed dictation 2" and holding() == ["whisper:turbo"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_yield_that_waits_for_the_lock_finishes_once_it_has_it(self):
        w, m = LockedClient(), music()
        await w.transcribe("dictation 1")
        await w.lock.acquire()  # an idle unload of the client's own is still running
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: w.lease.state == "draining")
        await asyncio.sleep(0.05)
        assert not contender.done() and card_lease._YIELDS

        w.lock.release()
        await asyncio.wait_for(contender, BOUND)
        assert holding() == ["music:yue2"] and w.events[-1] == "unloaded"
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_contender_that_gives_up_leaves_no_half_done_yield(self):
        w, m = LockedClient(), music()
        await w.transcribe("dictation 1")
        await w.lock.acquire()
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: w.lease.state == "draining")
        contender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await contender

        w.lock.release()
        await nothing_left()
        assert w.events[-1] == "unloaded" and not w.lease.granted and holding() == []
        await asyncio.wait_for(w.transcribe("dictation 2"), BOUND)
        assert holding() == ["whisper:turbo"]
