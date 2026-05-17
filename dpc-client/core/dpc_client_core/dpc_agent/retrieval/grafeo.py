"""Grafeo retrieval implementations (ADR-024 Phase 1.6b.2).

Wraps grafeo.GrafeoDB native vector / text search behind the VectorIndex /
TextIndex / HybridFuser ABCs from base.py.

Design choices for Phase 1.6b.2:

- **Separate DB per agent under `state/grafeo_retrieval/`** rather than
  reusing the knowledge-graph DB. Lets users mix backends independently
  (graph SQLite + retrieval Grafeo, etc). Unification is a 1.6c+ concern.

- **Separate labels (`ChunkVector`, `ChunkText`)** for the two channels.
  No shared-state coordination between VectorIndex and TextIndex; each
  owns its own node domain. Channels are fused by source_file in the
  HybridFuser layer (same dedup pattern as Native).

- **GrafeoHybridFuser intentionally inherits NativeHybridFuser unchanged
  (1.6c decision).** Grafeo's native db.hybrid_search() does RRF too, but
  it does not know about our per-layer source priority weights (L1/L5/L6
  /L7 — see LAYER_WEIGHTS in hybrid_search.py). Those weights are DPC
  policy, not a backend concern; routing through Grafeo's fuser would
  silently drop the policy. Channel-result fusion (RRF + layer weights +
  source_file dedup) therefore stays in the Native fuser regardless of
  which backend produced the channel results. `retrieval_fusion: "grafeo"`
  remains a valid config flag for symmetry and future
  layer-weight-aware Grafeo support, but currently behaves identically
  to `"custom"`.

Grafeo v0.5.42 caveats applied:

- Vector / text indices are in-memory only; rebuilt after bulk insert
  and on every open (`rebuild_vector_index`, `rebuild_text_index`).
- BM25 parameters (k1=1.2, Lucene IDF) hardcoded upstream — see Phase A
  benchmark report for the divergence-from-bm25s analysis.
- needs_rebuild(model_name) is functional as of 1.6c: a singleton
  `_RetrievalSchema` node carries the embedding-model identifier so the
  model-swap UX can detect a mismatch on Grafeo storage too.
"""

from __future__ import annotations

import logging
import pathlib
from typing import List, Optional, Tuple

import numpy as np

from .base import (
    FusionResult,
    HybridFuser,
    TextAddItem,
    TextIndex,
    VectorAddItem,
    VectorIndex,
)
from .native import NativeHybridFuser

log = logging.getLogger(__name__)

VECTOR_LABEL = "ChunkVector"
TEXT_LABEL = "ChunkText"
VECTOR_PROPERTY = "embedding"
TEXT_PROPERTY = "text_preprocessed"
RAW_TEXT_PROPERTY = "text"

# Singleton metadata node — carries the embedding-model identifier so that
# GrafeoVectorIndex.needs_rebuild(model_name) can detect a mismatch the same
# way NativeVectorIndex does via the FAISS index_meta.json header.
SCHEMA_LABEL = "_RetrievalSchema"


# Process-wide cache mirroring knowledge_graph._grafeo_instance_cache pattern
# (commit 07a6219) — Grafeo doesn't tolerate multiple handles on the same
# on-disk path within one process.
_grafeo_retrieval_db_cache: dict = {}


def _open_grafeo(db_path: pathlib.Path):
    """Open or return cached GrafeoDB at the given path. `:memory:` is uncached."""
    try:
        import grafeo
    except ImportError as e:
        raise ImportError(
            "Grafeo retrieval requires the `grafeo` package. "
            "Install with: uv sync --extra graph-grafeo"
        ) from e
    if str(db_path) == ":memory:":
        return grafeo.GrafeoDB()
    key = str(db_path.resolve())
    cached = _grafeo_retrieval_db_cache.get(key)
    if cached is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        cached = grafeo.GrafeoDB(key)
        _grafeo_retrieval_db_cache[key] = cached
    return cached


def _node_str(node, name: str) -> str:
    try:
        v = node.get(name)
        if v is None or v.is_null():
            return ""
        return v.as_str() or ""
    except Exception:
        return ""


