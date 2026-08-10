# Fixture backlog — exists only so --check has a file to be pointed at

The rules under test live in `docs/decisions/` next to this file. One deliberate violation
per ADR rule, plus one clean decision that must produce nothing at all.

Run it: `uv run python tools/backlog/build.py --check tools/backlog/adr_fixture/fixture-backlog.md`

Expected: 7 refusals, 1 warning, exit 1. The clean decision must produce nothing at all.

Not named `backlog.md`: `.gitignore:137` matches that name at any depth, so the file would
have been swallowed and the fixture would arrive in a clone with its entry point missing.
`build.py` derives `docs/decisions/` from whatever file it is pointed at, so the name is
free.

## OPEN
