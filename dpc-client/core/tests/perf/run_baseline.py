"""Phase A benchmark — baseline runner (current FAISS+BM25+RRF stack).

Loads the agent's existing FAISS and BM25 indices, runs each query in the
sampled set, captures top-K results per channel plus RRF fusion, and
records latency. Output is consumed by compare.py alongside the Grafeo
native run.

Run (with backend stopped or with embedding on CPU to avoid VRAM contention):
    python -m tests.perf.run_baseline \\
        --queries sampled.json \\
        --agent-root ~/.dpc/agents/agent_001 \\
        --out baseline_results.json \\
        --device cpu --faiss-k 10 --bm25-k 10 --rrf-k 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

from dpc_client_core.dpc_agent.bm25_index import BM25Index
from dpc_client_core.dpc_agent.faiss_index import FaissIndex
from dpc_client_core.dpc_agent.hybrid_search import reciprocal_rank_fusion
from dpc_client_core.dpc_agent.memory import EmbeddingProvider


def _meta_to_id(meta: dict) -> str:
    """Stable ID for a chunk — same scheme as RRF dedup."""
    return f"{meta.get('source_file', '?')}::{meta.get('heading', '')}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, required=True, help="Sampled queries JSON")
    parser.add_argument("--agent-root", type=Path, required=True, help="Agent root, e.g. ~/.dpc/agents/agent_001")
    parser.add_argument("--out", type=Path, required=True, help="Output results JSON")
    parser.add_argument("--device", type=str, default="cpu", help="Embedding device (default cpu — avoid VRAM contention)")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--faiss-k", type=int, default=10, help="Top-K for FAISS channel")
    parser.add_argument("--bm25-k", type=int, default=10, help="Top-K for BM25 channel")
    parser.add_argument("--rrf-k", type=int, default=20, help="Top-K after RRF fusion")
    parser.add_argument("--max-query-chars", type=int, default=800,
                        help="Truncate queries before embedding (BGE-M3 4096-token attention can OOM on CPU)")
    args = parser.parse_args()

    queries_data = json.loads(args.queries.read_text(encoding="utf-8"))
    queries = queries_data.get("queries", [])
    if not queries:
        print("No queries.", file=sys.stderr)
        return 1
    print(f"Loaded {len(queries)} queries from {args.queries}", file=sys.stderr)

    agent_root = args.agent_root.expanduser()
    index_dir = agent_root / "state" / "memory_index"
    faiss_idx = FaissIndex(index_dir)
    bm25_idx = BM25Index(index_dir)
    if not faiss_idx.load():
        print(f"FAISS index not found at {index_dir}", file=sys.stderr)
        return 2
    if not bm25_idx.load():
        print(f"BM25 index not found at {index_dir}", file=sys.stderr)
        return 2
    print(f"FAISS: {faiss_idx.total_vectors} vectors  BM25: loaded", file=sys.stderr)

    provider = EmbeddingProvider(local_files_only=True, device=args.device)
    provider._load_model()
    print(f"Embedding provider: {provider.model_name} on {provider.device}", file=sys.stderr)

    per_query: list[dict] = []
    for i, q in enumerate(queries, 1):
        text = q["text"][: args.max_query_chars]

        t0 = time.perf_counter()
        vec = np.array(
            provider._model.encode(text, normalize_embeddings=True, batch_size=args.batch_size, show_progress_bar=False),
            dtype=np.float32,
        )
        t_embed = time.perf_counter() - t0

        t0 = time.perf_counter()
        faiss_results = faiss_idx.search(vec, args.faiss_k)
        t_faiss = time.perf_counter() - t0

        t0 = time.perf_counter()
        bm25_results = bm25_idx.search(q["text"], args.bm25_k)
        t_bm25 = time.perf_counter() - t0

        t0 = time.perf_counter()
        fused = reciprocal_rank_fusion(faiss_results, bm25_results)
        t_rrf = time.perf_counter() - t0
        fused_top = fused[: args.rrf_k]

        per_query.append({
            "query_text": q["text"],
            "query_source": q.get("source_file", ""),
            "faiss_top": [
                {"id": _meta_to_id(m), "source_file": m.get("source_file"), "score": float(s)}
                for m, s in faiss_results
            ],
            "bm25_top": [
                {"id": _meta_to_id(m), "source_file": m.get("source_file"), "score": float(s)}
                for m, s in bm25_results
            ],
            "rrf_top": [
                {"id": _meta_to_id(r.chunk_meta), "source_file": r.chunk_meta.get("source_file"), "score": float(r.score)}
                for r in fused_top
            ],
            "latency_ms": {
                "embed": round(t_embed * 1000, 2),
                "faiss": round(t_faiss * 1000, 2),
                "bm25": round(t_bm25 * 1000, 2),
                "rrf": round(t_rrf * 1000, 2),
                "total": round((t_embed + t_faiss + t_bm25 + t_rrf) * 1000, 2),
            },
        })
        if i % 10 == 0:
            print(f"  [{i}/{len(queries)}] last total {per_query[-1]['latency_ms']['total']} ms", file=sys.stderr)

    totals = [pq["latency_ms"]["total"] for pq in per_query]
    summary = {
        "queries_count": len(per_query),
        "faiss_k": args.faiss_k,
        "bm25_k": args.bm25_k,
        "rrf_k": args.rrf_k,
        "device": provider.device,
        "embedding_model": provider.model_name,
        "latency_ms": {
            "mean": round(sum(totals) / len(totals), 2),
            "p50": round(float(np.percentile(totals, 50)), 2),
            "p95": round(float(np.percentile(totals, 95)), 2),
            "max": round(max(totals), 2),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"summary": summary, "per_query": per_query}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(per_query)} query results → {args.out}", file=sys.stderr)
    print(f"Summary: {summary['latency_ms']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
