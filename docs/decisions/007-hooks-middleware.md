# ADR-007: Hooks & Middleware Infrastructure (Phase 0)

**Status:** Accepted (2026-04-17)
**Date:** 2026-04-10 (Proposed) → 2026-04-17 (Accepted)
**Authors:** Ark (design), CC (research)
**Scope:** Phase 0 — Agent Maturity Track enabler
**External Review:** Hope, J.A.R.V.I.S. (2026-04-10)
**Implementation status:** Step 1 (`hooks.py`) implemented in `bdaeb03` + `df1e2ba`; Steps 2–4 pending.

---

## Context

DPC Agent's main loop (`loop.py`, 748 lines) contains 5 hardcoded guard checks:

| Guard | Line | State dependency |
|-------|------|------------------|
| ROUND_LIMIT | ~452 | round counter |
| TOOL_LIMIT | ~512 | per-turn tool count |
| RESEARCH_LIMIT | ~606 | `_consecutive_tool_only` counter |
| LOOP_GUARD | ~657 | sliding window of tool args |
| BUDGET_LIMIT | ~732 | accumulated cost |

These guards are inline if/elif blocks, tightly coupled to loop internals. Adding new behavior (skill recommendation, truncation auto-suggest, evolution verification) requires modifying loop.py directly — violating Open/Closed Principle.

**Root problem pattern:** S24 audit revealed that multiple agent issues (skill blindness, stale knowledge, duplicate reflections, truncation ignorance) share one root cause: write-side optimized, read-side neglected architecture. There is no interception layer between intent and execution where corrective behavior can be injected.

**Research basis:** Four independent AI agent frameworks were analyzed:
- Framework (Apache 2.0) — hooks (observe) + middleware (control), two-tier
- DeerFlow (MIT, Bytedance Ltd) — LangChain-based middleware chain, 13 components
- Claw Code (MIT, Peter Steinberger) — pre/post/error/permission hooks, convergence evidence
- Ouroboros (MIT, Anton Razzhigaev) — event bus, DPC's origin codebase

All four independently converge on the same pattern: an interceptor layer between intent and execution. Patterns analyzed, code not copied — independent implementation.

---

## Decision

We will implement a **per-process middleware chain** with two tiers:

1. **Observer hooks** — lifecycle event callbacks (logging, metrics, notification). Never fail pipeline. Errors are caught and logged silently.
2. **Guard middleware** — policy enforcement (SKIP, STOP). Can abort pipeline. Errors propagate.

### Design decisions (research-validated)

| Decision | Choice | Reason | Source |
|----------|--------|--------|--------|
| Ordering | Registration order, no priority field | Convergence of 3+ projects | Framework + DeerFlow |
| Chaining | First guard returning STOP_LOOP wins | Simple, deterministic | DeerFlow (implicit) |
| Error handling | Mixed: observer=silent catch, guard=re-raise | Different severity levels | Framework + DPC analysis |
| HookContext | Immutable identity + typed state dataclass | Prevents generic dict from becoming unreadable global bag | Framework + DeerFlow + Hope/J.A.R.V.I.S. review |
| Lifecycle | Per-process (new registry per `agent.process()`) | All 5 guards are per-task scope | DPC analysis |
| Hard stop mechanism | Explicit `HookAction.STOP_LOOP` signal, not message mutation | Signal visible at code-review, mutation isn't | Ark decision, S24 |
| Lifecycle type safety | `HookLifecycle` enum, not raw strings | Prevents silent no-op on typos | CC review S1 |
| Extension points | REDIRECT/RETRY = Phase 2 concern, not "never needed" | Current 2-value enum sufficient, but checkpoint semantics will differ in Phase 1+ | Hope review |

---

## Design

### HookAction enum

```python
from enum import Enum, auto

class HookAction(Enum):
    CONTINUE = auto()   # proceed normally
    STOP_LOOP = auto()  # terminate the agent loop
```

Two values only for Phase 0. Extension point: REDIRECT (change next action) and RETRY (re-execute current step) are anticipated for Phase 2+ hooks (e.g., skill recommendation, evolution proposal modification). These require different checkpoint semantics and are explicitly deferred, not excluded.

### HookLifecycle enum

```python
class HookLifecycle(Enum):
    BEFORE_LLM_CALL = "before_llm_call"
    AFTER_LLM_CALL = "after_llm_call"
    BEFORE_TOOL_EXEC = "before_tool_exec"
    AFTER_TOOL_EXEC = "after_tool_exec"
    BETWEEN_ROUNDS = "between_rounds"
```

Type-safe lifecycle identifiers. Prevents silent no-op on typos (e.g., `"before_llm_cal"`).

### Marker classes for error handling tiers

```python
class BaseMiddleware:
    """Base class for all middleware. Override lifecycle methods as needed."""

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
    """Marker subclass: errors propagate, never silent catch."""
    pass


class ObserverMiddleware(BaseMiddleware):
    """Marker subclass: errors are caught and logged."""
    pass
```

