"""Level 2 integration tests for the KnowledgeGraph high-level API
(ADR-024 KG-GRAFEO-VERIFICATION Level 2).

Level 1 (parity tests in test_grafeo_backend_parity.py) verifies that
each GraphBackend ABC method has identical observable behavior on
SQLite and Grafeo. Level 2 verifies that the *composition* of those
methods inside KnowledgeGraph still works — workflows like
bulk_import → structural extraction → entity persistence → graph
expansion → invalidation → close → reopen. Methods that pass parity
individually can still break in combination (transaction boundaries,
ordering, identity coherence), which is what this layer catches.

Parametrized over both backends; skips the grafeo case if the
`grafeo` package is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

grafeo = pytest.importorskip("grafeo")

from dpc_client_core.dpc_agent.knowledge_graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)


@pytest.fixture(params=["sqlite", "grafeo"])
def kg(request, tmp_path):
    """Create a fresh KnowledgeGraph backed by the parametrized backend."""
    agent_root = tmp_path / "dpc" / "agents" / "agent_001"
    agent_root.mkdir(parents=True)
    instance = KnowledgeGraph(agent_root, backend=request.param)
    yield instance
    instance.close()


def _write_md(dir_path: Path, name: str, body: str) -> Path:
    p = dir_path / f"{name}.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_kg_bulk_import_creates_nodes(kg, tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    _write_md(kdir, "alpha", "# Alpha\nFirst file.")
    _write_md(kdir, "beta", "# Beta\nSecond file referring to [[alpha]].")
    _write_md(kdir, "_skipped", "# Skipped\nStarts with underscore — ignored.")

    n = kg.bulk_import_knowledge_files(kdir)
    assert n == 2
    assert kg.backend.node_count() == 2
    alpha = kg.backend.get_node("kf:alpha")
    assert alpha is not None
    assert alpha.node_type == NodeType.KNOWLEDGE_FILE
    assert alpha.label == "Alpha"


def test_kg_structural_edges_idempotent(kg, tmp_path):
    # Two files linking each other via markdown link + bare file reference.
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    _write_md(kdir, "alpha", "# Alpha\nLinks to [Beta](beta.md).")
    _write_md(kdir, "beta", "# Beta\nLinks back to [Alpha](alpha.md).")
    kg.bulk_import_knowledge_files(kdir)

    n1 = kg.extract_structural_edges(kdir)
    n2 = kg.extract_structural_edges(kdir)
    # Idempotency: second extraction yields same edge_count as first
    # (clear_structural_edges runs at the start, then re-extracts).
    assert kg.backend.edge_count() > 0
    assert n1 == n2


def test_kg_persist_extracted_entities_writes_mentions(kg, tmp_path):
    # Pre-create a session archive node that entities will reference.
    kg.backend.add_node(GraphNode(
        node_id="archive:s125",
        node_type=NodeType.SESSION_ARCHIVE,
        label="S125 archive",
    ))
    synthetic_gliner_output = [
        {"entity": "Grafeo", "type": "technology", "source_id": "archive:s125", "score": 0.95},
        {"entity": "Cypher", "type": "concept", "source_id": "archive:s125", "score": 0.88},
        # Orphan — referenced session doesn't exist
        {"entity": "Ghost", "type": "concept", "source_id": "archive:missing", "score": 0.6},
    ]
    added = kg.persist_extracted_entities(synthetic_gliner_output)
    # 2 valid edges, 1 orphan dropped
    assert added == 2
    # All 3 Entity nodes still created (parity with bulk_upsert semantics)
    grafeo_node = kg.backend.get_node("e:grafeo")
    assert grafeo_node is not None
    assert grafeo_node.node_type == NodeType.ENTITY
    # MENTIONS edges only on the non-orphan side
    archive_edges = kg.backend.get_edges("archive:s125", direction="out")
    assert len(archive_edges) == 2
    assert all(e.edge_type == EdgeType.MENTIONS for e in archive_edges)


def test_kg_graph_expand_after_structural(kg, tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    _write_md(kdir, "alpha", "# Alpha\nReferences [Beta](beta.md) and [Gamma](gamma.md).")
    _write_md(kdir, "beta", "# Beta\nLinks to [Gamma](gamma.md).")
    _write_md(kdir, "gamma", "# Gamma\nLeaf.")
    kg.bulk_import_knowledge_files(kdir)
    kg.extract_structural_edges(kdir)

    expanded = kg.graph_expand(["alpha.md"], max_hops=1)
    expanded_paths = {row[0]["source_file"] for row in expanded}
    # 1-hop from alpha → both beta and gamma should appear
    assert "beta.md" in expanded_paths
    assert "gamma.md" in expanded_paths


def test_kg_invalidate_edges_bi_temporal(kg):
    kg.backend.add_node(GraphNode(node_id="a", node_type=NodeType.ENTITY, label="a"))
    kg.backend.add_node(GraphNode(node_id="b", node_type=NodeType.ENTITY, label="b"))
    kg.backend.add_edge(GraphEdge(
        source_id="a", target_id="b", edge_type=EdgeType.DEPENDS_ON,
    ))
    assert kg.backend.edge_count() == 1
    invalidated = kg.invalidate_edges("a")
    assert invalidated == 1
    edges = kg.backend.get_edges("a", direction="out")
    assert edges[0].t_invalidated is not None


def test_kg_close_reopen_persistence(tmp_path, request):
    # Persistence smoke test parameterized in-line — uses both backends
    # in sequence to verify each survives a close/reopen cycle without
    # data loss. Not parametrized via fixture because we need to control
    # backend selection per-phase.
    for backend in ("sqlite", "grafeo"):
        agent_root = tmp_path / backend / "agents" / "agent_001"
        agent_root.mkdir(parents=True)

        kg1 = KnowledgeGraph(agent_root, backend=backend)
        kg1.backend.add_node(GraphNode(
            node_id="persistent:1",
            node_type=NodeType.KNOWLEDGE_FILE,
            label="survives_reopen",
            properties={"check": "ok"},
        ))
        assert kg1.backend.node_count() == 1
        kg1.close()

        # Reopen with same backend → data must still be present
        kg2 = KnowledgeGraph(agent_root, backend=backend)
        try:
            assert kg2.backend.node_count() == 1, f"{backend}: data lost across reopen"
            fetched = kg2.backend.get_node("persistent:1")
            assert fetched is not None, f"{backend}: node lookup failed after reopen"
            assert fetched.label == "survives_reopen"
            assert fetched.properties == {"check": "ok"}
        finally:
            kg2.close()


def test_kg_clear_structural_then_reextract(kg, tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    _write_md(kdir, "alpha", "# Alpha\nLinks to [Beta](beta.md).")
    _write_md(kdir, "beta", "# Beta\nLeaf.")
    kg.bulk_import_knowledge_files(kdir)
    kg.extract_structural_edges(kdir)
    edges_before = kg.backend.edge_count()
    assert edges_before > 0

    cleared = kg.backend.clear_structural_edges()
    assert cleared == edges_before
    assert kg.backend.edge_count() == 0

    # Re-extract: same shape comes back
    kg.extract_structural_edges(kdir)
    assert kg.backend.edge_count() == edges_before
