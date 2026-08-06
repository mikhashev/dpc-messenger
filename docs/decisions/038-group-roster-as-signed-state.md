---
adr: 038
title: "Make the group roster signed state, and give it an owner"
status: proposed
date: 2026-08-06
deciders: [Mike]
consulted: [Ark, CC]
informed: []
depends_on: [ADR-036]
related: [ADR-022, ADR-023, ADR-037]
session: S65
---

## Context and Problem Statement

Mike stated the model he expects: **the group creator manages the node roster;
each member node manages its own agents; the creator may remove another node's
agent.** Measured against the code on 2026-08-06, one third of that holds and
the rest is not expressible.

| action | authority check | where |
|---|---|---|
| delete the group | creator only, locally **and** on the wire | `group_manager.py:456`, `group_handler.py:430` |
| add a member | **none** | `group_manager.py:393`, `service.py:5015` |
| remove a member | **none** | `group_manager.py:418`, `service.py:5078` |
| set this node's agents | own node only, correct by construction | `service.py:4463` |
| accept a peer's roster | higher version wins, **whole record replaced** | `group_manager.py:507` |

The only gate that exists is the one added by `4d3b7442`: `GROUP_SYNC` is
accepted only from a node already on the local roster. That answers *is this a
stranger*, never *was this node allowed to make this change*.

**The problem is not a missing `if`.** `created_by` is an ordinary field of the
same synced payload (`to_dict` is `asdict(self)`, `from_dict` reads it back at
`:46`), and `apply_sync` replaces the entire record when the remote version is
higher. So the record of who the creator is can be rewritten by exactly the
party a creator check would be defending against. On the current data model
"only the creator" cannot be stated, let alone enforced.

**Derived from the code, not reproduced:** a member sends `GROUP_SYNC` with
`created_by` set to itself and `version = local + 1`; every peer adopts it; the
existing creator-only checks on delete now pass for the new owner. The two gates
that do exist rest on a field the wire can overwrite.

Two further facts frame the work:

- **`specs/dptp_v1.md` does not mention `GROUP_SYNC` at all** — no command, no
  acceptance rules. Group membership travels outside the specified protocol,
  which is why no rule about authority had anywhere to live.
- **ADR-037 already recorded this**, from the other end. Precondition **P1 —
  membership as signed state**: *"A room is an overlay of N chains and the list
  of N must be derivable and signed. Without it, collusion fabricates the room
  rather than the speech."* We reached it from message forgery; Mike reached it
  from roster permissions. Same hole, two directions, and it is why γ is
  deferred rather than built.

## Decision Drivers

- **Authority must survive relaying.** Group traffic is a star: `_relay_to_group`
  forwards syncs on behalf of nodes that cannot reach each other. A rule based on
  the TLS peer identity would reject every relayed change, so the authority has
  to travel with the change, not with the connection.
- **`created_by` must stop being wire-mutable**, or no creator rule means anything.
- **The mechanism already exists.** Node keys, `CommitSigner`, and the TOFU
  certificate store (`~/.dpc/peers/`) landed with ADR-036. This needs no new key
  material.
- **A member manages its own agents.** That part works today and must not
  regress into "ask the creator for everything".
- **P1 for γ should be satisfied by the same work**, not by a second mechanism
  with the same shape.

## Decision

**The roster becomes signed state: `created_by` is pinned at first sight and
never taken from the wire, and every change to group metadata carries a
signature from the node entitled to make it.**

Three parts.

**1. `created_by` is pinned, not synced.** On the first `GROUP_CREATE` or
`GROUP_SYNC` that creates the local copy, `created_by` is recorded and thereafter
treated as immutable: an incoming payload proposing a different creator is
rejected outright and logged, whatever its version. Ownership transfer, if it is
ever wanted, becomes an explicit signed operation rather than a side effect of a
version bump.

**2. Authority is per field, and is checked before the change is applied.**

| field | who may change it |
|---|---|
| `members` | the creator |
| `topic` | the creator |
| `name` | **nobody, after creation** |
| `agents[X]`, `agent_names[X]` | node X, for its own entry — **or** the creator, to remove |
| `created_by` | nobody, after creation |

`name` is immutable by intent rather than by caution (Mike, 2026-08-06): one
group is one project, and a renameable group is the first step toward the
sprawl of near-duplicate channels that the design is meant to avoid. A project
that has become a different project is a different group.

`apply_sync` stops being a wholesale replace. It diffs the incoming record
against the local one and applies each changed field only if the signer of the
change is entitled to that field. A change with no valid signature is not
applied, and the local copy is kept.

**3. Every change is signed by its author, not by its carrier.** The signature
covers `(group_id, version, the changed fields)` under the same preimage
discipline as ADR-036 — canonical field order, explicit version tag — and is
verified against the signer's cached certificate. Relaying then costs nothing:
the relay cannot alter what it carries.

