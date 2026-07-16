---
adr: 033
title: "Replace agent tool-history truncation with structured LLM compaction"
status: proposed
date: 2026-07-16
deciders: [Mike]
consulted: [CC, Ark, Warren]
informed: []
depends_on: []
related: [ADR-010, ADR-014]
supersedes: []
session: S40
---

> **Amended 2026-07-16 (post-bench).** The original draft's model choice, timeout,
> fallback design, and open questions were revised after a five-stage benchmark on real
> agent tool-history. Key reversals: the summarizer runs on a **thinking-disabled** model
> (reasoning, not input size, was the latency floor); there is **no fallback model** (one
> user-selected model, notify on failure); compaction is **incremental**; and the whole
> feature is behind a per-agent **toggle, default off**. See *Validation* below.

## Context and Problem Statement

The embedded agent loop (`dpc_agent`) compacts its own message history mid-tool-loop
so a long task does not overflow the model context window before it finishes. Today the
only mechanism is `compact_tool_history` in `dpc_agent/context.py`, invoked each round from
`dpc_agent/loop.py` once the round counter passes a fixed threshold. It keeps the last few
tool-call rounds verbatim and **truncates everything older to the first 200 characters**
(`content[:200]`), with no LLM involvement.

Truncation to a fixed prefix discards information blindly and irreversibly: the first 200
characters of a `read_file` result, a `grep` list, or a `browse_page` extract are rarely the
part the agent needs later. The agent then either re-reads the same file (extra rounds,
extra tokens) or reasons on incomplete data. This is the single point in the system where
data is lost blindly in the middle of a task — the memory layer (knowledge commits, sleep
consolidation) does not help because it is cross-session, whereas this loss is within-session.

A survey of 14 open-source coding agents (verified against their source) found that 13/14
use LLM summarization as the primary compaction strategy and keep prefix truncation only as
an emergency fallback. DPC has this relationship inverted: the fallback is the primary
mechanism.

## Decision Drivers

- **Correctness:** the agent must not lose the meaning of its own earlier tool results mid-task.
- **Cost:** the summarizer runs on a remote token-API (DeepSeek) by default, so a naive design
  can cause recursive token spend.
- **Latency:** compaction runs in the agent's critical path — the agent waits for it.
- **Reuse:** DPC already has per-agent auxiliary-model plumbing (snapshot summarization, sleep)
  wired through the UI; the fix should extend it, not build a parallel system.
- **Determinism / research track:** some agents (analysis, review) value reproducibility; an
  LLM summary is non-deterministic where truncation was deterministic-but-lossy.

## Decision

Behind a per-agent **`compaction_enabled` toggle (default off)**, replace the fixed prefix
truncation in `compact_tool_history` with **structured LLM summarization on a per-agent,
thinking-disabled model**. When the toggle is off the current round-count prefix truncation
is used unchanged, so the change is strictly opt-in and additive.

When enabled:

- The summarizer runs on a **single user-selected model with reasoning/thinking disabled**
  (default `deepseek-flash` with `thinking.enabled = false`). There is **no fallback model** —
  if the model errors or times out we degrade the *strategy*, not the model, and **notify the
  user**.
- Compaction is **incremental** — this is the primary, steady-state mechanism: each large tool
  result is summarized as it ages out of the verbatim `keep_recent` window, one small call at a
  time, never a single batch summary of the whole old history (which can exceed the model's own
  window; see *Validation* bench-2). Incrementally summarizing as results age keeps the context
  compact in normal operation.
- The **token-proportion threshold with hysteresis** (compact at `context_usage_percent ≥ 0.8`,
  release below `0.6`, replacing the round count) is the **safety net**: it catches usage spikes
  the incremental pass did not — e.g. one oversized result in a single round — rather than being a
  second path that fires simultaneously with the incremental one.
- The summarization call has a **hard timeout (default 180s)**; exceeding it counts as a
  failure and drops to the next rung of the *strategy* fallback ladder.

Scope is the agent loop only; the human chat (hard-block at 100%) is not touched.

### Rationale

- Mid-loop compaction is not anomalous: Gemini's agent executor (`local-executor.ts`) compresses
  at the start of every turn inside its tool-loop, and Goose (`agents/agent.rs`) performs reactive
  overflow-recovery compaction inside the loop. The industry pattern (LLM summary + verbatim tail)
  applies directly to our mid-loop case; the gap is only *how* we compact, not *when*.
- Structured summarization preserves the meaning of old tool results (what was found) rather than
  a byte prefix, and validation showed it preserves exact numbers/hashes/paths verbatim on real
  research data.
