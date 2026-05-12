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
from typing import Any, Dict, List, Optional

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
        except ImportError:
            log.debug("GLiNER not installed — skip entity extraction (install with: uv sync --extra graph-ner)")
            return None
        log.info("Loading GLiNER model %s (first use, process-wide singleton)...", GLINER_MODEL_NAME)
        _GLINER_MODEL = GLiNER.from_pretrained(GLINER_MODEL_NAME)
        log.info("GLiNER model loaded")
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

    @staticmethod
    def _row_to_edge(row: tuple) -> GraphEdge:
        return GraphEdge(
            source_id=row[0], target_id=row[1], edge_type=EdgeType(row[2]),
            t_created=row[3], t_invalidated=row[4], confidence=row[5],
            justification=row[6], edge_weight=row[7],
            properties=json.loads(row[8]) if row[8] else {},
        )


class KnowledgeGraph:
    """High-level API for the agent knowledge graph."""

    def __init__(self, agent_root: Path):
        db_path = agent_root / "knowledge_graph.db"
        self._backend: GraphBackend = SQLiteGraphBackend(db_path)
        log.info("KnowledgeGraph initialized at %s (%d nodes, %d edges)", db_path, self._backend.node_count(), self._backend.edge_count())

    @property
    def backend(self) -> GraphBackend:
        return self._backend

    def bulk_import_knowledge_files(self, knowledge_dir: Path) -> int:
        """Create KnowledgeFile nodes from existing .md files."""
        count = 0
        if not knowledge_dir.exists():
            return count
        for md_file in sorted(knowledge_dir.glob("*.md")):
            if md_file.name.startswith("_"):
                continue
            node_id = f"kf:{md_file.stem}"
            mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc).isoformat()
            node = GraphNode(
                node_id=node_id,
                node_type=NodeType.KNOWLEDGE_FILE,
                label=md_file.stem.replace("_", " ").title(),
                source_layer="L5",
                properties={"path": str(md_file.name), "size_bytes": md_file.stat().st_size, "file_mtime": mtime},
            )
            self._backend.add_node(node)
            count += 1
        log.info("Bulk imported %d knowledge files as graph nodes", count)
        return count

    def graph_expand(self, filenames: List[str], max_hops: int = 1) -> List[tuple]:
        """Expand from FAISS/BM25 result filenames via graph edges.

        Returns list of (meta_dict, score) tuples compatible with RRF input.
        Score decreases with hop distance: 1-hop=1.0, 2-hop=0.5.
        """
        results = []
        seen: set = set()
        for fname in filenames:
            stem = Path(fname).stem
            src_id = f"kf:{stem}"
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
                        "source_layer": "L7",
                        "heading": neighbor.label,
                        "graph_node_id": neighbor.node_id,
                    }, 1.0))
        return results

    def get_graph_results_for_query(self, seed_node_ids: List[str], hops: int = 2) -> List[dict]:
        """Traverse graph from seed nodes, return results compatible with RRF fusion."""
        results = []
        seen: set = set()
        for seed_id in seed_node_ids:
            neighbors = self._backend.get_neighbors(seed_id, hops=hops)
            for neighbor in neighbors:
                if neighbor.node_id in seen:
                    continue
                seen.add(neighbor.node_id)
                path_prop = neighbor.properties.get("path", "")
                results.append({
                    "source_file": path_prop,
                    "source_layer": neighbor.source_layer,
                    "heading": neighbor.label,
                    "graph_node_id": neighbor.node_id,
                    "graph_node_type": neighbor.node_type.value,
                })
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
        session_re = re.compile(r'\bS(\d{2,3})\b')
        adr_re = re.compile(r'\bADR-(\d{3})\b')
        file_ref_re = re.compile(r'\b([\w_-]+\.md)\b')
        now = _utc_now()

        known_files = {f.stem: f.name for f in knowledge_dir.glob("*.md") if not f.name.startswith("_")}

        for stem, fname in known_files.items():
            src_id = f"kf:{stem}"
            text = (knowledge_dir / fname).read_text(encoding="utf-8", errors="replace")

            for match in md_link_re.finditer(text):
                target_path = match.group(2)
                target_stem = Path(target_path).stem
                if target_stem in known_files and target_stem != stem:
                    self._add_edge_safe(src_id, f"kf:{target_stem}", EdgeType.DEPENDS_ON,
                                        f"markdown link [{match.group(1)}]", now)
                    count += 1

            for match in file_ref_re.finditer(text):
                ref_stem = Path(match.group(1)).stem
                if ref_stem in known_files and ref_stem != stem:
                    if not self._edge_exists(src_id, f"kf:{ref_stem}", EdgeType.DEPENDS_ON):
                        self._add_edge_safe(src_id, f"kf:{ref_stem}", EdgeType.DEPENDS_ON,
                                            f"file reference {match.group(1)}", now)
                        count += 1

            for match in session_re.finditer(text):
                session_num = match.group(1)
                session_id = f"sa:S{session_num}"
                self._ensure_node(session_id, NodeType.SESSION_ARCHIVE, f"Session {session_num}")
                self._add_edge_safe(src_id, session_id, EdgeType.DERIVED_FROM,
                                    f"references session S{session_num}", now)
                count += 1

            for match in adr_re.finditer(text):
                adr_num = match.group(1)
                adr_id = f"d:adr-{adr_num}"
                self._ensure_node(adr_id, NodeType.DECISION, f"ADR-{adr_num}")
                self._add_edge_safe(src_id, adr_id, EdgeType.DECIDED_BY,
                                    f"references ADR-{adr_num}", now)
                count += 1

        for fname, file_meta in meta.items():
            stem = Path(fname).stem
            src_id = f"kf:{stem}"
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
        with self._backend._conn:
            self._backend._conn.execute("DELETE FROM edges WHERE properties LIKE '%\"auto\": true%' OR properties LIKE '%\"auto\":true%'")

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

    def _add_edge_safe(self, src: str, tgt: str, etype: EdgeType, justification: str, t_created: str) -> None:
        if not self._edge_exists(src, tgt, etype):
            self._backend.add_edge(GraphEdge(
                source_id=src, target_id=tgt, edge_type=etype,
                t_created=t_created, justification=justification,
                properties={"auto": True},
            ))

    def _edge_exists(self, src: str, tgt: str, etype: EdgeType) -> bool:
        row = self._backend._conn.execute(
            "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND edge_type=? LIMIT 1",
            (src, tgt, etype.value),
        ).fetchone()
        return row is not None

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
        edges_added = 0
        orphan_sources: set = set()
        # Single batched transaction for the whole entity list. Sleep pipeline
        # typically passes 100-200 entities; the previous per-entity pattern
        # called `_ensure_node` and `_add_edge_safe`, each opening their own
        # `with self._conn:` block — ~2N transactions per persist call. Inlined
        # SQL here keeps the per-call existence checks (idempotency and
        # skip-orphan) but amortises commit cost across the whole batch.
        conn = self._backend._conn
        entity_exempt = 1 if NodeType.ENTITY in ALWAYS_EXEMPT else 0
        with conn:
            for ent in entities:
                label = ent.get("entity", "").strip()
                if not label:
                    continue
                ent_type = ent.get("type", "concept")
                source_id = ent.get("source_id", "")
                score = ent.get("score", 0.0)
                entity_id = f"e:{label.lower().replace(' ', '_')}"

                # _ensure_node inlined: upsert entity node if missing.
                if conn.execute(
                    "SELECT 1 FROM nodes WHERE node_id=? LIMIT 1", (entity_id,)
                ).fetchone() is None:
                    conn.execute(
                        "INSERT OR REPLACE INTO nodes (node_id, node_type, label, source_layer, exempt, properties) VALUES (?,?,?,?,?,?)",
                        (entity_id, NodeType.ENTITY.value, label, "L7", entity_exempt, "{}"),
                    )

                if not source_id:
                    continue

                # Skip-orphan guard: caller (sleep_pipeline) may pass source_ids
                # for sessions that aren't represented as graph nodes yet
                # (group archives use a different stem format that bypasses
                # _extract_archive_edges). Without this check, the MENTIONS
                # edge INSERT would fail the FK constraint and abort the whole
                # batch transaction.
                if conn.execute(
                    "SELECT 1 FROM nodes WHERE node_id=? LIMIT 1", (source_id,)
                ).fetchone() is None:
                    orphan_sources.add(source_id)
                    continue

                # _add_edge_safe inlined: insert MENTIONS edge if not already present.
                if conn.execute(
                    "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND edge_type=? LIMIT 1",
                    (source_id, entity_id, EdgeType.MENTIONS.value),
                ).fetchone() is None:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, edge_type, t_created, t_invalidated, confidence, justification, edge_weight, properties) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            source_id, entity_id, EdgeType.MENTIONS.value,
                            now, None, 1.0,
                            f"GLiNER extracted ({ent_type}, score={score:.2f})",
                            "medium", json.dumps({"auto": True}, ensure_ascii=False),
                        ),
                    )
                    edges_added += 1
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
        with self._backend._conn:
            cursor = self._backend._conn.execute(
                "UPDATE edges SET t_invalidated = ? WHERE (source_id = ? OR target_id = ?) AND t_invalidated IS NULL",
                (now, node_id, node_id),
            )
            count = cursor.rowcount
        if count:
            log.info("Invalidated %d edges for node %s", count, node_id)
        return count

    def backfill_edge_timestamps(self, knowledge_dir: Path) -> int:
        """Backfill t_created on edges from source file mtime."""
        count = 0
        if not knowledge_dir.exists():
            return count
        with self._backend._conn:
            for md_file in sorted(knowledge_dir.glob("*.md")):
                if md_file.name.startswith("_"):
                    continue
                node_id = f"kf:{md_file.stem}"
                mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc).isoformat()
                cursor = self._backend._conn.execute(
                    "UPDATE edges SET t_created = ? WHERE (source_id = ? OR target_id = ?) AND (t_created = '' OR t_created IS NULL)",
                    (mtime, node_id, node_id),
                )
                count += cursor.rowcount
        if count:
            log.info("Backfilled t_created on %d edges from file timestamps", count)
        return count

    def close(self) -> None:
        self._backend.close()