### Rationale

Signature rather than connection identity is forced by the star topology: a
creator-only rule enforced against the TLS peer would reject every roster change
that arrived via a relay, which is most of them in a three-node star where two
edges cannot see each other. This is the same move ADR-036 made for messages —
authority at the origin, transport merely a courier — and it is the reason that
ADR is a dependency rather than a cross-reference.

Pinning `created_by` is listed first because it is the cheap half and it is what
makes the other two halves meaningful. Without it, part 2 checks a field the
attacker supplies.

Per-field authority rather than a single "roster owner" signature keeps the
third of Mike's model that already works: a node adds its own agents without
asking anyone, and the creator retains a veto by being entitled to remove.

## Considered Options

- **Option A — signed operations log.** Each change is a signed operation
  appended to a per-group log; state is the fold of the log. Signal group v2's
  shape.
- **Option B — signed state with per-field authority** (chosen). The record stays
  a record; each field carries the signature of whoever is entitled to it.
- **Option C — local check only.** Verify `created_by` before applying a roster
  change, no signatures.
- **Option D — accept the current behaviour** and document that any member may
  edit the roster.

### Pros and Cons of the Options

#### Option A — signed operations log

- Good: full history of who changed what and when; concurrent changes merge by
  operation rather than by version number; the proven shape.
- Good: subsumes P1 completely and gives fork evidence for free.
- Bad: a second append-only log per group, with its own growth and compaction
  problem — the same pathology ADR-037 P4 already names for agent chains.
- Bad: changes the wire format and the on-disk format at once, for a group size
  that is three.

#### Option B — signed state with per-field authority

- Good: keeps the existing record, version and sync path; the diff is local.
- Good: satisfies P1 — the member list becomes derivable and attested.
- Good: no new storage; signatures ride the payload already being sent.
- Neutral: concurrent changes still resolve by version and content-hash
  tie-break, which is unchanged and adequate at this size.
- Bad: no record of *who changed what* over time; only the current state is
  attested. If that history is wanted later, Option A is the upgrade path.

#### Option C — local check only

- Good: an hour of work.
- Bad: **does not hold.** `created_by` arrives in the same payload, so the check
  validates the attacker's own claim.
- Bad: breaks relayed changes if hardened to use the TLS peer identity instead.

#### Option D — do nothing

- Good: nothing to build.
- Bad: any member can remove any other member, including the creator, and it
  propagates as a routine version bump.
- Bad: leaves P1 open, so γ stays blocked for a second reason.

## Consequences

- **Positive:** Mike's model becomes expressible and enforced; ADR-037's P1 is
  satisfied by work already wanted for its own sake; `GROUP_SYNC` acquires a
  specification.
- **Positive:** the two authority checks that exist today (`delete_group`,
  `GROUP_DELETE`) stop resting on a wire-mutable field.
- **Negative:** a node that predates this sends unsigned changes. They must be
  accepted or rejected by an explicit rule, not by accident — see Q1.
- **Negative:** if the creator's node is lost for good, the roster freezes at its
  last state. Ownership transfer is deliberately out of scope here; the escape
  today is that a group can be recreated.
- **Neutral:** per-field diffing makes `apply_sync` longer and more explicit;
  the version and tie-break rules are untouched.
- **Negative, and it is a change of failure mode (@Ark):** wholesale replace
  fails loudly — the group looks wrong. Per-field apply can fail quietly — the
  group looks right and one field silently did not move. Every refused field
  needs a log line, and the tests need a case for partial refusal, not only for
  correct application.
- **Neutral:** the transition has to be coordinated by whoever runs the nodes,
  not by the protocol. Every node must be updated before unsigned changes stop
  being accepted; with three nodes that is an hour, with ten it is planning.

## Confirmation

- [ ] A `GROUP_SYNC` proposing a different `created_by` is rejected at any
      version, and the rejection is logged at WARNING with both node ids.
- [ ] A member that is not the creator cannot add or remove a member: the change
      is refused locally and, if it arrives on the wire, is not applied.
- [ ] A node can still add and remove **its own** agents without the creator.
- [ ] The creator can remove another node's agent.
- [ ] A roster change relayed by a third node is accepted when the signature is
      the creator's, proving the rule survives the star.
- [ ] A roster change whose signature does not verify leaves the local copy
      unchanged.
- [ ] A **signed but stale** change does not roll the roster back: the version
      rule still applies after signatures are added, and the refusal is logged.
- [ ] A field that fails its authority check leaves a log line. Per-field apply
      replaces "replaced the wrong thing", which is visible, with "quietly did
      not apply", which is not — so silence is the failure mode to design
      against (@Ark).
