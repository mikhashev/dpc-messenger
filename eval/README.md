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
| `loop/` | does the agent loop finish the kind of task we actually give it? and, `--tier long --preserve-reasoning both`, does carrying a round's notes change how a deep run ends? | easy/hard about two minutes; long estimated 3–7 hours for both arms (never run) — the local `llamacpp_server` alias `qwen3.8 27b`, one entry copied from `~/.dpc/providers.json`, the file untouched; needs 26 000 MiB free | 2026-09-23 (long: step 0 on 2026-10-06, unseeded, not reproduced) |
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
~~Still open: the loop agent runs with no firewall, every tool on.~~ Closed
2026-10-06: every tier runs under `_harness/benchmark_tools.benchmark_firewall`
(profile `loop_benchmark`, rules file in the run's workdir), and
`--auto-approve` attaches `Tier1AutoApprover` as `gaia/` does. Not yet run with
either.

## loop, long tier — the `preserve_reasoning` A/B (2026-10-06, step 0 run unseeded, not reproduced)

The question is the board card
`THE-MODEL-STARTS-EVERY-ROUND-WITHOUT-THE-REASONING-THAT-CHOSE-THE-TOOL`: does
sending a tool round's own notes back to llama-server (`preserve_reasoning`,
commit `2407f64f`, per alias, default off) change how a long run ends? Dry run,
then the step-0 preflight, then — only if the preflight says yes — the run, from
`dpc-client/core`, the DPC service stopped:

    uv run python ../../eval/loop/run_loop_eval.py --dry-run --step0-only --seed-history ~/.dpc/eval-results/loop/seeds/incident-2026-10-05-johnny-deep.json --auto-approve
    uv run python ../../eval/loop/run_loop_eval.py --step0-only --seed-history ~/.dpc/eval-results/loop/seeds/incident-2026-10-05-johnny-deep.json --auto-approve
    uv run python ../../eval/loop/run_loop_eval.py --tier long --preserve-reasoning both --auto-approve

(The A/B line takes the same `--seed-history` once step 0 says yes with it. The
incident seed, `incident-2026-10-05-johnny.json`, stays beside the deep one,
untouched; it is what Johnny loaded, and it is too shallow for the floor.)

**Seed files never enter the repository** — not the seed, not a copy, not a
fixture cut from it. They live under `~/.dpc/eval-results/loop/seeds/`; the
tests use synthetic records only.

- **Seeding — why the first step 0 measured nothing.** Step 0 ran unseeded on
  2026-10-06 (`long-qwen3.8_27b-step0-20261006-114852.json`): both off-arm tasks
  finished in 5–6 rounds, peak prompt 16–24 k, no budget hit — incident not
  reproduced. The incident's round 1 was already 67 177 tokens (per the board
  card); the agent had loaded its group's history with the task, 43 records
  (`dpc-client.log.1`, 2026-10-05 17:54:49: "Loaded 43 messages"), rendered as 42
  history turns, one of them its own ("History turns for reader Johnny: 42
  total, 1 assistant"). A fresh root starts near 10 k. `--seed-history PATH`
  (long tier only; refused on easy and hard) puts such a history in front of
  every task (`loop/seed_history.py`). **Rendered by production**: the seed
  reaches `DpcAgent.process` through the arguments `agent_manager` passes — a
  monitor serving the records, the reader's identity, the trigger's id — so
  `build_llm_messages` and `derive_history_role` make the turns, the reader's
  own records as assistant. `--seed-reader NAME` (default `Johnny`) names the
  reader; its identity has to be in the seed file. **The task replaces the
  trigger**: the seed's last record keeps its index, time and sender and takes
  the task as its body, so the prompt has the incident's shape (42 turns, then
  one user turn) and the model is not handed a question nobody scores.
