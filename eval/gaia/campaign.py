"""Run a planned queue of GAIA runs overnight, and stop before the deadline.

Why a queue rather than a sweep. The obvious idea — one run each at 1, 0.7,
0.3 and 0 — spends the night answering «how does temperature move the score»,
and it cannot answer it: measured on this box, the same model on the same task
answered `17` in one run and `8` in the next, both at temperature 1. With one
sample per point, the gaps between points are that noise. Four points, no
statistics.

What the same hours buy instead:

- **one run at temperature 0** — greedy decoding, the reproducible reference.
  This is what an agent-benchmark paper reports when it reports one number,
  because a second run of it gives the same number.
- **repeats at the production temperature** — a mean and a spread for what a
  user actually gets, which is a different and equally real question.

`pass@k` and majority voting are the third standard shape and are deliberately
not here: both need many samples per task, and at ~3 minutes a task that is a
different night's work.

Every run pins **both** axes. Reasoning effort left alone is not «default», it
is absent — the provider sends no word and the model's template answers with
its own, `xhigh` for this one. An unrecorded default is a guess wearing the
clothes of a setting.

The queue waits for the GPU to be free before each run, so it can be started
while something else is still finishing, and it refuses to start a run that
cannot finish before the deadline.

The one command, from `dpc-client/core`, with the DPC service stopped (it
holds the model through its own llama-server child, and the card has room for
one):

    cd dpc-client/core
    uv run --with pyarrow python ../../eval/gaia/campaign.py --dry-run
    uv run --with pyarrow python ../../eval/gaia/campaign.py --hours 7.5

`--dry-run` checks everything a night depends on — the alias, the pinned
llama-server binary, the GGUF, the token, the card, the results directory, the
tool set — and downloads nothing and loads no model. The real start runs the
same checks and refuses before the first run if any of them fails. The token
is `HF_TOKEN` or, when that is unset, the one `hf auth login` stored.

A run needs about 170 minutes, so 7.5 hours starts two of the four and says so
for the rest. The queue's order is what makes a short night still worth having.

Exit codes: 0 every started run scored clean; 1 a run failed or timed out;
2 the preflight refused; 3 a run was contaminated (the canary was read, or a
correct answer reached a published answer key); 4 the card never came free
or nothing could start before the deadline.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from _harness.results_root import results_root  # noqa: E402

RESULTS = results_root("gaia")
RUNNER = HERE / "run_gaia_eval.py"
# The local llama-server alias. An alias the providers file does not hold stops
# the campaign in the preflight, before any download — it used to be a
# hardcoded name that had been renamed, and every run would have died on it.
DEFAULT_ALIAS = "qwen3.8 27b"
FAILED_EXIT = 1
PREFLIGHT_EXIT = 2
NO_CARD_EXIT = 4
# A run that outlives this is killed with its whole tree (uv, the runner and
# the llama-server it started), so an unattended night cannot hang on one run.
DEFAULT_RUN_TIMEOUT_MIN = 240
TIMED_OUT = -9
# The runner's own exit for «the agent read a planted answer key», and since
# 2026-09-23 for «a correct answer reached a published one». Named here rather
# than folded into the generic failure branch: the run did not fail, it
# produced a number that must not be counted.
CONTAMINATED_EXIT = 3


def stops_the_queue(record: dict) -> bool:
    """A run that died before it started tells the rest of the queue nothing.

    Contamination is not that: the run worked, its number is simply not a
    score, and the next configuration is no more doomed than before. Folding
    exit 3 into «failed fast» said the opposite.
    """
    return (
        record["exit_code"] not in (0, CONTAMINATED_EXIT)
        and record["minutes"] < FAST_FAILURE_MINUTES
    )
CORE = HERE.parent.parent / "dpc-client" / "core"

# Order matters: the reference number first, so that if the night is cut short
# the thing we keep is the one that reproduces.
QUEUE = [
    {"name": "t0-xhigh", "temperature": 0.0, "reasoning_effort": "xhigh",
     "why": "greedy reference — a second run of this returns the same number"},
    {"name": "t0-low", "temperature": 0.0, "reasoning_effort": "low",
     "why": "the effort question, asked where the noise is smallest: this pair "
            "differs in one word and both runs are greedy"},
    {"name": "t1-xhigh", "temperature": 1.0, "reasoning_effort": "xhigh",
     "why": "the production setting, one draw from a spread measured at 9.4 points"},
    {"name": "t1-low", "temperature": 1.0, "reasoning_effort": "low",
     "why": "the same question at the production temperature — a second reading, "
            "weaker than the greedy pair and not a substitute for it"},
]

# What one run actually occupies: the 16 GB model plus its KV cache. The gate
# used to ask whether the card was *idle* (`used < 6000`), which is a different
# question and one the resident server can never answer yes to — so a run could
# deadlock the queue behind itself even when it leaked nothing.
GPU_NEEDED_MIB = 26000
POLL_SECONDS = 60
REPORT_EVERY = 5            # polls; one line a minute for eight hours is not a report
DEFAULT_WAIT_BUDGET_MIN = 30
# Names worth naming when the card is held. Windows does not attribute VRAM per
# process under WDDM — `nvidia-smi --query-compute-apps` returns `[N/A]` for
# every used_memory here — so these are candidates, never proof.
_COMPUTE_NAMES = ("llama-server", "python", "ollama", "camoufox")


def gpu_free_mib():
    """Free VRAM in MiB, or None where there is no nvidia-smi to ask.

    Three answers, not two. A number is a reading. `0` means the tool ran and
    the reading did not come back, and unknown is not "plenty" — refusing to
    start is the safe half. `None` means there is no NVIDIA card to contend
    for at all (a Mac, an AMD box), where a gate on its VRAM guards nothing
    and must not become a wall.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return None
    except Exception:
        return 0
    try:
        return int(out.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def gpu_holder_candidates() -> list:
    """Compute processes on the card, by pid and name. Never a MiB per process."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return []
    holders = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",", 1)]
        if len(parts) != 2:
            continue
        pid, name = parts
        if any(w in name.lower() for w in _COMPUTE_NAMES):
            base = name.replace("\\", "/").rsplit("/", 1)[-1]
            holders.append(f"{base} (pid {pid})")
    return holders


def wait_for_gpu(deadline: datetime, budget_minutes: float = DEFAULT_WAIT_BUDGET_MIN) -> bool:
    """True when the card has room for a run.

    Two things the previous version did not do. It says **what** is holding the
    card rather than repeating the same number — the 2026-08-25 log carries 598
    identical `waiting for the GPU (28 848 MiB in use)` lines and names nothing.
    And it gives up after a budget instead of spending the night: a card that is
    not free in half an hour is not going to give a clean measurement anyway.

    It does not reclaim anything. Killing a process that might be a colleague's
    is a decision for a person — the one orphan killed by hand on 2026-08-27
    took three checks first (parent dead, nothing connected, sole instance).
    """
    started = datetime.now()
    give_up_at = started + timedelta(minutes=budget_minutes)
    polls = 0
    while datetime.now() < deadline and datetime.now() < give_up_at:
        free = gpu_free_mib()
        if free is None:
            print("  no nvidia-smi on this machine: the VRAM gate does not apply",
                  flush=True)
            return True
        if free >= GPU_NEEDED_MIB:
            return True
        if polls % REPORT_EVERY == 0:
            waited = (datetime.now() - started).total_seconds() / 60
            holders = gpu_holder_candidates()
            who = ", ".join(holders) if holders else "no compute process named it"
            print(f"  waiting for the GPU: {free} MiB free, {GPU_NEEDED_MIB} needed, "
                  f"{waited:.0f} min so far — held by: {who}. Stop the DPC service "
                  f"(or unload its model): it holds the card through its own "
                  f"llama-server child", flush=True)
        polls += 1
        time.sleep(POLL_SECONDS)
    waited = (datetime.now() - started).total_seconds() / 60
    if datetime.now() >= give_up_at:
        print(f"  giving up on the GPU after {waited:.0f} min "
              f"({gpu_free_mib()} MiB free, {GPU_NEEDED_MIB} needed)", flush=True)

    return False


def runner_command(cfg: dict, out_json: Path, settings: dict) -> list:
    cmd = [
        "uv", "run", "--with", "pyarrow", "python", str(RUNNER),
        "--provider-alias", settings["alias"],
        "--with-files", "--auto-approve",
        "--temperature", str(cfg["temperature"]),
        "--reasoning-effort", cfg["reasoning_effort"],
        "--json", str(out_json),
    ]
    if settings.get("limit"):
        cmd += ["--limit", str(settings["limit"])]
    if settings.get("no_memory"):
        cmd += ["--no-memory"]
    return cmd


def _kill_tree(proc: subprocess.Popen) -> None:
    """The runner's children too: a killed uv alone leaves llama-server on the card."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=60)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception as exc:
        print(f"  warning: could not kill run tree {proc.pid}: {exc}", flush=True)
    try:
        proc.wait(timeout=60)
    except Exception:
        pass


def run_one(cfg: dict, deadline: datetime, stamp: str, settings: dict | None = None) -> dict:
    settings = settings or {"alias": DEFAULT_ALIAS}
    out_json = RESULTS / f"{stamp}-{cfg['name']}.json"
    out_log = RESULTS / f"{stamp}-{cfg['name']}.log"
    cmd = runner_command(cfg, out_json, settings)
    timeout_min = settings.get("run_timeout_minutes") or DEFAULT_RUN_TIMEOUT_MIN
    started = datetime.now()
    print(f"[{started:%H:%M:%S}] {cfg['name']}: {cfg['why']}", flush=True)
    timed_out = False
    with open(out_log, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, cwd=str(CORE), stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=(sys.platform != "win32"))
        try:
            returncode = proc.wait(timeout=timeout_min * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            returncode = TIMED_OUT
    elapsed = (datetime.now() - started).total_seconds() / 60
    record = {
        "name": cfg["name"], "temperature": cfg["temperature"],
        "reasoning_effort": cfg["reasoning_effort"], "minutes": round(elapsed, 1),
        "exit_code": returncode, "json": str(out_json), "alias": settings["alias"],
    }
    if timed_out:
        record["timed_out_after_minutes"] = timeout_min
    if out_json.exists():
        try:
            record.update(report_fields(json.loads(out_json.read_text(encoding="utf-8"))))
        except Exception as exc:
            record["read_error"] = str(exc)
    if returncode == CONTAMINATED_EXIT:
        record["contaminated"] = True
        print(f"  -> CONTAMINATED: {contamination_reason(record)}, so "
              f"{record.get('correct')}/{record.get('tasks')} is not a score; "
              f"{_clean(record)} ({record['minutes']} min) — {out_log}", flush=True)
    elif returncode == 0:
        print(f"  -> {record.get('correct')}/{record.get('tasks')} "
              f"= {record.get('accuracy')}, {_clean(record)} in {record['minutes']} min",
              flush=True)
    else:
        why = (f"timed out after {timeout_min:.0f} min, tree killed" if timed_out
               else f"exit {returncode}")
        print(f"  -> FAILED ({why}) after {record['minutes']} min "
              f"— {out_log}", flush=True)
        for line in _log_tail(out_log):
            print(f"     {line}", flush=True)
    return record


def report_fields(report: dict) -> dict:
    """What the night's summary carries from one run's report."""
    exposure = report.get("answer_key_exposure") or {}
    return {
        "accuracy": report.get("accuracy"),
        "correct": report.get("correct"),
        "tasks": report.get("tasks"),
        "correct_clean": report.get("correct_clean"),
        "accuracy_clean": report.get("accuracy_clean"),
        "canary_triggered": bool((report.get("canary") or {}).get("triggered")),
        "copied": [t[:8] for t in exposure.get("copied") or []],
        "exposed_tasks": exposure.get("exposed_tasks"),
        "policy_refusals": (report.get("answer_key_policy") or {}).get("refusals"),
    }


def contamination_reason(record: dict) -> str:
    reasons = []
    if record.get("canary_triggered"):
        reasons.append("the canary was read")
    if record.get("copied"):
        reasons.append(f"{len(record['copied'])} correct answer(s) reached an answer key "
                       f"({', '.join(record['copied'])})")
    return "; ".join(reasons) or "the run exited 3 (report unread)"


def _clean(record: dict) -> str:
    if record.get("correct_clean") is None:
        return "clean score not in the report"
    return f"clean {record['correct_clean']}/{record.get('tasks')} = {record.get('accuracy_clean')}"


def _log_tail(path: Path, lines: int = 3) -> list:
    """The last non-empty lines of a run's log, for the operator's screen.

    A failed run used to read `-> None/None = None in 0.1 min`, with the cause
    in a file nobody opens until morning.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()][-lines:]


# A run that dies in the first minutes died of its configuration — a token, a
# missing model, a path — and every other run in the queue carries the same
# configuration. Stopping is what keeps a typo from reading as a night's work.
FAST_FAILURE_MINUTES = 3.0


def campaign_exit(done: list, card_never_free: bool) -> int:
    """One status for the whole night, worst first: a caller reads only this."""
    codes = [r["exit_code"] for r in done]
    if any(c not in (0, CONTAMINATED_EXIT) for c in codes):
        return FAILED_EXIT
    if CONTAMINATED_EXIT in codes:
        return CONTAMINATED_EXIT
    if card_never_free or not done:
        return NO_CARD_EXIT
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=7.5,
                    help="stop starting new runs this long from now")
    ap.add_argument("--minutes-per-run", type=float, default=170,
                    help="a run is not started unless this much time is left")
    ap.add_argument("--wait-budget-minutes", type=float, default=DEFAULT_WAIT_BUDGET_MIN,
                    help="stop waiting for the card after this long and say so")
    ap.add_argument("--alias", default=DEFAULT_ALIAS,
                    help="provider alias in ~/.dpc/providers.json (a llamacpp_server entry)")
    ap.add_argument("--limit", type=int, default=None,
                    help="tasks per run, for a short smoke campaign (default: all 53)")
    ap.add_argument("--run-timeout-minutes", type=float, default=DEFAULT_RUN_TIMEOUT_MIN,
                    help="kill a run and its children after this long")
    ap.add_argument("--dry-run", action="store_true",
                    help="run every preflight check and stop: no download, no model load")
    ap.add_argument("--no-memory", action="store_true",
                    help="disable the agent's memory_search tool for every run in the "
                         "queue and skip the embedding-model preflight check, rather than "
                         "refuse when the model is not cached; passed through to each run")
    args = ap.parse_args()

    # Beside this file; it imports the client, so it is not loaded at import.
    import preflight

    checks = preflight.run_checks(args.alias, [c["reasoning_effort"] for c in QUEUE],
                                  RESULTS, GPU_NEEDED_MIB, no_memory=args.no_memory)
    preflight.print_checks(checks)
    if args.dry_run:
        print("\ndry run: nothing downloaded, no model loaded, no run started.", flush=True)
        return 0 if preflight.all_ok(checks) else PREFLIGHT_EXIT
    if not preflight.all_ok(checks):
        print("\n=== campaign === not started: the preflight refused (FAIL above)",
              flush=True)
        return PREFLIGHT_EXIT

    settings = {"alias": args.alias, "limit": args.limit,
                "run_timeout_minutes": args.run_timeout_minutes,
                "no_memory": args.no_memory}
    RESULTS.mkdir(parents=True, exist_ok=True)
    deadline = datetime.now() + timedelta(hours=args.hours)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    print(f"campaign until {deadline:%H:%M:%S}, {len(QUEUE)} run(s) queued, "
          f"alias {args.alias!r}", flush=True)

    done = []
    card_never_free = False
    for cfg in QUEUE:
        left = (deadline - datetime.now()).total_seconds() / 60
        if left < args.minutes_per_run:
            print(f"skipping {cfg['name']}: {left:.0f} min left, "
                  f"a run needs about {args.minutes_per_run:.0f}", flush=True)
            continue
        if not wait_for_gpu(deadline, args.wait_budget_minutes):
            card_never_free = True
            print("not starting the rest of the queue: the card never came free",
                  flush=True)
            break
        record = run_one(cfg, deadline, stamp, settings)
        done.append(record)
        summary = RESULTS / f"{stamp}-campaign.json"
        summary.write_text(json.dumps({"runs": done}, indent=2), encoding="utf-8")
        if stops_the_queue(record):
            print(f"\nstopping the queue: {cfg['name']} failed in "
                  f"{record['minutes']} min, so the rest would fail the same way. "
                  f"Fix what the lines above name and start the campaign again.",
                  flush=True)
            break

    print("\n=== campaign ===", flush=True)
    for r in done:
        if r["exit_code"] == CONTAMINATED_EXIT:
            outcome = (f"CONTAMINATED ({r.get('correct')}/{r.get('tasks')} reported, not a "
                       f"score; {_clean(r)}; {contamination_reason(r)})")
        elif r["exit_code"] == 0:
            outcome = f"{r.get('correct')}/{r.get('tasks')} = {r.get('accuracy')}, {_clean(r)}"
        elif r["exit_code"] == TIMED_OUT:
            outcome = f"FAILED (timed out, killed after {r.get('timed_out_after_minutes')} min)"
        else:
            outcome = f"FAILED (exit {r['exit_code']})"
        print(f"  {r['name']:12} t={r['temperature']} effort={r['reasoning_effort']:6} "
              f"{outcome} ({r['minutes']} min)", flush=True)
    code = campaign_exit(done, card_never_free)
    print(f"campaign exit {code}: {len(done)} of {len(QUEUE)} run(s) started, "
          f"alias {args.alias!r}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
