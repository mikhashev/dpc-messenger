"""Follow-up 11C (Zcode, follow-up 10 review, P2, pre-existing): a start cancelled inside `_spawn` used to
leave the engine in `starting` with no child (`except OSError` did not catch a cancellation). It now ends
`stopped`, with the log closed and the card let go, and the next start works."""

import asyncio

import pytest

from tests._acestep_helpers import free_env, install_fake_engine, make_engine
from tests._card_lease_helpers import BOUND


@pytest.fixture
def engine(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    return make_engine(home)


@pytest.mark.asyncio
async def test_a_cancel_inside_the_spawn_does_not_leave_the_engine_starting(engine, monkeypatch):
    real_spawn = engine._spawn
    inside = asyncio.Event()

    async def spawn(cmd):
        inside.set()
        await asyncio.Event().wait()   # the spawn never returns: only a cancel ends it

    monkeypatch.setattr(engine, "_spawn", spawn)
    start = asyncio.create_task(engine.start())
    await asyncio.wait_for(inside.wait(), BOUND)
    assert engine.state == "starting" and engine.card_lease.granted and not engine.alive()

    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(start, BOUND)
    assert engine.state == "stopped", "not `starting` for a child that never came"
    assert "cancelled" in engine.details()["reason"]
    assert not engine.alive() and not engine.card_lease.granted
    assert engine._log_fh is None, "the log opened for the start is closed"

    # Nothing is stuck: the same engine starts and stops afterwards.
    monkeypatch.setattr(engine, "_spawn", real_spawn)
    await asyncio.wait_for(engine.start(), 30)
    assert engine.state == "running"
    await engine.stop("ui_stop")
    assert engine.state == "stopped" and not engine.card_lease.granted
