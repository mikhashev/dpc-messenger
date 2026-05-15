"""Parity tests between SQLiteGraphBackend and GrafeoGraphBackend (ADR-024 Phase 2).

Covers the 4 methods landed in Phase 2: __init__, init_schema, add_node,
get_node, node_count. Each test runs against both backends parametrized
by fixture; behaviour must match.

Skips Grafeo-side tests if the `grafeo` package is not installed
(`uv sync --extra graph-grafeo`).
"""

from __future__ import annotations

import pytest

from dpc_client_core.dpc_agent.knowledge_graph import (
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
