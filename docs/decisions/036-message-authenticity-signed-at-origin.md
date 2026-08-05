---
adr: 036
title: "Sign a message at its author, not at whoever stored it"
status: proposed
date: 2026-08-05
deciders: [Mike]
consulted: [Ark, CC, Fable 5, GLM 5.2]
informed: []
related: [ADR-006]
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
the real path: `export_history()` ships a whitelist of seven fields and
`content_hash` / `signature` / `signer_node_id` are not among them, so the
verification branch has never executed for a message arriving through
`GROUP_HISTORY_RESPONSE`. And `~/.dpc/peers/` was never written by any code
path — the directory did not exist — so `verify_signature` could only ever
answer `None`.

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
   `sender_node_id`.
6. **Unverifiable is not the same as invalid.** `None` and absent fields mark a
   message `unverified` / `legacy`; only a *wrong* signature is rejected.
   Enforcement turns on per group once every member advertises support.

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
  today, which is what makes the change cheap now and expensive later.
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
- [ ] Two nodes running the new code exchange a group message and both show it
      `verified` — observed in production, not only in tests.

## Scope

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
- `dpc-client/ui/src/lib/panels/ChatPanel.svelte` — verified / legacy /
  unverified
- `specs/dptp_v1.md` §4.1 — the format

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| Persist the peer certificate the handshake proved | Done | `73a48a20` |
| Canonical preimage + spec §4.1 | Done | `634e13e1` |
| Sign at send; fields into `GROUP_TEXT` | Pending | — |
| Verify on receive; stop re-signing on store | Pending | — |
| Author from the signed payload (relay) | Pending | — |
| `export_history` + `merge_history` | Pending | — |
| Capability flag and staged enforcement | Pending | — |
| Verification states in the UI | Pending | — |

## Open Questions

- **Q1:** Does the relay decision (author from the signed payload) hold once a
  three-node star is actually running? Reasoned from code, never measured — no
  third node has been stood up. — @Mike / @CC
- **Q2:** Per-author feeds (Option D) — own ADR, or folded in later? — @Mike
- **Q3:** `import_history` on the private-chat path (`chat_history_handlers.py`)
  replaces local history wholesale with no verification. Delete the path or
  bring it under the same rules? — @CC
- **Q4:** What clears a divergence flag after a merge that legitimately added
  nothing? — @Ark
- **Q5:** Certificate expiry: `verify_signature` loads a certificate without
  checking its validity window. Harmless now; the day anyone enforces it, every
  old message flips to reject. — @CC

## Authors

- **Mike** — Decision, and the question that started it
- **Ark** — Analysis, synthesis of the external reviews
- **CC** — Code audit, measurements, implementation
- **Fable 5, GLM 5.2** — External adversarial review

## References

- `specs/dptp_v1.md` §4.1 — canonical preimage
- `ideas/dpc-research/group-auth-review-prompt.md` — the review prompt (v3)
- `ideas/dpc-research/group-auth-review-response-fable5.md` — Fable 5 review
- `REVIEW-GLM-5.2-group-authenticity.md` — GLM 5.2 review
- `73a48a20`, `634e13e1` — implemented steps
- Backlog: `MSG-SIGNATURE-IS-MINTED-BY-THE-RECEIVER`,
  `MSG-SIGNATURE-DOES-NOT-BIND-CONTENT`, `GROUP-RELAY-REATTRIBUTES-THE-AUTHOR`,
  `CERT-STORE-VALIDATES-BUT-READER-TRUSTS`, `MSG-CHAIN-NEVER-REACHES-THE-UI`
- [RFC 8785 — JSON Canonicalization Scheme](https://www.rfc-editor.org/info/rfc8785/)
- [RFC 9420 — Messaging Layer Security](https://en.wikipedia.org/wiki/Messaging_Layer_Security)
- [Secure Scuttlebutt — append-only feeds](https://ssbc.github.io/ssb-db/)
