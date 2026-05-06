"""Hybrid search with Reciprocal Rank Fusion (ADR-010 + ADR-018).

Merges FAISS dense and BGE-M3 sparse results using RRF.
BM25 fallback when BGE-M3 sparse not available.
Priority weights by source layer: L6 > L1 > L5 > L2-docs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

LAYER_WEIGHTS: Dict[str, float] = {
    "L6": 1.5,
    "L1": 1.3,
    "L7": 0.6,
    "L5": 1.0,
    "L2": 0.8,
    "EXT": 0.9,
}

DEFAULT_RRF_K = 60


@dataclass
class SearchResult:
    chunk_meta: dict
    score: float
    source: str


def reciprocal_rank_fusion(
    faiss_results: List[Tuple[dict, float]],
    sparse_or_bm25_results: List[Tuple[dict, float]],
    k: int = DEFAULT_RRF_K,
    layer_weights: Dict[str, float] = LAYER_WEIGHTS,
) -> List[SearchResult]:
    """Merge FAISS dense and sparse/BM25 results using RRF with layer priority weights."""
    scores: Dict[str, float] = {}
    meta_map: Dict[str, dict] = {}

    for rank, (meta, _score) in enumerate(faiss_results):
        key = _file_key(meta)
        meta_map[key] = meta
        weight = layer_weights.get(meta.get("source_layer", "L5"), 1.0)
        scores[key] = scores.get(key, 0) + weight / (k + rank + 1)

    for rank, (meta, _score) in enumerate(sparse_or_bm25_results):
        key = _file_key(meta)
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



def _file_key(meta: dict) -> str:
    return meta.get("source_file", "")
