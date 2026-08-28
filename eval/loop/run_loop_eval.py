"""Does the agent loop finish the kind of task we actually give it?

The second instrument. Until this ran, every statement about the loop in this
repository — including the ones in the backlog — was an anecdote: nobody had
ever scored a run.

Deliberately **not** GAIA. The task set is ours: read a named file and answer
from it, count something, list a directory, write a file that must then exist.
Each check is deterministic — a string that must appear, or an artefact that
must be on disk. No LLM judges anything here; a scorer that itself needs
verifying is the expensive tier bought before the cheap one.

Runs against a local model through its own provider file, so nothing is spent
and the operator's `~/.dpc/providers.json` is never touched. The agent gets a
throwaway root under the results directory: no real agent's memory is read or
written.

Run from `dpc-client/core` with Ollama up:

    uv run python ../../eval/loop/run_loop_eval.py

`--tasks N` for a smoke pass, `--model` to change the local model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PROVIDERS = HERE / "providers.eval.json"
DEFAULT_ROUNDS = 8


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


def check(task: Dict[str, Any], answer: str) -> Dict[str, Any]:
    """Deterministic scoring. No model decides anything here."""
    reasons: List[str] = []
    ok = True
    lowered = (answer or "").lower()

    for needle in task.get("expect_in_answer", []):
        if needle.lower() not in lowered:
            ok = False
            reasons.append(f"missing {needle!r}")
    for needle in task.get("reject_in_answer", []):
        if needle.lower() in lowered:
            ok = False
            reasons.append(f"said {needle!r}")

    want_order = task.get("expect_ordered")
    if want_order:
        positions = [lowered.find(name.lower()) for name in want_order]
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


async def main_async(args) -> int:
    from dpc_client_core.llm_manager import LLMManager
    from dpc_client_core.dpc_agent.agent import DpcAgent, AgentConfig

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

    providers = json.loads(PROVIDERS.read_text(encoding="utf-8"))
    if args.model:
        providers["providers"][0]["model"] = args.model
    live_providers = workdir / "providers.json"
    live_providers.write_text(json.dumps(providers), encoding="utf-8")

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
    for task in todo:
        outcome = await run_one(agent, task, args.rounds)
        results.append(outcome)
        mark = "pass" if outcome["passed"] else "FAIL"
        print(f"  {mark:4} {outcome['id']:34} {outcome['seconds']:6.1f}s "
              f"{'; '.join(outcome['why'])[:60]}")

    passed = sum(1 for r in results if r["passed"])
    report = {
        "tier": args.tier,
        "model": providers["providers"][0]["model"],
        "tasks": len(results),
        "passed": passed,
        "accuracy": round(passed / len(results), 3) if results else 0.0,
        "seconds": round(time.time() - started, 1),
        "results": results,
    }
    print()
    print(f"{passed}/{len(results)} = {report['accuracy']:.1%} on {report['model']} "
          f"in {report['seconds']}s")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
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
    ap.add_argument("--model", default=None, help="override the local model")
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    ap.add_argument("--json", default=None)
    ap.add_argument("--keep", action="store_true", help="keep the throwaway workdir")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
