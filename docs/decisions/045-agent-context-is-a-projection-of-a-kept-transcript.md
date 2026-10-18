---
adr: 045
title: "Build the agent's request as a projection of a kept transcript, never lose a cut result without an address"
status: proposed
date: 2026-10-18
axis: knowledge, network
deciders: [Mike]
consulted: [Ark, Johnny, Zcode, Warren, CC]
informed: []
depends_on: [ADR-033]
related: [ADR-044, ADR-010]
supersedes: []
session: "DPC Project group chat, 2026-10-18 — epic AN-AGENT-TURN-LOSES-TOOL-OUTPUT-FOR-GOOD-AND-RE-READS-A-PREFIX-THE-ENGINE-ALREADY-HELD; Mike accepted the synthesis and asked for this draft"
---

# ADR-045 — The agent's context is a projection of a kept transcript

> **Proposed, 2026-10-18.** Amends ADR-033 for the compaction-off setting and for how any
> cut is made. ADR-033's summariser (compaction on) stays as decided there. The human chat
> is out of scope: it is not cut and still blocks at a full window.

## Context and Problem Statement

Inside one agent task the loop keeps the window by cutting old tool results. With
compaction off — the default — a cut replaces each old result with its first 200
characters and nothing else (`dpc_agent/context.py`, `compact_tool_history`). The full
output is in fact persisted (`history.json`, `tool_calls[].output`), but only once the
turn ends, and the cut does not say so, so the model cannot get the data back and does not
know what it lost. ADR-033 kept this truncation unchanged for the off
setting.

Four further properties make the cut both lossy and expensive:

- **Retention is counted in rounds.** "Keep the last six rounds" and "cut every 24 uncut
  rounds" promise nothing about tokens, because one round's result ranges over more than
  an order of magnitude.
- **The list is edited in place.** A cut marks messages as compacted for good; the request
  is a side effect of the history of cuts, not a function of what happened and how much
  room there is. A file read before the agent rewrote it stays in context as if current.
- **The model sees its budget only at turn start.** Inside a turn it learns about the
  window from the stop at 95 % (`ContextLimitGuard`).
- **A local engine resumes from a checkpoint.** On a hybrid model it cannot truncate its
  state to an arbitrary point. On 2026-10-18 Strata resumed from its first checkpoint after
  every divergence — at turn starts with and without a memory write, and after a cut — so
  each of them re-read most of the prompt. The cause, read in Strata's code: six
  checkpoints by default, the oldest pinned and the rest evicted by least recent use, so a
  turn of more than a few rounds loses the checkpoint at its own start (epic child 1).

Eleven open harnesses were read for this (Codex, opencode, pi, Gemini CLI, coddy, qwen-code,
cline, aider, goose, crush, openhands; Ark, Johnny and Zcode, 2026-10-18). They agree on
the invariants adopted below. None aligns a cut to an engine's cache boundaries.

## Decision

1. **Transcript and projection are separate.** What happened in a task — every message,
   tool call, argument and full result — is the transcript, kept on disk. The request sent
   to the model is a projection built from the transcript and a budget, rebuilt for each
   round. Nothing edits the transcript to make room. Two projections of the same transcript
   under the same budget are byte-identical; a test holds this.
2. **A cut never loses a result without an address.** An evicted tool result becomes a
   placeholder naming the tool, its arguments, its size and where the full output lies. A
   repeatable call (`read_file`, `grep`, a listing) also says that re-running it returns
   the data. A result that cannot be reproduced (`run_shell`, network, state) stays
   reachable by address. A read of a file the agent wrote later becomes "modified, re-read
   for current contents".
3. **Retention is a token budget.** Two budgets, both in tokens: how much recent tool
   history stays verbatim, and how much of the window stays free for the next round and the
   answer. Rounds are not a unit of retention.
4. **With compaction off, the loop cuts only under window pressure.** The "every N uncut
   rounds" rule is removed once decision 2 is in place, not before: until a cut keeps an
   address, an early shallow cut is the lesser loss. Compaction on keeps ADR-033's
   summariser, with decisions 1–3 and 5–7 applying to it as well.
5. **Every cut says what it dropped.** The log line names the evicted calls, their sizes
   and their addresses, not only how many rounds were kept.
6. **The model knows its budget inside a turn.** A short budget line at the tail of each
   round (never inside the cached prefix), and a soft warning before the 95 % stop.
   `ContextLimitGuard` stays as the last line.
