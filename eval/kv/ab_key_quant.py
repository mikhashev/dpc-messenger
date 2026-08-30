"""K at q8_0 against K at q4_0, on the production model, at three depths.

Production quantises both halves of the KV cache to q4_0. The one public
measurement behind the "keys precise, values cheap" advice (llama.cpp #23470,
500 deterministic ARC-Challenge items) says q4_0 on K alone reproduces the full
collapse while q4_0 on V alone changes one item in 500 — so the half we cannot
see is the half that matters. Our only counter-evidence is Probe D of
2026-08-19: one retrieval question at 233 K where q4_0, q8_0 and f16 all
answered 7/7 identically. One question, one seed, and retrieval is the shape
least able to show a distorted attention score.

This runs the comparison the board entry asks for
(THE-KEYS-RUN-AT-Q4-0-AND-THE-ONE-CHECK-WAS-A-SINGLE-RETRIEVAL-QUESTION):

  * V stays q4_0 in both arms, K is the only variable;
  * temperature 0 and a fixed seed, so a difference is the cache and not the
    sampler;
  * three depths — 32 K, 120 K, 175 K — because the suspicion is accumulation;
  * needles carrying numbers and code, which fail loudly, rather than prose
    that can be paraphrased two ways and scored as agreement;
  * the measure is the rate of divergence against the other arm, not a score.

GAIA cannot answer this question: two greedy runs of one configuration differ
on 14 of 53 tasks, which is larger than any effect worth finding here.

It needs the card to itself. The production child holds 26-28 GiB, so run this
with the DPC service stopped — two 27B children on one 32 GiB card is the
incident of 2026-08-24, not an experiment.

    uv run python eval/kv/ab_key_quant.py --ctk q4_0
    uv run python eval/kv/ab_key_quant.py --ctk q8_0
    uv run python eval/kv/ab_key_quant.py --compare

Each arm writes eval/kv/results/<ctk>.json; --compare reads both and prints the
divergence table. Roughly 12 minutes per arm on this box: two model loads plus
six prefills of 32-175 K at the measured 750-900 tok/s.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

# The production alias, read rather than retyped: this probe is worthless if it
# measures a different model or a different pin than the fleet runs.
PROVIDERS = Path.home() / ".dpc" / "providers.json"
ALIAS_TYPE = "llamacpp_server"

DEPTHS = (32_000, 120_000, 175_000)
SEED = 42
# Deterministic decoding. The vendor does not recommend greedy for a thinking
# model, and that caveat is real for a quality verdict — but this is a paired
# comparison, and without determinism the difference between the arms is the
# sampler.
TEMPERATURE = 0.0


def _alias() -> dict:
    with open(PROVIDERS, encoding="utf-8") as f:
        doc = json.load(f)
    for p in doc.get("providers", []):
        if p.get("type") == ALIAS_TYPE:
            return p
    raise SystemExit(f"no {ALIAS_TYPE} provider in {PROVIDERS}")


def _binary() -> Path:
    sys.path.insert(0, str(HERE.parents[1] / "dpc-client" / "core"))
    from dpc_client_core.managers.llama_server_fetcher import (  # noqa: E402
        LLAMA_CPP_TAG, install_root, server_binary_name,
    )

    path = install_root(LLAMA_CPP_TAG) / server_binary_name()
    if not path.exists():
        raise SystemExit(f"pinned binary missing: {path}")
    return path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _free_vram_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used,memory.total",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=False,
    )
    used, total = (int(x) for x in out.stdout.strip().splitlines()[0].split(","))
    return total - used


# --- the probe set ---------------------------------------------------------
#
# Filler is deterministic and content-free; the needles are what is scored.
# Numbers and code are used on purpose: a wrong digit is a wrong answer, while
# a paraphrased sentence is a scoring argument.

_NEEDLES = [
    ("the calibration constant for sensor {i}", "{v}",
     "What is the calibration constant for sensor {i}? Answer with the number alone."),
    ("checksum of batch {i}", "{h}",
     "What is the checksum of batch {i}? Answer with the hex string alone."),
    ("def route_{i}(x): return x * {v} + {w}", "{r}",
     "In this transcript there is a function route_{i}. What does route_{i}(7) return? "
     "Answer with the number alone."),
]


def _corpus(rng: random.Random, target_tokens: int) -> tuple[str, list[dict]]:
    """Filler with needles spread through it, and the questions that read them.

    Needles sit at 10 %, 50 % and 90 % of the depth: a cache defect that eats
    the oldest region and one that eats the middle look identical if every
    needle is at the end.
    """
    # ~4 chars per token is the estimate this repo already uses elsewhere; the
    # exact depth does not matter as long as both arms get the same text.
    target_chars = target_tokens * 4
    blocks: list[str] = []
    needles: list[dict] = []
    n_slots = 3
    for slot in range(n_slots):
        kind, ans_t, q_t = _NEEDLES[slot % len(_NEEDLES)]
        i = rng.randrange(1000, 9999)
        v = rng.randrange(100, 999)
        w = rng.randrange(10, 99)
        h = hashlib.sha256(f"{i}:{v}:{w}".encode()).hexdigest()[:12]
        fmt = dict(i=i, v=v, w=w, h=h, r=7 * v + w)
        needles.append({
            "position": (slot + 1) / (n_slots + 1),
            "fact": kind.format(**fmt),
            "answer": ans_t.format(**fmt),
            "question": q_t.format(**fmt),
        })

    per_slot = target_chars // n_slots
    for slot in range(n_slots):
        filler = []
        while sum(len(x) for x in filler) < per_slot:
            filler.append(
                f"Entry {rng.randrange(10**6):06d}: routine log line, no facts of "
                f"interest, sequence {rng.randrange(10**9):09d}.\n"
            )
        half = len(filler) // 2
        blocks.extend(filler[:half])
        blocks.append(f"NOTE: {needles[slot]['fact']} is {needles[slot]['answer']}.\n")
        blocks.extend(filler[half:])
    return "".join(blocks), needles


# --- the server ------------------------------------------------------------


def _start(binary: Path, alias: dict, ctk: str, port: int) -> subprocess.Popen:
    cmd = [
        str(binary),
        "-m", alias["gguf_path"],
        "-c", "262144",
        "-ctk", ctk,
        "-ctv", "q4_0",
        "-ngl", "999",
        # One slot, no speculation, no projector: every one of those is a source
        # of difference between two runs that is not the KV cache.
        "-np", "1",
        "--kv-unified",
        "-ub", str(alias.get("n_ubatch", 1024)),
        "--jinja",
        "--host", "127.0.0.1", "--port", str(port),
    ]
    env = dict(os.environ)
    sys.path.insert(0, str(HERE.parents[1] / "dpc-client" / "core"))
    from dpc_client_core.managers.llama_server_fetcher import find_cuda_backend  # noqa: E402

    backend = find_cuda_backend(binary.parent)
    if backend:
        env["GGML_BACKEND_PATH"] = str(backend)
    log = RESULTS / f"server-{ctk}.log"
    RESULTS.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(cmd, stdout=open(log, "wb"), stderr=subprocess.STDOUT, env=env)
    return proc


def _wait_healthy(port: int, proc: subprocess.Popen, timeout_s: float = 600.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"server died with {proc.returncode}; see the log")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(2)
    raise SystemExit("server never became healthy")


def _ask(port: int, prompt: str, question: str) -> dict:
    body = json.dumps({
        "messages": [
            {"role": "user", "content": f"{prompt}\n\n{question}"},
        ],
        "temperature": TEMPERATURE,
        "seed": SEED,
        "max_tokens": 256,
        "cache_prompt": True,
        "chat_template_kwargs": {"reasoning_effort": "low"},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        doc = json.loads(r.read())
    text = doc["choices"][0]["message"]["content"] or ""
    return {
        "text": text.strip(),
        "prompt_tokens": doc.get("usage", {}).get("prompt_tokens"),
        "seconds": round(time.time() - t0, 1),
    }


def _hit(answer: str, text: str) -> bool:
    """The needle's value appears in the reply, as its own token.

    A substring test would count 512 inside 3512; the word boundary is the
    whole difference between a measurement and a flattering one.
    """
    return re.search(rf"(?<![0-9a-fA-F]){re.escape(answer)}(?![0-9a-fA-F])", text) is not None


def run_arm(ctk: str) -> Path:
    alias = _alias()
    binary = _binary()
    free = _free_vram_mib()
    if free < 26_000:
        raise SystemExit(
            f"only {free} MiB free on the card — stop the DPC service first; "
            "two 27B children on one card is an incident, not an experiment"
        )
    port = _free_port()
    proc = _start(binary, alias, ctk, port)
    record = {
        "ctk": ctk, "ctv": "q4_0", "seed": SEED, "temperature": TEMPERATURE,
        "gguf": alias["gguf_path"], "binary": str(binary),
        "free_vram_mib_before": free, "depths": {},
    }
    try:
        _wait_healthy(port, proc)
        for depth in DEPTHS:
            rng = random.Random(SEED * 1000 + depth)
            corpus, needles = _corpus(rng, depth)
            answers = []
            for n in needles:
                out = _ask(port, corpus, n["question"])
                answers.append({
                    "position": n["position"],
                    "question": n["question"],
                    "expected": n["answer"],
                    "got": out["text"],
                    "hit": _hit(n["answer"], out["text"]),
                    "prompt_tokens": out["prompt_tokens"],
                    "seconds": out["seconds"],
                })
                print(f"  {ctk} @{depth}: pos {n['position']:.2f} "
                      f"{'hit' if answers[-1]['hit'] else 'MISS'} "
                      f"({out['prompt_tokens']} tokens, {out['seconds']}s)", flush=True)
            record["depths"][str(depth)] = answers
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / f"{ctk}.json"
    out_path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    print(f"written {out_path}")
    return out_path


def compare() -> None:
    arms = {}
    for ctk in ("q4_0", "q8_0"):
        path = RESULTS / f"{ctk}.json"
        if not path.exists():
            raise SystemExit(f"missing {path} — run the {ctk} arm first")
        arms[ctk] = json.loads(path.read_text(encoding="utf-8"))

    print(f"{'depth':>8} {'pos':>5} {'q4_0 K':>8} {'q8_0 K':>8}  agree  answer")
    totals = {"q4_0": 0, "q8_0": 0, "n": 0, "agree": 0}
    for depth in DEPTHS:
        a = arms["q4_0"]["depths"][str(depth)]
        b = arms["q8_0"]["depths"][str(depth)]
        for x, y in zip(a, b):
            same = x["got"] == y["got"]
            totals["n"] += 1
            totals["q4_0"] += x["hit"]
            totals["q8_0"] += y["hit"]
            totals["agree"] += same
            print(f"{depth:>8} {x['position']:>5.2f} "
                  f"{'hit' if x['hit'] else 'MISS':>8} {'hit' if y['hit'] else 'MISS':>8}"
                  f"  {'yes' if same else 'NO':>5}  {x['expected']}")
    n = totals["n"]
    print(f"\nneedles found: q4_0 K {totals['q4_0']}/{n}, q8_0 K {totals['q8_0']}/{n}")
    print(f"identical replies: {totals['agree']}/{n} "
          f"(divergence {100 * (n - totals['agree']) / n:.0f}%)")
    print("\nBoth arms are greedy at one seed, so a divergence is the cache. "
          "Equal counts with different text is a real result and not a null: "
          "it says the keys move the trajectory without moving the answer.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ctk", choices=("q4_0", "q8_0"), help="run one arm")
    ap.add_argument("--compare", action="store_true", help="read both arms and diff them")
    args = ap.parse_args()
    if args.compare:
        compare()
    elif args.ctk:
        run_arm(args.ctk)
    else:
        ap.error("give --ctk or --compare")


if __name__ == "__main__":
    main()
