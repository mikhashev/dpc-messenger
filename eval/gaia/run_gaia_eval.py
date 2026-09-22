"""GAIA Level 1 — the only number here that says anything to an outsider.

Third instrument. The other two grade this system against itself; this one
grades it against a public split that other agents publish scores on, so it is
the only one where «better» has a meaning outside this repository.

Read the caveats before quoting any figure it produces:

- **A score from here is comparable with runs of this harness and with nothing
  else.** GAIA is public and plenty of figures are published on it; every one of
  them was produced with some model, quantisation, context window, step budget
  and memory configuration, and unless all five match, two numbers on the same
  split describe two experiments. Even against ourselves the floor is coarse:
  two greedy runs of one configuration disagreed on 14 of 53 tasks. (The figure
  that prompted this instrument was atomic-agent's 69.8 % — `qwen-3.6-35b-a3b`
  UD-Q4_K_XL at n_ctx 262144 on an M4 Max with hybrid recall against a second
  embedding daemon. It is here as provenance, not as a comparator.)
- **Many GAIA tasks need the open web.** A run without working browse tooling
  measures the tooling's absence, not the loop. The report separates tasks the
  agent answered from tasks it could not attempt.
- Scoring is the official leaderboard's `question_scorer`, ported rule for rule
  (`OFFICIAL_SCORER` below names the revision it was read at and the two
  deliberate deviations). No model judges anything.

The dataset is gated. Accept the licence at
https://huggingface.co/datasets/gaia-benchmark/GAIA, then either export
`HF_TOKEN` or log in once with `hf auth login`; the stored token is used when
the variable is unset. The token is never written to disk by this script.

The one command for an overnight campaign is `campaign.py` beside this file.
A single run, from `dpc-client/core`:

    uv run --with pyarrow python ../../eval/gaia/run_gaia_eval.py --limit 5
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
from _harness import benchmark_tools, provenance  # noqa: E402
from _harness.results_root import results_root  # noqa: E402

_DATASET_STATE = {}

# Not `HERE / "results"` any more: the guards below (reachable_gold,
# plant_canary, gold_in_traces) all read this path, so it has to be the one
# the runs actually write to or they inspect an empty directory and call it
# clean.
RESULTS_DIR = results_root("gaia")
REPO = "gaia-benchmark/GAIA"
SPLIT = "2023/validation/metadata.level1.parquet"
ATTACHMENT_DIR = "2023/validation"
# Where this project moved eleven gold-bearing files on 2026-08-29 rather than
# deleting them. Named here so the guard can see its own cold storage.
GOLD_ARCHIVE = Path.home() / "gaia-archive"
# What a crashed run leaves behind. The private hub lives at <workdir>/hf,
# and until 2026-09-02 nothing looked for it, so a leaked copy of the answers
# did not stop the next run the way every other cache does.
LEAKED_WORKDIR_GLOB = "dpc-gaia-*"
# A run whose canary was read exits with this rather than 0, so «the agent
# found a planted answer key» is visible to a caller that reads status codes
# and never opens the report.
CONTAMINATED_EXIT = 3
# The documented path: the local llama-server alias. The Ollama branch stays
# for a deliberate `--model`, never as a silent fallback.
DEFAULT_ALIAS = "qwen3.8 27b"


# --- scoring ---------------------------------------------------------------
#
# `question_scorer` from the official leaderboard (see OFFICIAL_SCORER), rule
# for rule, because this number exists to stand beside other people's: no
# article removal, all whitespace removed, lists split on `,`/`;` and ordered,
# a comma-formatted numeric gold is a list, units and Unicode minus are misses.
# Two deliberate deviations: only the FINAL ANSWER span is graded (the paper's
# prompt asks for that template), and a non-number against a numeric gold is a
# miss where official maps it to `inf`, which would equal a gold of "inf".

OFFICIAL_SCORER = {
    "source": "huggingface.co/spaces/gaia-benchmark/leaderboard scorer.py",
    "revision": "9f133d71362e77b3539f1514f31b9c101a545fec",
    "sha256": "0d44c07f3046eec521697c22e3eaca8719cc81e422a8eaf32695c5f22bdac6e2",
    "deviations": ["only the FINAL ANSWER span is graded; no span is a miss",
                   "a non-numeric answer to a numeric gold is a miss, not inf"],
}

_PUNCT = str.maketrans("", "", string.punctuation)
_LIST_SPLIT = re.compile(r"[,;]")


def _is_float(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _norm_number(text: str) -> Optional[float]:
    for char in ("$", "%", ","):
        text = text.replace(char, "")
    try:
        return float(text)
    except ValueError:
        return None


def _norm_string(text: str, remove_punct: bool = True) -> str:
    text = re.sub(r"\s", "", text).lower()
    return text.translate(_PUNCT) if remove_punct else text


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


def _number_equals(candidate: str, gold: str) -> bool:
    # The candidate must BE the number, never merely contain one (deviation 2).
    value = _norm_number(candidate)
    return value is not None and value == float(gold)


def _matches(candidate: str, gold: str) -> bool:
    """The official `question_scorer`, branch for branch."""
    if _is_float(gold):
        return _number_equals(candidate, gold)
    if "," in gold or ";" in gold:
        gold_parts = _LIST_SPLIT.split(gold)
        cand_parts = _LIST_SPLIT.split(candidate)
        if len(cand_parts) != len(gold_parts):
            return False
        return all(
            _number_equals(c, g) if _is_float(g)
            else _norm_string(c, remove_punct=False) == _norm_string(g, remove_punct=False)
            for c, g in zip(cand_parts, gold_parts)
        )
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
    out.extend(sorted(Path(tempfile.gettempdir()).glob(LEAKED_WORKDIR_GLOB)))
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
    found instead of pretending the directory is clean. A run reports it over
    its own ledger only: the whole results tree names earlier nights' files.
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
# The named mirrors are the ones A-MIRROR-PAGE-FULL-OF-GOLD... found in run
# context; the last branch is that entry's shape rule, because a static list
# of mirrors is always short: any URL carrying `gaia` beside a split word.
_MIRROR_RE = re.compile(
    r"huggingface\.co/(?:api/)?(?:datasets|spaces)/\S*gaia|harbor-datasets"
    r"|cmriat/gaia|bstraehle/gaia|MinorJerry/WebVoyager|MCP-1st-Birthday"
    r"|enlatics/Enlatics_benchmarking|lauspectrum/\S*gaia|Intelligent-Internet/\S*gaia"
    r"|https?://\S*gaia\S*(?:jsonl|validation|metadata|benchmark)",
    re.I,
)


def web_lookups(logs_dir: Optional[Path], task_ids: List[str],
                task_of: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
    """Ledger evidence that a run went looking for the answers on the web.

    Reports, never refuses: a mirror URL can appear in an honest search result,
    and a task id can be quoted by the harness itself. It names the ledger by
    its task directory, the task it belongs to (`task_of` maps one to the
    other), and what matched; a reader decides. An attachment is named
    `<task_id>.<ext>`, so that spelling is not a lookup and is skipped.
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
        rel = f.relative_to(logs_dir).as_posix()
        where = {"file": rel, "task": (task_of or {}).get(rel.split("/", 1)[0])}
        mirror = _MIRROR_RE.search(text)
        searched = next((t for t in ids
                         if re.search(re.escape(t) + r"(?!\.[A-Za-z0-9]{1,5}\b)", text)), None)
        if mirror:
            hits.append({**where, "marker": mirror.group(0), "kind": "mirror"})
        if searched:
            hits.append({**where, "marker": searched, "kind": "task_id_in_trace"})
    return hits


