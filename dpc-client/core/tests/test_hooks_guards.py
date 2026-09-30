"""Integration tests for hooks + guards (ADR-007 Step 3a).

Lock the five GuardMiddleware subclasses + HookRegistry against the
current inline-guard behaviour in ``loop.py``; the Step 3 refactor
must leave these passing unchanged.
"""

from __future__ import annotations

import pytest

from dpc_client_core.dpc_agent.hooks import (
    HookAction,
    HookContext,
    HookLifecycle,
    HookRegistry,
    LoopState,
    ObserverMiddleware,
)
from dpc_client_core.dpc_agent.guards import (
    BudgetLimitGuard,
    LoopGuard,
    ResearchLimitGuard,
    RoundLimitGuard,
    ToolLimitGuard,
)


def make_ctx(round_idx: int = 1, **state_overrides) -> HookContext:
    ctx = HookContext(
        agent_id="test-agent",
        task_id="test-task",
        session_id="test-session",
        round_idx=round_idx,
    )
    for key, value in state_overrides.items():
        setattr(ctx.state, key, value)
    return ctx


class TestRoundLimitGuard:
    @pytest.mark.asyncio
    async def test_below_max_allows(self):
        g = RoundLimitGuard(max_rounds=5)
        assert await g.between_rounds(make_ctx(round_idx=3)) is None

    @pytest.mark.asyncio
    async def test_at_max_allows(self):
        # Strict `>`: the max-th round is still allowed.
        g = RoundLimitGuard(max_rounds=5)
        assert await g.between_rounds(make_ctx(round_idx=5)) is None

    @pytest.mark.asyncio
    async def test_over_max_stops(self):
        g = RoundLimitGuard(max_rounds=5)
        assert (
            await g.between_rounds(make_ctx(round_idx=6))
            == HookAction.STOP_LOOP
        )


class TestToolLimitGuard:
    @pytest.mark.asyncio
    async def test_below_max_allows(self):
        g = ToolLimitGuard(max_per_turn=10)
        assert await g.after_llm_call(make_ctx(tool_calls_this_turn=5)) is None

    @pytest.mark.asyncio
    async def test_at_max_allows(self):
        g = ToolLimitGuard(max_per_turn=10)
        assert await g.after_llm_call(make_ctx(tool_calls_this_turn=10)) is None

    @pytest.mark.asyncio
    async def test_over_max_stops(self):
        g = ToolLimitGuard(max_per_turn=10)
        assert (
            await g.after_llm_call(make_ctx(tool_calls_this_turn=11))
            == HookAction.STOP_LOOP
        )


class TestResearchLimitGuard:
    @pytest.mark.asyncio
    async def test_increments_on_tool_only_rounds(self):
        g = ResearchLimitGuard(max_consecutive=3)
        ctx = make_ctx(last_response_has_text=False, tool_calls_this_turn=1)
        assert await g.after_llm_call(ctx) is None  # counter=1
        assert await g.after_llm_call(ctx) is None  # counter=2
        assert await g.after_llm_call(ctx) == HookAction.STOP_LOOP  # counter=3

    @pytest.mark.asyncio
    async def test_resets_on_text(self):
        g = ResearchLimitGuard(max_consecutive=3)
        tool_ctx = make_ctx(last_response_has_text=False, tool_calls_this_turn=1)
        text_ctx = make_ctx(last_response_has_text=True, tool_calls_this_turn=0)

        await g.after_llm_call(tool_ctx)  # counter=1
        await g.after_llm_call(tool_ctx)  # counter=2
        await g.after_llm_call(text_ctx)  # counter=0 (reset)
        assert await g.after_llm_call(tool_ctx) is None  # counter=1
        assert await g.after_llm_call(tool_ctx) is None  # counter=2

    @pytest.mark.asyncio
    async def test_empty_round_ignored(self):
        # No text, no tool calls — counter unchanged (empty, not research).
        g = ResearchLimitGuard(max_consecutive=2)
        empty_ctx = make_ctx(last_response_has_text=False, tool_calls_this_turn=0)
        for _ in range(5):
            assert await g.after_llm_call(empty_ctx) is None