- **The seed is private and stays out of git and out of reports.** The
  incident seed is `~/.dpc/eval-results/loop/seeds/incident-2026-10-05-johnny.json`:
  records `#1–#43` of the group's live `history.json`, chain hashes recomputed
  from genesis through `#43`, `#43` the trigger at 10:54:49.831Z. It is group
  chat — the same reason GAIA results left the tree (see *State of the set*:
  four files carried previews of our own chat). A seeded report and its
  provenance carry the seed's path, sha256, record count, depth figures and,
  for a deep seed, its source files, index ranges and hashes — never a record. Per row, a seeded run drops the answer text (the scored
  values stay as `answer_fields`, with `answer_chars`), digests each round's
  note opening (`sha256:…`; repeat detection compares for equality only), and
  keeps only an exception's type; then any string still sharing 40+ characters
  with the seed — a shell command in the approver's summary, a reason — is
  replaced by `[withheld: …]`, counted in `seed_history.withheld_strings`. A
  paraphrase passes that net, which is why the answer is dropped, not filtered.
- **Depth before any model, in engine tokens.** With a seed, `--dry-run` and the
  run itself build every task's round-1 request through a real `DpcAgent` on a
  probe root (its adapter's `chat` replaced by a recorder that answers at once)
  and print, per task, `round 1 ENGINE E (tokenizer|calibrated); chars/4 T =
  request R + tool schemas S [floor 60000 engine tokens] ok|BELOW`. The floor is
  step 0's prompt floor, which is in what llama-server reports as
  `prompt_tokens`, so it is compared with the engine-scale figure E, never with
  chars/4: production's estimator reads this conversation low — the incident's
  round 1 logged "Context size: estimated 41012" (tool schemas excluded) while
  the engine counted 67 177 with 52 schemas (`dpc-client.log.1`, 2026-10-05
  17:54:53 and 17:56:41, both read 2026-10-06). **E is counted with the model's
  own tokenizer**: `llama-tokenize` from the pinned build's directory (beside
  `llama-server`) over the alias's GGUF. That tool opens the file with
  `vocab_only` ("vocab only - skipping tensors", 0.5 s on the 27B file, no
  weights) and runs with `CUDA_VISIBLE_DEVICES=-1`, so no GPU is touched; the
  text goes in on stdin. What it counts is the request in ChatML framing plus the
  tool schemas as JSON lines — the chat template's own fixed wording around the
  tools is not reproduced. Where the binary or the GGUF is missing (another
  provider type, a missing pin), E falls back to the incident's ratio, 67 177 /
  41 012 = 1.638 applied to the request's chars/4 figure without tool schemas,
  and is printed and recorded as `calibrated`, not counted. That ratio has the
  incident's 52 schemas folded in, so with 7 it reads high: on the incident seed
  it says 31 068 where the tokenizer counts 26 342. Below `--seed-depth-floor`
  the run refuses before loading anything; the flag lowers it on purpose, and
  the report records the value, its unit and the method.
- **The incident seed is ~26 k engine tokens, not 60 k** (dry run, 2026-10-06:
  26 342 and 26 364 counted on the two step-0 tasks; 19 984 and 20 023 on
  chars/4). The 42 turns are 57 094 characters of content. The rest of the
  incident's 67 177 came from what a throwaway root does not have — Johnny's own
  system prompt, identity and scratchpad (23 842 + 23 270 + 84 911 characters
  on disk on 2026-10-06, not at the incident), Active Recall's three hints, and
  52 tool schemas against 7.
