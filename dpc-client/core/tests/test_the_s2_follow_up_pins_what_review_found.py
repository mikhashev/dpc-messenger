"""ADR-052, the S2 follow-up (group review #75-#91 of `1e57932f`): what the review found, pinned.

A person's Stop takes a chat's lease waiter out of the lease line as it takes the chat's requests out
of the card queue (rule 12), and leaves no mark behind once the yield the waiter began has ended. The
lease line and the supervisors ask a holder again after one interval, `_LEASE_ASK_AGAIN_S`, so one
patch moves both. A supervisor's start has two budgets that are not one: at most `_MAX_SWAP_ROUNDS`
asks to one lease (a lease that answers `refused` costs no round), and at most `_MAX_SWAP_ROUNDS`
holders actually let go or stopped.

Fake lease clients from `_card_lease_helpers`; real supervisors through their real `ensure_running`,
faked at their seams as in `test_one_local_engine_holds_the_card.py`.
"""

import asyncio

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import (
    CardHeldError,
    TakenOutOfTheCardQueue,
    card_held_for,
    leave_card_queue,
)

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
    fast,
    hold_open,
    holding,
    music,
    nothing_left,
    swap,
    until,
    waiting,
    whisper,
)
from tests.test_one_local_engine_holds_the_card import _llama, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick")


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.005)
    monkeypatch.setattr(base_mod, "_LAST_SWAP", None)


async def _in_chat(conversation_id, coro_fn):
    """The body runs under an agent turn's hold in `conversation_id`, as a tool's transcription or a
    music job started from a chat would."""
    async with card_held_for(None, "accepted_request", agent_id="agent", conversation_id=conversation_id):
        return await coro_fn()


class TestAPersonsStopTakesALeaseWaiterOut:
    @pytest.mark.asyncio
    async def test_a_waiter_until_cancelled_leaves_at_once_and_meets_taken_out(self):
        w, m = whisper(eta=10), music()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        job = asyncio.create_task(_in_chat("chat", lambda: m.lease.acquire("music job J", wait_s=None)))
        await until(lambda: waiting() == ["music:yue2"])
        hold = card_lease._LEASE_LINE[0].hold

        leave_card_queue("another chat")
        assert waiting() == ["music:yue2"], "a Stop in another chat takes nothing"

        leave_card_queue("chat")

        assert waiting() == [], "gone from the line in the Stop's own stretch"
        with pytest.raises(TakenOutOfTheCardQueue) as taken:
            await asyncio.wait_for(job, BOUND)
        assert isinstance(taken.value, card_lease.LeaseTakenOut)
        assert taken.value.holder_alias == "music:yue2"
        assert hold.taken_out is True
        assert not m.lease.granted and w.lease._draining is False
        release.set()
        await asyncio.wait_for(dictation, BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", ["refused", "could_not_let_go", "let_go"])
    async def test_the_mark_it_made_ends_with_the_yield_and_nobody_asks_again(self, answer):
        gate = asyncio.Event()
        w = whisper(gate=gate, outcome=answer)
        await asyncio.wait_for(w.lease.acquire("preload", wait_s=BOUND), BOUND)
        m = music()
        job = asyncio.create_task(_in_chat("chat", lambda: m.lease.acquire("music job J", wait_s=None)))
        await until(lambda: len(w.yields) == 1)
        assert w.lease._draining is True and w.lease._swap_for == "music:yue2"

        leave_card_queue("chat")
        with pytest.raises(TakenOutOfTheCardQueue):
            await asyncio.wait_for(job, BOUND)
        assert waiting() == []
        # The yield is the holder's own work and runs on: cutting the mark mid-unload would admit a
        # use onto a model that is going away (L5, L6).
        assert w.lease._draining is True

        gate.set()
        await until(lambda: not card_lease._YIELDS)

        assert w.lease._draining is False and w.lease._swap_for is None, "no mark for a waiter that is gone"
        await asyncio.sleep(10 * base_mod._LEASE_ASK_AGAIN_S)
        assert len(w.yields) == 1, "nothing asks again for a waiter that left"
        assert not m.lease.granted
        assert holding() == ([] if answer == "let_go" else ["whisper:turbo"])
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_later_owner_is_not_refused_by_the_stopped_waiters_ask(self):
        gate = asyncio.Event()
        w = whisper(gate=gate, outcome="refused")
        await asyncio.wait_for(w.lease.acquire("preload", wait_s=BOUND), BOUND)
        m = music()
        job = asyncio.create_task(_in_chat("chat", lambda: m.lease.acquire("music job J", wait_s=None)))
        await until(lambda: len(w.yields) == 1)
        leave_card_queue("chat")
        with pytest.raises(TakenOutOfTheCardQueue):
            await asyncio.wait_for(job, BOUND)
        gate.set()
        await until(lambda: not w.lease._draining)

        # The holder serves again: a new use is admitted at once, not refused as draining.
        async with w.lease.use("dictation", wait_s=0):
            assert w.lease._busy_for_the_card(w.lease.state)
        await nothing_left()


class TestOneConstantForAskingAgain:
    def test_the_lease_line_has_no_interval_of_its_own(self):
        assert not hasattr(card_lease, "_YIELD_RETRY_S")

    @pytest.mark.asyncio
    async def test_patching_the_supervisors_interval_moves_the_lease_lines_asks(self, monkeypatch):
        # A stale copy in the lease module, if one were read, would ask once in this window.
        monkeypatch.setattr(card_lease, "_YIELD_RETRY_S", 100.0, raising=False)
        monkeypatch.setattr(base_mod, "_LEASE_ASK_AGAIN_S", 0.02)
        w = whisper(outcome="refused")
        await asyncio.wait_for(w.lease.acquire("preload", wait_s=BOUND), BOUND)
        m = music()
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=BOUND))

        await until(lambda: len(w.yields) >= 2, bound=1)

        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        await nothing_left()


