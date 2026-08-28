# eval — instruments that produce a number about our own system

Not tests. A test says a function still does what it did; these say *how well*
the system does the thing it exists for, on the corpus we actually have.

The distinction matters because we had 180 test files and 2 480 passing tests
on the day it turned out nobody could say whether retrieval works.

## What is here

| harness | question | cost |
|---|---|---|
| `retrieval/` | does the index find a card, and does fusing BM25 with vectors help? | none — local encoder, no network, no paid call |

## Precedent

`docs/decisions/adr-033-benches/` came first and is the model to follow: five
progressive benches, each of which **killed a hypothesis** before production
code was written. Its weakness is that it ran once, to settle one design
question, and nothing re-runs it. These are meant to be re-run.

## Rules

- **Deterministic scoring first.** An LLM judge is a scorer that itself needs
  verifying; it is the expensive tier bought before the cheap one. Use one only
  where determinism provably cannot reach, and say so.
- **A number carries its population and its window.** Which agent, how many
  items, which backend, what date. A figure without them starts an argument
  about denominators rather than about the system.
- **Report absent separately from zero.** Never fold "could not measure" into
  "measured zero" — that mistake is why a metric here read zero for four months.
- **The first run should look bad.** If a new instrument reports a good number
  immediately, suspect that it is not touching the thing it claims to measure.
