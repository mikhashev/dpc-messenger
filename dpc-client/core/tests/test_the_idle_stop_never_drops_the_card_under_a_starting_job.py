"""Race tests for `SupervisedEngine._idle_loop` and `stop("idle_unload")`:
1. While `stop("idle_unload")` waits on `proc.wait`, a starting job enters `in_use()` and calls
   `start()`: `idle_unload` must not release the card out from under the starting job.
2. When `_idle_loop` checks `_busy` outside `_the_lock()` and a job enters `in_use()` (or a
   contender marks `_draining`) before `_the_lock()` is acquired, `idle_unload` must re-check under
   `_the_lock()` and stand down without stopping the running server, while keeping `_idle_loop`
   alive for later idle periods.
3. End-to-end with `MusicJobRunner`: a job proposed while `idle_unload` is waiting in `proc.wait`
   completes in `review` instead of failing with `card_busy`.
4. A contender waiting while `idle_unload` finishes `proc.wait` (with no `in_use()` taken) still
   receives the released card cleanly.
5. A `Yue2Server.start(backbone=...)` arriving while `idle_unload` waits in `proc.wait` keeps the
   card and finishes switching backbones.
"""

import asyncio

import pytest

from dpc_client_core.music import job_schemas as js

from tests._acestep_helpers import Rig, free_env, from_text, install_fake_engine, make_engine, wait_until
from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, until, whisper  # noqa: F401
from tests._yue2_helpers import install_fake_yue2, make_yue2, yue2_env