BENCH_PROFILE = "gaia_benchmark"
BENCH_RULES = HERE / "benchmark_rules.json"


def benchmark_firewall(workdir: Path):
    """The rules a benchmark agent runs under, owned by the run.

    Not the operator's `~/.dpc/privacy_rules.json`: that file grants eight
    profiles read of this repository, it is edited between runs, and loading it
    would let a score depend on one machine's personal configuration. The copy
    lands in the workdir because the firewall reconciles tool keys against the
    registry and writes the result back. Which tools are on is the harness's
    allow list (`_harness/benchmark_tools.py`): a tool nobody listed is off.
    """
    rules = json.loads(BENCH_RULES.read_text(encoding="utf-8"))
    return benchmark_tools.benchmark_firewall(workdir, BENCH_PROFILE, template=rules)


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


_TOKEN_GUARD_VARS = ("HF_HUB_DISABLE_IMPLICIT_TOKEN", "HF_TOKEN_PATH")


def resolve_hf_token() -> Tuple[Optional[str], str]:
    """(token, where it came from). The value is never printed.

    The environment first, as before; then the token `hf auth login` stored,
    because an unattended run started from a shell that never exported one
    should not die in its first second on a machine that is logged in.
    """
    for var in _GATED_TOKEN_VARS:
        if os.environ.get(var):
            return os.environ[var], f"env:{var}"
    try:
        from huggingface_hub import get_token
        token = get_token()
    except Exception:
        token = None
    return (token, "huggingface_hub stored token") if token else (None, "none")


