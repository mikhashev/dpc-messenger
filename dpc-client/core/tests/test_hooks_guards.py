"""Integration tests for hooks + guards infrastructure (ADR-007 Step 3a).

These tests exercise the five :class:`GuardMiddleware` subclasses in
:mod:`dpc_client_core.dpc_agent.guards` against the :class:`HookContext`
/ :class:`LoopState` contract from :mod:`dpc_client_core.dpc_agent.hooks`.

Scope:
- Each guard is exercised for below-threshold, at-threshold, and
  over-threshold conditions.
- Guard state transitions (counter reset on text for ResearchLimitGuard,
  independent fingerprint counters for LoopGuard) are covered.
- Registry end-to-end: register multiple guards, verify lifecycle
  dispatch and STOP_LOOP propagation.
- Observer error tier: exceptions from ObserverMiddleware are
  swallowed; exceptions from GuardMiddleware propagate.

These tests lock behaviour against the current inline checks in
``loop.py`` so the Step 3 refactor can be verified as no-op.
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


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def make_ctx(round_idx: int = 1, **state_overrides) -> HookContext:
    """Build a HookContext with a fresh LoopState and optional overrides."""
    ctx = HookContext(
        agent_id="test-agent",
        task_id="test-task",
        session_id="test-session",
        round_idx=round_idx,
    )
    for key, value in state_overrides.items():
        setattr(ctx.state, key, value)
    return ctx


# --------------------------------------------------------------------------- #
# RoundLimitGuard
# --------------------------------------------------------------------------- #


class TestRoundLimitGuard:
    @pytest.mark.asyncio
    async def test_below_max_allows(self):
        g = RoundLimitGuard(max_rounds=5)
        assert await g.between_rounds(make_ctx(round_idx=3)) is None

    @pytest.mark.asyncio
    async def test_at_max_allows(self):
        # Strict > means the max-th round is still allowed.
        g = RoundLimitGuard(max_rounds=5)
        assert await g.between_rounds(make_ctx(round_idx=5)) is None

    @pytest.mark.asyncio
    async def test_over_max_stops(self):
        g = RoundLimitGuard(max_rounds=5)
        assert (
            await g.between_rounds(make_ctx(round_idx=6))
            == HookAction.STOP_LOOP
        )


# --------------------------------------------------------------------------- #
# ToolLimitGuard
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# ResearchLimitGuard
# --------------------------------------------------------------------------- #


class TestResearchLimitGuard:
    @pytest.mark.asyncio
    async def test_increments_on_tool_only_rounds(self):
        g = ResearchLimitGuard(max_consecutive=3)
        ctx = make_ctx(last_response_has_text=False, tool_calls_this_turn=1)
        assert await g.after_llm_call(ctx) is None  # counter=1
        assert await g.after_llm_call(ctx) is None  # counter=2
        assert (
            await g.after_llm_call(ctx) == HookAction.STOP_LOOP
        )  # counter=3 triggers

    @pytest.mark.asyncio
    async def test_resets_on_text(self):
        g = ResearchLimitGuard(max_consecutive=3)
        tool_ctx = make_ctx(last_response_has_text=False, tool_calls_this_turn=1)
        text_ctx = make_ctx(last_response_has_text=True, tool_calls_this_turn=0)

        await g.after_llm_call(tool_ctx)  # counter=1
        await g.after_llm_call(tool_ctx)  # counter=2
        await g.after_llm_call(text_ctx)  # counter=0
        # Now two more tool-only rounds must not fire (counter reset)
        assert await g.after_llm_call(tool_ctx) is None  # counter=1
        assert await g.after_llm_call(tool_ctx) is None  # counter=2

    @pytest.mark.asyncio
    async def test_empty_round_ignored(self):
        # No text, no tool calls -> counter unchanged (round is empty, not research)
        g = ResearchLimitGuard(max_consecutive=2)
        empty_ctx = make_ctx(last_response_has_text=False, tool_calls_this_turn=0)
        for _ in range(5):
            assert await g.after_llm_call(empty_ctx) is None


# --------------------------------------------------------------------------- #
# LoopGuard
# --------------------------------------------------------------------------- #


class TestLoopGuard:
    @pytest.mark.asyncio
    async def test_same_fingerprint_fires_at_max(self):
        g = LoopGuard(max_duplicate_calls=3)
        call = {"name": "search", "args": {"q": "x"}}
        ctx = make_ctx(recent_tool_args=[call])
        assert await g.after_llm_call(ctx) is None  # count=1
        assert await g.after_llm_call(ctx) is None  # count=2
        assert (
            await g.after_llm_call(ctx) == HookAction.STOP_LOOP
        )  # count=3

    @pytest.mark.asyncio
    async def test_different_fingerprints_independent(self):
        g = LoopGuard(max_duplicate_calls=2)
        ctx_a = make_ctx(recent_tool_args=[{"name": "search", "args": {"q": "a"}}])
        ctx_b = make_ctx(recent_tool_args=[{"name": "search", "args": {"q": "b"}}])

        assert await g.after_llm_call(ctx_a) is None  # a count=1
        assert await g.after_llm_call(ctx_b) is None  # b count=1
        assert await g.after_llm_call(ctx_a) == HookAction.STOP_LOOP  # a count=2

    @pytest.mark.asyncio
    async def test_json_string_args_parsed(self):
        # loop.py can deliver args as a JSON string when the provider gives
        # raw LLM output — the fingerprint must normalise both to the same key.
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
        # Defensive: if recent_tool_args gets polluted with non-dicts,
        # the guard skips them without raising.
        g = LoopGuard(max_duplicate_calls=2)
        ctx = make_ctx(recent_tool_args=["not-a-dict", 42, None])
        assert await g.after_llm_call(ctx) is None


# --------------------------------------------------------------------------- #
# BudgetLimitGuard
# --------------------------------------------------------------------------- #


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
        # Even a huge cost shouldn't fire when budget tracking is off.
        assert await g.between_rounds(make_ctx(accumulated_cost_usd=1000.0)) is None

    @pytest.mark.asyncio
    async def test_zero_budget_is_noop(self):
        # Non-positive budget treated same as None — disabled.
        g = BudgetLimitGuard(budget_remaining_usd=0.0, max_fraction=0.5)
        assert await g.between_rounds(make_ctx(accumulated_cost_usd=0.1)) is None


# --------------------------------------------------------------------------- #
# Registry integration
# --------------------------------------------------------------------------- #


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
        # Register two guards that both fire; the one registered first
        # should short-circuit the chain.
        reg = HookRegistry()
        reg.register(RoundLimitGuard(max_rounds=0))   # always fires
        reg.register(ToolLimitGuard(max_per_turn=0))  # also would fire

        ctx = make_ctx(round_idx=1, tool_calls_this_turn=1)
        # between_rounds: only the round guard has a handler, fires STOP_LOOP
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

        # The observer raises, but the guard still executes and returns None.
        ctx = make_ctx(round_idx=3)
        assert await reg.fire(HookLifecycle.BETWEEN_ROUNDS, ctx) is None

    @pytest.mark.asyncio
    async def test_guard_error_propagates(self):
        # Symmetric contract: guard exceptions are not swallowed.
        class AngryGuard(RoundLimitGuard):
            async def between_rounds(self, ctx):
                raise ValueError("guard boom")

        reg = HookRegistry()
        reg.register(AngryGuard())

        with pytest.raises(ValueError, match="guard boom"):
            await reg.fire(HookLifecycle.BETWEEN_ROUNDS, make_ctx())
