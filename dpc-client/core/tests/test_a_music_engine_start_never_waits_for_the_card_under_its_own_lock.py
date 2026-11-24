"""ADR-052, Confirmation test 15 (Ark #44's falsifier; slice S4, section 3.2 "Lock order: the lease
first, then `_the_lock`").

A music engine's start never waits for the card, and never under `_the_lock`: a start that holds no
grant asks for one with `wait_s=0` before the lock, so with a supervisor on the card a start by hand is
refused at once naming it, and nothing is spawned. While a start holds `_the_lock` (the child boots)
the lease is `starting`, busy for the card: no contender marks it, a person's Stop answers that it is
busy, and the engine's own yield answers `refused` at once instead of sitting on the lock until a stop
bound. The yield of an idle engine completes and the contender is granted.

A real Strata supervisor faked at its seams (`test_one_local_engine_holds_the_card.py`); a real
acestep.cpp supervisor over `_fake_acestep_server.py`.
"""

import asyncio
import time

import pytest

from dpc_client_core.managers.card_lease import LeaseBusy
from dpc_client_core.music.engine_base import MusicEngineError

from tests._acestep_helpers import free_env, install_fake_engine, make_engine
from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, until, whisper  # noqa: F401
from tests.test_one_local_engine_holds_the_card import _strata, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "card")


@pytest.fixture
def home(tmp_path, monkeypatch):
    free_env(monkeypatch)
    h = tmp_path / "dpc"
    install_fake_engine(h)
    return h


@pytest.mark.asyncio
async def test_a_start_by_hand_with_an_engine_on_the_card_is_refused_at_once_naming_it(tmp_path, home):
    strata = _strata(tmp_path)
    await asyncio.wait_for(strata.ensure_running(), BOUND)
    engine = make_engine(home)
    began = time.monotonic()
    with pytest.raises(MusicEngineError) as err:
        await asyncio.wait_for(engine.start(), BOUND)
    assert time.monotonic() - began < 0.5
    assert err.value.code == "card_busy" and "flash" in err.value.message
    assert not engine.alive() and engine._proc is None, "nothing spawned"
    assert not engine._the_lock().locked() and not engine.card_lease.granted
    assert holding() == ["flash"]
    await nothing_left()


@pytest.mark.asyncio
async def test_a_booting_engine_is_never_marked_and_its_yield_never_waits_for_the_lock(home, monkeypatch):
    engine = make_engine(home)
    booting, go_on = asyncio.Event(), asyncio.Event()
    real_health = engine._health_ok

    async def health(port):
        booting.set()
        await go_on.wait()
        return await real_health(port)

    monkeypatch.setattr(engine, "_health_ok", health)
    start = asyncio.create_task(engine.start())
    try:
        await asyncio.wait_for(booting.wait(), 30)
        lease = engine.card_lease
        assert engine._the_lock().locked() and lease.state == "starting"

        began = time.monotonic()
        assert await asyncio.wait_for(engine._yield_card("yield for test"), BOUND) == "refused"
        assert time.monotonic() - began < 0.5, "answered at once, not after the start"
        with pytest.raises(LeaseBusy):
            await asyncio.wait_for(lease.let_go_by_hand("Stop by hand", 1.0), BOUND)

        w = whisper()
        waiter = asyncio.create_task(w.lease.acquire("dictation", wait_s=20))
        await asyncio.sleep(0.1)
        assert not lease._draining and lease._yield_task is None, "a booting engine is busy: never marked"
        assert not waiter.done()

        go_on.set()
        await asyncio.wait_for(start, 30)
        # Running and idle now: the waiter asks it to let go, the stop completes, the card is handed over.
        await asyncio.wait_for(waiter, 25)
        assert holding() == ["whisper:turbo"] and not engine.alive() and engine._last_exit["why"] == "swap"
        w.lease.release("test")
    finally:
        go_on.set()
        await engine.shutdown()
    await nothing_left()
