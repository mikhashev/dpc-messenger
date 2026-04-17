"""Hooks & middleware infrastructure for the agent loop.

Implements ADR-007: per-process middleware chain with two tiers.

- Observer hooks: lifecycle callbacks (logging, metrics, notifications).
  Errors caught and logged, never abort the loop.
- Guard middleware: policy enforcement (e.g., round limits, budget caps).
  Can signal STOP_LOOP. Errors propagate.

This module is standalone: it defines the types and registry, but does NOT
wire itself into loop.py. Integration is done in a later migration step
(see ADR-007 §Migration Plan).

Usage outline::

    hooks = HookRegistry()
    hooks.register(RoundLimitMiddleware(max_rounds=200))  # defined elsewhere
    hooks.register(MyObserver())                          # defined elsewhere

    ctx = HookContext(
        agent_id=..., task_id=..., session_id=..., round_idx=0,
    )
    # loop.py updates ctx.state BEFORE firing
    ctx.state.last_response_has_text = True
    action = await hooks.fire(HookLifecycle.BETWEEN_ROUNDS, ctx)
    if action == HookAction.STOP_LOOP:
        break
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class HookAction(Enum):
    """Return value of a hook handler.

    CONTINUE: proceed normally (same as returning None).
    STOP_LOOP: terminate the agent loop at the next checkpoint.

    Phase 0 deliberately ships only these two values. REDIRECT and RETRY are
    anticipated for Phase 2+ hooks (skill recommendation, evolution proposals)
    and will require different checkpoint semantics. Deferred, not excluded.
    """

    CONTINUE = auto()
    STOP_LOOP = auto()


class HookLifecycle(Enum):
    """Lifecycle points at which hooks may fire.

    Using a string-valued enum lets the registry dispatch by method name
    (``getattr(mw, lifecycle.value)``) while still catching typos at the
    call site (``HookLifecycle.BEFORE_LLM_CAL`` is an AttributeError, not a
    silent no-op).
    """

    BEFORE_LLM_CALL = "before_llm_call"
    AFTER_LLM_CALL = "after_llm_call"
    BEFORE_TOOL_EXEC = "before_tool_exec"
    AFTER_TOOL_EXEC = "after_tool_exec"
    BETWEEN_ROUNDS = "between_rounds"


# --------------------------------------------------------------------------- #
# Context and state
# --------------------------------------------------------------------------- #


@dataclass
class LoopState:
    """Typed state exposed by ``loop.py`` to middleware.

    **Ownership:** ``loop.py`` OWNS these fields. Middleware only READS them.

    **Mutation contract:** the loop MUST update the relevant fields BEFORE
    calling ``HookRegistry.fire()``. If the state is stale at fire time,
    guard decisions will be incorrect. Middleware that needs mutable
    counters or sliding windows stores them in its own instance
    attributes, not here.

    Fields are deliberately minimal for Phase 0. Add fields only when a
    specific guard or observer needs them — do not let this grow into a
    generic dict.
    """

    last_response_has_text: bool = False
    tool_calls_this_turn: int = 0
    consecutive_tool_only_rounds: int = 0
    accumulated_cost_usd: float = 0.0
    recent_tool_args: list = field(default_factory=list)


@dataclass
class HookContext:
    """Identity + typed state passed to every hook handler.

    Identity fields (``agent_id``, ``task_id``, ``session_id``,
    ``round_idx``) are written by the loop and should be treated as
    read-only by middleware. ``state`` follows the mutation contract
    described on :class:`LoopState`.
    """

    agent_id: str
    task_id: str
    session_id: str
    round_idx: int
    state: LoopState = field(default_factory=LoopState)

    # Convenience accessors — shorthand for the most frequently read fields.
    @property
    def last_response_has_text(self) -> bool:
        return self.state.last_response_has_text

    @property
    def tool_calls_this_turn(self) -> int:
        return self.state.tool_calls_this_turn

    @property
    def consecutive_tool_only_rounds(self) -> int:
        return self.state.consecutive_tool_only_rounds

    @property
    def accumulated_cost_usd(self) -> float:
        return self.state.accumulated_cost_usd

    @property
    def recent_tool_args(self) -> list:
        return self.state.recent_tool_args


# --------------------------------------------------------------------------- #
# Base middleware classes
# --------------------------------------------------------------------------- #


class BaseMiddleware:
    """Base class for hook handlers.

    Subclasses override the lifecycle methods they care about. Any method
    not overridden returns ``None`` (equivalent to ``HookAction.CONTINUE``).

    Subclass choice determines error handling — see :class:`GuardMiddleware`
    and :class:`ObserverMiddleware`.
    """

    async def before_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        return None

    async def after_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        return None

    async def before_tool_exec(self, ctx: HookContext) -> Optional[HookAction]:
        return None

    async def after_tool_exec(self, ctx: HookContext) -> Optional[HookAction]:
        return None

    async def between_rounds(self, ctx: HookContext) -> Optional[HookAction]:
        return None


class GuardMiddleware(BaseMiddleware):
    """Policy-enforcement middleware.

    Guards express hard rules (round limits, budget caps, loop detection).
    Exceptions raised by a guard handler **propagate** — the registry does
    not swallow them. Guards may return :attr:`HookAction.STOP_LOOP` to
    terminate the agent loop at the current checkpoint.
    """


class ObserverMiddleware(BaseMiddleware):
    """Observation / side-effect middleware.

    Observers record events (logs, metrics, notifications) without
    influencing control flow. Exceptions raised by an observer handler
    are caught and logged at DEBUG level; the loop continues unaffected.

    Observers SHOULD NOT return :attr:`HookAction.STOP_LOOP`. If they do,
    the registry still honours the signal, but doing so blurs the
    observer/guard distinction. Prefer a guard for hard stops.
    """


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class HookRegistry:
    """Per-process middleware chain.

    A fresh registry is instantiated for every ``DpcAgent.process()`` call.
    This scopes middleware state to the task: there is no cross-session
    memory and no need for locks on the registry itself. Middleware that
    requires cross-session state must implement its own persistence.

    Registration order determines dispatch order: the first middleware
    whose handler returns :attr:`HookAction.STOP_LOOP` wins and subsequent
    middleware in the chain is skipped for that lifecycle event.
    """

    def __init__(self) -> None:
        self._middleware: list[BaseMiddleware] = []

    def register(self, mw: BaseMiddleware) -> None:
        """Append a middleware instance to the chain.

        Duplicates are permitted but usually a bug — the registry does
        not deduplicate.
        """
        self._middleware.append(mw)

    def __len__(self) -> int:
        return len(self._middleware)

    async def fire(
        self, lifecycle: HookLifecycle, ctx: HookContext
    ) -> Optional[HookAction]:
        """Dispatch a lifecycle event to every registered middleware.

        Returns the first :attr:`HookAction.STOP_LOOP` encountered, or
        ``None`` if no middleware requested a stop. Guard exceptions
        propagate; observer exceptions are caught and logged.
        """
        method_name = lifecycle.value
        for mw in self._middleware:
            handler = getattr(mw, method_name, None)
            if handler is None:
                continue
            try:
                result = await handler(ctx)
            except Exception as exc:
                if isinstance(mw, GuardMiddleware):
                    raise
                log.debug(
                    "Observer hook error in %s.%s: %s",
                    mw.__class__.__name__,
                    method_name,
                    exc,
                )
                continue
            if result == HookAction.STOP_LOOP:
                return result
        return None


__all__ = [
    "HookAction",
    "HookLifecycle",
    "LoopState",
    "HookContext",
    "BaseMiddleware",
    "GuardMiddleware",
    "ObserverMiddleware",
    "HookRegistry",
]
