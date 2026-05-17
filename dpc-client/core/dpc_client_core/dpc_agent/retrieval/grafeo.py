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

- **GrafeoHybridFuser is a thin re-export of the RRF math** from
  hybrid_search.reciprocal_rank_fusion. Grafeo's native db.hybrid_search()
  takes the raw queries (text + vector) rather than already-fused results,
  which doesn't fit the current HybridFuser ABC signature. Wiring the
  native pathway would require an ABC extension (queries passed through)
  — deferred to Phase 1.6c. Until then, "grafeo" fusion is behaviorally
  identical to "custom".

Grafeo v0.5.42 caveats applied:

- Vector / text indices are in-memory only; rebuilt after bulk insert
  and on every open (`rebuild_vector_index`, `rebuild_text_index`).
- BM25 parameters (k1=1.2, Lucene IDF) hardcoded upstream — see Phase A
  benchmark report for the divergence-from-bm25s analysis.
- needs_rebuild(model_name) always returns False — Grafeo doesn't track
  embedding-model identifier yet. TODO 1.6c+: store a Schema node.
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
    try:
        rows = list(db.execute_cypher(f"MATCH (n:{label}) RETURN count(n) AS c"))
    except Exception as e:
        log.debug("count query failed for label %s: %s", label, e)
        return 0
    if not rows:
        return 0
    row = rows[0]
    # Cypher result rows expose .get(name) returning a Value wrapper.
    try:
        return int(row.get("c").as_int() or 0)
    except Exception:
        try:
            return int(row["c"])
        except Exception:
            return 0


class GrafeoVectorIndex(VectorIndex):
    """Vector index backed by Grafeo native HNSW (label ChunkVector)."""

    def __init__(
        self,
        db_path: pathlib.Path,
        dimensions: int = 384,
        m: int = 16,
        ef_construction: int = 200,
    ):
        self._db_path = db_path
        self._dimensions = dimensions
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
        # Grafeo doesn't store the embedding-model identifier today. The
        # model-swap UX (model_swap.py) won't detect changes on Grafeo
        # storage until 1.6c+ adds a Schema node carrying model_name.
        return False


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
    """Currently identical to NativeHybridFuser — same RRF math.

    Grafeo's native db.hybrid_search() takes the raw query inputs (text +
    vector) rather than fused channel results, which doesn't fit the
    HybridFuser ABC signature. Routing the native pathway is a 1.6c task
    (ABC extension to pass-through queries). Until then, choosing
    `retrieval_fusion: "grafeo"` simply selects this subclass, leaving
    the behavior bit-for-bit equal to "custom".
    """
    pass
