"""ADR-052, Confirmation test 16 (slice S2; Ark #44): with the swap off, a start blocked only by card
leases takes a place in the card queue (section 2.2 item 3), so `_CARD_QUEUE` is no longer empty
with the swap off. A background call (the card queue's rule 10) then meets the place and is skipped
with the queue's reason, in a configuration where it never was before; with no place waiting it
passes as today. With nothing in the way, or an engine in the way, the swap-off path is today's: no
place, and the claim refuses a supervisor holder.
"""

import asyncio

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import (
    BackgroundCallSkipped,
    CardHeldError,
    a_background_call,
    card_queue,
)

from tests._card_lease_helpers import BOUND, fast, hold_open, holding, nothing_left, swap, until, whisper  # noqa: F401
from tests.test_one_local_engine_holds_the_card import _llama, _strata, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick")


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.005)
    monkeypatch.setattr(base_mod, "_LEASE_ASK_AGAIN_S", 0.02)


def queued_starts():
    return [row["alias"] for row in card_queue() if "until_cancelled" not in row]


class TestALeaseOnlyPlaceWithTheSwapOff:
    @pytest.mark.asyncio
    async def test_a_start_takes_a_place_and_a_background_call_is_skipped_with_the_queue_reason(self):
        swap(False, 30)
        w = whisper()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)

        start = asyncio.create_task(_llama("bonsai", gguf_path="D:/models/bonsai.gguf").ensure_running())
        await until(lambda: queued_starts() == ["bonsai"])
        assert base_mod._CARD_QUEUE[0].requests[0].for_leases is True

        reflection = _llama("qwen27")
        with a_background_call("skill_reflection"):
            with pytest.raises(BackgroundCallSkipped) as skipped:
                await asyncio.wait_for(reflection.ensure_running(), BOUND)
        assert "a request is waiting in the queue for the card for 'bonsai'" in str(skipped.value)
        assert base_mod.last_background_skip()["skill_reflection"]["reason"] == (
            "a request is waiting in the queue for the card for 'bonsai'")
        assert reflection._proc is None and queued_starts() == ["bonsai"], "a background call takes no place"

        release.set()
        await asyncio.wait_for(asyncio.gather(dictation, start), BOUND)
        assert holding() == ["bonsai"] and queued_starts() == []
        await nothing_left()

    @pytest.mark.asyncio
    async def test_with_no_place_waiting_a_background_call_on_a_running_child_passes_as_today(self):
        swap(False, 30)
        llama = _llama()
        await llama.ensure_running()
        whisper()  # registered, not granted: nothing in the way
        with a_background_call("skill_reflection"):
            props = await asyncio.wait_for(llama.ensure_running(), BOUND)
        assert props == {"total_slots": 1} and queued_starts() == []
        assert "skill_reflection" not in base_mod.last_background_skip()

    @pytest.mark.asyncio
    async def test_a_background_call_with_only_a_lease_on_the_card_is_skipped_and_queues_nothing(self):
        swap(False, 30)
        w = whisper()
        await w.lease.acquire("preload", wait_s=BOUND)
        with a_background_call("self_reflection_confirmation"):
            with pytest.raises(BackgroundCallSkipped, match="'qwen27' is not on the card"):
                await asyncio.wait_for(_llama().ensure_running(), BOUND)
        assert queued_starts() == [] and w.yields == [], "a background call neither queues nor yields"
        assert w.lease.granted
        w.lease.release("done")
        await nothing_left()


class TestTheSwapOffPathStaysTodaysWithoutALeaseInTheWay:
    @pytest.mark.asyncio
    async def test_nothing_in_the_way_takes_no_place(self):
        swap(False, 30)
        whisper()
        await asyncio.wait_for(_llama().ensure_running(), BOUND)
        assert queued_starts() == [] and holding() == ["qwen27"]

    @pytest.mark.asyncio
    async def test_an_engine_in_the_way_is_refused_at_once_as_today(self, tmp_path):
        swap(False, 30)
        whisper()
        strata = _strata(tmp_path)
        await strata.ensure_running()
        with pytest.raises(CardHeldError, match="strata alias 'flash'"):
            await asyncio.wait_for(_llama().ensure_running(), BOUND)
        assert queued_starts() == [] and holding() == ["flash"]
