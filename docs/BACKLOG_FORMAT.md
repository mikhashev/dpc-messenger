# Backlog format — the shared standard

**Status:** in force from 2026-08-10 · **Applies to:** every project that keeps a
`backlog.md` · **Enforced by:** `tools/backlog/build.py --check`

This is not a new format. It is the shape the dpc-messenger backlog already grew into
across 226 entries, written down so the other projects can share it and so a script can
check it. Two independent reviews (Fable 5, GLM 5.2, 2026-08-09) surveyed the external
conventions — org-mode, todo.txt, Backlog.md, MADR, Keep a Changelog, Bugzilla — and both
concluded the same thing: publish the one that already works rather than import a
stranger's. What is borrowed is named where it is borrowed.

---

## 1. The entry

```markdown
### NAME-IN-CAPS: short description (PRIORITY, STATUS, YYYY-MM-DD — origin)

- **Observed.** What was seen, with a file:line, a log line, or a measurement.
- **Inferred.** What that means, marked as inference and not as observation.
- **First step.** The next concrete action, small enough to start today.
```

**One entry = one `###` heading.** Everything below the heading until the next heading is
free prose and the schema never descends into it. That is deliberate: the reasoning is the
part worth keeping, and a schema that turns it into fields kills it.

The heading carries five things and nothing else:

| part | rule |
|---|---|
| `NAME-IN-CAPS` | unique within the project, `SCREAMING-KEBAB`. This is the handle everything else cross-references. |
| `description` | one line, a claim rather than a topic. "X does Y when Z", not "the X problem". |
| `PRIORITY` | `CRITICAL` / `HIGH` / `MEDIUM` / `LOW` / `RESEARCH` |
| `STATUS` | `open` / `in-progress` / `done-awaiting-observation` / `closed` |
| `YYYY-MM-DD — origin` | when it was raised and by whom, in their words when there are words |

**Priority is read only from the trailing parenthesised block.** Scanning the whole heading
once turned a MEDIUM entry into a CRITICAL one because its prose contained "P0 needs Mike's
verb". The renderer already carries that fix and its comment; the validator inherits both.

**Keep counts out of the name.** A name is a permanent handle and a count is a measurement;
put a measurement in the handle and the handle starts lying, while renaming it to tell the
truth breaks every reference pointing at it. `FORTY-BACKLOG-REFERENCES-POINT-AT-ENTRIES-THAT-DO-NOT-EXIST`
was filed at 07:05 and the number was 28 by 07:46 — not because anything had been repaired,
but because the classifier that produced the forty got better. Counts belong in the body,
next to the date they were taken. (Warren, 2026-08-10.) <!-- no-refs -->

## 2. Status lives in two places, on purpose

The `## H2` section an entry sits under **is** its status — moving the entry is the status
change, and no field can go stale relative to it. `STATUS` in the heading duplicates it,
and the duplication buys two things:

- **the validator can catch drift** — an entry under `## OPEN` whose heading says `closed`
  is a real and otherwise invisible failure;
- **chunk safety** — retrieval injects an entry into an agent's prompt *without* its
  section heading, and cross-project (a dpc-messenger entry already lands in ai-studio
  prompts today). Without status in the heading the agent reads a finished defect as an
  open one.

Sections, and the status each implies:

| section | status | meaning |
|---|---|---|
| `## OPEN` | `open` | raised, not started |
| `## IN PROGRESS` | `in-progress` | someone is on it |
| `## DONE — AWAITING OBSERVATION` | `done-awaiting-observation` | code written, **not yet seen working in production** |
| `## BLOCKED ON DECISION` | `open` | waiting on a human decision, not on work |
| `## BACKLOG` | `open` | real but not now |
| `## IDEAS` | `open` | not yet a task |

`done-awaiting-observation` is ours. No external convention has it, because almost nobody
runs with "code written is not done". It is the shelf the board's headline number counts,
and an entry only leaves it by recording an observation.

A file whose section names differ (Russian headings, a table-based layout) declares the
mapping in its front matter rather than renaming — see §5.

## 3. Closing an entry

