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

Replace the fixed prefix truncation in `compact_tool_history` with **structured LLM
summarization on a per-agent, cheap-tier model**, keeping truncation only as the last rung of
a graduated fallback ladder. Trigger on a token-proportion threshold (with hysteresis) instead
of a round count. The summarization call has a **hard timeout (default 10s)**; exceeding it
counts as a failure and drops to the next rung of the fallback ladder, so a slow or hung
summarizer never stalls the agent. Scope is the agent loop only; the human chat (hard-block
at 100%) is not touched.

### Rationale

- Mid-loop compaction is not anomalous: Gemini's agent executor (`local-executor.ts`) compresses
  at the start of every turn inside its tool-loop, and Goose (`agents/agent.rs`) performs reactive
  overflow-recovery compaction inside the loop. The industry pattern (LLM summary + verbatim tail)
  applies directly to our mid-loop case; the gap is only *how* we compact, not *when*.
- Structured summarization preserves the meaning of old tool results (what was found) rather than
  a byte prefix, at lower token cost than the raw result.
- Per-agent auxiliary-model configuration already exists for snapshot summarization and sleep
  consolidation and is already exposed in the **Agent Models Configuration** UI dialog; compaction
  becomes a third field of the same kind rather than a new subsystem.
- The default snapshot summarizer already runs on the cheaper `deepseek-flash` tier, so putting
  compaction there keeps the recursive spend bounded and non-toxic.

## Considered Options

- **Option A — Keep prefix truncation (status quo).** Deterministic, zero cost, zero latency,
  but loses meaning blindly. Rejected as primary; retained as the last fallback rung.
- **Option B — Structured LLM summary + verbatim tail, per-agent cheap model (chosen).** Matches
  13/14 surveyed agents; preserves meaning; reuses existing plumbing.
- **Option C — Bridge compaction into the memory layer** (promote summaries into knowledge/scratch).
  Rejected: pollutes the signed, gated, consensus memory with weak-model auto-summaries. Summaries
  stay ephemeral (session archive only).

## Consequences

- **Positive:** agents stop forgetting the middle of long tasks → fewer re-reads, fewer decisions
  on incomplete data; compaction becomes context-window-aware instead of round-count-blind. The
  hysteresis deadband caps compaction *frequency*, so it doubles as a **cost-containment
  mechanism**, not only anti-thrashing — without it a thrashing loop could multiply flash spend
  ~10×.
- **Negative:** adds an LLM call (cost + latency) into the agent's critical path; introduces
  non-determinism into compaction. Both are bounded by cheap-tier model, timeout, and hysteresis.
- **Neutral:** a new per-agent config surface (one dropdown + one number) in an existing dialog.
- **Budget flow:** compaction on a *remote* provider already flows through the existing
  `BudgetLimitGuard` (`budget.py`) and counts against the session budget; a *local* provider
  bypasses the budget entirely, so the circuit breaker is the only bound there. Compaction is
  auxiliary spend, not the agent's primary work, so it should be **logged as a distinct metric**
  for visibility (see Scope).

Two groups of gates must pass before implementation is accepted.

**Bench gates** (measured on a real ~20-round tool history → summarizer on the flash tier):

- [ ] **Latency** per compaction call is within budget on the cheap-tier model (target: within the
      call timeout, default 10s).
- [ ] **Cost** per compaction is non-toxic (estimated ~\$0.004/call, ~\$0.02 per long task on flash;
      to be confirmed against `pricing.py`).
- [ ] **Fidelity:** the flash-tier model produces usable structured summaries of tool history
      (not just browser snapshots) — checked by inspection.

**Correctness gates** (verified in code / behaviour):

- [ ] Threshold denominator is the provider request limit, not the theoretical model window
      (see Open Questions Q1).
- [ ] No compaction thrash: hysteresis deadband prevents repeated compaction between 0.6–0.8 usage.

## Scope

- `dpc_agent/context.py` — `compact_tool_history`: LLM summary via the per-agent compaction
  provider, structured template, graduated fallback ladder ending in the existing prefix truncation.
- `dpc_agent/loop.py` — replace the round-count trigger with a token-proportion threshold read from
  the session token limit, with hysteresis (compact at 0.8, target ≤ 0.6). Also wire
  **overflow-recovery**: a distinct reactive mechanism where a provider "context window exceeded"
  error triggers a compaction and a retry of the same request — the safety net when the proactive
  threshold missed (e.g. an unexpectedly large tool result in one round).
- `agent_service.py` — `set_agent_models` / `get_agent_models`: add `compaction_provider` and
  `compaction_threshold` keys, mirroring the existing `snapshot_summarize_*` keys.
- `dpc-client/ui/src/lib/components/Sidebar.svelte` — Agent Models Configuration dialog: add a
  "Compaction LLM" dropdown and a "Compaction threshold" input, mirroring the Snapshot fields.
- Session stats / `budget.py` — log compaction token spend as a distinct `compaction_tokens_spent`
  metric so auxiliary compaction cost is visible separately from the agent's primary work (no new
  guard needed: remote compaction already flows through `BudgetLimitGuard`).

**Summary template (single, shared across agents):** `Goal / Progress / Decisions / Next / Files /
Findings / Errors`. Tool-result handling is heterogeneous: `read_file` results summarize to the
finding (not a prefix); `grep`/`search_files` lists are kept as-is (already compact); `browse_page`
is not double-summarized (it already passes through snapshot summarization).

**Fallback ladder:** LLM summary → `keep_recent = 12` → `keep_recent = 18` → prefix truncation;
consecutive-failure circuit breaker (max 3).

## Open Questions

- **Q1:** Is the session token limit (`session_state["tokens_limit"]`, consumed by
  `apply_message_token_soft_cap`) populated from the provider's per-request cap or from the model's
  theoretical context window? If the latter, the threshold may never fire when the provider caps
  requests below the model window — this is a correctness gate, not a detail. — @CC to verify
- **Q2:** Default threshold value. Proposed 0.8 (matching Goose/Codex). — @Mike
- **Q3:** Reproducibility for the research track — should analysis/review agents prefer
  deterministic-but-lossy prefix truncation over a non-deterministic LLM summary, or use LLM
  summary with temperature 0 as a middle ground? (Note: temperature 0 reduces but does not
  guarantee determinism — GPU float-order and provider-side variation remain.) — @Ark

## Authors

- **Mike** — Decision, direction
- **CC** — Research (14-repo verification), analysis, draft
- **Ark** — Analysis (mid-loop question, template `Findings`/`Errors`, hysteresis, ephemerality)
- **Warren** — Analysis (cost model, threshold denominator, graduated degraded mode)

## References

- `ADR-010` (`010-agent-memory-architecture.md`) — the separate, gated memory layer this decision must not pollute
- `ADR-014` (`014-sleep-consolidation-architecture.md`) — cross-session consolidation (distinct from within-session compaction)
- `dpc_agent/tools/browser.py` — `_maybe_summarize_snapshot` / `_load_agent_summarize_config`: the auxiliary-model pattern this decision reuses
- Compaction survey of 14 open-source coding agents (2026-07-16), verified against source for Goose, Qwen Code, Hermes, Gemini CLI, Crush, Roo Code, Cline
- Original compaction research (the two source documents this ADR's survey verifies against) by [vakovalskii](https://github.com/vakovalskii)
