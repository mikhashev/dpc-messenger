---
adr: 039
title: "Keep the backlog a local document and announce its changes as events"
status: accepted
date: 2026-08-10
axis: honesty
deciders: [Mike]
consulted: [CC, Ark, Warren, Fable 5, GLM 5.2]
informed: []
depends_on: []
related: [ADR-036, ADR-037, ADR-038]
supersedes: []
session: S72
---

# ADR-039: Keep the backlog a local document and announce its changes as events

## Context and Problem Statement

A project in this family may be developed by **one node** — one human with their agents — or
by **several nodes**, each with their own human and agents, coordinating in a DPC group chat
or working independently. The backlog is the artefact that has to survive that difference, and
today it is built entirely for the first case.

What exists: `backlog.md` (233 entries at the time of writing), `backlog_closed.md`, a format
standard (`docs/BACKLOG_FORMAT.md`), and one stdlib-only script, `tools/backlog/build.py`
(1326 lines), that parses, validates (`--check`), and renders a board, a link graph and
`graph.json`. The same script serves six projects. Two of the six backlogs, including this
one, are gitignored — for security, not convenience: `.gitignore:134-136` says the file
carries unpatched security findings, and the repository is public.

Three questions arrived together and are answered here as one, because separating them
produced answers that contradicted each other:

1. **Should writes go through one entry point** — a script that validates, allocates, and
   writes — so that six projects share one format by construction rather than by discipline?
2. **Should entries carry a generated identifier** (`DPC-142`) instead of a
   `SCREAMING-KEBAB` sentence?
3. **What happens when more than one node maintains the same backlog**, and does the answer
   have to be built out of DPC's own machinery rather than out of git?

The prompting evidence was three live defects found while asking: a duplicate entry name the
checker cannot see, a colon inside a name that breaks the parser and orphans every reference
to it, and a priority Mike set in words on 2026-07-17 that the tool has never displayed
because the envelope opens with `**`. **Two are name-shape defects and the third is an
envelope-shape defect** — the distinction matters, because a rule about names cannot catch
it, and the first draft of this ADR made exactly that mistake. None of the three is caught
today.

## Decision Drivers

- **The file opens in any editor.** No script can be "the only way in", so whatever guarantees
  the format cannot be the writer.
- **Agents are first-class readers.** An entry frequently arrives as a bare retrieval chunk
  without its section heading — this is why status is duplicated into the heading and why
  per-entry front matter was rejected.
- **`build.py` has zero third-party dependencies and no virtualenv**, which is what lets one
  copy serve six projects with six toolchains.
- **There is no CI, and two backlogs are gitignored**, so a pre-commit hook cannot be the
  design for all six.
- **The platform has never converged a concurrently-edited artefact.** Measured, twice:
  group history reconciles by `message_id` and drops the divergent copy
  (`conversation_monitor.py:2767-2772`, the open entry `HISTORY-DIVERGENCE-IS-PERMANENT`);
  skills are saved with an unconditional `write_text` and no version comparison
  (`skill_store.py:193`, four callers, zero version reads).
- **The threat model for a work list is accident, not forgery** — two writers clobbering each
  other, not a peer lying on the wire.

## Decision

**The backlog stays a local document. Its changes are announced as events. Nothing about it
rides the message-sync machinery.**

1. **Entry point.** `add`, `close`, `move` and `rename` become subcommands of `build.py`
   itself, not a sibling script. `close` is built first: it is the operation with total
   measured non-compliance — 83 closure lines in the archive, none carrying a resolution
   token from the six the standard defines. The subcommands run `--check` after every
   mutation, and a mutation fails on **refusals only, never on warnings** — the live file
   carries 102 warnings today, and a `close` that recites them every time is how people learn
   to stop reading the output.
2. **The checker is the guarantee; the script is convenience.** The script makes the common
   path correct by construction; the checker reads whatever ended up in the file, whoever put
   it there.