def _node_int(node, name: str) -> int:
    try:
        v = node.get(name)
        if v is None or v.is_null():
            return 0
        return int(v.as_int() or 0)
    except Exception:
        return 0


def _chunk_meta_from_node(db, node_id) -> Optional[dict]:
    """Reconstruct the chunk_meta dict from a Grafeo node id."""
    try:
        node = db.get_node(node_id)
    except Exception:
        return None
    return {
        "source_file": _node_str(node, "source_file"),
        "source_layer": _node_str(node, "source_layer") or "L5",
        "heading": _node_str(node, "heading"),
        "char_count": _node_int(node, "char_count"),
        "text": _node_str(node, RAW_TEXT_PROPERTY)[:500],
    }


def _node_count(db, label: str) -> int:
    # execute_cypher yields plain Python dicts with native values
    # (`{'c': 5}`) — different from db.get_node() which returns a Node
    # whose .get(name) is a Value wrapper. See _node_str/_node_int below
    # for the get_node API path.
    try:
        rows = list(db.execute_cypher(f"MATCH (n:{label}) RETURN count(n) AS c"))
    except Exception as e:
        log.debug("count query failed for label %s: %s", label, e)
        return 0
    if not rows:
        return 0
    val = rows[0].get("c")
    return int(val) if val is not None else 0


