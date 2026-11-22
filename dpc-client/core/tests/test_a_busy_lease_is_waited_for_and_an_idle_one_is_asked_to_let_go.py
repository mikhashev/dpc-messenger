"""ADR-052, Confirmation test 2 (slice S1): a busy lease is waited for, an idle one is asked to let go.

A busy holder is never marked and never asked; it is waited for and named (L6). An idle one is
marked `draining` before its `on_yield` runs, and admits no new use while it lets go (L5). The four
outcomes: `let_go` and `nothing_to_give` release (a granted lease with nothing loaded never keeps a
contender waiting), `refused` and `could_not_let_go` go back to `running` and the contender keeps
waiting to its deadline, asking again at most `_MAX_SWAP_ROUNDS` times. A deadline refusal names the
holder, what it is busy with and the bound. The estimate's `wait_s` is None when the holder's ETA is
unknown, never 0 (L10). Also the lease's own refusals (L3): waiting on itself, a person's Stop on a
busy lease, the backend stopping. Lease contenders only: supervisors honour leases from S2 on.
"""

import asyncio
import time

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import (
    LeaseBusy,
    LeaseClosed,
    LeaseWaitedTooLong,
    LeaseWouldWaitOnItself,
)
from dpc_client_core.managers.llama_server_supervisor import card_held_for, card_queue_refusal_in

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
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


def _row(alias):
    return next(row for row in base_mod.card_queue() if row["alias"] == alias)


def _holder(alias):
    return next(row for row in base_mod.card_holders() if row["alias"] == alias)


class TestABusyHolderIsWaitedFor:
    @pytest.mark.asyncio
    async def test_a_busy_holder_is_never_asked_and_its_wait_is_unknown_until_it_says(self):
        w, m = whisper(), music()
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])
        await asyncio.sleep(0.05)  # ten rounds of the line

        assert w.yields == [] and w.lease.state == "running"
        wait = _row("music:yue2")["wait"]
        assert wait == {"wait_s": None, "known": False, "behind": ["Whisper (turbo) (dictation)"],
                        "where": "busy_holder"}
        w.eta = 0  # a client that says 0 has said nothing
        assert _row("music:yue2")["wait"]["wait_s"] is None
        w.eta = 30
        assert _row("music:yue2")["wait"]["wait_s"] == 30 and _row("music:yue2")["wait"]["known"] is True

        release.set()
        await asyncio.wait_for(contender, BOUND)
        await asyncio.wait_for(holder, BOUND)
        assert holding() == ["music:yue2"]
        assert [state for _why, state in w.yields] == ["draining"], "asked once it was idle"
        await nothing_left()

    @pytest.mark.asyncio
    async def test_the_deadline_refusal_names_the_holder_what_it_is_busy_with_and_the_bound(self):
        w, m = whisper(), music()
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])
        started = time.monotonic()

        with pytest.raises(LeaseWaitedTooLong) as refused:
            await asyncio.wait_for(m.lease.acquire("music job J", wait_s=0.2), BOUND)

        assert 0.15 < time.monotonic() - started < 2
        message = str(refused.value)
        assert "yue2.cpp waited for 0.2 s" in message
        assert "Whisper (turbo) (busy: dictation)" in message
        assert refused.value.holder_alias == "whisper:turbo" and refused.value.holder_engine == "whisper"
        assert refused.value.wait["wait_s"] is None and refused.value.wait["known"] is False
        assert card_queue_refusal_in(refused.value) is refused.value, "never retried at the doors"
        assert w.yields == [] and waiting() == []
        release.set()
        await asyncio.wait_for(holder, BOUND)
        await nothing_left()


