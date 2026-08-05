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

1. **A permanent false alarm — and worse than "order-dependent" implies.**
   Measured 2026-08-06 across three nodes holding the same nine messages: the
   ids, the order, the indices 1…9, the content, the timestamps and even
   `sender_name` are **identical everywhere**, and all three chain tips differ
   anyway (`b9382ddb` / `92fe4de6` / `56ece077`).

   The cause is `role`, which `chain_input` covers and which is **per reader by
   construction**: each node marks its own messages `user` and everyone else's
   `peer`. It differed on all nine messages. So the room chain cannot converge
   between honest nodes even when nothing about delivery differs — divergence
   is not a race, it is guaranteed.

   This is the same principle ADR-031 states and ADR-036 §4.1 already honours
   by keeping `role` out of the signing preimage: a per-reader rendering is not
   a property of the message. The chain never got the memo.

   A note against an earlier claim of ours: `sender_name` was blamed for
   contributing to this. Measured — it is identical on all three nodes. It
   does not contribute.
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

And it is weaker than that, on a second axis Fable 5 supplied: **the substitute
catches only those who agree to be caught.** Deriving an id from a counter is a
sender-side convention, and an equivocator simply issues two unrelated random
ids for the two versions — no collision, no detection. To make the convention
binding, the receiver must verify `id = H(author, room, seq)`, which requires
`seq` to be on the wire and checkable. At that point the substitute is not γ
renamed, it *is* γ. In γ proper the counter is mandatory and contiguous, so
equivocation is forced to collide. Worse still today: even an honest collision
would be dropped in silence — `add_message_with_id` sees a duplicate `id`,
logs at DEBUG, and never compares content.

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

Each criterion carries the phase it belongs to. Without the tags the list is
unpassable by any accepted phase — β could be delivered in full and the sheet
would stay red on γ items, which turns a compliance test into a wish list.

- [ ] **[β]** Two nodes holding identical messages in different arrival order
      report **no** divergence — measured on the live pair, not only in tests.
- [ ] **[β]** A merge that legitimately adds nothing clears the divergence flag.
- [ ] **[β]** "Chain broken" disappears from a normal session log.
- [ ] **[β]** `history_hash` no longer depends on arrival order — the same
      message set in two orders produces the same digest.
- [ ] **[β]** A message that arrives without a chain hash stays marked as
      lacking one after a save/load cycle — the file remembers what the log
      says once (Q4).
- [ ] **[β]** Group history sync no longer travels the private
      `CHAT_HISTORY` path, or that path is under the same rules.
- [ ] **[now]** No identifier in code, no line in the spec, and no ADR calls
      this a "feed".
- [ ] **[γ]** A node holding only heads (no bodies) detects a fork presented
      to it.
- [ ] **[γ]** A layer without witnesses is labelled *unwitnessed* in the UI
      rather than showing the same badge as a witnessed one.
- [ ] **[γ]** In a star, C learns B's head without trusting the relay A.

## Scope (phase β)

- `dpc-client/core/dpc_client_core/conversation_monitor.py` — the
  order-independent digest (form per Q2); stop deriving `history_hash` from the
  chain tip. **And name which of the two ends "Chain broken":** removing the
  STATUS derivation kills the false alarm but not the warning, which is born in
  `load_history` recomputing the expected chain against verbatim-inserted
  foreign hashes. Either merge re-chains what it accepts, or `chain_hash` is
  declared a purely local artefact and foreign values are dropped on the way
  in. Pick one here, or β ships with the warning still in the log.
- `dpc-client/core/dpc_client_core/conversation_monitor.py` — stop minting a
  hash for a message that arrives without one; mark it instead. Today the
  minting is announced honestly, and then persisted, so the next load sees a
  message that has a hash and says nothing. The log tells the truth once and
  the disk forgets it.
- `dpc-client/core/dpc_client_core/message_handlers/group_handler.py` —
  `GROUP_HISTORY_STATUS` v2 carrying the β digest (per Q2)
