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
    {"name": "t1-xhigh-b", "temperature": 1.0, "reasoning_effort": "xhigh",
     "why": "second sample at the production setting, for a spread"},
    {"name": "t1-xhigh-c", "temperature": 1.0, "reasoning_effort": "xhigh",
     "why": "third sample — three points is the least that says anything"},
    {"name": "t0-high", "temperature": 0.0, "reasoning_effort": "high",
     "why": "does less thinking cost accuracy, measured against the greedy reference"},
]

GPU_FREE_MIB = 6000
POLL_SECONDS = 60


def gpu_used_mib() -> int:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30,
        )
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return 0


def wait_for_gpu(deadline: datetime) -> bool:
    """True when the GPU looks free; False if the deadline arrived first."""
    while datetime.now() < deadline:
        used = gpu_used_mib()
        if used < GPU_FREE_MIB:
            return True
        print(f"  waiting for the GPU ({used} MiB in use)…", flush=True)
        time.sleep(POLL_SECONDS)
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
    print(f"  -> {record.get('correct')}/{record.get('tasks')} "
          f"= {record.get('accuracy')} in {record['minutes']} min", flush=True)
    return record


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
        if not wait_for_gpu(deadline):
            print("deadline reached while waiting for the GPU", flush=True)
            break
        done.append(run_one(cfg, deadline, stamp))
        summary = RESULTS / f"{stamp}-campaign.json"
        summary.write_text(json.dumps({"runs": done}, indent=2), encoding="utf-8")

    print("\n=== campaign ===", flush=True)
    for r in done:
        print(f"  {r['name']:12} t={r['temperature']} effort={r['reasoning_effort']:6} "
              f"{r.get('correct')}/{r.get('tasks')} = {r.get('accuracy')} "
              f"({r['minutes']} min)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
