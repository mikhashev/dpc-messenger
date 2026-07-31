---
adr: 034
title: "Repair the Active Recall access layer, then adopt EvoLib's bookkeeping mechanisms"
status: proposed
date: 2026-07-31
deciders: [Mike]
consulted: [CC, Ark, Warren, Fable 5, GLM 5.2]
informed: []
depends_on: [ADR-010, ADR-013]
related: [ADR-024]
supersedes: []
session: S49
---

> **Rewritten 2026-07-31 after reading the runtime logs.** The first draft was written from
> code alone and got its own priorities wrong. Three rounds of review — two of them against
> the live logs of seven agents — showed that the layer this ADR proposed to build on has
> never had a working input. What follows is ordered by measured impact, not by how the
> mechanisms read on paper. The superseded reasoning is kept in *Revision history* rather
> than deleted, because unrecorded reversals come back.

## Context and Problem Statement

Microsoft Research published *Test-Time Learning with an Evolving Library*
([arXiv:2605.14477](https://arxiv.org/abs/2605.14477), v1 2026-05-14 / v2 2026-07-14;
[microsoft/EvoLib](https://github.com/microsoft/EvoLib), MIT) — a library of skills and
reflective insights that consolidates, weights, and evolves during inference. It is the
closest published formalization of DPC's knowledge system, and it prompted the question of
what to adopt.

Investigating that question surfaced a larger one. **DPC's Active Recall layer has been
ranking on its own output for 102 days.** The measurements below are from the live logs of
all seven agents (`~/.dpc/agents/*/state/knowledge_access.jsonl`, `*/logs/tools.jsonl`,
`*/knowledge/_meta.json`), read on 2026-07-31.

### What the data shows

**1. The advertised loop has never closed.** Active Recall prints
`call read_file("<source_file>")` where `source_file` is a *layer-relative index key*
(`EXT/…`, `L6/…`, or a bare agent-layer filename). `read_file` resolves relative paths
against the agent sandbox (`tools/core.py:51-63`), where `EXT/` and `L6/` do not exist and
agent-layer files live under `knowledge/`. Measured across all agents, all history:

| verbatim hint-follow attempts with a layer prefix | 28 |
|---|---|
| **successes** | **0** |

And every knowledge read that *did* succeed used an address the hint never prints:

| how successful knowledge reads were addressed | count |
|---|---|
| absolute path | 127 |
| `knowledge/` prefix | 87 |
| **the form the hint prints** | **0** |

**2. The access counter is therefore ~entirely self-generated.** Fleet-wide: **28 873
injections vs 166 genuine accesses (99.4% injection)**; per agent 86–99.9%. The top-ranked
key for the largest agent is `README.md` — injected 4 102 times, read 0 times.

**3. Decay has degenerated into a constant.** `decay_multiplier = max(0.1, access/max_count)`
normalizes against a counter keyed by **basename**, which pools files from every layer —
including project files (`README.md`, `backlog.md`, `protocol-13.md`) that are not knowledge
entries at all. Consequence, measured per agent: of the knowledge entries in `_meta.json`,
the share clearing `DECAY_FLOOR` is **0% on the two largest agents** (0/106 and 0/4).
Basename pooling also means a fresh file inherits the score of unrelated namesakes — the
ranking is not merely biased, it is not attributable to the document's own history.

**4. `_meta.json.access_count` counts writes, not reads.** `update_access()` has exactly one
production call site: `tools/core.py:194`, inside the *write* branch of `write_file`.
`read_file` never touches it. So `tier1_consolidate` — which does run, via the sleep
pipeline — has marked **40 files on agent_001 as `stale`** on a "not recently rewritten"
test, and `tier2_propose`'s archive gate (`access_count <= 1`) means "written at most once".

**5. `useful` was never populated:** 0 non-null out of 9 624 records.

**6. `tier2_propose()` has no production caller** — only `tests/test_cross_cutting.py`. The
merge half of consolidation does not exist; the docstring promises it.

The EvoLib question and the repair question are therefore not independent: every mechanism
worth adopting reads from this layer.

## Decision Drivers

- **Honest signals only.** A metric that cannot be computed must not ship under the name of
  the metric we wish we had.
- **Human arbiter is load-bearing** — VISION.md C1, P13 §11 (syntactic dedup autonomous,
  semantic merge gated).
- **Attribution survives consolidation** (P13 §12).
- **Privacy-first** — candidate detection must not ship knowledge content to a third party.
- **No GPU budget.** Everything here is CPU + local embeddings + LLM API.
- **Measured before ordered.** After this round, no item enters the plan on a code reading
  alone.

## Decision

**Repair the access layer first (T0), then add EvoLib's three bookkeeping shapes — merge
proposals, weight transfer, outcome-conditioned credit. Reject EvoLib's estimators.**

The hint modality stays (Mike, 2026-07-31: *«хинт-модальность нужна»*). The alternative —
injecting chunk text directly and dropping the read step — was considered and rejected; the
decision is to make hints work, not to replace them.

### T0 — repair the access layer

| # | Item | Why here |
|---|------|----------|
| **a** | **Make hints followable** — emit a path `read_file` accepts | Nothing downstream can work until the loop can close once |
| **b** | **Key the counter by layer-relative path, not basename** | Largest distortion; makes rank attributable to the file itself |
| **c** | **Injection ≠ access** — separate the two counters | 99.4% of today's signal |
| **d** | **`read_file` → `update_access`** | Fixes `stale` and the archive gate, which today test write-recency |
| **e** | **Honour `GRACE_PERIOD_SESSIONS`** — needs a `created` field, absent from `FileMeta` | Meaningful only after a–c; today newcomers are floored *together with* everyone else |
| **f** | **Rotate `knowledge_access.jsonl`** | It bypasses the shared rotating writer; injections accumulate forever while reads rotate away, biasing any comparison |
| **g** | **Deduplicate hint slots by source file** | ~25% of injections spend 2 of 3 slots on the same content |
| **h** | **Fix the telemetry line** | It logs the pre-decay candidate pool as "injected N hints"; without this, T0 cannot be verified from the log that exists to verify it |

### Then

- **T1** — merge action in `tier2_propose()`, **with score and attribution transfer in the
  same code path**, plus a delivery surface for the proposals (see Open Questions Q1).
- **T2** — growth telemetry, shipped as T0's verification instrument.
- **T3** — insight-from-error as a library-wide primitive.
- **T4** — fill `useful` as a **named heuristic**, re-ranking only.

### Rationale

**Why T0a is first and why it is not a one-line fix.** Both reviewers and the team's synthesis
called it one line. It is not. The index builds three key shapes, each reversing differently
(`agent_manager.py:345-425`):

| layer | key emitted | real location | what `read_file` needs |
|---|---|---|---|
| L5 (agent) | `foo.md` | `<sandbox>/knowledge/foo.md` | `knowledge/foo.md` — prefix |
| L6 (shared) | `L6/foo.md` | `$DPC_HOME/knowledge/foo.md` | **absolute** path, and extended-read must be enabled |
| EXT | `EXT/<rel>` | `<one of N indexed_paths>/<rel>` | **absolute**, and `<rel>` is relative to the *longest matching* indexed root — the key does not record which |

So L6 and EXT hints cannot be repaired by string surgery at hint time: the EXT reverse
mapping is ambiguous by construction, and both cross the firewall's extended-path gate.
**The fix belongs at index time** — the real `Path` is in hand at `agent_manager.py:346/370/398`;
store a resolvable address in the chunk metadata alongside the display key, and have
`format_recall_hints` print that. This also means a hint may be un-followable for a legitimate
reason (extended reads disabled for that agent) — which must be surfaced, not silently emitted.

**Why the estimators are rejected.** EvoLib's IG and Future IG are computed over
`k_q_per_problem = 3` parallel samples of the *same* problem — the "without this item" arm is
a sibling sample. DPC runs one trajectory per task; that arm does not exist and cannot be
reconstructed (comparing tasks *with* an entry against tasks *without* is confounded by
difficulty). Stochastic utility sampling and self-supervised scoring rest on the same batch
structure. The bookkeeping — credit records, weight carried across a merge, embed-then-merge
— has no such dependency.

**Why `useful` is last.** With a 0% hint-follow rate, `read ∧ success` would write `false` on
essentially every row today. Its first honest use is diagnostic (is recall delivering
anything?), not ranking fuel. It also needs a join key: `knowledge_access.jsonl` entries are
`{ts, mode, files, useful}` with no task identifier, while `tools.jsonl` already carries
`task_id` — one log needs the key, not two.

**Why merge and transfer ship together.** Merging without carrying access history forward
hands the merged entry an empty history and the floor at 0.1 — the merge buries its own
result. Transfer as a separate earlier step is a no-op: there is nothing to transfer until
merges exist. Post-T0 only: inheriting *today's* counts would port injection inflation into
the merged entry.

## Considered Options

- **A — port EvoLib wholesale**, estimators included.
- **B — repair first, then adopt the bookkeeping** *(chosen)*.
- **C — adopt consolidation only**, on cosine similarity, no utility signal.
- **D — add parallel sampling** so true IG becomes computable.
- **E — drop the hint modality**, inject chunk text directly *(rejected by Mike)*.

### Pros and Cons

**A** — Good: complete, internally consistent, published results. Bad: requires self-assigned
outcome scores (the autonomous self-evaluation loop C1 and P13 §11 exclude) and parallel
sampling DPC does not do.

**B** *(chosen)* — Good: every piece runs under a human gate; no GPU; each item independently
useful; the repairs are prerequisites for anything else regardless of EvoLib. Neutral: yields
a weaker signal than EvoLib's, and we should say so. Bad: T0 is eight items before any new
capability lands.

**C** — Good: cheapest. Bad: merges the similar rather than the useful — deduplication, not
evolution. Acceptable only because the human gate supplies the judgment the metric lacks.

**D** — Good: the only path to an honest IG. Bad: multiplies inference cost per task and needs
an automatic success measure, reintroducing A's governance problem.

**E** — Good: removes the failing step entirely; chunk text is already in the index; makes the
read counter and `useful` unnecessary. Bad: costs context budget on every turn whether or not
the content is wanted, and removes the agent's choice of what to open. **Rejected by Mike.**

## Consequences

- **Positive:** the inject→read→credit loop becomes completable for the first time; ranking
  becomes attributable to a document's own history; `stale` and the archive gate start testing
  what their names claim; the knowledge base gets a pressure-release valve other than
  archiving; attribution survives consolidation per P13 §12.
- **Negative:** T0 is eight items of unglamorous repair before any EvoLib-derived capability
  ships. Merge proposals add review load on Mike — the human gate is both the design and the
  bottleneck. After removing injections, the genuine signal is ~1 knowledge read per day
  across ~2 000 indexed files; that may not be enough to rank on, and the honest interim may
  be to let relevance rank alone until clean signal accumulates.
- **Neutral:** no EvoLib code is copied, so no `NOTICE` entry is required.

## Confirmation

Verification criteria. Data-integrity items are required (this ADR changes what the knowledge
layer records and how it is ranked).

- [ ] **A hint can be followed verbatim.** For each layer (L5, L6, EXT), a test issues
      `read_file("<exactly what the hint block printed>")` and receives content. Where the
      firewall legitimately denies it, the hint says so rather than printing an
      un-followable path.
- [ ] The access counter distinguishes injection from read; N consecutive injections with no
      read do not raise a file's decay multiplier, one real read does.
- [ ] Counter keys are layer-relative paths: two files sharing a basename across layers have
      independent counts (regression test with two `README.md`).
- [ ] `read_file` on a knowledge file increments `_meta.json.access_count`; a written-once,
      read-many file is not proposed for archive.
- [ ] A new entry is not pinned to `DECAY_FLOOR` during its grace period (requires `created`
      in `FileMeta`).
- [ ] `knowledge_access.jsonl` is bounded by the same rotation policy as other agent logs.
- [ ] No code path archives, deletes, or merges a knowledge entry without explicit human
      approval. `useful` and decay affect **ranking only**.
- [ ] A merged entry's `contributors` is the union of its parents'; a merged entry inherits
      **post-T0** access history and is not floored in the session it is created.
- [ ] Merge-candidate detection issues zero network calls to third-party embedding services.
- [ ] The similarity threshold was calibrated on DPC's own base; the procedure and the
      resulting number are recorded here, not inherited from EvoLib.
- [ ] The "Active Recall injected N hints" log line reports exactly the injected files,
      post-decay, with post-decay scores.
- [ ] `useful` is documented at its write site as a correlational heuristic, with the
      counterfactual named as unavailable and why.

## Scope

- `dpc_agent/active_recall.py` — hint rendering (T0a), counter keying (T0b), injection/read
  split (T0c), grace period (T0e), log rotation (T0f), slot dedup (T0g).
- `managers/agent_manager.py` — store a resolvable address in chunk metadata at index time
  (T0a); this is where the real `Path` exists.
- `dpc_agent/tools/core.py` — `read_file` → `update_access` (T0d).
- `dpc_agent/memory.py` — `created` field in `FileMeta` (T0e); merge writer (T1).
- `dpc_agent/context.py` — telemetry line (T0h).
- `dpc_agent/consolidation.py` — merge action, score/attribution transfer (T1).

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| T0a hint followability | Pending | — |
| T0b counter keying | Pending | — |
| T0c injection ≠ access | Pending | — |
| T0d read → update_access | Pending | — |
| T0e grace period (+ `created`) | Pending | — |
| T0f–h retention, dedup, telemetry | Pending | — |
| T1 merge + transfer | Pending | — |
| T2–T4 | Pending | — |

## Open Questions

- **Q1: Where do merge proposals surface?** `tier2_propose` has no production caller today, so
  a merge written into it would emit into nothing. Candidates: the sleep pipeline beside
  tier1 (output into the morning brief), a `pending_consolidation.jsonl` mirroring the skills
  queue, or a UI command. — @Mike
- **Q2: What does T1 merge — agent knowledge files or knowledge commits?** `FileMeta` has no
  `contributors`; attribution lives in the knowledge-commit layer (`participants`,
  `approved_by`, signatures), which also has a `parent_commit` chain. The two layers make
  "merge" mean different things, and P13 §12 applies to one of them. — @Mike
- **Q3: `_meta.json` covers ~5% of the indexed corpus** — `backfill_meta` walks the agent
  knowledge dir non-recursively (`memory.py:63`), so tier1/tier2 never see the EXT and L6
  layers where the duplication actually lives. Does consolidation's scope widen to the index,
  or stay at the agent layer? *(Fable 5 M5; not independently verified by CC.)* — @Mike
- **Q4: After T0c, is there enough read signal to rank on at all?** ~1 knowledge read/day
  across ~2 000 files. If not, decay should be disabled until clean signal accumulates rather
  than recalibrated. — @Mike
- **Q5: Corpus selection.** The set the agents actually read is nearly disjoint from the set
  that gets injected (org-mirror files, a session-archive JSON injected 1 161 times). No T
  item covers *what belongs in the index*. *(Fable 5 §1.7.)* — @Mike

## Authors

- **Mike** — Decision; called for the log audit that reversed the plan
- **CC** — Code and log verification, draft
- **Ark** — Initial analysis, synthesis across rounds
- **Warren** — Licence analysis
- **Fable 5, GLM 5.2** — Adversarial review, rounds 1 and 2 (empirical)

## Revision history — what changed and why

Kept because each reversal changed the decision.

**Round 1 (code only).** CC cited RSA as HMMT's strongest baseline; it is Best-of-N, so
EvoLib's real HMMT margin is +3.2, not +10.2 (Fable 5). CC claimed the counterfactual needed
only "pairing existing logs"; it needs parallel samples DPC does not draw (both reviewers).
CC claimed skill-side reflection already covered insight-from-error; it is scoped to one
skill's file and gated on a skill having been invoked. Ark mapped EvoLib's library sampling
onto Active Recall as a full match; EvoLib samples stochastically by accumulated utility,
DPC retrieves deterministically by relevance. GLM 5.2 read EvoLib's LiveCodeBench 70.0\* as a
win over RSA 70.0; the table footnote defines the asterisk as marking the best **score(s)**
above the **second-best**, and both carry it — it is a tie. Recorded because that claim had
already propagated into a synthesis as "confirmed by both reviewers".

**Round 2 (first draft of this ADR).** Two claims here were written from a reviewer's summary
without opening the code, and were wrong: that the human gate was "already architecturally in
place" (`tier2_propose` has no production caller) and that a birth date was "available in
`_meta.json`" (`FileMeta` has no such field). Both are now Confirmation items and Open
Questions instead.

**Round 3 (logs).** The team's own empirical brief overstated system health — total tool calls
counted as `read_file`, reads pooled by basename across projects — and every error pointed the
same way. Reviewers with raw-log access found the three defects that reordered this ADR: hints
unresolvable (Fable 5 M1), basename pooling (GLM 5.2 §2-B / Fable 5 M3), and `access_count`
measuring writes (both, §2-A / M6). CC independently reproduced all three before adopting
them, and corrected one of its own measurements in the process (a hint-follow classifier that
read a non-existent `result` key instead of `result_preview`, making its error check vacuous).

## References

- [arXiv:2605.14477](https://arxiv.org/abs/2605.14477) — *Test-Time Learning with an Evolving Library*
- [microsoft/EvoLib](https://github.com/microsoft/EvoLib) — reference implementation (MIT)
- `ideas/dpc-research/evolib-dpc-review-{fable5,glm52}.md` — round 1 review
- `ideas/dpc-research/evolib-dpc-empirical-review-{fable5,glm-5.2}.md` — round 2, against logs
- [ADR-010](010-agent-memory-architecture.md) — agent memory architecture
- [ADR-013](013-agent-selection-layer.md) — Active Recall decay is stage S4 of this ADR
- [ADR-024](024-knowledge-graph-infrastructure.md) — retrieval backend used for candidate detection
