---
adr: 037
title: "Make the author's chain the unit of history, in phases, and not all of it now"
status: proposed
date: 2026-08-05
deciders: [Mike]
consulted: [Ark, CC, Fable 5, GLM 5.2]
informed: []
depends_on: [ADR-036]
related: [ADR-036]
session: S64
---

## Context and Problem Statement

ADR-036 moves a message signature to its author. That answers *who said this*.
It does not answer *what the history is* — and the structure holding the
history is where three defects live, all of them the same defect wearing three
faces.

Today a room keeps **one chain**:
`chain_hash = SHA256(msg_index|message_id|role|sender_name|content|timestamp|prev_hash)`.
Both `msg_index` and `prev_hash` are functions of the order in which messages
*arrived at this node*. A room has N writers and no shared write head, so the
chain records a delivery order, not a history. From that single fact:

1. **A permanent false alarm.** Two honest nodes holding identical messages in
   different arrival order compute different chains, so `history_hash` differs,
   so `GROUP_HISTORY_STATUS` reports divergence; the sync that follows adds
   nothing (dedup by `id`) and the hashes still differ. Every connection,
   forever.
2. **A permanently broken chain.** `merge_history` appends a peer's
   `msg_index`/`chain_hash` verbatim, so the next load recomputes a different
   expected hash and logs "Chain broken" — and a mismatched hash is never
   rewritten, only a missing one.
3. **Undetectable deletion.** `.chain_meta.json` is local and is rewritten by
   whoever rewrote the history.

Two external reviews (Fable 5, GLM 5.2) examined the proposal to replace the
room chain with a per-author chain, in the shape Secure Scuttlebutt has run
since 2014. **They disagreed**, and the disagreement is the substance of this
ADR rather than an inconvenience to be smoothed over.

## Decision Drivers

- **Three layers, not one.** (A) humans, ≤150, replicate everything;
  (B) agents, 3–6 orders of magnitude faster (§5) and immortal (§3.7),
  cannot replicate everything; (C) both in one room — which is not a future
  case but the current product, including the group where this was decided.
- **Detection equals witness overlap.** A rewritten past is detectable exactly
  where someone kept the original. Graduated Autonomy (§6) makes replication
  partial *on purpose*, so detection strength is a function of topology, not a
  constant of the protocol.
- **AP, not CP (§3.2).** Consensus over a canonical order is excluded by a
  decision already made.
- **Non-atomic upgrade.** Wire changes cost a capability flag, a staged
  rollout, and a period where "old node" and "attacker imitating one" must stay
  distinguishable.
- **The product says no feeds.** `VISION.md:126` — "Not a social network — no
  feeds, no engagement metrics, no viral mechanics."

## Decision

**Adopt the author's chain as the unit of history — in three phases, and
commit only to the first two now.**