def _a_chain(clients, refusals_first=0):
    """Each client lets go when asked and, in the same stretch, the next one takes the card past the
    line (granted directly): a holder that keeps coming back, which only the safety net stops. The
    first one refuses `refusals_first` times before it lets go."""
    for i, client in enumerate(clients):
        nxt = clients[i + 1] if i + 1 < len(clients) else None
        answers = ["refused"] * (refusals_first if i == 0 else 0)

        def outcome(nxt=nxt, answers=answers):
            if answers:
                return answers.pop(0)
            if nxt is not None:
                nxt.lease._grant("came back past the line")
            return "let_go"

        client.outcome = outcome


class TestAStartHasTwoBudgetsNotOne:
    @pytest.mark.asyncio
    async def test_refusals_cost_no_round_so_four_holders_can_still_be_let_go(self):
        # One lease refuses 3 times, then 4 holders let go in turn: 4 asks to the first lease and 4
        # evictions. One merged counter would stop after the first lease's 4th answer.
        swap(True, 30)
        clients = [whisper(name) for name in ("a", "b", "c", "d")]
        _a_chain(clients, refusals_first=3)
        await asyncio.wait_for(clients[0].lease.acquire("preload", wait_s=BOUND), BOUND)

        await asyncio.wait_for(_llama().ensure_running(), BOUND)

        assert [len(client.yields) for client in clients] == [4, 1, 1, 1]
        assert holding() == ["qwen27"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_no_more_than_four_holders_are_let_go_for_one_start(self):
        swap(True, 30)
        clients = [whisper(name) for name in ("a", "b", "c", "d", "e")]
        _a_chain(clients)
        await asyncio.wait_for(clients[0].lease.acquire("preload", wait_s=BOUND), BOUND)

        with pytest.raises(CardHeldError) as refused:
            await asyncio.wait_for(_llama().ensure_running(), BOUND)

        assert [len(client.yields) for client in clients] == [1, 1, 1, 1, 0]
        assert refused.value.holder_alias == "whisper:e"
        assert holding() == ["whisper:e"]
        await nothing_left()
