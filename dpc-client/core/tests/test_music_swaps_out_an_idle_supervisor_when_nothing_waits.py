"""ADR-052, Confirmation test 35 (slice S2; Mike #70, SPEC decision 32 amended): "if the queue is
empty and the server just idles, unload it and take the card".

A music lease (`waits_for_engines=True`, section 3.2), first in both lines, swaps an idle supervisor
out by the owner-start rule (`swap_out_for_a_lease`, section 2.2 item 12): the swap on, the holder
idle by rule 1, nothing earlier waiting in either line. The owner's live conversation is saved
before the stop, the swap note says the parked conversations are dropped, and the job is granted
the card. A busy holder is waited for and never marked; another waiter ahead means no swap; with
the swap off the job waits and nothing is stopped. A lease that does not wait for engines (Whisper's
item 1a is open) never swaps a supervisor out.

Real supervisors faked at their seams (`test_one_local_engine_holds_the_card.py`); fake lease clients.
"""

import asyncio
import logging

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseWaitedTooLong
from dpc_client_core.managers.llama_server_supervisor import card_held_for

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
    fast,
    holding,
    music,
    nothing_left,
    swap,
    until,
    waiting,
    whisper,
)
from tests.test_one_local_engine_holds_the_card import _strata, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick")


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.005)
    monkeypatch.setattr(base_mod, "_LAST_SWAP", None)


async def running_strata(tmp_path, parked=2):
    strata = _strata(tmp_path)
    await strata.ensure_running()
    events = []

    async def parked_count():
        events.append("parked read")
        return parked

    async def save(save_by, *args, **kwargs):
        events.append("live saved")
        return {"outcome": "saved", "bytes": 4096, "ms": 5, "reason": None}

    real_stop = strata._stop_child

    async def stop_child():
        events.append("stopped")
        await real_stop()

    strata._parked_conversations = parked_count
    strata._save_live_conversation = save
    strata._stop_child = stop_child
    return strata, events


class TestAnIdleServerIsSwappedOutForMusic:
    @pytest.mark.asyncio
    async def test_the_lease_marks_it_saves_the_live_conversation_first_and_takes_the_card(self, tmp_path, caplog):
        strata, events = await running_strata(tmp_path)
        m = music(waits_for_engines=True)

        with caplog.at_level(logging.INFO):
            await asyncio.wait_for(m.lease.acquire("music job J", wait_s=None), BOUND)

        assert events == ["parked read", "live saved", "stopped"]
        assert strata._proc is None and holding() == ["music:yue2"]
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("swapped ")]
        assert len(lines) == 1 and lines[0].startswith("swapped 'flash' out for 'music:yue2'")
        assert lines[0].endswith("; its parked conversations are dropped")
        last = base_mod.last_swap()
        assert (last["from"], last["to"], last["to_engine"]) == ("flash", "music:yue2", "yue2.cpp")
        assert last["parked_dropped"] is True and last["live_saved"]["outcome"] == "saved"
        assert strata._draining is False and strata._swap_for is None, "reopened: its next call starts it"
        m.lease.release("job done")
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_busy_server_is_waited_for_and_never_marked(self, tmp_path):
        strata, events = await running_strata(tmp_path)
        m = music(waits_for_engines=True)
        turn_over = asyncio.Event()

        async def turn():
            async with card_held_for("flash", "accepted_request", agent_id="agent", conversation_id="chat"):
                await turn_over.wait()

        on_strata = asyncio.create_task(turn())
        await until(lambda: base_mod._CARD_HOLDS.get("flash"))
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=None))
        await until(lambda: waiting() == ["music:yue2"])
        await asyncio.sleep(0.05)
        assert events == [] and strata._draining is False and holding() == ["flash"]
        row = base_mod.card_queue()[-1]
        assert row["wait"]["where"] == "busy_holder" and row["wait"]["wait_s"] is None

        turn_over.set()
        await asyncio.wait_for(asyncio.gather(on_strata, job), BOUND)
        assert events == ["parked read", "live saved", "stopped"] and holding() == ["music:yue2"]
        m.lease.release("job done")
        await nothing_left()

    @pytest.mark.asyncio
    async def test_another_waiter_ahead_means_no_swap(self, tmp_path):
        strata, events = await running_strata(tmp_path)
        w, m = whisper(), music(waits_for_engines=True)
        dictation = asyncio.create_task(w.lease.acquire("dictation", wait_s=None))
        await until(lambda: waiting() == ["whisper:turbo"])
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=None))
        await until(lambda: waiting() == ["whisper:turbo", "music:yue2"])
        await asyncio.sleep(0.05)

        assert events == [] and holding() == ["flash"], "neither the first waiter (no swap) nor music behind it"
        for task in (dictation, job):
            task.cancel()
        await asyncio.gather(dictation, job, return_exceptions=True)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_with_the_swap_off_the_job_waits_and_nothing_is_stopped(self, tmp_path):
        swap(False)
        strata, events = await running_strata(tmp_path)
        m = music(waits_for_engines=True)
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=None))
        await until(lambda: waiting() == ["music:yue2"])
        await asyncio.sleep(0.05)
        assert events == [] and holding() == ["flash"] and strata._draining is False

        await strata.stop()  # a person's Stop X
        await asyncio.wait_for(job, BOUND)
        assert holding() == ["music:yue2"] and base_mod.last_swap() is None
        m.lease.release("job done")
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_lease_that_does_not_wait_for_engines_never_swaps_one_out(self, tmp_path):
        strata, events = await running_strata(tmp_path)
        w = whisper()
        with pytest.raises(LeaseWaitedTooLong) as refused:
            await asyncio.wait_for(w.lease.acquire("dictation", wait_s=0.1), BOUND)
        assert "'flash'" in str(refused.value)
        assert events == [] and holding() == ["flash"]
        await nothing_left()