The proposal decomposes (GLM 5.2's framing, adopted here):

| | What | Cost | This ADR |
|---|---|---|---|
| **α** | Author signature on the wire; author taken from the signature | Low | **Already decided** — ADR-036, independent of this |
| **β** | Order-independent digest for STATUS: a set of author heads, or a digest over `content_hash` | Medium | **Adopt now** |
| **γ** | Full model: `seq` + `prev` on the wire (`dptp-msg-v2`), fork evidence, checkpoints, membership as signed state, lifecycle | High | **Deferred behind stated preconditions** — not rejected |

And two decisions that hold across all phases:

1. **Naming.** Not "feeds". `VISION.md:126` forbids the word for good reason —
   "feed" carries subscription and a social graph, and this is neither. The
   structure is an **author attribution chain**.
2. **Verification is graduated, but not uniformly.** Content verification
   follows the Graduated Autonomy table; **integrity verification does not**.
   A held head is one signed message per author — a constant — and a holder of
   heads is a full witness for fork detection while holding no bodies at all.
   Mapping: layer 5 — bodies and heads; 15/50 — bodies partial, heads full;
   150 — heads only; 500 — neither, and that layer is labelled *unwitnessed*
   rather than quietly undefended.

### Rationale

**Why β now.** It fixes both permanent defects — the false alarm and the
broken chain — because it removes the order-dependent quantity rather than
repairing it. It needs no new preimage, no wire version, and no new message
types. Both reviewers agree on it, from opposite conclusions.

**Why γ is deferred and not rejected.** GLM argues γ buys nothing where the
project actually lives, since witnesses are absent by construction in layers B
and C, and offers a cheaper route to equivocation proofs: derive `message_id`
from `(author, seq)`. That route reintroduces `seq` — a per-author monotonic
counter is exactly the quantity whose absence the argument is built on. So α/β/γ
do not separate as cleanly as claimed: in the one property where γ is not
replaceable, the proposed substitute is γ under another name.

What survives from GLM's argument, and it is the important part: γ buys
provable equivocation **where a witness exists**, and nothing where none does.
That is not an argument against γ; it is the argument for pairing it with
anchors, which is why the preconditions below are preconditions and not
follow-ups.

**Why γ is not first.** A room was described as an overlay of N chains, and the
list of N is not derivable from the chains. Without signed membership, "a
participant whose chain I have not received" is indistinguishable from "not a
participant", and the collusion case stays open regardless of how well each
chain is signed. Both reviewers reached this independently.

**Why not per-author chains globally.** A chain is per (author, room).
A single global chain per identity — SSB's shape — leaks the volume and rhythm
of an author's entire activity to anyone entitled to replicate any one room.
`conversation_id` is already field 2 of the v1 preimage; `seq` must number
within a room.

## Considered Options

- **Option A — keep the room chain, ship ADR-036 only.**
- **Option B — adopt the full model now** (Fable 5's recommendation, with three
  conditions).
- **Option C — α + β now, γ behind preconditions** (chosen).
- **Option D — reject γ outright** (GLM 5.2's recommendation).

### Pros and Cons of the Options

#### Option A — ADR-036 only

- Good: nothing new to design; authorship is fixed.
- Bad: both permanent defects survive untouched. They are structural, not bugs
  — no amount of repair inside the room chain removes a quantity that depends
  on delivery order.

#### Option B — full model now

- Good: one coherent structure; the three symptoms die together.
- Good: N independent writers with no shared head is the actual topology, and
  the model matches it.
- Bad: pays for `dptp-msg-v2`, fork-evidence messages, checkpoints, membership
  and lifecycle in one step, while membership is a precondition of the rest.
- Bad: commits to the expensive half before the cheap half has demonstrated the
  fix.

#### Option C — phased (chosen)

- Good: the two permanent defects die in phase β, at a fraction of the cost.
- Good: preconditions are named, owned and testable rather than discovered
  mid-implementation.
- Neutral: two migrations instead of one; acceptable because β needs no
  preimage change and therefore does not strand anything.
- Bad: equivocation proofs wait.

#### Option D — reject γ

- Good: avoids a wire version and a dozen message types.
- Bad: leaves the only mechanism that turns "we disagree" into "here is proof,
  signed by you" unbuilt, and the offered substitute reintroduces `seq`.

## Consequences

- **Positive:** the false STATUS alarm and the permanent "Chain broken" end in
  phase β, and they end by removing the order-dependent quantity rather than by
  repairing it.
- **Positive:** the verification tiering states plainly where detection exists
  and where it does not, instead of implying uniform protection.
- **Negative:** equivocation stays undetectable until γ; a node that rewrites
  its own past remains detectable only by comparison with a witness.
- **Negative:** two rollouts, each needing a capability flag.
- **Neutral:** naming changes across documents and code that already say
  "feed".

## Confirmation

- [ ] Two nodes holding identical messages in different arrival order report
      **no** divergence — measured on the live pair, not only in tests.
- [ ] A merge that legitimately adds nothing clears the divergence flag.
- [ ] "Chain broken" disappears from a normal session log after β.
- [ ] `history_hash` no longer depends on arrival order — the same message set
      in two orders produces the same digest.
- [ ] A node holding only heads (no bodies) detects a fork presented to it.
- [ ] A layer without witnesses is labelled *unwitnessed* in the UI rather than
      showing the same badge as a witnessed one.
- [ ] No document or identifier in the codebase calls this a "feed".

## Scope

- `dpc-client/core/dpc_client_core/conversation_monitor.py` — order-independent
  digest; stop deriving `history_hash` from the chain tip
- `dpc-client/core/dpc_client_core/message_handlers/group_handler.py` —
  `GROUP_HISTORY_STATUS` v2 carrying author heads
- `specs/dptp_v1.md` — specify `GROUP_TEXT` and `GROUP_HISTORY_*`, which are
  not specified today, before adding to them
- `dpc-client/ui/src/lib/panels/ChatPanel.svelte` — provenance levels

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| α — ADR-036 (author signature on the wire) | Pending | — |
| β — order-independent digest, STATUS v2 | Pending | — |
| Naming swept out of docs and code | Pending | — |
| γ — preconditions below | Blocked | — |

## Preconditions for γ

Each is a decision in its own right; γ is not scheduled until they are made.

- **P1 — membership as signed state.** A room is an overlay of N chains and the
  list of N must be derivable and signed. Without it, collusion fabricates the
  room rather than the speech.
- **P2 — anchors / checkpoints.** Where witnesses are absent by construction,
  detection does not degrade — it vanishes. An anchor decouples detection from
  replication: what is needed is not a holder of the message but a holder of a
  root covering it.
- **P3 — agent cryptographic identity.** One key for a human and their agent
  makes repudiation symmetric in both directions. Needs a delegated key, and
  therefore revocation.
- **P4 — agent chain lifecycle.** An append-only chain that never ends is
  unbounded growth against §3.7. Checkpoints plus epochs, or γ reproduces the
  pathology it was meant to fix.

## Open Questions

- **Q1:** Recover-before-write — a node restored from a stale backup reuses a
  `seq` and self-forks, indistinguishable from an attack. Fable 5 reports this
  as the most common real cause of dead chains in SSB's decade of operation.
  Protocol requirement from day one of γ, or is it acceptable later? — @Mike
- **Q2:** Does β use author heads or a digest over `content_hash`? Heads carry
  metadata (who spoke, how much, when) and therefore belong under the firewall;
  a content digest does not, but says less. — @CC
- **Q3:** `GROUP_TEXT` and `GROUP_HISTORY_*` are not in the spec at all.
  Specify the existing protocol before extending it? — @Ark
- **Q4:** The minted-hash laundering: `load_history` counts a minted hash and
  says so, but persists it, so the next load sees a message that already has a
  hash and stays silent. The log tells the truth once and the disk forgets it.
  Mark minted hashes in the file, or stop minting? — @CC

## Authors

- **Mike** — Decision; the framing of three layers (A/B/C)
- **Ark** — Analysis, synthesis, phased-approach proposal
- **CC** — Code audit, measurements, prompts, this ADR
- **Fable 5, GLM 5.2** — External adversarial review, in disagreement

## References

- `[ADR-036](036-message-authenticity-signed-at-origin.md)` — signature at the author
- `ideas/dpc-research/group-auth-per-author-chains-prompt.md` (v4) — review prompt
- `ideas/dpc-research/group-auth-per-author-chains-review-fable5.md`
- `ideas/dpc-research/group-auth-per-author-chains-review-glm52.md`
- `ideas/dpc-full-picture/dpc-full-picture-s32.md` §3.2, §3.7, §5, §6
- `VISION.md:126` — no feeds
- [Secure Scuttlebutt — append-only feeds](https://ssbc.github.io/ssb-db/)
- [Certificate Transparency gossip](https://arxiv.org/pdf/1806.08817) — split-view detection
