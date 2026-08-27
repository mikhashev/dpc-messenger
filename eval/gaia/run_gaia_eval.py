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
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from _harness import provenance  # noqa: E402

_DATASET_STATE = {}

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


# The line the prompt asks for, at the start of its own line — with or without
# the colon, because a missing colon is a formatting slip and not a wrong
# answer. The second form allows the marker mid-line only when a separator
# makes the span unambiguous, so "the final answer is 42" stays unmatched
# rather than answering "is 42".
_FINAL_ANSWER_ANCHORED = re.compile(r"^\**\s*final\s*answer\s*\**\s*[:\-]?\s*(.*)$", re.IGNORECASE)
_FINAL_ANSWER_WITH_SEP = re.compile(r"final\s*answer\s*\**\s*[:\-]\s*(.*)$", re.IGNORECASE)


def extract_final_answer(answer: str) -> Optional[str]:
    """The span the prompt asked for, or None. There is no second guess."""
    for line in reversed([ln.strip() for ln in answer.splitlines() if ln.strip()]):
        for pattern in (_FINAL_ANSWER_ANCHORED, _FINAL_ANSWER_WITH_SEP):
            m = pattern.search(line)
            if m:
                span = m.group(1).strip().strip("*").strip()
                if span:
                    return span
    return None


def scores_as_correct(answer: str, gold: str) -> bool:
    """Normalised exact match against the FINAL ANSWER span. No scavenging.

    Scoring anything but that span is what made the old version wrong in both
    directions: with three candidates and a last-number-in-free-text fallback,
    "the correct count is 3, not 2" scored as an answer of 2.
    """
    if not answer:
        return False
    span = extract_final_answer(answer)
    if span is None:
        return False
    return _matches(span, gold)


def _matches(candidate: str, gold: str) -> bool:
    gold = gold.strip()
    # A comma inside a number is a thousands separator, not a list delimiter —
    # splitting `1,234` produced a two-element gold nothing could ever match.
    if "," in gold and _norm_number(gold) is None:
        gold_parts = [p.strip() for p in gold.split(",")]
        cand_parts = [p.strip() for p in candidate.split(",")]
        if len(cand_parts) != len(gold_parts):
            return False
        return all(_matches_one(c, g) for c, g in zip(cand_parts, gold_parts))
    return _matches_one(candidate, gold)


def _matches_one(candidate: str, gold: str) -> bool:
    gold_num = _norm_number(gold)
    if gold_num is not None:
        # The candidate must BE the number, never merely contain one.
        cand_num = _norm_number(candidate)
        return cand_num is not None and abs(cand_num - gold_num) < 1e-6
    return _norm_string(candidate) == _norm_string(gold)


# --- dataset ---------------------------------------------------------------

