"""The card lease's queue is visible (Mike, 2026-11-25): `card_lease.queue_snapshot()`.

One plain, JSON-safe dict of who holds the card (client, kind, purpose, agent and chat where known,
since), who waits in order (client, purpose, since, the lease's own wait estimate or "unknown", what
it waits for), and the last `RECENT_MAX` grants, releases and leavings. It follows a lease through
grant, wait, yield and cancel, and reads a supervised engine's holding and a card-queue place beside
the lease line, in the one order of the card's clock. Lease clients from `_card_lease_helpers`; the
supervisors through their real `ensure_running`, faked at their seams as in
`test_one_local_engine_holds_the_card.py`.
"""

import asyncio
import json
import os
import time

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseWaitedTooLong, queue_snapshot
from dpc_client_core.managers.llama_server_supervisor import card_held_for

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
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
from tests.test_one_local_engine_holds_the_card import _llama, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast")


def _waiters():
    return queue_snapshot()["waiters"]


class TestAFreeCard:
    def test_says_free_and_is_plain_json(self):
        snap = queue_snapshot()

        assert snap["schema"] == "card_lease/1"
        assert snap["free"] is True and snap["holder"] is None and snap["holders"] == []
        assert snap["waiters"] == [] and snap["recent"] == [] and snap["closed"] is None
        assert snap["backend_pid"] == os.getpid()
        assert abs(snap["updated_at"] - time.time()) < 5
        assert snap["updated_at_iso"].endswith("Z")
        assert json.loads(json.dumps(snap)) == snap, "plain data: nothing but JSON types"

    def test_a_backend_stopping_is_said(self):
        card_lease._CLOSED = "the backend is stopping"
        assert queue_snapshot()["closed"] == "the backend is stopping"


