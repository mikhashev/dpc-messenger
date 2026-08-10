---
project: fixture
entry_level: h3
sections:
  "IN REVIEW": open
---

# Fixture — one deliberate violation per rule, plus regressions that must stay silent

Run it: `uv run python tools/backlog/build.py --check tools/backlog/fixture.md`

Expected: 6 refusals, 3 warnings, exit 1. Five entries must produce nothing at all.

The non-English rule is the one rule not testable from here — a fixture entry proving it
would have to contain the very thing this file must not contain. It is asserted inside
`build.py` instead, against a string built from codepoint escapes.

## OPEN

### CLEAN-ENTRY: correct entry, must produce nothing (HIGH, open, 2026-08-11 — CC: fixture)

- **Observed.** Exists to prove the checker stays quiet on a valid entry.

### NESTED-PARENS-IN-ORIGIN: complete envelope whose origin quotes a parenthetical (HIGH, open, 2026-08-11 — Mike: "rewrite it onto the paid API (the subscription route got the account banned)"; "put it in the backlog")

- **Observed.** Regression for the bug Warren found on 2026-08-10: a non-nesting regex
  grabbed the inner aside and reported this complete entry as missing priority and origin.
  Must produce **nothing at all**.

### STATUS-WORD-INSIDE-THE-QUOTE: an origin quoting the word closed must not become a status (HIGH, open, 2026-08-11 — Mike: "what is this? Voting already closed (approved) — your vote was not counted")

- **Observed.** Must stay silent. Reading status from the whole envelope pulled `closed`
  out of this quote and reported a heading/section disagreement the author never wrote.

### PRIORITY-TYPO: a misspelled priority is a different mistake from a missing one (MED, open, 2026-08-11 — CC: fixture)

- **Observed.** Must be refused, and the message must name the bad token rather than
  claim the priority is absent.

### STATUS-DISAGREES: heading says closed while sitting under OPEN (HIGH, closed, 2026-08-11 — CC: fixture)

- **Observed.** Must be refused.

### CLEAN-ENTRY: same name again, post-cutoff (LOW, open, 2026-08-11 — CC: fixture)

- **Observed.** Must be refused: duplicate name.

### MISSING-ENVELOPE: post-cutoff entry with nothing but a date (2026-08-11)

- **Observed.** Must be refused: priority, status and origin all absent.

### BAD-RESOLUTION: closure line with a token outside the vocabulary (LOW, open, 2026-08-11 — CC: fixture)

- **Observed.** Must be refused on the closure line below.
- **Closed:** S71 · 2026-08-11 · solvedish · `deadbeef`

### GOOD-RESOLUTION: closure line with a real resolution and no commit (LOW, open, 2026-08-11 — CC: fixture)

- **Observed.** Must stay silent — `disproved` legitimately has no hash (§3).
- **Closed:** S71 · 2026-08-11 · disproved · measured in dpc-client.log, zero occurrences

### OLD-DUPLICATE: pre-cutoff legacy entry

- **Observed.** The first of a duplicated pair; the refusal is reported on the second.

### OLD-DUPLICATE: the same legacy name again

- **Observed.** Must be **refused despite being pre-cutoff** (ADR-039). A duplicate name
  corrupts the graph for every reader today, whatever year the entry was written in — that
  is a different class from envelope incompleteness, which stays date-gated. Until
  2026-08-10 this case expected a warning.

### ASIDE-IN-NAME (original triage, S143 2026-05-23)

- **Observed.** Must be refused twice: once because the name carries an aside that the
  parser cannot round-trip, and once as a duplicate of `ASIDE-IN-NAME` below — which is the
  point, since keying the dedup on the full name is exactly what hid a real duplicate for
  months.

### ASIDE-IN-NAME: the same name without the aside

- **Observed.** The clean half of the pair above.

### COLON-IN-NAME:1-MORE: a colon inside the name run (LOW, open, 2026-08-11 — CC: fixture)

- **Observed.** Must be refused dateless. The envelope uses `:` to separate name from
  description, so this parses as `COLON-IN-NAME` and every reference to the full name
  resolves to nothing.

### EMPHASISED-ENVELOPE: priority wrapped in markdown emphasis (**HIGH**, open, 2026-08-11 — CC: fixture)

- **Observed.** Must be refused as a post-cutoff entry, and must still parse as HIGH — the
  value is recovered on read so a legacy entry does not lose its priority, and refused on
  write so nothing new comes to depend on the tolerance.

## IN REVIEW

### MAPPED-SECTION: sits under a section declared only in front matter (LOW, open, 2026-08-11 — CC: fixture)

- **Observed.** Must stay silent — §5 says map, don't rename.

## RANDOM SECTION NAME

### ORPHANED: section neither recognised nor mapped (LOW, open, 2026-08-11 — CC: fixture)

- **Observed.** Must be refused.
