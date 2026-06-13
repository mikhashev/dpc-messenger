"""Regression tests for KnowledgeGraph + SQLiteGraphBackend (ADR-024, KG-ABSTRACTION-DEBT, S116).

Covers the 5 backend operations lifted from the fasade in S116:
- edge_exists
- clear_structural_edges
- update_edge_timestamp_for_node (t_invalidated + t_created)
- bulk_upsert_entities_with_mentions (batch path)

Plus the fasade methods that route through them: `_edge_exists`,
`_clear_structural_edges`, `invalidate_edges`, `backfill_edge_timestamps`,
`persist_extracted_entities`.

These tests pin pre-refactor behaviour. They should pass against the SQLite
backend with no behaviour change vs the original `_backend._conn`-leaking
implementation.
"""

import json
import time
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.knowledge_graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
    SQLiteGraphBackend,
)


@pytest.fixture
def backend(tmp_path):
    db_path = tmp_path / "kg.db"
    b = SQLiteGraphBackend(db_path)
    yield b
    b.close()


@pytest.fixture
def kg(tmp_path):
    g = KnowledgeGraph(tmp_path)
    yield g
    g.close()


def _add_node(backend, node_id, node_type=NodeType.KNOWLEDGE_FILE, label="x"):
    backend.add_node(GraphNode(node_id=node_id, node_type=node_type, label=label))


def _add_edge(backend, src, tgt, etype=EdgeType.DERIVED_FROM, properties=None, t_created="2026-01-01", t_invalidated=None):
    backend.add_edge(GraphEdge(
        source_id=src, target_id=tgt, edge_type=etype,
        t_created=t_created, t_invalidated=t_invalidated,
        properties=properties or {},
    ))


# ---------- edge_exists ----------

def test_edge_exists_true_when_edge_present(backend):
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2")
    assert backend.edge_exists("n1", "n2", EdgeType.DERIVED_FROM) is True


def test_edge_exists_false_when_missing(backend):
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    assert backend.edge_exists("n1", "n2", EdgeType.DERIVED_FROM) is False


def test_edge_exists_does_not_filter_invalidated(backend):
    """Active-or-invalidated semantics: invalidated edge still 'exists'."""
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", t_invalidated="2026-02-01")
    assert backend.edge_exists("n1", "n2", EdgeType.DERIVED_FROM) is True


def test_edge_exists_filters_by_edge_type(backend):
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", etype=EdgeType.DERIVED_FROM)
    assert backend.edge_exists("n1", "n2", EdgeType.DERIVED_FROM) is True
    assert backend.edge_exists("n1", "n2", EdgeType.MENTIONS) is False


# ---------- clear_structural_edges ----------

def test_clear_structural_edges_deletes_canonical_marker(backend):
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", properties={"source": "structural"})
    deleted = backend.clear_structural_edges()
    assert deleted == 1
    assert backend.edge_count() == 0


def test_clear_structural_edges_deletes_legacy_auto(backend):
    """Legacy edges (auto=true without source) should also be cleared."""
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", properties={"auto": True})
    deleted = backend.clear_structural_edges()
    assert deleted == 1


def test_clear_structural_edges_keeps_llm_relations(backend):
    """LLM/GLiNER edges (non-structural marker) should survive."""
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", properties={"source": "llm_relation"})
    deleted = backend.clear_structural_edges()
    assert deleted == 0
    assert backend.edge_count() == 1


def test_clear_structural_edges_keeps_gliner_with_source_and_auto(backend):
    """Edges with both source AND auto markers (non-legacy) should NOT be cleared."""
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", properties={"auto": True, "source": "llm_relation"})
    deleted = backend.clear_structural_edges()
    assert deleted == 0


# ---------- update_edge_timestamp_for_node ----------

def test_update_edge_timestamp_invalidates_active_edges(backend):
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", t_invalidated=None)
    updated = backend.update_edge_timestamp_for_node("n1", "t_invalidated", "2026-03-01")
    assert updated == 1


def test_update_edge_timestamp_skips_already_invalidated(backend):
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", t_invalidated="2026-02-01")
    updated = backend.update_edge_timestamp_for_node("n1", "t_invalidated", "2026-03-01")
    assert updated == 0