class _Rounds:
    """Drive ResearchLimitGuard the way run_llm_loop does: AFTER_LLM_CALL of
    round N sees round N's calls but round N-1's results."""

    def __init__(self, guard):
        self.g = guard
        self.prev_results: list = []

    async def silent(self, name, args, output):
        ctx = make_ctx(
            last_response_has_text=False,
            tool_calls_this_turn=1,
            recent_tool_args=[{"name": name, "args": args}],
            recent_tool_results=self.prev_results,
        )
        verdict = await self.g.after_llm_call(ctx)
        self.prev_results = [{"name": name, "output": output}]
        return verdict

    async def text(self):
        ctx = make_ctx(
            last_response_has_text=True,
            tool_calls_this_turn=0,
            recent_tool_args=[],
            recent_tool_results=self.prev_results,
        )
        return await self.g.after_llm_call(ctx)


class TestResearchLimitGuardProgress:
    @pytest.mark.asyncio
    async def test_paging_a_document_does_not_trip(self):
        r = _Rounds(ResearchLimitGuard())
        for page in range(1, 31):
            v = await r.silent(
                "read_document", {"path": "a.pdf", "pages": str(page)}, "text of page"
            )
            assert v is None, f"tripped at page {page}"

    @pytest.mark.asyncio
    async def test_same_page_repeated_trips(self):
        r = _Rounds(ResearchLimitGuard())
        verdicts = [
            await r.silent("read_document", {"path": "a.pdf", "pages": "1"}, "p1")
            for _ in range(16)  # the first read of page 1 was new ground
        ]
        assert verdicts[-1] == HookAction.STOP_LOOP
        assert all(v is None for v in verdicts[:-1])

    @pytest.mark.asyncio
    async def test_error_results_trip(self):
        r = _Rounds(ResearchLimitGuard())
        verdicts = [
            await r.silent(
                "browse_page", {"url": f"https://x.test/{i}"}, "⚠️ 403 refused"
            )
            for i in range(15)
        ]
        assert verdicts[-1] == HookAction.STOP_LOOP

    @pytest.mark.asyncio
    async def test_progress_then_no_progress_trips_after_fifteen(self):
        r = _Rounds(ResearchLimitGuard())
        for page in range(1, 11):
            assert await r.silent(
                "read_document", {"path": "a.pdf", "pages": str(page)}, "ok"
            ) is None
        verdicts = [
            await r.silent("read_document", {"path": "a.pdf", "pages": "10"}, "ok")
            for _ in range(16)
        ]
        # The first repeat is settled against page 10's fresh result, so the
        # counter starts over there: 15 no-progress rounds after that.
        assert verdicts.index(HookAction.STOP_LOOP) == 14

    @pytest.mark.asyncio
    async def test_absolute_ceiling_trips_even_with_progress(self):
        r = _Rounds(ResearchLimitGuard())
        verdict = None
        for page in range(1, 100):
            verdict = await r.silent(
                "read_document", {"path": "a.pdf", "pages": str(page)}, "ok"
            )
            if verdict is not None:
                break
        assert verdict == HookAction.STOP_LOOP
        assert page == 60
        assert "60 rounds" in r.g.stop_message()

    @pytest.mark.asyncio
    async def test_text_round_resets_both_counters(self):
        r = _Rounds(ResearchLimitGuard(max_consecutive=3, max_silent_total=5))
        for _ in range(2):
            await r.silent("read_document", {"path": "a.pdf", "pages": "1"}, "ok")
        await r.text()
        for _ in range(2):
            assert await r.silent(
                "read_document", {"path": "b.pdf", "pages": "1"}, "⚠️ no"
            ) is None

    @pytest.mark.asyncio
    async def test_snapshot_progress_only_when_output_changes(self):
        r = _Rounds(ResearchLimitGuard(max_consecutive=3))
        for i in range(10):
            assert await r.silent("browser_snapshot", {}, f"page {i}") is None
        verdicts = [await r.silent("browser_snapshot", {}, "same") for _ in range(5)]
        assert HookAction.STOP_LOOP in verdicts

    @pytest.mark.asyncio
    async def test_url_keyed_ignores_size_preset(self):
        r = _Rounds(ResearchLimitGuard(max_consecutive=3))
        v = [
            await r.silent("browse_page", {"url": "https://x.test", "size": s}, "ok")
            for s in ("s", "m", "l", "f", "s")
        ]
        assert HookAction.STOP_LOOP in v


