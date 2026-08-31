# eval — instruments that produce a number about our own system

Not tests. A test says a function still does what it did; these say *how well*
the system does the thing it exists for, on the corpus we actually have.

The distinction matters because we had 180 test files and 2 480 passing tests
on the day this file was written (2026-08-24) and nobody could say whether
retrieval works.

## What is here

Inventory taken 2026-08-30. Four instruments and one support package. Every
one of them runs against a local model, so **none of them spends money** — the
cost is the card and the hours.

| harness | question | cost | last run |
|---|---|---|---|
| `retrieval/` | does the index find a card, and does fusing BM25 with vectors help? | seconds — the embedding encoder production already loads, no model served, no network (189 queries in 1 s) | 2026-08-24 |
| `loop/` | does the agent loop finish the kind of task we actually give it? | a minute — a local model through the harness's own provider file, `~/.dpc/providers.json` untouched | 2026-08-24 |
| `kv/` | does K quantised to q4_0 diverge from q8_0 as depth grows? | the whole card and ~35 min of answer time for four arms; refuses to start while the DPC service holds VRAM | 2026-08-30 |
| `gaia/` | how does the loop score on a public split that other agents publish scores on? | a night per campaign (~2 h per run); gated dataset, needs `HF_TOKEN` | 2026-08-30 |

`_harness/` is not an instrument. `provenance.py` records the conditions a run
happened under; `auto_approve.py` answers the Tier 1 approval prompt in a
headless eval and nowhere else.

## State of the set, 2026-08-30

- **`gaia/` is the only one that runs on a schedule.** Five campaigns since
  2026-08-27, fifteen runs, eleven of them scored, 20.6 hours of run time: 53
  Level 1 tasks each, accuracy 47.2 %–69.8 %. Read the caveats at the top of
  `gaia/run_gaia_eval.py` before quoting any of that outside this repository —
  in particular, a single run's figure sits inside that spread, not above it.
- **`kv/` answered its question and stopped.** q4_0/q4_0 against q8_0/q8_0 at
  32 157 / 120 137 / 252 477 tokens: nine comparable cells, identical replies in
  both arms. The first pair's three computational cells are not among the nine —
  a 256-token output budget went to thinking and they were re-run at 4096, which
  is what `*-compute.json` holds. Two of those three discarded cells had
  diverged, both as an empty reply from the q8_0 arm.
- **`retrieval/` and `loop/` have each run once, on 2026-08-24** — the day they
  were written. Whatever this file says below about the precedent that ran once
  and was never re-run, half the set is currently in that state.
- **GAIA results are not in git, and since 2026-09-01 are not in the tree
  either** — `~/.dpc/eval-results/gaia`, 1 633 files, 15.4 MB, moved with every
  SHA256 checked. They stayed out on evidence, not taste: an audit of all of
  them found a disk serial in 45 files, three peers' node ids, 86 scraped
  third-party addresses, and in four files the previews of our own chat that a
  run read back through `read_session_archive`. Reports and traces still exist
  on one disk only, which is the thing this line has been saying since the
  count was 1 029. The other three harnesses commit their results.
- Results from `retrieval/` and `loop/` carry no date inside the file — the
  window lives only in the filename. `provenance.py` fixed that, for `gaia/`
  alone; it has never been wired into the older two.

## Precedent

`docs/decisions/adr-033-benches/` came first and is the model to follow: five
progressive benches, each of which **killed a hypothesis** before production
code was written. Its weakness is that it ran once, to settle one design
question, and nothing re-runs it. These are meant to be re-run.

## Rules

- **Deterministic scoring first.** An LLM judge is a scorer that itself needs
  verifying; it is the expensive tier bought before the cheap one. Use one only
  where determinism provably cannot reach, and say so. As of 2026-08-30 no
  instrument here uses one, and all four say so in their own header.
- **A number carries its population and its window.** Which agent, how many
  items, which backend, what date. A figure without them starts an argument
  about denominators rather than about the system. This inventory carries a date
  for the same reason.
- **Report absent separately from zero.** Never fold "could not measure" into
  "measured zero" — that mistake is why a metric here read zero for four months.
  `kv/` keeps "empty" apart from "wrong" for exactly this reason.
- **The first run should look bad.** If a new instrument reports a good number
  immediately, suspect that it is not touching the thing it claims to measure.
