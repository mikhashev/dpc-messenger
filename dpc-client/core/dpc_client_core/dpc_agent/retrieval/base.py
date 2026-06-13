"""Retrieval backend abstractions (ADR-024 Phase 1.6a).

Three independent ABCs (VectorIndex, TextIndex, HybridFuser) composed into a
single RetrievalBackend dataclass. Split (instead of one monolithic
RetrievalBackend ABC) enables mix-and-match: any Vector + any Text + any
Fuser implementation. Useful as an escape hatch — e.g., Grafeo vector +
native BM25 + custom RRF while Phase B human grading runs on text channel.

Contract invariants common to VectorIndex and TextIndex:

- Lifecycle: __init__ -> load() -> (add | search | remove)* -> save()
- Empty index: load() returns False when nothing on disk; search() returns []
  (never raises) when the index is empty.
- Thread-safety: implementations are NOT required to be thread-safe. Caller
  owns synchronization (asyncio / threading lock) if concurrent access is
  needed. FAISS and bm25s are single-threaded today and no production
  call-site dispatches them concurrently.
- Concurrent writers: last writer wins. Caller serializes if precise
  ordering matters.
- save() semantics: file-based backends persist to disk; self-persistent
  backends (Grafeo) may make this a no-op since state is already durable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np


@dataclass
class VectorAddItem:
    """One item to add to a vector index."""
    vector: np.ndarray  # shape (D,) or (1, D)
    meta: dict


@dataclass
class TextAddItem:
    """One item to add to a text index."""
    text: str
    meta: dict


@dataclass
class FusionResult:
    """Result of HybridFuser.fuse() — chunk meta + fused score + source channel."""
    chunk_meta: dict
    score: float
    source: str


class VectorIndex(ABC):
    """Vector index backend (FAISS HNSW / Grafeo HNSW / etc).

    See module docstring for lifecycle, empty, and thread-safety contracts.
    """

    @abstractmethod
    def add(self, items: List[VectorAddItem]) -> None:
        """Add one or many vectors. Empty input is a no-op."""

    @abstractmethod
    def search(self, query_vector: np.ndarray, top_k: int) -> List[Tuple[dict, float]]:
        """Search by vector. Empty index returns []. Output: (meta, score) tuples."""

    @abstractmethod
    def remove_by_source(self, source_file: str) -> int:
        """Remove items where meta['source_file'] == source_file. Returns removed count."""

    @abstractmethod
    def save(self) -> None:
        """Persist to disk. No-op for self-persistent backends."""

    @abstractmethod
    def load(self) -> bool:
        """Load from disk. False if empty/missing."""

    @abstractmethod
    def clear(self) -> None:
        """Drop all in-memory data."""

    @property
    @abstractmethod
    def total_items(self) -> int:
        """Item count."""

    def needs_rebuild(self, model_name: str) -> bool:
        """Embedding-model-change detector. Default: no detection (always False).

        Vector backends MAY override to compare stored model identifier
        against `model_name` and return True when they differ. Used by
        model_swap.py for the prompt-on-model-change UX.
        """
        return False


class TextIndex(ABC):
    """Text/BM25 index backend (bm25s / Grafeo native BM25 / etc).

    Same lifecycle / empty / thread-safety semantics as VectorIndex.
    """

    @abstractmethod
    def add(self, items: List[TextAddItem]) -> None:
        """Add documents. Implementations may rebuild the underlying index."""

    @abstractmethod
    def search(self, query: str, top_k: int) -> List[Tuple[dict, float]]:
        """Search by text query. Empty index returns []. Output: (meta, score) tuples."""

    @abstractmethod
    def remove_by_source(self, source_file: str) -> int:
        """Remove docs where meta['source_file'] == source_file. Returns removed count."""

    @abstractmethod
    def save(self) -> None:
        """Persist to disk. No-op for self-persistent backends."""

    @abstractmethod
    def load(self) -> bool:
        """Load from disk. False if empty/missing."""

    @abstractmethod
    def clear(self) -> None:
        """Drop all in-memory data."""

    @property
    @abstractmethod
    def total_items(self) -> int:
        """Document count."""


class HybridFuser(ABC):
    """Fuses results from vector + text + graph channels into one ranked list.

    Native impl: Reciprocal Rank Fusion with layer-priority weights.
    Grafeo impl (Phase 1.6b): Grafeo's native hybrid_search.

    Dedup policy (by source_file) and layer weighting live inside the fuser,
    not in the backends — fusers may legitimately apply different policies.
    """

    @abstractmethod
    def fuse(
        self,
        vector_results: List[Tuple[dict, float]],
        text_results: List[Tuple[dict, float]],
        graph_results: Optional[List[Tuple[dict, float]]] = None,
    ) -> List[FusionResult]:
        """Merge channels into a single deduplicated ranked list."""


@dataclass
class RetrievalBackend:
    """Composite: bundles one VectorIndex + one TextIndex + one HybridFuser.

    Independent components — callers can build heterogeneous combinations
    (e.g., Grafeo vector + native text + custom fuser) via the factory.
    """
    vector: VectorIndex
    text: TextIndex
    fuser: HybridFuser

    def load(self) -> bool:
        """Load both indexes. True only if BOTH loaded successfully."""
        v_loaded = self.vector.load()
        t_loaded = self.text.load()
        return v_loaded and t_loaded

    def save(self) -> None:
        """Persist both indexes."""
        self.vector.save()
        self.text.save()
