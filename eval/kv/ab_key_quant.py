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
from typing import Optional

import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

# The production alias, read rather than retyped: this probe is worthless if it
# measures a different model or a different pin than the fleet runs.
PROVIDERS = Path.home() / ".dpc" / "providers.json"
ALIAS_TYPE = "llamacpp_server"

DEPTHS = (32_000, 120_000, 175_000)
# The pair to compare, by report file name.
ARMS = ("q4_0-q4_0", "q8_0-q8_0")
N_CTX = 262_144
SEED = 42
# Deterministic decoding. The vendor does not recommend greedy for a thinking
# model, and that caveat is real for a quality verdict — but this is a paired
# comparison, and without determinism the difference between the arms is the
# sampler.
TEMPERATURE = 0.0
# 256 was too small for the needle that asks the model to compute: on 2026-08-30
# four of four failures were an empty answer, the whole budget spent thinking.
MAX_TOKENS = 256


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


def _corpus(rng: random.Random, target_chars: int) -> tuple[str, list[dict]]:
    """Filler with needles spread through it, and the questions that read them.

    Needles sit at 25 %, 50 % and 75 % of the depth: a defect that eats the
    oldest region and one that eats the middle look identical if every needle
    is at the end. The budget is characters; the caller converts from the depth
    it wants through the model's own tokeniser, because this filler runs 1.7×
    denser than the four-chars-per-token guess.
    """
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


