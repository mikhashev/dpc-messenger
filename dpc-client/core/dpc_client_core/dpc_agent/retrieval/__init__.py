"""Retrieval backend abstraction (ADR-024 Phase 1.6).

Composite RetrievalBackend bundles VectorIndex + TextIndex + HybridFuser.

Phase 1.6a (commit 51df1c7): native wrappers over the existing FaissIndex
/ BM25Index / reciprocal_rank_fusion code.

Phase 1.6b.1 (commit fa7fe63): migrated 6 production call-sites to the
composite — bit-for-bit identical behavior, NativeRetrieval default.

Phase 1.6b.2 (this commit): GrafeoVectorIndex / GrafeoTextIndex /
GrafeoHybridFuser, factory wired to config flags. Mix-and-match supported:
e.g. graph SQLite + retrieval vector Grafeo + text native. Picked via
`retrieval_vector` / `retrieval_text` / `retrieval_fusion` config keys.

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
from .grafeo import (
    GrafeoHybridFuser,
    GrafeoTextIndex,
    GrafeoVectorIndex,
)
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
    # Grafeo impls
    "GrafeoVectorIndex",
    "GrafeoTextIndex",
    "GrafeoHybridFuser",
    # Factory
    "build_retrieval_backend",
]
