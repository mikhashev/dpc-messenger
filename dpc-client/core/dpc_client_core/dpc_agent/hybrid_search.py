"""Hybrid search with Reciprocal Rank Fusion (ADR-010, MEM-3.6).

Merges FAISS cosine similarity and BM25 keyword results using RRF.
Priority weights by source layer: L6 > L1 > L5 > L2-docs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

LAYER_WEIGHTS: Dict[str, float] = {
    "L6": 1.5,
    "L1": 1.3,
    "L5": 1.0,
    "L2": 0.8,
}

DEFAULT_RRF_K = 60


@dataclass
class SearchResult:
    chunk_meta: dict
    score: float
    source: str


def reciprocal_rank_fusion(
    faiss_results: List[Tuple[dict, float]],
    bm25_results: List[Tuple[dict, float]],
    k: int = DEFAULT_RRF_K,
    layer_weights: Dict[str, float] = LAYER_WEIGHTS,
) -> List[SearchResult]:
    """Merge FAISS and BM25 results using RRF with layer priority weights."""
    scores: Dict[str, float] = {}
    meta_map: Dict[str, dict] = {}

    for rank, (meta, _score) in enumerate(faiss_results):
        key = _chunk_key(meta)
        meta_map[key] = meta
        weight = layer_weights.get(meta.get("source_layer", "L5"), 1.0)
        scores[key] = scores.get(key, 0) + weight / (k + rank + 1)

    for rank, (meta, _score) in enumerate(bm25_results):
        key = _chunk_key(meta)
        meta_map[key] = meta
        weight = layer_weights.get(meta.get("source_layer", "L5"), 1.0)
        scores[key] = scores.get(key, 0) + weight / (k + rank + 1)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    seen_files: set = set()
    deduped: List[SearchResult] = []
    for key, score in ranked:
        fname = meta_map[key].get("source_file", "")
        if fname not in seen_files:
            seen_files.add(fname)
            deduped.append(SearchResult(chunk_meta=meta_map[key], score=score, source="hybrid"))
    return deduped


def _chunk_key(meta: dict) -> str:
    return f"{meta.get('source_file', '')}:{meta.get('chunk_index', 0)}"
