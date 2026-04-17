"""Hooks & middleware infrastructure for the agent loop (ADR-007).

Two tiers: observer hooks for side effects (errors caught and logged)
and guard middleware for policy enforcement (errors propagate, may
STOP_LOOP). One registry per ``DpcAgent.process()`` call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

log = logging.getLogger(__name__)


class HookAction(Enum):
    """Return value of a hook handler.

    REDIRECT and RETRY are anticipated for Phase 2+ hooks and will need
    different checkpoint semantics — deferred, not excluded.
    """

    CONTINUE = auto()
    STOP_LOOP = auto()


class HookLifecycle(Enum):
    """Lifecycle points at which hooks may fire.

    String values let the registry dispatch via ``getattr(mw,
    lifecycle.value)`` while still raising AttributeError on typos at
    the call site instead of silently no-oping.
    """

    BEFORE_LLM_CALL = "before_llm_call"
    AFTER_LLM_CALL = "after_llm_call"
    BEFORE_TOOL_EXEC = "before_tool_exec"
    AFTER_TOOL_EXEC = "after_tool_exec"
    BETWEEN_ROUNDS = "between_rounds"


@dataclass
class LoopState:
    """Typed state the loop exposes to middleware.

    Mutation contract: the loop OWNS these fields and updates them
    BEFORE calling ``HookRegistry.fire()``; middleware only reads them.
    Stale values at fire time produce wrong guard decisions. Middleware
    that needs mutable counters keeps them on the instance, not here.
    """

    last_response_has_text: bool = False
    tool_calls_this_turn: int = 0
    consecutive_tool_only_rounds: int = 0
    accumulated_cost_usd: float = 0.0
    #: Last N tool-call argument dicts, oldest first.
    recent_tool_args: list[dict] = field(default_factory=list)


@dataclass
class HookContext:
    """Identity + typed state passed to every hook handler."""

    agent_id: str
    task_id: str
    session_id: str
    round_idx: int
    state: LoopState = field(default_factory=LoopState)

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


class BaseMiddleware:
    """Base class for hook handlers. Subclasses override what they need."""

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

    def stop_message(self) -> Optional[str]:
        """User-facing reason shown when this middleware returns STOP_LOOP.

        Synchronous and pure — called after ``fire()``, may read ``self``
        but must not do I/O. Return ``None`` to let the loop fall back
        to a generic reason.
        """
        return None


class GuardMiddleware(BaseMiddleware):
    """Policy-enforcement middleware. Exceptions propagate."""


class ObserverMiddleware(BaseMiddleware):
    """Side-effect middleware (logs, metrics). Exceptions caught and
    logged at DEBUG. Should not return STOP_LOOP — prefer a guard."""


class HookRegistry:
    """Per-process middleware chain.

    Fresh registry per ``DpcAgent.process()`` — no cross-session memory,
    no locks. Registration order is dispatch order; the first middleware
    to return STOP_LOOP wins and short-circuits the chain, regardless of
    tier. Convention is guards first, observers after, but the registry
    does not enforce it.
    """

    def __init__(self) -> None:
        self._middleware: list[BaseMiddleware] = []
        #: Middleware that most recently returned STOP_LOOP (or None).
        #: Reset per ``fire()`` call; consumers call its ``stop_message()``.
        self.last_triggered: Optional[BaseMiddleware] = None

    def register(self, mw: BaseMiddleware) -> None:
        self._middleware.append(mw)

    def __len__(self) -> int:
        return len(self._middleware)

    async def fire(
        self, lifecycle: HookLifecycle, ctx: HookContext
    ) -> Optional[HookAction]:
        """Dispatch a lifecycle event. Returns the first STOP_LOOP or None."""
        self.last_triggered = None
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
                self.last_triggered = mw
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