class GrafeoVectorIndex(VectorIndex):
    """Vector index backed by Grafeo native HNSW (label ChunkVector)."""

    def __init__(
        self,
        db_path: pathlib.Path,
        dimensions: int = 384,
        model_name: str = "",
        m: int = 16,
        ef_construction: int = 200,
    ):
        self._db_path = db_path
        self._dimensions = dimensions
        self._model_name = model_name
        self._m = m
        self._ef_construction = ef_construction
        self._db = None
        self._schema_initialized = False

    def _get_db(self):
        if self._db is None:
            self._db = _open_grafeo(self._db_path)
        return self._db

    def _ensure_schema(self) -> None:
        """Idempotent — create_vector_index raises if already defined; we swallow."""
        if self._schema_initialized:
            return
        db = self._get_db()
        try:
            db.create_vector_index(
                label=VECTOR_LABEL,
                property=VECTOR_PROPERTY,
                dimensions=self._dimensions,
                metric="cosine",
                m=self._m,
                ef_construction=self._ef_construction,
            )
        except Exception as e:
            log.debug("create_vector_index raised (likely exists): %s", e)
        self._schema_initialized = True

    def add(self, items: List[VectorAddItem]) -> None:
        if not items:
            return
        db = self._get_db()
        self._ensure_schema()
        # Record the embedding model behind these vectors on first content
        # write — anchors the needs_rebuild() check on subsequent opens.
        if self._model_name:
            self._set_stored_model(self._model_name)
        for item in items:
            vec = item.vector
            if vec.ndim > 1:
                vec = vec.reshape(-1)
            db.create_node(
                labels=[VECTOR_LABEL],
                properties={
                    VECTOR_PROPERTY: vec.astype(np.float32).tolist(),
                    "source_file": item.meta.get("source_file", ""),
                    "source_layer": item.meta.get("source_layer", "L5"),
                    "heading": item.meta.get("heading", ""),
                    "char_count": int(item.meta.get("char_count", 0)),
                    RAW_TEXT_PROPERTY: str(item.meta.get("text", ""))[:500],
                },
            )
        # Rebuild required after insert for search to see the new data.
        db.rebuild_vector_index(label=VECTOR_LABEL, property=VECTOR_PROPERTY)

    def _set_stored_model(self, model_name: str) -> None:
        """Write/refresh the singleton _RetrievalSchema node with model_name."""
        try:
            self._get_db().execute_cypher(
                f"MERGE (s:{SCHEMA_LABEL} {{singleton: 1}}) SET s.model_name = $m",
                {"m": model_name},
            )
        except Exception as e:
            log.debug("schema write failed: %s", e)

    def _get_stored_model(self) -> str:
        """Read embedding-model identifier from the singleton Schema node.

        Grafeo's execute_cypher yields plain dict rows with native Python
        values (verified empirically — `{'m': 'model-a'}`). The Value
        wrapper / .is_null() / .as_str() pattern in run_grafeo_native.py is
        only the get_node() property API, not the cypher row API.
        """
        try:
            rows = list(self._get_db().execute_cypher(
                f"MATCH (s:{SCHEMA_LABEL}) RETURN s.model_name AS m LIMIT 1"
            ))
        except Exception:
            return ""
        if not rows:
            return ""
        val = rows[0].get("m")
        if val is None:
            return ""
        return str(val)

    def search(self, query_vector: np.ndarray, top_k: int) -> List[Tuple[dict, float]]:
        db = self._get_db()
        if _node_count(db, VECTOR_LABEL) == 0:
            return []
        self._ensure_schema()
        vec = query_vector
        if vec.ndim > 1:
            vec = vec.reshape(-1)
        try:
            rows = db.vector_search(
                label=VECTOR_LABEL,
                property=VECTOR_PROPERTY,
                query=vec.astype(np.float32).tolist(),
                k=top_k,
            )
        except Exception as e:
            log.warning("Grafeo vector_search failed: %s", e)
            return []
        out: List[Tuple[dict, float]] = []
        seen_files: set = set()
        for row in rows:
            try:
                node_id, score = row[0], float(row[1])
            except (TypeError, IndexError):
                continue
            meta = _chunk_meta_from_node(db, node_id)
            if meta is None:
                continue
            sf = meta.get("source_file", "")
            if sf and sf in seen_files:
                continue
            seen_files.add(sf)
            out.append((meta, score))
        return out

    def remove_by_source(self, source_file: str) -> int:
        db = self._get_db()
        before = _node_count(db, VECTOR_LABEL)
        try:
            db.execute_cypher(
                f"MATCH (n:{VECTOR_LABEL} {{source_file: $sf}}) DETACH DELETE n",
                {"sf": source_file},
            )
        except Exception as e:
            log.warning("Grafeo remove_by_source failed: %s", e)
            return 0
        after = _node_count(db, VECTOR_LABEL)
        removed = max(0, before - after)
        if removed:
            try:
                db.rebuild_vector_index(label=VECTOR_LABEL, property=VECTOR_PROPERTY)
            except Exception as e:
                log.debug("rebuild after remove failed: %s", e)
        return removed

    def save(self) -> None:
        # Grafeo is self-persistent — no-op per ABC contract.
        return

    def load(self) -> bool:
        db = self._get_db()
        if _node_count(db, VECTOR_LABEL) == 0:
            return False
        self._ensure_schema()
        # v0.5.42 caveat: indices in-memory only — rebuild on open.
        try:
            db.rebuild_vector_index(label=VECTOR_LABEL, property=VECTOR_PROPERTY)
        except Exception as e:
            log.debug("rebuild_vector_index on load failed: %s", e)
        return True

    def clear(self) -> None:
        db = self._get_db()
        try:
            db.execute_cypher(f"MATCH (n:{VECTOR_LABEL}) DETACH DELETE n")
        except Exception as e:
            log.warning("Grafeo clear failed: %s", e)

    @property
    def total_items(self) -> int:
        return _node_count(self._get_db(), VECTOR_LABEL)

    def needs_rebuild(self, model_name: str) -> bool:
        # Compare requested model_name against the identifier stored on the
        # singleton _RetrievalSchema node (written on first add()). Empty
        # `model_name` arg or empty stored value (no Schema yet → first run)
        # both return False — no detectable swap means no rebuild needed.
        if not model_name:
            return False
        stored = self._get_stored_model()
        if not stored:
            return False
        return stored != model_name


