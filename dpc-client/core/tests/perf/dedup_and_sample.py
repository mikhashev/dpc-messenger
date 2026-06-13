"""Phase A benchmark — near-duplicate dedup + stratified sampling.

Input: candidate queries from extract_query_set.py output.
Output: final benchmark query set, ~30–50 queries, semantically diverse.

Pipeline:
  1. Load candidates JSON.
  2. Embed all candidates with BGE-M3 (batched).
  3. Greedy near-dup removal: keep a query only if max cosine vs kept set
     is below SIMILARITY_THRESHOLD (default 0.95, per CC S126 [#120]).
  4. Stratified sample by length bucket (short / medium / long) so the
     final set covers both terse questions and verbose ones.

Run:
    python -m tests.perf.dedup_and_sample \\
        --in queries.json --out sampled.json --target 40
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

import numpy as np

SIMILARITY_THRESHOLD = 0.95  # per CC S126 [#120]
LENGTH_BUCKETS = [
    ("short", 25, 80),
    ("medium", 80, 250),
    ("long", 250, 10_000),
]


def _bucket_for(length: int) -> str:
    for name, lo, hi in LENGTH_BUCKETS:
        if lo <= length < hi:
            return name
    return "long"


def greedy_near_dup_filter(
    texts: List[str],
    embeddings: np.ndarray,
    threshold: float = SIMILARITY_THRESHOLD,
) -> List[int]:
    """Return indices of texts to keep after greedy near-dup removal.

    embeddings are L2-normalized (BGE-M3 returns normalized vectors when
    requested), so cosine sim is just the dot product.
    """
    kept: List[int] = []
    if len(texts) == 0:
        return kept
    kept.append(0)
    kept_vecs = embeddings[[0]]
    for i in range(1, len(texts)):
        sims = kept_vecs @ embeddings[i]
        if float(sims.max()) < threshold:
            kept.append(i)
            kept_vecs = np.vstack([kept_vecs, embeddings[i]])
    return kept


def stratified_sample(
    queries: List[dict],
    target: int,
    rng: np.random.Generator,
) -> List[dict]:
    """Sample `target` queries with roughly proportional bucket coverage.

    Falls back gracefully when a bucket is empty or under-quota.
    """
    buckets: dict[str, List[dict]] = {name: [] for name, _, _ in LENGTH_BUCKETS}
    for q in queries:
        buckets[_bucket_for(len(q["text"]))].append(q)

    per_bucket = max(1, target // len(buckets))
    out: List[dict] = []
    leftover: List[dict] = []
    for name, items in buckets.items():
        if not items:
            continue
        take = min(per_bucket, len(items))
        picked_idx = rng.choice(len(items), size=take, replace=False)
        picked = [items[i] for i in picked_idx]
        out.extend(picked)
        leftover.extend(items[i] for i in range(len(items)) if i not in set(picked_idx))

    # Top up from leftover if we still have room.
    while len(out) < target and leftover:
        i = int(rng.integers(0, len(leftover)))
        out.append(leftover.pop(i))
    return out[:target]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="inp", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--target", type=int, default=40, help="Target final query count (default 40)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--similarity-threshold", type=float, default=SIMILARITY_THRESHOLD,
        help="Cosine sim threshold for near-dup (default 0.95)",
    )
    parser.add_argument(
        "--device", type=str, default="cpu",
        help="Embedding device: cpu (safe default, avoids GPU contention with running backend) "
             "or cuda/mps for speed (only if backend is stopped)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=32,
        help="Embedding batch size (lower = less memory)",
    )
    args = parser.parse_args()

    data = json.loads(args.inp.read_text(encoding="utf-8"))
    queries = data.get("queries", [])
    if not queries:
        print("No input queries.", file=sys.stderr)
        return 1
    print(f"Loaded {len(queries)} candidate queries", file=sys.stderr)

    # BGE-M3 embedding. Use CPU + small batch to avoid contending with the
    # running agent backend for VRAM (the backend pins the singleton provider
    # on GPU; benchmarks should not steal from it).
    from dpc_client_core.dpc_agent.memory import EmbeddingProvider
    provider = EmbeddingProvider(local_files_only=True, device=args.device)
    print(f"Embedding {len(queries)} queries with {provider.model_name} on {provider.device}...", file=sys.stderr)
    provider._load_model()
    # Truncate before tokenizer to bound attention-matrix size. Queries are
    # typically short; full chat messages can be 5000+ chars and would blow up
    # BGE-M3's 4096-token window on CPU. 800 chars ≈ 200 tokens is enough for
    # semantic-similarity intent matching.
    MAX_CHARS = 800
    texts = [q["text"][:MAX_CHARS] for q in queries]
    vecs = np.array(
        provider._model.encode(
            texts,
            normalize_embeddings=True,
            batch_size=args.batch_size,
            show_progress_bar=False,
        ),
        dtype=np.float32,
    )

    kept_idx = greedy_near_dup_filter([q["text"] for q in queries], vecs, args.similarity_threshold)
    deduped = [queries[i] for i in kept_idx]
    print(f"After near-dup filter (threshold={args.similarity_threshold}): {len(deduped)}", file=sys.stderr)

    rng = np.random.default_rng(args.seed)
    sampled = stratified_sample(deduped, args.target, rng)
    print(f"Stratified sample → {len(sampled)} queries (target={args.target})", file=sys.stderr)

    bucket_counts: dict[str, int] = {}
    for q in sampled:
        bucket_counts[_bucket_for(len(q["text"]))] = bucket_counts.get(_bucket_for(len(q["text"])), 0) + 1
    print(f"Bucket distribution: {bucket_counts}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({
            "queries": sampled,
            "count": len(sampled),
            "source_count": len(queries),
            "deduped_count": len(deduped),
            "similarity_threshold": args.similarity_threshold,
            "seed": args.seed,
            "bucket_distribution": bucket_counts,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(sampled)} queries → {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
