"""Hybrid search with Reciprocal Rank Fusion (ADR-010 + ADR-018).

Merges FAISS dense and BGE-M3 sparse results using RRF.
BM25 fallback when BGE-M3 sparse not available.
Priority weights by source layer: L6 > L1 > L5 > L2-docs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

LAYER_WEIGHTS: Dict[str, float] = {
    # L6 was 1.5 from ADR-010 — human-verified therefore most valuable — while
    # every one of its hints was unreadable (323 of 323 files headed by their
    # envelope), so the boost was being applied to noise. Set to 1.0 on
    # 2026-08-12, dated and reversible; the decision it feeds is in
    # AR-CORPUS-MISALIGNMENT.
    "L6": 1.0,
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


def _accumulate(results, default_layer, scores, meta_map, layer_weights, k):
    """Add one result list's RRF contributions, keyed by _file_key (the index key).

    A file appearing in multiple layers has its scores SUM (RRF combines evidence across
    lists); meta_map keeps the meta of the HIGHEST layer-weight occurrence, so the injected
    hint shows the top-priority layer's label/path (e.g. L6 over EXT) — honours the
    "keep the higher-weight variant on collapse" rule.
    """
    for rank, (meta, _score) in enumerate(results):
        key = _file_key(meta)
        weight = layer_weights.get(meta.get("source_layer", default_layer), 1.0)
        prev = meta_map.get(key)
        if prev is None or weight > layer_weights.get(prev.get("source_layer", default_layer), 1.0):
            meta_map[key] = meta
        scores[key] = scores.get(key, 0.0) + weight / (k + rank + 1)


def reciprocal_rank_fusion(
    faiss_results: List[Tuple[dict, float]],
    sparse_or_bm25_results: List[Tuple[dict, float]],
    graph_results: Optional[List[Tuple[dict, float]]] = None,
    k: int = DEFAULT_RRF_K,
    layer_weights: Dict[str, float] = LAYER_WEIGHTS,
) -> List[SearchResult]:
    """Merge FAISS dense, sparse/BM25, and graph results using RRF with layer priority weights.

    Dedup is by _file_key, i.e. by index key: one entry per document however many
    channels returned it. Cross-layer duplicates of the same file are prevented at
    indexing time (they were ~48.6% of injections before that fix), so nothing here
    needs to guess which two names mean one document.
    """
    scores: Dict[str, float] = {}
    meta_map: Dict[str, dict] = {}

    _accumulate(faiss_results, "L5", scores, meta_map, layer_weights, k)
    _accumulate(sparse_or_bm25_results, "L5", scores, meta_map, layer_weights, k)
    _accumulate(graph_results or [], "L7", scores, meta_map, layer_weights, k)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    # scores is keyed by _file_key → exactly one entry per logical file; no cross-layer
    # duplicate can survive (the old post-rank source_file dedup loop is now redundant).
    return [SearchResult(chunk_meta=meta_map[key], score=score, source="hybrid")
            for key, score in ranked]


def _file_key(meta: dict) -> str:
    """Fusion/dedup key: the index key itself, which is already unique per file.

    This used to strip the layer prefix, because the same file really could be indexed
    twice — once as L6, once as EXT — and arrive as two hints for one document. That is
    now prevented where it belongs, at indexing time: a path claimed by one layer is not
    collected again by another.

    With that gone, stripping only causes harm. It merges *different* files that share a
    relative path (`EXT/README.md` with `L6/README.md`), and under the root-qualified EXT
    keys it can even strip its way onto another layer's key — an indexed root whose last
    segment is `knowledge` would yield `knowledge/x.md`, the exact shape of an L5 key.
    Two distinct documents collapsing into one hint is silent, so prefer the identity.
    """
    return meta.get("source_file", "")
