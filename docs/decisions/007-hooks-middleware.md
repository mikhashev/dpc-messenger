# ADR-007: Hooks & Middleware Infrastructure (Phase 0)

**Status:** Proposed
**Date:** 2026-04-10
**Authors:** Ark (design), CC (research)
**Scope:** Phase 0 — Agent Maturity Track enabler

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
| HookContext | Immutable identity + mutable state dict + per-instance state | No leaky abstraction | Framework + DeerFlow |
| Lifecycle | Per-process (new registry per `agent.process()`) | All 5 guards are per-task scope | DPC analysis |
| Hard stop mechanism | Explicit `HookAction.STOP_LOOP` signal, not message mutation | Signal visible at code-review, mutation isn't | Ark decision, S24 |
| Lifecycle type safety | `HookLifecycle` enum, not raw strings | Prevents silent no-op on typos | CC review S1 |

---

## Design

### HookAction enum

```python
from enum import Enum, auto

class HookAction(Enum):
    CONTINUE = auto()   # proceed normally
    STOP_LOOP = auto()  # terminate the agent loop
```

Two values only. No SKIP_TOOL, no FORCE_FINISH — DeerFlow shows these can be expressed through STOP_LOOP + message modification at the call site.

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
@dataclass
class HookContext:
    # Immutable identity (set once per process)
    agent_id: str
    task_id: str
    session_id: str
    round_idx: int

    # Mutable state (loop exposes, middleware reads)
    state: dict  # generic bag for loop data

    # Convenience accessors
    @property
    def last_response_has_text(self) -> bool:
        """True if last LLM response contained text content (not just tool calls)."""
        return self.state.get("last_response_has_text", False)

    @property
    def tool_calls_this_turn(self) -> int:
        return self.state.get("tool_calls_this_turn", 0)
```

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
            state={},
        )

        while ...:
            ctx.round_idx += 1
            ctx.state["last_response_has_text"] = has_text
            ctx.state["tool_calls_this_turn"] = len(tool_calls)

            action = await hooks.fire(HookLifecycle.BETWEEN_ROUNDS, ctx)
            if action == HookAction.STOP_LOOP:
                break
            ...
```

---

## Migration Plan

1. **Create `hooks.py`** — HookRegistry, HookAction, HookLifecycle, BaseMiddleware, GuardMiddleware, ObserverMiddleware, HookContext (~250 lines)
2. **Extract 5 guards** into middleware classes in separate file or inline — each guard becomes a GuardMiddleware subclass with its own state (~150 lines)
3. **Modify `loop.py`** — instantiate HookRegistry per process, replace inline if/elif blocks with `hooks.fire()` calls (~150 lines modified)
4. **Verify** — run agent with low thresholds, confirm all 5 guards fire identically to pre-refactor behavior

---

## Scope Estimate

| Component | Lines |
|-----------|-------|
| hooks.py (registry, enums, base classes, context) | ~250 |
| Guard middleware implementations (5 guards) | ~150 |
| loop.py modifications | ~150 |
| events.py (new event types) | ~60 |
| Tests | ~200 |
| **Total** | **~700** |

---

## Open Questions

1. **Migration validation approach** — formal test fixtures vs manual low-threshold testing. DeerFlow uses the latter; DPC may benefit from both.
2. **HookContext state enrichment** — which loop internals to expose. Start minimal (round_idx, last_response_has_text, tool_calls_this_turn), expand per Phase 1-5 needs.
3. **Cross-session state for Phase 1+** — future hooks (skill recommendation cache, evolution metrics) will need state that persists across `process()` calls. Use asyncio.Lock when needed, not now.

---

## Consequences

**Positive:**
- Adding new behavior (skill recommendation, truncation auto-suggest, evolution verification) no longer requires modifying loop.py
- Guards are independently testable
- Clean separation: loop orchestration vs policy enforcement
- Observability: every hook fire is an event

**Negative:**
- Refactoring critical path (loop.py) — regression risk
- New abstraction layer adds indirection for code readers
- Per-process instantiation means no built-in cross-session memory in middleware

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