def test_update_edge_timestamp_backfills_empty_t_created(backend):
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", t_created="")
    updated = backend.update_edge_timestamp_for_node("n1", "t_created", "2026-04-01")
    assert updated == 1


def test_update_edge_timestamp_skips_populated_t_created(backend):
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", t_created="2026-01-01")
    updated = backend.update_edge_timestamp_for_node("n1", "t_created", "2026-04-01")
    assert updated == 0


def test_update_edge_timestamp_backfills_null_t_created(backend):
    """Legacy predicate covers both empty string AND SQL NULL — verify NULL branch."""
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", t_created=None)
    updated = backend.update_edge_timestamp_for_node("n1", "t_created", "2026-04-01")
    assert updated == 1


def test_update_edge_timestamp_unsupported_field_raises(backend):
    with pytest.raises(ValueError, match="Unsupported field"):
        backend.update_edge_timestamp_for_node("n1", "confidence", "0.5")


def test_update_edge_timestamp_matches_both_directions(backend):
    """WHERE source_id=node OR target_id=node — both directions."""
    _add_node(backend, "n1")
    _add_node(backend, "n2")
    _add_edge(backend, "n1", "n2", t_invalidated=None)
    _add_edge(backend, "n2", "n1", t_invalidated=None)
    updated = backend.update_edge_timestamp_for_node("n1", "t_invalidated", "2026-03-01")
    assert updated == 2


# ---------- bulk_upsert_entities_with_mentions ----------

def test_bulk_upsert_creates_entity_nodes_and_mentions(backend):
    _add_node(backend, "src1", node_type=NodeType.SESSION_ARCHIVE)
    entities = [
        {"entity": "Python", "type": "technology", "source_id": "src1", "score": 0.9},
        {"entity": "Mike", "type": "person", "source_id": "src1", "score": 0.95},
    ]
    edges_added, orphans = backend.bulk_upsert_entities_with_mentions(entities, "2026-05-13", False)
    assert edges_added == 2
    assert orphans == set()
    assert backend.node_count() == 3  # src1 + 2 entities
    assert backend.edge_count() == 2  # 2 MENTIONS edges


def test_bulk_upsert_is_idempotent(backend):
    _add_node(backend, "src1", node_type=NodeType.SESSION_ARCHIVE)
    entities = [{"entity": "Python", "type": "technology", "source_id": "src1", "score": 0.9}]
    backend.bulk_upsert_entities_with_mentions(entities, "2026-05-13", False)
    # Second call with same input should add zero new edges.
    edges_added, orphans = backend.bulk_upsert_entities_with_mentions(entities, "2026-05-13", False)
    assert edges_added == 0
    assert orphans == set()
    assert backend.edge_count() == 1


def test_bulk_upsert_returns_orphans_for_missing_sources(backend):
    """Skip-orphan guard: source_id absent from graph → edge skipped, src reported."""
    entities = [
        {"entity": "Python", "type": "technology", "source_id": "missing_src", "score": 0.9},
    ]
    edges_added, orphans = backend.bulk_upsert_entities_with_mentions(entities, "2026-05-13", False)
    assert edges_added == 0
    assert orphans == {"missing_src"}
    # Entity node was still upserted even without source.
    assert backend.get_node("e:python") is not None


def test_bulk_upsert_skips_empty_labels(backend):
    _add_node(backend, "src1", node_type=NodeType.SESSION_ARCHIVE)
    entities = [
        {"entity": "  ", "type": "concept", "source_id": "src1", "score": 0.5},
        {"entity": "Real", "type": "concept", "source_id": "src1", "score": 0.5},
    ]
    edges_added, _ = backend.bulk_upsert_entities_with_mentions(entities, "2026-05-13", False)
    assert edges_added == 1


def test_bulk_upsert_entity_exempt_flag_propagates(backend):
    _add_node(backend, "src1", node_type=NodeType.SESSION_ARCHIVE)
    entities = [{"entity": "Secret", "type": "concept", "source_id": "src1", "score": 0.9}]
    backend.bulk_upsert_entities_with_mentions(entities, "2026-05-13", True)
    node = backend.get_node("e:secret")
    assert node is not None
    assert node.exempt is True


