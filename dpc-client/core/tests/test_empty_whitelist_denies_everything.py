"""Denying every tool used to be the same thing as allowing every tool.

The gate read `if ctx.tool_whitelist and name not in ctx.tool_whitelist`. An empty set is
falsy, so the check was skipped whole — and an empty set is exactly what the firewall
returns when a user unticks every tool for an agent, or when the agent is switched off
while running. In that state the registry offered the model all 69 tools, restricted ones
included, and executed them.

`None` and `set()` have to mean opposite things: nobody answered, versus the answer was
"nothing". These tests hold both, at both gates — the schema list the model is shown and
the call that runs.
"""
from __future__ import annotations

import pathlib

import pytest

from dpc_client_core.dpc_agent.tools.registry import (
    RESTRICTED_TOOL_NAMES,
    ToolContext,
    ToolRegistry,
)


@pytest.fixture
def registry(tmp_path):
    return ToolRegistry(agent_root=tmp_path)


def _names(registry) -> set:
    return {s["function"]["name"] for s in registry.schemas(core_only=False, include_restricted=True)}


def test_an_empty_whitelist_offers_the_model_nothing(registry, tmp_path):
    registry.set_context(ToolContext(agent_root=tmp_path, tool_whitelist=set()))
    offered = _names(registry)
    assert offered == set(), f"empty whitelist still offered {len(offered)} tools"
    assert not offered & RESTRICTED_TOOL_NAMES


def test_an_empty_whitelist_refuses_execution(registry, tmp_path):
    registry.set_context(ToolContext(agent_root=tmp_path, tool_whitelist=set()))
    answer = registry.execute("list_dir", {"path": "."})
    assert "not in the allowed tools list" in answer


def test_no_whitelist_at_all_still_means_no_gate(registry, tmp_path):
    """`None` is the "no firewall answered" case and must not become a denial."""
    registry.set_context(ToolContext(agent_root=tmp_path, tool_whitelist=None))
    assert len(_names(registry)) == len(registry._entries)


def test_a_named_whitelist_admits_exactly_what_it_names(registry, tmp_path):
    registry.set_context(ToolContext(agent_root=tmp_path, tool_whitelist={"list_dir"}))
    assert _names(registry) == {"list_dir"}
    assert "not in the allowed tools list" in registry.execute("run_shell", {"command": "echo hi"})
