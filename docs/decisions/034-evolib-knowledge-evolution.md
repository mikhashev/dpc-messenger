---
adr: 034
title: "Repair the Active Recall access layer, then adopt EvoLib's bookkeeping mechanisms"
status: accepted
date: 2026-07-31
axis: knowledge
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
>
> **T0 shipped 2026-08-01 (S59–S60); status accepted.** All eight items are in `dev`; see
> *Implementation Status*. Two of them were solved differently from what this ADR specified
> and the difference is recorded there rather than quietly absorbed. T1–T4 are untouched and
> still gated on Q1–Q2 — and now on Q5, which review promoted from a question to a
> precondition.
>
> **Numbers in this document.** Mike, 2026-08-01: *«а нахуя вообще такие числа в ADR, они у
> нас сейчас такие — данных подгрузили в граф, и поменялись»*. He is right, and the rule that
> follows from it is:
>
> - **Evidence for the decision stays, frozen and dated.** It is what makes the decision
>   auditable, and it is never edited afterwards — a correction goes beside it, not over it.
> - **Current-state metrics do not belong here at all.** Counts of documents, reads, timings,
>   follow rate: every one changes the next time data is loaded or the code is touched, and a
>   figure that rots inside a decision record makes the whole record untrustworthy. They live
>   in `backlog.md`, which is rewritten as the system changes.
> - **Every figure carries the date it was taken and what it was taken with**, because the same
>   quantity measured through a broken counter and a fixed one is two different numbers — which
>   is exactly what happened to the "166" below.
>
> **D0 = 2026-08-01 17:05:25 local (10:05:25 UTC)** — the restart after `ca80a78e`, the first
> run in which the address works, the injection credit is bounded, and the grace period
> exists. It is the epoch of every follow-rate measurement: numbers from before it describe a
> different system, and this ADR's own "0 of 28" belongs to that one. D0 was not declared at
> the first repair commit but at this restart, because until it the credit constant was one we
> had already measured as wrong.

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

> **After D0 this stopped being zero** — the first verbatim follows in the system's history,
> and no slot printed without an address. The running count is in `backlog.md`; it changes
> every turn and does not belong here.
>
> Two things about it are permanent and do belong here. **The early follows were all files
> Mike had asked for in the same minutes**, so what a non-zero count proves is that the address
> is *followable*, not that the agent went *because of the hint* — separating those needs cases
> nobody asked for, and any report that skips this caveat is overclaiming. And **the first
> count was wrong in our favour**: the join credited one read to all five injections that had
> shown that file. A rate needs distinct events on both sides — each read is attributed to the
> nearest preceding injection and counted once, and a paginated read is one follow.

And every knowledge read that *did* succeed used an address the hint never prints:

| how successful knowledge reads were addressed | count |
|---|---|
| absolute path | 127 |
| `knowledge/` prefix | 87 |
| **the form the hint prints** | **0** |

**2. The access counter is therefore ~entirely self-generated.** Fleet-wide: **28 873
injections vs 166 genuine accesses (99.4% injection)**; per agent 86–99.9%. The top-ranked
key for the largest agent is `README.md` — injected 4 102 times, read 0 times.

> ### ⚠️ Correction — 2026-08-01
>
> **The 166 is an artefact of the counter it was measuring.** It came from the same counter
> this ADR was about to repair: reads pooled by basename, and only the live half of a rotating
> read log ever opened. Re-counted after the repair, the real figure is more than an order of
> magnitude higher.
>
> **The finding stands; the magnitude does not.** Injection did dominate, and that is what the
> decision rests on. The ratio was wrong on both sides.
>
> The number above is left as written because it is the evidence the decision was made on, and
> a decision record that edits its own evidence cannot be audited. The current figure is not
> reproduced here — see *Numbers in this document* — it lives in `backlog.md`, which is
> rewritten as the system changes.

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