@pytest.fixture
def ace(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    return home


def _gate_stop_wait(monkeypatch, engine, proc):
    """Pause `proc.wait()` inside `_stop_proc` (`engine._stopping` is True) with the child
    still alive until `allow_exit` is set, leaving `_watch()`'s own `proc.wait()` untouched."""
    in_wait, allow_exit = asyncio.Event(), asyncio.Event()
    real_wait = proc.wait
    real_signal = proc.send_signal
    deferred_sig = []

    def gated_signal(sig):
        if engine._stopping and not allow_exit.is_set():
            deferred_sig.append(sig)
            return
        real_signal(sig)

    async def gated_wait():
        if engine._stopping:
            in_wait.set()
            await allow_exit.wait()
            while deferred_sig:
                try:
                    real_signal(deferred_sig.pop(0))
                except ProcessLookupError:
                    pass
        return await real_wait()

    monkeypatch.setattr(proc, "send_signal", gated_signal)
    monkeypatch.setattr(proc, "wait", gated_wait)
    return in_wait, allow_exit


@pytest.mark.asyncio
async def test_idle_unload_waiting_in_proc_wait_keeps_the_card_when_in_use_enters_and_starts(ace, monkeypatch):
    engine = make_engine(ace, settings={"acestep_idle_stop_s": "0.1"})
    allow_exit = asyncio.Event()
    try:
        await asyncio.wait_for(engine.start(), 30)
        first_proc = engine._proc
        in_wait, allow_exit = _gate_stop_wait(monkeypatch, engine, first_proc)

        await asyncio.wait_for(in_wait.wait(), 10)
        assert engine._the_lock().locked() and engine.card_lease.granted

        started_ok = False

        async def starting_job():
            nonlocal started_ok
            async with engine.in_use("music job J", wait_s=BOUND):
                await engine.start()
                assert engine.alive() and engine._proc is not first_proc
                assert engine.state == "running" and engine.card_lease.granted
                started_ok = True

        job = asyncio.create_task(starting_job())
        await until(lambda: engine.busy)
        await asyncio.sleep(0.05)  # allow `starting_job` to pass `_card_for_a_start` and block on `_the_lock`

        allow_exit.set()
        await asyncio.wait_for(job, 30)
        assert started_ok is True
        assert engine.alive() and engine.state == "running"
        assert holding() == ["music:acestep"]

        # The new child also gets a working idle timer that unloads once idle again.
        await wait_until(lambda: not engine.alive(), timeout=10)
        await wait_until(lambda: not engine.card_lease.granted, timeout=5)
        assert engine._last_exit["why"] == "idle_unload"
    finally:
        allow_exit.set()
        await engine.shutdown()
    await nothing_left()


@pytest.mark.asyncio
async def test_idle_loop_rechecks_busy_under_the_lock_before_stopping(ace, monkeypatch):
    engine = make_engine(ace, settings={"acestep_idle_stop_s": "0.1"})
    try:
        await asyncio.wait_for(engine.start(), 30)
        proc = engine._proc
        lock = engine._the_lock()
        await lock.acquire()

        waiting_for_lock = asyncio.Event()
        real_acquire = lock.acquire

        async def spy_acquire():
            waiting_for_lock.set()
            return await real_acquire()

        monkeypatch.setattr(lock, "acquire", spy_acquire)
        await asyncio.wait_for(waiting_for_lock.wait(), 10)

        # `_idle_loop` has already passed its unlocked `_busy` check and is waiting for `_the_lock`.
        async with engine.in_use("music job J", wait_s=BOUND):
            lock.release()
            await asyncio.sleep(0.15)
            assert engine.alive() and engine._proc is proc, "must not stop a busy engine"
            assert engine.state == "running" and engine.card_lease.granted

        # Once `in_use` exits, the still-alive `_idle_loop` unloads the engine after `idle_stop_s`.
        await wait_until(lambda: not engine.alive(), timeout=10)
        await wait_until(lambda: not engine.card_lease.granted, timeout=5)
        assert engine._last_exit["why"] == "idle_unload"
    finally:
        if engine._the_lock().locked():
            engine._the_lock().release()
        await engine.shutdown()
    await nothing_left()


@pytest.mark.asyncio
async def test_idle_loop_stands_down_when_draining_starts_before_lock(ace, monkeypatch):
    engine = make_engine(ace, settings={"acestep_idle_stop_s": "0.1"})
    try:
        await asyncio.wait_for(engine.start(), 30)
        lock = engine._the_lock()
        await lock.acquire()

        waiting_for_lock = asyncio.Event()
        real_acquire = lock.acquire

        async def spy_acquire():
            waiting_for_lock.set()
            return await real_acquire()

        monkeypatch.setattr(lock, "acquire", spy_acquire)
        await asyncio.wait_for(waiting_for_lock.wait(), 10)

        # Simulate a contender marking the lease as draining just before `_idle_loop` acquires `_the_lock`.
        engine.card_lease._mark("whisper:turbo")
        lock.release()
        await asyncio.sleep(0.15)
        assert engine.alive(), "idle_unload must stand down when lease is already draining"
        assert engine.card_lease.granted

        engine.card_lease._end_swap_drain()
        await wait_until(lambda: not engine.alive(), timeout=10)
        await wait_until(lambda: not engine.card_lease.granted, timeout=5)
    finally:
        if engine._the_lock().locked():
            engine._the_lock().release()
        await engine.shutdown()
    await nothing_left()


@pytest.mark.asyncio
async def test_runner_job_completes_when_arriving_during_idle_unload_proc_wait(ace, monkeypatch):
    allow_exit = asyncio.Event()
    async with Rig(ace, start_runner=False, engine_settings={"acestep_idle_stop_s": "0.1"}) as rig:
        try:
            await asyncio.wait_for(rig.engine.start(), 30)
            in_wait, allow_exit = _gate_stop_wait(monkeypatch, rig.engine, rig.engine._proc)
            await asyncio.wait_for(in_wait.wait(), 10)

            rig.runner.start()
            job = rig.store.create_job("generate_from_text", from_text(variants=1))
            rig.runner.wake()

            await until(lambda: rig.engine.busy)
            await asyncio.sleep(0.05)  # allow runner to pass `_card_for_a_start` and block on `_the_lock`
            allow_exit.set()

            done = await rig.wait_for(job["id"], (js.REVIEW, js.FAILED, js.CANCELLED))
            assert done["state"] == js.REVIEW, done["state_reason"]
        finally:
            allow_exit.set()
    await nothing_left()


@pytest.mark.asyncio
async def test_contender_waiting_during_idle_unload_proc_wait_still_gets_the_released_card(ace, monkeypatch, fast):
    engine = make_engine(ace, settings={"acestep_idle_stop_s": "0.1"})
    allow_exit = asyncio.Event()
    try:
        await asyncio.wait_for(engine.start(), 30)
        in_wait, allow_exit = _gate_stop_wait(monkeypatch, engine, engine._proc)
        await asyncio.wait_for(in_wait.wait(), 10)

        w = whisper()
        waiter = asyncio.create_task(w.lease.acquire("dictation", wait_s=10))
        await asyncio.sleep(0.05)
        assert not waiter.done()

        allow_exit.set()
        await asyncio.wait_for(waiter, 10)
        assert holding() == ["whisper:turbo"]
        assert not engine.alive() and not engine.card_lease.granted
        w.lease.release("test")
    finally:
        allow_exit.set()
        await engine.shutdown()
    await nothing_left()


@pytest.mark.asyncio
async def test_yue2_backbone_restart_during_idle_unload_proc_wait_keeps_the_card(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_yue2(home, backbones=("stock", "rs-tw"))
    engine = make_yue2(home, settings={"yue2_idle_stop_s": "0.1"})
    allow_exit = asyncio.Event()
    try:
        await asyncio.wait_for(engine.start(backbone="stock"), 30)
        in_wait, allow_exit = _gate_stop_wait(monkeypatch, engine, engine._proc)
        await asyncio.wait_for(in_wait.wait(), 10)

        restart = asyncio.create_task(engine.start(backbone="rs-tw"))
        await until(lambda: engine.card_lease._starting)
        await asyncio.sleep(0.05)
        allow_exit.set()

        await asyncio.wait_for(restart, 30)
        assert engine.alive() and engine.running_backbone == "rs-tw" and engine.card_lease.granted
    finally:
        allow_exit.set()
        await engine.shutdown()
    await nothing_left()
