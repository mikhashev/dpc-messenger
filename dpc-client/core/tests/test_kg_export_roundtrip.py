"""A graph must survive leaving its store and coming back into the other one.

Until 2026-08-14 it could not leave at all: the interface could add a node and count
nodes and never enumerate one, so an agent's graph had no backup, no way to be read
from outside the process holding it, and no path to the other backend. A third to a
half of every agent's edges is the one class that no pass rebuilds — 1589 of 4754 on
agent_001, 1491 of 2608 on warren, measured — so "carry it exactly" is not a nicety.
"""

from __future__ import annotations

import json

import pytest

from dpc_client_core.dpc_agent.knowledge_graph import (
    DUMP_FORMAT,
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)

BACKENDS = ["sqlite", "grafeo"]


def _populate(kg: KnowledgeGraph) -> None:
    """One node of every kind and one edge of every source class."""
    kg.backend.add_node(GraphNode(node_id="kf:knowledge/faq.md", node_type=NodeType.KNOWLEDGE_FILE,
                                  label="faq.md", source_layer="L5",
                                  properties={"file_mtime": "2026-08-01T00:00:00Z"}))
    kg.backend.add_node(GraphNode(node_id="e:p2p", node_type=NodeType.ENTITY, label="p2p",
                                  source_layer="L7", exempt=True))
    kg.backend.add_node(GraphNode(node_id="e:грaфы", node_type=NodeType.ENTITY,
                                  label="графы — unicode and a dash"))
    kg.backend.add_node(GraphNode(node_id="sa:2026-08-01", node_type=NodeType.SESSION_ARCHIVE,
                                  label="archive"))
    kg.backend.add_edge(GraphEdge(source_id="kf:knowledge/faq.md", target_id="e:p2p",
                                  edge_type=EdgeType.MENTIONS, t_created="2026-08-01T00:00:00Z",
                                  justification="tag", properties={"source": "structural"}))
    kg.backend.add_edge(GraphEdge(source_id="sa:2026-08-01", target_id="e:p2p",
                                  edge_type=EdgeType.MENTIONS, t_created="2026-08-02T00:00:00Z",
                                  confidence=0.68, justification="GLiNER extracted",
                                  properties={"source": "gliner_ner"}))
    kg.backend.add_edge(GraphEdge(source_id="e:p2p", target_id="e:грaфы",
                                  edge_type=EdgeType.SUPPORTS, t_created="2026-08-03T00:00:00Z",
                                  confidence=0.5, justification="the irreplaceable class",
                                  edge_weight="high",
                                  properties={"source": "llm_relation", "needs_review": True}))


def _as_sets(kg: KnowledgeGraph):
    nodes = {
        (n.node_id, n.node_type.value, n.label, n.source_layer, bool(n.exempt),
         json.dumps(n.properties, sort_keys=True, ensure_ascii=False))
        for n in kg.backend.iter_nodes()
    }
    edges = {
        (e.source_id, e.target_id, e.edge_type.value, e.t_created, e.t_invalidated,
         e.confidence, e.justification, e.edge_weight,
         json.dumps(e.properties, sort_keys=True, ensure_ascii=False))
        for e in kg.backend.iter_edges()
    }
    return nodes, edges