- [x] **A hint can be followed verbatim.** For each layer (L5, L6, EXT), a test issues
      `read_file("<exactly what the hint block printed>")` and receives content. Where the
      firewall legitimately denies it, the hint says so rather than printing an
      un-followable path.
      — `tests/test_recall_hint_is_followable.py`, and `tests/test_recall_address_survives_the_store.py`
      which writes through a real backend and opens the address that backend hands back, on
      both retrieval backends. The second exists because the first builds its metas by hand
      and so passed for 102 days over a store that dropped the field. Confirmed in production
      after D0: 27 slots, 0 without an address, the shared layer among them.
- [x] The access counter distinguishes injection from read; N consecutive injections with no
      read do not raise a file's decay multiplier, one real read does. — `7e14b6a4`; the bound
      that makes "does not raise" true in the ranking, not only in the counter, is `ca80a78e`.
- [x] Counter keys are layer-relative paths: two files sharing a basename across layers have
      independent counts (regression test with two `README.md`). — `adaaaa45`.
- [x] `read_file` on a knowledge file increments `_meta.json.access_count`; a written-once,
      read-many file is not proposed for archive. — `5ee3625a`; the historical counts moved to
      a write column rather than being discarded, since every one of them was a write.
- [x] A new entry is not pinned to `DECAY_FLOOR` during its grace period. — `ca80a78e`. Grace
      returns 1.0, which is *level with* the busiest candidate in the set and not above it: it
      removes a penalty, it does not hand out a promotion. **The parenthetical "(requires
      `created` in `FileMeta`)" was dropped** — see T0e above.
- [ ] ~~`knowledge_access.jsonl` is bounded by the same rotation policy as other agent logs.~~
      **Criterion withdrawn — that policy deletes.** Replaced by: the log is bounded without
      losing a line, and the counter compares injections and reads over one window.
      — [x] `f9620ec0`, with the archive and the line-count conservation test.
- [x] No code path archives, deletes, or merges a knowledge entry without explicit human
      approval. `useful` and decay affect **ranking only**. — still true; re-checked while
      touching consolidation's neighbours. `tier2_propose` still has no production caller.
- [ ] A merged entry's `contributors` is the union of its parents'; a merged entry inherits
      **post-T0** access history and is not floored in the session it is created.
- [ ] Merge-candidate detection issues zero network calls to third-party embedding services.
- [ ] The similarity threshold was calibrated on DPC's own base; the procedure and the
      resulting number are recorded here, not inherited from EvoLib.
- [x] The "Active Recall injected N hints" log line reports exactly the injected files,
      post-decay, with post-decay scores. — `8a599d93`; observed in production on the first
      injection after the restart.
- [ ] `useful` is documented at its write site as a correlational heuristic, with the
      counterfactual named as unavailable and why.

## Scope

Planned scope, with what the implementation actually touched:

- `dpc_agent/active_recall.py` — hint rendering (T0a), counter keying (T0b), injection/read
  split (T0c), grace period (T0e), retention window and compaction (T0f), slot dedup (T0g).
- `managers/agent_manager.py` — store a resolvable address in chunk metadata at index time
  (T0a); this is where the real `Path` exists.
- `dpc_agent/tools/core.py` — `read_file` → `update_access` (T0d).
- `dpc_agent/memory.py` — ~~`created` field in `FileMeta` (T0e)~~ **not needed**, see T0e;
  the write column and its migration landed here instead. Merge writer (T1).
- `dpc_agent/context.py` — telemetry line (T0h).
- `dpc_agent/consolidation.py` — merge action, score/attribution transfer (T1). **Untouched.**

Three files were not in the plan and had to change, each because the defect had a second copy
the code reading had missed:

- `dpc_agent/index_keys.py` (new) — one place that decides what a document is called, after
  the EXT scheme was found to collapse every project's `README.md` onto one key.
- `dpc_agent/retrieval/grafeo.py` — the store dropped `source_path`, so T0a worked in tests
  and not in production.