def test_bulk_upsert_skips_entity_without_source(backend):
    """Entity with no source_id: node is upserted, no edge created, no orphan reported."""
    entities = [{"entity": "Orphan", "type": "concept", "source_id": "", "score": 0.5}]
    edges_added, orphans = backend.bulk_upsert_entities_with_mentions(entities, "2026-05-13", False)
    assert edges_added == 0
    assert orphans == set()
    assert backend.get_node("e:orphan") is not None


def test_bulk_upsert_writes_gliner_marker_in_properties(backend):
    _add_node(backend, "src1", node_type=NodeType.SESSION_ARCHIVE)
    entities = [{"entity": "Python", "type": "technology", "source_id": "src1", "score": 0.88}]
    backend.bulk_upsert_entities_with_mentions(entities, "2026-05-13", False)
    edges = backend.get_edges("src1", direction="out")
    assert len(edges) == 1
    assert edges[0].properties.get("source") == "gliner_ner"


# ---------- Fasade routing (KnowledgeGraph wraps the backend) ----------

def test_fasade_no_direct_conn_access():
    """Drift guard: KnowledgeGraph fasade must not reach into `_backend._conn`."""
    import inspect
    from dpc_client_core.dpc_agent import knowledge_graph as kg_module
    src = inspect.getsource(kg_module.KnowledgeGraph)
    assert "_backend._conn" not in src, (
        "KnowledgeGraph fasade still leaks SQLite connection access. "
        "All 5 operations must route through GraphBackend ABC methods."
    )


def test_fasade_edge_exists_routes_through_backend(kg):
    kg._ensure_node("n1", NodeType.KNOWLEDGE_FILE, "n1")
    kg._ensure_node("n2", NodeType.KNOWLEDGE_FILE, "n2")
    _add_edge(kg.backend, "n1", "n2")
    assert kg._edge_exists("n1", "n2", EdgeType.DERIVED_FROM) is True


def test_fasade_invalidate_edges_routes_through_backend(kg):
    kg._ensure_node("n1", NodeType.KNOWLEDGE_FILE, "n1")
    kg._ensure_node("n2", NodeType.KNOWLEDGE_FILE, "n2")
    _add_edge(kg.backend, "n1", "n2", t_invalidated=None)
    count = kg.invalidate_edges("n1")
    assert count == 1


def test_fasade_persist_extracted_entities_passes_orphan_log(kg):
    kg._ensure_node("src1", NodeType.SESSION_ARCHIVE, "src1")
    entities = [
        {"entity": "Python", "type": "technology", "source_id": "src1", "score": 0.9},
        {"entity": "Missing", "type": "concept", "source_id": "no_such_node", "score": 0.7},
    ]
    edges_added = kg.persist_extracted_entities(entities)
    # One edge for src1 → e:python; e:missing has no parent source node.
    assert edges_added == 1


def test_fasade_backfill_edge_timestamps_per_file(kg, tmp_path):
    knowledge_dir = tmp_path / "kn"
    knowledge_dir.mkdir()
    md1 = knowledge_dir / "topic_a.md"
    md1.write_text("body", encoding="utf-8")
    kg._ensure_node("kf:topic_a", NodeType.KNOWLEDGE_FILE, "Topic A")
    kg._ensure_node("other", NodeType.SESSION_ARCHIVE, "other")
    _add_edge(kg.backend, "kf:topic_a", "other", t_created="")

    count = kg.backfill_edge_timestamps(knowledge_dir)
    assert count == 1

    # Second call should be no-op (t_created now populated).
    count2 = kg.backfill_edge_timestamps(knowledge_dir)
    assert count2 == 0


def test_fasade_clear_structural_edges_routes_through_backend(kg):
    kg._ensure_node("n1", NodeType.KNOWLEDGE_FILE, "n1")
    kg._ensure_node("n2", NodeType.KNOWLEDGE_FILE, "n2")
    _add_edge(kg.backend, "n1", "n2", properties={"source": "structural"})
    _add_edge(kg.backend, "n1", "n2", etype=EdgeType.MENTIONS, properties={"source": "llm_relation"})
    kg._clear_structural_edges()
    assert kg.backend.edge_count() == 1  # LLM edge survives.
