"""ADR-052, Confirmation test 29 (Ark #60 §4; slice S4, section 3.2, the runner's second gate).

A job is `running` and its engine's child is gone (it exited on its own under the job's open use, so
the card was let go, L8), and another client took the card and is busy with it. The job's engine start
asks for the card again and waits: the engine does not start, the job shows its `card_wait` naming the
holder, and it is neither failed nor dropped as cancelled (the old gate, `state != queued`, would have
read a running job as cancelled). When the holder lets go the job is granted, starts its engine and
finishes; its wait is cleared. A real cancel during that wait ends it, the engine never started.

A real acestep.cpp supervisor over `_fake_acestep_server.py`; the other client is a fake lease.
"""

import asyncio

import pytest

from dpc_client_core.music import job_schemas as js

from tests._acestep_helpers import Rig, free_env, from_text, install_fake_engine, wait_until
from tests._card_lease_helpers import BOUND, hold_open, holding, whisper

DONE = (js.REVIEW, js.FAILED, js.CANCELLED)


@pytest.fixture
def home(tmp_path, monkeypatch):
    free_env(monkeypatch)
    h = tmp_path / "dpc"
    install_fake_engine(h)
    return h


async def _child_gone_and_card_taken(rig, w, over):
    """At the job's first progress step (running, before its engine start): the warm child exits on
    its own, and the other client takes the card it let go and stays busy."""
    real_progress = rig.runner._progress
    taken = []

    async def progress(job_id, value):
        await real_progress(job_id, value)
        if not taken:
            taken.append(asyncio.ensure_future(hold_open(w.lease, "dictation", over)))
            rig.engine._proc.kill()
            await wait_until(lambda: w.lease.granted, timeout=10)

    rig.runner._progress = progress
    return taken


@pytest.mark.asyncio
async def test_the_job_waits_running_names_the_holder_and_starts_when_it_lets_go(home):
    w, over = whisper(), asyncio.Event()
    async with Rig(home, start_runner=False) as rig:
        await asyncio.wait_for(rig.engine.start(), 30)
        taken = await _child_gone_and_card_taken(rig, w, over)
        rig.runner.start()
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()

        await wait_until(lambda: rig.store.get_job(job["id"]).get("card_wait") is not None)
        waiting = rig.store.get_job(job["id"])
        assert waiting["state"] == js.RUNNING
        assert waiting["card_wait"]["holders"] == [{"alias": "whisper:turbo", "engine": "whisper"}]
        assert waiting["state_reason"] == "waiting for the card: whisper:turbo holds it"
        await asyncio.sleep(0.3)
        assert rig.store.get_job(job["id"])["state"] == js.RUNNING, "neither failed nor dropped"
        assert not rig.engine.alive(), "the engine does not start while the card is held"
        assert rig.runner.current_job_id == job["id"]

        over.set()  # the dictation ends; the idle holder is asked to let go
        await asyncio.wait_for(taken[0], BOUND)
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done["state_reason"]
        assert done["card_wait"] is None
        assert w.yields and not w.lease.granted and rig.engine.alive()
        assert holding() == ["music:acestep"]


@pytest.mark.asyncio
async def test_a_real_cancel_during_that_wait_ends_the_job(home):
    w, over = whisper(), asyncio.Event()
    async with Rig(home, start_runner=False) as rig:
        await asyncio.wait_for(rig.engine.start(), 30)
        taken = await _child_gone_and_card_taken(rig, w, over)
        rig.runner.start()
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        await wait_until(lambda: rig.store.get_job(job["id"]).get("card_wait") is not None)

        rig.store.cancel_job(job["id"])
        rig.runner.wake()
        await wait_until(lambda: rig.runner.current_job_id is None)
        assert rig.store.get_job(job["id"])["state"] == js.CANCELLED
        assert not rig.engine.alive() and not rig.engine.card_lease.granted
        assert rig.engine.card_lease._waiters() == [], "the cancelled wait left the line"
        over.set()
        await asyncio.wait_for(taken[0], BOUND)
