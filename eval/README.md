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
| `gaia/` | how does the loop score on a public split that other agents publish scores on? | a night per campaign (~2 h per run); gated dataset, needs a Hugging Face token | 2026-08-31 |

`_harness/` is not an instrument. `provenance.py` records the conditions a run
happened under; `auto_approve.py` answers the Tier 1 approval prompt in a
headless eval and nowhere else; `benchmark_tools.py` is the allow list of agent
tools a benchmark run may hold (`tool_map()`, `benchmark_firewall(workdir,
profile, template=None)`), so a tool nobody listed is off.

## GAIA — how to run it (2026-09-23)

One command, from `dpc-client/core`, after a dry run:

    uv run --with pyarrow python ../../eval/gaia/campaign.py --dry-run
    uv run --with pyarrow python ../../eval/gaia/campaign.py --hours 7.5

Before it:

- **Stop the DPC service.** It holds the model through its own llama-server
  child, and the card has room for one: the campaign needs 26 000 MiB free,
  waits for it at most `--wait-budget-minutes` (30), and says who holds it.
- **The alias is `qwen3.8 27b`** (`--alias` to change it), a `llamacpp_server`
  entry in `~/.dpc/providers.json`. Its `model` field is a free label and is
  stale (`qwen3.8 27b Mythos`); reports name the model by its GGUF and SHA-256.
- **A Hugging Face token** with the GAIA licence accepted: `HF_TOKEN`, or the
  one `hf auth login` stored. It is removed from the agent's environment once
  the attachments are fetched, and the stored one is fenced off too.

`--dry-run` checks the alias, the pinned llama-server binary and its version,
the GGUF and mmproj (hashed once, digest cached), the effort words against the
model's own template, the token against the gated repo, readable copies of the
answers, free VRAM, the results directory and the tool set — and downloads
nothing and loads no model. A real start runs the same checks and exits 2 if
any fails. The night ends with one line and one status: 0 clean, 1 a run
failed or timed out, 2 preflight, 3 contaminated, 4 the card never came free.
A run is killed with its whole tree after `--run-timeout-minutes` (240), and a
task is recorded as a timeout after 45 min (the longest of 583 past tasks took
31).

What a run measures: 53 GAIA Level 1 validation tasks with their 11
attachments, each task in its own agent root, the agent holding 32 of 72 tools
(web, files inside the task root, shell, documents, images, audio; no
archives, chat history, personal context, messaging, scheduling or git),
Tier 1 answered by the eval approver only for inline code inside the task
(no installs, nothing reaching the home directory or credentials), scored by
the official leaderboard scorer's rules on the FINAL ANSWER span. The queue
is t0-xhigh, t0-low, t1-xhigh, t1-low.

**Not comparable with what came before.** The eleven runs of 2026-08-29 to
08-31 ran `27B-Cold-Fusion-GAIN-V1.1-NVFP4-MID-HIGH.gguf` (per their
provenance files; today's alias points at `Qwen3.8-27B-Uncensored-IQ4_XS.gguf`),
all 70 tools then registered, the old approver, a shorter prompt, and a scorer
that removed articles; the run of 2026-08-25 had no firewall at all.

## State of the set, 2026-09-23

- **`gaia/` has not run since 2026-08-31, and its harness changed underneath
  it on 2026-09-23** — the campaign named an alias that no longer existed, the
  approver passed commands that left the sandbox, and every tool was on. The
  eleven post-guard runs (2026-08-29 to 08-31) scored **47.2 %–66.0 %** (25–35
  of 53) as stored; re-scored with the official scorer's rules on the 539 of
  583 rows whose stored text was not truncated, five runs moved by one task
  and the range is **47.2 %–67.9 %**. The 69.8 % of 2026-08-28 is not in
  either: that run read the answer file. None of these is comparable with a
  run on the new harness (see above).

## State of the set, 2026-08-30

- **`gaia/` is the only one that runs on a schedule.** Five campaigns since
  2026-08-27, fifteen runs, eleven of them scored, 20.6 hours of run time: 53
  Level 1 tasks each, accuracy 47.2 %–69.8 %. Read the caveats at the top of
  `gaia/run_gaia_eval.py` before quoting any of that outside this repository —
  in particular, a single run's figure sits inside that spread, not above it.
  *(2026-09-23: 69.8 % was a contaminated run; the clean range is above.)*
- **`kv/` answered its question and stopped.** q4_0/q4_0 against q8_0/q8_0 at
  32 157 / 120 137 / 252 477 tokens: nine comparable cells, identical replies in
  both arms. The first pair's three computational cells are not among the nine —
  a 256-token output budget went to thinking and they were re-run at 4096, which
  is what `*-compute.json` holds. Two of those three discarded cells had
  diverged, both as an empty reply from the q8_0 arm.
- **`retrieval/` and `loop/` have each run once, on 2026-08-24** — the day they
  were written. Whatever this file says below about the precedent that ran
  once and was never re-run, half the set is currently in that state.
  *(2026-09-23: a separate change moves `loop/` onto the local llama-server;
  nothing here describes it yet.)*
- **GAIA results are not in git, and since 2026-09-01 are not in the tree
  either** — `~/.dpc/eval-results/gaia`, 1 633 files, 15.4 MB, moved with every
  SHA256 checked. They stayed out on evidence, not taste: an audit of all of
  them found a disk serial in 45 files, three peers' node ids, 86 scraped
  third-party addresses, and in four files the previews of our own chat that a
  run read back through `read_session_archive`. Reports and traces still exist
  on one disk only, which is the thing this line has been saying since the
  count was 1 029.
- **New output from every harness goes to `~/.dpc/eval-results/<benchmark>/`**
  (`results_root()` in `_harness/`, `DPC_EVAL_RESULTS` overrides the base).
  `kv` holds a constant and was rewired; `loop` and `retrieval` take their
  output path from the caller and have nothing to rewire. The seven result
  JSONs already committed under `kv/`, `loop/` and `retrieval/` stay tracked —
  they are the only eval numbers this repository publishes.
- **Corrected 2026-09-01, and the correction is the point.** The line above used
  to end «deleting tracked files would solve a problem those seven do not have».
  Fable 5 checked instead of agreeing: **four of the seven carry it** — each
  `kv/results/*.json` records the `binary` and `gguf` fields as absolute paths on
  the machine that ran the probe, and that machine's home directory contains the
  operator's account name. Same class as what the GAIA move stopped publishing.
- **Nothing there is configuration.** `_alias()` reads `~/.dpc/providers.json` and
  `_binary()` resolves the pinned install root, both on the machine running the
  probe (`ab_key_quant.py:101-119`), so another operator's run records their own
  paths and nothing has to be edited first. The two fields are provenance of one
  past run, not a setting anyone inherits.
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
