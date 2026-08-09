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