7. **Stable at the head, volatile at the tail.** Nothing that changes within a task is
   placed ahead of the conversation. Agent memory is not moved into the replayed turn tail,
   because past tails replay byte-for-byte and every turn would add a copy. Memory enters
   as a header plus its recent entries, with the body by address.
8. **The checkpoint at a turn's start survives the turn, and one-off calls take no
   checkpoint.** A local engine resumes only from a checkpoint at or before the divergence,
   and the next turn diverges where the previous turn's tool rounds began, so that is the
   checkpoint to keep. A call nobody continues does not occupy a checkpoint slot. How it is
   met today, recorded separately from the rule:
   - Strata keeps the turn-start checkpoint with `--prompt-cache-spread` (set on the
     alias 2026-10-19). Observed: before the flag 7 of 7 turn boundaries resumed from the
     first grid checkpoint; after it 1 of 1 boundary above the root resumed from the
     turn-start checkpoint. One observation; the card records further ones.
   - One-off calls marking themselves with `strata_checkpoint: false` is decided, not
     built: no DPC call sends it yet.
   - llama-server keeps two checkpoints and pins none; it has no equivalent setting, so
     there the lever is decision 2 (fewer and cheaper cuts), not the engine.
   Aligning cuts to the engine's checkpoint grid was considered and dropped: under
   least-recent-use eviction only the oldest grid point survives a long turn, so there is
   no grid to align to.

### Rationale

- Completeness is not a property of a threshold. Any window compresses; what matters is
  whether the compression is reversible. An address makes it reversible at the cost of
  writing each result once, at the call, which the loop already does after the turn.
- Counting in tokens is what makes a budget mean something; a round is an accident of what
  a tool returned.
- A pure projection removes the class of drift that "cut once, never again" was patching,
  and makes the prompt prefix testable by a unit test instead of a log.
- On a local engine a prefix rewrite costs a re-read from the last usable checkpoint. A cut
  that is rare and lands on a checkpoint costs little; a frequent cut, or one that lands
  far from a checkpoint, costs most of the prompt each time.

## Considered Options

- **A. Keep ADR-033's off branch (truncate to 200 characters, round-based).** Deterministic
  and free, but loses data with no way back and pays a re-read on every cut. Rejected.
- **B. Off means never cut; only the 95 % stop.** Simplest, and the prefix never moves
  until the stop. Rejected as the end state: a long task with large results ends early,
  and vendor calls carry every old result at full price. Decision 4 keeps its spirit —
  no cut without pressure — once cuts stop losing data.
- **C. Move the scratchpad into the turn tail** so a memory write does not move the head.
  Rejected: past tails replay byte-for-byte, so each turn would carry another copy.
- **D. This decision: projection, addresses, token budgets, pressure-only cuts, budget
  visible to the model, cuts aligned to engine checkpoints.**

## Consequences

- **Positive:** a cut no longer loses data; the agent can always get a result back. Cuts
  become rare and cheap on local engines. The prompt prefix becomes a tested property.
- **Negative:** placeholders cost a few tokens each and the model must learn to follow them;
  the projection is a rewrite of the loop's history handling, not a patch. Reading a full
  output back from `history.json` ties the loop to that file's lifetime.
- **Neutral:** the tool-schema share of each request (a separate card in the epic) is not
  decided here; it is the next-largest fixed cost after the prefix re-reads.

## Open Items

- **Strata pins the oldest checkpoint, not the system-prompt root.** With a system prompt
  longer than the 16384-token grid the pinned point is the first grid checkpoint and the
  root rotates out. To be raised upstream; not decided here.
- **Where full outputs live during a task.** `history.json` holds them, but it is written
  at the message boundary, after the turn, while a cut happens inside the task. Decision 2
  therefore needs a store written at the call, before any placeholder points at it; its
  form (per-call file or a larger preview in `tools.jsonl`), size and retention are left to
  the implementation card.
- **The pin.** Whether the model may mark a result "keep until the task ends" (coddy's
  `keep`) is left to the implementation card for decision 2.

## References

- ADR-033 — agent tool-loop LLM compaction (the off branch this amends).
- ADR-044 — Strata as a second supervised local engine (checkpoint grid, parking).
- Epic AN-AGENT-TURN-LOSES-TOOL-OUTPUT-FOR-GOOD-AND-RE-READS-A-PREFIX-THE-ENGINE-ALREADY-HELD
  and its children, which carry the 2026-10-18 measurements.
- DPC Project group chat 2026-10-18, harness surveys by Ark, Johnny and Zcode.
