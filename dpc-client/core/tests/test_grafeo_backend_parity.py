"""Parity tests between SQLiteGraphBackend and GrafeoGraphBackend (ADR-024 Phase 2 + 2.5).

Phase 2 (S123): __init__, init_schema, add_node, get_node, node_count.
Phase 2.5 (S125 Group A): add_edge, get_edges, get_neighbors, edge_count,
edge_exists, close.

Each test runs against both backends parametrized by fixture; behaviour
must match.

Skips Grafeo-side tests if the `grafeo` package is not installed
(`uv sync --extra graph-grafeo`).
"""

from __future__ import annotations

import pytest

from dpc_client_core.dpc_agent.knowledge_graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    SQLiteGraphBackend,
)

grafeo = pytest.importorskip("grafeo")

from dpc_client_core.dpc_agent.knowledge_graph import GrafeoGraphBackend  # noqa: E402


@pytest.fixture(params=["sqlite", "grafeo"])
def backend(request, tmp_path):
    if request.param == "sqlite":
        b = SQLiteGraphBackend(tmp_path / "kg.db")
    else:
        b = GrafeoGraphBackend(tmp_path / "kg.grafeo")
    yield b


def test_empty_node_count(backend):
    assert backend.node_count() == 0


def test_add_node_then_count(backend):
    backend.add_node(GraphNode(
        node_id="n1",
        node_type=NodeType.KNOWLEDGE_FILE,
        label="alpha.md",
    ))
    assert backend.node_count() == 1


def test_add_multiple_nodes_count(backend):
    for i in range(5):
        backend.add_node(GraphNode(
            node_id=f"n{i}",
            node_type=NodeType.KNOWLEDGE_FILE,
            label=f"file_{i}.md",
        ))
    assert backend.node_count() == 5


def test_get_node_roundtrip_minimal(backend):
    original = GraphNode(
        node_id="n1",
        node_type=NodeType.ENTITY,
        label="alpha",
    )
    backend.add_node(original)
    fetched = backend.get_node("n1")
    assert fetched is not None
    assert fetched.node_id == "n1"
    assert fetched.node_type == NodeType.ENTITY
    assert fetched.label == "alpha"
    assert fetched.source_layer == "L7"
    assert fetched.properties == {}


def test_get_node_roundtrip_with_properties(backend):
    original = GraphNode(
        node_id="n2",
        node_type=NodeType.DECISION,
        label="adr-024 phase 2",
        source_layer="L7",
        exempt=False,
        properties={"author": "mike", "tags": ["adr", "migration"], "score": 0.87},
    )
    backend.add_node(original)
    fetched = backend.get_node("n2")
    assert fetched is not None
    assert fetched.properties == original.properties


def test_get_node_missing_returns_none(backend):
    assert backend.get_node("does-not-exist") is None


def test_always_exempt_node_types_set_exempt_true(backend):
    # DECISION and SESSION_ARCHIVE are in ALWAYS_EXEMPT — backend should
    # promote exempt=True regardless of input value.
    backend.add_node(GraphNode(
        node_id="d1",
        node_type=NodeType.DECISION,
        label="d",
        exempt=False,  # caller said False, but ALWAYS_EXEMPT overrides
    ))
    assert backend.get_node("d1").exempt is True


# ─── Phase 2.5 Group A: edges ──────────────────────────────────────


def _add_chain(backend, n: int = 3) -> None:
    """Helper: insert nodes n0..n{n-1} and edges n0→n1→...→n{n-1} (DEPENDS_ON)."""
    for i in range(n):
        backend.add_node(GraphNode(
            node_id=f"n{i}",
            node_type=NodeType.ENTITY,
            label=f"node_{i}",
        ))
    for i in range(n - 1):
        backend.add_edge(GraphEdge(
            source_id=f"n{i}",
            target_id=f"n{i+1}",
            edge_type=EdgeType.DEPENDS_ON,
            t_created="2026-05-17",
            confidence=0.9,
        ))


def test_empty_edge_count(backend):
    assert backend.edge_count() == 0


def test_add_edge_then_count(backend):
    _add_chain(backend, n=2)
    assert backend.edge_count() == 1


