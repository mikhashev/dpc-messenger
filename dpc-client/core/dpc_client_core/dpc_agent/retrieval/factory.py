"""Retrieval backend factory (ADR-024 Phase 1.6b.2).

Config-driven builder. Picks one VectorIndex, one TextIndex, and one
HybridFuser implementation based on three config flags, then bundles them
into a RetrievalBackend composite.

Config schema (all optional, default native):
    retrieval_vector: "native" | "grafeo"   # vector channel backend
    retrieval_text:   "native" | "grafeo"   # text channel backend
    retrieval_fusion: "custom" | "grafeo"   # fuser

Grafeo channels share a dedicated DB under `<index_dir>/grafeo/` so the
graph backend (SQLite / Grafeo via ADR-024 Phase 1.5) and the retrieval
backend can be picked independently. Unknown config values raise
ValueError — silent fallback to native ended with 1.6a.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Optional

from .base import HybridFuser, RetrievalBackend, TextIndex, VectorIndex
from .grafeo import GrafeoHybridFuser, GrafeoTextIndex, GrafeoVectorIndex
from .native import NativeHybridFuser, NativeTextIndex, NativeVectorIndex

log = logging.getLogger(__name__)


def build_retrieval_backend(
    index_dir: pathlib.Path,
    config: Optional[dict] = None,
    model_name: str = "",
    dimensions: int = 384,
) -> RetrievalBackend:
    """Compose a RetrievalBackend from config flags. See module docstring."""
    cfg = config or {}
    vector = _build_vector(
        cfg.get("retrieval_vector", "native"),
        index_dir,
        model_name,
        dimensions,
    )
    text = _build_text(cfg.get("retrieval_text", "native"), index_dir)
    fuser = _build_fuser(cfg.get("retrieval_fusion", "custom"))
    return RetrievalBackend(vector=vector, text=text, fuser=fuser)


def _grafeo_path(index_dir: pathlib.Path) -> pathlib.Path:
    """Per-agent Grafeo retrieval DB lives next to the FAISS state dir.

    Co-locating under <index_dir>/grafeo/ keeps the rollback story simple
    (delete one directory) and parallels the SQLite/Grafeo split for the
    graph backend.
    """
    return index_dir / "grafeo"


def _build_vector(
    kind: str,
    index_dir: pathlib.Path,
    model_name: str,
    dimensions: int,
) -> VectorIndex:
    if kind == "native":
        return NativeVectorIndex(
            index_dir, model_name=model_name, dimensions=dimensions
        )
    if kind == "grafeo":
        return GrafeoVectorIndex(_grafeo_path(index_dir), dimensions=dimensions)
    raise ValueError(
        f"Unknown retrieval_vector={kind!r}. Valid: 'native', 'grafeo'."
    )


def _build_text(kind: str, index_dir: pathlib.Path) -> TextIndex:
    if kind == "native":
        return NativeTextIndex(index_dir)
    if kind == "grafeo":
        return GrafeoTextIndex(_grafeo_path(index_dir))
    raise ValueError(
        f"Unknown retrieval_text={kind!r}. Valid: 'native', 'grafeo'."
    )


def _build_fuser(kind: str) -> HybridFuser:
    if kind == "custom":
        return NativeHybridFuser()
    if kind == "grafeo":
        return GrafeoHybridFuser()
    raise ValueError(
        f"Unknown retrieval_fusion={kind!r}. Valid: 'custom', 'grafeo'."
    )
