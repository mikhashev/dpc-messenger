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

- **Observed.** Must warn only — migration is new-entries-only (§7).

### OLD-DUPLICATE: the same legacy name again

- **Observed.** Must warn, not refuse: both are pre-cutoff.

## IN REVIEW

### MAPPED-SECTION: sits under a section declared only in front matter (LOW, open, 2026-08-11 — CC: fixture)

- **Observed.** Must stay silent — §5 says map, don't rename.

## RANDOM SECTION NAME

### ORPHANED: section neither recognised nor mapped (LOW, open, 2026-08-11 — CC: fixture)

- **Observed.** Must be refused.