3. **Refusals split by defect class rather than only by date.** Structural defects — a
   duplicate name, a name the parser cannot round-trip, an unknown section — refuse
   regardless of when the entry was written, because they corrupt the graph for every reader
   today. Envelope incompleteness stays date-gated, as the migration policy requires. A
   name-charset refusal (`^[A-Z0-9][A-Z0-9-]+$` on the parsed name) is added, and the closure
   line check learns to read the archive. Two further one-line repairs belong to the same
   set, and neither was in the first draft:
   - **The duplicate check is rekeyed on the `NAME_RE` match rather than the full parsed
     name.** Today it compares the whole string left of the first `:` or `—`, so an aside on
     one copy (`SHUTDOWN-PIPE-DRAIN (original triage, S143 2026-05-23)`) makes two copies of
     one name compare unequal. Without this the charset refusal reports that heading as a
     malformed name and still never says "duplicate".
   - **Markdown emphasis is stripped from the envelope before the priority, status and origin
     are read**, which recovers the third defect rather than refusing it — the information is
     present and only the decoration hides it. New entries additionally refuse on emphasis
     inside the envelope, so nothing comes to depend on the tolerance.
4. **Identifiers stay `SCREAMING-KEBAB` sentences.** Generated identifiers are deferred behind
   named triggers rather than rejected forever (see Consequences).
5. **People are recorded as events, not as state.** No `owner:` field. `close` records who
   closed it in the closure line; `move` **appends** a body line (`taken: <actor> · <date>`)
   and never replaces one, so the record is a history rather than a field. The current
   assignee is derived from the last such line and is not editable, which is the property
   that keeps it from rotting the way `Updated:` did.
6. **One validator, two formats.** `--check` is extended to `docs/decisions/`. That closes
   `ADR-SCHEMA-1` as `fixed` — it asked for front-matter validation and gets exactly that —
   and `ADR-LINT-1` as **`superseded`**, not `fixed`: it asked for markdownlint plus a
   pre-commit hook, and a stdlib structural pass is a different thing. This is among the
   first closures written under the six-token vocabulary, and the precedent has to be honest.
   The ADR keeps its per-file front matter, the backlog keeps its heading envelope: the
   record boundary differs, so the metadata placement differs.
7. **Multi-node: the file is the artefact, the group chat carries the events.** Each
   subcommand emits a one-line announcement — `close FILE-TRANSFER-… · fixed · abc1234 · CC`
   — into the group chat, which is already authenticated, already ordered, and already where
   decisions are made. The announcement is **a text line in the closure-line grammar and
   nothing else**: no JSON payload, deliberately, because a payload is an invitation to grow
   the announcements into the sync channel this decision declines to build.
   Reconciliation of the *text* is git's job.
8. **No hosted remote, and the synchronisation question stays open in the right place.**
   Mike's decision, 2026-08-10: the gitignored files stay where they are for now, and the
   direction to think in is synchronisation over DPC itself rather than GitHub or GitLab.
   That direction already has a design in this repository —
   [`ideas/git-sync-plan.md`](../../ideas/git-sync-plan.md), 411 lines: `GIT_COMMIT_ANNOUNCE`
   / `GIT_SYNC_REQUEST` / `GIT_COMMIT_DATA` over the existing six-tier transport, with the
   commit announcement doing exactly what item 7 does for backlog mutations. It is a plan,
   not an implementation, and this ADR does not adopt it — it records that the multi-node
   answer belongs there and not here.
9. **No agent `ToolEntry` for the backlog now.** When an embedded agent does need to write
   one, build it the way skills are built — a permission-flagged internal write path, not a
   write tool.
10. **The platform gap is recorded separately and is not this ADR's to solve.**

### Rationale

The decisive question was not "document or conversation" in the abstract; it was what the
message machinery actually does with an edited record. It drops it. `merge_history` calls
`add_message_with_id`, which returns `False` for any id it has already seen — no field
comparison, no diff, no log of the divergence. A backlog on that transport is not one shared
backlog; it is *N independent backlogs that happen to share ids*, and every edited entry
diverges permanently and silently.

