---
adr: 050
title: "Redact personal data in the harness before any request goes to a model this node does not own"
status: proposed
date: 2026-11-11
axis: network, collective
deciders: [Mike]
consulted: [Ark, Johnny, CC]
informed: []
depends_on: [ADR-041]
related: [ADR-045, ADR-048]
supersedes: []
session: "DPC Project group chat, 2026-11-11 — epic PERSONAL-DATA-LEAVES-THE-MACHINE-UNREDACTED-TOWARD-EVERY-MODEL-THE-NODE-DOES-NOT-OWN"
---

# ADR-050 — Redact before a model the node does not own

> **Proposed, 2026-11-11.** Mike asked for this ADR and made one scope call (below, under
> Decided). Everything under Proposed is a design for review, not a decision. Nothing is
> built. Facts and licences are quoted from the cards, as read by CC on 2026-11-11.

## Context and Problem Statement

Whatever an agent or a chat sends to a cloud vendor, or to another node's local model,
goes as written: names, contacts, identifiers, keys. That covers the system prompt, the
history, tool inputs and results, and attachment text (epic card
PERSONAL-DATA-LEAVES-THE-MACHINE-UNREDACTED-TOWARD-EVERY-MODEL-THE-NODE-DOES-NOT-OWN).
Facts the cards add:

- **Compaction.** 7 of 9 agents name a cloud compaction summariser, all with compaction
  off (per the epic card).