- `dpc_agent/knowledge_graph.py` — the graph channel built its metadata outside the index and
  spoke a different document identity, so its hints had no address and its results could never
  be fused with the same document from another channel.

## Implementation Status

All of T0 landed on `dev` on 2026-08-01 across two sessions. Where a commit differs from what
this ADR specified, the difference is named in the row and expanded under the table.

| Task | Status | Commit |
|------|--------|--------|
| T0a hint followability | **Done** | `819cb52d`, `c51cf81b` — key per document, then an address the agent can open |
| T0a (prod) store keeps the address | **Done** | `a2ddcdae` — the Grafeo backend dropped `source_path`, so T0a was dead in production while green in tests |
| T0b counter keying | **Done** | `adaaaa45` |
| T0c injection ≠ access | **Done** | `7e14b6a4`, credit bound corrected in `ca80a78e` |
| T0d read → update_access | **Done** | `5ee3625a` |
| T0e grace period | **Done, differently** | `ca80a78e` — measured in days from file mtime; **no `created` field was added** |
| T0f retention | **Done, differently** | `f9620ec0` — not rotation: one window derived from the reads, plus an archive |
| T0g slot dedup | **Verified — not present** | no code, no config, no procedure: the measurement that was to justify the fix found no duplication to fix |
| T0h telemetry | **Done** | `8a599d93` |
| Measurability (`task_id`, printed addresses) | **Done** | `514d77eb` — added so T0 could be verified from the log at all |
| Graph channel addressing | **Done** | `eb03078c`, `59d2e211`, `238fb34c`, `86daefb9` — a second, independent copy of the same defect |
| Shared-layer gate at hint time | **Done** | `325b1f88` |
| Legacy-state test fixture | **Done** | `54b7a0ba` |
| T1 merge + transfer | Pending — gated on Q1, Q2, Q5; **API cost to be measured before implementation, not after** | — |
| T2–T4 | Pending | — |

**T0e — why no `created` field.** This ADR said the grace period needs a birth date and that
`FileMeta` has none. It still has none. Age is taken from the file's mtime via `source_path`,
which every layer now carries, so the check is a `stat()` per candidate and no schema change,
no backfill, and no question of what to seed the field with for documents that predate it. The
constant is also now in days rather than sessions: there is no session counter at this layer,
which is part of why the original one sat unused for months.

**T0f — why not rotation.** The item said to put this log under the shared rotating writer.
That writer keeps one old file and deletes the one before it, which would have destroyed the
only record of what the system offered over 103 days — and both reviewers said as much. What
shipped instead: the counter derives one window from the *reads*, since reads are the side that
expires, and ignores injections older than the oldest surviving read; a compaction step moves
what falls outside into `knowledge_access.archive.jsonl`, which nothing parses at runtime.
The property to hold on to is that compaction does not change what is counted — the archive is
data the window already excluded. The same commit also started reading the rotated half of
`tools.jsonl`, which had been on disk and ignored, roughly doubling the reads available to the
counter. Sizes and timings are in `backlog.md`.

**T0g — verified, not implemented.** The measurement that was to justify the fix found no
duplication. Nothing was written, nothing is configured, and there is no manual procedure —
if the duplication returns, this item starts from zero. Recorded this way because "Done"
against an item nobody implemented sends the next reader looking for the mechanism.

**T1 — the cost is a precondition, not a postscript.** This ADR's drivers say "no GPU budget:
CPU + local embeddings + LLM API", which prices the GPU and says nothing about the API. T1's
merge step is an LLM call per candidate pair, and nobody has estimated how many pairs. That
estimate is due **before** implementation starts, not as a surprise afterwards — the same rule
this ADR already applies to its own claims: measured before ordered.

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

  > **Answered 2026-08-01 — the premise was wrong by more than an order of magnitude, so this
  > question is closed on its own terms.** The "~1 read/day" was measured through the counter
  > this ADR repairs: injections were being counted as accesses, and half the read log was
  > never opened. Re-counted after T0b/T0c/T0f, against the index, with the indexer's own
  > roots: reads landing on indexed documents run to tens per day on the busiest agent. Decay
  > does not need disabling for want of signal. (Figures in `backlog.md` — they move.)
  >
  > **What this does not settle is whether it is the right signal**, and that now blocks
  > something. `dpc-messenger` is itself an indexed root of several hundred documents, so much
  > of the count is an agent reading the code it is working on rather than consulting
  > knowledge. See Q5, which this promotes from a question to a precondition.