`_is_guard(mw) = isinstance(mw, GuardMiddleware)`. Clean, no magic, extensible.

### HookContext

```python
from dataclasses import dataclass, field

@dataclass
class LoopState:
    """Typed state exposed by loop.py to middleware.
    
    Loop OWNS these fields. Middleware READS them.
    Mutation contract: loop updates state BEFORE calling fire(),
    middleware only reads. If middleware needs mutable data,
    it stores that in its own instance attributes.
    """
    last_response_has_text: bool = False
    tool_calls_this_turn: int = 0
    consecutive_tool_only_rounds: int = 0
    accumulated_cost_usd: float = 0.0
    recent_tool_args: list = field(default_factory=list)


@dataclass
class HookContext:
    # Immutable identity (set once per process)
    agent_id: str
    task_id: str
    session_id: str
    round_idx: int

    # Typed state (loop exposes, middleware reads — see mutation contract above)
    state: LoopState = field(default_factory=LoopState)

    # Convenience accessors
    @property
    def last_response_has_text(self) -> bool:
        return self.state.last_response_has_text

    @property
    def tool_calls_this_turn(self) -> int:
        return self.state.tool_calls_this_turn
```

**Design rationale:** `LoopState` dataclass replaces generic `dict`. Each field is explicit, typed, and documented with ownership semantics. This prevents the "generic bag" anti-pattern where unknown keys accumulate over time (Hope, J.A.R.V.I.S. review). If Phase 1+ needs additional state, it must be added to `LoopState` explicitly — not stashed as arbitrary dict keys.

**Mutation contract:** Loop updates `ctx.state` fields BEFORE calling `hooks.fire()`. Middleware only reads `ctx.state`. If middleware needs mutable counters (e.g., `ResearchLimitMiddleware._counter`), it stores them in its own instance attributes, not in HookContext.

State that guards need for decisions (like `consecutive_tool_only`) lives in **middleware instance attributes**, not in HookContext. This avoids leaky abstraction — each middleware owns its own state.

### HookRegistry

```python
class HookRegistry:
    def __init__(self):
        self._middleware: list[BaseMiddleware] = []

    def register(self, mw: BaseMiddleware) -> None:
        self._middleware.append(mw)

    async def fire(self, lifecycle: HookLifecycle, ctx: HookContext) -> Optional[HookAction]:
        method_name = lifecycle.value
        for mw in self._middleware:
            handler = getattr(mw, method_name, None)
            if handler is None:
                continue
            try:
                result = await handler(ctx)
                if result == HookAction.STOP_LOOP:
                    return result
            except Exception as e:
                if isinstance(mw, GuardMiddleware):
                    raise  # propagate guard errors
                else:
                    log.debug(f"Observer hook error in {mw.__class__.__name__}.{method_name}: {e}")
        return None
```

### between_rounds semantics

`between_rounds` fires **after** `after_tool_exec` of the last tool in current round and **before** `before_llm_call` of next round. Used by guards that need to inspect the round transition:
- ROUND_LIMIT checks cumulative count
- LOOP_GUARD checks sliding window of recent tool args
- RESEARCH_LIMIT checks consecutive tool-only rounds

**Mutation contract:** Before `hooks.fire(HookLifecycle.BETWEEN_ROUNDS, ctx)`, loop.py MUST update all relevant `ctx.state` fields. This is an explicit invariant — if state is stale at fire time, guard decisions will be incorrect.

### Example: ResearchLimitMiddleware

```python
class ResearchLimitMiddleware(GuardMiddleware):
    def __init__(self, max_consecutive: int = 15):
        self._max = max_consecutive
        self._counter = 0

    async def after_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        # Reset on text response, increment on tool-only response
        if ctx.last_response_has_text:
            self._counter = 0
        else:
            self._counter += 1
        if self._counter >= self._max:
            return HookAction.STOP_LOOP
        return None
```

State (`_counter`) lives in the middleware instance. Reset logic is self-contained. No leaky context pollution.

### Integration in loop.py

```python
class DpcAgent:
    async def process(self, ...):
        # Per-process registry — no shared state, no locks needed
        hooks = HookRegistry()
        hooks.register(RoundLimitMiddleware(max_rounds=200))
        hooks.register(ToolLimitMiddleware(max_per_turn=25))
        hooks.register(LoopGuardMiddleware(window=20, threshold=5))
        hooks.register(ResearchLimitMiddleware(max_consecutive=15))
        hooks.register(BudgetLimitMiddleware(max_fraction=0.5))

        ctx = HookContext(
            agent_id=self.agent_id,
            task_id=task.id,
            session_id=self.session_id,
            round_idx=0,
            state=LoopState(),
        )

        while ...:
            ctx.round_idx += 1
            # Mutation contract: update state BEFORE fire()
            ctx.state.last_response_has_text = has_text
            ctx.state.tool_calls_this_turn = len(tool_calls)

            action = await hooks.fire(HookLifecycle.BETWEEN_ROUNDS, ctx)
            if action == HookAction.STOP_LOOP:
                break
            ...
```