- **Sleep findings.** `sleep_pipeline` writes each session finding to `results/` keyed by
  `archive_hash`; on a cloud sleep alias a masked finding would be stored and
  re-synthesised every night without its table (per Johnny's review, 2026-11-11).
- **Gateway.** The loopback model gateway (ADR-041) receives requests from an outside
  agent, not from our call sites (per Ark's review, 2026-11-11).
- **Related open cards:** THE-CONTENTS-OF-AN-AUTHENTICATED-PAGE-ARE-SENT-TO-AN-EXTERNAL-MODEL-FOR-SUMMARY,
  THE-SHARED-KNOWLEDGE-LAYER-REACHES-A-PEER-MODEL-UNFILTERED-BY-THE-NODE-RULE.

Candidate tools were read on 2026-11-11 from their LICENSE files, READMEs and model
cards; none of the six covers the whole request, the reverse table and fail-closed
together (per the epic card):

- presidio (MIT, a framework): English by default; Russian needs our own model and
  recognisers.
- LiteLLM with a presidio guardrail: MIT outside `enterprise/`, whose licence was not
  read; only `pre_call` masks the request.
- anon_proxy (MIT): its model's card says primarily English; it skips the system prompt,
  tool definitions and thinking.
- kiji-proxy (Apache-2.0): no Russian.
- cloud-ru guardrails-llm-filter and -extproc (Apache-2.0): regex and checksums for
  SNILS, INN and OGRN, no names; an unparsable body passes through unmasked.
- Natasha/Slovnet and spaCy `ru_core_news_lg` (MIT): Russian names.

## Decision

### Decided (Mike's call, 2026-11-11)

1. The node's own local engines are **not** redacted: everything stays on the machine.
2. Cloud vendors and other nodes' models **are** redacted.
3. Whether and where redaction applies is **set by the user in the UI**.

### Proposed (not decided; each needs Mike's yes)

- **P1. Where.** Redaction runs in the harness, before every call to a model of
  Decided 2, with stable placeholders (`PERSON_1`) and a per-conversation reverse table
  that stays on this node and is never sent.
- **P2. Restoration.** The table restores originals in the final answer, in streamed
  text, and in tool-call arguments **before the tool executes** (otherwise `run_shell`
  or `write_file` receives `PERSON_1`); our own chat display reads it too. Open check
  from the card: a placeholder must survive the model's paraphrase in Russian.
- **P3. Fail-closed.** If any part of the request could not be redacted, the call is
  refused, with a text that says why. Nothing passes through on a detector error (the
  cloud-ru proxy does, which is the behaviour to avoid).
- **P4. The exemption is "proven to run on this machine", computed in the backend** from
  `route`, `provider_kind` and `host_scope` together. It must never read the
  display-only `where` field of the agent dialog's `ModelRoute` (GLOSSARY: `where`,
  `host_scope`). Why not `provider_kind` alone: it is `vendor` for a LAN box, and a peer
  serving its own model is `route: peer, provider_kind: self_hosted`. A review said the
  `remote_peer` and `dpc_agent` types read `self_hosted`; read in `provider_facts.py` on
  2026-11-11 they do not: `LOCAL_TYPES` is `ollama`, `llamacpp_server`, `strata`,
  `local_whisper`, so those two types read `unknown`, and an exemption that requires a
  proven local engine already refuses them. An `openai_compatible` URL on `0.0.0.0`
  counts as loopback; the inventory child checks whether that is right.
- **P5. Detector chosen by measurement, not by its card.** A small labelled set from our
  own chats (synthetic names in grammatical cases, mixed Russian/English lines, IDs,
  keys). Two numbers: recall of persons, including that one name in two cases resolves
  to one identity; and false positives on our own text (agent names, node ids, agent
  ids, Windows paths, commit hashes, DOIs, domains), because a masked node id or path
  hands the agent a broken answer. Candidates: Natasha/Slovnet and spaCy `ru_core_news`,
  with presidio as the frame (card
  NO-PERSONAL-DATA-DETECTOR-HAS-BEEN-MEASURED-ON-OUR-MIXED-RUSSIAN-AND-ENGLISH-TEXT).
- **P6. Licences.** presidio code is MIT; the licence of Russian NER **weights** is
  separate and not yet checked (presidio's languages page says to verify it; the
  fasttext-supernova and `ru_core_news` weights are unread). The cloud-ru rules are
  Apache-2.0: port the SNILS/INN/OGRN checksums with attribution and a NOTICE entry.
  All six candidates are compatible one way into GPLv3. presidio is moving from
  Microsoft to a community organisation: pin by commit sha, and record the date and
  the registry.

### Rationale

One pass in the harness covers the call sites we own and sits where the answer and the
tool arguments are. A detector picked from a card would repeat the English-first gap
every candidate shows. Reading the exemption from `route`, `provider_kind` and
`host_scope` keeps display wording from deciding a privacy outcome, as the GLOSSARY
already says for billing.

## Doors

Egress paths named on the cards so far. The egress-inventory child
(EVERY-PATH-FROM-THE-NODE-TO-A-MODEL-IT-DOES-NOT-OWN-IS-UNLISTED) completes this list
through Orbit and grep, and notes per door which parts of the request ride along and
whether audio or images do (audio cannot be text-redacted).

- cloud providers (`providers/` vendor types);
- `remote_peer_provider.py` (another node's model);
- the loopback gateway's peer door and its cloud aliases (ADR-041);
- auxiliary summarisers: snapshot and compaction;
- sleep and knowledge extraction on a remote alias;
- transcription sent to a peer;
- a place masked text may persist: the sleep findings store.

## Open Questions

1. **LAN host.** Where does `host_scope: lan` fall: redacted like a vendor, or exempt?
   Another machine of the owner's is not this machine. Mike to decide.
2. **No detector model.** On a node without the model (offline, first run): block every
   cloud call, or ship the model in an extra? P3 forces the choice before the code
   says "block".
3. **The gateway door.** Is the loopback gateway (ADR-041) in scope when it serves a
   cloud alias? Its requests come from an outside agent, so a pass on our call sites
   does not cover it.
4. **Sleep findings on disk.** May the findings cached in `results/` hold placeholders,
   and if so, where does their table live?

## Consequences

- **Positive:** a cloud or peer model sees placeholders for what the detector finds;
  the owner's own engines keep full context and full speed.
- **Negative:** every call to a non-exempt model pays a detection pass; what the
  detector misses still leaves (recall is a measured number, not a promise); a
  refused call is a visible failure.
- **Neutral:** the user's UI setting (Decided 3) is a new surface and needs a GLOSSARY
  row for the name chosen.

## References

Cards in `backlog.md` (gitignored), all 2026-11-11:

- PERSONAL-DATA-LEAVES-THE-MACHINE-UNREDACTED-TOWARD-EVERY-MODEL-THE-NODE-DOES-NOT-OWN (epic)
- EVERY-PATH-FROM-THE-NODE-TO-A-MODEL-IT-DOES-NOT-OWN-IS-UNLISTED
- NO-PERSONAL-DATA-DETECTOR-HAS-BEEN-MEASURED-ON-OUR-MIXED-RUSSIAN-AND-ENGLISH-TEXT
- A-REDACTED-REQUEST-HAS-NO-TABLE-TO-PUT-THE-NAMES-BACK-INTO-THE-ANSWER

Code: `dpc_agent/provider_facts.py` (`provider_kind`, `url_host_scope`, `LOCAL_TYPES`).