- **Q5: Corpus selection — promoted to a precondition of the T0 verdict, 2026-08-01.** The set
  the agents actually read is nearly disjoint from the set that gets injected (org-mirror
  files, a session-archive JSON injected 1 161 times). No T item covers *what belongs in the
  index*. *(Fable 5 §1.7.)* — @Mike

  > **Why this outranks waiting for follow-rate data.** The plan is to accumulate two weeks of
  > follow rate and decide from it whether the hint modality works. That decision is not
  > readable while Q5 is open: if the corpus is wrong, a low follow rate means "we are offering
  > the wrong documents", and if the corpus is right it means "the modality does not work" —
  > the same number, two opposite conclusions, and nothing in the data distinguishes them.
  >
  > So the corpus has to be settled *before* the window closes, not after. Q4's answer sharpens
  > this rather than relieving it: there is plenty of read signal, and much of it is an agent
  > reading the project it works on. Tracked as `AR-CORPUS-MISALIGNMENT` in `backlog.md`, ahead
  > of the follow-rate item. *(Raised by Warren and Ark on review of `12dcd94a`; both were
  > right that this was filed as a question when it is a blocker.)*

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

**Round 4 and implementation (S59–S60, 2026-08-01).** T0 shipped. Four things are worth
recording because each changed what this ADR says or how it should be read.

*The defect had copies.* T0a was fixed at hint time and at index time and was still dead in
production, because the Grafeo store never persisted `source_path`; then the graph channel
turned out to build its metadata outside the index entirely, a third instance of the same
mistake in a place no one had looked. "Fixed at the layer where the value is produced" is not
the same as "carried by every layer in between".

*Every defect was green in the suite and red on the first restart.* Four times, ending with a
guard that passed 1145 tests and then refused to import the shared layer on six agents. The
common shape: the tests built clean state, production is made of rows written by code that no
longer exists. `54b7a0ba` gives the suite five measured forms of legacy state; the guard
regression now fails in it.

*Two of our own measurements were wrong in our favour, and both were caught by re-deriving
rather than by re-reading.* A claim that the null-address rate was "now being measured" was
made 20 minutes before the commit that measures it was restarted into. And the first
follow-rate count reported 7 where the honest number was 3. The rule the team adopted after
S59 — a report about a running system states the last process-start time next to the last
commit time — was written because of the first and would have caught it.

*Both external reviewers measured a stale store, and so did the excuse for it.* Every graph
number in the fourth round — node counts, orphaned edges, stem collisions — came from a SQLite
file last written on 17 May, while production had been on Grafeo for weeks; one review
presented them as measured against the live agents, and the team's synthesis inherited them.
The first account of this said nobody was careless, since there is no way to snapshot the live
graph without stopping the backend. Half true: the tool is missing, and the *answer* was not —
`KnowledgeGraph initialized at <path> [backend=<name>] (N nodes, M edges)` is printed at every
startup, in the log all of us were reading. What was absent was the habit of asking the system
what it had opened before opening a file and assuming.

The proposal that followed — delete or rename the stale file so it stops looking live — was
also wrong, and Mike caught it: the backend is user-selectable (`[knowledge_graph] backend`,
default `sqlite`; retrieval backends per agent in the UI, which states that old index files are
kept deliberately). The stale store is the rollback state. Two of us reached for deletion
because we had each assumed the choice was permanent.

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