The skills subsystem looked like a precedent for the opposite conclusion, and reading it
settled the question in the same direction. Skills do not share a mutable artefact: on import
the copy's provenance is rewritten and its `shareable` flag is forced off, so the copy forks
from its origin by design and nothing ever reconciles them. Where two copies *can* both be
edited — both carrying `source: peer` — `save_skill` is an unconditional write, the manifest
carries a `version` integer that no write path reads, and the second write wins in silence.
For a strategy file that is tolerable, arguably correct. For a work list, silent divergence is
the definition of failure.

That leaves git, and the objection that git means a central server, which a peer-to-peer
product should not need. The objection dissolves on inspection: git is itself distributed, the
hosted remote is a convenience, and a bundle is a file that this product already knows how to
move between peers. What git supplies that nothing here supplies is twenty years of merge
semantics for text — and the prose bodies are the part of an entry the standard calls the
value.

Announcing the events into the group chat is the honest half of "the backlog is a
conversation". Events replay cleanly; prose edits do not, and pretending otherwise is what
would require inventing versioned records, parent pointers, a replay materialiser and a
per-field conflict rule — a CRDT-shaped project, to make a work list slightly more idiomatic.

## Considered Options

- **Ride the DPC message machinery** (signed per-author records, merge by record id).
  Rejected: verified to drop the second version of any edited record; would import the
  measured `HISTORY-DIVERGENCE-IS-PERMANENT` failure into the one artefact whose purpose is
  that everyone sees the same list.
- **Copy the skills sharing model** (P2P distribution with provenance and an opt-in gate).
  Rejected: skills fork on import by design and have no convergence story; the case that
  breaks — two received copies both edited — has simply never happened.
- **Generated identifiers (`DPC-142`) plus a human sentence.** Deferred, not rejected. The
  argument that carried it — that numbering collapses the reference-scanning heuristics to
  one regex — is false for any migration that preserves history, because the old names must
  survive as aliases and aliases must be resolved. The heuristic layer was also measured
  smaller than claimed: the whole reference-extraction block is 76 non-blank lines.
- **A private companion git repository.** Both external reviewers proposed it and Mike
  declined it: the files stay where they are, and the direction to explore is synchronisation
  over DPC (`ideas/git-sync-plan.md`), not a hosted remote under another name. Recorded here
  because it was the reviewers' unanimous recommendation and the reason for not taking it is
  a product choice, not a technical objection. **Not tested:** that a `git bundle` transits
  the existing peer file transfer unmodified. It is a file and the transport moves files, but
  size limits, extension filters and the firewall gate were not checked; the claim is a
  hypothesis until one transfer proves it.
- **A `backlog_write` agent tool.** Rejected: no embedded agent writes a backlog today, and a
  `ToolEntry` is a permanent row in every agent's access model, auto-seeded globally and into
  every profile.

## Consequences

**Good.** Every defect measured while deciding is closed by roughly 35 lines of checker work,
none of which depends on any of the larger questions. The board's numbers start meaning
something. Two entries open since May close as a side effect of the ADR validation pass. The
group chat gains a machine-readable trail of backlog mutations without becoming the store.

**Bad, and accepted.** Two identifier schemes are not introduced, which means the
phrase-versus-name ambiguity survives: an author writing an ordinary word in capitals can
still produce a false positive in the stale report. Today that is five of fifteen unique
tokens, from the `--check` summary line that reports how many stale references sit next to a
stated relation. The trigger to revisit: **if the false-positive share rises across two
consecutive monthly checks, or cross-project references become routine, or a backlog passes
~1000 live plus archived entries, numbering comes back.** If it does, it is `add`-time
allocation with a file lock, aliases generated rather than hand-maintained, and no
half-migration.

**That trigger was unfalsifiable as first written, and the fix is part of this decision.**
Nothing stored a history: `--check` prints and exits, the rendered artefacts are overwritten
each build, and all of them are gitignored. A trigger over a time series nobody keeps cannot
fire. `--check` therefore appends one dated line — `2026-08-10 · stale 28 · stated 8 ·
shortened 3` — to `check_history.log` beside the other build outputs, so the trigger reads
two lines of a file instead of two months of anybody's memory.

