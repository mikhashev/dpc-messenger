"""Does the agent loop finish the kind of task we actually give it?

The second instrument. Until this ran, every statement about the loop in this
repository — including the ones in the backlog — was an anecdote: nobody had
ever scored a run.

Deliberately **not** GAIA. The task set is ours: read a named file and answer
from it, count something, list a directory, write a file that must then exist.
Each check is deterministic — a whole-token match, or an artefact that must
be on disk. No LLM judges anything here; a scorer that itself needs verifying
is the expensive tier bought before the cheap one.

Runs against the local `llamacpp_server` provider by default — the alias
`--provider-alias` names (default `qwen3.8 27b`) is copied verbatim out of
the operator's `~/.dpc/providers.json`, the same pattern
`eval/gaia/run_gaia_eval.py`'s `providers_file_for` uses. Nothing is written
back to the operator's file. Ollama stays available as an explicit opt-in via
`--providers eval/loop/providers.eval.json`.

The agent gets a throwaway root under a temp dir: no real agent's memory is
read or written. Results are written under `results_root("loop")` unless
`--json` gives an explicit path.

The DPC service normally holds the same model in VRAM; two children do not
fit on one card. Before a `llamacpp_server` run, free VRAM is checked against
the threshold `eval/kv/ab_key_quant.py` uses for a 27B model on one card —
refusing loudly beats an OOM mid-run.

Run from `dpc-client/core`:

    uv run python ../../eval/loop/run_loop_eval.py

`--tasks N` for a smoke pass, `--tier hard` for the harder set, `--rounds N` for
the agent's round limit on any tier, `--dry-run` to validate the
alias/binary/gguf/VRAM/output-dir without loading a model, `--step0-only` for
the long tier's preflight (off arm, two tasks, step-0 verdict).

**Firewall (2026-10-06).** Every tier runs under
`_harness/benchmark_tools.benchmark_firewall`, a rules file owned by the run in
its workdir — never the operator's `~/.dpc/privacy_rules.json` — with the
benchmark allow list (the long tier narrows it to `tasks_long.LONG_TIER_TOOLS`).
Before this the loop agent ran with no firewall and every tool on.
`--auto-approve` attaches `_harness/auto_approve.Tier1AutoApprover`, as `gaia/`
does; its yes/no counts and every refusal go into the report.

**The `preserve_reasoning` A/B.** `--preserve-reasoning off|on|both` sets that
key on the copied provider entry — the copy, never the operator's file. `off`
(default) is the behaviour before the flag existed: the operator's alias carries
no such key and the provider defaults it to false
(`llamacpp_server_provider.py:223`). `both` runs every task under each arm, each
in its own root; one child serves both arms, the attribute is set on the live
provider before each run (it is read per call, `:987`, and nowhere else), so no
model is reloaded between arms. Every result row and the provenance name the arm.

**The long tier** (`--tier long`, `tasks_long.py`) is the one built for that A/B:
code-reading tasks over a `git archive` snapshot of this repository, deep enough
to reach the 2026-10-05 incident's prompt sizes. Its runs use the production
compaction trigger of the incident's agent (threshold 0.5, the window the loop
resolves for the alias) with the local alias as summariser, the agent's effort
word (`medium`), and print **step 0** first: whether the off arm reproduced the
incident at all. If it did not, the verdict says the A/B measured nothing, and
its totals are printed over all tasks and again over the reproducing subset.

**Seeding (2026-10-06).** `--seed-history PATH` (long tier only) puts a
conversation the agent had already loaded in front of every task, rendered by
production (`seed_history.py`): the first step 0 ran on fresh roots and stayed
at 16-24 k, while the incident's round 1 carried a 42-turn group history. With a
seed, both `--dry-run` and the run itself build each task's round-1 request
through a real `DpcAgent` before any model, size it in engine tokens (the
alias's own tokenizer, `llama-tokenize --vocab-only`; else the incident's ratio,
labelled calibrated) and refuse below `--seed-depth-floor` (default 60 000, the
step-0 floor). A seeded report carries the seed's path, sha256, counts and, for
a deep seed, its source ranges — no text the model wrote.
"""

from __future__ import annotations

import argparse
import hashlib
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CORE_DIR = REPO_ROOT / "dpc-client" / "core"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # for _harness
sys.path.insert(0, str(CORE_DIR))  # so this also runs standalone, not only from core/

from _harness import benchmark_tools, provenance  # noqa: E402
from _harness.results_root import results_root  # noqa: E402
import round_metrics  # noqa: E402

OLLAMA_PROVIDERS = HERE / "providers.eval.json"
DEFAULT_ALIAS = "qwen3.8 27b"
RESULTS_DIR = results_root("loop")
LOOP_PROFILE = "loop_benchmark"
ARMS = {"off": ("off",), "on": ("on",), "both": ("off", "on")}

# The long tier's run conditions, each taken from the incident it has to reach
# (agent Johnny, `~/.dpc/agents/agent_johnny_f309700d/config.json`, read
# 2026-10-06): effort `medium`, compaction on at threshold 0.5. Johnny's
# summariser is a cloud alias (`qwen3.8-27b-noreason NeuralDeep`); here it is the
# local alias itself, the only provider the run's LLMManager holds, so no paid
# call can happen. The trigger matches production; the summariser does not.
LONG_EFFORT = "medium"
LONG_COMPACTION_THRESHOLD = 0.5
# The incident took 26 rounds; 60 leaves room to see a run that does not stop.
LONG_MAX_ROUNDS = 60
# The incident's 26 rounds took 22.5 min. A task past 45 min is recorded as a
# timeout, the same figure `gaia/` uses per task. Known limit: 2x the incident is
# not 2x a slower run — a task that reads more per round can be cut before its
# deep rounds. Such a row carries `timed_out: true` and is reported as cut off,
# not as a failure of either arm.
LONG_TASK_TIMEOUT_MIN = 45.0
# `--step0-only` runs the off arm on this many long tasks, the cheap preflight
# before the full A/B.
STEP0_ONLY_TASKS = 2

# Same figure and the same reasoning as `eval/kv/ab_key_quant.py:_free_vram_mib`
# callers: a 27B GGUF, one card, one child — not derived from this GGUF's exact
# byte size, because neither is that one.
VRAM_FREE_THRESHOLD_MIB = 26_000


