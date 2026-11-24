"""ADR-052 rule L8 (slice S4; Zcode #87-4): a music engine lets go of the card however its child's
stop ends, also when that stop is cut short.

Three windows the review named, each closed here:

- a failed start whose own stop is cancelled a second time (a double cancel) used to leave the child
  counted as alive, so `_start_child`'s release (`if not self.alive()`) never ran;
- `proc.kill()` that raises an `OSError` other than `ProcessLookupError` (access denied, as Windows
  answers for a process that is exiting) used to escape the stop before the card was let go;
- `shutdown()` caught only `Exception`, so a cancellation (for instance while the stop waited for
  `_the_lock`) skipped the release.

Follow-up 10 (Zcode #14): a `shutdown()` cancelled while a start owns `_the_lock` and has not yet
spawned its child must not release the card under that start (its child would come up with no lease,
unseen by L11); and a failed start whose stop is cancelled a second time ends in the `error` state, not
`starting`.

The child is an in-process fake: it ignores the stop signal, and the test decides when (and whether)
a kill ends it. A cancellation is never swallowed: it goes on after the release.
"""

import asyncio
import logging

import pytest

from dpc_client_core.music import engine_base

from tests._acestep_helpers import free_env, install_fake_engine, make_engine
from tests._card_lease_helpers import BOUND, until


class _Child:
    """A child process that ignores its stop signal. `kill_error` is raised by `kill()`; with
    `exits_anyway` the child is gone all the same (it was already exiting), else a failed kill leaves
    it running."""

    pid = None  # never a registered pid: the process registry is not part of these tests

    def __init__(self, kill_error=None, exits_anyway=False):
        self.returncode = None
        self.signals = []
        self.kills = 0
        self.kill_error = kill_error
        self.exits_anyway = exits_anyway
        self._gone = asyncio.Event()

    def send_signal(self, sig):
        self.signals.append(sig)

    def _exit(self, code):
        self.returncode = code
        self._gone.set()

    def kill(self):
        self.kills += 1
        if self.kill_error is not None:
            if self.exits_anyway:
                self._exit(1)
            raise self.kill_error
        self._exit(-9)

    async def wait(self):
        await self._gone.wait()
        return self.returncode


class _ObedientChild(_Child):
    """A child that exits on its stop signal, as a healthy server does."""

    def send_signal(self, sig):
        super().send_signal(sig)
        self._exit(0)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    return make_engine(home)


async def _running(engine, child):
    """`engine` as a start leaves it: the card granted, `child` running."""
    await asyncio.wait_for(engine.card_lease.acquire("start of acestep.cpp", wait_s=0), BOUND)
    engine.card_lease.mark_running()
    engine._proc = child
    engine._set_state("running")


async def _a_start_cancelled_twice(engine, monkeypatch, child):
    """A start that boots for ever, cancelled once (its own stop begins; `child` ignores the signal) and
    then a second time inside that stop's wait. Returns after the second cancel has surfaced."""
    booting = asyncio.Event()

    async def spawn(cmd):
        return child

    async def wait_healthy(proc, port):
        booting.set()
        await asyncio.Event().wait()  # boots for ever: only a cancel ends it

    monkeypatch.setattr(engine, "_spawn", spawn)
    monkeypatch.setattr(engine, "_wait_healthy", wait_healthy)
    start = asyncio.create_task(engine.start())
    await asyncio.wait_for(booting.wait(), BOUND)
    assert engine.card_lease.granted and engine.alive()

    start.cancel()  # the first cancel: the failed start stops its child, which ignores the signal
    await until(lambda: child.signals)
    await asyncio.sleep(0.01)
    start.cancel()  # the second cancel lands inside that stop's wait
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(start, BOUND)


@pytest.mark.asyncio
async def test_a_failed_start_cancelled_twice_kills_its_child_and_lets_go(engine, monkeypatch):
    child = _Child()
    await _a_start_cancelled_twice(engine, monkeypatch, child)

    assert child.kills >= 1 and child.returncode is not None, "the child is killed, not left behind"
    assert not engine.alive()
    await until(lambda: not engine.card_lease.granted)


@pytest.mark.asyncio
async def test_a_failed_start_cancelled_twice_ends_in_the_error_state_not_starting(engine, monkeypatch):
    child = _Child()
    await _a_start_cancelled_twice(engine, monkeypatch, child)

    assert engine.state == "error", "the second cancel re-raised out of the stop; the state is still set"
    assert "did not start" in engine.status().reason


@pytest.mark.asyncio
async def test_a_kill_that_raises_access_denied_still_lets_go(engine, monkeypatch):
    monkeypatch.setattr(engine_base, "STOP_GRACE_S", 0.05)
    child = _Child(kill_error=PermissionError(13, "Access is denied"), exits_anyway=True)
    await _running(engine, child)

    assert await asyncio.wait_for(engine.stop("ui_stop"), BOUND) is True

    assert child.kills == 1 and not engine.alive()
    await until(lambda: not engine.card_lease.granted)


