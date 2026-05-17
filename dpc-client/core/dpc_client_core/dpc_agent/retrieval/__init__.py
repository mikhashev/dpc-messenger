"""Retrieval backend abstraction (ADR-024 Phase 1.6a).

Composite RetrievalBackend bundles VectorIndex + TextIndex + HybridFuser.
Phase 1.6a: thin native wrappers over existing FaissIndex / BM25Index /
reciprocal_rank_fusion. No call-sites changed yet — that lands in 1.6b
along with the Grafeo implementations.

Public API: see __all__.
"""

from .base import (
    FusionResult,
    HybridFuser,
    RetrievalBackend,
    TextAddItem,
    TextIndex,
    VectorAddItem,
    VectorIndex,
)
from .factory import build_retrieval_backend
from .native import (
    NativeHybridFuser,
    NativeTextIndex,
    NativeVectorIndex,
    make_native_backend,
)

__all__ = [
    # ABCs
    "VectorIndex",
    "TextIndex",
    "HybridFuser",
    # Dataclasses
    "VectorAddItem",
    "TextAddItem",
    "FusionResult",
    "RetrievalBackend",
    # Native impls
    "NativeVectorIndex",
    "NativeTextIndex",
    "NativeHybridFuser",
    "make_native_backend",
    # Factory
    "build_retrieval_backend",
]
