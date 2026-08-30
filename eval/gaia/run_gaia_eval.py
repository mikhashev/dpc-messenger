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
import hashlib
import json
import os
import re
import shutil
import string
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# The shared harness bits live one directory up.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from _harness import provenance  # noqa: E402

_DATASET_STATE = {}

RESULTS_DIR = HERE / "results"
REPO = "gaia-benchmark/GAIA"
SPLIT = "2023/validation/metadata.level1.parquet"
ATTACHMENT_DIR = "2023/validation"
# Where this project moved eleven gold-bearing files on 2026-08-29 rather than
# deleting them. Named here so the guard can see its own cold storage.
GOLD_ARCHIVE = Path.home() / "gaia-archive"


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

def hub_caches_in_effect() -> List[Path]:
    """Where huggingface_hub puts and finds things, as configured right now.

    Read before this run redirects anything: this is the cache the machine
    really uses, and the one an agent walks when it goes looking.
    """
    out: List[Path] = []
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(var):
            out.append(Path(os.environ[var]))
    if os.environ.get("HF_HOME"):
        out.append(Path(os.environ["HF_HOME"]) / "hub")
    out.append(Path.home() / ".cache" / "huggingface" / "hub")
    # Cold storage this project created on 2026-08-29 when it moved eleven
    # gold-bearing files «somewhere no profile's sandbox lists». Nothing in the
    # guard looked there afterwards, so the refusal it advertises was void for
    # every run since. It is ours, so it is named here rather than guessed at.
    out.append(GOLD_ARCHIVE)
    seen, uniq = set(), []
    for c in out:
        key = str(c).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def reachable_gold(caches: List[Path], results_dir: Optional[Path] = None,
                   archives: Optional[List[Path]] = None) -> List[Path]:
    """Copies of the answers an agent on this machine could open and read.

    Three kinds: the dataset in the hub cache; any report written before this
    run recorded digests instead of answers; and any run log from then, whose
    per-task progress line printed the gold beside the answer.
    """
    marker = "datasets--" + REPO.replace("/", "--")
    found = [c / marker for c in caches if (c / marker).is_dir()]
    # A directory that is not a hub cache still holds the split if the parquet
    # is anywhere under it: the archive keeps the snapshot layout without the
    # `datasets--` root, so the marker above walks straight past it.
    # The split by name, under anything enumerated: the archive keeps the
    # snapshot layout without the `datasets--` root, so the marker above walks
    # straight past it.
    split_name = Path(SPLIT).name
    for c in caches:
        if c.is_dir() and not (c / marker).is_dir():
            found.extend(sorted(c.rglob(split_name)))
    # The text scan runs only where *we* write reports — the caller names them.
    # A model cache is not one of those, and pointing it there refuses on
    # nineteen `vocab.json` files: a tokeniser maps the word «gold» to an id, so
    # the key test matches and the benchmark refuses because GPT-2 knows the word.
    for root in list(archives or []) + [results_dir]:
        found.extend(_files_carrying_gold(root))
    return found


