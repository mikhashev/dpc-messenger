"""The service command is the only place a live graph can be dumped from.

A second process cannot open a live `.grafeo` at all — copy fails with
PermissionError and leaves a torn zero-byte file, open fails GRAFEO-X003,
open_read_only fails GRAFEO-X001, all measured on this machine. So the export has to
run through the handle the service already holds, and it has to be reachable from
the UI, which means the command allowlist.
"""

from __future__ import annotations

import json

import pytest

from dpc_client_core import local_api
from dpc_client_core.dpc_agent.knowledge_graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)


def test_the_command_is_reachable_from_the_ui():
    """An unlisted command is invisible however well it works."""
    assert "export_knowledge_graph" in local_api.ALLOWED_COMMANDS


@pytest.mark.asyncio
async def test_it_dumps_the_graph_the_service_already_holds(tmp_path, monkeypatch):
    """And it must be that instance, not a fresh one — a second handle would answer
    for a different open file, which is the whole failure this exists to end."""
    from dpc_client_core import service as service_module

    agent_root = tmp_path / "agents" / "agent_test"
    agent_root.mkdir(parents=True)
    kg = KnowledgeGraph(agent_root, backend="sqlite")
    kg.backend.add_node(GraphNode(node_id="e:p2p", node_type=NodeType.ENTITY, label="p2p"))
    kg.backend.add_node(GraphNode(node_id="e:graph", node_type=NodeType.ENTITY, label="graph"))
    kg.backend.add_edge(GraphEdge(source_id="e:p2p", target_id="e:graph",
                                  edge_type=EdgeType.SUPPORTS, t_created="2026-08-14T00:00:00Z",
                                  properties={"source": "llm_relation"}))

    monkeypatch.setattr(service_module, "DPC_HOME_DIR", tmp_path)
    monkeypatch.setattr("dpc_client_core.dpc_agent.context._get_knowledge_graph",
                        lambda root: kg)

    svc = service_module.CoreService.__new__(service_module.CoreService)
    result = await service_module.CoreService.export_knowledge_graph(svc, agent_id="agent_test")

    assert result["status"] == "ok"
    assert (result["nodes"], result["edges"]) == (2, 1)
    assert result["bytes"] > 0

    records = [json.loads(line) for line in
               open(result["path"], encoding="utf-8").read().splitlines()]
    assert records[0]["kind"] == "header"
    assert records[0]["snapshot"]["edges_by_source"] == {"llm_relation": 1}
    assert result["path"].endswith(".jsonl")
    assert "knowledge_graph_export" in result["path"], "default location, per agent"


@pytest.mark.asyncio
async def test_an_unknown_agent_is_an_error_not_an_exception(tmp_path, monkeypatch):
    from dpc_client_core import service as service_module

    monkeypatch.setattr(service_module, "DPC_HOME_DIR", tmp_path)
    svc = service_module.CoreService.__new__(service_module.CoreService)
    result = await service_module.CoreService.export_knowledge_graph(svc, agent_id="nobody")
    assert result["status"] == "error" and "No such agent" in result["message"]