**Also accepted.** The event announcements are a social protocol, not a sync protocol. Two
nodes that edit the same entry still need a human to reconcile them; the announcements make
the disagreement visible in the place where the team already resolves disagreements, and that
is all they do.

**Found on the way, and not caused by this decision.** The skills sharing path has a real
defect: a re-import, or a peer push, overwrites a locally improved skill with no prompt, no
diff and no backup, because the guard checks provenance and nothing checks version. It is
latent only because `accept_peer_skills` defaults to off. It is filed as
`SKILL-REIMPORT-OVERWRITES-LOCAL-IMPROVEMENTS`, named here so the commitment is checkable
rather than aspirational.

## Confirmation

- `build.py --check` refuses the two live name-shape defects and stays green afterwards; the
  fixture grows a case per new refusal, because a rule nobody has watched fire is only written
  down.
- The duplicate `SHUTDOWN-PIPE-DRAIN` is reported **as a duplicate**. Today it is invisible
  for two compounding reasons: the parser takes the name as everything left of the first `:`
  or `—`, and the second heading has neither, so its name carries the aside
  `(original triage, S143 2026-05-23)`; and the duplicate check compares those full names,
  which therefore differ. The charset refusal alone would report it as a malformed name and
  never as a duplicate — the rekey in Decision §3 is what makes this criterion true.
- The `SHUTDOWN-PIPE-DRAIN` heading parses as HIGH — the priority Mike set on 2026-07-17 and
  the board has never shown.
- After the ADR pass, `--check docs/decisions/` refuses a malformed new ADR and warns on the
  001–026 tail.
- A `close` run writes a compliant closure line, moves the entry, records the actor, and
  posts one announcement into the group chat.
- **Not claimed until observed:** that the announcements are useful. If nobody reads them
  after a month, they are decoration and should be removed rather than defended.

## Scope

In: `tools/backlog/build.py`, `docs/BACKLOG_FORMAT.md`, `tools/backlog/fixture.md`, and the
group-chat send path used by the subcommands.

Out: the numbering scheme, any change to the message machinery, any agent `ToolEntry`,
repository synchronisation between nodes (`ideas/git-sync-plan.md`), and the platform
primitive.

**One constraint on the shape, not the size.** This decision adds roughly 500 lines to
`build.py`. What matters is not the length but that it stays **one stdlib file with no
package around it** — six projects copy it as a unit, and splitting it into a package is what
would end that.

## Implementation Status

| item | status |
|---|---|
| Name-charset refusal, dateless structural refusals, archive closure validation, duplicate rekey, envelope-emphasis tolerance, `check_history.log` (~35 lines) | **done**, 2026-08-10. Fired on both live defects — the colon inside `MESSAGEMAPPER-1:1-RELOAD-REFACTOR` and the duplicate `SHUTDOWN-PIPE-DRAIN` — and both were repaired. Four fixture cases added; the archive counter reports 83 of 85 tokenless closures and refuses none of them |
| `close` subcommand | **done**, 2026-08-10. Refuses a resolution outside the six and a closure with no evidence; rewrites the archived heading's status to `closed`; joins or opens the day's batch. First two real uses: `ADR-SCHEMA-1` and `ADR-LINT-1` below |
| `move` / `rename` / `add` subcommands | **done**, 2026-08-10. `move` rewrites the heading status to match the destination and appends `taken:` without ever replacing it; `rename` rewrites inbound references in both files in the same edit and marks its own trace `<!-- no-refs -->`; `add` refuses a name that is not a name, a missing origin and an empty body. Every verb validates a scratch copy before writing and keeps nothing that would refuse |
| Event announcement into the group chat | **partial**, 2026-08-10. Each verb prints one `ANNOUNCE` line in the closure-line grammar, no payload. Sending it is still the caller's job — the script does not open a socket |
| ADR validation pass in `--check` | **done**, 2026-08-10. Filename shape, duplicate numbers, `adr:`↔filename agreement, required fields, status vocabulary (widened to include `implemented`, which two ADRs already used), date shape, `## Decision` presence; dangling `depends_on`/`related`/`supersedes` warn. Front matter required from ADR-027 on, which found exactly one violation — ADR-028, now fixed. Rules watched firing in `tools/backlog/adr_fixture/` |
| Verbs watched firing | **done**, 2026-08-10. `tools/backlog/verbs_fixture.py`, 26 assertions over a throwaway backlog, including that a write which would refuse is not written at all |
| Section hygiene sweep of `IN PROGRESS` | in progress — 2 of the 7 entries this sweep classified as *not tasks* (three ADR trackers, one plan marked REFERENCE, two unnamed containers, one verification-only item) have been retired. That is a named set, not "everything that does not look like a task": the other 45 entries in the section are real tasks in the wrong place, and they are the rest of the sweep |

