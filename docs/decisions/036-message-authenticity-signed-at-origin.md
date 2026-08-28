---
adr: 036
title: "Sign a message at its author, not at whoever stored it"
status: accepted
date: 2026-08-05
deciders: [Mike]
consulted: [Ark, CC, Fable 5, GLM 5.2]
informed: []
related: [ADR-006, ADR-037]
session: S64
---

## Context and Problem Statement

Mike asked a plain question about group chats: if one node edits its local copy
of the history, how does the other one find out?

The stated answer was three layers — per-message signature, `chain_hash` over
the local history, and a hash exchange on connect. Reading the code, the first
layer does not do what its name says, and the other two rest on it.

**A signature is applied by the receiver, with the receiver's key.**
`conversation_monitor._get_signer()` loads `~/.dpc/node.key` — always the local
node, no other branch — and `add_message()` signs *every* message that reaches
the monitor, including one that just arrived from a peer. Measured across ten
group histories on one node: `signer_node_id` is that node in all of them,
including twelve messages authored elsewhere. The signature attests to who
stored the message, not to who wrote it.

Three further holes sit in `merge_history()`:

1. `content_hash` is read from the incoming message and never recomputed from
   the received `content`, so altering the text while keeping the hash and
   signature passes;
2. `if sig and content_hash and signer:` — a message with those fields absent
   skips verification entirely, so rejection requires a *wrong* signature, not
   a missing one;
