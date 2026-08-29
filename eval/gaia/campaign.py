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

Run it from `dpc-client/core` — there is no project at the repository root for
`uv run` to resolve, and the runs it launches are started there anyway. The
campaign itself imports nothing outside the standard library; `--with pyarrow`
belongs to the child command and is already in it.

    cd dpc-client/core
    HF_TOKEN=... uv run python ../../eval/gaia/campaign.py --hours 7.5

A run needs about 170 minutes, so 7.5 hours starts two of the four and says so
for the rest. The queue's order is what makes a short night still worth having.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
RUNNER = HERE / "run_gaia_eval.py"
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
                  f"{waited:.0f} min so far — held by: {who}", flush=True)
        polls += 1
        time.sleep(POLL_SECONDS)
    waited = (datetime.now() - started).total_seconds() / 60
    if datetime.now() >= give_up_at:
        print(f"  giving up on the GPU after {waited:.0f} min "
              f"({gpu_free_mib()} MiB free, {GPU_NEEDED_MIB} needed)", flush=True)

    return False


def run_one(cfg: dict, deadline: datetime, stamp: str) -> dict:
    out_json = RESULTS / f"{stamp}-{cfg['name']}.json"
    out_log = RESULTS / f"{stamp}-{cfg['name']}.log"
    cmd = [
        "uv", "run", "--with", "pyarrow", "python", str(RUNNER),
        "--provider-alias", "qwen3.8 27b Mythos",
        "--with-files", "--auto-approve",
        "--temperature", str(cfg["temperature"]),
        "--reasoning-effort", cfg["reasoning_effort"],
        "--json", str(out_json),
    ]
    started = datetime.now()
    print(f"[{started:%H:%M:%S}] {cfg['name']}: {cfg['why']}", flush=True)
    with open(out_log, "w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, cwd=str(CORE), stdout=log, stderr=subprocess.STDOUT)
    elapsed = (datetime.now() - started).total_seconds() / 60
    record = {
        "name": cfg["name"], "temperature": cfg["temperature"],
        "reasoning_effort": cfg["reasoning_effort"], "minutes": round(elapsed, 1),
        "exit_code": proc.returncode, "json": str(out_json),
    }
    if out_json.exists():
        try:
            report = json.loads(out_json.read_text(encoding="utf-8"))
            record["accuracy"] = report.get("accuracy")
            record["correct"] = report.get("correct")
            record["tasks"] = report.get("tasks")
        except Exception as exc:
            record["read_error"] = str(exc)
    if proc.returncode == 0:
        print(f"  -> {record.get('correct')}/{record.get('tasks')} "
              f"= {record.get('accuracy')} in {record['minutes']} min", flush=True)
    else:
        print(f"  -> FAILED (exit {proc.returncode}) after {record['minutes']} min "
              f"— {out_log}", flush=True)
        for line in _log_tail(out_log):
            print(f"     {line}", flush=True)
    return record


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


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=7.5,
                    help="stop starting new runs this long from now")
    ap.add_argument("--minutes-per-run", type=float, default=170,
                    help="a run is not started unless this much time is left")
    ap.add_argument("--wait-budget-minutes", type=float, default=DEFAULT_WAIT_BUDGET_MIN,
                    help="stop waiting for the card after this long and say so")
    args = ap.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    deadline = datetime.now() + timedelta(hours=args.hours)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    print(f"campaign until {deadline:%H:%M:%S}, {len(QUEUE)} run(s) queued", flush=True)

    done = []
    for cfg in QUEUE:
        left = (deadline - datetime.now()).total_seconds() / 60
        if left < args.minutes_per_run:
            print(f"skipping {cfg['name']}: {left:.0f} min left, "
                  f"a run needs about {args.minutes_per_run:.0f}", flush=True)
            continue
        if not wait_for_gpu(deadline, args.wait_budget_minutes):
            print("not starting the rest of the queue: the card never came free",
                  flush=True)
            break
        record = run_one(cfg, deadline, stamp)
        done.append(record)
        summary = RESULTS / f"{stamp}-campaign.json"
        summary.write_text(json.dumps({"runs": done}, indent=2), encoding="utf-8")
        if record["exit_code"] != 0 and record["minutes"] < FAST_FAILURE_MINUTES:
            print(f"\nstopping the queue: {cfg['name']} failed in "
                  f"{record['minutes']} min, so the rest would fail the same way. "
                  f"Fix what the lines above name and start the campaign again.",
                  flush=True)
            break

    print("\n=== campaign ===", flush=True)
    for r in done:
        outcome = (f"{r.get('correct')}/{r.get('tasks')} = {r.get('accuracy')}"
                   if r["exit_code"] == 0 else f"FAILED (exit {r['exit_code']})")
        print(f"  {r['name']:12} t={r['temperature']} effort={r['reasoning_effort']:6} "
              f"{outcome} ({r['minutes']} min)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