@pytest.mark.asyncio
async def test_a_child_that_outlives_its_kill_is_logged_and_the_card_is_let_go(engine, monkeypatch, caplog):
    monkeypatch.setattr(engine_base, "STOP_GRACE_S", 0.05)
    child = _Child(kill_error=PermissionError(13, "Access is denied"))
    await _running(engine, child)

    with caplog.at_level(logging.WARNING, logger=engine_base.logger.name):
        await asyncio.wait_for(engine.stop("ui_stop"), BOUND)

    assert child.returncode is None, "the fake child is still there: the kill failed"
    assert any("could not kill" in r.getMessage() for r in caplog.records)
    assert any("has not exited" in r.getMessage() for r in caplog.records)
    assert not engine.alive(), "no longer counted, so the stop goes on and the card is let go"
    await until(lambda: not engine.card_lease.granted)


@pytest.mark.asyncio
async def test_a_shutdown_cancelled_before_its_stop_could_run_kills_the_child_and_lets_go(engine):
    child = _Child()
    await _running(engine, child)
    lock = engine._the_lock()
    await lock.acquire()  # a start or a stop under way: the shutdown's stop waits for the lock
    try:
        shutdown = asyncio.create_task(engine.shutdown())
        await asyncio.sleep(0.01)
        assert not shutdown.done()
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown, BOUND)
    finally:
        lock.release()

    assert child.kills == 1 and child.returncode is not None
    await until(lambda: not engine.card_lease.granted)


async def _a_start_that_has_not_spawned(engine, monkeypatch, child, *, healthy):
    """A start under `_the_lock` with the card granted, held in `_spawn` (`_proc` still None). Returns the
    start task and the event that lets the spawn go on; `healthy` decides how the boot then ends."""
    in_spawn, go_on = asyncio.Event(), asyncio.Event()

    async def spawn(cmd):
        in_spawn.set()
        await go_on.wait()
        return child

    async def wait_healthy(proc, port):
        if not healthy:
            raise engine_base.MusicEngineError("start_failed", "no healthy /health")

    async def props(path):
        return {"version": "test"}

    monkeypatch.setattr(engine, "_spawn", spawn)
    monkeypatch.setattr(engine, "_wait_healthy", wait_healthy)
    monkeypatch.setattr(engine, "_get_json", props)
    monkeypatch.setattr(engine, "_check_props", lambda p: None)
    start = asyncio.create_task(engine.start())
    await asyncio.wait_for(in_spawn.wait(), BOUND)
    assert engine._proc is None and engine._the_lock().locked() and engine.card_lease.granted
    return start, go_on


async def _a_shutdown_cancelled_while_the_start_owns_the_engine(engine):
    shutdown = asyncio.create_task(engine.shutdown())
    await asyncio.sleep(0.01)  # its stop waits for the lock the start holds
    assert not shutdown.done()
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(shutdown, BOUND)


@pytest.mark.asyncio
async def test_a_shutdown_cancelled_before_the_spawn_leaves_the_card_to_the_start_that_owns_the_engine(
        engine, monkeypatch):
    child = _ObedientChild()
    start, go_on = await _a_start_that_has_not_spawned(engine, monkeypatch, child, healthy=True)

    await _a_shutdown_cancelled_while_the_start_owns_the_engine(engine)
    assert engine.card_lease.granted, "the start's grant is not the shutdown's to release"

    go_on.set()
    await asyncio.wait_for(start, BOUND)
    try:
        assert engine.alive() and engine.state == "running"
        assert engine.card_lease.granted, "the child that came up holds the lease, visible to L11"
    finally:
        await asyncio.wait_for(engine.shutdown(), BOUND)
    await until(lambda: not engine.card_lease.granted)


@pytest.mark.asyncio
async def test_a_shutdown_cancelled_before_the_spawn_then_a_failed_start_lets_go_exactly_once(
        engine, monkeypatch):
    child = _ObedientChild()
    start, go_on = await _a_start_that_has_not_spawned(engine, monkeypatch, child, healthy=False)
    released = []
    real = engine._let_go_of_the_card
    monkeypatch.setattr(engine, "_let_go_of_the_card", lambda why: (released.append(why), real(why))[1])

    await _a_shutdown_cancelled_while_the_start_owns_the_engine(engine)
    assert released == [], "the shutdown leaves the card to the start"

    go_on.set()
    with pytest.raises(engine_base.MusicEngineError):
        await asyncio.wait_for(start, BOUND)

    assert released == ["failed_start"], "the start lets go once, on its own failure path"
    assert not engine.alive() and child.returncode is not None
    await until(lambda: not engine.card_lease.granted)