def test_add_multiple_edges_count(backend):
    _add_chain(backend, n=5)
    assert backend.edge_count() == 4


def test_get_edges_out(backend):
    _add_chain(backend, n=3)
    out = backend.get_edges("n1", direction="out")
    assert len(out) == 1
    assert out[0].source_id == "n1"
    assert out[0].target_id == "n2"
    assert out[0].edge_type == EdgeType.DEPENDS_ON


def test_get_edges_in(backend):
    _add_chain(backend, n=3)
    incoming = backend.get_edges("n1", direction="in")
    assert len(incoming) == 1
    assert incoming[0].source_id == "n0"
    assert incoming[0].target_id == "n1"


def test_get_edges_both(backend):
    _add_chain(backend, n=3)
    both = backend.get_edges("n1", direction="both")
    assert len(both) == 2
    sources = {e.source_id for e in both}
    targets = {e.target_id for e in both}
    assert sources == {"n0", "n1"}
    assert targets == {"n1", "n2"}


def test_get_edges_preserves_metadata(backend):
    backend.add_node(GraphNode(node_id="a", node_type=NodeType.ENTITY, label="a"))
    backend.add_node(GraphNode(node_id="b", node_type=NodeType.ENTITY, label="b"))
    backend.add_edge(GraphEdge(
        source_id="a", target_id="b", edge_type=EdgeType.SUPPORTS,
        t_created="2026-05-17", confidence=0.75,
        justification="parity test", edge_weight="high",
        properties={"channel": "test", "score": 0.42},
    ))
    edges = backend.get_edges("a", direction="out")
    assert len(edges) == 1
    e = edges[0]
    assert e.t_created == "2026-05-17"
    assert e.confidence == 0.75
    assert e.justification == "parity test"
    assert e.edge_weight == "high"
    assert e.properties == {"channel": "test", "score": 0.42}


def test_edge_exists_after_add(backend):
    _add_chain(backend, n=2)
    assert backend.edge_exists("n0", "n1", EdgeType.DEPENDS_ON) is True


def test_edge_exists_missing_returns_false(backend):
    _add_chain(backend, n=2)
    # Wrong direction
    assert backend.edge_exists("n1", "n0", EdgeType.DEPENDS_ON) is False
    # Wrong edge type
    assert backend.edge_exists("n0", "n1", EdgeType.SUPPORTS) is False
    # Wrong nodes
    assert backend.edge_exists("nope", "n1", EdgeType.DEPENDS_ON) is False


def test_get_neighbors_single_hop(backend):
    _add_chain(backend, n=3)
    nbrs = backend.get_neighbors("n1", hops=1)
    ids = {n.node_id for n in nbrs}
    assert ids == {"n0", "n2"}


def test_get_neighbors_two_hop(backend):
    _add_chain(backend, n=3)
    nbrs = backend.get_neighbors("n0", hops=2)
    ids = {n.node_id for n in nbrs}
    assert ids == {"n1", "n2"}


def test_get_neighbors_filter_by_edge_type(backend):
    backend.add_node(GraphNode(node_id="x", node_type=NodeType.ENTITY, label="x"))
    backend.add_node(GraphNode(node_id="y", node_type=NodeType.ENTITY, label="y"))
    backend.add_node(GraphNode(node_id="z", node_type=NodeType.ENTITY, label="z"))
    backend.add_edge(GraphEdge(
        source_id="x", target_id="y", edge_type=EdgeType.DEPENDS_ON,
    ))
    backend.add_edge(GraphEdge(
        source_id="x", target_id="z", edge_type=EdgeType.SUPPORTS,
    ))
    nbrs_depends = backend.get_neighbors("x", edge_type=EdgeType.DEPENDS_ON, hops=1)
    nbrs_supports = backend.get_neighbors("x", edge_type=EdgeType.SUPPORTS, hops=1)
    assert {n.node_id for n in nbrs_depends} == {"y"}
    assert {n.node_id for n in nbrs_supports} == {"z"}


def test_close_does_not_raise(backend):
    # close() must not raise on an active backend; double-close behaviour
    # is intentionally unspecified (SQLite raises, Grafeo varies).
    backend.close()