An entry leaves the working file for the archive (`backlog_closed.md`) **only after it has
been observed in production**, and it leaves carrying a closure line:

```markdown
**Closed:** S<session> · YYYY-MM-DD · <resolution> · <evidence>
```

`resolution` is the *why it left*, separate from the status — the status/resolution split
is Bugzilla's (1998), carried into JIRA and MADR:

| resolution | means | evidence required |
|---|---|---|
| `fixed` | implemented and observed | commit hash **and** the observation |
| `disproved` | a measurement falsified the premise; never implemented | the measurement or log line that falsified it |
| `moot` | the premise dissolved | what changed in the world |
| `superseded` | replaced by another entry or decision | the id or ADR that replaced it |
| `duplicate` | the same thing as another entry | the id it duplicates |
| `wontfix` | deliberate refusal | Mike's verb |

**Evidence is mandatory, but its type follows the resolution.** A `disproved` closure
legitimately has no commit — demanding a hash there teaches people to invent one. That is
the whole point of the table: `—` in the hash position is correct for four of the six.

Never backfill closure lines onto old entries. Inventing a date or a hash for something
nobody recorded is manufacturing data, and the archive's own rule already forbids it.

### The session identifier

`S<YYYY-MM-DD>.<N>` — the UTC date the session was opened, then its position among the
sessions opened on that date. Current example: `S2026-08-27.1`.

Decided by Mike on 2026-08-27 (UTC), after three numbering families had accumulated and
one identifier already named two different sessions —
`THE-SAME-SESSION-IDENTIFIER-NAMES-TWO-DIFFERENT-SESSIONS` in the backlog carries the
measurements. **UTC, not the machine clock**, because the store the number is derived from
already stamps UTC: `2026-08-27T12-07-16_reset_session.json` has a local mtime of
`2026-08-27 19:07:16` on the box that wrote it. At the hours this team works those two
clocks are a day apart, so an unnamed clock is a second collision waiting.

**Derivation, mechanical and re-runnable** — the archive of the DPC Project group is the
source, one file per reset, and a reset is what opens the next session:

```bash
A=~/.dpc/conversations/group-b88b65076b85-dpc-project/archive
find "$A" -name '*_reset_session.json' -printf '%f\n' | sort | tail -1   # opened the live session
find "$A" -name "2026-08-27*_reset_session.json" | wc -l                  # how many opened that UTC day
```

The last filename gives the date; the count of that day's files gives `N` (the live session
is the last of them). Nobody carries the number in their head, and nobody derives it from a
scratchpad.

**The three families it replaces are historical and are not renumbered.** Renaming would
break `origin`, which is the field this board is worth reading for.

| family | example | why it is retired |
|---|---|---|
| plain ordinal | `S21`, `S72`, up to at least `S223` | carries no date and is not unique — `S24` names both an April and an August session |
| archive count | `S123`, written into closure lines on 2026-08-26/27 | reproducible, but its first number collided at once: `S123` already named the May Grafeo session |
| pre-repository | `S47`–`S206` as cited in ROADMAP | the observations they stand for were never written into this repository, so the tokens resolve to nothing |

A closure line carrying one of these stays as written. Only the date beside it tells the
eras apart, which is why the date field is not optional.

**Not enforced by the tool.** `build.py close` requires `--session` to be present and does
not look at its shape, so this section is the only place the scheme exists. A format check
there is a reasonable follow-up and is deliberately not being added in the same edit that
writes the rule.

**Scope.** The closure line is UTC in both of its first two fields: the session id and the
date beside it. They sit together and a reader compares them, so a UTC id next to a local
date would rebuild the ambiguity this section exists to remove. Dates elsewhere on the
board — an entry's envelope, a dated amendment — keep the convention they already had,
which is the writer's local day. Unifying those is a separate question, not settled here.

## 4. Fields we deliberately do **not** have

Both reviews agreed with the maintainer's list, and the reasons are worth keeping because
they will be re-proposed:

- **`Updated:`** — rots silently, and for a gitignored backlog there is nothing to audit it
  against, so a stale value is not merely wrong but unfalsifiable. What works instead is a
  dated amendment *inside the prose*: `**Corrected 2026-08-06:** the earlier number was
  measured on one side only.` That line is self-certifying; a mutable field is not.
