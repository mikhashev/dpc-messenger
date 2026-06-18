"""Hybrid search with Reciprocal Rank Fusion (ADR-010 + ADR-018).

Merges FAISS dense and BGE-M3 sparse results using RRF.
BM25 fallback when BGE-M3 sparse not available.
Priority weights by source layer: L6 > L1 > L5 > L2-docs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

LAYER_WEIGHTS: Dict[str, float] = {
    "L6": 1.5,
    "L1": 1.3,
    "L7": 0.6,
    "L5": 1.0,
    "L2": 0.8,
    "EXT": 0.9,
}

DEFAULT_RRF_K = 60

# Layer prefixes stripped by _file_key so the SAME logical file indexed under more than
# one layer (e.g. EXT/<path> and L6/<path>) fuses to one entry instead of a cross-layer dup.
_LAYER_PREFIXES = frozenset(LAYER_WEIGHTS)


@dataclass
class SearchResult:
    chunk_meta: dict
    score: float
    source: str


def _accumulate(results, default_layer, scores, meta_map, layer_weights, k):
    """Add one result list's RRF contributions, keyed by _file_key (layer-stripped path).

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

    Dedup is by _file_key (layer-stripped path): a file indexed under multiple layers
    collapses to a single fused entry instead of passing as a cross-layer duplicate
    (RECALL-RELEVANCE dup — measured ~48.6% of injections before this fix).
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
    """Fusion/dedup key: source_file with a single leading layer prefix stripped.

    `EXT/archive/protocol-13.md` and `L6/archive/protocol-13.md` -> `archive/protocol-13.md`
    (same logical file under two layers -> collapse). NOT basename: that would wrongly merge
    distinct files sharing a name in different dirs (e.g. `EXT/archive/protocol-13.md` vs
    `EXT/protocol-13.md`). A source_file with no known layer prefix is returned unchanged.
    """
    sf = meta.get("source_file", "")
    head, sep, rest = sf.partition("/")
    return rest if (sep and rest and head in _LAYER_PREFIXES) else sf
