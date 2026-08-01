"""The graph has to be able to describe itself, because nothing else can read it.

The store is held with an exclusive lock: a second process cannot open it, and on
Windows cannot even copy it. So the only graph a reviewer could open was whatever
stale file lay next to the live one — which is exactly what three analyses in a row
measured while reporting it as production. The counts have to come from the process
holding the database.

What the snapshot is for, specifically: the key-as-id migration rested on the claim
that every edge touching a knowledge-file node is structural, and therefore rewritten
on the next indexing pass. The total edge count cannot test that claim — an orphaned
edge is still an edge — so the breakdown has to be explicit.
"""

from __future__ import annotations

import pytest

from dpc_client_core.dpc_agent.knowledge_graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
    node_id_for,
)

pytest.importorskip("grafeo")


@pytest.fixture(params=["sqlite", "grafeo"])
def kg(request, tmp_path):
    root = tmp_path / request.param
    root.mkdir(parents=True)
    instance = KnowledgeGraph(root, backend=request.param)
    yield instance
    instance.close()


def _kf(kg, key: str) -> str:
    node_id = node_id_for(key)
    kg.backend.add_node(GraphNode(
        node_id=node_id, node_type=NodeType.KNOWLEDGE_FILE, label=key,
        source_layer="L5", properties={"path": key, "source_path": f"C:/x/{key}"},
    ))
    return node_id


def _entity(kg, name: str) -> str:
    kg.backend.add_node(GraphNode(node_id=f"e:{name}", node_type=NodeType.ENTITY, label=name))
    return f"e:{name}"


def test_the_snapshot_names_the_store_it_read(kg, tmp_path):
    """The first question anyone should ask, and the one nobody asked for three rounds."""
    snap = kg.snapshot()

    assert snap["backend"] in ("sqlite", "grafeo")
    assert snap["backend"] in snap["store_path"] or snap["store_path"].endswith(
        (".db", ".grafeo"))


def test_empty_graph_reports_zeros_rather_than_failing(kg):
    snap = kg.snapshot()

    assert snap["nodes_total"] == 0
    assert snap["edges_total"] == 0
    assert snap["kf_edges_non_structural"] == 0


def test_nodes_and_edges_are_broken_down_by_type(kg):
    a = _kf(kg, "knowledge/alpha.md")
    b = _kf(kg, "knowledge/beta.md")
    e = _entity(kg, "p2p")
    kg.backend.add_edge(GraphEdge(source_id=a, target_id=b, edge_type=EdgeType.DEPENDS_ON,
                                  properties={"source": "structural"}))
    kg.backend.add_edge(GraphEdge(source_id=a, target_id=e, edge_type=EdgeType.MENTIONS,
                                  properties={"source": "gliner_ner"}))

    snap = kg.snapshot()

    assert snap["nodes_by_type"]["KnowledgeFile"] == 2
    assert snap["nodes_by_type"]["Entity"] == 1
    assert snap["edges_by_type"]["DEPENDS_ON"] == 1
    assert snap["edges_by_type"]["MENTIONS"] == 1
    assert snap["nodes_total"] == 3 and snap["edges_total"] == 2


def test_a_non_structural_edge_on_a_knowledge_file_is_counted_as_such(kg):
    """The number the migration argument needed and never had.

    A MENTIONS edge from GLiNER is not rewritten by the indexing pass, so a change of
    node identity leaves it pointing at a node no query reaches. It would still be
    counted in the total — which is why the total proved nothing.
    """
    a = _kf(kg, "knowledge/alpha.md")
    e = _entity(kg, "p2p")
    kg.backend.add_edge(GraphEdge(source_id=a, target_id=e, edge_type=EdgeType.MENTIONS,
                                  properties={"source": "gliner_ner"}))

    snap = kg.snapshot()

    assert snap["kf_edges_total"] == 1
    assert snap["kf_edges_structural"] == 0
    assert snap["kf_edges_non_structural"] == 1


def test_structural_and_non_structural_are_separated_not_summed(kg):
    a = _kf(kg, "knowledge/alpha.md")
    b = _kf(kg, "knowledge/beta.md")
    e = _entity(kg, "p2p")
    kg.backend.add_edge(GraphEdge(source_id=a, target_id=b, edge_type=EdgeType.DEPENDS_ON,
                                  properties={"source": "structural"}))
    kg.backend.add_edge(GraphEdge(source_id=b, target_id=e, edge_type=EdgeType.MENTIONS,
                                  properties={"source": "gliner_ner"}))

    snap = kg.snapshot()

    assert snap["kf_edges_structural"] == 1
    assert snap["kf_edges_non_structural"] == 1


def test_edges_between_two_entities_are_not_attributed_to_knowledge_files(kg):
    """Otherwise every GLiNER edge in the graph would look like a migration risk."""
    x, y = _entity(kg, "p2p"), _entity(kg, "context")
    kg.backend.add_edge(GraphEdge(source_id=x, target_id=y, edge_type=EdgeType.SUPPORTS,
                                  properties={"source": "llm_relation"}))

    snap = kg.snapshot()

    assert snap["edges_total"] == 1
    assert snap["kf_edges_total"] == 0


def test_legacy_stem_nodes_are_counted_apart_from_keyed_ones(kg):
    """Both live in the store after the migration — the old ones were never deleted,
    because there is no node-delete API. Reporting one number for both would hide
    exactly what a reader is trying to see."""
    _kf(kg, "knowledge/alpha.md")
    kg.backend.add_node(GraphNode(
        node_id="kf:alpha", node_type=NodeType.KNOWLEDGE_FILE, label="Alpha",
        source_layer="L5", properties={"path": "alpha.md"},
    ))

    snap = kg.snapshot()

    assert snap["kf_nodes_keyed"] == 1
    assert snap["kf_nodes_legacy_stem"] == 1


def test_both_backends_answer_in_the_same_shape(tmp_path):
    """A reader comparing two stores should be comparing numbers, not formats."""
    shapes = []
    for backend in ("sqlite", "grafeo"):
        root = tmp_path / f"same_{backend}"
        root.mkdir(parents=True)
        kg = KnowledgeGraph(root, backend=backend)
        try:
            a = _kf(kg, "knowledge/alpha.md")
            b = _kf(kg, "knowledge/beta.md")
            kg.backend.add_edge(GraphEdge(source_id=a, target_id=b,
                                          edge_type=EdgeType.DEPENDS_ON,
                                          properties={"source": "structural"}))
            shapes.append(kg.snapshot())
        finally:
            kg.close()

    assert set(shapes[0]) == set(shapes[1])
    for field in ("nodes_total", "edges_total", "kf_edges_total",
                  "kf_edges_structural", "kf_edges_non_structural",
                  "kf_nodes_keyed", "kf_nodes_legacy_stem"):
        assert shapes[0][field] == shapes[1][field], field
