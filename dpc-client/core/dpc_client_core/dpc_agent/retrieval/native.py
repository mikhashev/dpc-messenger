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
        # BM25Index.remove_by_source and BM25Index.add both rebuild the corpus
        # from meta["text"] (see bm25_index.py:118 and :170). Inject the text
        # into meta when caller didn't supply it so the rebuild path doesn't
        # see an empty corpus and crash inside bm25s.
        metas = [
            {**item.meta, "text": item.meta.get("text") or item.text}
            for item in items
        ]
        self._inner.add(texts, metas)

    def search(self, query: str, top_k: int) -> List[Tuple[dict, float]]:
        return self._inner.search(query, top_k)

    def remove_by_source(self, source_file: str) -> int:
        return self._inner.remove_by_source(source_file)

    def save(self) -> None:
        self._inner.save()

    def load(self) -> bool:
        return self._inner.load()

    def clear(self) -> None:
        # Intentional coupling to BM25Index internals — that class has no
        # public clear() method, and clear() is only invoked from tests today.
        # Will be reconsidered if BM25Index grows a public reset path.
        self._inner._retriever = None
        self._inner._chunk_metas = []
        self._inner._corpus_stop_words = frozenset()

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
