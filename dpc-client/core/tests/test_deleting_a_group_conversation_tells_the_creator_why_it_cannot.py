"""Deleting a group's conversation tells the creator why it cannot.

Johnny's review of ADR-038 part 1 (2026-11-13): `delete_conversation` for a
`group-` id is `leave_group`, which now refuses the creator, so the creator was
told "Group not found or could not be deleted" for a group it holds. The
behaviour is unchanged (the creator still keeps the group); only the answer
says why, and points at deleting the group instead.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.managers.group_manager import GroupManager, GroupMetadata

SELF = "dpc-node-" + "a" * 32
OTHER = "dpc-node-" + "b" * 32


def _service(tmp_path):
    ks = KnowledgeService.__new__(KnowledgeService)
    ks.group_manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    ks.conversation_monitors = {}
    ks.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    return ks


@pytest.mark.asyncio
async def test_the_creator_is_told_it_cannot_leave_and_keeps_the_group(tmp_path):
    ks = _service(tmp_path)
    group = ks.group_manager.create_group("mine", "", [OTHER])

    result = await ks.delete_conversation(group.group_id)

    assert result["status"] == "error"
    assert "creator cannot leave" in result["message"] and "delete the group" in result["message"]
    assert "not found" not in result["message"]
    assert ks.group_manager.get_group(group.group_id) is not None
    ks.local_api.broadcast_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_member_still_leaves_by_deleting_the_conversation(tmp_path):
    ks = _service(tmp_path)
    gid = "group-0123456789ab"
    ks.group_manager.apply_sync(GroupMetadata(
        group_id=gid, name="theirs", created_by=OTHER, members=[OTHER, SELF]).to_dict(), OTHER)

    result = await ks.delete_conversation(gid)

    assert result["status"] == "success"
    assert ks.group_manager.get_group(gid) is None
    ks.local_api.broadcast_event.assert_awaited_once_with(
        "conversation_deleted", {"conversation_id": gid})


@pytest.mark.asyncio
async def test_a_group_not_held_is_reported_as_not_found(tmp_path):
    result = await _service(tmp_path).delete_conversation("group-ffffffffffff")

    assert result == {"status": "error", "message": "Group group-ffffffffffff not found"}