class TestTheHolderTheLineAndTheRecord:
    @pytest.mark.asyncio
    async def test_a_use_is_the_holders_purpose_and_the_chat_it_runs_for(self):
        w = whisper()
        release = asyncio.Event()

        async def dictate():
            async with card_held_for(None, "accepted_request", agent_id="agent_ark", conversation_id="group-a"):
                await hold_open(w.lease, "dictation", release)

        before = time.time()
        task = asyncio.create_task(dictate())
        await until(lambda: holding() == ["whisper:turbo"])
        snap = queue_snapshot()

        holder = snap["holder"]
        assert snap["holders"] == [holder] and snap["free"] is False
        assert (holder["client"], holder["display"], holder["kind"], holder["engine"]) == (
            "whisper:turbo", "Whisper (turbo)", "transcription", "whisper")
        assert (holder["purpose"], holder["uses"], holder["state"], holder["busy"]) == (
            "dictation", ["dictation"], "running", True)
        assert (holder["agent_id"], holder["conversation_id"]) == ("agent_ark", "group-a")
        assert before - 1 <= holder["since"] <= time.time() + 1
        assert [(r["event"], r["client"], r["purpose"]) for r in snap["recent"]] == [
            ("granted", "whisper:turbo", "dictation")]

        release.set()
        await asyncio.wait_for(task, BOUND)

    @pytest.mark.asyncio
    async def test_a_warm_grant_with_no_use_names_what_it_was_asked_for_and_is_idle(self):
        w = whisper()
        await asyncio.wait_for(w.lease.acquire("preload", wait_s=BOUND), BOUND)

        holder = queue_snapshot()["holder"]

        assert (holder["purpose"], holder["uses"], holder["busy"]) == ("preload", [], False)

    @pytest.mark.asyncio
    async def test_waiters_stand_in_order_with_the_leases_own_estimate_or_unknown(self):
        w, m, e = whisper(), music(), embedder()
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])
        first = asyncio.create_task(hold_open(m.lease, "music job J", release, wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])
        second = asyncio.create_task(e.lease.acquire("embed", wait_s=None))
        await until(lambda: waiting() == ["music:yue2", "embedder:BAAI/bge-m3"])

        rows = _waiters()
        assert [(r["position"], r["client"], r["purpose"]) for r in rows] == [
            (1, "music:yue2", "music job J"), (2, "embedder:BAAI/bge-m3", "embed")]
        assert (rows[0]["wait_known"], rows[0]["wait_s"], rows[0]["where"]) == (False, None, "busy_holder"), \
            "a holder that cannot say when it ends is unknown, never 0"
        assert rows[0]["waits_for"] == ["Whisper (turbo) (dictation)"]
        assert rows[0]["until_cancelled"] is False and rows[0]["give_up_at"] > time.time()
        assert rows[1]["until_cancelled"] is True and rows[1]["give_up_at"] is None
        assert "yue2.cpp (music job J)" in rows[1]["waits_for"], "the waiter ahead is named too"
        assert all(isinstance(r["since"], float) for r in rows)

        w.eta = 30
        assert (_waiters()[0]["wait_known"], _waiters()[0]["wait_s"]) == (True, 30)
        assert _waiters()[1]["wait_known"] is False, "one part unknown leaves the whole unknown"

        release.set()
        await asyncio.wait_for(holder, BOUND)
        await asyncio.wait_for(first, BOUND)
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_yield_and_a_grant_are_recorded_in_order_with_what_they_cost(self):
        w, m = whisper(), music()
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])
        contender = asyncio.create_task(hold_open(m.lease, "music job J", release, wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])
        await asyncio.sleep(0.08)  # long enough to count as a wait (the record names waits of 50 ms or more)

        release.set()
        await asyncio.wait_for(holder, BOUND)
        await asyncio.wait_for(contender, BOUND)

        snap = queue_snapshot()
        events = [(r["event"], r["client"]) for r in snap["recent"]]
        assert events[:3] == [("granted", "whisper:turbo"), ("released", "whisper:turbo"),
                              ("granted", "music:yue2")]
        released = snap["recent"][1]
        assert released["why"].startswith("yield for music:yue2") and released["held_s"] >= 0
        assert snap["recent"][2]["purpose"] == "music job J" and snap["recent"][2]["waited_s"] > 0
        assert [h["client"] for h in snap["holders"]] == ["music:yue2"]
        assert snap["waiters"] == []
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_cancelled_waiter_leaves_the_line_and_the_record_says_why(self):
        w, m = whisper(), music()
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])

        contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)

        snap = queue_snapshot()
        assert snap["waiters"] == []
        left = snap["recent"][-1]
        assert (left["event"], left["client"], left["purpose"], left["why"]) == (
            "left_the_line", "music:yue2", "music job J", "cancelled")
        assert left["waited_s"] >= 0
        release.set()
        await asyncio.wait_for(holder, BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_waiter_that_ran_out_of_time_is_recorded_with_the_refusal(self):
        w, m = whisper(), music()
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])

        with pytest.raises(LeaseWaitedTooLong):
            await asyncio.wait_for(m.lease.acquire("music job J", wait_s=0.05), BOUND)

        left = queue_snapshot()["recent"][-1]
        assert left["event"] == "left_the_line" and left["why"].startswith("LeaseWaitedTooLong:")
        assert queue_snapshot()["waiters"] == []
        release.set()
        await asyncio.wait_for(holder, BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_the_record_keeps_the_last_ten_and_drops_the_oldest(self):
        w = whisper()
        for i in range(12):
            await asyncio.wait_for(w.lease.acquire(f"call {i}", wait_s=BOUND), BOUND)
            w.lease.release("test")

        recent = queue_snapshot()["recent"]

        assert len(recent) == card_lease.RECENT_MAX == 10
        assert [r["event"] for r in recent] == ["granted", "released"] * 5
        assert recent[0]["purpose"] == "call 7" and recent[-1]["purpose"] == "call 11"


class TestSupervisorsAndPlacesBesideTheLeaseLine:
    @pytest.mark.asyncio
    async def test_a_supervised_engine_on_the_card_is_a_holder_and_a_lease_waits_behind_it(self, card):  # noqa: F811
        llama = _llama()
        await asyncio.wait_for(llama.ensure_running(), BOUND)
        m = music()
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])

        snap = queue_snapshot()

        holder = snap["holder"]
        assert (holder["client"], holder["kind"], holder["engine"], holder["state"]) == (
            "qwen27", "inference", llama.ENGINE, "running")
        assert (holder["busy"], holder["purpose"], holder["since"]) == (False, "idle", None)
        assert snap["waiters"][0]["client"] == "music:yue2"
        assert snap["waiters"][0]["waits_for"] == [f"{llama.ENGINE} 'qwen27'"]
        contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_request_for_a_supervised_engine_is_a_place_in_the_one_order(self, card):  # noqa: F811
        llama = _llama()
        await asyncio.wait_for(llama.ensure_running(), BOUND)
        m = music()
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])
        async with card_held_for("other", "accepted_request", agent_id="agent_x", conversation_id="group-b"):
            place = base_mod._CardPlace("other")
            place.requests.append(base_mod._QueuedRequest(place))
            base_mod._CARD_QUEUE.append(place)

        rows = queue_snapshot()["waiters"]

        assert [(r["position"], r["client"], r["kind"]) for r in rows] == [
            (1, "music:yue2", "music"), (2, "other", "inference")], "the place came after the lease waiter"
        assert rows[1]["purpose"] == "agent agent_x in chat group-b"
        assert (rows[1]["agent_id"], rows[1]["conversation_id"]) == ("agent_x", "group-b")
        assert (rows[1]["wait_known"], rows[1]["wait_s"], rows[1]["where"]) == (False, None, "card_queue")
        assert rows[1]["waits_for"] == [f"{llama.ENGINE} 'qwen27'"]
        assert rows[1]["give_up_at"] > time.time() and rows[1]["until_cancelled"] is False
        contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_an_earlier_place_stands_before_a_later_lease_waiter(self, card):  # noqa: F811
        llama = _llama()
        await asyncio.wait_for(llama.ensure_running(), BOUND)
        place = base_mod._CardPlace("other")
        place.requests.append(base_mod._QueuedRequest(place))
        base_mod._CARD_QUEUE.append(place)
        m = music()
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])

        rows = queue_snapshot()["waiters"]

        assert [(r["position"], r["client"]) for r in rows] == [(1, "other"), (2, "music:yue2")]
        assert rows[0]["purpose"] == "a model call", "a request with no hold names no one"
        contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)
        await nothing_left()
