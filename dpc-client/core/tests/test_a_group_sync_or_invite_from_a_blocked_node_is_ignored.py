"""A GROUP_SYNC or GROUP_CREATE sent by a blocked node is ignored; a third member's roster is not.

Membership is this node's copy only, and any remaining member can put a
removed node back with a higher version (`add_group_member` has no creator
check). So the gates read the block list, never the roster: a roster or an
invitation the blocked node itself sends is ignored even while our copy still
lists it, and a GROUP_SYNC from a third member whose roster includes the
blocked node is applied as that group's state, with one INFO line — the block
still holds at every door.

Real `GroupSyncHandler`, `GroupCreateHandler` and `GroupManager` in tmp_path.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.group_handler import GroupCreateHandler, GroupSyncHandler

SELF = "dpc-node-" + "a" * 32
BLOCKED = "dpc-node-" + "b" * 32
THIRD = "dpc-node-" + "c" * 32
SYNC_LOGGER = "dpc_client_core.message_handlers.GroupSyncHandler"
CREATE_LOGGER = "dpc_client_core.message_handlers.GroupCreateHandler"


class _Firewall:
    def is_blocked(self, node_id):
        return node_id == BLOCKED


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    manager = GroupManager(tmp_path / "dpc", SELF)
    service = SimpleNamespace(
        firewall=_Firewall(),
        group_manager=manager,
        p2p_manager=SimpleNamespace(node_id=SELF, peers={}, send_message_to_peer=AsyncMock()),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        peer_metadata={},
        conversation_monitors={},
        clear_group_access_denied=lambda peer, gid: None,
    )
    # Our copy still lists BLOCKED: the gate must not lean on the roster.
    group = manager.create_group("g", "", [BLOCKED, THIRD])
    return SimpleNamespace(service=service, group=group)


def _bumped(group, **changes):
    payload = group.to_dict()
    payload.update(changes, version=group.version + 1)
    return payload


def _events(service, name):
    return [c.args[1] for c in service.local_api.broadcast_event.await_args_list if c.args[0] == name]


@pytest.mark.asyncio
async def test_a_sync_sent_by_a_blocked_node_is_ignored(node, caplog):
    caplog.set_level(logging.INFO, logger=SYNC_LOGGER)
    before = node.group.version

    await GroupSyncHandler(node.service).handle(BLOCKED, _bumped(node.group, topic="mine now"))

    kept = node.service.group_manager.get_group(node.group.group_id)
    assert kept.version == before and kept.topic == ""
    assert _events(node.service, "group_updated") == []
    assert any("Ignoring GROUP_SYNC" in r.getMessage() and BLOCKED in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_a_sync_sent_by_a_third_member_is_applied(node):
    await GroupSyncHandler(node.service).handle(THIRD, _bumped(node.group, topic="new topic"))

    assert node.service.group_manager.get_group(node.group.group_id).topic == "new topic"


@pytest.mark.asyncio
async def test_a_third_members_roster_that_lists_the_blocked_node_is_applied_and_logged(node, caplog):
    caplog.set_level(logging.INFO, logger=SYNC_LOGGER)

    await GroupSyncHandler(node.service).handle(
        THIRD, _bumped(node.group, members=[SELF, THIRD, BLOCKED]))

    assert BLOCKED in node.service.group_manager.get_group(node.group.group_id).members
    lines = [r.getMessage() for r in caplog.records if "lists blocked node" in r.getMessage()]
    assert len(lines) == 1 and BLOCKED in lines[0]


@pytest.mark.asyncio
async def test_an_invitation_from_a_blocked_node_is_ignored(node, caplog):
    caplog.set_level(logging.INFO, logger=CREATE_LOGGER)
    invite = dict(node.group.to_dict(), group_id="group-from-blocked", created_by=BLOCKED)

    await GroupCreateHandler(node.service).handle(BLOCKED, invite)

    assert node.service.group_manager.get_group("group-from-blocked") is None
    assert _events(node.service, "group_invite_received") == []
    node.service.p2p_manager.send_message_to_peer.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_invitation_from_an_unblocked_node_is_taken(node):
    invite = dict(node.group.to_dict(), group_id="group-from-third", created_by=THIRD)

    await GroupCreateHandler(node.service).handle(THIRD, invite)

    assert node.service.group_manager.get_group("group-from-third") is not None
    assert len(_events(node.service, "group_invite_received")) == 1