def _provider_entry_for_alias(alias: str) -> Dict[str, Any]:
    """Copy one entry out of `~/.dpc/providers.json`, verbatim, unwritten-back."""
    src = Path.home() / ".dpc" / "providers.json"
    if not src.is_file():
        raise SystemExit(f"no providers file at {src}")
    raw = json.loads(src.read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("providers", [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    match = [r for r in rows if r.get("alias") == alias]
    if not match:
        raise SystemExit(
            f"no provider aliased {alias!r} in {src}. Available: "
            + ", ".join(sorted(str(r.get("alias")) for r in rows))
        )
    return dict(match[0])


def _free_vram_mib() -> Optional[int]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=False, timeout=15,
        )
        used, total = (int(x) for x in out.stdout.strip().splitlines()[0].split(","))
        return total - used
    except Exception:
        return None


def _check_vram_or_refuse(entry: Dict[str, Any]) -> Optional[int]:
    """Applies only to `llamacpp_server`: other provider types do not hold this
    card's VRAM the way the DPC service's own child does."""
    if entry.get("type") != "llamacpp_server":
        return None
    free = _free_vram_mib()
    if free is None:
        print("  VRAM: nvidia-smi unreadable — proceeding without a check "
              "(reading failed, not \"zero free\")")
        return None
    print(f"  VRAM: {free} MiB free (need >= {VRAM_FREE_THRESHOLD_MIB} MiB)")
    if free < VRAM_FREE_THRESHOLD_MIB:
        raise SystemExit(
            f"only {free} MiB free on the card — stop the DPC service first; "
            "two 27B children on one card is an incident, not an experiment"
        )
    return free


def resolve_max_rounds(tier: str, rounds: Optional[int]) -> int:
    """The agent's round limit for this run: `--rounds`, else the tier's own.

    Long: LONG_MAX_ROUNDS. Easy and hard: `AgentConfig`'s default, which is what
    they ran under before `--rounds` was wired (`AgentConfig()` with no
    argument) — read from the class, not copied here, so it cannot drift.
    """
    if rounds is not None:
        if rounds < 1:
            raise SystemExit(f"--rounds must be at least 1, got {rounds}")
        return rounds
    if tier == "long":
        return LONG_MAX_ROUNDS
    from dpc_client_core.dpc_agent.agent import AgentConfig
    return AgentConfig().max_rounds


def require_context_window(entry: Dict[str, Any]) -> int:
    """Refuse an alias without `context_window`.

    Without it the manager falls back to 4096 for a model it does not know
    (`LLMManager.get_context_window`), and the long tier would run its
    compaction trigger and session limit on that number in silence.
    """
    raw = entry.get("context_window")
    try:
        window = int(raw)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        raise SystemExit(
            f"alias {entry.get('alias')!r} carries no usable context_window ({raw!r}); "
            "the manager would fall back to 4096 tokens — set it on the alias first"
        )
    return window


def _resolve_binary_or_none(entry: Dict[str, Any]) -> Optional[Path]:
    from dpc_client_core.managers.llama_server_fetcher import resolve_binary
    try:
        return resolve_binary(entry)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))


def arm_entries(entry: Dict[str, Any], arms) -> Dict[str, Dict[str, Any]]:
    """One copy of the provider entry per arm, `preserve_reasoning` set on the copy.

    The argument is itself a copy (`_provider_entry_for_alias` returns a new
    dict); nothing here can reach `~/.dpc/providers.json`.
    """
    return {arm: {**entry, "preserve_reasoning": arm == "on"} for arm in arms}


def harness_agent_configs(configs: Dict[str, Dict[str, Any]]):
    """Serve the loop's per-agent config from the harness, not from `~/.dpc/agents`.

    `run_llm_loop` reads compaction settings with `load_agent_config(agent_root.name)`
    (`dpc_agent/loop.py:1076`), which resolves `~/.dpc/agents/<name>/config.json`
    and **creates that directory** — the eval runs of August and September left
    `~/.dpc/agents/agent` and `task-001…` behind that way. A throwaway root cannot
    carry a config there without writing into the operator's home, so the loop's
    reference is replaced for this process: a root named in `configs` gets its
    dict, any other name gets `{}` (what a missing config.json gave before).
    Only the loop's binding is patched; `load_agent_config` itself is untouched.
    """
    from dpc_client_core.dpc_agent import loop as loop_module

    def from_harness(agent_id: str) -> Dict[str, Any]:
        return dict(configs.get(agent_id, {}))

    loop_module.load_agent_config = from_harness
    return from_harness


def long_compaction_config(alias: str, mode: str, threshold: float) -> Dict[str, Any]:
    """The config.json a long-tier root is given (see `harness_agent_configs`).

    `provider_alias` without `context_window`, as in Johnny's config, so the loop
    resolves the window itself through `get_context_window` — the production path
    (`loop.py:1074-1097`). `mode="off"` leaves compaction disabled, which is the
    throwaway default: deterministic truncation of tool history after round 8.
    """
    if mode == "off":
        return {"provider_alias": alias, "compaction_enabled": False}
    return {"provider_alias": alias, "compaction_enabled": True,
            "compaction_threshold": threshold, "compaction_provider": alias}


def build_fixture(root: Path) -> None:
    """The small world the tasks ask about. Fixed content, so answers are fixed."""
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "config.txt").write_text(
        "host=example.internal\nport=8443\nretries=7\nmode=strict\n", encoding="utf-8"
    )
    (docs / "notes.md").write_text(
        "# Release notes\n\n- shipped the drain watchdog\n- fixed the split history\n"
        "- the build number is 4172\n",
        encoding="utf-8",
    )
    (docs / "empty.log").write_text("", encoding="utf-8")


