"""ADR-052, Confirmation test 1 (slice S1): a lease is granted once, and two acquires cannot both pass.

The check that nothing is in the way and the grant are one synchronous stretch (L1), so concurrent
acquires of different leases never hold the card together. A granted lease is re-entrant: `use()`
opens one more use and grants nothing again. A use closes however its body ends; the grant stays (a
warm grant) until the client releases it, and releasing a lease that is not granted does nothing. A
thread client waits through `acquire_blocking`, which raises on the owner loop instead of
deadlocking (L9). Fake clients only (`tests/_card_lease_helpers.py`); no GPU.
"""

import asyncio
import logging

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseWaitedTooLong

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
    FakeClient,
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


def _granted_lines(caplog, display):
    return [r for r in caplog.records if r.getMessage().startswith(f"card: {display} granted")]


class TestOneGrantAtATime:
    @pytest.mark.asyncio
    async def test_two_concurrent_acquires_of_different_leases_cannot_both_pass(self):
        a, b = whisper(), music()
        release = asyncio.Event()
        tasks = [asyncio.create_task(hold_open(a.lease, "dictation", release)),
                 asyncio.create_task(hold_open(b.lease, "music job J", release))]
        await until(lambda: len(holding()) == 1 and len(waiting()) == 1)
        assert sorted(holding() + waiting()) == ["music:yue2", "whisper:turbo"]
        assert holding() == ["whisper:turbo"], "the first to ask is the first granted"

        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), BOUND)
        assert holding() == ["music:yue2"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_many_contenders_never_hold_the_card_together(self):
        clients = [FakeClient(f"client:{i}", engine=f"engine-{i}") for i in range(6)]
        inside, most = 0, 0

        async def job(client):
            nonlocal inside, most
            async with client.lease.use("job", wait_s=BOUND):
                inside += 1
                most = max(most, inside)
                assert holding() == [client.lease.alias]
                await asyncio.sleep(0.01)
                inside -= 1

        await asyncio.wait_for(asyncio.gather(*(job(c) for c in clients)), BOUND)
        assert most == 1
        assert len(holding()) == 1
        await nothing_left()


class TestAGrantedLeaseIsReEntrant:
    @pytest.mark.asyncio
    async def test_nested_and_parallel_uses_open_on_one_grant(self, caplog):
        caplog.set_level(logging.INFO, logger=card_lease.__name__)
        a = whisper()
        async with a.lease.use("dictation", wait_s=BOUND):
            async with a.lease.use("voice message", wait_s=BOUND):
                [row] = base_mod.card_holders()
                assert [u["what"] for u in row["uses"]] == ["dictation", "voice message"]
                assert row["busy"] is True and row["state"] == "running"
            release = asyncio.Event()
            parallel = [asyncio.create_task(hold_open(a.lease, f"call {i}", release)) for i in range(2)]
            await until(lambda: len(a.lease._uses) == 3)
            release.set()
            await asyncio.wait_for(asyncio.gather(*parallel), BOUND)

        assert a.lease._grant_no == 1
        assert len(_granted_lines(caplog, "Whisper (turbo)")) == 1
        [row] = base_mod.card_holders()
        assert row["uses"] == [] and row["busy"] is False, "warm: granted with no use"
        assert a.lease.state == "running"

    @pytest.mark.asyncio
    async def test_acquire_on_a_granted_lease_returns_at_once(self):
        a = whisper()
        await asyncio.wait_for(a.lease.acquire("preload", wait_s=BOUND), BOUND)
        await asyncio.wait_for(a.lease.acquire("again", wait_s=0), BOUND)
        assert a.lease._grant_no == 1 and holding() == ["whisper:turbo"]


class TestReleaseOnEveryPath:
    @pytest.mark.asyncio
    async def test_a_use_closes_however_its_body_ends_and_the_grant_stays_until_released(self, caplog):
        caplog.set_level(logging.INFO, logger=card_lease.__name__)
        a = whisper()
        async with a.lease.use("ends", wait_s=BOUND):
            pass
        with pytest.raises(ValueError):
            async with a.lease.use("fails", wait_s=BOUND):
                raise ValueError("the body failed")
        started = asyncio.Event()

        async def cancelled_body():
            async with a.lease.use("cancelled", wait_s=BOUND):
                started.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(cancelled_body())
        await asyncio.wait_for(started.wait(), BOUND)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert a.lease._uses == []
        assert a.lease.granted and a.lease.state == "running", "a closed use does not release the grant"
        a.lease.release("unloaded")
        assert not a.lease.granted and a.lease.state == "free" and holding() == []
        assert any(r.getMessage().startswith("card: Whisper (turbo) let go (unloaded), held ")
                   for r in caplog.records)

        lines = len(caplog.records)
        a.lease.release("again")
        music().lease.release("never granted")
        assert len(caplog.records) == lines, "releasing a lease that is not granted does nothing"

    @pytest.mark.asyncio
    async def test_a_waiter_that_is_cancelled_or_times_out_leaves_the_line_holding_nothing(self):
        a, b = whisper(), music()
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(a.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])

        waiter = asyncio.create_task(b.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert waiting() == [] and not b.lease.granted

        with pytest.raises(LeaseWaitedTooLong):
            await asyncio.wait_for(b.lease.acquire("music job J", wait_s=0.05), BOUND)
        assert waiting() == [] and not b.lease.granted
        assert a.yields == [], "a busy holder is never asked"

        release.set()
        await asyncio.wait_for(holder, BOUND)
        await nothing_left()


class TestAThreadClient:
    @pytest.mark.asyncio
    async def test_acquire_blocking_waits_on_the_owner_loop_from_a_thread(self):
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        e = embedder()
        await asyncio.wait_for(asyncio.to_thread(e.lease.acquire_blocking, "index rebuild", wait_s=BOUND), BOUND)
        assert e.lease.granted and holding() == ["embedder:BAAI/bge-m3"]

        await asyncio.to_thread(e.lease.release, "moved to the CPU")
        await until(lambda: not e.lease.granted)

    @pytest.mark.asyncio
    async def test_acquire_blocking_from_a_thread_waits_for_a_busy_holder(self):
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        a, e = whisper(), embedder()
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(a.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])

        thread = asyncio.create_task(asyncio.to_thread(e.lease.acquire_blocking, "index rebuild", wait_s=BOUND))
        await until(lambda: waiting() == ["embedder:BAAI/bge-m3"])
        assert not thread.done()
        release.set()
        await asyncio.wait_for(thread, BOUND)
        await asyncio.wait_for(holder, BOUND)
        assert holding() == ["embedder:BAAI/bge-m3"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_use_from_another_loop_opens_and_closes_on_the_owner_loop(self):
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        w = whisper()

        def tool_thread():
            async def call():
                async with w.lease.use("agent tool", wait_s=BOUND):
                    return [u.what for u in w.lease._uses]

            return asyncio.run(call())

        assert await asyncio.wait_for(asyncio.to_thread(tool_thread), BOUND) == ["agent tool"]
        await until(lambda: w.lease._uses == [])
        assert w.lease.granted and holding() == ["whisper:turbo"]

    @pytest.mark.asyncio
    async def test_acquire_blocking_on_the_owner_loop_raises_at_once(self):
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        e = embedder()
        with pytest.raises(RuntimeError, match="owner loop"):
            e.lease.acquire_blocking("index rebuild", wait_s=BOUND)
        assert not e.lease.granted and waiting() == []

    @pytest.mark.asyncio
    async def test_acquire_blocking_without_an_owner_loop_raises(self):
        e = embedder()
        with pytest.raises(RuntimeError, match="owner loop"):
            await asyncio.to_thread(e.lease.acquire_blocking, "index rebuild", wait_s=BOUND)
        assert not e.lease.granted
