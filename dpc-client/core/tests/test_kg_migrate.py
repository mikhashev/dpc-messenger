"""The gate has to fail on the loss that matters and pass on the churn that does not.

A migration check that compares totals would do neither: structural edges are cleared
and rebuilt on every pass, so totals move by over a thousand on their own, while a
missing `llm_relation` edge — the class with no source outside the store — is a single
row nobody would notice.
"""

from __future__ import annotations

import json

import pytest

import kg_migrate
from dpc_client_core.dpc_agent.knowledge_graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)


def _graph(root, *, llm=2, structural=3, gliner=1):
    kg = KnowledgeGraph(root, backend="sqlite")
    kg.backend.add_node(GraphNode(node_id="e:a", node_type=NodeType.ENTITY, label="a"))
    kg.backend.add_node(GraphNode(node_id="e:b", node_type=NodeType.ENTITY, label="b"))
    kg.backend.add_node(GraphNode(node_id="kf:knowledge/x.md",
                                  node_type=NodeType.KNOWLEDGE_FILE, label="x.md"))
    for i in range(llm):
        kg.backend.add_edge(GraphEdge(source_id="e:a", target_id="e:b",
                                      edge_type=EdgeType.SUPPORTS, justification=f"llm {i}",
                                      properties={"source": "llm_relation"}))
    for i in range(structural):
        kg.backend.add_edge(GraphEdge(source_id="kf:knowledge/x.md", target_id="e:a",
                                      edge_type=EdgeType.MENTIONS, justification=f"struct {i}",
                                      properties={"source": "structural"}))
    for i in range(gliner):
        kg.backend.add_edge(GraphEdge(source_id="kf:knowledge/x.md", target_id="e:b",
                                      edge_type=EdgeType.MENTIONS, justification=f"gliner {i}",
                                      properties={"source": "gliner_ner"}))
    return kg


def _dump(kg, path):
    kg.export_to(path)
    return path


def _verify(before, after, expect_dropped=0, capsys=None):
    args = kg_migrate.argparse.Namespace(before=str(before), after=str(after),
                                         expect_dropped=expect_dropped)
    return kg_migrate.verify(args)


def test_the_gate_passes_when_the_irreplaceable_class_arrives_whole(tmp_path, capsys):
    src = _graph(tmp_path / "src")
    before = _dump(src, tmp_path / "before.jsonl")
    dst = KnowledgeGraph(tmp_path / "dst", backend="grafeo")
    dst.import_from(before)
    after = _dump(dst, tmp_path / "after.jsonl")

    assert _verify(before, after) == 0
    assert "GATE PASSED" in capsys.readouterr().out


def test_the_gate_fails_on_one_missing_llm_relation_edge(tmp_path, capsys):
    src = _graph(tmp_path / "src")
    before = _dump(src, tmp_path / "before.jsonl")

    kept = [l for l in before.read_text(encoding="utf-8").splitlines()
            if '"source": "llm_relation"' not in l or 'llm 1' not in l]
    after = tmp_path / "after.jsonl"
    after.write_text("\n".join(kept) + "\n", encoding="utf-8")

    assert _verify(before, after) == 1
    out = capsys.readouterr().out
    assert "GATE FAILED" in out and "llm_relation edges vanished" in out


def test_structural_churn_alone_does_not_fail_the_gate(tmp_path, capsys):
    """The class that a pass rebuilds moves by over a thousand on agent_001 by itself."""
    src = _graph(tmp_path / "src", structural=3)
    before = _dump(src, tmp_path / "before.jsonl")

    dst = KnowledgeGraph(tmp_path / "dst", backend="sqlite")
    dst.import_from(before)
    dst.backend.clear_structural_edges()
    after = _dump(dst, tmp_path / "after.jsonl")

    assert _verify(before, after) == 0, "structural edges come back on the next pass"
    assert "GATE PASSED" in capsys.readouterr().out


def test_a_node_that_vanishes_undeclared_fails_and_declaring_it_passes(tmp_path, capsys):
    src = _graph(tmp_path / "src")
    before = _dump(src, tmp_path / "before.jsonl")

    lines = [l for l in before.read_text(encoding="utf-8").splitlines()
             if '"kf:knowledge/x.md"' not in l]
    after = tmp_path / "after.jsonl"
    after.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert _verify(before, after) == 1
    assert "a delta has to be written down" in capsys.readouterr().out

    assert _verify(before, after, expect_dropped=1) == 0
    assert "GATE PASSED" in capsys.readouterr().out


def test_prepare_moves_the_old_store_aside_before_building_the_new_one(tmp_path, monkeypatch):
    """The file a config flip would otherwise have opened in silence."""
    monkeypatch.setattr(kg_migrate, "DPC_HOME", tmp_path)
    agent_root = tmp_path / "agents" / "agent_pilot"
    agent_root.mkdir(parents=True)

    stale = _graph(agent_root, llm=1, structural=1, gliner=0)
    stale.backend.close()
    assert (agent_root / "knowledge_graph.db").exists()

    src = _graph(tmp_path / "src", llm=2, structural=3)
    dump = _dump(src, tmp_path / "dump.jsonl")

    args = kg_migrate.argparse.Namespace(dump=str(dump), agent="agent_pilot", to="sqlite")
    assert kg_migrate.prepare(args) == 0

    aside = list(agent_root.glob("knowledge_graph.db.pre-migration-*"))
    assert len(aside) == 1, "the old store is kept, not deleted, and not opened"
    rebuilt = KnowledgeGraph(agent_root, backend="sqlite")
    assert rebuilt.snapshot()["edges_by_source"]["llm_relation"] == 2
    rebuilt.backend.close()


def test_a_relabelled_node_cannot_launder_a_lost_edge(tmp_path, capsys):
    """Found by Fable 5 in review, demonstrated before it was fixed.

    `dropped_ids` was read off the difference of node *tuples*, so a node that survives
    with a changed label, layer or exempt flag looked dropped — and every genuinely
    lost edge touching it was then filed as collateral. The transform this gate exists
    for is a declared node drop, which is exactly when node tuples change.
    """
    src = _graph(tmp_path / "src", llm=1, structural=0, gliner=0)
    before = _dump(src, tmp_path / "before.jsonl")

    rewritten = []
    for line in before.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("kind") == "edge":
            continue  # the llm_relation edge is lost outright
        if record.get("node_id") == "e:a":
            record["label"] = "a (renamed by the transform)"
        rewritten.append(json.dumps(record, ensure_ascii=False))
    after = tmp_path / "after.jsonl"
    after.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    rc = _verify(before, after, expect_dropped=1)
    out = capsys.readouterr().out
    assert rc == 1, "a node that is still there cannot excuse the loss of its edges"
    assert "llm_relation edges vanished" in out
