---
adr: 042
title: "An agent's tool calls belong to the node that ran them: sign a digest, ship no blob"
status: accepted
date: 2026-09-07
axis: honesty, collective
deciders: [Mike]
consulted: [CC_linux, CC]
informed: [Ark]
depends_on: [ADR-036]
related: [ADR-023, ADR-031, ADR-036]
supersedes: []
session: "DPC Project, 2026-09-07 — Mike: «tool calls не должны уезжать в группы … чужих tool calls агентов я видеть не должен ни в истории ни в чате, каждый видит только свои»"
---

# ADR-042 — An agent's tool calls belong to the node that ran them

> **Accepted (Mike, 2026-09-07): option C — sign the digest, ship no blob.** Implemented the
> same day; the three open questions below stay open and are his. CC — draft and
> implementation; CC_linux — measurement of the receiving side and the correction to what
> the UI actually does; Ark — review.

## Context

`tool_calls` is the record of what an agent did: for each call a `tool`, its `input`, its
full `output`, `is_error`, `duration_ms`, `round` and `round_text`. It is attached to the
agent's message in a group chat.

Three facts, measured on both nodes on 2026-09-07:

1. **It travels.** `service.py:5543-5564` builds one payload with `"tool_calls": tool_calls
   or []`, broadcasts it to the local UI (`:5568`) and relays **the same object** to peers
   as `GROUP_TEXT` (`:5577`). There is no separate shape for a peer.
2. **It is stored by the receiver and is one click from being read.** On the Linux node's
   disk, `history.json` of this group holds six records with `tool_calls`, all authored by
   the Windows node's agent — 52 foreign calls with their outputs, one of them 13 512 bytes
   of `execute_skill` with the skill name and request text inside (CC_linux). They reach the
   UI: that node's `ui.log` records `DIAG mapped: total=130, agents=65, withToolCalls=6`.
   The render gate is `ChatMessageList.svelte:188`, `isAiSender(...) &&
   msg.tool_calls?.length` — no comparison with `agent_owner`. **Precisely: rendered, not
   displayed.** `AgentProgressCollapsible` opens on `isLive`, which a stored message does
   not pass (`:48`), so a foreign agent's calls appear as a collapsed strip that any group
   member expands with one click. Mike, looking at that node, correctly reports not seeing
   them; CC_linux's first formulation («rendered to everyone») was withdrawn by him and is
   corrected here.
3. **It is the bulk of what is signed.** On the Windows node, the same group's 130 records
   carry 341 226 bytes of `content` and **1 845 639 bytes of `tool_calls`**, of which
   1 019 764 are tool outputs; the largest single output is 66 351 bytes. The signature
   covers 5.4× more machine protocol than conversation.

Mike's rule, stated 2026-09-07: **each node sees only its own agents' tool calls, in the
chat and in the history.** Today the opposite happens, silently, in both directions.

## Decision Drivers

- **D1 — a tool output is the owner's data.** It contains file contents, paths, skill
  inputs and errors from the owner's machine. In a privacy-first product it is the last
  thing that should be replicated to every group member by default. That it currently
  arrives folded rather than open changes who has noticed, not who holds it.