def _start(binary: Path, alias: dict, ctk: str, ctv: str, port: int) -> subprocess.Popen:
    cmd = [
        str(binary),
        "-m", alias["gguf_path"],
        "-c", str(N_CTX),
        "-ctk", ctk,
        "-ctv", ctv,
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
    log = RESULTS / f"server-{ctk}-{ctv}.log"
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


def _count_tokens(port: int, text: str) -> int:
    """The model's own tokeniser, so a depth is the depth it claims to be."""
    body = json.dumps({"content": text}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/tokenize",
        data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return len(json.loads(r.read())["tokens"])


def _corpus_at_depth(rng: random.Random, depth: int, port: int, budget: int):
    """A corpus whose token count is `depth`, measured rather than assumed.

    One calibration read plus one correction lands inside a few per cent, and
    the result is clamped to `budget` so the deepest rung cannot ask for more
    cells than the pool has — the failure that would look like a cache defect.
    """
    corpus, needles = _corpus(rng, depth * 4)
    for _ in range(3):
        actual = _count_tokens(port, corpus)
        if abs(actual - depth) <= max(500, depth // 50) or actual > budget:
            break
        rng2 = random.Random(rng.random())
        corpus, needles = _corpus(rng2, int(len(corpus) * depth / actual))
    while actual > budget:
        corpus, needles = _corpus(random.Random(rng.random()),
                                  int(len(corpus) * budget / actual * 0.97))
        actual = _count_tokens(port, corpus)
    return corpus, needles, actual


def _ask(port: int, prompt: str, question: str, max_tokens: int = MAX_TOKENS) -> dict:
    body = json.dumps({
        "messages": [
            {"role": "user", "content": f"{prompt}\n\n{question}"},
        ],
        "temperature": TEMPERATURE,
        "seed": SEED,
        "max_tokens": max_tokens,
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
    choice = doc["choices"][0]
    text = choice["message"]["content"] or ""
    return {
        "text": text.strip(),
        "finish_reason": choice.get("finish_reason"),
        "prompt_tokens": doc.get("usage", {}).get("prompt_tokens"),
        "seconds": round(time.time() - t0, 1),
    }


def _hit(answer: str, text: str) -> bool:
    """The needle's value appears in the reply, as its own token.

    A substring test would count 512 inside 3512; the word boundary is the
    whole difference between a measurement and a flattering one.
    """
    return re.search(rf"(?<![0-9a-fA-F]){re.escape(answer)}(?![0-9a-fA-F])", text) is not None


def run_arm(ctk: str, ctv: str, only: Optional[int] = None,
            max_tokens: int = MAX_TOKENS, suffix: str = "") -> Path:
    alias = _alias()
    binary = _binary()
    free = _free_vram_mib()
    if free < 26_000:
        raise SystemExit(
            f"only {free} MiB free on the card — stop the DPC service first; "
            "two 27B children on one card is an incident, not an experiment"
        )
    port = _free_port()
    proc = _start(binary, alias, ctk, ctv, port)
    record = {
        "ctk": ctk, "ctv": ctv, "seed": SEED, "temperature": TEMPERATURE,
        "gguf": alias["gguf_path"], "binary": str(binary),
        "free_vram_mib_before": free, "max_tokens": max_tokens,
        "only_needle": only, "depths": {},
    }
    try:
        _wait_healthy(port, proc)
        for depth in DEPTHS:
            rng = random.Random(SEED * 1000 + depth)
            corpus, needles, actual = _corpus_at_depth(rng, depth, port, N_CTX - 2_000)
            record.setdefault("corpus_tokens", {})[str(depth)] = actual
            print(f"  {ctk} @{depth}: corpus is {actual} tokens", flush=True)
            answers = []
            for i, n in enumerate(needles):
                if only is not None and i != only:
                    continue
                out = _ask(port, corpus, n["question"], max_tokens)
                answers.append({
                    "position": n["position"],
                    "question": n["question"],
                    "expected": n["answer"],
                    "got": out["text"],
                    "hit": _hit(n["answer"], out["text"]),
                    "prompt_tokens": out["prompt_tokens"],
                    "finish_reason": out["finish_reason"],
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
    out_path = RESULTS / f"{ctk}-{ctv}{suffix}.json"
    out_path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    print(f"written {out_path}")
    return out_path


def compare() -> None:
    """The two arms side by side, with «empty» kept apart from «wrong».

    An empty reply is the model spending its whole budget thinking, not a
    retrieval failure, and scoring the two together would blame the cache for
    the sampler.
    """
    arms = {}
    for name in ARMS:
        path = RESULTS / f"{name}.json"
        if not path.exists():
            raise SystemExit(f"missing {path} — run that arm first")
        arms[name] = json.loads(path.read_text(encoding="utf-8"))
    a_name, b_name = ARMS

    def verdict(row):
        return "hit" if row["hit"] else ("empty" if not row["got"] else "wrong")

    print(f"{'depth':>8} {'pos':>5} {a_name:>12} {b_name:>12}  agree  answer")
    totals = {a_name: 0, b_name: 0, "n": 0, "agree": 0, "empty": 0}
    for depth in DEPTHS:
        rows_a = arms[a_name]["depths"][str(depth)]
        rows_b = arms[b_name]["depths"][str(depth)]
        for x, y in zip(rows_a, rows_b):
            same = x["got"] == y["got"]
            totals["n"] += 1
            totals[a_name] += x["hit"]
            totals[b_name] += y["hit"]
            totals["agree"] += same
            totals["empty"] += (not x["got"]) + (not y["got"])
            depth_real = arms[a_name].get("corpus_tokens", {}).get(str(depth), depth)
            print(f"{depth_real:>8} {x['position']:>5.2f} {verdict(x):>12} {verdict(y):>12}"
                  f"  {'yes' if same else 'NO':>5}  {x['expected']}")
    n = totals["n"]
    print(f"\nneedles found: {a_name} {totals[a_name]}/{n}, {b_name} {totals[b_name]}/{n}"
          f"   (empty replies, scored as neither: {totals['empty']})")
    print(f"identical replies: {totals['agree']}/{n} "
          f"(divergence {100 * (n - totals['agree']) / n:.0f}%)")
    print("\nBoth arms are greedy at one seed, so a divergence is the cache. "
          "Equal counts with different text is a result and not a null: it says "
          "the cache moves the trajectory without moving the answer.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ctk", choices=("q4_0", "q8_0"), help="run one arm")
    ap.add_argument("--ctv", choices=("q4_0", "q8_0"), default="q4_0",
                    help="only a MATCHED pair has a flash-attention kernel on the "
                         "pinned CUDA build: measured 2026-08-30, q8_0 K over q4_0 V "
                         "prefills at 34 tok/s and falling against 800-900 matched")
    ap.add_argument("--compare", action="store_true", help="read both arms and diff them")
    ap.add_argument("--only", type=int, choices=(0, 1, 2),
                    help="run one needle: 0 constant, 1 checksum, 2 the one that computes")
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    ap.add_argument("--suffix", default="", help="report name suffix, to keep runs apart")
    args = ap.parse_args()
    if args.compare:
        compare()
    elif args.ctk:
        run_arm(args.ctk, args.ctv, args.only, args.max_tokens, args.suffix)
    else:
        ap.error("give --ctk or --compare")


if __name__ == "__main__":
    main()