class TestLoopGuard:
    @pytest.mark.asyncio
    async def test_same_fingerprint_fires_at_max(self):
        g = LoopGuard(max_duplicate_calls=3)
        call = {"name": "search", "args": {"q": "x"}}
        ctx = make_ctx(recent_tool_args=[call])
        assert await g.after_llm_call(ctx) is None  # count=1
        assert await g.after_llm_call(ctx) is None  # count=2
        assert await g.after_llm_call(ctx) == HookAction.STOP_LOOP  # count=3

    @pytest.mark.asyncio
    async def test_different_fingerprints_independent(self):
        g = LoopGuard(max_duplicate_calls=2)
        ctx_a = make_ctx(recent_tool_args=[{"name": "search", "args": {"q": "a"}}])
        ctx_b = make_ctx(recent_tool_args=[{"name": "search", "args": {"q": "b"}}])

        assert await g.after_llm_call(ctx_a) is None  # a=1
        assert await g.after_llm_call(ctx_b) is None  # b=1
        assert await g.after_llm_call(ctx_a) == HookAction.STOP_LOOP  # a=2

    @pytest.mark.asyncio
    async def test_json_string_args_parsed(self):
        # Providers sometimes deliver args as a JSON string; fingerprint
        # must normalise them to the same key as a dict.
        g = LoopGuard(max_duplicate_calls=2)
        dict_call = {"name": "tool", "args": {"x": 1}}
        str_call = {"name": "tool", "args": '{"x": 1}'}
        assert await g.after_llm_call(make_ctx(recent_tool_args=[dict_call])) is None
        assert (
            await g.after_llm_call(make_ctx(recent_tool_args=[str_call]))
            == HookAction.STOP_LOOP
        )

    @pytest.mark.asyncio
    async def test_non_dict_call_skipped(self):
        # Defensive: non-dict entries in recent_tool_args are skipped.
        g = LoopGuard(max_duplicate_calls=2)
        ctx = make_ctx(recent_tool_args=["not-a-dict", 42, None])
        assert await g.after_llm_call(ctx) is None


class TestBudgetLimitGuard:
    @pytest.mark.asyncio
    async def test_below_threshold_allows(self):
        g = BudgetLimitGuard(budget_remaining_usd=1.0, max_fraction=0.5)
        assert await g.between_rounds(make_ctx(accumulated_cost_usd=0.4)) is None

    @pytest.mark.asyncio
    async def test_above_threshold_stops(self):
        g = BudgetLimitGuard(budget_remaining_usd=1.0, max_fraction=0.5)
        assert (
            await g.between_rounds(make_ctx(accumulated_cost_usd=0.6))
            == HookAction.STOP_LOOP
        )

    @pytest.mark.asyncio
    async def test_none_budget_is_noop(self):
        g = BudgetLimitGuard(budget_remaining_usd=None)
        assert await g.between_rounds(make_ctx(accumulated_cost_usd=1000.0)) is None

    @pytest.mark.asyncio
    async def test_zero_budget_is_noop(self):
        # Non-positive budget is treated the same as None — disabled.
        g = BudgetLimitGuard(budget_remaining_usd=0.0, max_fraction=0.5)
        assert await g.between_rounds(make_ctx(accumulated_cost_usd=0.1)) is None