- **The deep seed — more conversation, not what Johnny saw.** Mike's order of
  2026-10-06: deepen the seed from the conversation history.
  `~/.dpc/eval-results/loop/seeds/incident-2026-10-05-johnny-deep.json` is the
  incident seed's 43 records unchanged (same order, the trigger still replaced
  by the task) with 73 records put in front: `#96–#168`, the newest end of the
  group's previous session (`archive/2026/10/2026-10-05T08-29-59_reset_session.json`),
  taken whole, newest first, until the tokenizer count reached the target of
  67 000 (the incident's level) and stopped at the record that crossed it. **A
  reset sits between those 73 and the incident's 43: production would not have
  loaded them**, and the file's `deepening` block, the dry run's `DEEP SEED`
  line and every report's `seed_history.deepening` say so. The block lists the
  source file with its sha256, the index range and count taken, how many came
  from outside the incident's loaded history (73), how many the renderer skips
  anyway (0 — every one has content and a `sender_type`; 26 carry `tool_calls`
  metadata, which the renderer does not read), and the base seed's sha256.
  Prepended records keep their own index as `source_msg_index` and carry
  `msg_index: null`: the earlier session counted from 1 again, and
  `select_prior_history` drops every integer index at or above the trigger's,
  so they render as `[HH:MM:SS | sender]` without `#N`. Role derivation holds
  for all of them (5 are Johnny's, all assistant turns; the group/1:1 check finds
  no record that would change role). The dry run counts round 1 at 67 868 and
  67 889 engine tokens (a few tokens move between runs: the turn context carries
  the clock) — within 67 000 ± 10 % and below Johnny's compaction
  trigger (0.5 × 215 040 = 107 520), so the off arm starts where the incident
  started, not past it. `deepen` in `loop/seed_history.py` does the taking; the
  run that wrote the file was a script outside the repository.

- **Attempt 3 (built 2026-10-06, not yet run).** Two step 0s said no: unseeded
  (5–6 rounds, peak 24 k) and seeded with the deep seed (round 1 ≈ 67.8 k engine
  tokens, as the incident's 67 177; 14 and 6 rounds, 0 budget hits, 13/14 and
  5/6 silent rounds, 120.2 s and 120.5 s per task —
  `long-qwen3.8_27b-step0-seeded-20261006-124416.json`). The reviewers' reading
  (Ark, Zcode): depth is necessary but not sufficient — "find these constants"
  is cheap to re-plan. Ark's rival hypothesis: the burning belongs to a question
  the model cannot close, not to forgotten reasoning; only an off arm that
  actually burns can tell the two apart. Attempt 3 adds three tasks to the
  eight (the eight are unchanged) and four instruments:
  - *`long-audit-claims`* (kind `incident`) — five audit-style claims about the
    memory-index and agent-loop code touched by 339028b2 / 5fc168fa / 2407f64f,
    each answered `claim_N=holds|fixed|partly` plus `claim_N_where=<file>:<line>`.
    `holds` means the code at the snapshot does what the claim says, `fixed` that
    it does not (repaired or never true), `partly` that one part is true and
    another is not. Golds: 1 `fixed` — BM25 keeps full texts in
    `bm25_texts.json` and rebuilds from them, so the claim is **refuted by the
    code** and an "the audit is right" answer fails; 2 `holds` —
    `sync_firewall_settings` is still `pass`; 3 `fixed` — a knowledge write
    replaces the file's rows (`write_file` → `replace_file_in_index`, two files);
    4 `holds` — `_meta.json` is keyed by basename in `knowledge/`, so
    `knowledge/a/notes.md` and `knowledge/b/notes.md` share one entry (two
    files); 5 `partly` — `repo_delete` drops the index rows but never touches the
    `_meta.json` entry claim 4 located (returns to earlier findings). A place is
    scored by span: it passes when the line falls inside any accepted function
    span of the right file, and every span is recomputed by `derive` from the
    snapshot like the verdicts.
  - *`long-control-unresolvable`* — at what prompt size compaction fires for an
    agent whose config is not in the snapshot. The default (0.8,
    `CompactionState.__init__`) applies only when the per-agent config names no
    threshold, and the config is read from `~/.dpc/agents/<id>/config.json`
    (`loop.py` → `load_agent_config`), which no snapshot holds: insufficient
    evidence, not conflicting. Scored `settleable=no` plus `evidence_a` /
    `evidence_b` naming both places in either order; a confident number fails.
  - *`long-control-multistep`* — answerable, a chain of six dependent lookups
    (`write_file` → `l5_key` in `index_keys.py` → the key for
    `knowledge/a/notes.md` → `replace_file_in_index` → preview length →
    `index_meta.json` / `file_hashes` / `index_meta.py` → hash length), each
    step's file named by the answer before it.
  - **What step 0 then says** (`step0_outcome` in `loop/round_metrics.py`),
    per task whether the off arm burned: only on the unresolvable control →
    the rival hypothesis; on the audit or multistep task (and not on the
    control) → the card's mechanism is plausible; on both → not separated;
    nowhere → "not reproduced outside production", with the residual gap
    named: a throwaway root has no Johnny system prompt, identity, memory or
    Active Recall.
  - **The intersection axis.** Per round, *silent AND at the budget*
    (`silent_budget_hits`); the incident had 7 — every capped round was a
    silent one (per the card, not re-read here). It is printed per task, in
    total, on the step-0 verdict line (`silent AND at budget: N [the incident:
    7]`) and as `silent_and_hit` in the verdict, beside the two separate axes.
    It is reported, not added to the symptom rule.
  - **Repeats.** `--repeats N` (long tier, default 1) runs each selected task N
    times per arm, each in a fresh root (`long-NN-rK-arm`); step 0 prints
    `budget-burn reproduced k/N` per task. `--task-ids a,b,c` picks tasks by id.
    The dry run prints the run-time estimate: task-runs × 120 s (seeded; 45 s
    unseeded), both from the step 0s of 2026-10-06, with the ceiling at the
    45-min task timeout — a run that burns takes far longer than one that did
    not.
  - **Effort provenance.** `reasoning_effort_requested` (renamed from
    `reasoning_effort_sent`; no reader of the old key exists in the
    repository) is the word asked for. `served_effort` is the rung each round
    ran on, named by production's own rule — `Gateway._served_effort`, called
    unbound: the provider's word read off the body it sent, else the requested
    word on the alias's ladder, else the alias's default — per round, per task
    and as a run total. `alias_reasoning_effort_default` is the alias's own
    `reasoning_effort` field.
  - **The seed is named.** A seeded report's `seed_history` carries the path,
    sha256 and `approximation: {records_beyond_incident_history,
    exceeds_incident_history}`, and the step-0 verdict line ends
    `approximation: 73 records beyond the incident's history` for the deep seed.
  - **The attempt-3 step 0** (estimated ~18 min if nothing burns, ceiling 6.75 h):

        uv run python ../../eval/loop/run_loop_eval.py --step0-only \
          --task-ids long-audit-claims,long-control-unresolvable,long-control-multistep \
          --repeats 3 --auto-approve \
          --seed-history ~/.dpc/eval-results/loop/seeds/incident-2026-10-05-johnny-deep.json

  - **If attempt 3 also says no — the agreed plan, not done:** attempt 4 runs
    the same tasks with the seed plus Johnny's real `system_prompt` and
    knowledge in the root. After four "no"s the card is recorded as
    "plausible, 0 of N reproductions" and `preserve_reasoning` stays off (the
    owner's decision).

- **The preflight first.** `--step0-only` is the long tier's off arm on the first
  two tasks (`--tasks N` for another count), under the same run conditions as
  the full run, printing only the step-0 verdict. Estimated 20–50 min against
  3–7 hours for the A/B: if the off arm never reaches the incident's regime here,
  the full run would measure nothing about the flag, and the owner decides before
  spending the card for an evening. It is the full run's code path with one arm
  and fewer tasks, not a separate harness.

- **The arm flag.** `--preserve-reasoning off|on|both` (any tier) sets the key on
  the *copied* provider entry; the operator's file is hashed before and after and
  the report says whether it changed. `off`, the default, is the behaviour before
  the flag: the alias carries no such key. `both` runs every task under each arm
  in its own root, order alternating per task; one child serves both arms and the
  flag is set on the live provider before each run, so no model reload between.
- **The tasks** (`loop/tasks_long.py`, 8 of them, 11 since attempt 3) read this repository's own
  source — `git archive` of `dpc_agent/`, `agent_manager.py` and two providers at
  the commit the run starts at, copied into each task root, because the approver
  never answers a sandbox-boundary question. Each names 2–5 files of 92–319 k
  characters and ends in `key=value` lines scored deterministically. Every gold
  was written by reading the code at `57ba6879` and is re-derived mechanically
  (AST or an anchored regex) from the run's snapshot before the first task; a gold
  the snapshot no longer supports stops the run with exit 2. Tools: read, list,
  search, shell, scratchpad — no web (the repository is public; a model reading
  it on GitHub would answer from another commit). `long-compaction-ladder` asked
  for "the first round number" at which truncation starts; the gold 9 came from
  `round_idx > 8`, and step 0's answer of 10 was a reading that wording allowed.
  `run_llm_loop` sets `round_idx = 0` and increments it before
  `apply_compaction` and the call, so the first round is 1 and truncation first
  applies at `round_idx` 9. The question now asks for the value of the loop's
  own `round_idx` (key `first_truncation_round_idx`), and the derive checks the
  loop still hands that variable over before the call.
- **Run conditions copied from the incident's agent** (Johnny's config, read
  2026-10-06): effort `medium`, compaction on at threshold 0.5 with the window
  the loop resolves for the alias (215 040, so it fires at 107 520). The
  summariser differs on purpose: Johnny's is a cloud alias; here it is the local
  alias, the only provider the run holds, so no paid call can happen. `--compaction
  off` gives the throwaway default instead, deterministic truncation after round 8
  — which keeps the prompt small and could not reproduce the incident.
- **Step 0 is printed first.** The incident (2026-10-05, Johnny on `qwen3.8 27b`):
  26 rounds, prompt 67 177 → 93 603, seven rounds at the ~10 k note budget,
  eleven of twelve rounds from 14 on with no visible text. A long run prints
  `incident symptom reproduced in the off arm: yes/no (budget hits N, peak
  prompt P, budget-hit share past 60000: h/r = S)` — yes when one off-arm task
  had ≥ 2 budget hits, a prompt ≥ 60 000, **and** ≥ 0.25 of its rounds past
  60 000 at the budget. The share keeps two capped thoughts lost in a long run
  from passing; 0.25 accepts the incident whether its deep rounds were the 12
  from round 14 on (7/12 = 0.58) or all 26 (7/26 = 0.27) — the derivation is at
  `SYMPTOM_MIN_BUDGET_SHARE` in `loop/round_metrics.py`. On no, the A/B measured
  nothing, and the report says so. **Not verified**:
  that a throwaway root gets there — its system prompt is ~10.3 k tokens (GAIA
  `20260924-0348-t0-low`, 1-round tasks), so ~13 full 15 000-character reads have
  to stay in the history, and the model may search instead of reading.
- **Per task and arm**: rounds, rounds-to-completion (absent on failure), success,
  budget-hit rounds by number, silent rounds and the longest silent streak, peak
  prompt, compaction round / count / failures and summariser calls, note tokens
  (and how many rows were estimated), the share of rounds whose notes open with
  the same 80 normalised characters as an earlier one, wall time.
- **Visible output is its own axis.** The verdict prints both arms side by side on
  every axis and ranks nothing; it warns when the on arm went silent in a larger
  share of rounds or compacted earlier, and then refuses to read fewer budget
  hits as a result. Totals are printed twice — over every paired task, and over
  the subset whose off arm reproduced the incident, named task by task — because
  only that subset is in the regime the flag is meant to change. A sample relayed with the brief for this instrument had the on
  arm silent in 33 of 41 rounds against 0 of 41 (not re-read here).
- **Cost: plan it, do not slot it between other work.** It needs the card free —
  the service stopped or its model unloaded, 26 000 MiB — for the whole run.
  Estimated from the incident's 26 rounds in 22.5 min and GAIA's 14–21 s per
  round: 10–25 min per task per arm, so roughly 3–7 hours for 8 tasks × 2 arms,
  more if compaction fires (each summary is a local call with its own notes).
  `--tasks N` runs the first N for a smoke pass.
- **`--rounds N`** is the agent's round limit (`AgentConfig.max_rounds`) on every
  tier. Default: 60 for long (the incident took 26), `AgentConfig`'s own default
  for easy and hard — what they ran under before the flag was wired; until
  2026-10-06 `--rounds` was parsed and never read. `--max-rounds` is kept as an
  alias.
- **Known limits.**
  - *The per-task timeout is 45 min*, 2× the incident's 22.5 min, and is kept.
    A task that reads more per round than the incident did can be cut before
    its deep rounds. A cut task is printed `TIME`, carries `timed_out: true`, has
    its own verdict axis and a warning, and step 0 names it — it is a run the
    harness stopped, not a failure of either arm. `--task-timeout-minutes`
    raises it.
  - *The window comes from the alias.* Without `context_window` on the copied
    alias the manager falls back to 4096 tokens for a model it does not know
    (`LLMManager.get_context_window`), and the compaction trigger and session
    limit would follow it in silence. The dry run refuses such an alias on every
    tier, and a long-tier run refuses it before loading anything.
- The predecessor `dpc-client/core/tests/perf/run_reasoning_carry_ab.py` stays
  until this has run; its metric code lives on in `loop/round_metrics.py`.

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
