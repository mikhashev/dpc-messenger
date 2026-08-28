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
    # Addressed by index key, the identity the index, the fuser and read_file all use.
    alpha = kg.backend.get_node("kf:knowledge/alpha.md")
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
    """The channel speaks index keys, because that is what everything else calls identity.

    It used to emit bare filenames. The fuser dedups on this string, so a graph hit for
    a document the vector channel had already found could not merge with it — RRF split
    the evidence instead of summing it — and no address could be built from the name.
    """
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    _write_md(kdir, "alpha", "# Alpha\nReferences [Beta](beta.md) and [Gamma](gamma.md).")
    _write_md(kdir, "beta", "# Beta\nLinks to [Gamma](gamma.md).")
    _write_md(kdir, "gamma", "# Gamma\nLeaf.")
    kg.bulk_import_knowledge_files(kdir, source_layer="L5")
    kg.extract_structural_edges(kdir)

    expanded = kg.graph_expand(["knowledge/alpha.md"], max_hops=1)
    expanded_paths = {row[0]["source_file"] for row in expanded}
    # 1-hop from alpha → both beta and gamma should appear, spelled as keys
    assert "knowledge/beta.md" in expanded_paths
    assert "knowledge/gamma.md" in expanded_paths


def test_kg_graph_expand_returns_an_addressable_meta(kg, tmp_path):
    """Whatever the channel returns has to survive the trip to a printed address."""
    from dpc_client_core.dpc_agent.active_recall import hint_address

    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    _write_md(kdir, "alpha", "# Alpha\nReferences [Beta](beta.md).")
    _write_md(kdir, "beta", "# Beta\nLeaf.")
    kg.bulk_import_knowledge_files(kdir, source_layer="L5")
    kg.extract_structural_edges(kdir)

    expanded = kg.graph_expand(["knowledge/alpha.md"], max_hops=1)
    assert expanded, "expected at least one neighbour"
    for meta, _score in expanded:
        assert meta["source_file"].startswith("knowledge/"), meta
        assert meta.get("source_path"), meta
        assert hint_address(meta, extended_read_enabled=True) is not None, meta


def test_kg_graph_expand_ignores_a_namesake(kg, tmp_path):
    """A stem match is a claim about a name; the key is what settles which document."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    _write_md(kdir, "alpha", "# Alpha\nReferences [Beta](beta.md).")
    _write_md(kdir, "beta", "# Beta\nLeaf.")
    kg.bulk_import_knowledge_files(kdir, source_layer="L5")
    kg.extract_structural_edges(kdir)

    # Same stem, different document: an external root holding its own alpha.md.
    assert kg.graph_expand(["EXT/some-project/docs/alpha.md"], max_hops=1) == []


def test_kg_two_layers_holding_one_stem_are_two_documents(kg, tmp_path):
    """The collision the old scheme had to guard against does not exist under keys.

    With `kf:<stem>` the second import silently overwrote the first document's node, so
    the code carried a guard that refused it — and that guard, reading the layer label
    to decide, refused the entire shared layer on its first run. The key carries the
    layer, so the two documents simply have two ids and neither has to be sacrificed.
    """
    l5 = tmp_path / "knowledge"
    l5.mkdir()
    _write_md(l5, "shared", "# Shared\nagent layer")
    l6 = tmp_path / "shared-knowledge"
    l6.mkdir()
    _write_md(l6, "shared", "# Shared\nhuman layer")

    assert kg.bulk_import_knowledge_files(l5, source_layer="L5") == 1
    assert kg.bulk_import_knowledge_files(l6, source_layer="L6") == 1

    own = kg.backend.get_node("kf:knowledge/shared.md")
    shared = kg.backend.get_node("kf:L6/shared.md")
    assert own.source_layer == "L5" and own.properties["path"] == "knowledge/shared.md"
    assert shared.source_layer == "L6" and shared.properties["path"] == "L6/shared.md"
    assert own.properties["source_path"] != shared.properties["source_path"]


def test_kg_a_node_written_before_keys_is_left_behind_not_mistaken_for_the_document(kg, tmp_path):
    """Stem-addressed rows survive the scheme change; they must not be treated as current.

    Under `kf:<stem>` a legacy row was the same id as the document, so an import either
    upgraded it or — as happened in production — was refused by a guard reading its
    stale label. Under keys it is a different id: the document gets its own node, and
    the old row is inert, holding no edges after the next extraction pass.
    """
    l6 = tmp_path / "shared-knowledge"
    l6.mkdir()
    _write_md(l6, "commit-note", "# Commit\nhuman layer")

    kg.backend.add_node(GraphNode(
        node_id="kf:commit-note",               # the old scheme: a stem
        node_type=NodeType.KNOWLEDGE_FILE,
        label="Commit Note",
        source_layer="L5",                      # what the old code wrote for every layer
        properties={"path": "commit-note.md"},  # bare name, no source_path
    ))

    assert kg.bulk_import_knowledge_files(l6, source_layer="L6") == 1

    current = kg.backend.get_node("kf:L6/commit-note.md")
    assert current.source_layer == "L6"
    assert current.properties["path"] == "L6/commit-note.md"
    assert current.properties["source_path"]

    stale = kg.backend.get_node("kf:commit-note")
    assert stale is not None and stale.properties["path"] == "commit-note.md"
    assert kg.graph_expand(["commit-note.md"], max_hops=1) == []  # unreachable as a seed


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