@pytest.mark.parametrize("source_backend", BACKENDS)
@pytest.mark.parametrize("target_backend", BACKENDS)
def test_a_graph_survives_a_trip_through_a_dump(tmp_path, source_backend, target_backend):
    source = KnowledgeGraph(tmp_path / "src", backend=source_backend)
    _populate(source)
    written = source.export_to(tmp_path / "dump.jsonl")
    assert written == {"nodes": 4, "edges": 3, "path": str(tmp_path / "dump.jsonl")}

    target = KnowledgeGraph(tmp_path / "dst", backend=target_backend)
    read = target.import_from(tmp_path / "dump.jsonl")
    assert (read["nodes"], read["edges"]) == (4, 3)

    src_nodes, src_edges = _as_sets(source)
    dst_nodes, dst_edges = _as_sets(target)
    assert dst_nodes == src_nodes
    assert dst_edges == src_edges, "an edge lost a field on the way across"


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_dump_is_readable_without_this_codebase(tmp_path, backend):
    """Header first, nodes before edges — a plain JSONL reader must cope."""
    kg = KnowledgeGraph(tmp_path / "g", backend=backend)
    _populate(kg)
    kg.export_to(tmp_path / "dump.jsonl")

    records = [json.loads(line) for line in
               (tmp_path / "dump.jsonl").read_text(encoding="utf-8").splitlines()]
    assert records[0]["kind"] == "header" and records[0]["format"] == DUMP_FORMAT
    assert records[0]["snapshot"]["edges_by_source"] == {
        "gliner_ner": 1, "llm_relation": 1, "structural": 1}
    kinds = [r["kind"] for r in records[1:]]
    assert kinds == ["node"] * 4 + ["edge"] * 3, (
        "edges must follow every node, or an import hits a missing endpoint"
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_importing_over_a_populated_graph_is_refused(tmp_path, backend):
    """Mixing two histories by accident is the failure this guard exists for."""
    kg = KnowledgeGraph(tmp_path / "g", backend=backend)
    _populate(kg)
    kg.export_to(tmp_path / "dump.jsonl")

    with pytest.raises(ValueError, match="refusing to import"):
        kg.import_from(tmp_path / "dump.jsonl")

    merged = kg.import_from(tmp_path / "dump.jsonl", merge=True)
    assert merged["nodes"] == 4, "merge=True must still be allowed to say so"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_foreign_dump_is_rejected_by_its_header(tmp_path, backend):
    (tmp_path / "alien.jsonl").write_text(
        json.dumps({"kind": "header", "format": "something-else/9"}) + "\n", encoding="utf-8")
    kg = KnowledgeGraph(tmp_path / "g", backend=backend)
    with pytest.raises(ValueError, match="not a dpc-kg-dump"):
        kg.import_from(tmp_path / "alien.jsonl")


@pytest.mark.parametrize("backend", BACKENDS)
def test_an_interrupted_export_does_not_replace_the_last_good_one(tmp_path, monkeypatch, backend):
    kg = KnowledgeGraph(tmp_path / "g", backend=backend)
    _populate(kg)
    kg.export_to(tmp_path / "dump.jsonl")
    good = (tmp_path / "dump.jsonl").read_text(encoding="utf-8")

    def _explode(self):
        yield from ()
        raise RuntimeError("disk went away mid-dump")

    monkeypatch.setattr(type(kg.backend), "iter_edges", _explode)
    with pytest.raises(RuntimeError):
        kg.export_to(tmp_path / "dump.jsonl")

    assert (tmp_path / "dump.jsonl").read_text(encoding="utf-8") == good


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_truncated_dump_is_refused_instead_of_restoring_short(tmp_path, backend):
    """The failure this guard exists for: a backup that comes back smaller in silence.

    Both reviewers caught that the first version checked only the format, and only if
    a header happened to appear — so a dump cut short, or one that lost its first
    line, imported cleanly with fewer edges and nothing said.
    """
    source = KnowledgeGraph(tmp_path / "src", backend=backend)
    _populate(source)
    source.export_to(tmp_path / "dump.jsonl")
    lines = (tmp_path / "dump.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)

    (tmp_path / "cut.jsonl").write_text("".join(lines[:-1]), encoding="utf-8")
    target = KnowledgeGraph(tmp_path / "cut", backend=backend)
    with pytest.raises(ValueError, match="incomplete"):
        target.import_from(tmp_path / "cut.jsonl")

    (tmp_path / "headless.jsonl").write_text("".join(lines[1:]), encoding="utf-8")
    other = KnowledgeGraph(tmp_path / "headless", backend=backend)
    with pytest.raises(ValueError, match="does not begin with a dump header"):
        other.import_from(tmp_path / "headless.jsonl")


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_record_this_version_does_not_understand_is_skipped_not_fatal(tmp_path, backend):
    """A dump from a later version must load what it can, and say what it could not.

    Skipping a node means skipping the edges that touch it, or the import dies on a
    missing endpoint and leaves half a graph behind.
    """
    source = KnowledgeGraph(tmp_path / "src", backend=backend)
    _populate(source)
    source.export_to(tmp_path / "dump.jsonl")

    lines = (tmp_path / "dump.jsonl").read_text(encoding="utf-8").splitlines()
    rewritten = []
    for line in lines:
        record = json.loads(line)
        if record.get("node_id") == "e:p2p":
            record["node_type"] = "Hypergraph"  # a type invented after this version
        rewritten.append(json.dumps(record, ensure_ascii=False))
    (tmp_path / "future.jsonl").write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    target = KnowledgeGraph(tmp_path / "dst", backend=backend)
    result = target.import_from(tmp_path / "future.jsonl")

    assert result["nodes"] == 3, "the three known nodes must still arrive"
    assert result["skipped"] == 4, "the unknown node and its three edges"
    assert target.backend.node_count() == 3 and target.backend.edge_count() == 0


def test_two_facades_on_one_root_share_the_live_handle():
    """The property the nightly backup rests on, pinned rather than inherited.

    Ark flagged that `_export_graph_snapshot` builds its own `KnowledgeGraph` instead
    of going through the service's cached one, and asked whether a second in-process
    handle on a live `.grafeo` works at all — a second *process* certainly does not
    (PermissionError / X003 / X001, measured). It works because the backend keys a
    singleton on the resolved path, so both facades end up on one `GrafeoDB`. That is
    also why nothing on the backup path may call close(): it would shut the store the
    service is still using.
    """
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp())
    first = KnowledgeGraph(root, backend="grafeo")
    second = KnowledgeGraph(root, backend="grafeo")
    try:
        assert first.backend._db is second.backend._db, (
            "a second facade must not open a second handle on a live store"
        )
        first.backend.add_node(GraphNode(node_id="e:x", node_type=NodeType.ENTITY, label="x"))
        assert second.backend.node_count() == 1, "and both must see the same graph"
    finally:
        first.backend.close()
