"""Retrieval backend factory (ADR-024 Phase 1.6b.2 + 1.6d wiring).

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

Two entry points:
- `build_retrieval_backend(index_dir, config, ...)` — caller supplies
  index_dir and the config dict explicitly. Used by tests and any caller
  with its own config source.
- `make_backend_for_agent(agent_root, ...)` — reads `<agent_root>/config.json`,
  extracts `retrieval_*` keys, derives `index_dir = agent_root/state/memory_index`,
  delegates to `build_retrieval_backend`. The path production code uses
  (1.6d wiring) so config flags in agent's config.json actually take
  effect.
"""

from __future__ import annotations

import json
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
    """Compose a RetrievalBackend from an explicit config dict. See module docstring."""
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


def make_backend_for_agent(
    agent_root: pathlib.Path,
    model_name: str = "",
    dimensions: int = 384,
) -> RetrievalBackend:
    """Build a RetrievalBackend for an agent, reading `retrieval_*` flags from its config.json.

    This is the production entry point — every call-site that used to call
    `make_native_backend(index_dir)` directly should switch to this so that
    `retrieval_vector` / `retrieval_text` / `retrieval_fusion` keys in
    `<agent_root>/config.json` actually take effect.

    Missing config.json or unreadable file → empty config → native default.
    """
    config_path = agent_root / "config.json"
    cfg: dict = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
        except (OSError, ValueError) as e:
            log.debug("Could not read %s, using empty config: %s", config_path, e)
            cfg = {}
    # Only pass through retrieval_* keys; everything else is unrelated.
    retrieval_cfg = {k: v for k, v in cfg.items() if k.startswith("retrieval_")}
    index_dir = agent_root / "state" / "memory_index"
    return build_retrieval_backend(
        index_dir,
        config=retrieval_cfg,
        model_name=model_name,
        dimensions=dimensions,
    )


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
        return GrafeoVectorIndex(
            _grafeo_path(index_dir),
            dimensions=dimensions,
            model_name=model_name,
        )
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