3. `verify_signature` is tri-state (`True` / `False` / `None` = "peer cert not
   cached, cannot verify") but only `False` is rejected, and `except Exception`
   logs at DEBUG and accepts.

Two external reviewers then found, independently, that all of that is moot on
the real path: `export_history()` ships a whitelist of twelve fields — five
unconditional and seven conditional — and `content_hash` / `signature` /
`signer_node_id` are not among them, so the verification branch has never
executed for a message arriving through `GROUP_HISTORY_RESPONSE`. And
`~/.dpc/peers/` was never written by any code path — the directory did not
exist — so `verify_signature` could only ever answer `None`.

Two properties that do hold, and that bound the severity:

- **The transport authenticates the author on a direct link.** The `node_id`
  handlers receive comes from `peer.node_id`, set after the HELLO challenge
  (CN, public-key fingerprint, RSA-PSS proof over a fresh nonce), and
  `payload["sender_node_id"]` is never read. Impersonation over a direct
  connection is not possible today.
- **Dedup by `message_id`** prevents an already-delivered message from being
  overwritten on a peer.

But in a star topology `group_handler` relays a payload verbatim, and the
receiving node takes the author from the transport — so a message authored by
B and relayed by A is recorded on C as A's. Attribution is already wrong there,
before any of this is fixed.

## Threat Model

Named because the value of this fix is a function of it, and without it the
fix reads as more than it is.

**In scope.** An active participant of the group holding a valid key, and a
relaying or compromised node. Concretely: A hands D a history in which what B
said has been rewritten, and D catches it by checking B's certificate; or C
receives B's message through A and attributes it to B rather than to A.

**Out of scope — and this is the honest boundary.** The operator of a node
against their own node. `~/.dpc/node.key` is an unencrypted PEM
(`load_pem_private_key(..., password=None)`), so an operator re-signs anything
of their own trivially. Nothing here changes that, and nothing here should be
read as claiming it does. Protecting a key from its owner's machine is a
different problem.

So the whole value is **cross-node attribution**: what a second node can prove
about what a first one says a third one said.

## Decision Drivers

- **Dunbar scale.** Groups are units to dozens of known participants; a
  consensus mechanism heavy enough for open membership is not paid for here.
- **Hub-optional.** No central authority may be required to establish who wrote
  what.
- **Non-atomic upgrade.** Nodes update weeks apart; mixed-version groups must
  keep working, and "old node" must stay distinguishable from "attacker
  imitating an old node".
- **Three platforms.** Windows, Linux, macOS run the same code and must produce
  the same bytes; an honest message must not reject for having crossed an
  operating system.
- **Prior art exists.** Every piece of this problem is solved somewhere with
  decades of production behind it. Novelty here is a defect, not a feature.

## Decision

**A message is signed by its author, at the moment of sending, over a canonical
preimage; a receiver verifies that signature and stores it rather than minting
its own.**

Concretely:

1. **Trust on first use for peer certificates.** The certificate a handshake
   already proved is persisted to `~/.dpc/peers/<node_id>.crt`. The store
   re-derives `node_id` from the public key instead of trusting its caller,
   because the outbound path validates CN alone and a CN is a claim.
2. **A canonical preimage** (`dptp-msg-v1`, `specs/dptp_v1.md` §4.1) covering
   ten fields, length-prefixed, with UTC-normalised timestamp and canonical
   JSON for `tool_calls`.
3. **Signature fields travel** in `GROUP_TEXT` and in `export_history()`.
4. **The receiver verifies and does not re-sign.** `add_message()` must accept
   supplied signature fields instead of overwriting them, or verification is
   immediately undone by storage.
5. **Relaying stays; attribution moves to the signature.** The author is taken
   from the signed payload, not from the socket. `signer_node_id` must equal
   `sender_node_id` — an invariant of v1 only: ADR-037 P3 introduces delegated
   agent keys, and at that point the check becomes validation of a delegation
   chain rather than equality. An unsigned message on a relay path is never
   attributed to the `sender_node_id` it claims; it is either attributed to the
   transport peer or shown as "claimed X, unverified".
6. **Unverifiable is not the same as invalid.** `None` and absent fields mark a
   message `unverified` / `legacy`; only a *wrong* signature is rejected.
   Enforcement turns on per group once every member advertises support.

### Gates on enforcement

Enforcement is the irreversible half, and three things must be settled before
it is switched on for any group. They are gates, not wishes.

- **The roster must be trustworthy, or enforcement is defeated by it.** With an
  ungated roster, any connected peer adds a phantom member; the phantom never
  advertises support; the group is pinned in the soft phase indefinitely, and
  the soft phase is where unsigned injection lives. Closed by `3e49b044`
  (GROUP_SYNC accepted only from a current member) — recorded here because
  enforcement depends on it, not because it belongs to this ADR.
- ~~**Q1 must be measured, not reasoned.**~~ Measured 2026-08-06 on a live
  three-node star, both before and after: see Q1 and Confirmation.
- ~~**Q5 must be decided.**~~ Decided 2026-08-06: the validity window is never
  checked. See Q5.

Where the advertised capability lives was undecided when this was written; it
is a `capabilities` field in HELLO (Q6). DPTP had no negotiation mechanism at
all, so "a wire format change" understated it — this is a mechanism that did
not exist.

### Rationale

The single fact that decides this: **a signature applied at storage cannot
express authorship**, because the storing node's key is the only key it has.
No amount of repair inside `merge_history` reaches that — a node can alter a
peer's text, and its own `add_message` will re-hash and re-sign the result
automatically, without intent and without anyone's key but its own. Verifying
harder against a signature the verifier's neighbour produced verifies nothing.

Moving the signature to the author is not a design of ours. It is what DKIM
does for mail (because SMTP relays rewrite the envelope), what Matrix does for
events, what Nostr and Secure Scuttlebutt do for every message they carry. The
same reasoning disposes of the relay question that looked like a fork: in all
of those protocols the transport was never the source of authorship, so
"relaying breaks the transport guarantee" is not a cost — it is the reason
origin signing exists.

Length prefixes rather than a separator: `"|".join` was injective only because
the field count was fixed. That is a property of the count, not of the
encoding, and the first optional field would have ended it silently. The
property should hold by construction, so that extending the field set does not
require re-deriving the argument each time.

Timestamp normalisation belongs inside the preimage because signer and verifier
that each spell the same instant their own way will reject honest messages —
Python writes UTC as `+00:00`, other stacks write `Z`.

`None` mapped to reject would be a denial of service against ourselves: with an
empty certificate store every peer message is unverifiable, and even with the
store filling, a message can legitimately arrive before its author's first
handshake. Both reviewers converged on flag-not-reject independently.

## Considered Options

- **Option A — repair `merge_history` only.** The original plan: recompute the
  hash, require the fields, reject on `None`.
- **Option B — sign at the author over a canonical preimage** (chosen).
- **Option C — adopt MLS (RFC 9420).**
- **Option D — restructure history into per-author append-only feeds (SSB
  model).**

### Pros and Cons of the Options

#### Option A — repair `merge_history` only

- Good: smallest change, ~20 lines, one function.
- Bad: **does not achieve its stated goal.** A node altering a peer's message
  re-signs it with its own key as a side effect of storing it; the receiver
  recomputes the hash (matches), verifies the signature (valid), accepts.
- Bad: unreachable in practice — `export_history` strips the fields the branch
  reads, so the repaired code would still verify nothing.

#### Option B — sign at the author over a canonical preimage

- Good: binds content to the author's key; the only option that answers Mike's
  original question.
- Good: matches DKIM / Matrix / Nostr / SSB, so the failure modes are known
  rather than discovered by us in production.
- Good: closes five fields that were forgeable under a valid signature —
  cross-group replay, author renaming, human-presented-as-agent, agent owner,
  and an agent's `tool_calls` audit trail.
- Neutral: changes the wire format, so it needs a capability flag and a staged
  rollout.
- Bad: does not fix retroactive editing of a node's *own* messages, nor
  deletion; those need Option D or a set digest.

#### Option C — MLS (RFC 9420)

- Good: an IETF standard with forward secrecy and post-compromise security.
- Bad: solves confidentiality and group membership, not authorship of stored
  history — the actual hole stays open.
- Bad: designed for groups up to 50 000; the machinery is not paid for at
  Dunbar scale.

#### Option D — per-author append-only feeds (SSB model)

- Good: ordering is defined by the author, so the order-dependent false
  divergence alarm disappears as a class.
- Good: a gap in an author's own sequence makes deletion detectable, which
  local `.chain_meta.json` cannot do — it is rewritten by whoever edits the
  history.
- Good: two messages at one sequence number are an equivocation proof rather
  than a conflict needing a vote.
- Bad: restructures history storage; too large to carry alongside the wire
  change.
- **Deferred to its own ADR**, and recommended.

## Consequences

- **Positive:** authorship becomes checkable without a hub or an authority;
  five previously uncovered fields come under the signature; the certificate
  store makes `verify_signature` able to answer at all.
- **Positive:** relayed messages get the right author instead of the relay's.
- **Negative:** wire format change — mixed-version groups need a capability
  flag and a soft phase, and during it a downgrade to "no fields, treat as
  legacy" is available to an attacker.
- **Negative:** every message currently on disk is signed by its holder;
  those cannot be repaired (no way to recover an author's signature after the
  fact) and stay `legacy` forever.
- **Neutral:** stored `content_hash` values change format; nothing reads them
  on any reachable path today — `merge_history` does read the field, but that
  branch is dead because the export strips it — which is what makes the change
  cheap now and expensive later.
- **Neutral:** `role` is deliberately outside the preimage although the old
  `chain_hash` covered it: per ADR-031 `role` is a per-reader rendering, not a
  property of the message, so signing it would bind a message to one reader's
  view of it.
- **Neutral:** verification states must reach the UI, or the cryptography
  changes nobody's decisions.

## Confirmation

- [ ] A message whose `content` is altered while `content_hash` and `signature`
      are left intact is rejected. Red on the pre-ADR code.
- [ ] A message whose `signer_node_id` differs from `sender_node_id` is
      rejected.
- [ ] A message with the signature fields absent is stored as `legacy`, not
      rejected, and not silently indistinguishable from a verified one.
- [ ] `verify_signature` returning `None` marks `unverified` and does not
      reject; re-verification happens once the certificate arrives.
- [ ] A relayed message in a three-node star is attributed to its author, not
      to the relay.
- [ ] The same instant written `Z`, `+00:00` and `+03:00` produces one
      preimage; verified on all three platforms, not only Windows.
- [ ] A peer certificate whose public key does not hash to the claimed
      `node_id` is refused by the store.
- [ ] A signed message from room X, placed inside a `GROUP_HISTORY_RESPONSE`
      for room Y, is rejected — the verifier recomputes the preimage with the
      `conversation_id` of the **destination monitor**, never the one carried in
      the message.
- [ ] The same instant at different clock precisions (milliseconds,
      microseconds, nanoseconds) canonicalises to one preimage.
- [ ] In a strict group, a message with no signature fields from a member that
      **did** advertise support is `unverified`; from a member that did not, it
      is `legacy`. The two are distinguishable — otherwise imitating an old
      node is the cheapest attack of the rollout.
- [ ] A member without the capability joining a strict group downgrades the
      group predictably or is refused entry — never silently both modes at once.
- [x] **Observed in production 2026-08-06.** On the same three-node star, a
      message sent from Linux is shown on macOS as `Mike (linux)
      (dpc-node-6d218e95…)`, and one sent from macOS is shown on Linux as
      `Mike (MacOS) (dpc-node-f9e0ec2d…)`. Before the change both read
      `Mike Windows PC` — the relay. Attribution is correct on both edges.
- [ ] Two nodes running the new code exchange a group message and both show it
      `verified` — the *status field*, not just the name, still to be read off
      a record.

## Scope

**Ordered, because two of these must land before the first signature exists.**

1. `dpc-client/core/dpc_client_core/service.py` — normalise `sender_name`
   (derive from the group / HELLO instead of the literal `"User"` at `:4516`)
   and `agent_owner` (node_id everywhere; today the monitor stores a node_id at
   `:4806` and the wire carries a display name at `:4824`). **Before signing
   starts.** Both fields are inside the preimage: sign first and the author's
   node signs one value while storing another, so its own `export_history`
   ships a history that fails to verify against its own signature — honest
   messages rejected on the first sync.
2. Everything below, in any order.

- `dpc-protocol/dpc_protocol/message_signing.py` — canonical preimage (new)
- `dpc-protocol/dpc_protocol/commit_integrity.py` — re-derive identity when
  loading a cached certificate
- `dpc-client/core/dpc_client_core/p2p_manager.py` — certificate persistence
- `dpc-client/core/dpc_client_core/service.py` — sign at send, carry the fields
  in `GROUP_TEXT`
- `dpc-client/core/dpc_client_core/message_handlers/group_handler.py` — verify
  on receive; author from the signed payload; relay unchanged
- `dpc-client/core/dpc_client_core/conversation_monitor.py` — accept supplied
  signature fields in `add_message`; export them in `export_history`; four
  checks in `merge_history`
- `dpc-client/core/dpc_client_core/p2p_manager.py` — emit an event when a peer
  certificate is first cached, so messages parked as `unverified` are
  re-verified rather than staying that way forever
- `dpc-client/ui/src/lib/panels/ChatPanel.svelte` — verified / legacy /
  unverified
- `specs/dptp_v1.md` §4.1 — the format
- Tests: cross-platform preimage determinism
  (`dpc-protocol/tests/test_message_preimage.py`, exists); a star-topology
  integration test for relay attribution (does not exist — Q1 cannot be closed
  without it)

## Implementation Status

**Signatures travel.** This preface read "nothing is signed on the wire yet"
for three weeks after `50b8b6b6` made it untrue: the table was written before
that commit and never revisited. Measured on this box 2026-08-28 — 360 of 535
stored records carry `preimage_version: dptp-msg-v1` with a signature and a
signer beside it. The rows below were re-checked against the code that day.

| Task | Status | Commit |
|------|--------|--------|
| Persist the peer certificate the handshake proved | Done | `bc2fbeb1` |
| Canonical preimage + spec §4.1 | Done | `d92f5012` |
| Roster gate — precondition for enforcement | Done | `3e49b044` |
| Normalise `sender_name` / `agent_owner` (before signing) | Done | `50b8b6b6` |
| Sign at send; fields into `GROUP_TEXT` | Done | `50b8b6b6` |
| Verify on receive; stop re-signing on store | Done | `50b8b6b6` |
| Author from the signed payload (relay) | Done | `50b8b6b6` |
| Re-check a parked `unverified` once the certificate arrives | Done | `reverify_author` |
| `export_history` + `merge_history` | Done | `50b8b6b6`, `f9b9b3dd` |
| Capability flag and staged enforcement | Pending | — |
| Verification states in the UI | Pending | — |

The last row stays Pending on the evidence: `group_handler` broadcasts a
`verification` field with every group message and nothing under
`dpc-client/ui/src` reads it. A verdict computed and never shown is the shape
this codebase produces most often.

## Open Questions

- **Q1:** ~~Does the relay decision hold once a three-node star is actually
  running? Reasoned from code, never measured.~~ **Measured 2026-08-06** on a
  live star — Windows in the middle, Linux and macOS on the edges with no link
  between them. Every message from either edge is recorded on the other under
  the relay's identity, and the UI shows the stored id: `dpc-node-86cdcd26…`,
  which is the relay's, not the author's. Symmetric in both directions, so on
  the edges the whole conversation reads as if the middle node said all of it.
  The reasoning from code was right; this is no longer inference.
- **Q2:** ~~Per-author feeds (Option D) — own ADR, or folded in later?~~ Answered:
  [ADR-037](037-author-attribution-chains.md), phased, and not called feeds.
- **Q3:** ~~`import_history` still replaces a conversation wholesale, though a
  reply is now only accepted against a request we made (`3e49b044`). Delete the
  path or bring it under the same verification rules?~~ **Brought under them,
  `f9b9b3dd`.** It was the wider of the two doors: no check at all, while
  replacing an entire conversation. The request registry proves we asked the
  question, never that the answer is honest. Both paths now pass one gate
  (`_verify_incoming`, spec §4.2), every record leaves it with a verdict, and an
  import that survives nothing is refused whole rather than emptying the
  conversation. It still replaces rather than merges — that half stays with
  ADR-037 phase β. — @CC
- **Q6:** ~~Where does an advertised capability live?~~ **Decided 2026-08-06
  (Mike): a `capabilities` field in HELLO.** Implementation with @Ark. Two
  details still belong to whoever builds it: the observed capability has to
  persist somewhere (HELLO is per connection and members go offline), and a
  strict group must behave predictably when a member without it joins —
  downgrade or refuse, never both modes at once.
- **Q4:** What clears a divergence flag after a merge that legitimately added
  nothing? — @Ark
- **Q5:** ~~Certificate expiry.~~ **Decided 2026-08-06 (Mike): the validity
  window is never checked.** A node certificate here is a proof of possession
  of a key, not a statement about a period of time — `node_id` *is* the key's
  fingerprint, and the key does not expire on a date. Enforcing a window would
  reject a correctly signed message for a reason unrelated to whether it was
  signed correctly, and would do it retroactively to the whole corpus on the
  day it was switched on.

## Authors

- **Mike** — Decision, and the question that started it
- **Ark** — Analysis, synthesis of the external reviews
- **CC** — Code audit, measurements, implementation
- **Fable 5, GLM 5.2** — External adversarial review

## References

- `specs/dptp_v1.md` §4.1 — canonical preimage
- `ideas/dpc-research/group-auth-review-prompt.md` — the review prompt (v4)
- `ideas/dpc-research/adr-036-signed-at-origin-review-glm52.md`,
  `ideas/dpc-research/adr-036-037-review-fable5.md` — reviews of this ADR
- `ideas/dpc-research/group-auth-review-response-fable5.md` — Fable 5 review
- `REVIEW-GLM-5.2-group-authenticity.md` — GLM 5.2 review
- `bc2fbeb1`, `d92f5012` — implemented steps
- Backlog: `MSG-SIGNATURE-IS-MINTED-BY-THE-RECEIVER`,
  `MSG-SIGNATURE-DOES-NOT-BIND-CONTENT`, `GROUP-RELAY-REATTRIBUTES-THE-AUTHOR`,
  `CERT-STORE-VALIDATES-BUT-READER-TRUSTS`, `MSG-CHAIN-NEVER-REACHES-THE-UI`
- [RFC 8785 — JSON Canonicalization Scheme](https://www.rfc-editor.org/info/rfc8785/)
- [RFC 9420 — Messaging Layer Security](https://en.wikipedia.org/wiki/Messaging_Layer_Security)
- [Secure Scuttlebutt — append-only feeds](https://ssbc.github.io/ssb-db/)
