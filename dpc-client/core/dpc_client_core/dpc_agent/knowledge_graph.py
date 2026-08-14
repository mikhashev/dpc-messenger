"""Knowledge Graph infrastructure (ADR-024).

DB-agnostic graph layer with SQLite fallback. Stores typed nodes and edges
representing structural relationships between knowledge files, sessions,
entities, decisions, and agents.

Graph traversal feeds the L7 channel in hybrid_search.py RRF fusion.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .index_keys import l5_key, l6_key


#: The `properties.source` markers every writer stamps its edges with. Counting by
#: them separates what an indexing pass rebuilds for free (structural), what a sleep
#: rebuilds from the cached findings (gliner_ner), and what has no source outside the
#: graph at all (llm_relation) — which is the only class where a missing edge is gone.
_SOURCE_MARKERS = ("structural", "gliner_ner", "llm_relation")

#: Version tag in every dump's header line. Bump it when a record shape changes in a
#: way an older reader would misread — a dump outlives the code that wrote it, and
#: this is the only thing that will tell the reader so.
DUMP_FORMAT = "dpc-kg-dump/1"


def _snapshot_dict(store_path, backend: str, nodes_by_type: dict, edges_by_type: dict,
                   kf_edges_by_type: dict, kf_structural: int,
                   kf_nodes_keyed: int, kf_nodes_legacy_stem: int,
                   edges_by_source: dict | None = None) -> dict:
    """One shape for both backends, so a reader compares numbers and not formats.

    `kf_edges_non_structural` is the number the whole key-as-id migration rested on:
    structural edges are cleared and rebuilt on every indexing pass, so they survive a
    change of node identity by being written again — anything else attached to a
    knowledge-file node does not, and would be left pointing at a node no query
    reaches. Note that an orphaned edge is still an edge: the total count does not move
    when one is orphaned, which is why the count alone proves nothing and this
    breakdown exists.
    """
    kf_edges_total = sum(kf_edges_by_type.values())
    return {
        "store_path": str(store_path),
        "backend": backend,
        "nodes_total": sum(nodes_by_type.values()),
        "edges_total": sum(edges_by_type.values()),
        "nodes_by_type": dict(sorted(nodes_by_type.items())),
        "edges_by_type": dict(sorted(edges_by_type.items())),
        "kf_edges_total": kf_edges_total,
        "kf_edges_by_type": dict(sorted(kf_edges_by_type.items())),
        "kf_edges_structural": kf_structural,
        "kf_edges_non_structural": kf_edges_total - kf_structural,
        "kf_nodes_keyed": kf_nodes_keyed,
        "kf_nodes_legacy_stem": kf_nodes_legacy_stem,
        "edges_by_source": dict(sorted((edges_by_source or {}).items())),
    }


def node_id_for(index_key: str) -> str:
    """A knowledge file's node id: its index key, which is already its identity.

    The graph used to address documents by stem, which is a name rather than an
    identity — two layers could hold one stem, a seed key had to be cut down to match,
    and neither the fuser nor the decay counter could line a graph result up with the
    same document from another channel. Keying on the index key makes those questions
    disappear instead of guarding them: the key is what the index, the fuser, the
    counter and read_file all already mean by "this document".
    """
    return f"kf:{index_key}"

log = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NodeType(str, Enum):
    KNOWLEDGE_FILE = "KnowledgeFile"
    SESSION_ARCHIVE = "SessionArchive"
    ENTITY = "Entity"
    DECISION = "Decision"
    AGENT = "Agent"


class EdgeType(str, Enum):
    DERIVED_FROM = "DERIVED_FROM"
    DEPENDS_ON = "DEPENDS_ON"
    RESPONDS_TO = "RESPONDS_TO"
    CONTRADICTS = "CONTRADICTS"
    SUPPORTS = "SUPPORTS"
    DECIDED_BY = "DECIDED_BY"
    SHARED_WITH = "SHARED_WITH"
    MENTIONS = "MENTIONS"
    TEMPORAL_NEXT = "TEMPORAL_NEXT"


ALWAYS_EXEMPT = {NodeType.DECISION, NodeType.SESSION_ARCHIVE}

DEFAULT_ENTITY_TYPES = ["person", "organization", "technology", "concept", "location"]
GLINER_MODEL_NAME = "urchade/gliner_multi-v2.1"
GLINER_THRESHOLD = 0.5
GLINER_MAX_TEXT_LEN = 5000

# Module-level GLiNER singleton — mirrors the BGE-M3 embedding singleton
# pattern from S105 (get_embedding_provider). Loading the model is ~2 GB
# of RAM and ~30 s of first-time download; parallel group sleep would
# otherwise instantiate it once per agent. Double-checked locking under
# threading.Lock guards init; after _GLINER_MODEL is set, every reader is
# a pure attribute read (safe under the GIL).
_GLINER_MODEL: Any = None
_GLINER_LOAD_LOCK = threading.Lock()


def _get_gliner_model():
    """Return the process-wide GLiNER model, loading lazily on first call.

    Returns None if `gliner` is not installed — callers should treat the
    absence as "skip NER", mirroring the ImportError path in
    extract_entities_gliner. Safe to call from any thread (including
    asyncio worker threads via to_thread()).
    """
    global _GLINER_MODEL
    if _GLINER_MODEL is not None:
        return _GLINER_MODEL
    with _GLINER_LOAD_LOCK:
        if _GLINER_MODEL is not None:
            return _GLINER_MODEL
        try:
            from gliner import GLiNER
            import torch
        except ImportError:
            log.debug("GLiNER not installed — skip entity extraction (install with: uv sync --extra graph-ner)")
            return None
        device = "cuda" if torch.cuda.is_available() else "cpu"
        log.info("Loading GLiNER model %s on %s (first use, process-wide singleton)...", GLINER_MODEL_NAME, device)
        _GLINER_MODEL = GLiNER.from_pretrained(GLINER_MODEL_NAME).to(device)
        log.info("GLiNER model loaded on %s", device)
        return _GLINER_MODEL


@dataclass
class GraphNode:
    node_id: str
    node_type: NodeType
    label: str
    source_layer: str = "L7"
    exempt: bool = False
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    source_id: str
    target_id: str
    edge_type: EdgeType
    t_created: str = ""
    t_invalidated: Optional[str] = None
    confidence: float = 1.0
    justification: str = ""
    edge_weight: str = "medium"
    properties: Dict[str, Any] = field(default_factory=dict)


class GraphBackend(ABC):
    """Abstract graph storage interface."""

    @abstractmethod
    def init_schema(self) -> None: ...

    @abstractmethod
    def add_node(self, node: GraphNode) -> None: ...

    @abstractmethod
    def add_edge(self, edge: GraphEdge) -> None: ...

    @abstractmethod
    def get_node(self, node_id: str) -> Optional[GraphNode]: ...

    @abstractmethod
    def get_neighbors(self, node_id: str, edge_type: Optional[EdgeType] = None, hops: int = 1) -> List[GraphNode]: ...

    @abstractmethod
    def get_edges(self, node_id: str, direction: str = "both") -> List[GraphEdge]: ...

    @abstractmethod
    def node_count(self) -> int: ...

    @abstractmethod
    def edge_count(self) -> int: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def edge_exists(self, source_id: str, target_id: str, edge_type: EdgeType) -> bool:
        """Return True iff ANY edge of given type connects source_id → target_id,
        regardless of t_invalidated. Active-only filtering can be added later as
        a separate method or include_invalidated parameter."""
        ...

    @abstractmethod
    def snapshot(self) -> dict:
        """Counts of what is actually in this store, for someone auditing a claim.

        Exists because the store cannot be read from outside: the file is held with
        an exclusive lock, and on Windows it cannot even be copied while the backend
        runs. Three analyses in a row therefore measured a SQLite file last written in
        May and reported it as the live graph — not for want of care, but because it
        was the only thing openable. The answer has to come from the process holding
        the database, and nothing else will do.

        Keys are stable and dumb on purpose: this output is meant to be handed to
        someone who did not write the code and asked to check a number.
        """
        ...

    @abstractmethod
    def iter_nodes(self) -> Iterator[GraphNode]:
        """Every node in the store, once each; order is not part of the contract.

        SQLite sorts by primary key and Grafeo returns whatever its MATCH yields, so
        two dumps of the same graph agree as sets and need not agree line by line —
        which is what any comparison of them has to be written against.

        The one thing this interface could not do until 2026-08-14, and the reason a
        graph could not be moved, backed up or read from outside the process holding
        it. Both backends had the primitive one level down the whole time — two
        SELECTs, or one MATCH — it was simply never lifted to where callers live.
        Generators, not lists: the dump streams, and nothing needs the graph twice
        over in memory.
        """
        ...

    @abstractmethod
    def iter_edges(self) -> Iterator[GraphEdge]:
        """Every edge in the store, endpoints named by node_id rather than by
        whatever internal identifier the backend uses — that is what makes a dump
        portable between the two."""
        ...

    def wal_info(self) -> dict:
        """What the store's write-ahead log looks like right now, or {} if it has none.

        Not abstract: a backend that commits synchronously has nothing to report and
        should not be forced to say so. Exists because the counts at open have
        repeatedly disagreed with the counts a pass left behind, and every explanation
        offered for that so far has been an inference about WAL behaviour that nobody
        could see. This is the line that turns the next restart into a measurement.
        """
        return {}

    @abstractmethod
    def clear_structural_edges(self) -> int:
        """Delete all structural edges. Returns rows deleted. Property-matching
        and any legacy markers are backend-internal — fasade does not know SQL
        LIKE patterns."""
        ...

    @abstractmethod
    def update_edge_timestamp_for_node(self, node_id: str, field: str, value: str) -> int:
        """Set `field` to `value` on edges touching node_id where `field` is
        empty/null. `field` ∈ {"t_invalidated", "t_created"}. Empty predicate
        is field-specific (t_invalidated uses IS NULL; t_created uses ='' OR IS
        NULL for backward compat with pre-bi-temporal rows). Returns rows affected."""
        ...

    @abstractmethod
    def bulk_upsert_entities_with_mentions(
        self,
        entities: List[dict],
        t_created: str,
        entity_exempt: bool,
    ) -> tuple[int, set[str]]:
        """Upsert Entity nodes and MENTIONS edges from GLiNER output. Idempotent.
        Backend MAY implement as single transaction (SQLite) or as ordered
        operations (other backends) — caller treats as logical batch with
        skip-orphan semantics: edges to source_ids absent from graph are skipped
        and reported in the returned orphan set for caller logging.
        Returns (edges_added, orphan_source_ids)."""
        ...


class SQLiteGraphBackend(GraphBackend):
    """SQLite-based graph storage. Zero external dependencies."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def init_schema(self) -> None:
        c = self._conn
        c.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                node_type TEXT NOT NULL,
                label TEXT NOT NULL,
                source_layer TEXT DEFAULT 'L7',
                exempt INTEGER DEFAULT 0,
                properties TEXT DEFAULT '{}'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                t_created TEXT DEFAULT '',
                t_invalidated TEXT,
                confidence REAL DEFAULT 1.0,
                justification TEXT DEFAULT '',
                edge_weight TEXT DEFAULT 'medium',
                properties TEXT DEFAULT '{}',
                FOREIGN KEY (source_id) REFERENCES nodes(node_id),
                FOREIGN KEY (target_id) REFERENCES nodes(node_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type)")
        c.commit()

    def add_node(self, node: GraphNode) -> None:
        # `with self._conn:` auto-commits on success, rolls back on exception.
        # Prevents orphan transactions holding the WAL write lock when an
        # INSERT fails (e.g. FK violation in add_edge), which would otherwise
        # produce "database is locked" for any subsequent KG writer.
        exempt = 1 if (node.exempt or node.node_type in ALWAYS_EXEMPT) else 0
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO nodes (node_id, node_type, label, source_layer, exempt, properties) VALUES (?,?,?,?,?,?)",
                (node.node_id, node.node_type.value, node.label, node.source_layer, exempt, json.dumps(node.properties, ensure_ascii=False)),
            )

    def add_edge(self, edge: GraphEdge) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO edges (source_id, target_id, edge_type, t_created, t_invalidated, confidence, justification, edge_weight, properties) VALUES (?,?,?,?,?,?,?,?,?)",
                (edge.source_id, edge.target_id, edge.edge_type.value, edge.t_created, edge.t_invalidated, edge.confidence, edge.justification, edge.edge_weight, json.dumps(edge.properties, ensure_ascii=False)),
            )

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        row = self._conn.execute("SELECT node_id, node_type, label, source_layer, exempt, properties FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        if not row:
            return None
        return GraphNode(
            node_id=row[0], node_type=NodeType(row[1]), label=row[2],
            source_layer=row[3], exempt=bool(row[4]),
            properties=json.loads(row[5]) if row[5] else {},
        )

    def get_neighbors(self, node_id: str, edge_type: Optional[EdgeType] = None, hops: int = 1) -> List[GraphNode]:
        visited: set = set()
        frontier = {node_id}
        for _ in range(hops):
            next_frontier: set = set()
            for nid in frontier:
                if edge_type:
                    rows = self._conn.execute(
                        "SELECT target_id FROM edges WHERE source_id=? AND edge_type=? UNION SELECT source_id FROM edges WHERE target_id=? AND edge_type=?",
                        (nid, edge_type.value, nid, edge_type.value),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT target_id FROM edges WHERE source_id=? UNION SELECT source_id FROM edges WHERE target_id=?",
                        (nid, nid),
                    ).fetchall()
                for (rid,) in rows:
                    if rid not in visited and rid != node_id:
                        next_frontier.add(rid)
            visited |= frontier
            frontier = next_frontier
            if not frontier:
                break
        result = []
        for nid in frontier | (visited - {node_id}):
            node = self.get_node(nid)
            if node:
                result.append(node)
        return result

    def get_edges(self, node_id: str, direction: str = "both") -> List[GraphEdge]:
        results = []
        if direction in ("out", "both"):
            for row in self._conn.execute("SELECT source_id, target_id, edge_type, t_created, t_invalidated, confidence, justification, edge_weight, properties FROM edges WHERE source_id=?", (node_id,)):
                results.append(self._row_to_edge(row))
        if direction in ("in", "both"):
            for row in self._conn.execute("SELECT source_id, target_id, edge_type, t_created, t_invalidated, confidence, justification, edge_weight, properties FROM edges WHERE target_id=?", (node_id,)):
                results.append(self._row_to_edge(row))
        return results

    def node_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    def edge_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def edge_exists(self, source_id: str, target_id: str, edge_type: EdgeType) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND edge_type=? LIMIT 1",
            (source_id, target_id, edge_type.value),
        ).fetchone()
        return row is not None

    def iter_nodes(self) -> Iterator[GraphNode]:
        for row in self._conn.execute(
            "SELECT node_id, node_type, label, source_layer, exempt, properties "
            "FROM nodes ORDER BY node_id"
        ):
            yield GraphNode(
                node_id=row[0], node_type=NodeType(row[1]), label=row[2],
                source_layer=row[3], exempt=bool(row[4]),
                properties=json.loads(row[5]) if row[5] else {},
            )

    def iter_edges(self) -> Iterator[GraphEdge]:
        for row in self._conn.execute(
            "SELECT source_id, target_id, edge_type, t_created, t_invalidated, "
            "confidence, justification, edge_weight, properties FROM edges ORDER BY rowid"
        ):
            yield self._row_to_edge(row)

    def snapshot(self) -> dict:
        c = self._conn
        nodes_by_type = {r[0]: r[1] for r in c.execute(
            "SELECT node_type, count(*) FROM nodes GROUP BY node_type")}
        edges_by_type = {r[0]: r[1] for r in c.execute(
            "SELECT edge_type, count(*) FROM edges GROUP BY edge_type")}
        kf = "kf:%"
        kf_edges_by_type = {r[0]: r[1] for r in c.execute(
            "SELECT edge_type, count(*) FROM edges "
            "WHERE source_id LIKE ? OR target_id LIKE ? GROUP BY edge_type", (kf, kf))}
        structural = c.execute(
            "SELECT count(*) FROM edges WHERE (source_id LIKE ? OR target_id LIKE ?) "
            "AND (properties LIKE '%\"source\": \"structural\"%' "
            "OR properties LIKE '%\"source\":\"structural\"%')", (kf, kf)).fetchone()[0]
        keyed = c.execute(
            "SELECT count(*) FROM nodes WHERE node_id LIKE ? AND node_id LIKE '%/%'",
            (kf,)).fetchone()[0]
        legacy = c.execute(
            "SELECT count(*) FROM nodes WHERE node_id LIKE ? AND node_id NOT LIKE '%/%'",
            (kf,)).fetchone()[0]
        edges_by_source = {}
        for src in _SOURCE_MARKERS:
            n = c.execute(
                "SELECT count(*) FROM edges WHERE properties LIKE ? OR properties LIKE ?",
                (f'%"source": "{src}"%', f'%"source":"{src}"%')).fetchone()[0]
            if n:
                edges_by_source[src] = n
        unmarked = sum(edges_by_type.values()) - sum(edges_by_source.values())
        if unmarked:
            edges_by_source["(unmarked)"] = unmarked
        return _snapshot_dict(self._db_path, "sqlite", nodes_by_type, edges_by_type,
                              kf_edges_by_type, structural, keyed, legacy, edges_by_source)

    def clear_structural_edges(self) -> int:
        # Canonical marker is source=structural. Legacy edges (pre-KG-LLM-MARKER
        # fix) carry only auto=true with no source field — matched here so they
        # re-extract with the new marker on next startup. LLM relations and
        # GLiNER MENTIONS in the legacy state also get cleared; sleep pipeline
        # regenerates them with source=llm_relation / source=gliner_ner on the
        # next sleep cycle. Once all KGs migrate (after one sleep), the legacy
        # clauses can be removed — tracked in backlog as KG-LEGACY-CLEANUP.
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM edges WHERE "
                "properties LIKE '%\"source\": \"structural\"%' "
                "OR properties LIKE '%\"source\":\"structural\"%' "
                "OR ((properties LIKE '%\"auto\": true%' OR properties LIKE '%\"auto\":true%') "
                "AND properties NOT LIKE '%\"source\"%')"
            )
            return cursor.rowcount

    def update_edge_timestamp_for_node(self, node_id: str, field: str, value: str) -> int:
        if field == "t_invalidated":
            empty_predicate = "t_invalidated IS NULL"
        elif field == "t_created":
            # Legacy rows pre-dating bi-temporal schema have t_created=''.
            empty_predicate = "(t_created = '' OR t_created IS NULL)"
        else:
            raise ValueError(
                f"Unsupported field: {field!r}. Expected one of: t_invalidated, t_created"
            )
        with self._conn:
            cursor = self._conn.execute(
                f"UPDATE edges SET {field} = ? "
                f"WHERE (source_id = ? OR target_id = ?) AND {empty_predicate}",
                (value, node_id, node_id),
            )
            return cursor.rowcount

    def bulk_upsert_entities_with_mentions(
        self,
        entities: List[dict],
        t_created: str,
        entity_exempt: bool,
    ) -> tuple[int, set[str]]:
        edges_added = 0
        orphan_sources: set = set()
        exempt_int = 1 if entity_exempt else 0
        # Single batched transaction for the whole entity list. Sleep pipeline
        # typically passes 100-200 entities; per-entity transactions would mean
        # ~2N commits. Inlined SQL here keeps per-call existence checks
        # (idempotency and skip-orphan) but amortises commit cost across the
        # whole batch.
        with self._conn:
            for ent in entities:
                label = ent.get("entity", "").strip()
                if not label:
                    continue
                ent_type = ent.get("type", "concept")
                source_id = ent.get("source_id", "")
                score = ent.get("score", 0.0)
                entity_id = f"e:{label.lower().replace(' ', '_')}"

                # INSERT OR IGNORE skips existing rows via PRIMARY KEY on
                # node_id, leaving existing entity nodes untouched. OR REPLACE
                # would DELETE-then-INSERT and could nuke properties if entities
                # later gain them (frequency counts, last-seen timestamps);
                # OR IGNORE is defensively equivalent to SELECT-then-INSERT in
                # one statement.
                self._conn.execute(
                    "INSERT OR IGNORE INTO nodes (node_id, node_type, label, source_layer, exempt, properties) VALUES (?,?,?,?,?,?)",
                    (entity_id, NodeType.ENTITY.value, label, "L7", exempt_int, "{}"),
                )

                if not source_id:
                    continue

                # Skip-orphan guard: caller (sleep_pipeline) may pass source_ids
                # for sessions that aren't represented as graph nodes yet (group
                # archives use a different stem format that bypasses
                # _extract_archive_edges). Without this check, the MENTIONS edge
                # INSERT would fail the FK constraint and abort the whole batch.
                if self._conn.execute(
                    "SELECT 1 FROM nodes WHERE node_id=? LIMIT 1", (source_id,)
                ).fetchone() is None:
                    orphan_sources.add(source_id)
                    continue

                # Insert MENTIONS edge if not already present (idempotency).
                # Inlined edge_exists check to stay within the batch transaction
                # — calling self.edge_exists() here would race with concurrent
                # readers. Keep this SELECT in sync with edge_exists().
                if self._conn.execute(
                    "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND edge_type=? LIMIT 1",
                    (source_id, entity_id, EdgeType.MENTIONS.value),
                ).fetchone() is None:
                    self._conn.execute(
                        "INSERT INTO edges (source_id, target_id, edge_type, t_created, t_invalidated, confidence, justification, edge_weight, properties) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            source_id, entity_id, EdgeType.MENTIONS.value,
                            t_created, None, 1.0,
                            f"GLiNER extracted ({ent_type}, score={score:.2f})",
                            "medium", json.dumps({"source": "gliner_ner"}, ensure_ascii=False),
                        ),
                    )
                    edges_added += 1
        return edges_added, orphan_sources

    @staticmethod
    def _row_to_edge(row: tuple) -> GraphEdge:
        return GraphEdge(
            source_id=row[0], target_id=row[1], edge_type=EdgeType(row[2]),
            t_created=row[3], t_invalidated=row[4], confidence=row[5],
            justification=row[6], edge_weight=row[7],
            properties=json.loads(row[8]) if row[8] else {},
        )