class GrafeoTextIndex(TextIndex):
    """Text index backed by Grafeo native BM25 (label ChunkText)."""

    def __init__(self, db_path: pathlib.Path):
        self._db_path = db_path
        self._db = None
        self._schema_initialized = False

    def _get_db(self):
        if self._db is None:
            self._db = _open_grafeo(self._db_path)
        return self._db

    def _ensure_schema(self) -> None:
        if self._schema_initialized:
            return
        db = self._get_db()
        try:
            db.create_text_index(label=TEXT_LABEL, property=TEXT_PROPERTY)
        except Exception as e:
            log.debug("create_text_index raised (likely exists): %s", e)
        self._schema_initialized = True

    def add(self, items: List[TextAddItem]) -> None:
        if not items:
            return
        # Match the preprocessing the benchmark used so query-time and
        # index-time agree (bm25_index.tokenize → stopwords-iso + script).
        from ..bm25_index import tokenize as bm25_tokenize
        db = self._get_db()
        self._ensure_schema()
        for item in items:
            preprocessed = " ".join(bm25_tokenize(item.text))
            db.create_node(
                labels=[TEXT_LABEL],
                properties={
                    TEXT_PROPERTY: preprocessed,
                    RAW_TEXT_PROPERTY: item.text[:500],
                    "source_file": item.meta.get("source_file", ""),
                    "source_layer": item.meta.get("source_layer", "L5"),
                    "heading": item.meta.get("heading", ""),
                    "char_count": int(item.meta.get("char_count", 0)),
                },
            )
        db.rebuild_text_index(label=TEXT_LABEL, property=TEXT_PROPERTY)

    def search(self, query: str, top_k: int) -> List[Tuple[dict, float]]:
        db = self._get_db()
        if _node_count(db, TEXT_LABEL) == 0:
            return []
        self._ensure_schema()
        from ..bm25_index import tokenize as bm25_tokenize
        preprocessed_query = " ".join(bm25_tokenize(query))
        if not preprocessed_query:
            return []
        try:
            rows = db.text_search(
                label=TEXT_LABEL,
                property=TEXT_PROPERTY,
                query=preprocessed_query,
                k=top_k,
            )
        except Exception as e:
            log.warning("Grafeo text_search failed: %s", e)
            return []
        out: List[Tuple[dict, float]] = []
        seen_files: set = set()
        for row in rows:
            try:
                node_id, score = row[0], float(row[1])
            except (TypeError, IndexError):
                continue
            meta = _chunk_meta_from_node(db, node_id)
            if meta is None:
                continue
            sf = meta.get("source_file", "")
            if sf and sf in seen_files:
                continue
            seen_files.add(sf)
            out.append((meta, score))
        return out

    def remove_by_source(self, source_file: str) -> int:
        db = self._get_db()
        before = _node_count(db, TEXT_LABEL)
        try:
            db.execute_cypher(
                f"MATCH (n:{TEXT_LABEL} {{source_file: $sf}}) DETACH DELETE n",
                {"sf": source_file},
            )
        except Exception as e:
            log.warning("Grafeo remove_by_source failed: %s", e)
            return 0
        after = _node_count(db, TEXT_LABEL)
        removed = max(0, before - after)
        if removed:
            try:
                db.rebuild_text_index(label=TEXT_LABEL, property=TEXT_PROPERTY)
            except Exception as e:
                log.debug("rebuild after remove failed: %s", e)
        return removed

    def save(self) -> None:
        return  # self-persistent

    def load(self) -> bool:
        db = self._get_db()
        if _node_count(db, TEXT_LABEL) == 0:
            return False
        self._ensure_schema()
        try:
            db.rebuild_text_index(label=TEXT_LABEL, property=TEXT_PROPERTY)
        except Exception as e:
            log.debug("rebuild_text_index on load failed: %s", e)
        return True

    def clear(self) -> None:
        db = self._get_db()
        try:
            db.execute_cypher(f"MATCH (n:{TEXT_LABEL}) DETACH DELETE n")
        except Exception as e:
            log.warning("Grafeo clear failed: %s", e)

    @property
    def total_items(self) -> int:
        return _node_count(self._get_db(), TEXT_LABEL)


class GrafeoHybridFuser(NativeHybridFuser):
    """Intentionally identical to NativeHybridFuser — see module docstring.

    The decision NOT to route through Grafeo's native db.hybrid_search() is
    deliberate, not a deferred-work item. Grafeo's fuser does not know
    about our per-layer priority weights (LAYER_WEIGHTS in
    hybrid_search.py — L1/L5/L6/L7 etc), which are DPC policy. Routing
    fusion through Grafeo would silently drop those weights regardless of
    any ABC extension. `retrieval_fusion: "grafeo"` remains a config slot
    for symmetry and to leave room for layer-weight-aware Grafeo support
    later, but right now it does the same RRF the Native fuser does.
    """
    pass