def tasks_for(root: Path) -> List[Dict[str, Any]]:
    """Each task: a prompt, and a check that needs no judgement."""
    docs = root / "docs"
    return [
        {
            "id": "read-a-value",
            "prompt": f"Read the file {docs / 'config.txt'} and tell me the value of `port`. "
                      f"Answer with the number only.",
            "expect_in_answer": ["8443"],
        },
        {
            "id": "read-a-second-value",
            "prompt": f"In {docs / 'config.txt'}, what is `retries` set to? Answer with the number.",
            "expect_in_answer": ["7"],
        },
        {
            "id": "find-a-fact-in-prose",
            "prompt": f"Read {docs / 'notes.md'} and tell me the build number.",
            "expect_in_answer": ["4172"],
        },
        {
            "id": "count-lines",
            "prompt": f"How many non-empty lines does {docs / 'config.txt'} have? Answer with a number.",
            "expect_in_answer": ["4"],
        },
        {
            "id": "list-a-directory",
            "prompt": f"List the file names in {docs}. Give the names only.",
            "expect_in_answer": ["config.txt", "notes.md", "empty.log"],
        },
        {
            "id": "an-empty-file-is-not-a-missing-one",
            "prompt": f"Is the file {docs / 'empty.log'} missing, or present and empty? Answer in one word: "
                      f"MISSING or EMPTY.",
            "expect_in_answer": ["EMPTY"],
            "reject_in_answer": ["MISSING"],
        },
        {
            "id": "write-a-file",
            "prompt": f"Create a file at {root / 'out' / 'result.txt'} containing exactly the word "
                      f"acknowledged, then say done.",
            "expect_file": {"path": str(root / "out" / "result.txt"), "contains": "acknowledged"},
        },
        {
            "id": "a-file-that-is-not-there",
            "prompt": f"Read {docs / 'does-not-exist.txt'} and tell me its first line. If it is not there, "
                      f"say NOT FOUND and nothing else.",
            "expect_in_answer": ["NOT FOUND"],
        },
        {
            "id": "arithmetic-without-tools",
            "prompt": "What is 17 * 23? Answer with the number only.",
            "expect_in_answer": ["391"],
        },
        {
            "id": "two-values-one-answer",
            "prompt": f"From {docs / 'config.txt'}, give `host` and `mode` separated by a comma.",
            "expect_in_answer": ["example.internal", "strict"],
        },
    ]


def _word_boundary_search(needle: str, haystack: str) -> int:
    """Position of `needle` in `haystack` as its own token, or -1.

    Plain substring containment let `14` satisfy a gold of `4` and `19`
    satisfy `9` (`THE-LOOP-HAS-NEVER-BEEN-SCORED-ON-A-TASK-IT-ACTUALLY-DOES`,
    backlog.md). A match only counts when it is not glued to another
    alphanumeric character on either side — `eval/kv/ab_key_quant.py:_hit`
    uses the same rule so `512` does not count inside `3512`. Both arguments
    are expected already lowercased.
    """
    m = re.search(rf"(?<![0-9a-z])" + re.escape(needle) + r"(?![0-9a-z])", haystack)
    return m.start() if m else -1


_THOUSANDS = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


def _field_value(key: str, lowered: str) -> Optional[str]:
    """The value written as `key=value` (or `key: value`), last occurrence wins.

    Last, because the long tier asks for the block at the end of the answer and
    the reasoning before it may mention the same key with a draft value. Quotes,
    backticks and bold markers around the value are dropped, then a trailing
    period; `100,000` is read as a number, `a, b` as `a`.
    """
    pat = (rf"(?<![0-9a-z_]){re.escape(key.lower())}[`*]*\s*[=:]\s*[`'\"*]*"
           r"([^\s;`'\"*]+)")
    found = re.findall(pat, lowered)
    if not found:
        return None
    value = found[-1].rstrip(".")
    if not _THOUSANDS.match(value):
        value = value.split(",")[0]
    return value.replace(",", "")


def _same_value(got: str, want: Any) -> bool:
    want_s = str(want).lower()
    try:
        return abs(float(got) - float(want_s)) < 1e-9
    except ValueError:
        return got == want_s


def check(task: Dict[str, Any], answer: str) -> Dict[str, Any]:
    """Deterministic scoring. No model decides anything here.

    `expect_in_answer` / `reject_in_answer` / `expect_ordered` match as whole
    tokens (`_word_boundary_search`). `expect_file` keeps exact substring
    semantics — a file-content check is about what the file holds, not a
    token parsed out of prose. `expect_fields` (the long tier) reads
    `key=value` lines and compares numbers as numbers, the rest as lowercase
    text; a key the answer never wrote is reported as missing, not as wrong.
    """
    reasons: List[str] = []
    ok = True
    lowered = (answer or "").lower()

    for key, want in (task.get("expect_fields") or {}).items():
        got = _field_value(key, lowered)
        if got is None:
            ok = False
            reasons.append(f"no {key}=")
        elif not _same_value(got, want):
            ok = False
            reasons.append(f"{key}={got} (want {str(want).lower()})")

    for needle in task.get("expect_in_answer", []):
        if _word_boundary_search(needle.lower(), lowered) < 0:
            ok = False
            reasons.append(f"missing {needle!r}")
    for needle in task.get("reject_in_answer", []):
        if _word_boundary_search(needle.lower(), lowered) >= 0:
            ok = False
            reasons.append(f"said {needle!r}")

    want_order = task.get("expect_ordered")
    if want_order:
        positions = [_word_boundary_search(name.lower(), lowered) for name in want_order]
        if any(pos < 0 for pos in positions):
            ok = False
            reasons.append("not all names given")
        elif positions != sorted(positions):
            ok = False
            reasons.append("wrong order")

    want_file = task.get("expect_file")
    if want_file:
        p = Path(want_file["path"])
        if not p.exists():
            ok = False
            reasons.append("file not created")
        else:
            body = p.read_text(encoding="utf-8", errors="replace")
            if want_file["contains"].lower() not in body.lower():
                ok = False
                reasons.append("file content wrong")
            # An edit is only correct if it left the rest of the file alone.
            for keep in want_file.get("still_contains", []):
                if keep.lower() not in body.lower():
                    ok = False
                    reasons.append(f"lost {keep!r}")
            for gone in want_file.get("must_not_contain", []):
                if gone.lower() in body.lower():
                    ok = False
                    reasons.append(f"still has {gone!r}")

    return {"passed": ok, "why": reasons}


