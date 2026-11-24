"""ADR-052, Confirmation test 11 (slice S4, section 3.2, rule L8): a music engine holds the card lease
`music:<key>` while its child is on the card, and lets go however the child ends.

A start asks for the card before `_the_lock` and never waits for it (a start by hand on a free card
is granted at once); `starting` while the child boots (busy for the card: never asked to let go),
`running` once it answers. The grant is let go on the idle stop, on a person's Stop (`ui_stop`), on a
yield to another client (`swap`), when the child exits on its own, after a failed start, and at the
backend's stop. A child that exits under a job's open use lets go too, because its memory is gone:
the use stays open, the engine stays busy, and the job's next start asks for the card again with a
fresh arrival. A yue2.cpp restart for another backbone is one start: the card stays with the engine
across it. Both servers are real child processes (`_fake_acestep_server.py`, `_fake_yue2_server.py`).
"""

import asyncio

import pytest

from dpc_client_core.managers import card_lease

from dpc_client_core.music.acestep import AceStepServer
from dpc_client_core.music.yue2 import Yue2Server

from tests._acestep_helpers import free_env, install_fake_engine, make_engine, wait_until
from tests._card_lease_helpers import BOUND, holding, nothing_left, whisper
from tests._yue2_helpers import install_fake_yue2, make_yue2, yue2_env


