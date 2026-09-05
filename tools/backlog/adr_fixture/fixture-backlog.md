# Fixture backlog — exists only so --check has a file to be pointed at

The rules under test live in `docs/decisions/` next to this file. One deliberate violation
per ADR rule, plus one clean decision that must produce nothing at all.

Run it: `uv run python tools/backlog/build.py --check tools/backlog/adr_fixture/fixture-backlog.md`

Expected: 11 refusals, 3 warnings, exit 1 — two of the warnings come from `docs/GLOSSARY.md`
beside this file (a link to no file, a link to no heading), and its five axis rows must stay
silent (2026-09-05). Three decisions must produce nothing at all:
the clean one, the superseded one and the proposed one — the last two are the escapes
invariant И1 leaves open, and a fixture that did not hold them would let the check
start shouting at every decision the project has already replaced or not yet taken.
(Was 7 and 1 until 2026-09-01, when the axis rule arrived. Measured, not remembered.)

Not named `backlog.md`: `.gitignore:137` matches that name at any depth, so the file would
have been swallowed and the fixture would arrive in a clone with its entry point missing.
`build.py` derives `docs/decisions/` from whatever file it is pointed at, so the name is
free.

## OPEN