- **D2 — the audit trail must stay signed.** `tool_calls` entered the preimage in
  `50b8b6b6` for a named reason: in a star topology the relay is a real node forwarding
  someone else's message, and an unsigned field is one a relay rewrites unpunished
  («rewrite an agent's audit trail», `message_signing.py:1-8`).
- **D3 — a peer cannot verify what it does not hold.** While the preimage covers the blob,
  «signed» and «not shipped» are incompatible: the receiver must recompute the hash, so it
  must have the bytes (CC_linux).
- **D4 — a preimage change is not free.** Every existing record stays `dptp-msg-v1`, and
  verifiers keep both rules for good. We already carry one generation of unportable
  records; a second is a real cost, not a formality.
- **D5 — the migration is already forced.** Eleven records on the Windows node cannot be
  verified by anybody (their stored hash reproduces only without their `tool_calls`), and
  they are refused on every sync. Whatever is decided here, that population has to be
  addressed.

## Options

| | What it does | Meets Mike's rule | Audit trail signed | Cost |
|---|---|---|---|---|
| **A. Leave as is** | nothing | **no** | yes | none now; the leak stays |
| **B. Drop `tool_calls` from the preimage** | v2 preimage over the remaining fields; the blob still travels unless separately stripped | only if also stripped | **no** — a relay may rewrite it | v2 + two rules for ever |
| **C. Sign the digest, ship no blob** | preimage carries `sha256(_canonical_json(tool_calls))`; the record carries the digest; the blob stays on the owner's node | **yes** | yes | v2 + two rules for ever |
| **D. Ship the blob encrypted to the owner** | preimage unchanged over ciphertext | yes for reading, **no** for shipping | yes | key management; still 1.8 MB on the wire and on every disk |

**Recommendation: C.** It is the only option that satisfies D1 and D2 together, and it
also removes the cost measured in fact 3 — the signature becomes a constant 32 bytes where
it is now up to 220 KB for one message of 609 bytes of text.

**The trade C makes, stated plainly:** a peer verifies the author's signature over a digest
it cannot open. It stops being a witness to *what* the agent did and remains a witness to
*that the author committed to a specific record of it*. If the owner ever needs to prove
the content to a peer, it sends the blob then and the peer checks it against the digest it
already holds — the proof stays available, it is just no longer pushed by default.

## Open questions for Mike

- **Q1 — the records that already travelled.** 52 foreign calls sit on the Linux node's
  disk with their outputs. Deleting them from a history breaks the local chain and trips
  the deletion detector — the same argument that stopped us cutting #42
  (`consensus_manager` / `.chain_meta.json`, board entry
  `A-KNOWLEDGE-COMMIT-IS-APPROVED-BY-WHOEVER-VOTED-BEFORE-THE-DEADLINE`). Options: leave
  them (they are v1 and stay verifiable), or add an owner-side redaction that replaces the
  blob with its digest and re-signs under v2 — which changes the record's hash and so must
  be treated as a new record, not an edit.
- **Q2 — is the UI gate wanted as well?** Under C nothing foreign arrives, so the render
  gate is redundant for new records. It is not redundant for the 52 already on disk. Add
  the owner check to `ChatMessageList.svelte:188` regardless?
- **Q3 — the eleven unportable records.** They are unrelated to this decision in cause but
  share its migration window. CC_linux holds verifiable copies of ten of them; the cheapest
  repair is for Windows to take those copies rather than to exclude them for ever.

## Confirmation

- A group message from an agent, sent from node A to node B: B's `history.json` holds the
  record with a `tool_calls_digest` and **no** `tool_calls`; B's chat shows no tool calls
  for that message; B's verifier accepts the record.
- A's own record still shows its calls, and still verifies on A after a restart.
- A v1 record with a blob still verifies on both nodes after the change.
- The signed preimage of a message with 31 calls is the same length as one with none.

## What was built, 2026-09-07

`dptp-msg-v2`: the tenth preimage field is `sha256(_canonical_json(tool_calls))` instead of
that JSON. `LEGACY_PREIMAGE_VERSIONS` keeps v1 readable, and **every recomputation now reads
the version off the record** rather than assuming the running one — a verifier that assumes
its own version rejects everything written before it. The rule lives in one place,
`ConversationMonitor._recompute_hash`, used by both the incoming verifier and the
extraction-window check; two sites computing one hash their own way is the defect class this
pair spent two days inside.

The calls stop travelling: the agent post builds one payload for its own UI and relays a
copy without `tool_calls`, history export ships `tool_calls_digest` and never the calls, and
a receiver stores the digest beside the signature. The UI gate (Q2, taken as read) hides a
collapsible whose `agent_owner` is another node — the 52 records already on disk cannot be
unsent, but they can stop being one click from a reader.

A consequence worth naming: under v2 **writing calls into a record after it is signed is
harmless**, because the signature covers the digest taken at signing time. That is the exact
bug class behind #42, the eleven unportable records and both voting incidents, and it cannot
recur. Pinned by its own test.

Eight mutations of eight red; the three test files that encoded the v1 contract were
rewritten to the v2 one and keep a v1 case each, because eleven v1 records are still here.

## References

- `dpc-protocol/dpc_protocol/message_signing.py:1-8` — why the four fields were added
- `50b8b6b6` — «sign at the author, and take the author from the signature»
- `28ecd67a` — tool calls stored before signing, which is what made the blob load-bearing
- `service.py:5543-5577` — one payload for the UI and the wire
- `ChatMessageList.svelte:188`, `messageMapper.ts:115` — the render gate and the field
- Measurements: CC_linux (receiver side, 52 foreign calls), CC (owner side, 1 845 639 bytes)