def _files_carrying_gold(root: Optional[Path]) -> List[Path]:
    """Reports and logs under `root` that spell an answer out."""
    if not root or not root.is_dir():
        return []
    out = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix not in (".json", ".log"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if '"gold"' in text or "gold='" in text:
            out.append(f)
    return out


def gold_in_traces(results_dir: Optional[Path]) -> List[Path]:
    """Tool ledgers carrying the answers as free text — reported, not refused.

    The gold reaches `*.agent-logs/tools.jsonl` as the output of whatever the
    agent ran, so it is not the `"gold"` key the refusal keys on. Refusing on it
    would stop tonight's campaign for every ledger written before today, which
    is a decision rather than a fix; until it is taken, the run says what it
    found instead of pretending the directory is clean.
    """
    if not results_dir or not results_dir.is_dir():
        return []
    needle = "Final answer"
    hits = []
    for f in sorted(results_dir.rglob("*.jsonl")):
        try:
            if needle in f.read_text(encoding="utf-8", errors="replace"):
                hits.append(f)
        except Exception:
            continue
    return hits


# A task id in a search query is not ambiguous: nothing but the answer key uses
# it. On 2026-08-29 a run searched `"72e110e7-…" answer`, found a public mirror
# with the same question, and its answer said so — the local guards saw nothing,
# because nothing local was touched. No decoy can be planted on the open web, so
# the ledger is the only surface there is.
_MIRROR_RE = re.compile(
    r"huggingface\.co/(?:api/)?datasets/\S*gaia|harbor-datasets|cmriat/gaia",
    re.I,
)


def web_lookups(logs_dir: Optional[Path], task_ids: List[str]) -> List[Dict[str, str]]:
    """Ledger evidence that a run went looking for the answers on the web.

    Reports, never refuses: a mirror URL can appear in an honest search result,
    and a task id can be quoted by the harness itself. It names the file and
    what matched, and a reader decides.
    """
    if not logs_dir or not logs_dir.is_dir():
        return []
    ids = [t for t in task_ids if t]
    hits: List[Dict[str, str]] = []
    for f in sorted(logs_dir.rglob("*.jsonl")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        mirror = _MIRROR_RE.search(text)
        searched = next((t for t in ids if t in text), None)
        if mirror:
            hits.append({"file": f.name, "marker": mirror.group(0), "kind": "mirror"})
        if searched:
            hits.append({"file": f.name, "marker": searched, "kind": "task_id_in_trace"})
    return hits


BENCH_PROFILE = "gaia_benchmark"
BENCH_RULES = HERE / "benchmark_rules.json"


def benchmark_firewall(workdir: Path):
    """The rules a benchmark agent runs under, owned by the run.

    Not the operator's `~/.dpc/privacy_rules.json`: that file grants eight
    profiles read of this repository, it is edited between runs, and loading it
    would let a score depend on one machine's personal configuration. The copy
    lands in the workdir because the firewall reconciles tool keys against the
    registry and writes the result back.
    """
    from dpc_client_core.dpc_agent.tools.registry import ToolRegistry
    from dpc_client_core.firewall import ContextFirewall

    # Every tool the registry knows, on. The runs this score is compared with
    # had no firewall at all, and the registry's own defaults leave the web,
    # the shell and write_file off - a benchmark that quietly drops them
    # measures the tooling rather than the loop.
    rules = json.loads(BENCH_RULES.read_text(encoding="utf-8"))
    every_tool = {name: True for name in ToolRegistry()._entries}
    rules["dpc_agent"]["tools"] = dict(every_tool)
    rules["agent_profiles"][BENCH_PROFILE]["tools"] = dict(every_tool)

    local = workdir / "privacy_rules.json"
    local.write_text(json.dumps(rules, indent=2), encoding="utf-8")
    return ContextFirewall(local)


def gold_fingerprint(gold: str) -> str:
    """What a report may say about an answer without handing it over.

    A run used to record the answer itself so a later re-score could compare
    against it, which left one readable answer key per run on the machine the
    next run's agent walks. The digest identifies the answer without being it;
    a re-score reads the dataset, which is where the answers belong.
    """
    return hashlib.sha256(gold.strip().encode("utf-8")).hexdigest()


CANARY_NAME = "gaia-validation-answers.json"


def plant_canary(caches: List[Path], results_dir: Optional[Path], token: str) -> List[Path]:
    """A decoy answer key where the real one used to live.

    An empty machine and an agent that never looked produce the same clean
    trace, so the trace alone cannot say which happened. The decoy is the
    positive control: bait an agent would open if it went looking, holding a
    token that appears nowhere else, so finding it in an answer or in the tool
    ledger is proof of a read rather than absence of evidence.
    """
    payload = json.dumps(
        {"canary": token, "note": "decoy planted by the GAIA harness", "answers": {}},
        indent=2,
    )
    planted: List[Path] = []
    for target in [c / CANARY_NAME for c in caches] + (
        [results_dir / CANARY_NAME] if results_dir else []
    ):
        if not target.parent.is_dir():
            continue
        try:
            target.write_text(payload, encoding="utf-8")
        except Exception:
            continue
        planted.append(target)
    return planted


def canary_was_read(token: str, results: List[Dict[str, Any]], logs_dir: Path) -> Dict[str, Any]:
    """Where the token surfaced: the answers, the tool ledger, or nowhere."""
    in_answers = [r["task_id"] for r in results if token in (r.get("answer") or "")]
    in_trace = []
    if logs_dir.is_dir():
        for f in sorted(logs_dir.rglob("*.jsonl")):
            try:
                if token in f.read_text(encoding="utf-8", errors="replace"):
                    in_trace.append(f.name)
            except Exception:
                continue
    return {
        "token": token,
        "seen_in_answers": in_answers,
        "seen_in_trace": in_trace,
        "triggered": bool(in_answers or in_trace),
    }


# Phrases an answer reached for when it had found the key rather than solved the
# task. Every one is verbatim from a report on this machine — 2026-08-25
# («the official answer file confirms»), 08-28 («I located the official answer in
# the local GAIA dataset»), 08-29 («I found the ground truth answer from a dataset
# with this exact question»). The canary saw none of them: the file read was real.
_ADMISSION_RE = re.compile(
    r"reference answer|official answer|answer key|ground.truth answer|true_answer"
    r"|authoritative source|local GAIA dataset|metadata\.level1",
    re.I,
)


def answers_admitting_a_lookup(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tasks whose own answer text says where the answer came from.

    A second surface rather than a replacement: the trace detector keys on the
    parquet's column names surfacing in a tool result, and a run whose logs have
    been deleted cannot be checked that way at all — the stored answer outlives
    the logs. This flags for a reader; it scores nothing and refuses nothing,
    because an honest answer may quote the phrase too.
    """
    flagged = []
    for row in results:
        hit = _ADMISSION_RE.search(row.get("answer") or "")
        if hit:
            flagged.append({
                "task_id": row.get("task_id"),
                "phrase": hit.group(0),
                "correct": row.get("correct"),
            })
    return flagged


_GATED_TOKEN_VARS = ("HF_TOKEN", "HUGGINGFACE_HUB_TOKEN")


def drop_gated_credentials() -> List[str]:
    """Remove the dataset credential from the environment the agent inherits.

    The token outlives the answers it fetched: the agent runs in-process and its
    shell inherits `os.environ`, so a gated re-download of the *original* key is
    one command away from a run that has just deleted its local copy — and it
    evades both the canary and the path enumeration, which watch files. Called
    once the attachments are prefetched, after which nothing here needs it.
    """
    return [var for var in _GATED_TOKEN_VARS if os.environ.pop(var, None) is not None]


_HUB_VARS = ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_DATASETS_CACHE")


def redirect_hub_into(workdir: Path) -> Tuple[Path, Dict[str, Optional[str]]]:
    """Point huggingface_hub at a directory this run owns, so it can delete it.

    Returns the directory and the settings it replaced, which `restore_hub`
    puts back: the redirect hides every other model in the machine's cache as
    well as the dataset, and the agent needs its embedding model from there.
    """
    private = workdir / "hf"
    (private / "hub").mkdir(parents=True, exist_ok=True)
    previous = {var: os.environ.get(var) for var in _HUB_VARS}
    os.environ["HF_HOME"] = str(private)
    os.environ["HF_HUB_CACHE"] = str(private / "hub")
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(private / "hub")
    os.environ["HF_DATASETS_CACHE"] = str(private / "datasets")
    return private, previous


def restore_hub(previous: Dict[str, Optional[str]]) -> None:
    """Give the machine's own cache back, once the dataset is gone from disk.

    The environment is only half of it. `huggingface_hub` reads these variables
    **once, at import**, into `constants.HF_HUB_CACHE`, and `load_tasks` does
    that import inside the redirect — so putting the variables back left the
    library still pointing at a directory this run had just deleted. Measured
    2026-08-30 on a --limit 1 run: the agent re-downloaded BAAI/bge-m3, 2.27 GB,
    into the temp hub, and the first task ran while that download was at 37 %.
    Every run since the redirect landed has paid it.
    """
    for var, value in previous.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
    try:
        from huggingface_hub import constants as _hf_constants

        home = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
        _hf_constants.HF_HOME = home
        _hf_constants.HF_HUB_CACHE = os.environ.get("HF_HUB_CACHE") or str(Path(home) / "hub")
        _hf_constants.HUGGINGFACE_HUB_CACHE = _hf_constants.HF_HUB_CACHE
    except Exception:
        # The library not being importable here is not a reason to fail a run;
        # the cost of missing it is a re-download, not a wrong number.
        pass


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
    # An opaque id. `gaia-<task_id[:8]>` handed the model the split's own row
    # key, and a 2026-08-25 answer used it: «matching our gaia-e142056d … the
    # official answer file confirms». The report keeps the mapping.
    conversation_id = f"gaia-run-{uuid.uuid4().hex[:12]}"
    started = time.time()
    answer, error = "", None
    try:
        answer = await agent.process(
            message=question + PROMPT_SUFFIX,
            conversation_id=conversation_id,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    # The loop accumulates usage and `process()` returns only text, so the
    # counters live on the agent afterwards. Absent stays absent: a task whose
    # provider reported nothing records `null`, not a confident zero.
    usage = dict(getattr(agent, "_last_usage", None) or {})
    return {
        "task_id": row["task_id"],
        "conversation_id": conversation_id,
        "gold_sha256": gold_fingerprint(row["Final answer"]),
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

    # Measured 2026-08-28: an agent wrote a script into its own sandbox and ran
    # it, and the script read the gold parquet out of the hub cache — two Tier-0
    # steps that no path gate sees, because the path lives inside the file. So
    # the answer is not a better gate, it is not leaving the answers where a
    # process can open them.
    real_caches = hub_caches_in_effect()
    visible = reachable_gold(real_caches, RESULTS_DIR, archives=[GOLD_ARCHIVE])
    _DATASET_STATE["gold_reachable_at_start"] = [str(v) for v in visible]
    _DATASET_STATE["gold_reachable_allowed"] = bool(args.allow_reachable_gold)
    if visible and not args.allow_reachable_gold:
        listing = "\n  ".join(str(v) for v in visible[:8])
        more = f"\n  … and {len(visible) - 8} more" if len(visible) > 8 else ""
        raise SystemExit(
            "the answers are readable from this machine, so a score would not be "
            f"worth reading:\n  {listing}{more}\n"
            "move or delete them, or pass --allow-reachable-gold to run anyway "
            "(the run then records that it was contaminable)."
        )
    if visible:
        print(f"WARNING: {len(visible)} readable copy(ies) of the answers left in place "
              "on purpose — this score is contaminable", flush=True)

    workdir = Path(tempfile.mkdtemp(prefix="dpc-gaia-"))
    # Staging only. Each task gets its own agent root below, and the attachment
    # it needs is copied in: outside the root, ADR-030 Tier 1 stops every read
    # as off-sandbox and a headless run has no approver to ask.
    attachments_dir = workdir / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    logs_root = workdir / "agent-logs"
    logs_root.mkdir(parents=True, exist_ok=True)

    private_hub, hub_before = redirect_hub_into(workdir)
    rows = load_tasks(token, args.limit, args.with_files)
    print(f"{len(rows)} task(s) from GAIA L1 validation", flush=True)

    # Every attachment is fetched now, while the cache still exists, and copied
    # into the sandbox. After this the run needs the hub for nothing, so the
    # cache goes and the gold survives only in `rows` — in memory.
    prefetched: Dict[str, Optional[Path]] = {}
    for row in rows:
        if row.get("file_name"):
            prefetched[row["task_id"]] = fetch_attachment(
                token, row["file_name"], attachments_dir
            )
    shutil.rmtree(private_hub, ignore_errors=True)
    _DATASET_STATE["private_cache_removed"] = not private_hub.exists()
    restore_hub(hub_before)
    _DATASET_STATE["token_dropped"] = drop_gated_credentials()
    print(f"attachments prefetched: {len(prefetched)}; hub cache removed: "
          f"{_DATASET_STATE['private_cache_removed']}", flush=True)

    providers_path, entry = providers_file_for(
        args.provider_alias, args.model, args.base_url, args.context_window, workdir,
        temperature=args.temperature, reasoning_effort=args.reasoning_effort,
    )
    print(f"provider: {entry['alias']!r} type={entry.get('type')} model={entry.get('model')}")

    # Planted here rather than thirty lines earlier, and the distance was the
    # bug: the `finally` that removes the decoy starts below, and `load_tasks`
    # sat in between. The night of 2026-08-30 03:12 died there on a stale token
    # four times and left a decoy behind each time. Nothing between the old site
    # and this one reads the canary, and the agent — the only thing the bait is
    # for — runs below.
    canary_token = f"GAIA-CANARY-{uuid.uuid4().hex}"
    canary_files = plant_canary(real_caches, RESULTS_DIR, canary_token)
    _DATASET_STATE["canary_planted"] = [str(p) for p in canary_files]
    print(f"canary planted in {len(canary_files)} place(s)", flush=True)

    llm = LLMManager(config_path=providers_path)
    firewall = benchmark_firewall(workdir)
    try:
        approver = None
        if args.auto_approve:
            from _harness.auto_approve import Tier1AutoApprover
            approver = Tier1AutoApprover().start()
            print("Tier 1 auto-approval ON (Tier 2 still blocked)", flush=True)

        results = []
        started = time.time()
        for i, row in enumerate(rows, 1):
            # A root per task, so nothing an agent writes reaches the next one:
            # scratchpad, knowledge, logs and task_results all start empty.
            task_root = workdir / f"task-{i:03d}"
            (task_root / "gaia-files").mkdir(parents=True, exist_ok=True)
            attachment = prefetched.get(row["task_id"])
            if attachment is not None:
                attachment = Path(shutil.copy2(attachment, task_root / "gaia-files"))
            agent = DpcAgent(
                llm_manager=llm, config=AgentConfig(), agent_root=task_root,
                firewall=firewall, firewall_profile=BENCH_PROFILE,
            )
            outcome = await run_one(agent, row, attachment)
            if (task_root / "logs").is_dir():
                shutil.copytree(task_root / "logs", logs_root / task_root.name,
                                dirs_exist_ok=True)
            results.append(outcome)
            mark = "OK  " if outcome["correct"] else "MISS"
            # flush: redirected stdout is block-buffered, so a run watched through
            # a log file showed zero completed tasks for over an hour while the
            # agent was demonstrably on its third. The progress line is the only
            # window into a run that takes hours; it has to reach the file.
            print(f"  [{i:2}/{len(rows)}] {mark} {outcome['seconds']:6.1f}s  "
                  f"task={outcome['task_id'][:8]} got={outcome['answer'][-60:].strip()!r}",
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
            "canary": {
                **canary_was_read(canary_token, results, logs_root),
                "planted": [str(p) for p in canary_files],
            },
            # What the canary structurally cannot see: an answer that names its
            # own source. Four such tasks are already on the board for 2026-08-28
            # and two more were found on 08-30 in runs nobody had re-read.
            "admissions": answers_admitting_a_lookup(results),
            # The two surfaces that report rather than refuse: answers reachable
            # in the run's own ledgers, and the open web, where nothing can be
            # planted. Turning either into a refusal stops the campaign for every
            # ledger already on disk, which is a decision, not a fix.
            "traces_carrying_gold": [str(p) for p in gold_in_traces(RESULTS_DIR)],
            "web_lookups": web_lookups(logs_root, [r.get("task_id") for r in results]),
            # The one bit whose whole job is «this number is dirty», computed at
            # :449 since the guard was written and dropped before the report ever
            # since — the `--allow-reachable-gold` help promises the run records
            # it. Found by GLM 5.3 in the 2026-08-30 history audit; no report on
            # disk carries either field.
            "containment": {
                "gold_reachable_at_start": _DATASET_STATE.get("gold_reachable_at_start", []),
                "gold_reachable_allowed": _DATASET_STATE.get("gold_reachable_allowed", False),
                "private_cache_removed": _DATASET_STATE.get("private_cache_removed"),
            },
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
                    # Which tools were on decides what the number measures at
                    # least as much as the model does: the runs before this one
                    # had no firewall, so they had all of them.
                    "tools_enabled": sorted(
                        name for name, on in
                        firewall.get_agent_tools_map(BENCH_PROFILE).items() if on
                    ),
                },
            ),
        }
        print()
        tok = report["tokens"]["total"]
        print(f"{correct}/{len(results)} = {report['accuracy']:.1%} on {entry.get('model')} "
              f"in {report['seconds']}s | {tok['total']} tokens over {tok['reported_by']} task(s)",
              flush=True)
        print("NOT comparable with atomic-agent's 69.8%: different model and setup.")
        if report["canary"]["triggered"]:
            print(f"CANARY TRIGGERED: the decoy answer key was read — "
                  f"answers={report['canary']['seen_in_answers']} "
                  f"trace={report['canary']['seen_in_trace']}", flush=True)

        if args.json:
            out = Path(args.json)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            provenance.write_beside(out, report["provenance"])
            print(f"full report -> {out}", flush=True)
            # The tool ledger this run wrote is evidence, and the cleanup below
            # deletes it with the workdir — so a night could score itself and never
            # be an observation of anything. Copied beside the report instead.
            logs_src = logs_root
            if any(logs_src.iterdir()):
                logs_dst = out.parent / f"{out.stem}.agent-logs"
                shutil.rmtree(logs_dst, ignore_errors=True)
                shutil.copytree(logs_src, logs_dst)
                print(f"agent logs  -> {logs_dst}", flush=True)

        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        return 0
    finally:
        for bait in canary_files:
            bait.unlink(missing_ok=True)
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
    ap.add_argument("--allow-reachable-gold", action="store_true",
                    help="run even though the answers are readable on this machine; "
                         "the report records that the score is contaminable")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
