"""GuardMiddleware implementations for the agent loop (ADR-007).

Five guards that used to live as inline if/elif blocks in
``loop.py::_process_agent_loop``. Each one owns its own state and
exposes a ``stop_message()`` that the loop reads via
:attr:`HookRegistry.last_triggered` after :meth:`HookRegistry.fire`.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .hooks import GuardMiddleware, HookAction, HookContext

log = logging.getLogger(__name__)


class RoundLimitGuard(GuardMiddleware):
    """Stop the loop after a fixed number of LLM rounds.

    Uses strict ``>``: round indices are 1-based counts of rounds
    executed, so ``max_rounds`` itself is the last allowed round.
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

    def stop_message(self) -> str:
        return (
            f"[ROUND_LIMIT] Task exceeded MAX_ROUNDS ({self._max_rounds}). "
            "Consider breaking into smaller tasks."
        )


class ToolLimitGuard(GuardMiddleware):
    """Stop if the LLM emits too many tool calls in a single turn.

    A burst this large in one response means the model has fanned out
    and will not converge on a text answer.
    """

    def __init__(self, max_per_turn: int = 25) -> None:
        self._max_per_turn = max_per_turn
        self._last_count = 0

    async def after_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        self._last_count = ctx.tool_calls_this_turn
        if ctx.tool_calls_this_turn > self._max_per_turn:
            log.warning(
                "ToolLimitGuard: %d tool calls in one turn exceeded max=%d",
                ctx.tool_calls_this_turn,
                self._max_per_turn,
            )
            return HookAction.STOP_LOOP
        return None

    def stop_message(self) -> str:
        return (
            f"[TOOL_LIMIT] You generated {self._last_count} tool calls in a "
            f"single turn, which exceeds the limit of {self._max_per_turn}. "
            "Stop calling tools. Summarise what you know and give your "
            "final answer now."
        )


class ResearchLimitGuard(GuardMiddleware):
    """Force a final answer after too many consecutive tool-only rounds.

    Counter lives on the instance (per ADR-007: guard-specific state
    does not leak into :class:`HookContext`). Reset on any round that
    produced text, increment on a round that emitted only tool calls.
    Rounds with neither text nor tool calls are empty and ignored.

    Uses non-strict ``>=``: the counter is incremented before the check,
    so the Nth consecutive tool-only round triggers the stop.
    """

    def __init__(self, max_consecutive: int = 15) -> None:
        self._max = max_consecutive
        self._counter = 0

    async def after_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        if ctx.last_response_has_text:
            self._counter = 0
        elif ctx.tool_calls_this_turn > 0:
            self._counter += 1

        if self._counter >= self._max:
            log.warning(
                "ResearchLimitGuard: %d consecutive tool-only rounds reached max=%d",
                self._counter,
                self._max,
            )
            return HookAction.STOP_LOOP
        return None

    def stop_message(self) -> str:
        return (
            f"[RESEARCH_LIMIT] You have spent {self._counter} consecutive "
            "rounds calling tools without providing any text response to "
            "the user. Stop researching. Summarise your findings and give "
            "your answer now."
        )


class LoopGuard(GuardMiddleware):
    """Stop if a single (tool, args) fingerprint repeats too many times.

    Session-scoped counter per ``name::json_sorted_args`` fingerprint.
    When any fingerprint hits the cap the agent is stuck in a repeat
    loop. Tool-call dicts are shaped ``{"name": str, "args": dict}``;
    ``args`` may arrive as a JSON string from some providers and is
    normalised before fingerprinting.
    """

    def __init__(self, max_duplicate_calls: int = 5) -> None:
        self._max = max_duplicate_calls
        self._counts: dict[str, int] = {}
        self._last_stuck: list[str] = []

    @staticmethod
    def _fingerprint(call: dict) -> str:
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
                name = call.get("name", "?")
                if name not in self._last_stuck:
                    self._last_stuck.append(name)
                return HookAction.STOP_LOOP
        return None

    def stop_message(self) -> str:
        dedup = ", ".join(sorted(set(self._last_stuck))) or "?"
        return (
            f"[LOOP_GUARD] You have called the following tool(s) with "
            f"identical arguments {self._max} or more times without new "
            f"information: {dedup}. Stop repeating these calls. "
            "Summarise what you know so far and give your final answer now."
        )


class BudgetLimitGuard(GuardMiddleware):
    """Stop when accumulated cost crosses a fraction of the budget.

    ``budget_remaining_usd`` of ``None`` or non-positive disables the
    guard — not every task carries a budget.
    """

    def __init__(
        self,
        budget_remaining_usd: Optional[float] = None,
        max_fraction: float = 0.5,
    ) -> None:
        self._budget = budget_remaining_usd
        self._max_fraction = max_fraction
        self._last_cost = 0.0

    async def between_rounds(self, ctx: HookContext) -> Optional[HookAction]:
        if self._budget is None or self._budget <= 0:
            return None
        self._last_cost = ctx.accumulated_cost_usd
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

    def stop_message(self) -> str:
        return (
            f"[BUDGET_LIMIT] Task spent ${self._last_cost:.3f} (>"
            f"{self._max_fraction * 100:.0f}% of budget "
            f"${self._budget:.2f}). Give your final response now."
        )


__all__ = [
    "RoundLimitGuard",
    "ToolLimitGuard",
    "ResearchLimitGuard",
    "LoopGuard",
    "BudgetLimitGuard",
]
