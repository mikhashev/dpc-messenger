"""Every group event names the version the backend holds after it.

The UI drops an event older than the entry it holds (`mergeGroupEvent`); an
event without a version slipped past that check, and a topic change, a room's
effort and a member leaving were the three that carried none. An accepted
GROUP_SYNC about a group the UI does not hold yet now names its creator, so
the entry it adds is a whole one.
"""

import pytest

from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.group_handler import GroupCreateHandler, GroupSyncHandler
from dpc_client_core.service import CoreService
from tests.test_group_roster_and_history_gate import MEMBER, SELF
from tests.test_group_roster_and_history_gate import _Service


@pytest.mark.asyncio
async def test_a_topic_change_carries_the_version_it_raised(tmp_path):
    service = _Service(GroupManager(dpc_home=tmp_path, node_id=SELF))
    service.p2p_manager.node_id = SELF

    async def _no_peers(group_id, message):
        return None

    service._broadcast_to_group = _no_peers
    gid = service.group_manager.create_group("Room", "", []).group_id
    before = service.group_manager.get_group(gid).version

    await CoreService.update_group_topic(service, gid, "new topic")

    [(name, payload)] = service.local_api.events
    assert name == "group_updated"
    assert payload["version"] == service.group_manager.get_group(gid).version > before


@pytest.mark.asyncio
async def test_an_accepted_sync_names_the_creator_and_the_version(tmp_path):
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    service = _Service(manager)
    gid = "group-0123456789ab"
    record = {
        "group_id": gid, "name": "work", "topic": "", "created_by": MEMBER,
        "members": [MEMBER, SELF], "agents": {}, "agent_names": {}, "version": 22,
    }
    await GroupCreateHandler(service).handle(MEMBER, record)
    service.local_api.events.clear()

    await GroupSyncHandler(service).handle(MEMBER, {**record, "topic": "later", "version": 23})

    updates = [p for name, p in service.local_api.events if name == "group_updated"]
    assert updates, "the sync was not applied, so this test proves nothing"
    assert updates[-1]["created_by"] == MEMBER
    assert updates[-1]["version"] == 23
    # The room's effort is this node's own knob; a peer's sync never carries it to the UI.
    assert "reasoning_effort" not in updates[-1]


@pytest.mark.asyncio
async def test_a_sync_for_a_group_deleted_here_raises_no_event(tmp_path):
    """The deleted-groups registry guards GROUP_CREATE; a late GROUP_SYNC is turned
    away earlier, because the deleted group has no local copy to sync into."""
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    service = _Service(manager)
    gid = "group-0123456789ab"
    record = {
        "group_id": gid, "name": "work", "topic": "", "created_by": MEMBER,
        "members": [MEMBER, SELF], "agents": {}, "agent_names": {}, "version": 22,
    }
    await GroupCreateHandler(service).handle(MEMBER, record)
    assert manager.delete_group(gid, MEMBER)
    service.local_api.events.clear()

    await GroupSyncHandler(service).handle(MEMBER, {**record, "version": 30})

    assert manager.get_group(gid) is None
    assert service.local_api.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["add_member", "remove_member", "set_agents"])
async def test_each_roster_command_announces_the_version_it_left(tmp_path, command):
    service = _Service(GroupManager(dpc_home=tmp_path, node_id=SELF))
    service.firewall = None
    service.agent_service = None

    async def _no_peers(group_id, message):
        return None

    service._broadcast_to_group = _no_peers
    gid = service.group_manager.create_group("Room", "", [MEMBER]).group_id

    if command == "add_member":
        result = await CoreService.add_group_member(service, gid, "dpc-node-" + "a" * 32)
    elif command == "remove_member":
        result = await CoreService.remove_group_member(service, gid, MEMBER)
    else:
        result = await CoreService.set_group_agents(service, gid, ["ext:CC"])

    assert result["status"] == "success", result
    [payload] = [p for name, p in service.local_api.events if name == "group_updated"]
    assert payload["version"] == service.group_manager.get_group(gid).version