def fence_stored_token(workdir: Path) -> Dict[str, Optional[str]]:
    """Make the stored token invisible to anything the agent starts.

    Dropping the env vars alone meant little once the stored token was a
    fallback: `huggingface_hub` in any subprocess would read it from disk by
    itself. These two settings stop the library from sending it implicitly and
    point it at a file that does not exist. A script that opens the token file
    by path still reads it; that is the script-gate class, not this one.
    """
    previous = {var: os.environ.get(var) for var in _TOKEN_GUARD_VARS}
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_TOKEN_PATH"] = str(workdir / "no-hf-token")
    return previous


def restore_env(previous: Dict[str, Optional[str]]) -> None:
    for var, value in previous.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value


def fence_operator_interpreters() -> Dict[str, Optional[str]]:
    """Make pip refuse to install into any interpreter that is not a venv.

    The agent's `python` is the throwaway environment `uv run --with` builds and
    deletes afterwards; its `pip` is not — measured 2026-09-23 it resolves to the
    operator's system Python, which carries openpyxl, pypdf, pytesseract and
    curl_cffi that earlier runs installed. A plain `pip install` is Tier 0 and
    never reaches the approver, so the refusal has to live in pip itself.
    """
    previous = {"PIP_REQUIRE_VIRTUALENV": os.environ.get("PIP_REQUIRE_VIRTUALENV")}
    os.environ["PIP_REQUIRE_VIRTUALENV"] = "true"
    return previous