- [ ] The member list is derivable and attested — P1 of ADR-037 marked satisfied
      there, not only here.

## Scope

- `dpc-client/core/dpc_client_core/managers/group_manager.py` — pin
  `created_by`; per-field diff and authority in `apply_sync`; sign on mutation.
- `dpc-client/core/dpc_client_core/message_handlers/group_handler.py` —
  verification before apply; keep the existing membership gate from `4d3b7442`.
- `dpc-client/core/dpc_client_core/service.py` — `add_group_member` /
  `remove_group_member` refuse when this node is not the creator.
- `dpc-protocol/dpc_protocol/message_signing.py` — preimage for a roster change.
- `specs/dptp_v1.md` — document `GROUP_CREATE`, `GROUP_SYNC`, `GROUP_DELETE`,
  their payloads and acceptance rules. They are absent today. The acceptance
  rules move **out of the code and into the spec** (version precedence,
  content-hash tie-break, per-field authority) rather than being described
  loosely — otherwise the two drift apart again within a month (@Ark).
- `dpc-client/ui` — the creator-only affordances become the truth rather than a
  hint; a refused change needs a message.

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| ADR drafted | Done | — |
| `created_by` pinned | Pending | — |
| Per-field authority in `apply_sync` | Pending | — |
| Signed roster changes | Pending | — |
| `GROUP_SYNC` in the spec | Pending | — |

## Open Questions

- **Q1 — open.** What happens to an unsigned change from a node that predates
  this — accepted as legacy, or refused? — @Mike

  **CC recommends refusing, against Ark's legacy-accept-with-deadline**, on a
  precedent from this codebase rather than on principle. ADR-036 fixed exactly
  this shape: `if sig and content_hash and signer:` meant a message with those
  fields *absent* skipped verification entirely, so rejection required a wrong
  signature rather than a missing one. "Accept unsigned as legacy" is that line
  again — the check becomes optional, and anyone wanting to bypass it omits the
  signature and is treated as an old node. A transition window is a window for
  the attacker too, and it cannot be told from a genuine old node.

  What makes refusing affordable here and not for messages: roster changes are
  rare and deliberate, so a hard cutover costs one coordinated update rather
  than a stream of dropped traffic. The cost is real and should be stated: until
  every node is updated, a roster change made on an old node does not reach the
  new ones.

- **Q2 — closed (Mike, 2026-08-06): out of scope.** A pinned `created_by` means
  a lost creator freezes the roster, and the way out is to recreate the group.
  Accepted deliberately: under the Dunbar-scale model of `VISION.md` and
  `dpc-full-picture-s32.md` a creator is a person you know, not an anonymous
  account that silently disappears.

- **Q3 — open.** Does this ADR carry the `session_started_at` marker as well? —
  @Mike

  Concretely, what it buys: three nodes agree a New Session, all vote yes, all
  clear. One node's process dies a second after voting and before
  `NEW_SESSION_RESULT` reaches it. It returns holding the whole history while
  the others hold none — and the next sync hands its copy back to them, undoing
  the reset with nobody noticing. The marker turns the reset from a message into
  a fact about the group: the returning node sees a `session_started_at` newer
  than its own history and clears itself. It is the same field of the same
  record, riding the same sync.

- **Q4 — closed (Mike, 2026-08-06):** `topic` creator-only, `name` immutable
  after creation. See the authority table.

## Authors

- **Mike** — Decision, requirement ("only the creator manages nodes; nodes add
  their own agents; the creator may remove them")
- **CC** — Measurement, draft
- **Ark** — Review

## References

- [ADR-036](036-message-authenticity-signed-at-origin.md) — sign at the author;
  the key material and preimage discipline this builds on
- [ADR-037](037-author-attribution-chains.md) — precondition **P1, membership as
  signed state**, which this ADR is the answer to
- [ADR-023](023-group-chat-participant-model.md) — introduced
  `agents: {node_id: [agent_ids]}` and **explicitly deferred to "Phase 3" the
  richer multi-node format that this ADR now defines**. Not merely related: 038
  is the deferred half of 023.
- `ideas/dpc-full-picture/dpc-full-picture-s32.md` §13 — the threat model this
  ADR serves, in the project's own words: *"a zero-trust/hostile-federation
  threat model in place of OHS's cooperative-Alliance assumption"*, with
  *"cryptographic identity derived from a keypair (trust without prior
  introduction)"*. Dunbar-scale trust is the social layer; the wire is not
  assumed friendly.
- [ADR-022](022-multi-agent-safety-governance.md) — Layer 2, "governance in the
  wire, not in the model"; a roster change fits as another signed action
- `4d3b7442` — the existing gate: `GROUP_SYNC` only from an existing member
- `backlog.md` — `GROUP-ROSTER-HAS-NO-OWNER`, with the measurement above
