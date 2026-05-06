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
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


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
        exempt = 1 if (node.exempt or node.node_type in ALWAYS_EXEMPT) else 0
        self._conn.execute(
            "INSERT OR REPLACE INTO nodes (node_id, node_type, label, source_layer, exempt, properties) VALUES (?,?,?,?,?,?)",
            (node.node_id, node.node_type.value, node.label, node.source_layer, exempt, json.dumps(node.properties, ensure_ascii=False)),
        )
        self._conn.commit()

    def add_edge(self, edge: GraphEdge) -> None:
        self._conn.execute(
            "INSERT INTO edges (source_id, target_id, edge_type, t_created, t_invalidated, confidence, justification, edge_weight, properties) VALUES (?,?,?,?,?,?,?,?,?)",
            (edge.source_id, edge.target_id, edge.edge_type.value, edge.t_created, edge.t_invalidated, edge.confidence, edge.justification, edge.edge_weight, json.dumps(edge.properties, ensure_ascii=False)),
        )
        self._conn.commit()

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
            node = GraphNode(
                node_id=node_id,
                node_type=NodeType.KNOWLEDGE_FILE,
                label=md_file.stem.replace("_", " ").title(),
                source_layer="L5",
                properties={"path": str(md_file.name), "size_bytes": md_file.stat().st_size},
            )
            self._backend.add_node(node)
            count += 1
        log.info("Bulk imported %d knowledge files as graph nodes", count)
        return count

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

    def close(self) -> None:
        self._backend.close()
