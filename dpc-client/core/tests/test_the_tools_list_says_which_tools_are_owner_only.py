"""Music tools follow-up 12, item (a): `CoreService.list_all_tools`, what the Agent Permissions
panel renders, carries each tool's `owner_only` (ToolEntry.owner_only, ADR-051 D43), so the
panel's "owner-only" badge reads the registry instead of a list kept in the UI.

Live path: the real `CoreService.list_all_tools` over a fresh `ToolRegistry` with every module.
"""

from __future__ import annotations

import pytest

from dpc_client_core.dpc_agent.tools.registry import ToolRegistry
from dpc_client_core.service import CoreService


@pytest.mark.asyncio
async def test_each_listed_tool_says_whether_it_is_owner_only_as_the_registry_does(tmp_path):
    answer = await CoreService.list_all_tools(object.__new__(CoreService))
    assert answer["status"] == "success", answer
    listed = {t["name"]: t for t in answer["tools"]}
    assert all(isinstance(t.get("owner_only"), bool) for t in listed.values())
    registry = ToolRegistry(agent_root=tmp_path / "agent")
    assert {n for n, t in listed.items() if t["owner_only"]} == registry.owner_only_names() & set(listed)
    # ADR-053: the badge reads owner_approval_for_others now; no tool is owner_only.
    assert all(isinstance(t.get("owner_approval_for_others"), bool) for t in listed.values())
    flagged = registry.owner_approval_names()
    assert {n for n, t in listed.items() if t["owner_approval_for_others"]} == flagged & set(listed)
    for name in ("music_propose_job", "chat_post_file"):
        assert listed[name]["owner_approval_for_others"] is True and listed[name]["owner_only"] is False
    assert listed["read_file"]["owner_approval_for_others"] is False
