"""A turn's pre-turn check and its loop are one request on the card (ADR-044, amendment
2026-11-02, the card queue, rule 1: one accepted_request from acceptance to answer).

Card THE-PRE-TURN-CHECK-TAKES-THE-CARD-AS-ITS-OWN-REQUEST-SO-TWO-AGENTS-SWAP-ENGINES-FOR-A-TOKEN-COUNT.
On 2026-11-15 the pre-turn token count stood on the card as a request of its own and let go when
the count came back; the loop then took a new hold, behind the other alias's waiter, so two engine
loads in a row answered only a token count. The replay: Strata is on the card, Warren's turn on the
other alias swaps it out, and Johnny's turn on Strata arrives while Warren's count is being served.

Real `DpcAgent.process`, `run_llm_loop`, adapter and supervisors with the fakes of
`test_one_local_engine_holds_the_card.py`; the provider is a double that uses its supervisor as
the real ones do, for the count as for the rounds.
"""

import asyncio
import contextvars

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

from tests.test_a_request_holds_the_card_from_its_first_round_to_its_last import (  # noqa: F401
    _Engine, _Manager, _agent, _alive, _finish, swap_on, settings,
)
from tests.test_one_local_engine_holds_the_card import _strata, card  # noqa: F401

# The two aliases of the log: Johnny's Strata and Warren's 27B.
STRATA = "strata flash-next"
QWEN = "qwen3.6-27b"


class _CountingEngine(_Engine):
    """`_Engine` that also counts on its engine, as `llamacpp_server` and `strata` do: the
    count starts the child if it is not up (`ensure_running`) and is served in a call slot.
    Every served request is written to `served` as (alias, what, the child that served it)."""

    def __init__(self, supervisor, alias, served, rounds=1):
        super().__init__(supervisor, rounds=rounds)
        self.alias = alias
        self.served = served
        self.in_the_count = None  # awaited inside the count's slot, when set

    def counts_on_its_engine(self):
        return True

    async def engine_context_window(self):
        await self.supervisor.ensure_running()
        return 128000

    async def engine_token_count(self, text, *, as_prompt=False):
        await self.supervisor.ensure_running()
        async with self.supervisor.call_slot():
            self.served.append((self.alias, "count", self.supervisor._proc))
            if self.in_the_count is not None:
                await self.in_the_count()
            return len(text) // 4

    async def generate_with_tools(self, messages, tools, system=None, on_chunk=None,
                                  conversation_id=None, **kw):
        reply = await super().generate_with_tools(messages, tools, system=system, on_chunk=on_chunk,
                                                  conversation_id=conversation_id, **kw)
        self.served.append((self.alias, "round", self.supervisor._proc))
        return reply


def _two_agents_one_card(tmp_path):
    strata, qwen = _strata(tmp_path / "strata", STRATA), _strata(tmp_path / "qwen", QWEN)
    _ACTIVE_SUPERVISORS[STRATA], _ACTIVE_SUPERVISORS[QWEN] = strata, qwen
    served = []
    engines = {STRATA: _CountingEngine(strata, STRATA, served, rounds=2),
               QWEN: _CountingEngine(qwen, QWEN, served, rounds=2)}
    return strata, qwen, engines, _Manager(engines), served


def _own_request(coro) -> asyncio.Task:
    """`coro` as a request of its own, with no hold inherited from the test's task."""
    return asyncio.get_running_loop().create_task(coro, context=contextvars.Context())


