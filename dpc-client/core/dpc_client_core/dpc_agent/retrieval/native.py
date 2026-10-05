"""Native retrieval implementations (ADR-024 Phase 1.6a).

Thin ABC wrappers over the existing FaissIndex / BM25Index /
reciprocal_rank_fusion code. No behavioral change — these adapters exist
to give the rest of the codebase a uniform interface so Phase 1.6b can
swap in Grafeo implementations and flip a config flag.
"""

from __future__ import annotations

import pathlib
from typing import List, Optional, Tuple

import numpy as np

from ..bm25_index import BM25Index
from ..faiss_index import FaissIndex
from ..hybrid_search import (
    DEFAULT_RRF_K,
    LAYER_WEIGHTS,
    SearchResult,
    reciprocal_rank_fusion,
)
from .base import (
    FusionResult,
    HybridFuser,
    RetrievalBackend,
    TextAddItem,
    TextIndex,
    VectorAddItem,
    VectorIndex,
)


def _group_by_source(metas) -> dict:
    rows: dict = {}
    for meta in metas:
        rows.setdefault(meta.get("source_file", ""), []).append(meta)
    return rows


class NativeVectorIndex(VectorIndex):
    """ABC wrapper over FaissIndex (IndexFlatIP / HNSW upgrade path)."""

    def __init__(
        self,
        index_dir: pathlib.Path,
        model_name: str = "",
        dimensions: int = 384,
    ):
        self._inner = FaissIndex(index_dir, model_name=model_name, dimensions=dimensions)

    def add(self, items: List[VectorAddItem]) -> None:
        if not items:
            return
        vectors = np.vstack([
            item.vector.reshape(1, -1) if item.vector.ndim == 1 else item.vector
            for item in items
        ])
        metas = [item.meta for item in items]
        self._inner.add(vectors, metas)

    def search(self, query_vector: np.ndarray, top_k: int) -> List[Tuple[dict, float]]:
        return self._inner.search(query_vector, top_k)

    def remove_by_source(self, source_file: str) -> int:
        return self._inner.remove_by_source(source_file)

    def remove_by_sources(self, source_files) -> int:
        return self._inner.remove_by_sources(source_files)

    def source_rows(self):
        return _group_by_source(self._inner._chunks)

    def keep_one_row_per_source(self, source_files, is_current):
        keys = set(source_files)
        last_current = {}
        for i, meta in enumerate(self._inner._chunks):
            key = meta.get("source_file", "")
            if key in keys and is_current(meta):
                last_current[key] = i
        keep = [i for i, meta in enumerate(self._inner._chunks)
                if meta.get("source_file", "") not in keys
                or last_current.get(meta.get("source_file", "")) == i]
        self._inner.keep_rows(keep)
        return keys - set(last_current)

    def save(self) -> None:
        self._inner.save()

    def load(self) -> bool:
        return self._inner.load()

    def clear(self) -> None:
        self._inner.clear()

    @property
    def total_items(self) -> int:
        return self._inner.total_vectors

    def needs_rebuild(self, model_name: str) -> bool:
        return self._inner.needs_rebuild(model_name)


class NativeTextIndex(TextIndex):
    """ABC wrapper over BM25Index (bm25s + stopwords-iso tokenization)."""

    def __init__(self, index_dir: Optional[pathlib.Path] = None):
        self._inner = BM25Index(index_dir)

    def add(self, items: List[TextAddItem]) -> None:
        if not items:
            return
        texts = [item.text for item in items]
        # meta["text"] is a preview for readers, never the rebuild source — BM25Index
        # keeps the full texts itself. Filled when the caller left it out, at the same
        # length the indexing pipeline and the Grafeo backend store.
        metas = [
            {**item.meta, "text": item.meta.get("text") or item.text[:500]}
            for item in items
        ]
        self._inner.add(texts, metas)

    def source_rows(self):
        return _group_by_source(self._inner._chunk_metas)

    def sources_missing_full_text(self):
        return self._inner.sources_missing_full_text()

    def begin_batch(self) -> None:
        """Forward the deferral the indexing pass asks for.

        `BM25Index.add` rebuilds the whole index on every call, and it implements
        `begin_batch`/`end_batch` to skip that during a bulk pass. The pass asks
        for it behind a `hasattr` check, so while this wrapper lacked the two
        methods the request was answered "no" in silence: a 328-document rebuild
        rebuilt the index twenty-one times, once per batch of sixteen, and the
        cost grows with the square of the corpus.
        """
        self._inner.begin_batch()

    def end_batch(self) -> None:
        self._inner.end_batch()

    def search(self, query: str, top_k: int) -> List[Tuple[dict, float]]:
        return self._inner.search(query, top_k)

    def remove_by_source(self, source_file: str) -> int:
        return self._inner.remove_by_source(source_file)

    def remove_by_sources(self, source_files) -> int:
        return self._inner.remove_by_sources(source_files)

    def save(self) -> None:
        self._inner.save()

    def load(self) -> bool:
        return self._inner.load()

    def clear(self) -> None:
        self._inner.clear()

    @property
    def total_items(self) -> int:
        return self._inner.total_documents


class NativeHybridFuser(HybridFuser):
    """ABC wrapper over reciprocal_rank_fusion with layer-priority weights."""

    def __init__(self, k: int = DEFAULT_RRF_K, layer_weights: Optional[dict] = None):
        self._k = k
        self._weights = layer_weights or LAYER_WEIGHTS

    def fuse(
        self,
        vector_results: List[Tuple[dict, float]],
        text_results: List[Tuple[dict, float]],
        graph_results: Optional[List[Tuple[dict, float]]] = None,
    ) -> List[FusionResult]:
        merged: List[SearchResult] = reciprocal_rank_fusion(
            vector_results,
            text_results,
            graph_results,
            k=self._k,
            layer_weights=self._weights,
        )
        return [
            FusionResult(chunk_meta=r.chunk_meta, score=r.score, source=r.source)
            for r in merged
        ]


def make_native_backend(
    index_dir: pathlib.Path,
    model_name: str = "",
    dimensions: int = 384,
) -> RetrievalBackend:
    """Build a fully-native RetrievalBackend (FAISS + bm25s + custom RRF)."""
    return RetrievalBackend(
        vector=NativeVectorIndex(index_dir, model_name=model_name, dimensions=dimensions),
        text=NativeTextIndex(index_dir),
        fuser=NativeHybridFuser(),
    )