def load_tasks(token: str, limit: Optional[int], with_files: bool) -> List[Dict[str, Any]]:
    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq

    path = hf_hub_download(REPO, SPLIT, repo_type="dataset", token=token)
    _DATASET_STATE["local_path"] = str(path)
    # .../snapshots/<revision>/2023/validation/... — the revision is the only
    # thing that pins which version of a gated dataset was actually read.
    parts = Path(path).parts
    if "snapshots" in parts:
        _DATASET_STATE["revision"] = parts[parts.index("snapshots") + 1]
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
                       context_window: int, workdir: Path,
                       temperature: Optional[float] = None,
                       reasoning_effort: Optional[str] = None) -> tuple:
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
    # Both axes are pinned rather than inherited when asked for. Left alone,
    # `reasoning_effort` is absent from the alias and the provider sends no
    # word at all, which the model's own template answers with its default —
    # `xhigh` for this one. An unrecorded default is not a setting, it is a
    # guess that looks like a setting.
    if temperature is not None:
        entry["temperature"] = temperature
    if reasoning_effort:
        entry["reasoning_effort"] = reasoning_effort

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
    # The loop accumulates usage and `process()` returns only text, so the
    # counters live on the agent afterwards. Absent stays absent: a task whose
    # provider reported nothing records `null`, not a confident zero.
    usage = dict(getattr(agent, "_last_usage", None) or {})
    return {
        "task_id": row["task_id"],
        "gold": row["Final answer"],
        "answer": (answer or "")[:600],
        "correct": scores_as_correct(answer or "", row["Final answer"]),
        "error": error,
        "had_attachment": bool(row.get("file_name")),
        "seconds": round(time.time() - started, 1),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "rounds": usage.get("rounds"),
            "cost_usd": usage.get("cost"),
            # Reported by providers that have a prompt cache (DeepSeek does).
            # The local llama.cpp path does not report it today — recorded as
            # missing rather than as zero, because a zero here would read as
            # "the cache never hit".
            "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
            "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
        },
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
        args.provider_alias, args.model, args.base_url, args.context_window, workdir,
        temperature=args.temperature, reasoning_effort=args.reasoning_effort,
    )
    print(f"provider: {entry['alias']!r} type={entry.get('type')} model={entry.get('model')}")

    llm = LLMManager(config_path=providers_path)
    try:
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

        def _sum(field):
            vals = [r["usage"].get(field) for r in results]
            present = [v for v in vals if isinstance(v, (int, float))]
            # Three numbers, never two: how many tasks reported it, how many did
            # not, and the total over those that did.
            return {"total": sum(present), "reported_by": len(present),
                    "not_reported_by": len(vals) - len(present)}
        report = {
            "benchmark": "GAIA L1 validation",
            "model": entry.get("model"),
            "provider_type": entry.get("type"),
            "temperature": entry.get("temperature"),
            "reasoning_effort": entry.get("reasoning_effort", "(template default: xhigh)"),
            "tasks": len(results),
            "correct": correct,
            "accuracy": round(correct / len(results), 3) if results else 0.0,
            "seconds": round(time.time() - started, 1),
            "with_attachments": args.with_files,
            "caveat": (
                "Not comparable with any published figure unless model, quantisation, "
                "context window, step budget and memory configuration all match."
            ),
            "tokens": {
                "prompt": _sum("prompt_tokens"),
                "completion": _sum("completion_tokens"),
                "total": _sum("total_tokens"),
                "rounds": _sum("rounds"),
                "cost_usd": _sum("cost_usd"),
                "prompt_cache_hit": _sum("prompt_cache_hit_tokens"),
                "prompt_cache_miss": _sum("prompt_cache_miss_tokens"),
                "note": ("cache hit/miss is reported only by providers that expose a "
                         "prompt cache; reported_by / not_reported_by above say which "
                         "of this run's tasks did, so absent never reads as zero"),
            },
            "results": results,
            **({"approvals": approver.summary()} if approver is not None else {}),
            "provenance": provenance.snapshot(
                repo_root=HERE.parent.parent,
                provider_entry=entry,
                dataset={
                    "repo": REPO,
                    "split_file": SPLIT,
                    "revision": _DATASET_STATE.get("revision", "[unresolved]"),
                    "local_path": _DATASET_STATE.get("local_path", "[unresolved]"),
                    "tasks_selected": len(rows),
                    "attachments_included": args.with_files,
                    "limit": args.limit,
                },
                harness_file=Path(__file__).resolve(),
                argv=sys.argv[1:],
                extra={
                    "scoring": "normalised exact match; numbers as numbers, "
                               "comma lists elementwise, strings lowercased and "
                               "de-articled; no model judges anything",
                    "tier1_auto_approved": bool(args.auto_approve),
                },
            ),
        }
        print()
        tok = report["tokens"]["total"]
        print(f"{correct}/{len(results)} = {report['accuracy']:.1%} on {entry.get('model')} "
              f"in {report['seconds']}s | {tok['total']} tokens over {tok['reported_by']} task(s)",
              flush=True)
        print("NOT comparable with atomic-agent's 69.8%: different model and setup.")

        if args.json:
            out = Path(args.json)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            provenance.write_beside(out, report["provenance"])
            print(f"full report -> {out}", flush=True)
            # The tool ledger this run wrote is evidence, and the cleanup below
            # deletes it with the workdir — so a night could score itself and never
            # be an observation of anything. Copied beside the report instead.
            logs_src = agent_root / "logs"
            if logs_src.is_dir():
                logs_dst = out.parent / f"{out.stem}.agent-logs"
                shutil.rmtree(logs_dst, ignore_errors=True)
                shutil.copytree(logs_src, logs_dst)
                print(f"agent logs  -> {logs_dst}", flush=True)

        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        return 0
    finally:
        # Without this the llama-server the provider spawned outlives the run,
        # and the campaign's own GPU gate then waits for a child of the run
        # before it: 0 of 4 runs started on 2026-08-25, 1 of 4 on 2026-08-27.
        try:
            await llm.shutdown()
        except Exception as exc:
            print(f"warning: provider shutdown failed: {exc}", flush=True)

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
    ap.add_argument("--temperature", type=float, default=None,
                    help="pin the sampling temperature for this run")
    ap.add_argument("--reasoning-effort", default=None,
                    help="pin the reasoning effort word (low/medium/high/max/xhigh/off)")
    ap.add_argument("--auto-approve", action="store_true",
                    help="answer ADR-030 Tier 1 prompts automatically — eval only; "
                         "Tier 2 remains hard-blocked and never reaches the queue")
    ap.add_argument("--json", default=None)
    ap.add_argument("--keep", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
