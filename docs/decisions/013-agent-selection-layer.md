# ADR-013: Agent Selection Layer

**Status:** Proposed (S58, 2026-04-20)
**Date:** 2026-04-20
**Authors:** Ark (draft), CC (co-analyst), Mike (direction)
**Session:** S58 — diagnosis convergence and architectural decision
**Related:** [ADR-009 Knowledge Extraction Flows](009-knowledge-flows-redesign.md), [ADR-010 Agent Memory Architecture](010-agent-memory-architecture.md)
**Foundation:** C3 (Human Cognitive Bottleneck), C7 (Lifecycle Asymmetry), Memory Asymmetry, Observability Asymmetry

---

## Context

### The Producer-Without-Consumer Pattern

DPC agent subsystems generate output continuously but lack mechanisms to evaluate what survives. Three subsystems exhibit the same anti-pattern:

| Subsystem | Output | Growth | Consumer |
|---|---|---|---|
| Consciousness (`consciousness.py`) | Reflections every 60-300s | 20+ entries, many identical | None (write-only) |
| Evolution (`evolution.py`) | Proposals every 60 min | pending_changes.json, 0 of 40 applied | Mike (manual, bottleneck) |
| Decision extraction (`decision_proposals.py`) | Proposals per long response | 5 DRAFTs, 3 duplicates | Not wired (MEM-4.3 blocked) |

Plus two growing data stores without bounds or cleanup: `tools.jsonl` (4.7 MB), `events.jsonl` (4.3 MB).

### Seven Diagnoses, Zero Fixes

This gap has been identified independently at least 7 times across 6 weeks:

1. **S8** — Sleep Consolidation idea: "analyze archives while agent sleeps"
2. **S13** — "Consciousness generates 583 thoughts → 0 actions"
3. **S30** — "Variation without selection = cancer, not evolution"
4. **S32** — "Lifecycle asymmetry — accumulation without succession"
5. **S43 (mem0 research)** — "No feedback loop from rejection to extraction prompts"
6. **S43 (ASI-Evolve research)** — "Selection: DOES NOT EXIST. Critical gap."
7. **S58** — "Producer without consumer — 3 subsystems"

Sleep Consolidation Phase 3 (~460 lines) was designed as the fix but never implemented. The system accumulates; nothing prunes.

### ADR-009 Conflict

ADR-009 removed automatic extraction from the knowledge store, establishing "manual discipline via P18 ritual." MEM-4.2 (ADR-010 Component 3) reintroduced automatic extraction to a staging area (`decision_proposals.jsonl`), creating an architectural tension: auto-generation is permitted again, but without a selection mechanism to filter output.

### Foundation Constraints

Several constraints from the foundation document directly require a selection layer:

- **C7 (Lifecycle Asymmetry):** "Knowledge accumulation without succession, knowledge persists without a living curator" — the agent accumulates without pruning.
- **Memory Asymmetry:** "Agent does not forget naturally → Pruning, active recall, knowledge lifecycle" — pruning is an architectural requirement, not an optional feature.
- **Observability Asymmetry:** "Agent can read its own metrics; human cannot" — the agent is better positioned for routine filtering because it has data the human does not.
- **C3 (Human Cognitive Bottleneck):** "Knowledge automatically extracted by an LLM does not create learning. The human decides what to keep." — human sets direction and exercises veto, but routine filtering is not "deciding what to keep" in the C3 sense.

---

## Decision

### Principle: Agent = Routine Selector, Human = Direction + Veto

Selection is the agent's responsibility for routine operations (dedup, decay, relevance scoring). The human provides direction (what is valuable) and veto (reject incorrect decisions). This resolves the ADR-009 tension: auto-generation is permitted because a selection layer governs what survives.

### Selection = Deterministic Rules + Data, Not LLM Judgment