class TestTwoAgentsOnTwoAliasesOfOneCard:
    @pytest.mark.asyncio
    async def test_each_turn_swaps_once_and_its_count_and_rounds_are_served_by_one_engine_load(
        self, tmp_path, monkeypatch,
    ):
        strata, qwen, engines, manager, served = _two_agents_one_card(tmp_path)
        swaps = []
        real_note = type(strata)._note_swap

        def noting(self, holder, *args, **kwargs):
            swaps.append((holder.alias, self.alias))
            real_note(self, holder, *args, **kwargs)

        monkeypatch.setattr(type(strata), "_note_swap", noting)
        await strata.ensure_running()  # 12:54:58, before the swap: Strata is on the card

        in_the_count, go_on = asyncio.Event(), asyncio.Event()

        async def warrens_count_waits():
            in_the_count.set()
            await go_on.wait()

        engines[QWEN].in_the_count = warrens_count_waits
        warren = _own_request(
            _agent(tmp_path, manager, provider_alias=QWEN, name="agent_warren").process("a task", "warren-chat"))
        johnny = None
        try:
            # 12:55:10.893: the 27B is up and serving Warren's count.
            await asyncio.wait_for(in_the_count.wait(), 30)
            assert _alive(qwen) and not _alive(strata)

            # Johnny's turn on Strata arrives while Warren's count is served, and waits in line.
            johnny = _own_request(
                _agent(tmp_path, manager, provider_alias=STRATA, name="agent_johnny").process("a task", "johnny-chat"))
            for _ in range(50):
                if base_mod.card_queue():
                    break
                await asyncio.sleep(0.01)
            assert [q["alias"] for q in base_mod.card_queue()] == [STRATA]

            go_on.set()
            assert await asyncio.wait_for(warren, 30) == "the answer"
            assert await asyncio.wait_for(johnny, 30) == "the answer"
        finally:
            go_on.set()
            await _finish(*[t for t in (warren, johnny) if t is not None])

        # One swap per turn: Strata -> 27B for Warren's, 27B -> Strata for Johnny's.
        assert swaps == [(STRATA, QWEN), (QWEN, STRATA)], swaps
        # Each turn in a row, on one load, with nothing of the other between: the pre-turn count,
        # round 1, round 2's count of what round 1 appended, round 2.
        turn = ["count", "round", "count", "round"]
        what = [(alias, kind) for alias, kind, _ in served]
        assert what == [(QWEN, kind) for kind in turn] + [(STRATA, kind) for kind in turn], what
        for alias in (QWEN, STRATA):
            loads = {id(proc) for a, _, proc in served if a == alias}
            assert len(loads) == 1, f"{alias}: its count and its rounds were served by {len(loads)} loads"
        assert not base_mod._CARD_HOLDS and not base_mod.card_queue()

    @pytest.mark.asyncio
    async def test_a_turn_stopped_during_its_pre_turn_check_lets_go_of_the_card(self, tmp_path):
        strata, qwen, engines, manager, served = _two_agents_one_card(tmp_path)
        await qwen.ensure_running()
        in_the_count = asyncio.Event()
        stop = asyncio.Event()

        async def count_waits_forever():
            in_the_count.set()
            await asyncio.sleep(30)

        engines[QWEN].in_the_count = count_waits_forever
        warren = _own_request(
            _agent(tmp_path, manager, provider_alias=QWEN, name="agent_warren").process(
                "a task", "warren-chat", stop_event=stop))
        try:
            await asyncio.wait_for(in_the_count.wait(), 30)
            assert {alias: len(h) for alias, h in base_mod._CARD_HOLDS.items()} == {QWEN: 1}
            stop.set()
            text = await asyncio.wait_for(warren, 10)
        finally:
            await _finish(warren)

        assert text.startswith("⚠️ Stopped by user after 0 rounds")
        assert [kind for _, kind, _ in served] == ["count"], "no round after a Stop in the check"
        assert not base_mod._CARD_HOLDS, "the hold ended with the turn"

    @pytest.mark.asyncio
    async def test_a_turn_whose_pre_turn_check_fails_keeps_one_hold_and_lets_go_at_its_end(
        self, tmp_path, monkeypatch,
    ):
        strata, qwen, engines, manager, served = _two_agents_one_card(tmp_path)
        await qwen.ensure_running()
        holds_seen = []

        async def count_fails(text, *, as_prompt=False):
            holds_seen.append(list(base_mod._CARD_HOLDS.get(QWEN, [])))
            raise RuntimeError("tokenize failed")

        monkeypatch.setattr(engines[QWEN], "engine_token_count", count_fails)
        real_round = engines[QWEN].generate_with_tools

        async def round_sees(*args, **kwargs):
            holds_seen.append(list(base_mod._CARD_HOLDS.get(QWEN, [])))
            return await real_round(*args, **kwargs)

        monkeypatch.setattr(engines[QWEN], "generate_with_tools", round_sees)
        warren = _own_request(
            _agent(tmp_path, manager, provider_alias=QWEN, name="agent_warren").process("a task", "warren-chat"))
        try:
            assert await asyncio.wait_for(warren, 30) == "the answer"
        finally:
            await _finish(warren)

        # The pre-turn count, round 1, round 2's own count of what it appended, round 2.
        assert len(holds_seen) == 4 and all(len(seen) == 1 for seen in holds_seen), holds_seen
        assert all(seen[0] is holds_seen[0][0] for seen in holds_seen),             "one hold, the same through the failed pre-turn count and both rounds"
        assert not base_mod._CARD_HOLDS