class TestRegistryIntegration:
    @pytest.mark.asyncio
    async def test_guards_dispatch_via_lifecycle(self):
        reg = HookRegistry()
        reg.register(RoundLimitGuard(max_rounds=5))
        reg.register(ToolLimitGuard(max_per_turn=3))

        ctx = make_ctx(round_idx=3, tool_calls_this_turn=2)
        assert await reg.fire(HookLifecycle.BETWEEN_ROUNDS, ctx) is None
        assert await reg.fire(HookLifecycle.AFTER_LLM_CALL, ctx) is None

        ctx.state.tool_calls_this_turn = 10
        assert (
            await reg.fire(HookLifecycle.AFTER_LLM_CALL, ctx) == HookAction.STOP_LOOP
        )

    @pytest.mark.asyncio
    async def test_first_guard_to_stop_wins(self):
        reg = HookRegistry()
        reg.register(RoundLimitGuard(max_rounds=0))   # always fires
        reg.register(ToolLimitGuard(max_per_turn=0))  # also would fire

        ctx = make_ctx(round_idx=1, tool_calls_this_turn=1)
        assert (
            await reg.fire(HookLifecycle.BETWEEN_ROUNDS, ctx) == HookAction.STOP_LOOP
        )

    @pytest.mark.asyncio
    async def test_observer_error_swallowed_by_registry(self):
        class NoisyObserver(ObserverMiddleware):
            async def between_rounds(self, ctx):
                raise RuntimeError("observer boom")

        reg = HookRegistry()
        reg.register(NoisyObserver())
        reg.register(RoundLimitGuard(max_rounds=5))

        ctx = make_ctx(round_idx=3)
        assert await reg.fire(HookLifecycle.BETWEEN_ROUNDS, ctx) is None

    @pytest.mark.asyncio
    async def test_guard_error_propagates(self):
        class AngryGuard(RoundLimitGuard):
            async def between_rounds(self, ctx):
                raise ValueError("guard boom")

        reg = HookRegistry()
        reg.register(AngryGuard())

        with pytest.raises(ValueError, match="guard boom"):
            await reg.fire(HookLifecycle.BETWEEN_ROUNDS, make_ctx())


class TestContextLimitGuard:
    """The sixth guard: nothing watched the one resource a long task exhausts.

    Rounds, tools, research, repetition and money were all guarded; the context
    window was watched by compaction alone, and compaction is a best effort — it
    reaches only tool results outside the recent rounds and reduces each of them
    once. Observed 2026-08-23: an agent six rounds in, threshold 0.3 so compaction
    firing continuously, sitting at 81 % of its window with nothing to stop it.
    """

    @pytest.mark.asyncio
    async def test_a_prompt_past_the_ceiling_stops_the_loop(self):
        from dpc_client_core.dpc_agent.guards import ContextLimitGuard

        guard = ContextLimitGuard(ratio=0.95)
        ctx = make_ctx(round_idx=7, last_prompt_tokens=205_000, context_window=210_000)

        assert await guard.between_rounds(ctx) is HookAction.STOP_LOOP
        assert "CONTEXT_LIMIT" in guard.stop_message()
        assert "98%" in guard.stop_message()

    @pytest.mark.asyncio
    async def test_a_run_compaction_can_still_rescue_is_not_interrupted(self):
        """81 % is the case that prompted this guard and it must NOT fire there:
        compaction still has rounds to work with, and a guard that stops a run
        the mechanism would have saved is worse than no guard."""
        from dpc_client_core.dpc_agent.guards import ContextLimitGuard

        guard = ContextLimitGuard(ratio=0.95)
        ctx = make_ctx(round_idx=6, last_prompt_tokens=174_005, context_window=215_040)

        assert await guard.between_rounds(ctx) is None

    @pytest.mark.asyncio
    async def test_a_window_nobody_could_resolve_is_not_a_limit_to_enforce(self):
        """Local models leave `context_window` null and the loop resolves it from
        the provider. When that fails the honest answer is silence, not a guess
        that stops every agent on the box."""
        from dpc_client_core.dpc_agent.guards import ContextLimitGuard

        guard = ContextLimitGuard(ratio=0.95)
        assert await guard.between_rounds(
            make_ctx(round_idx=3, last_prompt_tokens=900_000, context_window=0)
        ) is None

    @pytest.mark.asyncio
    async def test_the_first_round_has_no_previous_size_to_judge(self):
        from dpc_client_core.dpc_agent.guards import ContextLimitGuard

        guard = ContextLimitGuard(ratio=0.95)
        assert await guard.between_rounds(
            make_ctx(round_idx=1, last_prompt_tokens=0, context_window=215_040)
        ) is None
