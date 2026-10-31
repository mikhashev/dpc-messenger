"""This node does not add a blocked node to a group: it says to unblock first.

Block removes the node from every group (Mike's call, 2026-10-31); adding it
back from here, by `add_group_member` or in a new group's first roster, is
refused with a message that names the way out, and the roster and the network
are left untouched. An unblocked node is added as before.

Real `CoreService` methods and `GroupManager` in tmp_path.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.service import CoreService

SELF = "dpc-node-" + "a" * 32
BLOCKED = "dpc-node-" + "b" * 32
FRIEND = "dpc-node-" + "c" * 32


class _Firewall:
    def is_blocked(self, node_id):
        return node_id == BLOCKED


class _P2P:
    def __init__(self):
        self.node_id = SELF
        self.peers = {}
        self.sent = []

    async def send_message_to_peer(self, node_id, message):
        self.sent.append((node_id, message))


class _Api:
    async def broadcast_event(self, name, payload):
        pass


@pytest.fixture
def service(tmp_path):
    s = CoreService.__new__(CoreService)
    s.firewall = _Firewall()
    s.group_manager = GroupManager(tmp_path / "dpc", SELF)
    s.p2p_manager = _P2P()
    s.local_api = _Api()
    return s


@pytest.mark.asyncio
async def test_adding_a_blocked_node_is_refused_and_changes_nothing(service):
    group = service.group_manager.create_group("g", "", [FRIEND])
    version = group.version

    result = await service.add_group_member(group.group_id, BLOCKED)

    assert result["status"] == "error" and "Unblock it first" in result["message"]
    kept = service.group_manager.get_group(group.group_id)
    assert BLOCKED not in kept.members and kept.version == version
    assert service.p2p_manager.sent == []


@pytest.mark.asyncio
async def test_adding_an_unblocked_node_works_as_before(service):
    group = service.group_manager.create_group("g", "", [FRIEND])
    other = "dpc-node-" + "d" * 32

    result = await service.add_group_member(group.group_id, other)

    assert result["status"] == "success"
    assert other in service.group_manager.get_group(group.group_id).members


@pytest.mark.asyncio
async def test_a_new_group_with_a_blocked_node_in_its_roster_is_not_created(service):
    result = await service.create_group_chat("g", "", [FRIEND, BLOCKED])

    assert result["status"] == "error" and "Unblock it first" in result["message"]
    assert service.group_manager.get_all_groups() == []

    ok = await service.create_group_chat("g", "", [FRIEND])
    assert ok["status"] == "success"