- **`depends-on:`** — dependencies are already stated in prose ("do these three as one
  block") and that survives; a formal field is filled once and then lies. A board that
  renders stale edges manufactures false confidence.
- **`owner:`** — for a team this size, `origin` already names who cares.

## 4a. The one field that was added anyway — `axis:`

```markdown
- **axis:** network
- **axis:** collective, honesty      ← two is allowed, three is a smell
```

Vocabulary, five words, from VISION's three vectors plus the two loops that VISION does
not promise but the project cannot be honest without:

| token | what it serves |
|---|---|
| `collective` | from personal to collective — P2P, groups, identity, signatures, history |
| `knowledge` | from passive to collaborative — memory, retrieval, the agent that works on it |
| `network` | from local to networked — local inference, compute, cost, the gateway |
| `honesty` | that our own numbers mean something — eval, CI, the board and its gates |
| `reach` | that somebody outside can find and use this — docs, releases, distribution |

**§4 above says a formal field is filled once and then lies, and that objection is
correct about `depends-on:` and wrong about this one.** `depends-on` encodes a relation
between two moving things, so both ends can drift out from under it. An axis encodes what
the entry is *about*, which changes only when the entry is rewritten — and then the axis
is rewritten with it. It is also falsifiable in a word, which is what `Updated:` was not:
a reviewer who thinks an entry is `knowledge` and not `network` can say so and be right or
wrong. And unlike `owner:`, nothing else in the entry carries it: no existing field says
which direction the work serves.

**Two values, not one.** The first real use case had two: a three-node bench serves
`collective` and `honesty` at once, and ADR-041 serves `network` and `honesty`. A field
that forces one value makes the author pick a favourite, and the counter then reports a
preference rather than a fact. Three or more is warned about, not refused — an entry that
serves everything reports nothing, and is usually two entries.

**Words, not letters.** Ark's original scheme was `A / B / C` for the vectors and
`C1 / C2` for the loops; `C` and `C1` are one keystroke apart and mean different things.
Words cost four characters and need no legend.

**Migration is the same shape as §7.** `AXIS_CUTOFF = 2026-09-01`: an entry dated on or
after it is refused without the field, everything older warns. The unmarked count is
printed in the check summary and is the backfill meter, exactly as the warning count is
the language meter. The first pass marked 396 entries whose name made the direction
unambiguous and deliberately left 130 blank — a guessed axis is worse than a missing one,
because the meter then reports coverage it does not have.

**What the counter is for.** The check prints, per axis, how many entries sit in
`DONE — AWAITING OBSERVATION`. That number is the project's own health signal — work
finished and never seen working — and per axis it says *which direction* is running ahead
of its evidence.

## 5. File-level front matter

Per-**entry** front matter is rejected: it doubles the structural noise, pushes metadata
below the fold, and would remove the property that makes an entry legible when an agent
receives it as a bare chunk. Per-**file** front matter is where project-level truth goes —
one block, six files:

```yaml
---
project: dpc-messenger
entry_level: h3            # h3 | table | bullet — what a task is in this file
status_machine: v1
language_cutoff: 2026-08-10
sections:                  # local heading → canonical status
  "IN REVIEW": in-progress    # a heading this project uses and the standard does not
---
```

Map, don't rename. Renaming a working file's headings costs real churn and buys nothing a
mapping doesn't.

## 6. Language

**English**, for new and touched entries, from `language_cutoff`. Existing Russian prose is
**not** bulk-translated: these entries are measurement narrative, and translating 400+ of
them is re-authoring with a real risk of claim drift that nobody will review at that
volume. Both reviewers independently said the same, in stronger words than the maintainer
would have.

An old entry becomes English when it is edited for another reason, as part of that edit.
Files stay mixed-language for months; the validator's warning count is the honest measure
of that debt, and it goes down by touching, not by a sprint.

## 7. Migration

Three separate migrations with three different costs. Only the first is mechanical.

1. **Structure** — section mapping via front matter (no text moves), id backfill for the
   projects that lack ids, front matter in each file. Cheap, reversible, no meaning
   touched.
2. **Status hygiene** — the closure line and resolution token apply **forward only**.
3. **Language** — see §6. Never as part of a format migration.

New entries conform. Old entries conform when touched. That is the whole plan.

## 8. The check

```bash
uv run python tools/backlog/build.py --check
```

**Refusals apply to post-cutoff entries only.** Migration is new-entries-only (§7), so
refusing on legacy content would leave the exit code stuck at 1 and mean nothing. The same
violation in an older entry warns instead, and the warning count is the migration debt
meter — it goes down by touching entries, not by a sprint.

**Refuses regardless of date** (ADR-039) — a defect in this class corrupts the graph for
every reader *today*, whatever year the entry was written in, so the cutoff below does not
apply to it. Envelope incompleteness is migration debt and stays date-gated; these are not:

- a **duplicate name**, compared on the `SCREAMING-KEBAB` run rather than the whole parsed
  name. Keyed on the whole name, an aside on one copy — `NAME (original triage, S143 …)` —
  made two copies of one entry compare unequal, and a real duplicate went unseen for months;
- a **name the parser cannot round-trip**: a name carrying an aside (as above), or a colon
  inside the name run (`NAME:1-MORE-NAME`), which parses as `NAME` and leaves every reference
  to the full name resolving to nothing — that is where a dangling token in the stale report
  came from;
- a **section** that is neither recognised nor mapped in front matter.

A heading with no name run at all is a *rubric*, not a malformed entry, and warns: turning
rubrics into entries is a separate job and refusing them would light the exit code
permanently red, which §8 exists to avoid.

**Tolerated on read, refused on write:** markdown emphasis at the start of the envelope.
`(**HIGH — …` reads as *no priority at all*, because the priority is matched from the first
character — a HIGH set on 2026-07-17 was invisible on the board for three weeks. The
information is present and only decoration hides it, so decoration is stripped before the
priority, status and origin are read, and a post-cutoff entry that writes it is refused so
nothing new comes to depend on the tolerance.

**Reported, never refused:** closure lines in `backlog_closed.md` carrying no resolution from
the six in §3 — 83 of 84 at the time of writing. They are pre-standard, and §3 forbids
backfilling one, so the checker counts them and says why the archive cannot explain itself.

**Refuses** (exit 1) — structure that breaks tooling or misleads a reader:

- a duplicate entry name within a project;
- a `###` entry in a section that is neither recognised nor mapped in front matter;
- a heading `STATUS` that disagrees with the section the entry sits under;
- a closure line whose resolution is not one of the six in §3;
- a priority token outside the vocabulary — `MED` for `MEDIUM` is reported as the typo it
  is, not as an absent priority, because those are different mistakes;
- a missing priority, status, date or origin;
- Cyrillic in the **name or description**. The origin is exempt: §1 asks for who raised it
  *in their words*, and those words are often Russian. A checker that refused them would
  have this standard contradict itself.

**Warns** — content quality, because a backlog is allowed to be untidy:

- any of the above in a pre-cutoff entry.

**Reports, without touching the exit code** — these are content, and the classifier behind
them has a known false-positive shape, so they point rather than gate:

- `stale` — a name-shaped token in an entry's body that resolves to no entry in
  `backlog.md` or `backlog_closed.md`. An entry name is a SCREAMING-KEBAB sentence (§1), so
  a name written in another entry's body **is** a reference, and one that resolves to
  nothing is the same defect this standard exists to prevent, one level up: the document
  points at something that is not there. Tokens shaped like model names or version numbers
  (`GLM-5`, `BGE-M3`, `D-PC`) are indistinguishable by shape from the legacy identifiers
  this finds (`MENTION-1`, `DEDUP-1`), so they are only reported when the line states a
  relation — *Cross-ref*, *sibling of*, *parent task*, `[[…]]`. Line and session spans
  (`L451-L462`, `S111-S113`) are never reported.
  The total is an **upper bound on real breakage, not a count of it**, and the summary line
  says so: the number that matters is how many sit next to a stated relation, because those
  are the ones an author asserted. A phrase only vouches for tokens within 80 characters
  *after* it — applying it to the whole line let a marker at character 257 corroborate a
  token at character 68, which is not corroboration by any reading.
- `short` — a token that is the head of exactly one real name; a shortened reference, not a
  broken one, and named separately so it does not bury the real breakage.
- `archive` — headings in `backlog_closed.md` that carry no parseable entry name. The
  archive is parsed by a simpler path than the live file and edited under different
  circumstances; when its conventions drift, entry→archive links vanish with nothing to
  show for it. The counter earned itself on its first run: the archive strikes closed
  entries through (`### ~~NAME~~ — CLOSED S191`), 45 names were being lost to the tildes,
  and seven live references were being reported as broken because their target could not
  be seen.
- **Quoting a dead name on purpose:** end the line with `<!-- no-refs -->` and no token on
  it is read as a reference. An entry written *about* broken references quotes them, and
  without this the report flags the entry that exists to fix them — for good, since the
  examples never go away. It is per line, not per entry, so it cannot silence a whole entry
  by accident, and it is invisible in rendered markdown. Backticks were tried as the signal
  first and rejected by measurement: 53 of the 123 mentions of live entry names sit inside
  code spans, so that rule would have discarded nearly half the real graph.
- **freshness** — how old `backlog.html` and `graph.html` are against `backlog.md`. Both
  are gitignored, so nothing in `git status` ever says they went stale, and this line is
  the only signal that exists. It prints; it does not rebuild. A checker that quietly
  regenerated the board to silence its own warning would be exactly the thing the last
  paragraph of this section forbids.
- **glossary** — `docs/GLOSSARY.md` is a table of words with a «Defined in» link each, and
  the same pass resolves every link: a file that does not exist or a heading anchor the
  file does not carry is a warning (content, not structure — the row still names a word),
  and an axis token from §4a with no row is a warning too, because the board files work
  under a word nobody has written down where a reader would look. The glossary points at
  its sources and never defines on its own; a row without a link is not a row. Added
  2026-09-05, on Mike's «нам нужен документ с глоссарием по проекту». The walk itself
  is `tools/backlog/glossary_check.py`, and the client suite runs it too
  (`tests/test_the_glossary_points_at_headings_that_exist.py`): this check needs
  `backlog.md`, which no clone has, so a moved heading was visible only where the
  board is — the test is the half that reaches CI (Linus's question, same day).

**Three artefacts, one pass.** The default run (no `--check`) writes `backlog.html` — the
board, what is open — `graph.html` — what leans on what, which a flat list cannot show —
and `graph.json`, the same graph for readers that cannot click. They are written together
so they can never disagree about how fresh they are. Entries with no link at all are drawn on a
ring around the linked graph as hollow dots, behind a legend chip that starts off, and are
also listed underneath by priority, because "HIGH entries no one has connected to anything"
is a finding of its own; every legend chip — priority, section, ADR, roadmap phase, archive,
the ring — is a filter. ADR nodes are drawn as a second node
type: the backlog and the roadmap already speak the same language — decisions — so the join
between the two documents costs nobody a new habit. **No `depends_on` field exists and none
is asked for.** Every link is already in the prose, which also bounds what the picture may
claim: that two entries mention each other, not that one blocks the other.

`graph.json` exists because agents are first-class readers of these files (§ the constraint
that started this) and an agent cannot click a picture. Its most useful key is `backlinks`:
an entry handed to an agent as a retrieval chunk carries its own outgoing references in the
prose, but *what references it* appears nowhere the agent can see. The pipeline computed
that all along and was throwing it away. A second key, `dependencies`, carries just the
edges worth walking — and its length is the honest measure of how much of "what blocks
what" this project has actually written down.

**Say which relation you mean, and it becomes walkable.** The phrase in front of a name is
kept, not just the fact that there was one: `blocked by`, `blocks NAME`, `depends on`,
`builds on`, `parent task`, `child task`, `superseded by`, `duplicate of`, `sibling of`,
`follow-up to`, `Cross-ref` / `related to` / `see also`. The nearest phrase before a name
wins, so `blocked by [[X]], related to [[Y]]` labels each one correctly. Only the first
group — blocked by, blocks, depends on, parent, child — answers *what do I fix first*;
everything else means *see also* and is not worth walking. Measured on the day this
shipped: of 223 links, **three** carried dependency semantics. The tooling was never the
thing missing — nobody was writing the dependencies down.

**`ROADMAP.md` is read too, for its decisions only.** The backlog cites ADRs and the
roadmap says which phase each ADR belongs to, so the two documents already share a
vocabulary and the chain *entry → decision → phase* exists in prose without anyone
maintaining a mapping. Roadmap sections become a third node type, and `Dependencies:` lines
(`ADR-024 builds on ADR-010, ADR-018, ADR-019`) become decision-to-decision edges. Both
ends of such a claim must sit within a short distance of the phrase — without that limit
the parser read *"Memory Upgrade (ADR-010) — … model_swap superseded by ADR-018"* as a
statement about ADR-010, which the sentence never made.

**Write a relation as `[[NAME]]` when you mean one.** A bracketed name is a reference by
construction, so it skips every heuristic the scanner otherwise needs — no stoplist, no
shape guard, no relation-phrase test — and if it resolves to nothing it is reported without
softening. This is encouraged, never required, and bare names keep working: a scan that
recognised only brackets would trade visible false positives for invisible missing edges,
which is the worse failure for a graph whose purpose is finding links nobody maintained.

**The layout is part of the build, and warm-starts from the previous one.** Coordinates are
computed in `build.py` and stored in `graph.json`, so the picture is an artifact of the
build rather than of whoever opened it. Deterministic is not the same as stable: seeding
each node from a hash of its name is deterministic and was measured to still move the
median node 303 px when a single entry was added, because a force simulation has many
near-equivalent minima. Starting from the previous layout brings that to 7 px. Delete
`graph.json` to force a fresh layout.

**`build.py` has no third-party dependencies, and that is a decision, not an accident.**
It has no `pyproject.toml` and no virtualenv of its own; it runs under any Python, which is
what lets one copy of it serve six projects with six different toolchains. Anything that
needs a package must be optional, imported inside the function that needs it, and reachable
without installing anything into a project — `uv run --with <pkg> python tools/backlog/build.py`
is the pattern. Measured 2026-08-10 on the dpc-messenger machine: `sqlite3` is in the
standard library, `grafeo` and `networkx` are present in the client venv, `duckdb` is
present in neither that venv nor the system interpreter.

**Not checked, and why.** *How long an entry has sat on the observation shelf* — we record
when an entry was **raised**, never when it landed on the shelf, so any age computed from
the envelope would measure the wrong interval. Implementing it honestly needs a date
written when the entry moves; until that exists, the rule stays out rather than shipping a
number that looks like an answer.

**It never rewrites a file.** Every automated classifier in this repository's history has
documented its own false positives — including this one, on its first day: a non-nesting
regex read the envelope of the first entry written to this standard and reported a complete
entry as missing its priority and origin, because the origin quoted Mike verbatim and the
quote contained parentheses. Report, never auto-fix.

**Every rule above is covered by a fixture** — `tools/backlog/fixture.md`, run it with
`build.py --check tools/backlog/fixture.md` — carrying deliberate violations, one per rule, plus
entries that must stay silent — a valid entry, a nested-paren origin, a `disproved` closure
with no commit hash, and a section mapped through front matter. A rule that has not been
watched to fire on a case built to trip it is not enforced, it is only written down.

**Where it runs:** on demand, by whoever edits the backlog — that is honest about the fact
that two of the six backlogs are gitignored and there is no CI. The four tracked projects
can additionally hang it on a pre-commit hook. The maintainer running it before declaring
an audit done is the actual enforcement point; the format living in the maintainer's own
instructions (see §9) is what prevents drift in the first place.

### The same pass reads `docs/decisions/`

One validator, two formats (ADR-039 item 6). The decisions sit next to the backlog, 26 of
the backlog's links point at them, and nothing checked them at all — so `--check` reads
them too. The record boundary differs: an ADR is a whole file and keeps per-file YAML front
matter, an entry is a heading and keeps its envelope. The rules are the same rules —
required fields, a closed vocabulary, references that resolve, and a migration cutoff
(front matter is required from ADR-027 on; 001–026 warn).

The full list, and the fixture that watches each rule fire, live in
[docs/decisions/TEMPLATE.md](decisions/TEMPLATE.md#validation).

## 8a. Writing an entry with the tool

Editing the file by hand stays correct and always will — §8 is what holds the format, and
no script can be the only way in. The four verbs exist so that the common path is right by
construction, and so that a rename cannot leave its inbound references behind (ADR-039).

```bash
uv run python tools/backlog/build.py add NAME-IS-A-CLAIM --by=CC \
    --desc='what happens, when'  --priority=HIGH  --origin="Mike: '…'" \
    --observed='file:line or a measurement'  [--first-step='…']  [--section=OPEN]

uv run python tools/backlog/build.py move NAME --to='IN PROGRESS' --by=CC
uv run python tools/backlog/build.py rename OLD-NAME NEW-NAME --by=CC
uv run python tools/backlog/build.py close NAME --session=S72 --resolution=fixed --by=CC \
    --evidence='commit abc1234, observed in the 2026-08-11 startup log'
```

What each one guarantees, beyond typing less:

- **Every verb validates before it writes.** The candidate file is checked in a scratch
  copy and the write is kept only if the result carries no refusal. Warnings never block —
  the live file carries 99 of them, and a `close` that recites them every time is how
  people learn to stop reading the output. `--dry-run` validates and writes nothing.
- **`close` cannot omit the resolution or the evidence.** 83 of 85 archived closure lines
  say nothing about why the entry left; that is the failure this verb exists to stop
  repeating. It also rewrites the heading status to `closed` on the way to the archive,
  because the archived entry no longer sits in a status section and retrieval hands an
  agent the heading without either.
- **`move` rewrites the heading status to match the destination section**, so the §2
  drift check cannot fire on a move that was meant to be correct.
- **`--by` is mandatory on every write verb, and falls back to `DPC_BACKLOG_BY` rather
  than to the OS user.** Five actors share one account here, so a derived name would stamp
  one label on all of them and look authoritative doing it. Both usage blocks that teach
  these verbs showed it in brackets for a day after the code stopped accepting it that way;
  the guard was right and the documents were wrong.
- **`move --by` appends `- **taken:** who · date` and never replaces it.** People are
  recorded as events, not as a field: the current assignee is the last such line, derived
  and not editable, which is the property that keeps it from rotting the way `Updated:`
  did. There is still no `owner:` field (§4).
- **`rename` rewrites every inbound reference in `backlog.md` and `backlog_closed.md` in
  the same edit**, and leaves a trace line carrying `<!-- no-refs -->` — quoting the dead
  name without that marker would manufacture exactly the dangling reference this tool
  reports.

The verbs are watched to fire: `uv run python tools/backlog/verbs_fixture.py` builds a
throwaway backlog, runs all four plus every refusal path, and asserts what the file says
afterwards. Same rule as the read fixture — a rule nobody has seen fire is written down,
not enforced.

## 9. Who this binds

- **Internal agents** — via `protocol-13.md`, which points here.
- **External agents** working from a public brief — via `cc_cron_prompt_public.md` and
  `protocol-13-public.md`, which point here.
- **Humans** — same document; there is no separate human version.

If you are an agent about to write a backlog entry and this file and your instructions
disagree, this file is newer.

---

## Sources

- The entry shape, the observation shelf, and the closure line: dpc-messenger's own
  practice, formalised — not imported.
- Status/resolution split: Bugzilla (1998), carried into JIRA and MADR.
- Task-as-heading with metadata on the heading line, states as a keyword vocabulary,
  closed items refiled to an archive: org-mode's task model (2003). Its software is not
  Markdown; its design is the precedent.
- File-level YAML front matter: Jekyll-era convention, and MADR's argument for putting
  status metadata there.
- Reviews that produced this: `ideas/dpc-research/backlog-standard-fable5.md` and
  `backlog-standard-glm5.2.md` (2026-08-09, independent), with the reconciliation of their
  four disagreements recorded in the project group chat the same day.