## Open Questions

1. **How does a repository synchronise between nodes without a hosted remote?** Decided in
   direction, not in mechanism: over DPC, per Mike, with `ideas/git-sync-plan.md` as the
   existing design to start from. Open: whether commit announcement plus pull-on-demand is
   enough, or whether the merge story needs more than git gives when two nodes have both
   committed; and whether a bundle actually transits our own file transfer.
2. **Closed during review.** Whether the announcement carries a machine-readable payload —
   no, a text line in the closure-line grammar, for the reason now in Decision §7. Whether
   `ADR-022` Phase 1 is done — a real question, but it belongs to `BUDGET-WIRING-IS-DEAD`
   and to ADR-022, not to a second copy here.

---

## Amendment 2026-09-05 — the axis vocabulary is declared in VISION and read from there

**Status of this amendment: proposed.** Drafted by CC_linux on Mike's «давай как поправку к
ADR-039» (2026-09-05); nothing below is implemented until Mike accepts it. The form — an
amendment rather than ADR-042 — is Mike's call of the same day.

### What is closed and what is not

Protocol 13 rule 13 (И1–И3, 2026-08-27 → 09-01) welded three of the four layers: every
accepted ADR carries an `axis:`, every board entry carries one, and the ROADMAP status block
is rendered from both rather than typed. The vocabulary those fields draw from is a constant
in the tool — `AXES` at `tools/backlog/build.py:97`, read in fifteen places — and the document
the vocabulary is *about* is read by nothing. Measured 2026-09-01
(`THE-FOUR-LAYERS-ARE-JOINED-PAIRWISE-AND-THERE-IS-NO-PROCESS`): `grep VISION build.py` is
empty, VISION cites no ADR, ROADMAP cites VISION zero times; upward the links exist (the
board cites ROADMAP 17 times and VISION 28), downward they are a person remembering. Mike,
2026-09-01: «надо придумать механизм и формализовать в протоколе 13».

Four of the five axes already have a paragraph in VISION's *Direction* section — *from
personal to collective*, *from passive to collaborative*, *from local to networked*, *from one
practice to many* — so the vocabulary is VISION's, not the board's. The fifth, `honesty`, is
the loop the format standard added because the project «cannot be honest without» it
(`docs/BACKLOG_FORMAT.md` §4a); VISION does not promise it today.

### Decision (proposed)

1. **VISION.md carries the vocabulary in a front-matter block at its top.** One record per
   axis: `token` (the word `axis:` fields use), `vector` (the VISION phrase it stands for,
   quoted from the prose), `done_when` (what would count as finishing — the one kind of prose
   И2 permits a status document to carry). The block is YAML front matter, which GitHub
   renders as a table above the document: the file stays a document people read and becomes
   one the tool reads, in the same bytes. The prose of VISION is not changed by this
   amendment.
2. **`build.py` reads the vocabulary from that block.** The constant stays as the fallback
   for a project whose VISION has no block, and using the fallback prints a warning naming the
   file to declare it in — so the five sibling projects keep working the day this lands and
   are told what to do next. No parser is added: the front-matter reader that already serves
   `docs/decisions/*.md` reads this block too.
3. **The check runs in both directions.** An `axis:` token in an entry or an ADR that VISION
   does not declare is a **refusal** — the same refusal that exists today, now against the
   document's list rather than the tool's. An axis VISION declares with no accepted decision
   and no board entry behind it is a **warning**: a direction promised and not worked on,
   which today nothing can see. Warning and not refusal, because a new project legitimately
   declares a direction before the first entry under it exists — that is what declaring a
   direction is for. (This answers the first open question the backlog entry asked.)