- **Thinking must be disabled.** Benchmarking showed the latency floor is the summarizer's own
  reasoning, not the input size: on identical inputs `deepseek-flash` with thinking on took 43–86s
  regardless of input, but 1.7–4.1s with thinking off. Compaction is a mechanical task; reasoning
  only adds a latency floor that blows any workable timeout.
- Per-agent auxiliary-model configuration already exists for snapshot summarization and sleep
  consolidation and is already exposed in the **Agent Models Configuration** UI dialog; compaction
  becomes a third field of the same kind rather than a new subsystem.

## Considered Options

- **Option A — Keep prefix truncation (status quo).** Deterministic, zero cost, zero latency,
  but loses meaning blindly. Retained as the toggle-off default and as the last rung of the
  strategy fallback ladder.
- **Option B — Structured LLM summary + verbatim tail, single thinking-disabled per-agent model
  (chosen).** Matches 13/14 surveyed agents; preserves meaning; reuses existing plumbing;
  measured at 1.7–4.1s/call.
- **Option C — Bridge compaction into the memory layer** (promote summaries into knowledge/scratch).
  Rejected: pollutes the signed, gated, consensus memory with weak-model auto-summaries. Summaries
  stay ephemeral (session archive only).
- **Option D — Fallback to a second model on failure** (e.g. flash → local lfm2.5). Rejected by
  Mike: one model per agent is one point of failure; a silent model switch hides problems. On
  failure we degrade strategy and notify the user, never switch model.

## Consequences

- **Positive:** agents stop forgetting the middle of long tasks → fewer re-reads, fewer decisions
  on incomplete data; compaction becomes context-window-aware instead of round-count-blind. The
  hysteresis deadband caps compaction *frequency*, so it doubles as a **cost-containment
  mechanism**. Opt-in toggle means zero behaviour change for agents that do not enable it.
- **Negative:** adds an LLM call (cost + latency) into the agent's critical path when enabled, and
  introduces non-determinism into compaction. Both are bounded by the thinking-disabled model
  (1.7–4.1s/call, ~$0.01), the timeout, and the hysteresis. With the toggle off, the pre-existing
  blind-truncation gap persists for that agent until the user opts in.
- **Neutral:** a new per-agent config surface (one toggle + one dropdown + one number) in an
  existing dialog.
- **Budget flow:** compaction on a *remote* provider already flows through the existing
  `BudgetLimitGuard` (`budget.py`) and counts against the session budget; a *local* provider
  bypasses the budget entirely, so the circuit breaker is the only bound there. Compaction is
  auxiliary spend, not the agent's primary work, so it should be **logged as a distinct metric**
  for visibility (see Scope).

## Validation

Five progressive benches on real agent tool-history (`logs/tools.jsonl` + archived autoresearch
session), summarizer on the flash tier. Each bench retired a wrong hypothesis before any code.

| Bench | Setup | Result |
|-------|-------|--------|
| 1 — single-shot | scout tool-history, ~62K-token old-history, flash thinking-on | latency 33s median (≫10s), cost $0.009, fidelity good on decision-relevant content |
| 2 — autoresearch single-shot | real research session, old-history = **1.4M tokens** | **exceeds the 1M model window** → single-shot compaction impossible → **incremental required** |
| 3 — incremental (thinking-on) | per-round, 12 calls | latency **53.8s median, uncorrelated with input size** → batching is not the latency lever; killed the "incremental = seconds" hypothesis |
| 4 — local model | lfm2.5 vs flash thinking-on | local 2–7s vs flash 43–86s → the model, not batching, is the latency lever |
| 5 — thinking off | flash thinking-off vs on vs local | **flash thinking-off = 1.7–4.1s**, faster than local, all under timeout; reasoning was 100% of the floor |

**Gates:**

- [x] **Latency** — 1.7–4.1s/call on flash thinking-off (bench-5), well within the timeout.
- [x] **Cost** — ~$0.01/call on flash (bench-1/2), non-toxic.
- [x] **Fidelity** — structured summary preserved exact commit hashes, metrics, formulas, paths,
      and error text verbatim on real research data (bench-2, spot-checked).
- [x] **No thrash** — hysteresis deadband (0.6–0.8) prevents repeated compaction.

Bench scripts: `docs/decisions/adr-033-benches/`.

## Scope

- `dpc_agent/context.py` — `compact_tool_history`: when `compaction_enabled`, LLM summary via the
  per-agent compaction provider (thinking disabled), incremental (summarize each large result as it
  ages past `keep_recent`), structured template; `grep`/`search` lists (< ~500 tokens) kept
  verbatim; `browse_page` not double-summarized when snapshot summarization already ran — but if
  snapshot summarization is disabled for the agent, `browse_page` results fall through to the
  general summary template (correct, just more token-costly). Strategy fallback ladder ending in the
  existing prefix truncation.