class TestAnIdleHolderIsAskedToLetGo:
    @pytest.mark.asyncio
    async def test_it_is_marked_draining_before_on_yield_and_admits_no_new_use_meanwhile(self):
        gate = asyncio.Event()
        # The music job keeps the card once it has it: the uses queued behind it ask in vain.
        w, m = whisper(gate=gate), music(outcome="refused")
        await w.lease.acquire("preload", wait_s=BOUND)
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: w.yields)

        assert w.yields == [("yield for music:yue2", "draining")]
        row = _holder("whisper:turbo")
        assert row["state"] == "draining" and row["busy"] is True
        assert w.lease._swap_for == "music:yue2" and w.lease._draining is True
        dictation = asyncio.create_task(w.lease.acquire("dictation", wait_s=BOUND))
        late_use = asyncio.create_task(hold_open(w.lease, "dictation", asyncio.Event()))
        await until(lambda: waiting() == ["music:yue2", "whisper:turbo", "whisper:turbo"])
        assert w.lease._uses == [], "no new use is admitted while it lets go"
        assert _row("music:yue2")["wait"]["where"] == "yielding"

        gate.set()
        await asyncio.wait_for(contender, BOUND)
        assert holding() == ["music:yue2"] and not w.lease.granted and w.loaded is False
        for task in (dictation, late_use):
            task.cancel()
        await asyncio.gather(dictation, late_use, return_exceptions=True)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_nothing_to_give_releases_at_once(self):
        w, m = whisper(outcome="nothing_to_give"), music()
        await w.lease.acquire("preload", wait_s=BOUND)
        await asyncio.wait_for(m.lease.acquire("music job J", wait_s=BOUND), BOUND)
        assert holding() == ["music:yue2"] and w.lease.state == "free"
        assert len(w.yields) == 1
        await nothing_left()

    @pytest.mark.asyncio
    async def test_refused_goes_back_to_running_and_the_contender_waits_to_its_deadline(self):
        w, m = whisper(outcome="refused"), music()
        await w.lease.acquire("preload", wait_s=BOUND)

        with pytest.raises(LeaseWaitedTooLong) as refused:
            await asyncio.wait_for(m.lease.acquire("music job J", wait_s=0.4), BOUND)

        assert w.lease.granted and w.lease.state == "running" and not w.lease._draining
        assert 1 <= len(w.yields) <= base_mod._MAX_SWAP_ROUNDS
        assert refused.value.holder_alias == "whisper:turbo"
        assert holding() == ["whisper:turbo"] and waiting() == []
        await nothing_left()

    @pytest.mark.asyncio
    async def test_could_not_let_go_keeps_the_grant_and_a_later_ask_may_succeed(self):
        answers = iter(["could_not_let_go", "let_go"])
        w, m = whisper(outcome=lambda: next(answers)), music()
        await w.lease.acquire("preload", wait_s=BOUND)
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: len(w.yields) == 1 and not w.lease._draining)
        assert w.lease.granted and w.lease.state == "running", "the memory is still held: no release"

        await asyncio.wait_for(contender, BOUND)
        assert len(w.yields) == 2 and holding() == ["music:yue2"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_yield_that_raises_is_could_not_let_go(self):
        async def broken():
            raise RuntimeError("the unload's worker was cancelled before its thread started")

        w, m = whisper(), music()
        w.lease.rebind(lambda why: broken())
        await w.lease.acquire("preload", wait_s=BOUND)
        with pytest.raises(LeaseWaitedTooLong):
            await asyncio.wait_for(m.lease.acquire("music job J", wait_s=0.2), BOUND)
        assert w.lease.granted and w.lease.state == "running"
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_contender_that_gives_up_never_cuts_a_yield_in_half(self):
        gate = asyncio.Event()
        w, m = whisper(gate=gate), music()
        await w.lease.acquire("preload", wait_s=BOUND)
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))
        await until(lambda: w.yields)
        contender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await contender
        assert w.lease.state == "draining" and card_lease._YIELDS

        gate.set()
        await nothing_left()
        assert not w.lease.granted and w.loaded is False and holding() == []


class TestTheRefusalsOfTheLease:
    @pytest.mark.asyncio
    async def test_a_request_that_stands_on_the_holder_through_a_use_is_refused_at_once(self):
        w, m = whisper(), music()
        async with card_held_for(None, "accepted_request", agent_id="agent", conversation_id="chat"):
            async with w.lease.use("agent 'agent' tool transcribe_audio_file", wait_s=BOUND):
                started = time.monotonic()
                with pytest.raises(LeaseWouldWaitOnItself) as refused:
                    await asyncio.wait_for(m.lease.acquire("music job J", wait_s=BOUND), BOUND)
                assert time.monotonic() - started < 0.5
        assert refused.value.holder_alias == "whisper:turbo"
        assert waiting() == []

    @pytest.mark.asyncio
    async def test_a_request_that_stands_on_a_supervisor_holder_is_refused_at_once(self):
        class Supervisor:
            alias, ENGINE, port = "flash", "strata", None
            _proc, props, _draining = None, None, False

        base_mod._CARD_CLAIMS[Supervisor()] = "claimed"
        w = whisper()
        async with card_held_for("flash", "accepted_request", agent_id="agent", conversation_id="chat"):
            started = time.monotonic()
            with pytest.raises(LeaseWouldWaitOnItself) as refused:
                await asyncio.wait_for(w.lease.acquire("dictation", wait_s=BOUND), BOUND)
            assert time.monotonic() - started < 0.5
        assert refused.value.holder_alias == "flash" and "'flash'" in str(refused.value)
        assert waiting() == [] and not w.lease.granted

    @pytest.mark.asyncio
    async def test_a_persons_stop_lets_an_idle_lease_go_and_names_the_uses_of_a_busy_one(self):
        w = whisper()
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        with pytest.raises(LeaseBusy, match="busy with dictation"):
            await w.lease.let_go_by_hand("Stop by hand", 1)
        assert w.yields == []
        release.set()
        await asyncio.wait_for(holder, BOUND)

        assert await asyncio.wait_for(w.lease.let_go_by_hand("Stop by hand", 1), BOUND) is True
        assert w.yields == [("yield for Stop by hand", "draining")]
        assert not w.lease.granted
        assert await w.lease.let_go_by_hand("Stop by hand", 1) is False, "nothing granted, nothing to stop"
        await nothing_left()

    @pytest.mark.asyncio
    async def test_the_backend_stopping_refuses_every_waiter_and_every_later_request(self):
        w, m = whisper(), music()
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])
        contender = asyncio.create_task(m.lease.acquire("music job J", wait_s=None))
        await until(lambda: waiting() == ["music:yue2"])
        assert base_mod.card_queue()[0]["until_cancelled"] is True and base_mod.card_queue()[0]["give_up_at"] is None

        card_lease.close_the_line("shutdown")
        with pytest.raises(LeaseClosed):
            await asyncio.wait_for(contender, BOUND)
        with pytest.raises(LeaseClosed):
            await m.lease.acquire("music job K", wait_s=BOUND)
        assert waiting() == []
        release.set()
        await asyncio.wait_for(holder, BOUND)
        await nothing_left()