def provider_entry_for(alias: Optional[str], model: Optional[str], base_url: str,
                       context_window: int, temperature: Optional[float] = None,
                       reasoning_effort: Optional[str] = None) -> Dict[str, Any]:
    """The provider entry the eval will run against, resolved before any download.

    `--provider-alias` copies the named entry out of the operator's real
    `~/.dpc/providers.json` **verbatim**, so the run uses exactly the
    production configuration rather than a second copy of it that drifts.
    Nothing is written back to the operator's file. A missing alias exits here,
    before the gated dataset reaches the disk.
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
    elif not model:
        raise SystemExit("no provider: pass --provider-alias (the documented path) "
                         "or --model for an Ollama model")
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
    return entry


def write_providers_file(entry: Dict[str, Any], workdir: Path) -> Path:
    path = workdir / "providers.json"
    path.write_text(json.dumps({"providers": [entry], "default_provider": entry["alias"]}),
                    encoding="utf-8")
    return path


def providers_file_for(alias: str, model: Optional[str], base_url: str,
                       context_window: int, workdir: Path,
                       temperature: Optional[float] = None,
                       reasoning_effort: Optional[str] = None) -> tuple:
    """Resolve the entry and write the throwaway providers file, in one call."""
    entry = provider_entry_for(alias, model, base_url, context_window,
                               temperature=temperature, reasoning_effort=reasoning_effort)
    return write_providers_file(entry, workdir), entry


# --- the run ---------------------------------------------------------------

# The format rules of the GAIA paper's prompt, verbatim from the leaderboard's
# `content.py`. The scorer above keeps articles and units, so the rules that tell
# the model to drop them have to travel with it or it grades a different game.
PROMPT_SUFFIX = (
    "\n\nFinish your answer with the following template: FINAL ANSWER: [YOUR FINAL "
    "ANSWER]. YOUR FINAL ANSWER should be a number OR as few words as possible OR "
    "a comma separated list of numbers and/or strings. If you are asked for a "
    "number, don't use comma to write your number neither use units such as $ or "
    "percent sign unless specified otherwise. If you are asked for a string, don't "
    "use articles, neither abbreviations (e.g. for cities), and write the digits in "
    "plain text unless specified otherwise. If you are asked for a comma separated "
    "list, apply the above rules depending of whether the element to be put in the "
    "list is a number or a string."
)


ANSWER_KEEP_CHARS = 600
DEFAULT_TASK_TIMEOUT_SECONDS = 2700
# The key under which `run_one` hands the untruncated answer to the caller. The
# caller pops it before the row is written: the scans need the whole text, the
# report needs only the ends of it.
FULL_ANSWER_KEY = "_answer_full"


async def run_one(agent, row: Dict[str, Any], attachment: Optional[Path],
                  timeout_seconds: Optional[float] = DEFAULT_TASK_TIMEOUT_SECONDS
                  ) -> Dict[str, Any]:
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
        # Bounded, because nobody is watching: one task that never returns
        # would otherwise hold the whole night.
        answer = await asyncio.wait_for(agent.process(
            message=question + PROMPT_SUFFIX,
            conversation_id=conversation_id,
        ), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        error = f"TimeoutError: no answer within {timeout_seconds:.0f}s"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    # The loop accumulates usage and `process()` returns only text, so the
    # counters live on the agent afterwards. Absent stays absent: a task whose
    # provider reported nothing records `null`, not a confident zero.
    usage = dict(getattr(agent, "_last_usage", None) or {})
    answer = answer or ""
    return {
        "task_id": row["task_id"],
        "conversation_id": conversation_id,
        "gold_sha256": gold_fingerprint(row["Final answer"]),
        # The head alone lost the verdict: FINAL ANSWER sits at the end, so the
        # graded span and the tail are kept beside the first 600 characters.
        "final_answer": extract_final_answer(answer),
        "answer": answer[:ANSWER_KEEP_CHARS],
        "answer_tail": answer[-ANSWER_KEEP_CHARS:] if len(answer) > ANSWER_KEEP_CHARS else "",
        "answer_chars": len(answer),
        FULL_ANSWER_KEY: answer,
        "correct": scores_as_correct(answer, row["Final answer"]),
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
    token, token_source = resolve_hf_token()
    if not token:
        raise SystemExit("no Hugging Face token: export HF_TOKEN or run `hf auth login` "
                         "once — the GAIA split is a gated dataset.")
    _DATASET_STATE["token_source"] = token_source

    from dpc_client_core.llm_manager import LLMManager
    from dpc_client_core.dpc_agent.agent import DpcAgent, AgentConfig

    # The alias is resolved before anything is downloaded: a missing one used to
    # surface only after the gated split had reached the disk.
    entry = provider_entry_for(
        args.provider_alias, args.model, args.base_url, args.context_window,
        temperature=args.temperature, reasoning_effort=args.reasoning_effort,
    )
    model_record = provenance.model_files(entry, RESULTS_DIR)
    max_rounds = getattr(args, "max_rounds", None)
    task_timeout = getattr(args, "task_timeout", None) or DEFAULT_TASK_TIMEOUT_SECONDS

    def _agent_config():
        return AgentConfig(max_rounds=max_rounds) if max_rounds else AgentConfig()

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
    canary_files: List[Path] = []
    hub_before = None
    llm = None
    approver = None
    env_before: Dict[str, Optional[str]] = {}
    # The gold lands on disk inside the setup below, and this guard used to open
    # after it: the night of 2026-08-30 03:12 died on a stale token four times
    # between the two and left a hub full of answers in the temp directory each
    # time. Everything the run creates is now removed on every path out.
    try:
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
        hub_before = None
        _DATASET_STATE["token_dropped"] = drop_gated_credentials()
        token = None
        env_before.update(fence_stored_token(workdir))
        env_before.update(fence_operator_interpreters())
        print(f"attachments prefetched: {len(prefetched)}; hub cache removed: "
              f"{_DATASET_STATE['private_cache_removed']}", flush=True)

        providers_path = write_providers_file(entry, workdir)
        print(f"provider: alias {entry['alias']!r} type={entry.get('type')} "
              f"model file={_model_identity(entry, model_record)}", flush=True)

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
        if args.auto_approve:
            from _harness.auto_approve import Tier1AutoApprover
            approver = Tier1AutoApprover().start()
            print("Tier 1 auto-approval ON (Tier 2 still blocked)", flush=True)

        results = []
        # The whole answer stays in memory for the scans below and never reaches
        # the report, which keeps only its ends (`run_one`).
        full_answers: List[Dict[str, Any]] = []
        task_of: Dict[str, str] = {}
        agent_config = _agent_config()
        started = time.time()
        for i, row in enumerate(rows, 1):
            # A root per task, so nothing an agent writes reaches the next one:
            # scratchpad, knowledge, logs and task_results all start empty.
            task_root = workdir / f"task-{i:03d}"
            task_of[task_root.name] = row["task_id"]
            (task_root / "gaia-files").mkdir(parents=True, exist_ok=True)
            attachment = prefetched.get(row["task_id"])
            if attachment is not None:
                attachment = Path(shutil.copy2(attachment, task_root / "gaia-files"))
            agent = DpcAgent(
                llm_manager=llm, config=_agent_config(), agent_root=task_root,
                firewall=firewall, firewall_profile=BENCH_PROFILE,
            )
            outcome = await run_one(agent, row, attachment, timeout_seconds=task_timeout)
            full = outcome.pop(FULL_ANSWER_KEY, None)
            full_answers.append({"task_id": outcome.get("task_id"),
                                 "answer": full if full is not None else outcome.get("answer") or "",
                                 "correct": outcome.get("correct")})
            if (task_root / "logs").is_dir():
                shutil.copytree(task_root / "logs", logs_root / task_root.name,
                                dirs_exist_ok=True)
            results.append(outcome)
            mark = "OK  " if outcome["correct"] else "MISS"
            got = outcome.get("final_answer") or ("<no FINAL ANSWER> " + (outcome.get("answer") or "")[-40:])
            # flush: redirected stdout is block-buffered, so a run watched through
            # a log file showed zero completed tasks for over an hour while the
            # agent was demonstrably on its third. The progress line is the only
            # window into a run that takes hours; it has to reach the file.
            print(f"  [{i:2}/{len(rows)}] {mark} {outcome['seconds']:6.1f}s  "
                  f"task={outcome['task_id'][:8]} got={got[:80].strip()!r}",
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
            "alias": entry.get("alias"),
            # The `model` field is a free label and has been stale (an alias
            # renamed, the label kept). The model is the file and its digest.
            "model_label": entry.get("model"),
            "model_file": _model_identity(entry, model_record),
            "provider_type": entry.get("type"),
            "temperature": entry.get("temperature"),
            "reasoning_effort": provenance.effort_record(entry),
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
                **canary_was_read(canary_token, full_answers, logs_root),
                "planted": [str(p) for p in canary_files],
            },
            # What the canary structurally cannot see: an answer that names its
            # own source. Four such tasks are already on the board for 2026-08-28
            # and two more were found on 08-30 in runs nobody had re-read.
            "admissions": answers_admitting_a_lookup(full_answers),
            # The two surfaces that report rather than refuse: answers reachable
            # in the run's own ledgers, and the open web, where nothing can be
            # planted. Turning either into a refusal stops the campaign for every
            # ledger already on disk, which is a decision, not a fix.
            # This run's own ledger, before it is copied anywhere: scanning the
            # results tree named earlier nights and could never name this one.
            "traces_carrying_gold": [p.relative_to(logs_root).as_posix()
                                     for p in gold_in_traces(logs_root)],
            "web_lookups": web_lookups(logs_root, [r.get("task_id") for r in results],
                                       task_of=task_of),
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
                    "scoring": {**OFFICIAL_SCORER,
                                "prompt": "the paper's format rules, appended to each question",
                                "prompt_suffix_sha256": hashlib.sha256(
                                    PROMPT_SUFFIX.encode("utf-8")).hexdigest()},
                    "model_files": model_record,
                    "max_rounds": getattr(agent_config, "max_rounds", None),
                    "task_timeout_seconds": task_timeout,
                    "hf_token_source": _DATASET_STATE.get("token_source"),
                    "subprocess_env_fences": sorted(env_before),
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
        print(f"{correct}/{len(results)} = {report['accuracy']:.1%} on alias "
              f"{entry.get('alias')!r} ({report['model_file']}) "
              f"in {report['seconds']}s | {tok['total']} tokens over {tok['reported_by']} task(s)",
              flush=True)
        # The report's own `caveat` field has said the general thing since it was
        # written; this line named one project and one number, and our own best
        # draw is 37/53 — which *is* 69.8 %, so it read as a denial of the very
        # parity it was printed under. Say what the number can be compared with.
        print("One draw of 53 tasks. Comparable with runs of this harness at the same "
              "tree, alias and effort, and with nothing else: two greedy runs of one "
              "configuration have disagreed on 14 of the 53. No published figure means "
              "the same thing unless model, quantisation, context window, step budget "
              "and memory configuration all match.")
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

        # A triggered canary means the agent read a planted answer key, so the
        # number above is not a score. It used to leave by the same door as a
        # clean run, which made the one signal that says «do not cite this»
        # invisible to the campaign, to CI and to anything reading the status.
        return CONTAMINATED_EXIT if report["canary"]["triggered"] else 0
    finally:
        if approver is not None:
            approver.stop()
        restore_env(env_before)
        for bait in canary_files:
            bait.unlink(missing_ok=True)
        if hub_before is not None:
            restore_hub(hub_before)
        # Without this the llama-server the provider spawned outlives the run,
        # and the campaign's own GPU gate then waits for a child of the run
        # before it: 0 of 4 runs started on 2026-08-25, 1 of 4 on 2026-08-27.
        if llm is not None:
            try:
                await llm.shutdown()
            except Exception as exc:
                print(f"warning: provider shutdown failed: {exc}", flush=True)
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

def _model_identity(entry: Dict[str, Any], record: Dict[str, Any]) -> str:
    """One line naming the model by its file and digest, else by its label."""
    gguf = record.get("gguf") if isinstance(record, dict) else None
    if isinstance(gguf, dict) and gguf.get("path"):
        sha = gguf.get("sha256") or "sha256 unread"
        return f"{Path(gguf['path']).name} sha256:{sha[:16]}"
    return f"{entry.get('model')} (label only; no model file recorded)"


def main() -> int:
    # Model answers carry arrows, dashes and non-Latin text; a Windows console
    # defaults to cp1252 and a run that finished would die on printing it.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--provider-alias", default=DEFAULT_ALIAS,
                    help="run the eval on this alias from ~/.dpc/providers.json, verbatim "
                         f"(default {DEFAULT_ALIAS!r}, the local llama-server)")
    ap.add_argument("--model", default=None,
                    help="an Ollama model instead of the alias; nothing falls back to it")
    ap.add_argument("--base-url", default="http://127.0.0.1:11434")
    ap.add_argument("--context-window", type=int, default=32768,
                    help="Ollama only; an alias carries its own")
    ap.add_argument("--max-rounds", type=int, default=None,
                    help="agent round limit (default: AgentConfig's)")
    ap.add_argument("--task-timeout", type=float, default=DEFAULT_TASK_TIMEOUT_SECONDS,
                    help="seconds one task may take before it is recorded as a timeout")
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
    args = ap.parse_args()
    if args.model:
        args.provider_alias = None
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