4. **Prose and block are not machine-compared.** A reader can compare them because the block
   quotes the vector phrase verbatim; drift between the paragraph and the record stays a
   review matter, exactly as И2's own prose does. Writing a prose checker here would promise
   what «ни одно утверждение нельзя опровергнуть кодом» already says cannot be checked.
   (This answers the second open question.)
5. **Whether VISION promises `honesty` is Mike's decision, taken when the block is written.**
   Either the block gains a fifth record and VISION gains one sentence promising it, or the
   record is marked as the format standard's loop rather than a VISION vector. The tool does
   not care; the document's author does.
6. **First consumer after the validator: the roadmap view.** Mike asked on 2026-09-05 for an
   HTML rendering of ROADMAP; it is drawn as lanes per axis from the same function the
   validator reads, never from a second copy of the list, so the lanes come from VISION the
   day this lands and do not change when it does. Statuses that ROADMAP states in prose are
   drawn grey and labelled «claimed in prose, not measured» (Mike, 2026-09-05). The view is a
   fourth artefact of the default build beside `backlog.html`, `graph.html` and `graph.json`,
   with the same freshness line in `--check`.
7. **Protocol 13 rule 13 is proposed to gain И4** — every axis in use is declared in VISION,
   and VISION is read by the tool. Protocol changes are agreed by the three parties; this
   amendment records the proposal and does not enact it.

### Consequences

The top arrow of Mike's chain stops being a memory: a token in use *is* a reference into
VISION, and «zero links from VISION» stops being the diagnosis. The vocabulary gains a source
that can be argued with in the document that owns it. A foreign project declares its own axes
in its own VISION instead of inheriting ours — the scratch run of 2026-09-01 showed it cannot
today (first entry and first decision both refused on `axis token 'delivery' not in the
vocabulary`). Accepted cost: VISION.md, a public file, now opens with a block of metadata, and
a hand edit to that block can break the check for every writer — which is the point.

### Confirmation

- `grep VISION tools/backlog/build.py` is no longer empty, and `--check` prints where the
  vocabulary came from: `vocabulary  VISION.md (5 axes)` or `vocabulary  built-in constant —
  declare it in VISION.md`.
- Fixture cases, each watched to fire: a token not declared in VISION refuses; a declared axis
  with nothing behind it warns; a project with no block falls back and warns.
- Neither `--check` nor the roadmap view carries a second copy of the axis list — one function,
  measured by grep.
- Not claimed until observed: that the dead-axis warning is read by anyone. If it is never
  acted on within a month of landing, it is decoration.

### Scope

In: the front-matter block in `VISION.md`, the vocabulary reader and the two-way check in
`build.py`, the fixture, `docs/BACKLOG_FORMAT.md` §4a (which currently says the vocabulary is
«five words, from VISION's three vectors plus the two loops» and will say where it is read
from), and the roadmap view of item 6.

Out: any change to VISION's prose beyond the block and the one sentence item 5 may add; the
И4 wording in Protocol 13; generated identifiers and everything else this ADR already leaves
out.

---

## Authors

CC (draft, measurements, code reading), Ark (review, convergence synthesis), Warren (cost
framing, the platform-gap formulation), Fable 5 and GLM 5.2 (independent external review,
three rounds), Mike (direction, the multi-node framing, decision).

## References

- `docs/BACKLOG_FORMAT.md` — the format this ADR governs the tooling for.
- `ideas/dpc-research/backlog-entrypoint-prompt.md` and the two independent reviews
  `backlog-entrypoint-fable5.md`, `backlog-entrypoint-glm5.2.md` (three rounds, 2026-08-10).
- ADR-036, ADR-037, ADR-038 — the machinery this ADR declines to use, and why it exists.
- `HISTORY-DIVERGENCE-IS-PERMANENT`, `SKILL-LINT-1`, `ADR-SCHEMA-1`, `ADR-LINT-1` — open
  entries this decision touches.
