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

### Pipeline: Per-Session Processing via llm_manager (S66 amendment)

Pipeline processes archives one session at a time — no truncation, full content analysis:

1. **Per-session pass (N calls):** For each unprocessed session, load full archive (no char limit), one `llm_manager.query()` call per session, write per-session finding to `sleep_results/` directory. Crash-safe — completed sessions are preserved if pipeline fails mid-cycle.
2. **Synthesis pass (1 call):** Read all per-session findings (~2KB each), one final LLM call to produce cross-session patterns, morning brief, and sleep findings.

Total: N+1 LLM calls per sleep cycle. Cost ~$0.10 for 5 sessions (vs $0.05 for single-call with truncation). No truncation = decisions and patterns captured fully.

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
   *(superseded 2026-11-10: see the amendment at the end; the brief rides once in the first turn's tail)*

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

## Amendment, 2026-11-10 — the morning brief rides once in the first turn's tail

The original text above stands, except the **Dual Injection (S66 amendment)** point and the
"injected into agent scratchpad" line under *Output*, which this amendment supersedes.

The brief was put into the semi-stable system block (Block 2, between Scratchpad and Identity) the
first time a turn read it, and marked consumed. The system prefix changed for that one turn and
changed back on the next: two cold prefills on a local engine, and after the first turn the model
no longer had the brief. [ADR-040](040-local-inference-second-path-as-a-dpc-owned-llama-server.md) (F)
makes a prefix-stable prompt within an agent the precondition for everything else, and nothing that
changes once belongs in front of the history.

1. **Where a 1:1 agent's brief goes.** The brief rides once, at the end of the `<turn_context>` tail
   of the first turn after sleep that reads it (ADR-040 F2). It is not in the system block, so the
   system block is the same bytes with a pending brief and without one.
2. **Dual Injection is superseded.** The brief no longer appears in the agent's scratchpad or Block
   2. Of the two places the S66 amendment named, the chat message stays: it is a history message,
   which the model reads on every turn of that session. The second place is the first turn's tail.
3. **It is replayed as part of that one message.** What the runtime records for replay in
   `sent_annotations` is the tail exactly as sent, the brief included, so a later turn replays the
   first message byte-for-byte ([ADR-040](040-local-inference-second-path-as-a-dpc-owned-llama-server.md)
   F2) and the model has the brief for the rest of the session. It is in that message once and is
   never appended to a later one. The cost is the brief's size, about 0.2-0.5K tokens of the window
   for the session (measured 2026-11-11 on the seven briefs in `~/.dpc/conversations/agent_*/`:
   the section as `_pending_morning_brief` renders it is 657-1611 characters, 160-400 tokens at
   chars/4 and 200-465 with Cyrillic counted at the x1.38 engine ratio measured here; the section
   is capped at five decisions and five unresolved items, so it cannot grow far past that), and no
   cold prefill after the first turn. The system block's note (`TURN_CONTEXT_NOTE`) tells the model
   where the brief is and that a turn with L5 closed replays it without. On a later turn with L5 closed
   ([ADR-048](048-memory-layer-audience.md): the brief is L5 memory) the replayed tail goes without
   the brief, cut by its heading (`strip_closed_layers`), at the cost of one cold prefill. A tail
   that carries the brief is recorded once the request has been answered, so a turn that never got
   an answer leaves no replayable brief for the next offer to double. This departs from
   [ADR-045](045-agent-context-is-a-projection-of-a-kept-transcript.md) decision 7, which keeps agent
   memory out of the replayed turn tail because every turn would add a copy: the brief is one-shot
   (point 5), so it adds one copy, once. The first version of this amendment cited decision 7 as its
   ground for not replaying; option A reverses that, and the reference is kept here for that reason.
   The turn tail is a 1:1 path only; in a group the brief lives only as the chat message (point 6).
4. **The L5 gate moves with it.** With L5 closed for the turn (ADR-048) the tail carries no brief
   and the brief is not marked consumed; the first turn that has L5 open gets it.
5. **One-shot means one placement.** The brief is marked consumed when the request that carries it
   has been sent and answered, not when the prompt was built: a pre-flight refusal, a guard, a
   provider refusal or a Kill before that leaves it unconsumed for the next turn. Or it is marked
   when a chat post has carried it into the history (the 1:1 agent's start-up check and the
   sleep-completion post of a 1:1 agent). It is never offered a second time by either route.
6. **The group path is unchanged.** A group sleep posts the brief to the group chat at sleep time and
   marks `morning_brief_group_<group>.json` consumed at once (`service.py`, `_run_group_sleep`); it
   never enters a prompt block or a turn tail, because `_pending_morning_brief` reads only the 1:1
   `conversations/<agent>/morning_brief.json`. In a group the brief exists only as that chat
   message, a history message like any other. The
   Telegram bridge only formats the brief.

Mike's call, 2026-11-10; consume point corrected 2026-11-11 after review; point 3 changed:
Mike's call, 2026-11-11 (option A).

## Amendment, 2026-11-11 — findings that do not fit one synthesis prompt are summarised by month

The original text stands, except that the **Synthesis pass (1 call)** reads every per-session
finding only when they fit. Per the reviewers' measurements on 2026-11-11 (not re-measured here),
a 1:1 agent had 469 findings of about 896 tokens each (about 420K) against a local-engine budget of
about 244 736 tokens, so about 42% of its sessions would have left the brief with the old trim;
a group brief had 165 findings of about 236K, 16 of them dropped. Findings per month are uneven
(one agent: 72 in April, 80 in May, 4 in June, 6 in July), so a fixed batch size would not do.
The oldest-first trim of `_fit_prompt_on_the_engine` stays only as the last resort inside the
reduce (point 5).

1. **When they fit, nothing changes.** If all findings are selected within the budget and, on a
   local engine, the whole prompt counted by the engine leaves the answer reserve free, the
   synthesis is one call over the raw findings, as before. The map-reduce starts only when they
   do not (`run_sleep`, `over_budget`).
2. **Map: one month summary per calendar month, from raw findings only.** Findings are grouped by
   the month of the raw session date (`digest_date`); a finding with no date goes to a group
   `undated`, which sorts first. Each group is condensed by one model call from that month's raw
   findings, never from other summaries, so nothing is compressed twice. A month whose findings
   exceed the **ceiling** is split into consecutive parts, each summarised by its own call. The
   ceiling is `MONTH_SUMMARY_CEILING_SHARE` (0.5) of the prompt limit less the map template: the
   limit is the one the synthesis already has (`_engine_prompt_limit`: window less the answer
   reserve, on an engine; `_compute_synthesis_budget` with no template, on the estimate path).
   Half, so one call's prefill stays well inside `SYNTHESIS_TIMEOUT_SECONDS` and a part never
   fills the window. The map calls carry the synthesis's own answer room and effort
   (`_synthesis_request_limits`). Tokens are counted by the engine where there is one.
3. **Cache.** A month summary is written beside the per-session findings:
   `sleep_results/month_summary_<scope>_<YYYY-MM>_<hash16>.json`, `scope` being `all` or
   `group-<group_id>` so the agent's own sleep and a group sleep, which see different sets of
   sessions in the same directory, never prune each other's files. The key is a sha256 over the
   map prompt template and exactly the successful findings of the part, so a finding added,
   re-analysed or removed, or a changed template, makes a new key and the old file is deleted at
   the end of the run (files of the same scope only). **A month with a session whose analysis
   errored is not frozen:** it is summarised each night from its successful findings and nothing
   is written, so tomorrow's retry of the errored session is not locked out by an old summary.
   A failed call writes nothing.
4. **Reduce: the synthesis reads the month summaries in date order.** The prompt is
   `SYNTHESIS_PROMPT` with its input renamed (`SYNTHESIS_FROM_MONTH_SUMMARIES_PROMPT`, derived by
   replacement, a test pins every replacement). The newest finding is still put in verbatim in
   the MOST RECENT SESSION block.
5. **Dates and counts are set by code.** `period`, `sessions_analyzed`, `last_session.date`, the
   oldest included date and every count come from raw session dates and raw findings. A summary's
   header (`Month 2026-09 (2 sessions, 2026-09-02 to 2026-09-20)`) is written by code; the
   model's own `month`, `sessions` and `period` keys in a summary are dropped, and the brief's
   `period`, `sessions_analyzed` and `last_session.date` overwrite whatever the synthesis wrote.
   If the summaries together still exceed the prompt limit, the oldest summaries leave first
   (`summaries_left_out`).
6. **A failed month-summary call does not drop the month.** The part falls back to as many of its
   raw findings as fit in what the summaries leave, newest first. Nothing is cached for it. The
   brief says so: `synthesis_budget` keeps `total_findings`, `included_findings`,
   `oldest_included_date`, `budget_tokens_used` and `budget_tokens_available` (now counting the
   findings behind the summaries as included) and gains `month_summaries`: `ceiling_tokens`,
   `parts`, `built`, `cached`, `failed`, `summaries_left_out`, one record per month (`findings`,
   `parts`, `built`, `cached`, `failed`, `cache` frozen or not, `fallback_findings`,
   `findings_in_prompt`) and `failures` (month, part, error).
7. **Cost.** The first night over budget makes one call per month part; later nights make one per
   changed month, plus one per month with an errored session. Each call has the synthesis timeout,
   and the pipeline's `SLEEP_TIMEOUT_MINUTES` window is one lock for all of them, as it already is
   for the per-session calls: a first night with many months can approach it.

Previous briefs are still not input to the next one. Mike's call, 2026-11-11, on the reviewers'
consensus (the Zcode and Ark conditions are points 3 and 5, and the split in point 2).