@pytest.fixture
def ace(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    return home


def lease_of(engine):
    return engine.card_lease


def test_each_engine_has_one_music_lease_with_its_declared_peak(tmp_path):
    home = tmp_path / "dpc"
    yue2, ace = make_yue2(home), make_engine(home)
    assert card_lease.lease_named("music:yue2") is yue2.card_lease
    assert card_lease.lease_named("music:acestep") is ace.card_lease
    for engine, peak, display in ((yue2, Yue2Server.VRAM_ESTIMATE_MIB, "yue2.cpp"),
                                  (ace, AceStepServer.VRAM_ESTIMATE_MIB, "acestep.cpp")):
        lease = engine.card_lease
        assert (lease.kind, lease.ENGINE, lease.display, lease.vram_mib) == ("music", engine.ENGINE, display, peak)
        assert lease.yield_cost == "engine" and lease.waits_for_engines is True and lease.shares_with is None
        assert lease.state == "free" and not lease.granted


@pytest.mark.asyncio
async def test_a_start_takes_the_card_and_a_persons_stop_lets_it_go(ace):
    engine = make_engine(ace)
    try:
        await asyncio.wait_for(engine.start(), 30)
        assert holding() == ["music:acestep"] and lease_of(engine).state == "running"
        assert not lease_of(engine)._busy_for_the_card(lease_of(engine).state), "idle: no job"
        assert await engine.stop("ui_stop") is True
        assert holding() == [] and not lease_of(engine).granted
    finally:
        await engine.shutdown()
    await nothing_left()


@pytest.mark.asyncio
async def test_a_booting_engine_is_busy_for_the_card(ace, monkeypatch):
    engine = make_engine(ace)
    states = []
    real_health = engine._health_ok

    async def health(port):
        states.append((lease_of(engine).state, lease_of(engine)._busy_for_the_card(lease_of(engine).state)))
        return await real_health(port)

    monkeypatch.setattr(engine, "_health_ok", health)
    try:
        await asyncio.wait_for(engine.start(), 30)
        assert states and set(states) == {("starting", True)}
        assert lease_of(engine).state == "running"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_the_idle_stop_lets_go(ace):
    engine = make_engine(ace, settings={"acestep_idle_stop_s": "0.2"})
    try:
        await asyncio.wait_for(engine.start(), 30)
        assert lease_of(engine).granted
        await wait_until(lambda: not engine.alive(), timeout=10)
        await wait_until(lambda: not lease_of(engine).granted, timeout=5)
        assert engine._last_exit["why"] == "idle_unload" and holding() == []
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_an_idle_engine_yields_to_another_client_by_stopping(ace):
    engine = make_engine(ace)
    try:
        await asyncio.wait_for(engine.start(), 30)
        w = whisper()
        await asyncio.wait_for(w.lease.acquire("dictation", wait_s=20), 25)
        assert holding() == ["whisper:turbo"]
        assert not engine.alive() and engine._last_exit["why"] == "swap" and not lease_of(engine).granted
        w.lease.release("test")
    finally:
        await engine.shutdown()
    await nothing_left()


@pytest.mark.asyncio
async def test_a_child_that_exits_on_its_own_lets_go(ace):
    engine = make_engine(ace)
    try:
        await asyncio.wait_for(engine.start(), 30)
        engine._proc.kill()
        await wait_until(lambda: engine.state == "error", timeout=10)
        assert not lease_of(engine).granted and holding() == []
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_a_failed_start_lets_go(ace, monkeypatch):
    monkeypatch.setenv("FAKE_ACESTEP_MODE", "exit_at_start")
    engine = make_engine(ace)
    with pytest.raises(Exception) as err:
        await asyncio.wait_for(engine.start(), 30)
    assert getattr(err.value, "code", None) == "start_failed"
    assert not lease_of(engine).granted and holding() == []
    await nothing_left()


@pytest.mark.asyncio
async def test_the_backends_stop_lets_go(ace):
    engine = make_engine(ace)
    await asyncio.wait_for(engine.start(), 30)
    await engine.shutdown()
    assert not lease_of(engine).granted and holding() == []


@pytest.mark.asyncio
async def test_a_child_that_exits_under_a_jobs_use_lets_go_and_the_next_start_asks_again(ace):
    engine = make_engine(ace)
    try:
        async with engine.in_use("music job J", wait_s=BOUND):
            await asyncio.wait_for(engine.start(), 30)
            first = lease_of(engine)._grant_no
            assert engine.busy and lease_of(engine).who_uses() == "music job J"
            engine._proc.kill()
            await wait_until(lambda: engine.state == "error", timeout=10)
            assert not lease_of(engine).granted, "its memory is gone: the grant is let go"
            assert engine.busy and [u.what for u in lease_of(engine)._uses] == ["music job J"], "the use stays"
            await asyncio.wait_for(engine.start(), 30)
            assert lease_of(engine).granted and lease_of(engine)._grant_no == first + 1, "asked again"
            assert lease_of(engine)._busy_for_the_card(lease_of(engine).state)
        assert engine.alive() and lease_of(engine).granted and not engine.busy, "warm: granted, no use"
    finally:
        await engine.shutdown()
    await nothing_left()


@pytest.mark.asyncio
async def test_a_yue2_restart_for_another_backbone_keeps_the_card(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_yue2(home, backbones=("stock", "rs-tw"))
    engine = make_yue2(home)
    w = whisper()
    seen, waiter = [], []
    try:
        await asyncio.wait_for(engine.start(), 30)
        first = lease_of(engine)._grant_no
        real_health = engine._health_ok

        async def health(port):
            # Inside the restart: the old child is gone, the new one boots.
            seen.append((lease_of(engine).granted, lease_of(engine).state))
            if not waiter:
                waiter.append(asyncio.create_task(w.lease.acquire("dictation", wait_s=20)))
                await asyncio.sleep(0.3)  # the contender's rounds see a booting engine: never asked
                assert not lease_of(engine)._draining and lease_of(engine)._yield_task is None
                assert engine._last_exit["why"] == "swap"
            return await real_health(port)

        monkeypatch.setattr(engine, "_health_ok", health)
        await asyncio.wait_for(engine.start(backbone="rs-tw"), 30)
        assert engine.running_backbone == "rs-tw"
        assert seen and set(seen) == {(True, "starting")}, "nobody took the card between the two children"
        assert lease_of(engine)._grant_no == first
        # Once it answers again it is idle, and the waiter asks it to let go.
        await asyncio.wait_for(waiter[0], 25)
        assert holding() == ["whisper:turbo"] and not engine.alive()
        w.lease.release("test")
    finally:
        await engine.shutdown()
    await nothing_left()
