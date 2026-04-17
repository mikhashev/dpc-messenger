"""GuardMiddleware implementations for the agent loop (ADR-007 Step 2).

Extracts the five existing inline guards in :mod:`loop.py` as
:class:`GuardMiddleware` subclasses. Behaviour is preserved 1:1 with
the current implementation; the goal is to have these guards ready
to register via :class:`HookRegistry` once :mod:`loop.py` is refactored
in Step 3.

Until Step 3 lands, nothing imports this module — the inline checks in
``loop.py`` remain the source of truth for runtime behaviour. These
classes are tested independently and plugged in atomically.

Defaults match the constants in the current ``loop.py``:

======================  ========
Constant                Default
======================  ========
DEFAULT_MAX_ROUNDS      200
MAX_TOOL_CALLS_PER_TURN 25
MAX_CONSEC_TOOL_ONLY    15
MAX_DUPLICATE_CALLS     5
BUDGET_FRACTION         0.5
======================  ========
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .hooks import GuardMiddleware, HookAction, HookContext

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 1. Round limit
# --------------------------------------------------------------------------- #


class RoundLimitGuard(GuardMiddleware):
    """Stop the loop after a fixed number of LLM rounds.

    Fires in ``between_rounds`` — by the time this guard sees the context,
    the current ``round_idx`` has already been incremented by the loop, so
    ``round_idx > max_rounds`` means "this round would exceed the cap, do
    not run it". Stateless.

    Operator note: uses strict ``>`` (round indices start at 1 and count
    rounds executed; ``max_rounds`` is inclusive, round number
    ``max_rounds`` itself is allowed). Mirrors ``loop.py:450``.
    """

    def __init__(self, max_rounds: int = 200) -> None:
        self._max_rounds = max_rounds

    async def between_rounds(self, ctx: HookContext) -> Optional[HookAction]:
        if ctx.round_idx > self._max_rounds:
            log.warning(
                "RoundLimitGuard: round_idx=%d exceeded max_rounds=%d",
                ctx.round_idx,
                self._max_rounds,
            )
            return HookAction.STOP_LOOP
        return None


# --------------------------------------------------------------------------- #
# 2. Tool-calls-per-turn limit
# --------------------------------------------------------------------------- #


class ToolLimitGuard(GuardMiddleware):
    """Stop if the LLM emits too many tool calls in a single turn.

    A burst of 25+ tool calls in one response indicates the model has
    fanned out and will not converge on a text answer. Fires in
    ``after_llm_call`` after the loop has set ``tool_calls_this_turn``.
    Stateless.
    """

    def __init__(self, max_per_turn: int = 25) -> None:
        self._max_per_turn = max_per_turn

    async def after_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        if ctx.tool_calls_this_turn > self._max_per_turn:
            log.warning(
                "ToolLimitGuard: %d tool calls in one turn exceeded max=%d",
                ctx.tool_calls_this_turn,
                self._max_per_turn,
            )
            return HookAction.STOP_LOOP
        return None


# --------------------------------------------------------------------------- #
# 3. Research limit (consecutive tool-only rounds)
# --------------------------------------------------------------------------- #


class ResearchLimitGuard(GuardMiddleware):
    """Force a final answer if the agent researches without producing text.

    Mirrors the current ``_consecutive_tool_only`` counter in ``loop.py``:
    reset on any round that produced text content, increment on a round
    that emitted only tool calls. Per ADR-007 §Example, the counter lives
    on the middleware instance, not on :class:`HookContext` — this keeps
    state local and avoids leaky abstraction.

    Fires in ``after_llm_call`` after the loop has set
    ``last_response_has_text`` and ``tool_calls_this_turn``.

    Operator note: uses non-strict ``>=`` (the counter tracks number of
    rounds that contributed; ``max_consecutive`` is exclusive — the
    Nth consecutive tool-only round triggers the stop, not the
    following one). Mirrors ``loop.py:598``.
    """

    def __init__(self, max_consecutive: int = 15) -> None:
        self._max = max_consecutive
        self._counter = 0

    async def after_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        if ctx.last_response_has_text:
            self._counter = 0
        elif ctx.tool_calls_this_turn > 0:
            self._counter += 1
        # Rounds with no text AND no tool calls are ignored — they
        # indicate an empty response, not research.

        if self._counter >= self._max:
            log.warning(
                "ResearchLimitGuard: %d consecutive tool-only rounds reached max=%d",
                self._counter,
                self._max,
            )
            return HookAction.STOP_LOOP
        return None


# --------------------------------------------------------------------------- #
# 4. Loop guard (duplicate tool calls)
# --------------------------------------------------------------------------- #


class LoopGuard(GuardMiddleware):
    """Stop if a single (tool, args) fingerprint repeats too many times.

    Matches the current ``_tool_call_counts`` dict in ``loop.py``: every
    (tool_name, JSON-sorted args) pair gets a session-scoped counter;
    when any counter reaches ``max_duplicate_calls`` the agent is stuck.

    Fires in ``after_llm_call`` — at this point the loop has populated
    ``ctx.state.recent_tool_args`` with the current turn's tool calls,
    each dict shaped as ``{"name": str, "args": dict}``. The guard
    updates its internal fingerprint counters with the new calls and
    returns STOP_LOOP if any exceeds the threshold.
    """

    def __init__(self, max_duplicate_calls: int = 5) -> None:
        self._max = max_duplicate_calls
        self._counts: dict[str, int] = {}

    @staticmethod
    def _fingerprint(call: dict) -> str:
        """Stable ``name::args_json`` key for a single tool call dict."""
        name = call.get("name", "?")
        raw_args = call.get("args", {})
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except Exception:
                pass
        if isinstance(raw_args, dict):
            args_key = json.dumps(raw_args, sort_keys=True)
        else:
            args_key = str(raw_args)
        return f"{name}::{args_key}"

    async def after_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        for call in ctx.recent_tool_args:
            if not isinstance(call, dict):
                continue
            key = self._fingerprint(call)
            self._counts[key] = self._counts.get(key, 0) + 1
            if self._counts[key] >= self._max:
                log.warning(
                    "LoopGuard: fingerprint %r hit %d repeats (max=%d)",
                    key,
                    self._counts[key],
                    self._max,
                )
                return HookAction.STOP_LOOP
        return None


# --------------------------------------------------------------------------- #
# 5. Budget limit
# --------------------------------------------------------------------------- #


class BudgetLimitGuard(GuardMiddleware):
    """Stop if accumulated task cost crosses a fraction of the budget.

    Matches the current ``budget_remaining_usd`` + 50% threshold check in
    ``loop.py``. A ``None`` or non-positive ``budget_remaining_usd``
    disables the guard entirely. Fires in ``between_rounds`` so the
    check runs after tool execution of the just-finished round, before
    the next LLM call.
    """

    def __init__(
        self,
        budget_remaining_usd: Optional[float] = None,
        max_fraction: float = 0.5,
    ) -> None:
        self._budget = budget_remaining_usd
        self._max_fraction = max_fraction

    async def between_rounds(self, ctx: HookContext) -> Optional[HookAction]:
        if self._budget is None or self._budget <= 0:
            return None
        threshold = self._budget * self._max_fraction
        if ctx.accumulated_cost_usd > threshold:
            log.warning(
                "BudgetLimitGuard: cost $%.4f exceeded %.0f%% of budget $%.2f",
                ctx.accumulated_cost_usd,
                self._max_fraction * 100,
                self._budget,
            )
            return HookAction.STOP_LOOP
        return None


__all__ = [
    "RoundLimitGuard",
    "ToolLimitGuard",
    "ResearchLimitGuard",
    "LoopGuard",
    "BudgetLimitGuard",
]