- **The other door into history.** `GROUP_CREATE` and `GROUP_SYNC` seed a
  group's history through the private `REQUEST_CHAT_HISTORY` → `import_history`
  path — a wholesale replace. `4d3b7442` closed the unsolicited half (a reply
  is accepted only against a request we made), but the newcomer — the one
  holding no evidence at all — still receives history by the weakest route.
  Either move the group calls onto `GROUP_HISTORY_*` or bring `CHAT_HISTORY`
  under the same merge rules. β's own criterion ("two honest nodes do not
  diverge") can be green while this is open.
- `specs/dptp_v1.md` — specify `GROUP_TEXT` and `GROUP_HISTORY_*`, which are
  not specified today, before adding to them
- `dpc-client/ui/src/lib/panels/ChatPanel.svelte` — provenance levels
- **Naming sweep perimeter:** code, spec, ADRs and living documents. Not the
  research archive — the prompts and reviews say "feeds" because that is what
  was said, and editing them afterwards falsifies the record of the discussion.
  The same principle by which ADR-036 refuses to re-sign old history.

## Scope (phase γ, when unblocked)

- STATUS relay in a star: `GroupHistoryStatusHandler` answers only its sender,
  so in B↔A↔C node C never sees B's status directly. Harmless for β — the
  pairs converge transitively through A — but once A's honesty is the question,
  it is not.

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| α — see [ADR-036](036-message-authenticity-signed-at-origin.md) | Done | `1f2c10cd` |
| β — digest form decided (Q2) | Done — V2, per author | `5b160a93` |
| β — order-independent digest, STATUS v2 | Done | `5b160a93` |
| β — "Chain broken" cause removed | Pending | — |
| β — stop minting hashes on load | Pending | — |
| β — group history off the private path | Pending | — |
| Naming swept out of code, spec, ADRs | Pending | — |
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
- **P5 — recover-before-write.** A node restored from a stale backup reuses a
  `seq` and forks its own chain, and the result is indistinguishable from
  deliberate equivocation. Promoted from an open question to a precondition on
  Fable 5's argument, which is the one that settles it: once backup restores
  happen at any noticeable rate, **"it was my backup" becomes the standard
  alibi of a real equivocator**. Fork evidence is the single mechanism that
  turns "we disagree" into "here is your signature", and it loses exactly as
  much accusatory force as the innocent explanation is plausible. So this is
  not ergonomics — it is the condition under which the evidence stays evidence.
  Mechanically: on joining or reconnecting, ask peers for **your own** head
  first; if theirs is ahead, adopt it and re-attach your tail before writing.

## Open Questions

- **Q1:** ~~Recover-before-write — requirement or later?~~ Resolved:
  precondition P5.
- **Q2:** Which form does β's digest take? The choice is three-way, not two,
  and it decides which Confirmation items are even meaningful and what falls
  under the firewall.
  - **V1 — one flat set digest** over the room's sorted `content_hash` values.
    32 bytes on STATUS; on mismatch, exchange id lists (the protocol already
    does this in `GOSSIP_SYNC`) and fetch what is missing. Leaks no metadata,
    firewall-neutral. Cannot say *whose* messages are missing.
  - **V2 — per-author digests** `{author: (count, digest)}`. Addressable
    refetch without any chronology; medium metadata exposure, so Q2's firewall
    question applies. **Recommended** — and the 2026-08-06 measurement supports
    it beyond convenience: a digest over `content_hash` inherits the v1
    preimage's field set, which excludes `role`, so it is immune by
    construction to the defect that keeps the current chain from converging.
  - **V3 — heads with a local `seq`.** Nearly γ's mechanics with maximum
    metadata, and a false promise: without v2 the `seq` is unsigned, so heads
    carry no adversarial weight at all — an author writes whatever it likes in
    them. Useful only as a hint for honest reconciliation. β must not appear to
    offer more than it does. **Do not choose.**
  — @CC
- **Q3:** `GROUP_TEXT` and `GROUP_HISTORY_*` are not in the spec at all.
  Specify the existing protocol before extending it? — @Ark
- **Q4:** ~~Mark minted hashes, or stop minting?~~ Resolved in β's scope: mark,
  do not mint — a computed hash that outlives its own warning is
  indistinguishable from a verified one.

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
