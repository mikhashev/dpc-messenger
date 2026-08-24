"""GAIA Level 1 — the only number here that says anything to an outsider.

Third instrument. The other two grade this system against itself; this one
grades it against a public split that other agents publish scores on, so it is
the only one where «better» has a meaning outside this repository.

Read the caveats before quoting any figure it produces:

- **A score from here is not comparable with atomic-agent's 69.8 % unless the
  setup matches.** Theirs ran `qwen-3.6-35b-a3b` (UD-Q4_K_XL) at `n_ctx`
  262144 on an M4 Max, with hybrid recall on against a second embedding
  daemon. Different model, different quantisation, different step budget or
  different memory configuration and the two numbers describe two experiments.
- **Many GAIA tasks need the open web.** A run without working browse tooling
  measures the tooling's absence, not the loop. The report separates tasks the
  agent answered from tasks it could not attempt.
- Scoring is the official-style normalised exact match: numbers compared as
  numbers, comma-separated lists elementwise, strings lowercased and stripped.
  No model judges anything.

The dataset is gated. Accept the licence at
https://huggingface.co/datasets/gaia-benchmark/GAIA, then export `HF_TOKEN`.
The token is never written to disk by this script.

Run from `dpc-client/core`:

    HF_TOKEN=... uv run --with pyarrow python ../../eval/gaia/run_gaia_eval.py --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import string
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# The shared harness bits live one directory up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = "gaia-benchmark/GAIA"
SPLIT = "2023/validation/metadata.level1.parquet"
ATTACHMENT_DIR = "2023/validation"


# --- scoring ---------------------------------------------------------------

_ARTICLES = {"a", "an", "the"}


def _norm_number(text: str) -> Optional[float]:
    cleaned = text.strip().replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _norm_string(text: str) -> str:
    text = text.strip().lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    return " ".join(w for w in text.split() if w not in _ARTICLES)


def scores_as_correct(answer: str, gold: str) -> bool:
    """Official-style normalised exact match. Deterministic, no judgement."""
    if not answer:
        return False
    answer = answer.strip()

    # The loop is told to end with the answer on its own line; take the last
    # non-empty line, then fall back to the whole text.
    candidates = [answer]
    lines = [ln.strip() for ln in answer.splitlines() if ln.strip()]
    if lines:
        candidates.insert(0, lines[-1])
        # "FINAL ANSWER: x" is the phrasing GAIA prompts ask for.
        for ln in reversed(lines):
            m = re.search(r"final answer\s*[:\-]\s*(.+)$", ln, re.IGNORECASE)
            if m:
                candidates.insert(0, m.group(1).strip())
                break

    gold_parts = [p.strip() for p in gold.split(",")] if "," in gold else [gold.strip()]

    for cand in candidates:
        if len(gold_parts) > 1:
            cand_parts = [p.strip() for p in cand.split(",")]
            if len(cand_parts) != len(gold_parts):
                continue
            if all(_matches_one(c, g) for c, g in zip(cand_parts, gold_parts)):
                return True
        elif _matches_one(cand, gold):
            return True
    return False


def _matches_one(candidate: str, gold: str) -> bool:
    gold_num = _norm_number(gold)
    if gold_num is not None:
        cand_num = _norm_number(candidate)
        if cand_num is not None:
            return abs(cand_num - gold_num) < 1e-6
        # A number may sit inside a sentence; take the last number offered.
        found = re.findall(r"-?\d[\d,]*\.?\d*", candidate)
        if found:
            last = _norm_number(found[-1])
            return last is not None and abs(last - gold_num) < 1e-6
        return False
    return _norm_string(candidate) == _norm_string(gold)


# --- dataset ---------------------------------------------------------------

def load_tasks(token: str, limit: Optional[int], with_files: bool) -> List[Dict[str, Any]]:
    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq

    path = hf_hub_download(REPO, SPLIT, repo_type="dataset", token=token)
    rows = pq.read_table(path).to_pylist()
    if not with_files:
        rows = [r for r in rows if not r.get("file_name")]
    if limit:
        rows = rows[:limit]
    return rows


def fetch_attachment(token: str, file_name: str, into: Path) -> Optional[Path]:
    from huggingface_hub import hf_hub_download

    try:
        src = hf_hub_download(
            REPO, f"{ATTACHMENT_DIR}/{file_name}", repo_type="dataset", token=token
        )
    except Exception:
        return None
    into.mkdir(parents=True, exist_ok=True)
    dst = into / file_name
    shutil.copy2(src, dst)
    return dst


def providers_file_for(alias: str, model: Optional[str], base_url: str,
                       context_window: int, workdir: Path) -> tuple:
    """Write the throwaway providers file the eval will run against.

    `--provider-alias` copies the named entry out of the operator's real
    `~/.dpc/providers.json` **verbatim**, so the run uses exactly the
    production configuration rather than a second copy of it that drifts.
    Nothing is written back to the operator's file.
    """
    if alias:
        src = Path.home() / ".dpc" / "providers.json"
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
        entry = dict(match[0])
    else:
        entry = {
            "alias": "eval_local",
            "type": "ollama",
            "model": model,
            "base_url": base_url,
            "context_window": context_window,
        }
    path = workdir / "providers.json"
    path.write_text(json.dumps({"providers": [entry], "default_provider": entry["alias"]}),
                    encoding="utf-8")
    return path, entry


# --- the run ---------------------------------------------------------------

PROMPT_SUFFIX = (
    "\n\nAnswer with the shortest possible answer: a number, a single word, or a "
    "comma-separated list. Do not explain. End your reply with a line reading "
    "FINAL ANSWER: <answer>"
)


async def run_one(agent, row: Dict[str, Any], attachment: Optional[Path]) -> Dict[str, Any]:
    question = row["Question"]
    if attachment:
        question = f"{question}\n\nThe attached file is at: {attachment}"
    started = time.time()
    answer, error = "", None
    try:
        answer = await agent.process(
            message=question + PROMPT_SUFFIX,
            conversation_id=f"gaia-{row['task_id'][:8]}",
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {
        "task_id": row["task_id"],
        "gold": row["Final answer"],
        "answer": (answer or "")[:600],
        "correct": scores_as_correct(answer or "", row["Final answer"]),
        "error": error,
        "had_attachment": bool(row.get("file_name")),
        "seconds": round(time.time() - started, 1),
    }


async def main_async(args) -> int:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is not set — the GAIA split is a gated dataset.")

    from dpc_client_core.llm_manager import LLMManager
    from dpc_client_core.dpc_agent.agent import DpcAgent, AgentConfig

    rows = load_tasks(token, args.limit, args.with_files)
    print(f"{len(rows)} task(s) from GAIA L1 validation", flush=True)

    workdir = Path(tempfile.mkdtemp(prefix="dpc-gaia-"))
    agent_root = workdir / "agent"
    agent_root.mkdir(parents=True, exist_ok=True)
    # Attachments live inside the agent root: outside it, ADR-030 Tier 1 stops
    # every read as off-sandbox and a headless run has no approver to ask.
    attachments_dir = agent_root / "gaia-files"

    providers_path, entry = providers_file_for(
        args.provider_alias, args.model, args.base_url, args.context_window, workdir
    )
    print(f"provider: {entry['alias']!r} type={entry.get('type')} model={entry.get('model')}")

    llm = LLMManager(config_path=providers_path)
    agent = DpcAgent(llm_manager=llm, config=AgentConfig(), agent_root=agent_root)

    approver = None
    if args.auto_approve:
        from _harness.auto_approve import Tier1AutoApprover
        approver = Tier1AutoApprover().start()
        print("Tier 1 auto-approval ON (Tier 2 still blocked)", flush=True)

    results = []
    started = time.time()
    for i, row in enumerate(rows, 1):
        attachment = None
        if row.get("file_name"):
            attachment = fetch_attachment(token, row["file_name"], attachments_dir)
        outcome = await run_one(agent, row, attachment)
        results.append(outcome)
        mark = "OK  " if outcome["correct"] else "MISS"
        # flush: redirected stdout is block-buffered, so a run watched through
        # a log file showed zero completed tasks for over an hour while the
        # agent was demonstrably on its third. The progress line is the only
        # window into a run that takes hours; it has to reach the file.
        print(f"  [{i:2}/{len(rows)}] {mark} {outcome['seconds']:6.1f}s  "
              f"gold={outcome['gold'][:28]!r:32} got={outcome['answer'][-60:].strip()!r}",
              flush=True)

    if approver is not None:
        approver.stop()
    correct = sum(1 for r in results if r["correct"])
    report = {
        "benchmark": "GAIA L1 validation",
        "model": entry.get("model"),
        "provider_type": entry.get("type"),
        "tasks": len(results),
        "correct": correct,
        "accuracy": round(correct / len(results), 3) if results else 0.0,
        "seconds": round(time.time() - started, 1),
        "with_attachments": args.with_files,
        "caveat": (
            "Not comparable with any published figure unless model, quantisation, "
            "context window, step budget and memory configuration all match."
        ),
        "results": results,
        **({"approvals": approver.summary()} if approver is not None else {}),
    }
    print()
    print(f"{correct}/{len(results)} = {report['accuracy']:.1%} on {entry.get('model')} "
          f"in {report['seconds']}s")
    print("NOT comparable with atomic-agent's 69.8%: different model and setup.")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"full report -> {out}")

    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


def main() -> int:
    # Model answers carry arrows, dashes and non-Latin text; a Windows console
    # defaults to cp1252 and a run that finished would die on printing it.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--provider-alias", default=None,
                    help="run the eval on this alias from ~/.dpc/providers.json, verbatim")
    ap.add_argument("--model", default="qwen3.8:latest")
    ap.add_argument("--base-url", default="http://127.0.0.1:11434")
    ap.add_argument("--context-window", type=int, default=32768)
    ap.add_argument("--with-files", action="store_true",
                    help="include the 11 tasks that carry an attachment")
    ap.add_argument("--auto-approve", action="store_true",
                    help="answer ADR-030 Tier 1 prompts automatically — eval only; "
                         "Tier 2 remains hard-blocked and never reaches the queue")
    ap.add_argument("--json", default=None)
    ap.add_argument("--keep", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
