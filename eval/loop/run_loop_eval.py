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

`--tasks N` for a smoke pass, `--tier hard` for the harder set, `--dry-run`
to validate the alias/binary/gguf/VRAM/output-dir without loading a model.
"""

from __future__ import annotations

import argparse
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

from _harness import provenance  # noqa: E402
from _harness.results_root import results_root  # noqa: E402

OLLAMA_PROVIDERS = HERE / "providers.eval.json"
DEFAULT_ROUNDS = 8
DEFAULT_ALIAS = "qwen3.8 27b"
RESULTS_DIR = results_root("loop")

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


def _resolve_binary_or_none(entry: Dict[str, Any]) -> Optional[Path]:
    from dpc_client_core.managers.llama_server_fetcher import resolve_binary
    try:
        return resolve_binary(entry)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))


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


def check(task: Dict[str, Any], answer: str) -> Dict[str, Any]:
    """Deterministic scoring. No model decides anything here.

    `expect_in_answer` / `reject_in_answer` / `expect_ordered` match as whole
    tokens (`_word_boundary_search`). `expect_file` keeps exact substring
    semantics — a file-content check is about what the file holds, not a
    token parsed out of prose.
    """
    reasons: List[str] = []
    ok = True
    lowered = (answer or "").lower()

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


async def run_one(agent, task: Dict[str, Any], rounds: int) -> Dict[str, Any]:
    started = time.time()
    error = None
    answer = ""
    try:
        answer = await agent.process(
            message=task["prompt"],
            conversation_id=f"eval-{task['id']}",
        )
    except Exception as exc:  # a crash is a failure, recorded as one
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - started
    verdict = check(task, answer or "")
    return {
        "id": task["id"],
        "passed": verdict["passed"] and error is None,
        "why": verdict["why"] + ([error] if error else []),
        "seconds": round(elapsed, 1),
        "answer": (answer or "")[:400],
    }


def _dry_run(entry: Dict[str, Any]) -> int:
    print(f"  alias: {entry.get('alias')!r} (type={entry.get('type')})")
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
    out_dir = RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    probe = out_dir / ".dry-run-write-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    print(f"  output dir writable: {out_dir}")
    print("dry run OK — nothing was loaded")
    return 0


async def main_async(args) -> int:
    from dpc_client_core.llm_manager import LLMManager
    from dpc_client_core.dpc_agent.agent import DpcAgent, AgentConfig

    if args.providers:
        providers_doc = json.loads(Path(args.providers).read_text(encoding="utf-8"))
        entry = dict(providers_doc["providers"][0])
    else:
        entry = _provider_entry_for_alias(args.provider_alias)
    if args.model:
        entry["model"] = args.model

    free_vram = _check_vram_or_refuse(entry)

    if args.dry_run:
        return _dry_run(entry)

    workdir = Path(tempfile.mkdtemp(prefix="dpc-loop-eval-"))
    agent_root = workdir / "agent"
    agent_root.mkdir(parents=True, exist_ok=True)
    # The world lives *inside* the agent root on purpose. Put it outside and
    # ADR-030 Tier 1 stops every read for being off-sandbox, no approver is
    # attached in a headless run, and the harness measures the approval gate
    # instead of the loop. Measured: 0/2 with the fixture in the temp dir.
    fixture = agent_root / "world"
    if args.tier == "hard":
        from tasks_hard import build_fixture as build, tasks_for as make_tasks
    else:
        build, make_tasks = build_fixture, tasks_for
    build(fixture)

    live_providers = workdir / "providers.json"
    live_providers.write_text(
        json.dumps({"providers": [entry], "default_provider": entry["alias"]}),
        encoding="utf-8",
    )

    llm = LLMManager(config_path=live_providers)
    agent = DpcAgent(
        llm_manager=llm,
        config=AgentConfig(),
        agent_root=agent_root,
    )

    todo = make_tasks(fixture)
    if args.tasks:
        todo = todo[: args.tasks]

    results = []
    started = time.time()
    try:
        for task in todo:
            outcome = await run_one(agent, task, args.rounds)
            results.append(outcome)
            mark = "pass" if outcome["passed"] else "FAIL"
            print(f"  {mark:4} {outcome['id']:34} {outcome['seconds']:6.1f}s "
                  f"{'; '.join(outcome['why'])[:60]}")
    finally:
        # LLMManager.shutdown() sweeps stop_all_supervisors() unconditionally
        # (llm_manager.py:1050-1051), so the child this run started is stopped
        # even on a task exception or Ctrl-C.
        try:
            await llm.shutdown()
        except Exception as exc:
            print(f"  (provider shutdown raised: {type(exc).__name__}: {exc})")

    prov_block = provenance.snapshot(
        repo_root=REPO_ROOT,
        provider_entry=entry,
        dataset={"kind": "fixed fixture", "tier": args.tier},
        harness_file=Path(__file__),
        argv=sys.argv,
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
        "tasks": len(results),
        "passed": passed,
        "accuracy": round(passed / len(results), 3) if results else 0.0,
        "seconds": round(time.time() - started, 1),
        "results": results,
    }
    if entry.get("type") == "llamacpp_server":
        from dpc_client_core.managers.llama_server_fetcher import LLAMA_CPP_TAG
        report["llama_cpp_tag"] = LLAMA_CPP_TAG

    print()
    print(f"{passed}/{len(results)} = {report['accuracy']:.1%} on {report['model']} "
          f"in {report['seconds']}s")

    out = Path(args.json) if args.json else (
        RESULTS_DIR / f"{args.tier}-{entry.get('alias', 'unknown').replace(' ', '_')}"
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tier", choices=("easy", "hard"), default="easy",
                    help="easy is the regression floor; hard is the set that asks for more than one hop")
    ap.add_argument("--tasks", type=int, default=None, help="run only the first N tasks")
    ap.add_argument("--provider-alias", default=DEFAULT_ALIAS,
                    help=f"alias to copy verbatim from ~/.dpc/providers.json (default: {DEFAULT_ALIAS!r})")
    ap.add_argument("--providers", default=None,
                    help="explicit opt-in: an operator-supplied providers file "
                         "(e.g. eval/loop/providers.eval.json for Ollama) instead of "
                         "--provider-alias")
    ap.add_argument("--model", default=None, help="override the model field on the chosen provider entry")
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    ap.add_argument("--json", default=None, help="write the report here instead of results_root()/loop/")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway workdir")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                     help="validate alias/binary/gguf/VRAM/output-dir and exit; loads no model")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