- `dpc_agent/loop.py` — replace the round-count trigger with a token-proportion threshold: the
  round's real prompt size (`last_prompt_tokens`, reported by the provider — it counts the **whole**
  prompt: system, assembled context/memory, tool schemas, and the growing message tail) over the
  agent's model window (resolved from the agent's provider when `config.json` leaves `context_window`
  null; per Q1), with hysteresis (compact at 0.8, release ≤ 0.6) and a **user notification** on any
  compaction failure. **Reactive overflow-recovery** — a provider "context window exceeded" error
  triggering a compaction + retry of the same request — is **deferred to backlog**
  (`ADR-033-REACTIVE-OVERFLOW`); today an overflow ends the task with a "start a new session" message.
- `agent_service.py` — `set_agent_models` / `get_agent_models`: add `compaction_enabled`,
  `compaction_provider`, and `compaction_threshold` keys, mirroring the existing
  `snapshot_summarize_*` keys.
- `dpc-client/ui/src/lib/components/Sidebar.svelte` — Agent Models Configuration dialog: add a
  "Compaction" enable toggle, a "Compaction LLM" dropdown, and a "Compaction threshold" input,
  mirroring the Snapshot fields.
- Session stats / `budget.py` — log compaction token spend as a distinct `compaction_tokens_spent`
  metric so auxiliary compaction cost is visible separately from the agent's primary work.

**Summary template (single, shared across agents):** `Goal / Progress / Decisions / Next / Files /
Findings / Errors`. Tool-result handling is heterogeneous: `read_file` results summarize to the
finding (not a prefix); `grep`/`search_files` lists are kept as-is; `browse_page` is not
double-summarized.

**Strategy fallback ladder (not a model switch):** LLM summary → `keep_recent = 12` →
`keep_recent = 18` → prefix truncation; consecutive-failure circuit breaker (max 3); user is
notified on failure. The model never changes — only the strategy degrades. Note the direction:
when LLM summarization fails, each ladder step preserves **more** verbatim context (12, then 18
rounds) as a data-integrity safeguard — accepting a higher risk of context overflow rather than
losing data — before finally falling back to lossy prefix truncation.

## Open Questions — resolved

- **Q1 (resolved, @CC):** the trigger denominator is the **agent's model context window**. In the
  compaction loop it is resolved in `loop.py` from the agent's provider (`get_context_window(model)`)
  when `config.json` leaves `context_window` null — local models do (fallback 204800); this mirrors
  `agent_manager._resolve_context_window()`. The numerator is the round's real prompt size
  (`last_prompt_tokens`), which the provider reports over the **full** prompt — system prompt,
  assembled context/memory, tool schemas, and the growing message tail — not just history text, so
  the threshold reflects true window pressure. The original gate's worry — that a provider
  request-cap below the model window could keep the threshold from ever firing — is **not yet
  covered**: the reactive overflow-recovery path that would catch it is deferred (backlog
  `ADR-033-REACTIVE-OVERFLOW`).
- **Q2 (resolved, @Mike):** default threshold **0.8**, exposed as a per-agent setting (not
  hardcoded), with release at 0.6.
- **Q3 (resolved, @Ark):** on real research data the structured summary preserved every critical
  number/hash/formula verbatim (bench-2), so the general/research split is dropped: a single
  summary path is used. Research agents that want strict determinism can disable compaction (toggle
  off → deterministic prefix truncation). No separate fidelity run on a fallback model is needed
  because there is no fallback model.

## Authors

- **Mike** — Decision, direction (no fallback model, opt-in toggle, threshold in settings)
- **CC** — Research (14-repo verification), five validation benches, analysis, draft
- **Ark** — Analysis (mid-loop question, template `Findings`/`Errors`, hysteresis, ephemerality,
  research-determinism review), ADR review
- **Warren** — Analysis (cost model, threshold denominator, graduated degraded mode)

## References

- `ADR-010` (`010-agent-memory-architecture.md`) — the separate, gated memory layer this decision must not pollute
- `ADR-014` (`014-sleep-consolidation-architecture.md`) — cross-session consolidation (distinct from within-session compaction)
- `dpc_agent/tools/browser.py` — `_maybe_summarize_snapshot` / `_load_agent_summarize_config`: the auxiliary-model pattern this decision reuses
- `docs/decisions/adr-033-benches/` — the five validation bench scripts
- Compaction survey of 14 open-source coding agents (2026-07-16), verified against source for Goose, Qwen Code, Hermes, Gemini CLI, Crush, Roo Code, Cline
- Original compaction research (the two source documents this ADR's survey verifies against) by [vakovalskii](https://github.com/vakovalskii)
