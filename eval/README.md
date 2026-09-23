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
| `loop/` | does the agent loop finish the kind of task we actually give it? | about two minutes — the local `llamacpp_server` alias `qwen3.8 27b`, one entry copied from `~/.dpc/providers.json`, the file untouched; needs 26 000 MiB free | 2026-09-23 |
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
    uv run --with pyarrow python ../../eval/gaia/campaign.py --hours 11

`--hours 11` fits the four-run queue: a run is started only while at least
`--minutes-per-run` (170) remain, and on 2026-09-23 a run took 142–156 min, so
`--hours 7.5` runs two and skips the rest by design.

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

**Published answer keys are refused in the run** (`_harness/answer_key_policy.py`,
versioned and hashed in provenance): the web tools refuse URLs shaped like a GAIA
answer list — the dataset's own Hugging Face pages and discussions, `Who_and_When`,
`harbor-datasets`/`harbor-index`, HAL's GAIA analysis, `Final_Assignment` spaces,
named mirrors, any URL carrying `gaia` beside jsonl/validation/metadata/benchmark/
leaderboard/answer — refuse searches naming GAIA, drop such items from search
results and withhold a browser page that is one; every refusal is in the report.
`run_shell` is refused on its command text only, so a script that fetches a mirror
is caught by the scan below, not prevented.
**`accuracy_clean`** counts correct answers of tasks that never met an answer key:
a call naming such a URL (fetched or refused), a query naming GAIA, a listed
answer-key page showing the task's answer, a dataset-row marker (`groundtruth`,
`"Final answer"`, `Expected answer`) in a tool result, or an answer naming its
source. A correct task that *reached* one — not refused — makes the run exit 3.

**Not comparable with what came before.** The eleven runs of 2026-08-29 to
08-31 ran `27B-Cold-Fusion-GAIN-V1.1-NVFP4-MID-HIGH.gguf` (per their
provenance files; today's alias points at `Qwen3.8-27B-Uncensored-IQ4_XS.gguf`),
all 70 tools then registered, the old approver, a shorter prompt, and a scorer
that removed articles; the run of 2026-08-25 had no firewall at all.

## loop — how to run it (2026-09-23)

From `dpc-client/core`, the DPC service stopped or its model unloaded:

    uv run python ../../eval/loop/run_loop_eval.py --dry-run
    uv run python ../../eval/loop/run_loop_eval.py --tier hard

`--provider-alias` (default `qwen3.8 27b`) copies that entry verbatim out of
`~/.dpc/providers.json`, the way `gaia/` does, so the run is the production
configuration rather than a copy of it that drifts; Ollama stays reachable
only as an explicit `--providers eval/loop/providers.eval.json`. Below 26 000
MiB free the harness refuses to start a `llamacpp_server` run, and it stops
its own child on exit — normal, exception or Ctrl-C — through
`LLMManager.shutdown()`. Answer checks were substring containment until
2026-09-23, so `14` satisfied a gold of `4`; they match whole tokens now, and
file-content checks stay exact substrings. Results go to
`~/.dpc/eval-results/loop/` with the same provenance block as `gaia/`.
Still open: the loop agent runs with no firewall, every tool on — it should
take `_harness/benchmark_tools.benchmark_firewall` as `gaia/` does.

## State of the set, 2026-09-23

- **`loop/` ran again, on the production path.** Easy tier on `qwen3.8 27b`
  through llama-server: **10/10 in 125 s**. Ten out of ten on the only tier
  run is still the result this file tells you to distrust; the hard tier has
  not been run on llama-server yet.

- **`gaia/` ran on the new harness: `20260923-0543`, t0-xhigh and t0-low,
  40/53 and 41/53 reported, 37/53 and 37/53 clean.** The agent searched for
  GAIA by name and read published answers in 3 and 4 correct tasks (xhigh 005,
  014, 016; low 004, 005, 014, 017), and both runs exited 0. Both numbers
  predate the answer-key policy above; the next run is the first under it.
- **Before that campaign `gaia/` had not run since 2026-08-31, and its harness
  changed underneath it on 2026-09-23** — the campaign named an alias that no longer existed, the
  approver passed commands that left the sandbox, and every tool was on. The
  eleven post-guard runs (2026-08-29 to 08-31) scored **47.2 %–66.0 %** (25–35
  of 53) as stored; re-scored with the official scorer's rules on the 539 of
  583 rows whose stored text was not truncated, five runs moved by one task
  and the range is **47.2 %–67.9 %**. The 69.8 % of 2026-08-28 is not in
  either: that run read the answer file. None of these is comparable with a
  run on the new harness (see above). *(Later the same day it ran — the line
  above.)*

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
  *(2026-09-23: `loop/` has moved onto the local llama-server and ran again —
  see above.)*
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