The agent does not "decide" what is valuable through subjective self-assessment (self-inflation risk, per Ark Lesson #4). Instead, the agent **applies rules derived from data**:

| Rule | Mechanism | Data source |
|---|---|---|
| Dedup | String/proposal-level similarity check | Existing entries |
| Decay | Access count over time | Knowledge access log |
| Rejection feedback | Pattern matching on past rejections | Extraction feedback log |
| Novelty threshold | Diff with previous entries | Consciousness log |

This is categorically different from "the LLM evaluates its own output" — the agent executes deterministic filters. Goodhart's Law is mitigated because metrics inform rules, not replace judgment.

### Three Layers of Governance

| Layer | Who decides | What |
|---|---|---|
| **Direction** | Human | What is valuable, where to grow, what to prioritize |
| **Routine filtering** | Agent (deterministic) | Dedup, decay, relevance scoring, access tracking |
| **Veto** | Human | "This is wrong, roll it back" |

Direction and veto are human territory. Routine filtering is agent territory. They do not conflict.

---

## Implementation Phases

### Phase 1: Data Collection (S1-S3, ~55 lines, immediate)

Three consumers (not producers) that begin measuring the system:

| Step | What | Where | Lines |
|---|---|---|---|
| **S1** | Knowledge access log — track which files Active Recall pulls into context | `active_recall.py` → `get_recall_block()` | ~15 |
| **S2** | Consciousness dedup guard — suppress entries identical to previous | `consciousness.py` → `_think()` | ~20 |
| **S3** | Extraction dedup + rejection log — filter duplicate proposals, log decisions | `decision_proposals.py` → `extract_decisions()` | ~20 |

Key design decisions:
- S2 uses existing `_reflection_signature()` mechanism, extended to all thought types (not just identity)
- S3 uses proposal-level checksum (`hash(frozenset(topics))`), not entry-level string matching, to catch duplicate proposal sets
- All new log files (`knowledge_access.jsonl`, `extraction_feedback.jsonl`) are session-scoped with rotation (cap at 100 entries) — no unbounded growth

### Phase 2: Closing Loops (S4-S5, ~70 lines, immediately with retroactive baseline)

| Step | What | Depends on |
|---|---|---|
| **S4** | Knowledge decay — unused files sink in Active Recall ranking, used files float | S1 data |
| **S5** | Extraction prompt adjustment — rejection patterns modify future extraction prompts | S3 data |

Key constraints for Phase 2:
- **Decay floor:** minimum access score of 0.1 (never zero) — prevents positive feedback loop where rarely-accessed knowledge disappears entirely
- **Grace period:** new knowledge files get 5 sessions before decay kicks in
- **Prompt adjustment:** additive instruction to extraction prompt based on aggregate rejection patterns, not per-rejection modification

### Phase 3: Full Feedback Loop (future, scope TBD)

Connects all subsystems through shared metrics:
- Consciousness suppression rate → evolution tuning parameters
- Evolution proposal quality → extraction trigger thresholds
- Knowledge access patterns → Active Recall priority weighting
- Session archive analysis → baseline metrics for new subsystems

This phase corresponds to Sleep Consolidation Phase 3 from S8 design. Scope and decomposition depend on Phase 1-2 data.

### Retroactive Baseline (Primary Data Source for Phase 2)

71+ session archives contain historical tool call data. An archive parser extracts knowledge access patterns (which files were read via `read_file`/`memory_search`), providing immediate baseline for S4 decay scoring. S4-S5 use both archive data and live `knowledge_access.jsonl` — archive data is the primary source until live collection accumulates sufficient volume.

---

## Consequences

### Positive

- **ADR-009 conflict resolved.** Auto-generation is architecturally permitted when selection layer is active. ADR-009's prohibition was on auto-generation *without selection*, not on auto-generation itself.
- **Consciousness noise eliminated.** Dedup guard stops 8 identical "identity stable" entries from becoming 1 entry + suppressed_count.
- **Extraction quality improves over time.** Rejection feedback loop creates the missing selection mechanism identified in S30/S43.
- **Agent autonomy bounded.** Agent handles routine filtering without human involvement, but cannot make subjective quality judgments — only deterministic operations on data.
- **Human cognitive load reduced.** Mike sees filtered output, not raw producer noise.

### Negative

- **New log files.** `knowledge_access.jsonl` and `extraction_feedback.jsonl` add data to disk. Mitigated by rotation (cap at 100 entries).
- **Phase 2 uses dual data sources.** Retroactive baseline from 71+ session archives provides immediate patterns; live S1-S3 data supplements over time. Archive format differs from live format (raw tool calls vs structured JSONL), requiring a parser for each source.
- **Deterministic rules may miss edge cases.** String similarity dedup won't catch semantic duplicates with different wording. Acceptable for Phase 1 — Phase 3 embedding-based similarity can improve this.

---

## What This Does Not Address

- **Human veto UI.** A mechanism for Mike to reject agent filtering decisions is not part of this ADR. Current workflow (Mike reads proposals at session close, tells Ark to keep/discard) suffices.
- **Evolution selection.** Evolution proposals (pending_changes.json) are governed by Mike approval. Adding deterministic filtering to evolution is a future extension of the same principle.
- **Cross-agent selection.** Each agent applies selection to its own subsystems. No coordination mechanism between agents.
- **Archive cleanup.** 71 session archives grow without bounds. Selection layer does not address this directly — Sleep Consolidation Phase 3 addresses archive analysis.
- **L3 (CC memory).** CC's auto-memory operates independently. Not in scope.

---

## Alternatives Considered

1. **Kill producers (disable subsystems).** Eliminates noise but also eliminates variation. System becomes static, not self-reflective. Rejected: contradicts co-evolution vision.

2. **Agent self-review with subjective judgment.** Agent evaluates its own output quality. Rejected: self-inflation risk (Lesson #4) — without external feedback, agent rates itself too highly.

3. **Metrics-based targets.** Define quality scores and optimize toward them. Rejected: Goodhart's Law — "when a measure becomes a target, it ceases to be a good measure." Metrics inform rules but do not replace judgment.

4. **Batch consolidation only (Sleep Consolidation P3 as-is).** Periodic analysis of accumulated data. Rejected: shifts the problem (creates a fourth producer) rather than adding selection to existing producers.

5. **Human gate for everything.** Mike manually reviews all output from all subsystems. Rejected: does not scale (C3 Human Cognitive Bottleneck). Direction + veto is sufficient; routine filtering is agent territory.

---

## References

- ADR-009: Knowledge Extraction Flows Redesign (ADR-009 conflict source)
- ADR-010: Agent Memory Architecture (MEM track, extraction pipeline)
- Foundation constraints: `ideas/karpathy-gist/mike-gist-posts/foundation-constraints-review-draft.md`
- Sleep Consolidation Plan: `ideas/sleep-consolidation-plan.md`
- mem0 research: `ideas/cc-research-plan-2026-04-17-mem0.md` (15 findings, Finding 12 = rejection feedback)
- ASI-Evolve analysis: `ideas/cc-mike-research/asi-evolve-analysis-v2.md` (biological model, selection gap)
- S30 knowledge sync: `ideas/s30-knowledge-sync/cc-memory-s30.md` ("variation without selection = cancer")
- DPC full picture: `ideas/dpc-full-picture/dpc-full-picture-s32.md` (lifecycle asymmetry)