async def run_one(agent, task: Dict[str, Any], *,
                  recorder: Optional[round_metrics.RoundRecorder] = None,
                  timeout_s: Optional[float] = None,
                  reasoning_effort: Optional[str] = None,
                  session_state: Optional[Dict[str, Any]] = None,
                  budget: Optional[int] = None,
                  queries: Optional[round_metrics.QueryCounter] = None,
                  process_kwargs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One task on one agent. `process_kwargs` carries a seeded history
    (`seed_history.seeded_process_kwargs`); a seeded row keeps no text the model
    wrote — see `seed_history.redact_seeded_outcome`."""
    seeded = bool(process_kwargs)
    started = time.time()
    error = None
    timed_out = False
    answer = ""
    if recorder is not None:
        recorder.rows = []
    with round_metrics.CompactionLog() as compactions:
        try:
            call = agent.process(
                message=task["prompt"],
                conversation_id=f"eval-{task['id']}",
                reasoning_effort=reasoning_effort,
                session_state=session_state,
                **(process_kwargs or {}),
            )
            answer = await (asyncio.wait_for(call, timeout_s) if timeout_s else call)
        except asyncio.TimeoutError:
            timed_out = True
            error = f"timeout after {timeout_s:.0f}s"
        except Exception as exc:  # a crash is a failure, recorded as one
            # A seeded run keeps the type only: a provider error can echo the prompt.
            error = type(exc).__name__ if seeded else f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - started
    verdict = check(task, answer or "")
    passed = verdict["passed"] and error is None
    out = {
        "id": task["id"],
        "passed": passed,
        # Cut off by the harness, not failed by the agent: kept apart so the
        # verdict never reads a timeout as the flag's failure.
        "timed_out": timed_out,
        "why": verdict["why"] + ([error] if error else []),
        "seconds": round(elapsed, 1),
        "answer": (answer or "")[:400],
    }
    if task.get("expect_fields"):
        # The checked block is at the end; a head alone would not show it.
        out["answer_tail"] = (answer or "")[-800:]
    if recorder is not None:
        m = round_metrics.summarise_rounds(recorder.rows, budget)
        m["compaction_round"] = compactions.rounds[0] if compactions.rounds else None
        m["compactions"] = len(compactions.rounds)
        m["compaction_rounds"] = list(compactions.rounds)
        m["compaction_failures"] = compactions.failures
        if queries is not None:
            m.update(queries.take())
        # Absent when it failed: a failed task has no rounds-to-completion.
        m["rounds_to_completion"] = m["rounds"] if passed else None
        m["symptom"] = round_metrics.task_symptom(m)
        out["metrics"] = m
        out["per_round"] = recorder.rows
    if seeded:
        import seed_history
        seed_history.redact_seeded_outcome(out, task, _field_value, answer)
    return out


def _operator_providers_digest() -> Optional[str]:
    src = Path.home() / ".dpc" / "providers.json"
    try:
        return hashlib.sha256(src.read_bytes()).hexdigest()
    except OSError:
        return None


def _allowed_tools(tier: str):
    if tier == "long":
        from tasks_long import LONG_TIER_TOOLS
        assert LONG_TIER_TOOLS <= benchmark_tools.BENCHMARK_TOOLS, "long tools must be listed"
        return LONG_TIER_TOOLS
    return benchmark_tools.BENCHMARK_TOOLS


def _dry_run(entry: Dict[str, Any], args, entries: Dict[str, Dict[str, Any]],
             effort: Optional[str]) -> int:
    print(f"  alias: {entry.get('alias')!r} (type={entry.get('type')})")
    # Every copied arm entry is checked, though they share the field with `entry`.
    for e in entries.values():
        require_context_window(e)
    if entry.get("type") == "llamacpp_server":
        binary = _resolve_binary_or_none(entry)
        if binary is None:
            raise SystemExit(
                "no pinned llama-server binary installed — run the DPC "
                "service once so it fetches the pin, or set binary_path "
                "on the alias"
            )
        print(f"  binary: {binary} (exists={binary.is_file()})")
        gguf = Path(entry.get("gguf_path") or "")
        print(f"  gguf: {gguf} (exists={gguf.is_file()})")
        if not gguf.is_file():
            raise SystemExit(f"gguf_path does not exist: {gguf}")
    print(f"  operator alias preserve_reasoning: "
          f"{entry.get('preserve_reasoning', '(absent -> provider default false)')}")
    for arm, e in entries.items():
        print(f"  arm {arm}: copy carries preserve_reasoning={e['preserve_reasoning']}")
    if entry.get("type") != "llamacpp_server" and "on" in entries:
        print("  NOTE: only llamacpp_server reads preserve_reasoning; on this provider "
              "the two arms are the same run")
    print(f"  reasoning effort sent: {effort or '(none — the alias decides)'}; "
          f"note budget: {entry.get('reasoning_budget_tokens')}; "
          f"alias context_window: {entry.get('context_window')}")
    print(f"  max rounds: {resolve_max_rounds(args.tier, args.rounds)}"
          f"{'' if args.rounds is not None else ' (tier default)'}")

    allowed = _allowed_tools(args.tier)
    with tempfile.TemporaryDirectory(prefix="dpc-loop-dry-") as tmp:
        tmp_path = Path(tmp)
        firewall = benchmark_tools.benchmark_firewall(tmp_path, LOOP_PROFILE, allowed=allowed)
        tools = firewall.get_agent_tools_map(LOOP_PROFILE) or {}
        on = sorted(k for k, v in tools.items() if v)
        missing = sorted(set(allowed) - set(tools))
        print(f"  firewall: profile {LOOP_PROFILE!r}, {len(on)} of {len(tools)} registered "
              f"tools on: {', '.join(on)}")
        if missing:
            raise SystemExit(f"allowed tools not registered: {missing}")
        if set(on) != set(allowed):
            raise SystemExit(f"firewall map disagrees with the allow list: on={on}")

        if args.tier == "long":
            import tasks_long
            commit = tasks_long.resolve_commit(REPO_ROOT, args.snapshot_commit or "HEAD")
            info = tasks_long.snapshot_source(REPO_ROOT, commit, tmp_path / "snapshot")
            print(f"  snapshot: {commit} ({info['files']} files, {info['bytes']} bytes) "
                  f"gold commit {tasks_long.GOLD_COMMIT[:12]}")
            rows = tasks_long.verify_golds(tmp_path / "snapshot")
            bad = [r for r in rows if not r["ok"]]
            print(f"  golds: {len(rows) - len(bad)}/{len(rows)} agree with the snapshot")
            for r in bad:
                print(f"    BAD {r['task']} {r['key']}: gold={r['gold']!r} "
                      f"derived={r['derived']!r} {r.get('error', '')}")
            root = tmp_path / "long-00-off"
            tasks_long.build_fixture(root, tmp_path / "snapshot")
            todo = tasks_long.tasks_for(root)
            for t in todo:
                need = set(t["tools_needed"])
                named = [root / "src" / tasks_long.PKG / f for f in t["files"]]
                print(f"    {t['id']:28} files={len(named)} "
                      f"chars={sum(p.stat().st_size for p in named)} "
                      f"in_root={all(p.is_file() for p in named)} "
                      f"tools_needed_listed={need <= set(allowed)}")
                if not need <= set(allowed) or not all(p.is_file() for p in named):
                    raise SystemExit(f"task {t['id']} cannot run under this tool set/snapshot")
            if bad:
                raise SystemExit("golds disagree with the snapshot — re-derive them, or pass "
                                 f"--snapshot-commit {tasks_long.GOLD_COMMIT[:12]}")
            cfg = long_compaction_config(entry["alias"], args.compaction,
                                         LONG_COMPACTION_THRESHOLD)
            print(f"  compaction config served to each root: {cfg}")
            print(f"  task timeout {args.task_timeout_minutes} min, arms run per task in "
                  f"{' then '.join(entries)} / reversed order alternately")

    out_dir = RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    probe = out_dir / ".dry-run-write-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    print(f"  output dir writable: {out_dir}")
    return 0


# The incident's pair lives with the probe (`seed_history`); printed beside every
# seeded estimate so a chars/4 figure is not read as engine tokens.
from seed_history import INCIDENT_ROUND1_COUNTED, INCIDENT_ROUND1_ESTIMATED  # noqa: E402


def seed_tokenizer(entry: Dict[str, Any]):
    """The alias's own tokenizer for the depth probe, or None (then calibrated).

    `llama-tokenize` from the pinned build's directory over the alias's GGUF,
    vocabulary only; a missing binary or file is not an error here.
    """
    import seed_history
    if entry.get("type") != "llamacpp_server" or not Path(entry.get("gguf_path") or "").is_file():
        return None
    try:
        binary = _resolve_binary_or_none(entry)
    except SystemExit:
        return None
    return seed_history.tokenizer_beside(binary, Path(entry.get("gguf_path") or ""))


def load_seed_or_refuse(args) -> Optional[Dict[str, Any]]:
    """The seed for this run, or None. Refused before anything is loaded."""
    if not args.seed_history:
        return None
    import seed_history
    seed = seed_history.load_seed(Path(args.seed_history), args.seed_reader)
    differ = seed_history.roles_agree_for_group_and_direct(seed)
    if differ:
        raise SystemExit(
            f"--seed-history: records {differ} would render with another role under the "
            "eval's 1:1 conversation id than in their group; the prompt would not be "
            "the one production built")
    return seed


async def seed_depth_or_refuse(seed: Dict[str, Any], args, entry: Dict[str, Any],
                               effort: Optional[str], workdir: Path, firewall,
                               agent_configs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Round 1 of every task this run will start, measured before any model.

    Each task's request is built by a real `DpcAgent` on a probe root
    (`seed_history.probe_round1`) and sized in engine tokens — counted by the
    alias's tokenizer, or calibrated from the incident where that is missing.
    A task whose engine-scale figure is below `--seed-depth-floor` (itself in
    engine tokens) stops the run. Returns what the provenance records — numbers,
    no text.
    """
    import seed_history
    import tasks_long
    window = require_context_window(entry)
    floor = args.seed_depth_floor
    rec = seed_history.seed_record(seed)
    tokenizer = seed_tokenizer(entry)
    print(f"  seed: {rec['path']}")
    print(f"    sha256 {rec['sha256']}, {rec['messages']} records "
          f"({rec['history_records']} history + the trigger, whose body the task replaces), "
          f"reader {rec['reader']!r}")
    deep = rec.get("deepening")
    if deep:
        print(f"    DEEP SEED: {deep.get('outside_incident_history')} records from outside the "
              f"incident's loaded history (production would not have loaded them), from "
              + "; ".join(f"{s['file']} #{s['msg_index_from']}-#{s['msg_index_to']} "
                          f"({s['records']})" for s in deep.get("sources") or [])
              + f"; base {deep.get('base_seed', {}).get('sha256', '?')[:16]}")
    if tokenizer is not None:
        print(f"    engine scale: COUNTED with the model's tokenizer ({tokenizer.describe()}, "
              "no weights, CUDA hidden) over the request in ChatML framing plus tool schemas")
    else:
        print(f"    engine scale: CALIBRATED, not measured — no llama-tokenize/GGUF for this "
              f"alias; the chars/4 request estimate x {INCIDENT_ROUND1_COUNTED}/"
              f"{INCIDENT_ROUND1_ESTIMATED} = x{seed_history.CALIBRATION:.3f}")
    print(f"    the incident's round 1: {INCIDENT_ROUND1_ESTIMATED} on chars/4 (tool schemas "
          f"excluded), {INCIDENT_ROUND1_COUNTED} counted by the engine (dpc-client.log.1, "
          "2026-10-05 17:54:53 / 17:56:41)")
    probe_root = workdir / "depth-probe"
    n = len(tasks_long.tasks_for(probe_root))
    rows: List[Dict[str, Any]] = []
    for i, task in enumerate(tasks_long.tasks_for(probe_root)[: min(n, args.tasks or n)]):
        root = workdir / f"depth-{i + 1:02d}"
        agent_configs[root.name] = long_compaction_config(entry["alias"], args.compaction,
                                                          LONG_COMPACTION_THRESHOLD)
        m = await seed_history.probe_round1(
            root, firewall=firewall, profile=LOOP_PROFILE, alias=entry["alias"],
            task_prompt=task["prompt"], seed=seed, conversation_id=f"eval-{task['id']}",
            session_state={"tokens_limit": window}, reasoning_effort=effort,
            tokenizer=tokenizer)
        m.pop("messages")
        m["id"] = task["id"]
        m["below_floor"] = m["engine_tokens"] < floor
        rows.append(m)
        cross = (f", calibrated would say {round(m['prompt_tokens_est'] * seed_history.CALIBRATION)}"
                 if m["engine_method"] == seed_history.METHOD_TOKENIZER else "")
        print(f"    {task['id']:28} history turns {m['history_turns']} "
              f"({m['assistant_turns']} assistant), round 1 ENGINE {m['engine_tokens']} "
              f"({m['engine_method']}{cross}); chars/4 {m['round1_tokens_est']} "
              f"= request {m['prompt_tokens_est']} + tool schemas {m['tool_schema_tokens_est']} "
              f"[floor {floor} engine tokens] {'BELOW' if m['below_floor'] else 'ok'}")
    out = {**rec, "depth_floor": floor, "depth_floor_unit": "engine prompt tokens",
           "depth_method": rows[0]["engine_method"] if rows else None,
           "depth_counter": rows[0]["engine_counter"] if rows else None,
           "depth_by_task": rows}
    low = [r["id"] for r in rows if r["below_floor"]]
    if low:
        worst = min(r["engine_tokens"] for r in rows)
        raise SystemExit(
            f"SEEDED ROUND 1 BELOW THE STEP-0 FLOOR: {len(low)} of {len(rows)} task(s) at "
            f"under {floor} engine tokens (lowest {worst}, {rows[0]['engine_method']}): "
            f"{', '.join(low)}. The run would start shallow and step 0 would again measure "
            "nothing. Seed deeper, or pass --seed-depth-floor N to start knowingly "
            "(recorded in the report)")
    return out


async def main_async(args) -> int:
    arms = ARMS[args.preserve_reasoning]
    long_tier = args.tier == "long"
    effort = args.reasoning_effort if args.reasoning_effort is not None else (
        LONG_EFFORT if long_tier else None)

    digest_before = _operator_providers_digest()
    if args.providers:
        providers_doc = json.loads(Path(args.providers).read_text(encoding="utf-8"))
        entry = dict(providers_doc["providers"][0])
    else:
        entry = _provider_entry_for_alias(args.provider_alias)
    if args.model:
        entry["model"] = args.model
    entries = arm_entries(entry, arms)
    seed = load_seed_or_refuse(args)

    if args.dry_run:
        rc = _dry_run(entry, args, entries, effort)
        refusal: Optional[SystemExit] = None
        if seed is not None:
            with tempfile.TemporaryDirectory(prefix="dpc-loop-seed-dry-") as tmp:
                configs: Dict[str, Dict[str, Any]] = {}
                harness_agent_configs(configs)
                fw = benchmark_tools.benchmark_firewall(Path(tmp), LOOP_PROFILE,
                                                        allowed=_allowed_tools("long"))
                try:
                    await seed_depth_or_refuse(seed, args, entry, effort, Path(tmp), fw,
                                               configs)
                except SystemExit as exc:
                    refusal = exc
        if refusal is None:
            print("dry run checks passed — nothing was loaded; VRAM last:")
        else:
            print("dry run REFUSED (the reason is the last line); VRAM, for the record:")
        # Last, so the checks above are reported even while the service holds the
        # card; it still refuses (exit 1) when the card is not free.
        _check_vram_or_refuse(entry)
        if refusal is not None:
            raise refusal
        return rc

    if long_tier:
        # The harness reads the window for the long tier (session limit and
        # compaction trigger); easy and hard never ask for it.
        require_context_window(entry)
    max_rounds = resolve_max_rounds(args.tier, args.rounds)
    free_vram = _check_vram_or_refuse(entry)

    from dpc_client_core.llm_manager import LLMManager
    from dpc_client_core.dpc_agent.agent import DpcAgent, AgentConfig

    workdir = Path(tempfile.mkdtemp(prefix="dpc-loop-eval-"))
    alias = entry["alias"]
    allowed = _allowed_tools(args.tier)
    firewall = benchmark_tools.benchmark_firewall(workdir, LOOP_PROFILE, allowed=allowed)
    agent_configs: Dict[str, Dict[str, Any]] = {}
    harness_agent_configs(agent_configs)

    snapshot_info: Optional[Dict[str, Any]] = None
    compaction_cfg: Optional[Dict[str, Any]] = None
    if long_tier:
        import tasks_long
        commit = tasks_long.resolve_commit(REPO_ROOT, args.snapshot_commit or "HEAD")
        snapshot_info = tasks_long.snapshot_source(REPO_ROOT, commit, workdir / "snapshot")
        gold_rows = tasks_long.verify_golds(workdir / "snapshot")
        bad = [r for r in gold_rows if not r["ok"]]
        snapshot_info["gold_commit"] = tasks_long.GOLD_COMMIT
        snapshot_info["gold_check"] = f"{len(gold_rows) - len(bad)}/{len(gold_rows)}"
        if bad:
            print("golds disagree with the snapshot: "
                  + "; ".join(f"{r['task']}.{r['key']}" for r in bad))
            shutil.rmtree(workdir, ignore_errors=True)
            return 2
        compaction_cfg = long_compaction_config(alias, args.compaction, LONG_COMPACTION_THRESHOLD)

    seed_info: Optional[Dict[str, Any]] = None
    if seed is not None:
        # Before the model: a seed too shallow for step 0 stops here.
        try:
            seed_info = await seed_depth_or_refuse(seed, args, entry, effort, workdir,
                                                   firewall, agent_configs)
        except SystemExit:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

    live_providers = workdir / "providers.json"
    live_providers.write_text(
        json.dumps({"providers": [entries[arms[0]]], "default_provider": alias}),
        encoding="utf-8",
    )
    llm = LLMManager(config_path=live_providers)
    provider = llm.providers.get(alias)
    queries = round_metrics.QueryCounter()
    queries.wrap(llm)
    budget = entry.get("reasoning_budget_tokens")
    session_state = None
    timeout_s = None
    if long_tier:
        window = llm.get_context_window(provider.model) if provider is not None else None
        session_state = {"tokens_limit": window} if window else None
        timeout_s = args.task_timeout_minutes * 60

    approver = None
    if args.auto_approve:
        from _harness.auto_approve import Tier1AutoApprover
        approver = Tier1AutoApprover().start()
        print("Tier 1 auto-approval ON (Tier 2 still blocked)")

    def new_agent(root: Path):
        agent = DpcAgent(
            llm_manager=llm,
            config=AgentConfig(max_rounds=max_rounds),
            agent_root=root,
            firewall=firewall,
            firewall_profile=LOOP_PROFILE,
            provider_alias=alias if long_tier else None,
        )
        recorder = round_metrics.RoundRecorder()
        recorder.wrap(agent.llm)
        return agent, recorder

    def set_arm(arm: str) -> None:
        if provider is not None:
            provider.preserve_reasoning = entries[arm]["preserve_reasoning"]

    import seed_history

    def kwargs_for(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return seed_history.seeded_process_kwargs(seed, task["prompt"]) if seed else None

    # (arm, task, agent, recorder) in run order.
    plan: List[tuple] = []
    if long_tier:
        n = len(tasks_long.tasks_for(workdir / "probe"))
        for i in range(min(n, args.tasks or n)):
            # ABBA: alternate which arm goes first, so neither always meets a
            # child that just served the other.
            for arm in (arms if i % 2 == 0 else tuple(reversed(arms))):
                root = workdir / f"long-{i + 1:02d}-{arm}"
                tasks_long.build_fixture(root, workdir / "snapshot")
                agent_configs[root.name] = compaction_cfg
                agent, recorder = new_agent(root)
                plan.append((arm, tasks_long.tasks_for(root)[i], agent, recorder))
    else:
        if args.tier == "hard":
            from tasks_hard import build_fixture as build, tasks_for as make_tasks
        else:
            build, make_tasks = build_fixture, tasks_for
        for arm in arms:
            # The world lives *inside* the agent root on purpose. Put it outside and
            # ADR-030 Tier 1 stops every read for being off-sandbox, and the harness
            # measures the approval gate instead of the loop. Measured: 0/2 with the
            # fixture in the temp dir. One root per arm: the hard tier edits a file.
            agent_root = workdir / ("agent" if len(arms) == 1 else f"agent-{arm}")
            agent_root.mkdir(parents=True, exist_ok=True)
            fixture = agent_root / "world"
            build(fixture)
            agent, recorder = new_agent(agent_root)
            todo = make_tasks(fixture)
            if args.tasks:
                todo = todo[: args.tasks]
            plan.extend((arm, task, agent, recorder) for task in todo)

    results = []
    started = time.time()
    try:
        for arm, task, agent, recorder in plan:
            set_arm(arm)
            outcome = await run_one(agent, task, recorder=recorder,
                                    timeout_s=timeout_s, reasoning_effort=effort,
                                    session_state=session_state, budget=budget,
                                    queries=queries, process_kwargs=kwargs_for(task))
            outcome["arm"] = arm
            outcome["preserve_reasoning"] = entries[arm]["preserve_reasoning"]
            results.append(outcome)
            m = outcome.get("metrics") or {}
            mark = "pass" if outcome["passed"] else ("TIME" if outcome["timed_out"] else "FAIL")
            print(f"  {mark:4} {outcome['id']:34} {arm:>3} {outcome['seconds']:7.1f}s "
                  f"r={m.get('rounds')} hits={m.get('budget_hits')} "
                  f"silent={m.get('silent_rounds')} peak={m.get('peak_prompt_tokens')} "
                  f"{'; '.join(outcome['why'])[:60]}", flush=True)
    finally:
        if approver is not None:
            approver.stop()
        # LLMManager.shutdown() sweeps stop_all_supervisors() unconditionally
        # (llm_manager.py:1155-1156), so the child this run started is stopped
        # even on a task exception or Ctrl-C.
        try:
            await llm.shutdown()
        except Exception as exc:
            print(f"  (provider shutdown raised: {type(exc).__name__}: {exc})")

    digest_after = _operator_providers_digest()
    dataset = ({"kind": "dpc-messenger source snapshot (git archive)", "tier": "long",
                **(snapshot_info or {})}
               if long_tier else {"kind": "fixed fixture", "tier": args.tier})
    run_conditions = {
        "step0_only": bool(args.step0_only),
        "arms": list(arms),
        "preserve_reasoning_by_arm": {a: e["preserve_reasoning"] for a, e in entries.items()},
        "reasoning_effort_sent": effort,
        "note_budget": budget,
        "compaction": compaction_cfg,
        "max_rounds": max_rounds,
        "task_timeout_s": timeout_s,
        "session_tokens_limit": (session_state or {}).get("tokens_limit"),
        "firewall": {"profile": LOOP_PROFILE, "tools": sorted(allowed)},
        "operator_providers_unchanged": digest_before == digest_after,
        # Path, sha256, record count and the depth estimate — never the records.
        "seed_history": seed_info,
    }
    approvals = approver.summary() if approver is not None else None
    withheld = [0]
    if seed is not None:
        # The net under the per-row redaction in run_one: any string the model
        # typed that still carries 40+ characters of the chat verbatim.
        windows = seed_history.seed_windows(seed)
        results = seed_history.withhold_seed_text(results, windows, counter=withheld)
        approvals = seed_history.withhold_seed_text(approvals, windows, counter=withheld)
        run_conditions["seed_history"]["withheld_strings"] = withheld[0]
    prov_block = provenance.snapshot(
        repo_root=REPO_ROOT,
        provider_entry=entry,
        dataset=dataset,
        harness_file=Path(__file__),
        argv=sys.argv,
        extra={"run_conditions": run_conditions,
               **({"approvals": approvals} if approvals is not None else {})},
    )

    passed = sum(1 for r in results if r["passed"])
    report = {
        "tier": args.tier,
        "model": entry.get("model"),
        "provider_alias": entry.get("alias"),
        "provider_type": entry.get("type"),
        "gguf_path": entry.get("gguf_path"),
        "llama_cpp_tag": None,
        "reasoning_effort": entry.get("reasoning_effort"),
        "temperature": entry.get("temperature"),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": prov_block.get("code", {}).get("repo", {}).get("sha"),
        "free_vram_mib_before": free_vram,
        **run_conditions,
        "tasks": len(results),
        "passed": passed,
        "accuracy": round(passed / len(results), 3) if results else 0.0,
        "seconds": round(time.time() - started, 1),
        **({"approvals": approvals} if approvals is not None else {}),
        "results": results,
    }
    if entry.get("type") == "llamacpp_server":
        from dpc_client_core.managers.llama_server_fetcher import LLAMA_CPP_TAG
        report["llama_cpp_tag"] = LLAMA_CPP_TAG

    print()
    for arm in arms:
        rows = [r for r in results if r["arm"] == arm]
        ok = sum(1 for r in rows if r["passed"])
        cut = sum(1 for r in rows if r.get("timed_out"))
        print(f"arm {arm}: {ok}/{len(rows)} on {report['model']}"
              + (f" ({cut} timed out — cut off by the harness, not failed)" if cut else ""))
    # Step 0 belongs to the long tier: easy and hard never reach the incident's depth.
    s0 = round_metrics.step0(results) if long_tier else {"reproduced": None, "why": "not a long-tier run"}
    report["step0"] = s0
    if long_tier:
        print(round_metrics.step0_line(s0))
        if s0["reproduced"] is False:
            print(f"  {s0['why']}")
    if args.step0_only:
        if s0["reproduced"]:
            print("step-0 preflight: the off arm reached the incident's regime — the full "
                  "A/B (--preserve-reasoning both) can measure the flag here")
        else:
            print("step-0 preflight: the off arm did not reach the incident's regime — the "
                  "full A/B would measure nothing about the flag on this setup")
    if len(arms) == 2:
        lines = round_metrics.verdict_lines(results)
        report["verdict"] = lines
        print("both sides of each trade (no arm is ranked here):")
        for line in lines:
            print(line)
    print(f"{passed}/{len(results)} = {report['accuracy']:.1%} in {report['seconds']}s")

    out = Path(args.json) if args.json else (
        RESULTS_DIR / f"{args.tier}-{entry.get('alias', 'unknown').replace(' ', '_')}"
        f"-{'step0' if args.step0_only else args.preserve_reasoning}"
        f"{'-seeded' if seed is not None else ''}"
        f"-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    provenance.write_beside(out, prov_block)
    print(f"full report -> {out}")

    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"workdir kept at {workdir}")
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tier", choices=("easy", "hard", "long"), default=None,
                    help="easy (default) is the regression floor; hard asks for more than one "
                         "hop; long is the deep code-reading set built for the "
                         "preserve_reasoning A/B")
    ap.add_argument("--tasks", type=int, default=None, help="run only the first N tasks")
    ap.add_argument("--provider-alias", default=DEFAULT_ALIAS,
                    help=f"alias to copy verbatim from ~/.dpc/providers.json (default: {DEFAULT_ALIAS!r})")
    ap.add_argument("--providers", default=None,
                    help="explicit opt-in: an operator-supplied providers file "
                         "(e.g. eval/loop/providers.eval.json for Ollama) instead of "
                         "--provider-alias")
    ap.add_argument("--model", default=None, help="override the model field on the chosen provider entry")
    # `--max-rounds` was the long tier's spelling; it stays as an alias so a
    # command written on 2026-10-06 still means what it said.
    ap.add_argument("--rounds", "--max-rounds", type=int, default=None, dest="rounds",
                    help=f"the agent's round limit (AgentConfig.max_rounds) on every tier; "
                         f"default: {LONG_MAX_ROUNDS} for long, AgentConfig's own default for "
                         f"easy and hard")
    ap.add_argument("--preserve-reasoning", choices=tuple(ARMS), default="off",
                    dest="preserve_reasoning",
                    help="set preserve_reasoning on the copied provider entry: off (default, "
                         "the behaviour before the flag), on, or both (every task under each arm)")
    ap.add_argument("--step0-only", action="store_true", dest="step0_only",
                    help=f"the preflight before the A/B: the long tier's off arm on the first "
                         f"{STEP0_ONLY_TASKS} tasks (or --tasks N), then the step-0 verdict")
    ap.add_argument("--reasoning-effort", default=None,
                    help=f"effort word sent per call (default: none for easy/hard, "
                         f"{LONG_EFFORT!r} for long)")
    ap.add_argument("--compaction", choices=("production", "off"), default="production",
                    help="long tier: production = enabled at threshold "
                         f"{LONG_COMPACTION_THRESHOLD}, local alias as summariser; off = disabled "
                         "(deterministic truncation after round 8)")
    ap.add_argument("--snapshot-commit", default=None,
                    help="long tier: commit to snapshot (default HEAD at start)")
    ap.add_argument("--task-timeout-minutes", type=float, default=LONG_TASK_TIMEOUT_MIN,
                    help="long tier: a task past this is recorded as a timeout")
    ap.add_argument("--auto-approve", action="store_true",
                    help="answer ADR-030 Tier 1 prompts with the eval approver (inline code "
                         "inside the task only); Tier 2 stays hard-blocked")
    ap.add_argument("--seed-history", default=None, dest="seed_history", metavar="PATH",
                    help="long tier only: a seed file of conversation records rendered in front "
                         "of every task the way production renders a loaded history; its last "
                         "record's body is replaced by the task. Kept outside the repository; "
                         "reports carry its path, sha256 and count, never its text")
    ap.add_argument("--seed-reader", default="Johnny", dest="seed_reader", metavar="NAME",
                    help="whose history the seed is: that reader's own records become "
                         "assistant turns (default: Johnny); its identity must be in the file")
    ap.add_argument("--seed-depth-floor", type=int, default=round_metrics.SYMPTOM_MIN_PEAK_PROMPT,
                    dest="seed_depth_floor", metavar="N",
                    help="with --seed-history: refuse to start when a task's round-1 prompt is "
                         f"below N engine tokens (default {round_metrics.SYMPTOM_MIN_PEAK_PROMPT}, "
                         "the step-0 floor)")
    ap.add_argument("--json", default=None, help="write the report here instead of results_root()/loop/")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway workdir")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                     help="validate alias/binary/gguf/arms/tools/snapshot/golds/output-dir, "
                          "then VRAM, and exit; loads no model")
    args = ap.parse_args(argv)
    if args.step0_only:
        if args.tier not in (None, "long"):
            ap.error("--step0-only runs the long tier; drop --tier " + args.tier)
        if args.preserve_reasoning != "off":
            ap.error("--step0-only runs the off arm only; drop --preserve-reasoning "
                     + args.preserve_reasoning)
        args.tier = "long"
        args.tasks = args.tasks or STEP0_ONLY_TASKS
    args.tier = args.tier or "easy"
    if args.seed_history and args.tier != "long":
        ap.error(f"--seed-history seeds the long tier only; the {args.tier} tier's tasks "
                 "are scored against a fixture a chat history has nothing to do with")
    return args


def main() -> int:
    # Model answers carry arrows and non-Latin text; a cp1252 console would die
    # on printing a finished run.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
