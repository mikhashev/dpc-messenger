"""Phase A benchmark — Grafeo native runner (HNSW + BM25 + hybrid_search).

Loads the temp Grafeo index built by build_grafeo_native.py and runs every
query through Grafeo's native vector + text + hybrid endpoints. Output
shape mirrors run_baseline.py so compare.py can join them.

Query embedding uses the SAME BGE-M3 provider as the baseline runner
(Ark's correctness note S128: identical embedding spaces or the
comparison is meaningless).

Run:
    python -m tests.perf.run_grafeo_native \\
        --queries sampled.json \\
        --grafeo-db C:/Users/mike/AppData/Local/Temp/bench_grafeo.grafeo \\
        --out grafeo_results.json --device cpu \\
        --vector-k 10 --text-k 10 --hybrid-k 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

CHUNK_LABEL = "Chunk"
TEXT_PROPERTY = "text_preprocessed"  # match what build_grafeo_native.py indexes
VECTOR_PROPERTY = "embedding"


def _record_id(row: dict) -> str:
    """Same id scheme as baseline: source_file::heading."""
    return f"{row.get('source_file', '?')}::{row.get('heading', '')}"


def _hits_from(db, result) -> list[dict]:
    """Normalize a Grafeo search result into a list of dict rows.

    Grafeo v0.5.42 returns `[(node_id_int, score_float), ...]`. We resolve
    each node_id to source_file + heading via get_node — this is the
    property-access pattern documented in the Rust API.
    """
    out: list[dict] = []
    for item in result:
        # Tuple shape: (node_id, score).
        try:
            node_id, score = item[0], float(item[1])
        except (TypeError, IndexError):
            continue
        try:
            node = db.get_node(node_id)
        except Exception:
            continue
        # Node exposes .get(key) returning a grafeo.Value wrapper. Unwrap
        # to native str so the result is JSON-serializable downstream.
        def _str_prop(name: str) -> str | None:
            try:
                v = node.get(name)
                return v.as_str() if v is not None and not v.is_null() else None
            except Exception:
                return None
        source_file = _str_prop("source_file")
        heading = _str_prop("heading")
        out.append({
            "id": f"{source_file or '?'}::{heading or ''}",
            "source_file": source_file,
            "heading": heading,
            "score": score,
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--grafeo-db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--device", type=str, default="cpu", help="Query embedding device (cpu default — match baseline)")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-query-chars", type=int, default=800)
    parser.add_argument("--vector-k", type=int, default=10)
    parser.add_argument("--text-k", type=int, default=10)
    parser.add_argument("--hybrid-k", type=int, default=20)
    args = parser.parse_args()

    queries_data = json.loads(args.queries.read_text(encoding="utf-8"))
    queries = queries_data.get("queries", [])
    if not queries:
        print("No queries.", file=sys.stderr)
        return 1
    print(f"Loaded {len(queries)} queries", file=sys.stderr)

    from grafeo import GrafeoDB
    db = GrafeoDB(str(args.grafeo_db))
    print(f"Opened Grafeo DB at {args.grafeo_db}", file=sys.stderr)

    # Grafeo v0.5.42 doesn't persist vector/text indices across close/reopen;
    # the schema entry survives but the underlying HNSW/BM25 structure is
    # in-memory only. Rebuild on open. (Discovered in this run — fix or
    # confirmed-by-design verification belongs in upstream Grafeo before
    # we declare this a long-term workaround.)
    print("Rebuilding indices for this session...", file=sys.stderr)
    t0 = time.perf_counter()
    db.rebuild_vector_index(label=CHUNK_LABEL, property=VECTOR_PROPERTY)
    db.rebuild_text_index(label=CHUNK_LABEL, property=TEXT_PROPERTY)
    print(f"  rebuilt in {time.perf_counter() - t0:.2f}s", file=sys.stderr)

    # Same preprocessing the build script applied to chunks — apply to the
    # query so Grafeo BM25 compares apples to apples with our bm25s baseline.
    from dpc_client_core.dpc_agent.bm25_index import tokenize as bm25_tokenize

    from dpc_client_core.dpc_agent.memory import EmbeddingProvider
    provider = EmbeddingProvider(local_files_only=True, device=args.device)
    provider._load_model()
    print(f"Embedding: {provider.model_name} on {provider.device}", file=sys.stderr)

    per_query: list[dict] = []
    for i, q in enumerate(queries, 1):
        text = q["text"][: args.max_query_chars]

        t0 = time.perf_counter()
        vec = np.array(
            provider._model.encode(text, normalize_embeddings=True, batch_size=args.batch_size, show_progress_bar=False),
            dtype=np.float32,
        ).tolist()
        t_embed = time.perf_counter() - t0

        t0 = time.perf_counter()
        vector_hits = _hits_from(db, db.vector_search(
            label=CHUNK_LABEL, property=VECTOR_PROPERTY, query=vec, k=args.vector_k,
        ))
        t_vector = time.perf_counter() - t0

        # Preprocess query with the same tokenizer used at index build time.
        preprocessed_query = " ".join(bm25_tokenize(q["text"]))

        t0 = time.perf_counter()
        text_hits = _hits_from(db, db.text_search(
            label=CHUNK_LABEL, property=TEXT_PROPERTY, query=preprocessed_query, k=args.text_k,
        ))
        t_text = time.perf_counter() - t0

        t0 = time.perf_counter()
        hybrid_hits = _hits_from(db, db.hybrid_search(
            label=CHUNK_LABEL,
            text_property=TEXT_PROPERTY,
            vector_property=VECTOR_PROPERTY,
            query_text=preprocessed_query,
            query_vector=vec,
            k=args.hybrid_k,
            fusion="rrf",
        ))
        t_hybrid = time.perf_counter() - t0

        per_query.append({
            "query_text": q["text"],
            "query_source": q.get("source_file", ""),
            "vector_top": vector_hits,
            "text_top": text_hits,
            "hybrid_top": hybrid_hits,
            "latency_ms": {
                "embed": round(t_embed * 1000, 2),
                "vector": round(t_vector * 1000, 2),
                "text": round(t_text * 1000, 2),
                "hybrid": round(t_hybrid * 1000, 2),
                "total": round((t_embed + t_vector + t_text + t_hybrid) * 1000, 2),
            },
        })
        if i % 10 == 0:
            print(f"  [{i}/{len(queries)}] last total {per_query[-1]['latency_ms']['total']} ms", file=sys.stderr)

    db.close()

    totals = [pq["latency_ms"]["total"] for pq in per_query]
    summary = {
        "queries_count": len(per_query),
        "vector_k": args.vector_k,
        "text_k": args.text_k,
        "hybrid_k": args.hybrid_k,
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
    print(f"Wrote {len(per_query)} → {args.out}", file=sys.stderr)
    print(f"Summary: {summary['latency_ms']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
