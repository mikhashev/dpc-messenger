"""Retrieval backend factory (ADR-024 Phase 1.6a).

Config-driven builder. Phase 1.6a ships only the "native" branch for each
of the three components; unknown values fall back to native with a log
warning. Phase 1.6b will add the "grafeo" branches and surface a
config-validation error instead of silent fallback.

Config schema (all optional, default native):
    retrieval_vector: "native" | "grafeo"   # vector channel
    retrieval_text:   "native" | "grafeo"   # text channel
    retrieval_fusion: "custom" | "grafeo"   # fuser
"""

from __future__ import annotations

import logging
import pathlib
from typing import Optional

from .base import HybridFuser, RetrievalBackend, TextIndex, VectorIndex
from .native import NativeHybridFuser, NativeTextIndex, NativeVectorIndex

log = logging.getLogger(__name__)


def build_retrieval_backend(
    index_dir: pathlib.Path,
    config: Optional[dict] = None,
    model_name: str = "",
    dimensions: int = 384,
) -> RetrievalBackend:
    """Compose a RetrievalBackend from config flags.

    See module docstring for the config schema. Phase 1.6a returns a
    fully-native backend regardless of input (unknown values fall back
    with a warning), so callers can already pass Phase 1.6b configs
    without crashing.
    """
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
    log.warning(
        "retrieval_vector=%r not implemented in Phase 1.6a, using native",
        kind,
    )
    return NativeVectorIndex(
        index_dir, model_name=model_name, dimensions=dimensions
    )


def _build_text(kind: str, index_dir: pathlib.Path) -> TextIndex:
    if kind == "native":
        return NativeTextIndex(index_dir)
    log.warning(
        "retrieval_text=%r not implemented in Phase 1.6a, using native",
        kind,
    )
    return NativeTextIndex(index_dir)


def _build_fuser(kind: str) -> HybridFuser:
    if kind == "custom":
        return NativeHybridFuser()
    log.warning(
        "retrieval_fusion=%r not implemented in Phase 1.6a, using custom RRF",
        kind,
    )
    return NativeHybridFuser()
