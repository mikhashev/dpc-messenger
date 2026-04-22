# ADR-014: Sleep Consolidation Architecture

**Status:** Accepted
**Date:** 2026-04-23
**Authors:** Ark (analysis), CC (code audit), Mike (direction + key constraints)
**Session:** S66
**Related:** [ADR-007 Hooks/Middleware](007-hooks-middleware.md), [ADR-013 Selection Layer](013-agent-selection-layer.md)
**Replaces:** Consciousness background worker (disabled S65), partially replaces Evolution trigger

---

## Context

Consciousness background worker disabled (S65) — extended thinking already provides reactive analysis during conversation. Evolution disabled pending real data. Agent has no mechanism for inter-session learning or retrospective analysis. 81 session archives and 53 digest entries sit unused between sessions.

Mike's original insight (S8): instead of continuous low-value background ticks, use idle time for deep analysis of session archives. Like human sleep — consolidate experiences into long-term understanding.

## Decision

### Trigger: UI Button (Sleep/Wakeup Toggle)

User-initiated, not automated. Toggle button in sidebar following the Markdown/Text pattern (`SessionControls.svelte`). Two states:

- **Awake** (default): chat works normally, button shows "Sleep"
- **Sleeping**: chat locked (textarea disabled), pipeline runs in background, button shows "Wakeup"

**Guard:** Sleep button disabled when chat has active session (messages > 0). User must End Session first.

### Pipeline: Python-side via llm_manager

Sleep pipeline is a single LLM call, not a multi-round agent conversation. Uses `llm_manager.execute_query()` directly — already has retry and provider routing. Agent loop (process_message) is overkill for input→output transformation.

Hooks/middleware (ADR-007) not applicable — they operate inside agent loop between LLM rounds. Sleep is a separate process.

### Two-Level Input

1. **digest.jsonl** (DONE, 53 entries since S8): deterministic session metadata — message count, tool stats, duration, participants. Acts as index for selecting which archives to analyze.
2. **Full session archives**: Sleep pipeline reads complete archive JSONs for sessions not yet analyzed. LLM performs retrospective: decisions, patterns, unresolved items, recurring errors.

### Output

- `morning_brief.json`: injected into agent scratchpad AND posted as first chat message after auto-wakeup.
- `sleep_findings.json`: structured findings for Evolution feed (when re-enabled).
- `last_sleep.json`: timestamp tracking for unprocessed archive detection.

### Auto-Wakeup (S66 amendment)

Pipeline completion triggers automatic wakeup — no manual button press needed. Flow: Sleep button → pipeline runs → completes → auto-wakeup → morning brief posted to chat + injected in scratchpad → chat unlocked. Mike opens app next morning and sees "While you were sleeping..." as first message. Manual Wakeup button exists only as override if pipeline hangs.

### Morning Brief: Dual Injection (S66 amendment)

Morning brief appears in TWO places:
1. **Chat message**: posted as agent assistant message in history.json — visible to Mike in UI
2. **Scratchpad**: injected into Block 2 of agent context — available to Ark for reasoning

### No Telegram Notification (S66 amendment)

Telegram notification for sleep completion deferred — not needed for v1.

## Implementation Order

1. `sleep_pipeline.py` + `run_sleep` WebSocket handler + `sleep_state.json` — backend only, test via WebSocket manually
2. UI toggle button in `SessionControls.svelte` — visual layer
3. Morning brief injection in `context.py` — wiring to agent startup

## What Already Exists

- Task 1 (Session Digest): DONE — `conversation_monitor.py:1548`, hooked into `archive_session()`
- Task 4 partial (Evolution Feed): `evolution.py:411` already reads `digest.jsonl`
- `consolidation.py`: 85 lines, tier1 stale marking — reusable
- `archive.py`: read/search tools — reusable for archive access

## What Does Not Exist

- `sleep_pipeline.py`
- `morning_brief.json` / `sleep_findings.json` / `last_sleep.json`
- `run_sleep` WebSocket command
- UI Sleep/Wakeup toggle
- Morning brief injection in context builder

## Open Questions (Deferred)

- How many archives per sleep cycle? All unprocessed since last sleep, or capped at N?
- Retrospective prompt design — what specific questions to ask the LLM
- Notification on sleep completion (Telegram?)
- Consciousness cleanup (Task 6) — separate from Sleep, can happen independently

## Consequences

- Agent gains inter-session learning capability without continuous background cost
- User controls when analysis happens (DDA-compatible, no autonomous behavior)
- Evolution gets real data when re-enabled instead of vacuum analysis
- Morning brief provides continuity between sessions
