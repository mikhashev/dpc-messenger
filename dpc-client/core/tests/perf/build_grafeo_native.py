"""Phase A benchmark — build temporary Grafeo native index from FAISS chunks.

For fair retrieval comparison the Grafeo side must use the *same* chunks
and the *same* embeddings as the baseline FAISS stack. This script:

  1. Reads chunks from `index_meta.json` (text + metadata).
  2. Reconstructs the BGE-M3 vectors directly from `vectors.faiss`
     (avoids re-embedding 805 chunks and guarantees identical embedding
     space — Ark's correctness note S128).
  3. Creates a temp Grafeo DB with a Chunk label that has both a vector
     index (HNSW, cosine) and a text index (BM25). Inserts every chunk
     as a node carrying text + vector properties.
  4. Returns the path so the query runner (run_grafeo_native.py) can
     open it read-only and hammer hybrid_search().

Run:
    python -m tests.perf.build_grafeo_native \\
        --agent-root ~/.dpc/agents/agent_001 \\
        --out C:/Users/mike/AppData/Local/Temp/bench_grafeo
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np


CHUNK_LABEL = "Chunk"
TEXT_PROPERTY = "text"  # raw text (kept for debugging / future re-tokenization)
TEXT_PREPROCESSED_PROPERTY = "text_preprocessed"  # tokenize() output joined by space
VECTOR_PROPERTY = "embedding"


def _read_faiss_vectors(index_dir: Path, expected_count: int) -> np.ndarray:
    """Reconstruct all vectors from vectors.faiss (in chunk order)."""
    import faiss
    idx = faiss.read_index(str(index_dir / "vectors.faiss"))
    assert idx.ntotal == expected_count, (
        f"FAISS index has {idx.ntotal} vectors but index_meta.json lists {expected_count} chunks"
    )
    vectors = np.empty((idx.ntotal, idx.d), dtype=np.float32)
    for i in range(idx.ntotal):
        vectors[i] = idx.reconstruct(i)
    return vectors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="Temp Grafeo DB path (will be created/overwritten)")
    parser.add_argument(
        "--m", type=int, default=16,
        help="HNSW M (graph degree). Default 16 matches FAISS HNSW typical config",
    )
    parser.add_argument(
        "--ef-construction", type=int, default=200,
        help="HNSW ef_construction (build-time accuracy). Default 200",
    )
    args = parser.parse_args()

    agent_root = args.agent_root.expanduser()
    index_dir = agent_root / "state" / "memory_index"
    meta = json.loads((index_dir / "index_meta.json").read_text(encoding="utf-8"))
    chunks = meta["chunks"]
    header = meta["header"]
    print(f"Loaded {len(chunks)} chunks (model={header['model_name']}, dim={header['dimensions']})", file=sys.stderr)

    print("Reconstructing FAISS vectors (same embedding space as baseline)...", file=sys.stderr)
    t0 = time.perf_counter()
    vectors = _read_faiss_vectors(index_dir, len(chunks))
    print(f"  reconstructed {vectors.shape} in {time.perf_counter() - t0:.2f}s", file=sys.stderr)

    out = args.out.expanduser()
    if out.exists():
        # GrafeoDB is a directory; rmtree handles both file and directory layouts safely.
        if out.is_dir():
            shutil.rmtree(out)
        else:
            out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"Creating Grafeo DB at {out}...", file=sys.stderr)
    from grafeo import GrafeoDB
    db = GrafeoDB(str(out))

    print("Creating vector + text indices...", file=sys.stderr)
    # Native HNSW. Cosine metric matches FAISS IndexFlatIP on L2-normalized vectors.
    db.create_vector_index(
        label=CHUNK_LABEL,
        property=VECTOR_PROPERTY,
        dimensions=int(header["dimensions"]),
        metric="cosine",
        m=args.m,
        ef_construction=args.ef_construction,
    )
    # Text index lives on the PREPROCESSED field so Grafeo's BM25 sees the
    # same tokens our pipeline indexes (stopwordsiso + corpus stops +
    # script-aware tokenization). Without this Grafeo indexes raw text and
    # divergence vs our stack hits 0.058 Jaccard (Phase A finding, S128).
    db.create_text_index(label=CHUNK_LABEL, property=TEXT_PREPROCESSED_PROPERTY)

    # Reuse the production tokenizer so build-time and query-time agree.
    from dpc_client_core.dpc_agent.bm25_index import tokenize as bm25_tokenize

    print(f"Inserting {len(chunks)} nodes...", file=sys.stderr)
    t0 = time.perf_counter()
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        raw_text = chunk.get("text", "")
        # Apply our production tokenization so Grafeo BM25 indexes the same
        # tokens our bm25s pipeline does. Join back to a string because
        # Grafeo's text_search expects a text field, not a token list.
        preprocessed = " ".join(bm25_tokenize(raw_text))
        props = {
            TEXT_PROPERTY: raw_text,
            TEXT_PREPROCESSED_PROPERTY: preprocessed,
            VECTOR_PROPERTY: vec.astype(np.float32).tolist(),
            "source_file": chunk.get("source_file", ""),
            "source_layer": chunk.get("source_layer", ""),
            "heading": chunk.get("heading", ""),
            "chunk_idx": i,
        }
        db.create_node(labels=[CHUNK_LABEL], properties=props)
        if (i + 1) % 100 == 0:
            print(f"  [{i + 1}/{len(chunks)}] inserted", file=sys.stderr)
    elapsed = time.perf_counter() - t0
    print(f"Inserted all {len(chunks)} nodes in {elapsed:.2f}s ({elapsed * 1000 / len(chunks):.1f} ms/chunk)", file=sys.stderr)

    # Grafeo doesn't auto-build indices incrementally; explicit rebuild
    # after bulk insert is required before search will succeed. Discovered
    # the hard way (vector_search → GRAFEO-X001 "No vector index found").
    print("Rebuilding vector + text indices...", file=sys.stderr)
    t0 = time.perf_counter()
    db.rebuild_vector_index(label=CHUNK_LABEL, property=VECTOR_PROPERTY)
    db.rebuild_text_index(label=CHUNK_LABEL, property=TEXT_PREPROCESSED_PROPERTY)
    print(f"  rebuilt in {time.perf_counter() - t0:.2f}s", file=sys.stderr)

    db.close()
    print(f"Temp Grafeo index ready: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
