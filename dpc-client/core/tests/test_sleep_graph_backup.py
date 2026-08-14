"""Sleep leaves the graph a copy of itself, and keeps the last few.

The class that makes this matter is `llm_relation`: 40.8% of the fleet's edges and
57.2% of warren's, written only by sleep and reconstructible from nothing outside the
store. Hanging the copy off sleep is right — it is the moment those edges have just
been written — but it buys a cadence, not a schedule: **nothing runs sleep by clock**,
a human does, and only on an empty chat. Seven kept dumps are seven sleeps. A quiet
week produces none, and these tests say "sleep", never "night", for that reason.
"""

from __future__ import annotations

import json

import pytest

from dpc_client_core.dpc_agent import sleep_pipeline as SP
from dpc_client_core.dpc_agent.knowledge_graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)


def _graph_with_content(agent_root):
    kg = KnowledgeGraph(agent_root, backend="sqlite")
    kg.backend.add_node(GraphNode(node_id="e:p2p", node_type=NodeType.ENTITY, label="p2p"))
    kg.backend.add_node(GraphNode(node_id="e:graph", node_type=NodeType.ENTITY, label="graph"))
    kg.backend.add_edge(GraphEdge(source_id="e:p2p", target_id="e:graph",
                                  edge_type=EdgeType.SUPPORTS, t_created="2026-08-14T00:00:00Z",
                                  properties={"source": "llm_relation"}))
    return kg


@pytest.fixture
def agent(tmp_path, monkeypatch):
    root = tmp_path / "agents" / "agent_test"
    root.mkdir(parents=True)
    # The helper imports get_agent_root at call time, so patching the module it comes
    # from is what takes effect.
    monkeypatch.setattr("dpc_client_core.dpc_agent.utils.get_agent_root", lambda agent_id: root)
    return root


def test_a_completed_sleep_leaves_a_restorable_copy(agent, tmp_path):
    _graph_with_content(agent)

    dump = SP._export_graph_snapshot("agent_test", tmp_path / "conversations" / "agent_test")

    assert dump is not None and dump.exists()
    assert dump.name.endswith("-nightly.jsonl"), "rotation only ever touches its own files"

    restored = KnowledgeGraph(tmp_path / "restored", backend="sqlite")
    result = restored.import_from(dump)
    assert (result["nodes"], result["edges"], result["skipped"]) == (2, 1, 0)
    assert restored.snapshot()["edges_by_source"] == {"llm_relation": 1}


def test_an_empty_graph_is_not_backed_up(agent, tmp_path):
    """A store with nothing in it produces no file to rotate later."""
    KnowledgeGraph(agent, backend="sqlite")
    assert SP._export_graph_snapshot("agent_test", tmp_path / "conversations" / "agent_test") is None


def test_only_the_last_few_sleeps_are_kept(agent, tmp_path):
    _graph_with_content(agent)
    export_dir = agent / SP.GRAPH_EXPORT_DIR
    export_dir.mkdir(exist_ok=True)
    for day in range(1, 13):
        (export_dir / f"202608{day:02d}T000000Z-nightly.jsonl").write_text("stale\n", encoding="utf-8")
    by_hand = export_dir / "before-the-migration.jsonl"
    by_hand.write_text("kept\n", encoding="utf-8")

    SP._export_graph_snapshot("agent_test", tmp_path / "conversations" / "agent_test")

    nightly = sorted(p.name for p in export_dir.glob("*-nightly.jsonl"))
    assert len(nightly) == SP.GRAPH_EXPORT_KEEP
    assert nightly[-1].startswith("2026"), "the newest survives"
    assert "20260801T000000Z-nightly.jsonl" not in nightly, "the oldest is rotated out"
    assert by_hand.exists(), "a dump taken by hand must survive rotation"


def test_a_backup_that_dies_halfway_leaves_the_previous_one_alone(agent, tmp_path, monkeypatch):
    """The night's copy is written beside the last good one, never over it."""
    _graph_with_content(agent)
    conv = tmp_path / "conversations" / "agent_test"
    first = SP._export_graph_snapshot("agent_test", conv)
    good = first.read_text(encoding="utf-8")

    from dpc_client_core.dpc_agent import knowledge_graph as KG

    def _die(self):
        yield from ()
        raise OSError("disk full")

    monkeypatch.setattr(KG.SQLiteGraphBackend, "iter_edges", _die)
    with pytest.raises(OSError):
        SP._export_graph_snapshot("agent_test", conv)

    assert first.read_text(encoding="utf-8") == good
    leftovers = list((agent / SP.GRAPH_EXPORT_DIR).glob("*.part"))
    assert leftovers, "the half-written file stays as .part and never claims the name"
    assert all(p.suffix == ".part" for p in leftovers)


def test_rotation_refuses_to_evict_history_when_the_graph_shrinks(agent, tmp_path):
    """Raised by Fable 5: rotation must not finish off a damaged graph.

    The standing symptom on this fleet is a store that reopens smaller. If that ever
    reaches the class nothing rebuilds, the graph is damaged-but-not-empty, sleep still
    completes, and seven more successful backups evict the last copy that still had
    those edges in it.
    """
    kg = _graph_with_content(agent)
    conv = tmp_path / "conversations" / "agent_test"
    export_dir = agent / SP.GRAPH_EXPORT_DIR

    # One real dump, then copies of it stamped earlier — the timestamp has second
    # resolution, so a loop of exports inside one second would all be the same file.
    good = SP._export_graph_snapshot("agent_test", conv)
    for day in range(1, SP.GRAPH_EXPORT_KEEP + 1):
        (export_dir / f"202608{day:02d}T000000Z-nightly.jsonl").write_text(
            good.read_text(encoding="utf-8"), encoding="utf-8")
    good.unlink()  # or the next export, landing in the same second, would overwrite it
    assert len(list(export_dir.glob("*-nightly.jsonl"))) == SP.GRAPH_EXPORT_KEEP

    # The graph loses its irreplaceable edge, and the next sleep still finishes.
    kg.backend.clear_structural_edges()
    for edge in list(kg.backend.iter_edges()):
        if edge.properties.get("source") == "llm_relation":
            kg.backend._conn.execute(
                "DELETE FROM edges WHERE source_id=? AND target_id=?",
                (edge.source_id, edge.target_id))
            kg.backend._conn.commit()
    assert kg.snapshot()["edges_by_source"].get("llm_relation", 0) == 0

    SP._export_graph_snapshot("agent_test", conv)

    kept = sorted(export_dir.glob("*-nightly.jsonl"))
    assert len(kept) == SP.GRAPH_EXPORT_KEEP + 1, "nothing may be evicted on a shrink"
    with kept[0].open(encoding="utf-8") as fh:
        oldest = json.loads(fh.readline())
    assert oldest["snapshot"]["edges_by_source"]["llm_relation"] == 1, (
        "the surviving history must still contain the edges that went missing"
    )