---

## Migration Plan

1. **Create `hooks.py`** — HookRegistry, HookAction, HookLifecycle, BaseMiddleware, GuardMiddleware, ObserverMiddleware, HookContext, LoopState (~270 lines)
2. **Extract 5 guards** into middleware classes in separate file or inline — each guard becomes a GuardMiddleware subclass with its own state (~150 lines)
3. **Modify `loop.py`** — instantiate HookRegistry per process, replace inline if/elif blocks with `hooks.fire()` calls (~150 lines modified)
4. **Verify with integration tests:**
   - 4a. Write 5 integration tests, one per guard, exercising pre-refactor behavior with mocked LLM responses (~150 lines)
   - 4b. Run tests against current loop.py — verify they pass (sanity check)
   - 4c. Refactor loop.py per Steps 1-3
   - 4d. Re-run tests — must pass identically
   - 4e. Add tests to CI (recommended but not blocking)

---

## Scope Estimate

| Component | Lines |
|-----------|-------|
| hooks.py (registry, enums, base classes, context, LoopState) | ~270 |
| Guard middleware implementations (5 guards) | ~150 |
| loop.py modifications | ~150 |
| events.py (new event types) | ~60 |
| Integration tests (5 tests, one per guard) | ~150 |
| **Total** | **~880** |

Note: DeerFlow's analogous refactoring resulted in ~1400 lines including extensive test coverage. Our estimate is conservative for Phase 0 (5 guards only) and may grow with Phase 1+ hooks.

---

## Open Questions

1. **Cross-session state for Phase 1+** — future hooks (skill recommendation cache, evolution metrics) will need state that persists across `process()` calls. Use asyncio.Lock when needed, not now.
2. **HookContext state enrichment** — which additional loop internals to expose in `LoopState`. Start minimal (`last_response_has_text`, `tool_calls_this_turn`, `consecutive_tool_only_rounds`, `accumulated_cost_usd`, `recent_tool_args`), expand per Phase 1-5 needs.
3. **HookAbort semantics per checkpoint** — for current 5 guards, all checkpoints are pre-execution (revert is trivial). Phase 1+ hooks like `on_message_send` will need different error semantics (revert impossible after send). This is a Phase 1+ design decision.
4. **Finally/cleanup semantics** — current guards have no side effects (no DB connections, no locks). When Phase 1+ introduces hooks with resources, explicit cleanup mechanism needed (try/finally in handler, or cleanup hook). Deferred.

---

## Consequences

**Positive:**
- Adding new behavior (skill recommendation, truncation auto-suggest, evolution verification) no longer requires modifying loop.py
- Guards are independently testable
- Clean separation: loop orchestration vs policy enforcement
- Observability: every hook fire is an event
- Typed LoopState prevents undocumented state accumulation

**Negative:**
- Refactoring critical path (loop.py) — regression risk
- New abstraction layer adds indirection for code readers
- Per-process instantiation means no built-in cross-session memory in middleware

---

## External Review Notes

**Hope** (2026-04-10):
- Identified `state: dict` as future bug → addressed with typed `LoopState` dataclass
- Recommended extension point note for REDIRECT/RETRY → added to Decision table
- Flagged `between_rounds` mutation contract as implicit → made explicit with assertion
- Recommended formal integration tests → incorporated into Migration Step 4
- Noted ~700 lines optimistic → revised to ~880, cited DeerFlow's ~1400 as data point

**J.A.R.V.I.S.** (2026-04-10):
- Independently identified `state: dict` ownership concern → same fix (LoopState)
- Flagged HookAbort semantics differ per checkpoint → documented as Open Question #3
- Asked about finally/cleanup for side-effect hooks → documented as Open Question #4
- Assessed architecture as mature, concerns are future-facing

---

## References

| # | Source | License | What we used |
|---|--------|---------|--------------|
| 1 | `enumerated-strolling-seahorse.md` | — | Phase 0-5 roadmap with dependency graph |
| 2 | `dpc-agent-deer-flow-analysis.md` | MIT (Bytedance Ltd) | Middleware chain patterns, state management |
| 3 | `dpc-agent-claw-code-harness-analysis.md` | MIT (Peter Steinberger) | Hooks convergence evidence |
| 4 | `agent-ouroboros-gap-analysis.md` | MIT (Anton Razzhigaev) | Event system, DPC origin |
| 5 | `Framework/hooks.py` + `Framework/middleware.py` | Apache 2.0 | Two-tier hook/middleware split |
| 6 | S24 session transcript | — | Root problem analysis, research, ADR drafting |

All projects researched for architectural patterns only. No code copied — independent implementation.