# Module-level GrafeoDB connection cache (S125 Level 3 smoke fix).
#
# Grafeo's WAL implementation creates a per-file `<db>.wal/wal_NNNNNNNN.log`
# on first write. Opening a *second* GrafeoDB on the same path within the
# same process and then issuing a write raises GRAFEO-X003 "file already
# exists" on Windows — both instances try to create the same WAL segment.
#
# SQLite tolerates this pattern (multiple sqlite3.Connection objects on the
# same file are fine), so the call sites at the time of Phase 1.5 landing
# instantiate KnowledgeGraph (and thus the backend) ad-hoc in several
# places: agent_manager.py, sleep_pipeline.py (two spots), context.py
# (cached). Caching the underlying GrafeoDB by absolute path makes the
# Grafeo backend equivalent to SQLite under that pattern without auditing
# every call site.
#
# Trade-off: tests that explicitly want a fresh in-memory Grafeo per case
# pass db_path == ":memory:" and bypass the cache (each call gets its own
# handle, matching SQLite ":memory:" semantics).
_grafeo_instance_cache: "Dict[str, Any]" = {}


class GrafeoGraphBackend(GraphBackend):
    """Grafeo-based graph storage (ADR-024 migration).

    Implements the whole interface, with parity tests against SQLiteGraphBackend.
    (This paragraph said "remaining 9 ABC methods still raise NotImplementedError"
    long after they stopped doing so — a comment is a claim, and it outlives the
    session that wrote it.)

    Mapping (D1=A, D2=a per S123 design review):
    - Grafeo node label = NodeType.value (5 labels: KnowledgeFile, Entity,
      SessionArchive, Decision, Agent). Each node carries exactly one label.
    - DPC `node_id` stored as a node property (Grafeo assigns its own
      internal ID). Lookup by node_id uses MATCH-by-property; a property
      index on `node_id` is a Phase 2.5 follow-up for scale.
    - Opaque `properties` JSON blob preserved as a string property — parity
      with SQLite, no field-collision risk.

    Encryption: deferred. Grafeo Python binding does not expose its Rust
    encryption feature as of 0.5.42; security docs explicitly state
    "no encryption at rest" and recommend application-level encryption
    or OS-level FDE (BitLocker / LUKS / FileVault). DPC currently relies
    on OS FDE — see ADR-024 for the decision.

    Phase 2 known limitation: add_node uses INSERT only (no upsert).
    SQLite parity requires INSERT OR REPLACE semantics — deferred to
    Phase 2.5 once a property index lets us MATCH-and-DELETE efficiently.
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        try:
            import grafeo
        except ImportError as e:
            raise ImportError(
                "GrafeoGraphBackend requires the `grafeo` package. "
                "Install with: uv sync --extra graph-grafeo"
            ) from e
        # `:memory:` always gets a fresh handle — there is no on-disk WAL
        # to clash with, and parity tests rely on isolation between cases.
        if str(db_path) == ":memory:":
            self._db = grafeo.GrafeoDB()
        else:
            key = str(db_path.resolve())
            cached = _grafeo_instance_cache.get(key)
            if cached is None:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                cached = grafeo.GrafeoDB(key)
                _grafeo_instance_cache[key] = cached
            self._db = cached
        self.init_schema()

    def init_schema(self) -> None:
        # Grafeo LPG is schemaless: labels and properties are defined on
        # insert. SQLite's init_schema creates explicit tables; the Grafeo
        # equivalent is a no-op. The method exists to satisfy the ABC
        # contract and to keep a future-proof hook for property indexes.
        return

    def add_node(self, node: GraphNode) -> None:
        # Parity with SQLite INSERT OR REPLACE: MERGE upsert on node_id.
        # Existing node of matching type has its mutable properties
        # refreshed; missing node is created with the requested label set.
        # Caveat: changing node_type (Grafeo label) for an already-stored
        # node_id is out of contract — MERGE with the new label won't
        # match the existing differently-labeled node and would create a
        # sibling. KG / sleep flows never change node_type on the same
        # node_id, so this is a documented boundary, not a bug.
        exempt = bool(node.exempt or node.node_type in ALWAYS_EXEMPT)
        self._db.execute_cypher(
            f"MERGE (n:{node.node_type.value} {{node_id: $id}}) "
            "SET n.label = $label, n.source_layer = $sl, "
            "n.exempt = $ex, n.properties = $props",
            {
                "id": node.node_id,
                "label": node.label,
                "sl": node.source_layer,
                "ex": exempt,
                "props": json.dumps(node.properties, ensure_ascii=False),
            },
        )

    def add_edge(self, edge: GraphEdge) -> None:
        # Grafeo edges link by internal int id, not by string node_id.
        # Look up int ids first; raise on missing nodes to match SQLite's
        # FK constraint (PRAGMA foreign_keys=ON in SQLiteGraphBackend).
        # node_id is the agent's canonical identifier and is unique by
        # convention (SQLite PRIMARY KEY, Grafeo property index target);
        # the MATCH cartesian product yields exactly one (sid, tid) pair
        # when both nodes exist, so LIMIT 1 is correctness-preserving.
        rows = list(self._db.execute_cypher(
            "MATCH (s {node_id: $src}), (t {node_id: $tgt}) "
            "RETURN id(s) AS sid, id(t) AS tid LIMIT 1",
            {"src": edge.source_id, "tgt": edge.target_id},
        ))
        if not rows:
            raise ValueError(
                f"add_edge: source or target node not found "
                f"({edge.source_id} → {edge.target_id})"
            )
        sid, tid = rows[0]["sid"], rows[0]["tid"]
        self._db.create_edge(
            sid, tid, edge.edge_type.value,
            {
                "t_created": edge.t_created,
                "t_invalidated": edge.t_invalidated,
                "confidence": edge.confidence,
                "justification": edge.justification,
                "edge_weight": edge.edge_weight,
                "properties": json.dumps(edge.properties, ensure_ascii=False),
            },
        )

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        rows = list(self._db.execute_cypher(
            "MATCH (n {node_id: $id}) "
            "RETURN labels(n)[0] AS lbl, n.label AS l, "
            "n.source_layer AS sl, n.exempt AS ex, n.properties AS props",
            {"id": node_id},
        ))
        if not rows:
            return None
        r = rows[0]
        # D1=A: exactly one label per node, so labels(n)[0] is the node_type.
        return GraphNode(
            node_id=node_id,
            node_type=NodeType(r["lbl"]),
            label=r["l"],
            source_layer=r["sl"],
            exempt=bool(r["ex"]),
            properties=json.loads(r["props"]) if r["props"] else {},
        )

    def get_neighbors(self, node_id: str, edge_type: Optional[EdgeType] = None, hops: int = 1) -> List[GraphNode]:
        # Undirected variable-length match — parity with SQLite's UNION
        # over outgoing+incoming edges. SQLite's iterative BFS collects
        # nodes reachable within `hops`; Grafeo's [*1..hops] returns the
        # same set when paired with DISTINCT.
        # `edge_type.value` is f-string-interpolated into the query, not
        # passed as a Cypher parameter — relationship-type tokens are not
        # parameterizable in Cypher. Safe today because EdgeType is a
        # closed Enum of identifier-shaped strings; if the enum ever
        # admits special characters this becomes a query-injection vector.
        if edge_type:
            query = (
                f"MATCH (s {{node_id: $id}})-[r:{edge_type.value}*1..{hops}]-(t) "
                "WHERE t.node_id <> $id "
                "RETURN DISTINCT t.node_id AS nid, labels(t)[0] AS lbl, "
                "t.label AS l, t.source_layer AS sl, t.exempt AS ex, "
                "t.properties AS props"
            )
        else:
            query = (
                f"MATCH (s {{node_id: $id}})-[*1..{hops}]-(t) "
                "WHERE t.node_id <> $id "
                "RETURN DISTINCT t.node_id AS nid, labels(t)[0] AS lbl, "
                "t.label AS l, t.source_layer AS sl, t.exempt AS ex, "
                "t.properties AS props"
            )
        rows = list(self._db.execute_cypher(query, {"id": node_id}))
        return [
            GraphNode(
                node_id=r["nid"],
                node_type=NodeType(r["lbl"]),
                label=r["l"],
                source_layer=r["sl"],
                exempt=bool(r["ex"]),
                properties=json.loads(r["props"]) if r["props"] else {},
            )
            for r in rows
        ]

    def get_edges(self, node_id: str, direction: str = "both") -> List[GraphEdge]:
        results: List[GraphEdge] = []
        if direction in ("out", "both"):
            for r in self._db.execute_cypher(
                "MATCH (s {node_id: $id})-[r]->(t) RETURN "
                "s.node_id AS src, t.node_id AS tgt, type(r) AS et, "
                "r.t_created AS tc, r.t_invalidated AS ti, r.confidence AS conf, "
                "r.justification AS j, r.edge_weight AS ew, r.properties AS p",
                {"id": node_id},
            ):
                results.append(self._cypher_row_to_edge(r))
        if direction in ("in", "both"):
            for r in self._db.execute_cypher(
                "MATCH (s)-[r]->(t {node_id: $id}) RETURN "
                "s.node_id AS src, t.node_id AS tgt, type(r) AS et, "
                "r.t_created AS tc, r.t_invalidated AS ti, r.confidence AS conf, "
                "r.justification AS j, r.edge_weight AS ew, r.properties AS p",
                {"id": node_id},
            ):
                results.append(self._cypher_row_to_edge(r))
        return results

    @staticmethod
    def _cypher_row_to_edge(r: dict) -> GraphEdge:
        return GraphEdge(
            source_id=r["src"],
            target_id=r["tgt"],
            edge_type=EdgeType(r["et"]),
            t_created=r["tc"] or "",
            t_invalidated=r["ti"],
            confidence=r["conf"] if r["conf"] is not None else 1.0,
            justification=r["j"] or "",
            edge_weight=r["ew"] or "medium",
            properties=json.loads(r["p"]) if r["p"] else {},
        )

    def node_count(self) -> int:
        return self._db.node_count

    def edge_count(self) -> int:
        return self._db.edge_count

    def iter_nodes(self) -> Iterator[GraphNode]:
        for r in self._db.execute_cypher(
            "MATCH (n) RETURN n.node_id AS nid, labels(n)[0] AS lbl, n.label AS l, "
            "n.source_layer AS sl, n.exempt AS ex, n.properties AS props"
        ):
            yield GraphNode(
                node_id=r["nid"], node_type=NodeType(r["lbl"]), label=r["l"],
                source_layer=r["sl"], exempt=bool(r["ex"]),
                properties=json.loads(r["props"]) if r["props"] else {},
            )

    def iter_edges(self) -> Iterator[GraphEdge]:
        # Endpoints come back as node_id, not as Grafeo's internal int id, so the
        # dump means the same thing on the other backend.
        for r in self._db.execute_cypher(
            "MATCH (a)-[r]->(b) RETURN a.node_id AS src, b.node_id AS tgt, "
            "type(r) AS et, r.t_created AS tc, r.t_invalidated AS ti, "
            "r.confidence AS conf, r.justification AS j, r.edge_weight AS ew, "
            "r.properties AS p"
        ):
            yield self._cypher_row_to_edge(r)

    def snapshot(self) -> dict:
        def rows(query, params=None):
            try:
                return list(self._db.execute_cypher(query, params or {}))
            except Exception as e:
                log.debug("snapshot query failed: %s", e)
                return []

        nodes_by_type = {r["t"]: r["c"] for r in rows(
            "MATCH (n) RETURN labels(n)[0] AS t, count(n) AS c")}
        edges_by_type = {r["t"]: r["c"] for r in rows(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS c")}
        # A node id is the index key with a `kf:` prefix, so "touches a knowledge
        # file" is a prefix test on either end of the edge.
        kf_edges_by_type = {r["t"]: r["c"] for r in rows(
            "MATCH (a)-[r]->(b) WHERE a.node_id STARTS WITH 'kf:' "
            "OR b.node_id STARTS WITH 'kf:' RETURN type(r) AS t, count(r) AS c")}
        structural_rows = rows(
            "MATCH (a)-[r]->(b) WHERE (a.node_id STARTS WITH 'kf:' "
            "OR b.node_id STARTS WITH 'kf:') AND (r.properties CONTAINS $sa "
            "OR r.properties CONTAINS $sb) RETURN count(r) AS c",
            {"sa": '"source": "structural"', "sb": '"source":"structural"'})
        keyed_rows = rows(
            "MATCH (n) WHERE n.node_id STARTS WITH 'kf:' AND n.node_id CONTAINS '/' "
            "RETURN count(n) AS c")
        all_kf_rows = rows(
            "MATCH (n) WHERE n.node_id STARTS WITH 'kf:' RETURN count(n) AS c")
        structural = structural_rows[0]["c"] if structural_rows else 0
        keyed = keyed_rows[0]["c"] if keyed_rows else 0
        all_kf = all_kf_rows[0]["c"] if all_kf_rows else 0
        edges_by_source = {}
        for src in _SOURCE_MARKERS:
            r = rows("MATCH ()-[r]->() WHERE r.properties CONTAINS $a "
                     "OR r.properties CONTAINS $b RETURN count(r) AS c",
                     {"a": f'"source": "{src}"', "b": f'"source":"{src}"'})
            n = r[0]["c"] if r else 0
            if n:
                edges_by_source[src] = n
        unmarked = sum(edges_by_type.values()) - sum(edges_by_source.values())
        if unmarked:
            edges_by_source["(unmarked)"] = unmarked
        return _snapshot_dict(self._db_path, "grafeo", nodes_by_type, edges_by_type,
                              kf_edges_by_type, structural, keyed, all_kf - keyed,
                              edges_by_source)

    def wal_info(self) -> dict:
        try:
            return dict(self._db.wal_status())
        except Exception as e:  # binding surface differs by version
            log.debug("wal_status unavailable: %s", e)
            return {}

    def close(self) -> None:
        # Drop from the singleton cache so a subsequent
        # GrafeoGraphBackend(same_path) gets a fresh handle instead of
        # the closed one. Only meaningful for on-disk paths — :memory:
        # never enters the cache.
        if str(self._db_path) != ":memory:":
            key = str(Path(self._db_path).resolve())
            cached = _grafeo_instance_cache.get(key)
            if cached is self._db:
                _grafeo_instance_cache.pop(key, None)
        self._db.close()

    def edge_exists(self, source_id: str, target_id: str, edge_type: EdgeType) -> bool:
        rows = list(self._db.execute_cypher(
            "MATCH (s {node_id: $src})-[r]->(t {node_id: $tgt}) "
            "WHERE type(r) = $et RETURN 1 LIMIT 1",
            {"src": source_id, "tgt": target_id, "et": edge_type.value},
        ))
        return bool(rows)

    def clear_structural_edges(self) -> int:
        # Parity with SQLite: match structural edges (canonical marker
        # source=structural) plus legacy auto=true edges with no source
        # field. Properties is a JSON-encoded string on the edge; we
        # substring-match the same shapes SQLite does (both spaced and
        # un-spaced JSON serializations). Grafeo Cypher counts the
        # matched rows before DELETE consumes them in the same statement.
        marker_a = '"source": "structural"'
        marker_b = '"source":"structural"'
        auto_a = '"auto": true'
        auto_b = '"auto":true'
        source_marker = '"source"'
        rows = list(self._db.execute_cypher(
            "MATCH ()-[r]->() WHERE "
            "r.properties CONTAINS $sa OR r.properties CONTAINS $sb OR "
            "((r.properties CONTAINS $aa OR r.properties CONTAINS $ab) "
            "AND NOT r.properties CONTAINS $sm) "
            "DELETE r RETURN count(r) AS deleted",
            {"sa": marker_a, "sb": marker_b, "aa": auto_a, "ab": auto_b, "sm": source_marker},
        ))
        if not rows:
            return 0
        deleted = rows[0].get("deleted")
        return int(deleted) if deleted is not None else 0

    def update_edge_timestamp_for_node(self, node_id: str, field: str, value: str) -> int:
        # Parity with SQLite: bump t_created or t_invalidated for edges
        # touching node_id (either endpoint) where the field is currently
        # empty/null. Undirected MATCH covers both endpoints in one pass.
        if field == "t_invalidated":
            empty_clause = "r.t_invalidated IS NULL"
        elif field == "t_created":
            # Legacy edges pre-bi-temporal store t_created='' (empty
            # string). Treat both '' and NULL as "needs backfill".
            empty_clause = "(r.t_created = '' OR r.t_created IS NULL)"
        else:
            raise ValueError(
                f"Unsupported field: {field!r}. Expected one of: t_invalidated, t_created"
            )
        # Field name is f-string-interpolated; safe because the if/elif
        # above accepts only two whitelist values.
        rows = list(self._db.execute_cypher(
            f"MATCH (s {{node_id: $id}})-[r]-() WHERE {empty_clause} "
            f"SET r.{field} = $val RETURN count(r) AS updated",
            {"id": node_id, "val": value},
        ))
        if not rows:
            return 0
        updated = rows[0].get("updated")
        return int(updated) if updated is not None else 0

    def bulk_upsert_entities_with_mentions(
        self,
        entities: List[dict],
        t_created: str,
        entity_exempt: bool,
    ) -> tuple[int, set[str]]:
        # Three-phase batch (UNWIND) to amortise per-entity round-trips:
        #   1. MERGE entity nodes (idempotent — matches SQLite INSERT OR IGNORE)
        #   2. Detect orphan source_ids via single existence query
        #   3. Find existing MENTIONS edges, CREATE only the new ones
        # Phase 3 splits read+write rather than MERGE-with-marker because
        # MERGE's ON CREATE/ON MATCH writes a marker property that would
        # pollute the edge's persistent properties. Two queries, clean edges.
        orphan_sources: set = set()

        # Pre-process: build entity + edge batches, dedupe sources.
        entity_batch: List[dict] = []
        edge_batch: List[dict] = []
        seen_sources: set = set()
        for ent in entities:
            label = ent.get("entity", "").strip()
            if not label:
                continue
            ent_type = ent.get("type", "concept")
            source_id = ent.get("source_id", "")
            score = ent.get("score", 0.0)
            entity_id = f"e:{label.lower().replace(' ', '_')}"
            entity_batch.append({"entity_id": entity_id, "label": label})
            if source_id:
                seen_sources.add(source_id)
                edge_batch.append({
                    "entity_id": entity_id,
                    "source_id": source_id,
                    "justification": f"GLiNER extracted ({ent_type}, score={score:.2f})",
                })

        if not entity_batch:
            return 0, orphan_sources

        # Phase 1: MERGE entity nodes. ON CREATE sets properties; ON MATCH
        # is intentionally omitted so existing entities keep their props
        # (parity with SQLite's INSERT OR IGNORE which preserves rows).
        list(self._db.execute_cypher(
            "UNWIND $batch AS row "
            "MERGE (e:Entity {node_id: row.entity_id}) "
            "ON CREATE SET e.label = row.label, e.source_layer = 'L7', "
            "e.exempt = $ex, e.properties = '{}' "
            "RETURN e.node_id AS nid",
            {"batch": entity_batch, "ex": entity_exempt},
        ))

        if not edge_batch:
            return 0, orphan_sources

        # Phase 2: orphan detection — which source_ids have a node?
        source_ids = list(seen_sources)
        existing_sources = {
            r["existing"]
            for r in self._db.execute_cypher(
                "UNWIND $ids AS sid MATCH (s {node_id: sid}) "
                "RETURN s.node_id AS existing",
                {"ids": source_ids},
            )
        }
        orphan_sources = set(source_ids) - existing_sources
        edge_batch = [r for r in edge_batch if r["source_id"] in existing_sources]
        if not edge_batch:
            return 0, orphan_sources

        # Phase 3a: which (source, entity) MENTIONS edges already exist?
        existing_edges: set = set()
        for r in self._db.execute_cypher(
            "UNWIND $batch AS row "
            "MATCH (s {node_id: row.source_id})-[r:MENTIONS]->"
            "(e {node_id: row.entity_id}) "
            "RETURN row.source_id AS sid, row.entity_id AS eid",
            {"batch": edge_batch},
        ):
            existing_edges.add((r["sid"], r["eid"]))

        # Phase 3b: CREATE only the new edges. Properties JSON is a
        # constant string per spec ('{"source": "gliner_ner"}') — embed
        # via Cypher literal rather than parameter to keep the round-trip
        # batch-shaped.
        new_edges = [
            r for r in edge_batch
            if (r["source_id"], r["entity_id"]) not in existing_edges
        ]
        if new_edges:
            list(self._db.execute_cypher(
                "UNWIND $batch AS row "
                "MATCH (s {node_id: row.source_id}), (e {node_id: row.entity_id}) "
                "CREATE (s)-[r:MENTIONS {"
                "t_created: $tc, t_invalidated: null, confidence: 1.0, "
                "justification: row.justification, edge_weight: 'medium', "
                "properties: '{\"source\": \"gliner_ner\"}'"
                "}]->(e) "
                "RETURN r.t_created AS tc",
                {"batch": new_edges, "tc": t_created},
            ))

        return len(new_edges), orphan_sources


class KnowledgeGraph:
    """High-level API for the agent knowledge graph."""

    def __init__(self, agent_root: Path, backend: Optional[str] = None):
        # Backend selection (ADR-024 Phase 1.5): explicit `backend` arg
        # wins (used by tests + integration scripts); otherwise read
        # [knowledge_graph] backend from settings, defaulting to "sqlite"
        # until Grafeo migration Level 2 + Level 3 verification close.
        if backend is None:
            from dpc_client_core.settings import Settings
            # Settings takes the DPC home directory; the agent root lives
            # inside it (~/.dpc/agents/<id>/), so the home is the agent
            # root's grandparent (parent of `agents/`).
            dpc_home = agent_root.parent.parent
            backend = Settings(dpc_home).get_kg_backend()
        backend = backend.strip().lower()

        if backend == "grafeo":
            db_path = agent_root / "knowledge_graph.grafeo"
            self._backend = GrafeoGraphBackend(db_path)
        else:
            db_path = agent_root / "knowledge_graph.db"
            self._backend = SQLiteGraphBackend(db_path)
        log.info(
            "KnowledgeGraph initialized at %s [backend=%s] (%d nodes, %d edges)",
            db_path, backend, self._backend.node_count(), self._backend.edge_count(),
        )
        # What the store held before anything in this process touched it, broken down
        # by writer. The count on the line above has repeatedly opened lower than the
        # count the previous session's pass reported, and with only a total there is no
        # way to tell a rebuildable class going missing from the one class that cannot
        # come back. This line answers that on the next restart instead of the next
        # theory. Kept cheap: a handful of counting queries, once per open.
        try:
            log.info(
                "KnowledgeGraph at open: edges_by_source=%s wal=%s",
                self._backend.snapshot().get("edges_by_source"), self._backend.wal_info(),
            )
        except Exception as e:
            log.debug("open-time graph breakdown unavailable: %s", e)

    @property
    def backend(self) -> GraphBackend:
        return self._backend

    def snapshot(self) -> dict:
        """What this graph contains right now — see GraphBackend.snapshot."""
        return self._backend.snapshot()

    def export_to(self, path: Path) -> dict:
        """Write the whole graph to a JSONL dump. Returns what went out.

        One line per record, header first, then every node, then every edge — nodes
        before edges because both backends refuse an edge whose endpoints are not
        there yet, SQLite by foreign key and Grafeo by raising. The format is
        deliberately dull: a reader with no code from this project can open it, and
        the header carries the same `snapshot()` dict the team already reads.

        This is the only way a graph leaves its store. Until it existed the live file
        was the single copy — unreadable from outside, uncopyable on Windows while the
        service holds it, and impossible to move to the other backend. A dump is
        therefore three things at once: the migration carrier, the first backup this
        graph has ever had, and the measurement, since counting a dump beats
        extrapolating from totals.

        Written to a `.part` file and renamed, so an interrupted export leaves the
        previous dump intact rather than a half-written one wearing its name.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".part")
        nodes = edges = 0
        with partial.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({
                "kind": "header",
                "format": DUMP_FORMAT,
                "exported_at": _utc_now(),
                "snapshot": self.snapshot(),
            }, ensure_ascii=False) + "\n")
            for node in self._backend.iter_nodes():
                fh.write(json.dumps({
                    "kind": "node",
                    "node_id": node.node_id,
                    "node_type": node.node_type.value,
                    "label": node.label,
                    "source_layer": node.source_layer,
                    "exempt": bool(node.exempt),
                    "properties": node.properties,
                }, ensure_ascii=False) + "\n")
                nodes += 1
            for edge in self._backend.iter_edges():
                fh.write(json.dumps({
                    "kind": "edge",
                    "source_id": edge.source_id,
                    "target_id": edge.target_id,
                    "edge_type": edge.edge_type.value,
                    "t_created": edge.t_created,
                    "t_invalidated": edge.t_invalidated,
                    "confidence": edge.confidence,
                    "justification": edge.justification,
                    "edge_weight": edge.edge_weight,
                    "properties": edge.properties,
                }, ensure_ascii=False) + "\n")
                edges += 1
        partial.replace(path)
        log.info("Knowledge graph exported: %d nodes, %d edges → %s", nodes, edges, path)
        return {"nodes": nodes, "edges": edges, "path": str(path)}

    def import_from(self, path: Path, merge: bool = False) -> dict:
        """Read a dump back in, and refuse one that cannot prove it is whole.

        Two refusals, and the second is the one that earns its keep. Importing over a
        live graph silently mixes two histories, so a non-empty target is refused
        unless the caller says `merge=True`. And a dump whose record count disagrees
        with its own header is refused outright: a backup that cannot tell you it
        arrived short is worse than no backup, because it hands back a smaller graph
        with no sign that anything is missing. Both reviewers caught that the first
        version checked only the format, and only if a header happened to appear —
        lose the header and nothing was checked at all.

        Unknown record *kinds* are counted and skipped, so a dump from a later version
        still loads what this version understands. An unknown node or edge *type* is
        treated the same way rather than raising, and any edge touching a node that
        was skipped goes with it — otherwise the import dies on a missing endpoint
        halfway through and leaves a half-built graph behind.

        `merge=True` is not idempotent for edges: both backends upsert a node by id,
        but `add_edge` inserts unconditionally, so importing the same dump twice
        doubles them. Fine for a migration, where the target is always empty; not yet
        fine for restoring into a graph that is still being written to.
        """
        path = Path(path)
        if not merge:
            existing = self._backend.node_count() + self._backend.edge_count()
            if existing:
                raise ValueError(
                    f"refusing to import into a graph that already holds {existing} "
                    f"records — pass merge=True to add to it on purpose"
                )
        header = None
        nodes = edges = skipped = 0
        seen_nodes = seen_edges = 0
        skipped_node_ids: set[str] = set()
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                kind = record.get("kind")
                if header is None:
                    if kind != "header":
                        raise ValueError(
                            f"{path} does not begin with a dump header — it is truncated, "
                            f"concatenated, or not a dump at all (first record: {kind!r})"
                        )
                    if record.get("format") != DUMP_FORMAT:
                        raise ValueError(
                            f"{path} is not a {DUMP_FORMAT} dump (format={record.get('format')!r})"
                        )
                    header = record
                elif kind == "node":
                    seen_nodes += 1
                    try:
                        node_type = NodeType(record["node_type"])
                    except ValueError:
                        skipped += 1
                        skipped_node_ids.add(record["node_id"])
                        continue
                    self._backend.add_node(GraphNode(
                        node_id=record["node_id"],
                        node_type=node_type,
                        label=record["label"],
                        source_layer=record.get("source_layer", "L7"),
                        exempt=bool(record.get("exempt", False)),
                        properties=record.get("properties") or {},
                    ))
                    nodes += 1
                elif kind == "edge":
                    seen_edges += 1
                    if (record["source_id"] in skipped_node_ids
                            or record["target_id"] in skipped_node_ids):
                        skipped += 1
                        continue
                    try:
                        edge_type = EdgeType(record["edge_type"])
                    except ValueError:
                        skipped += 1
                        continue
                    self._backend.add_edge(GraphEdge(
                        source_id=record["source_id"],
                        target_id=record["target_id"],
                        edge_type=edge_type,
                        t_created=record.get("t_created", ""),
                        t_invalidated=record.get("t_invalidated"),
                        confidence=record.get("confidence", 1.0),
                        justification=record.get("justification", ""),
                        edge_weight=record.get("edge_weight", "medium"),
                        properties=record.get("properties") or {},
                    ))
                    edges += 1
                else:
                    skipped += 1

        expected = (header or {}).get("snapshot") or {}
        want_nodes = expected.get("nodes_total")
        want_edges = expected.get("edges_total")
        if (want_nodes, want_edges) != (None, None) and (
            seen_nodes != want_nodes or seen_edges != want_edges
        ):
            raise ValueError(
                f"{path} is incomplete: its header promises {want_nodes} nodes and "
                f"{want_edges} edges, the file carries {seen_nodes} and {seen_edges}"
            )

        log.info("Knowledge graph imported: %d nodes, %d edges from %s (%d records skipped)",
                 nodes, edges, path, skipped)
        return {"nodes": nodes, "edges": edges, "skipped": skipped}

    def bulk_import_knowledge_files(self, knowledge_dir: Path, source_layer: str = "L5") -> int:
        """Create KnowledgeFile nodes from existing .md files.

        `path` holds the index key, not the bare filename, because the key is what the
        rest of the system means by "this document": the fuser dedups on it, the decay
        counter buckets on it, and an address is built from it. A node that names its
        document differently from everyone else cannot be matched to anything.

        The layer is a parameter because this is called once per layer and the two
        cannot be told apart from the directory alone — the shared layer used to be
        imported as L5, and a node that lies about its layer cannot say where it lives.
        """
        count = 0
        if not knowledge_dir.exists():
            return count
        for md_file in sorted(knowledge_dir.glob("*.md")):
            if md_file.name.startswith("_"):
                continue
            key = (l5_key(md_file, knowledge_dir) if source_layer == "L5"
                   else l6_key(md_file, knowledge_dir))
            # No collision guard: two layers holding one stem now produce two ids,
            # because the key carries the layer. The guard this replaces was written
            # for a danger the scheme removes — and, reading the layer label to detect
            # it, refused to import the shared layer at all on its first run.
            node_id = node_id_for(key)
            mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc).isoformat()
            node = GraphNode(
                node_id=node_id,
                node_type=NodeType.KNOWLEDGE_FILE,
                label=md_file.stem.replace("_", " ").title(),
                source_layer=source_layer,
                properties={"path": key, "source_path": str(md_file),
                            "size_bytes": md_file.stat().st_size, "file_mtime": mtime},
            )
            self._backend.add_node(node)
            count += 1
        log.info("Bulk imported %d %s knowledge files as graph nodes", count, source_layer)
        return count

    def graph_expand(self, filenames: List[str], max_hops: int = 1) -> List[tuple]:
        """Expand from FAISS/BM25 result filenames via graph edges.

        Returns list of (meta_dict, score) tuples compatible with RRF input.
        Score decreases with hop distance: 1-hop=1.0, 2-hop=0.5.
        """
        results = []
        seen: set = set()
        for fname in filenames:
            # The seed arrives as an index key and the node is addressed by one, so the
            # lookup is exact. Nothing to cut down and nothing to verify afterwards: a
            # namesake in another root is a different key, therefore a different node.
            src_id = node_id_for(fname)
            if self._backend.get_node(src_id) is None:
                continue
            neighbors = self._backend.get_neighbors(src_id, hops=max_hops)
            for neighbor in neighbors:
                if neighbor.node_id in seen or neighbor.node_type != NodeType.KNOWLEDGE_FILE:
                    continue
                seen.add(neighbor.node_id)
                path = neighbor.properties.get("path", "")
                if path and path not in filenames:
                    results.append(({
                        "source_file": path,
                        "source_path": neighbor.properties.get("source_path", ""),
                        # The channel, not the document's layer: LAYER_WEIGHTS prices
                        # L7 at 0.6, and re-labelling here would silently re-weight
                        # fusion under cover of an addressing fix.
                        "source_layer": "L7",
                        "heading": neighbor.label,
                        "graph_node_id": neighbor.node_id,
                    }, 1.0))
        return results

    def extract_structural_edges(self, knowledge_dir: Path, archive_dir: Optional[Path] = None) -> int:
        """Extract deterministic edges from existing files (Task 002).

        Parses markdown links, _meta.json tags, session references,
        and ADR references. Idempotent: clears existing structural edges
        before re-extracting.
        """
        import re
        self._clear_structural_edges()
        count = 0
        meta = self._load_meta(knowledge_dir)
        md_link_re = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
        adr_re = re.compile(r'\bADR-(\d{3})\b')
        file_ref_re = re.compile(r'\b([\w_-]+\.md)\b')
        now = _utc_now()

        # Links inside a document name a file; nodes are addressed by index key. The map
        # is the translation, and it is the only place a stem is allowed to stand for a
        # document — inside one directory, where a stem is unambiguous by definition.
        known_files = {f.stem: (f.name, node_id_for(l5_key(f, knowledge_dir)))
                       for f in knowledge_dir.glob("*.md") if not f.name.startswith("_")}

        for stem, (fname, src_id) in known_files.items():
            text = (knowledge_dir / fname).read_text(encoding="utf-8", errors="replace")

            for match in md_link_re.finditer(text):
                target_stem = Path(match.group(2)).stem
                if target_stem in known_files and target_stem != stem:
                    self._add_edge_safe(src_id, known_files[target_stem][1], EdgeType.DEPENDS_ON,
                                        f"markdown link [{match.group(1)}]", now)
                    count += 1

            for match in file_ref_re.finditer(text):
                ref_stem = Path(match.group(1)).stem
                if ref_stem in known_files and ref_stem != stem:
                    ref_id = known_files[ref_stem][1]
                    if not self._edge_exists(src_id, ref_id, EdgeType.DEPENDS_ON):
                        self._add_edge_safe(src_id, ref_id, EdgeType.DEPENDS_ON,
                                            f"file reference {match.group(1)}", now)
                        count += 1

            for match in adr_re.finditer(text):
                adr_num = match.group(1)
                adr_id = f"d:adr-{adr_num}"
                self._ensure_node(adr_id, NodeType.DECISION, f"ADR-{adr_num}")
                self._add_edge_safe(src_id, adr_id, EdgeType.DECIDED_BY,
                                    f"references ADR-{adr_num}", now)
                count += 1

        for fname, file_meta in meta.items():
            entry = known_files.get(Path(fname).stem)
            if entry is None:
                continue
            src_id = entry[1]
            if self._backend.get_node(src_id) is None:
                continue
            for tag in file_meta.get("tags", []):
                tag_clean = tag.strip().lower().replace(" ", "_")
                if not tag_clean:
                    continue
                entity_id = f"e:{tag_clean}"
                self._ensure_node(entity_id, NodeType.ENTITY, tag)
                self._add_edge_safe(src_id, entity_id, EdgeType.MENTIONS,
                                    f"tagged with '{tag}'", now)
                count += 1

        if archive_dir and archive_dir.exists():
            count += self._extract_archive_edges(archive_dir, known_files, now)

        log.info("Extracted %d structural edges from %d knowledge files", count, len(known_files))
        return count

    def _clear_structural_edges(self) -> None:
        self._backend.clear_structural_edges()

    def _load_meta(self, knowledge_dir: Path) -> dict:
        meta_path = knowledge_dir / "_meta.json"
        if not meta_path.exists():
            return {}
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _ensure_node(self, node_id: str, node_type: NodeType, label: str) -> None:
        if self._backend.get_node(node_id) is None:
            self._backend.add_node(GraphNode(node_id=node_id, node_type=node_type, label=label))

    def _add_edge_safe(self, src: str, tgt: str, etype: EdgeType, justification: str, t_created: str, properties: dict | None = None) -> bool:
        """Add the edge unless it is already there. True iff this call wrote one.

        The return value exists because a caller that counts its own attempts cannot
        tell an insertion from a duplicate, and the nightly relation counter did
        exactly that — reporting "N relations (from N candidates)" every single night,
        which it could not help doing.
        """
        if self._edge_exists(src, tgt, etype):
            return False
        self._backend.add_edge(GraphEdge(
            source_id=src, target_id=tgt, edge_type=etype,
            t_created=t_created, justification=justification,
            properties=properties if properties is not None else {"source": "structural"},
        ))
        return True

    def _edge_exists(self, src: str, tgt: str, etype: EdgeType) -> bool:
        return self._backend.edge_exists(src, tgt, etype)

    def _extract_archive_edges(self, archive_dir: Path, known_files: dict, now: str) -> int:
        import re
        count = 0
        for json_file in sorted(archive_dir.rglob("*_reset_session.json"), key=lambda f: f.name):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            session_id_str = json_file.stem.split("_")[0]
            archive_id = f"sa:{session_id_str}"
            self._ensure_node(archive_id, NodeType.SESSION_ARCHIVE, session_id_str)
            senders = {m.get("sender_name", "") for m in data.get("messages", [])}
            for sender in senders:
                if sender and sender not in ("", "system"):
                    agent_id = f"ag:{sender.lower().replace(' ', '_')}"
                    self._ensure_node(agent_id, NodeType.AGENT, sender)
                    self._add_edge_safe(archive_id, agent_id, EdgeType.DERIVED_FROM,
                                        f"participant in session", now)
                    count += 1
        return count

    def extract_entities_gliner(self, texts: List[dict], entity_types: Optional[List[str]] = None) -> List[dict]:
        """Run GLiNER NER over texts and return extracted entities. Pure
        compute — no SQLite writes. Safe to call from a worker thread.

        Uses the module-level GLiNER singleton (_get_gliner_model) — parallel
        group sleep on N agents shares one model instance instead of loading
        N copies (~2 GB each).

        Args:
            texts: list of {"source_id": str, "text": str} dicts
            entity_types: NER labels to extract (default: person, organization, technology, concept)

        Returns:
            list of {"entity": str, "type": str, "source_id": str, "score": float}.
            Call persist_extracted_entities() from the main thread to write the
            corresponding Entity nodes + MENTIONS edges. Split exists because
            GLiNER load/predict is offloaded to asyncio.to_thread() and SQLite
            connections are bound to the thread that created them.
        """
        if not texts:
            return []

        model = _get_gliner_model()
        if model is None:
            return []

        if entity_types is None:
            entity_types = DEFAULT_ENTITY_TYPES

        all_entities = []
        for item in texts:
            source_id = item.get("source_id", "")
            text = item.get("text", "")
            if not text or len(text) < 20:
                continue
            try:
                entities = model.predict_entities(text[:GLINER_MAX_TEXT_LEN], entity_types, threshold=GLINER_THRESHOLD)
            except Exception as e:
                log.debug("GLiNER extraction failed for %s: %s", source_id, e)
                continue
            for ent in entities:
                label = ent.get("text", "").strip()
                ent_type = ent.get("label", "concept")
                if not label or len(label) < 2:
                    continue
                all_entities.append({
                    "entity": label,
                    "type": ent_type,
                    "source_id": source_id,
                    "score": float(ent.get("score", 0.0)),
                })

        log.info("GLiNER extracted %d entities from %d texts", len(all_entities), len(texts))
        return all_entities

    def persist_extracted_entities(self, entities: List[dict]) -> int:
        """Write Entity nodes + MENTIONS edges for entities produced by
        extract_entities_gliner(). Must run on the SQLite connection's owner
        thread (typically the main asyncio loop thread).

        Args:
            entities: list of dicts from extract_entities_gliner()

        Returns:
            number of MENTIONS edges added (entities without source_id are
            still upserted as nodes but contribute no edge).
        """
        if not entities:
            return 0
        now = _utc_now()
        entity_exempt = NodeType.ENTITY in ALWAYS_EXEMPT
        edges_added, orphan_sources = self._backend.bulk_upsert_entities_with_mentions(
            entities, now, entity_exempt
        )
        if orphan_sources:
            log.warning(
                "KG persist: skipped MENTIONS edges for %d orphan source(s) not in graph: %s",
                len(orphan_sources),
                sorted(orphan_sources)[:5],
            )
        return edges_added

    def invalidate_edges(self, node_id: str) -> int:
        """Mark all active edges touching node_id as invalidated (bi-temporal)."""
        now = _utc_now()
        count = self._backend.update_edge_timestamp_for_node(node_id, "t_invalidated", now)
        if count:
            log.info("Invalidated %d edges for node %s", count, node_id)
        return count

    def backfill_edge_timestamps(self, knowledge_dir: Path, source_layer: str = "L5") -> int:
        """Backfill t_created on edges from source file mtime.

        Atomicity note: each file's UPDATE is its own backend transaction (one
        per file), not a single transaction wrapping the whole iteration. The
        operation is idempotent and monotonic — partial failure on file N
        leaves files 1..N-1 committed; the next run completes the rest. Safe
        trade-off vs the prior single-transaction implementation that accessed
        the backend connection directly."""
        count = 0
        if not knowledge_dir.exists():
            return count
        for md_file in sorted(knowledge_dir.glob("*.md")):
            if md_file.name.startswith("_"):
                continue
            key = (l5_key(md_file, knowledge_dir) if source_layer == "L5"
                   else l6_key(md_file, knowledge_dir))
            mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc).isoformat()
            count += self._backend.update_edge_timestamp_for_node(node_id_for(key), "t_created", mtime)
        if count:
            log.info("Backfilled t_created on %d edges from file timestamps", count)
        return count

    def close(self) -> None:
        self._backend.close()
