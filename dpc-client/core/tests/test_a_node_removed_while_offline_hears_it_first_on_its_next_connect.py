"""A node removed while offline hears it first on its next connect, even after a restart.

`remove_group_member` used to log "it will find out by refusal" and queue
nothing. The returning node then spent its one connect-time exchange on two
refusals, while this node's own loop stayed silent for the group
(REMOVING-AN-OFFLINE-MEMBER-QUEUES-NOTHING-SO-ITS-FIRST-RECONNECT-SPENDS-THE-
ONE-SYNC-CHANCE-ON-REFUSAL). The removal is now kept on disk and the roster that
leaves the node out is the first thing it is sent when it connects.

The remover runs the real `on_peer_list_change`; the removed node runs the real
sync and refusal handlers against its own home directory.
"""

from pathlib import Path

import pytest

from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.chat_history_handlers import HistoryRequestRegistry
from dpc_client_core.message_handlers.group_handler import (
    GroupAccessDeniedHandler,
    GroupHistoryStatusHandler,
    GroupSyncHandler,
)
from dpc_client_core.service import CoreService

A_ID = "dpc-node-" + "a" * 32
B_ID = "dpc-node-" + "b" * 32
C_ID = "dpc-node-" + "c" * 32


class _Api:
    async def broadcast_event(self, name, payload):
        pass


class _Cache:
    def add_or_update_peer(self, **kwargs):
        pass


class _P2P:
    def __init__(self, net, node_id):
        self.net = net
        self.node_id = node_id
        self.peers = {}
        self.peer_cache = _Cache()

    async def send_message_to_peer(self, node_id, message):
        self.net.queue.append((node_id, self.node_id, message))
        self.net.sent.append((self.node_id, node_id, message))


class _Service:
    """What `on_peer_list_change`, remove and the group handlers read."""

    on_peer_list_change = CoreService.on_peer_list_change
    _deliver_removals_owed = CoreService._deliver_removals_owed
    remove_group_member = CoreService.remove_group_member
    _broadcast_to_group = CoreService._broadcast_to_group
    note_group_access_denied = CoreService.note_group_access_denied
    clear_group_access_denied = CoreService.clear_group_access_denied

    def __init__(self, net, node_id, group_manager):
        self.group_manager = group_manager
        self.local_api = _Api()
        self.p2p_manager = _P2P(net, node_id)
        self.conversation_monitors = {}
        self.history_requests = HistoryRequestRegistry()
        self.knowledge_service = None
        self.peer_metadata = {}
        self._history_requested_peers = set()
        self._group_access_denied = set()

    def _settle_reconnects_for_connected_peers(self):
        pass

    async def get_status(self):
        return {}

    def _get_or_create_conversation_monitor(self, conversation_id, instruction_set_name=None):
        return None  # the 1:1 half of the connect loop is not under test


class _Node:
    def __init__(self, net, node_id, home, group_manager):
        self.node_id = node_id
        self.home = home
        self.service = _Service(net, node_id, group_manager)
        self.handlers = {
            h.command_name: h for h in (
                GroupSyncHandler(self.service),
                GroupHistoryStatusHandler(self.service),
                GroupAccessDeniedHandler(self.service),
            )
        }


class _Net:
    def __init__(self, monkeypatch):
        self.queue = []
        self.sent = []
        self.nodes = {}
        self.current = None
        monkeypatch.setattr(Path, "home", lambda: self.current.home)

    def on(self, node):
        self.current = node
        return node

    async def connect(self, node):
        self.on(node)
        await node.service.on_peer_list_change()

    async def pump(self, limit=20):
        delivered = 0
        while self.queue:
            to, frm, message = self.queue.pop(0)
            node = self.on(self.nodes[to])
            await node.handlers[message["command"]].handle(frm, message["payload"])
            delivered += 1
            assert delivered <= limit, "the exchange does not stop"


def _manager(home, node_id):
    manager = GroupManager(dpc_home=home / ".dpc", node_id=node_id)
    manager.load_from_disk()
    return manager


@pytest.fixture
def removed_offline(tmp_path, monkeypatch):
    """A removes B while B is offline, then A restarts."""
    net = _Net(monkeypatch)
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()

    a = _Node(net, A_ID, home_a, _manager(home_a, A_ID))
    net.on(a)
    group = a.service.group_manager.create_group("work", "topic", [B_ID, C_ID])
    b = _Node(net, B_ID, home_b, _manager(home_b, B_ID))
    net.on(b)
    b.service.group_manager.apply_sync(group.to_dict())

    net.nodes = {A_ID: a, B_ID: b}
    return net, a, b, group.group_id, home_a


@pytest.mark.asyncio
async def test_a_removal_made_offline_survives_a_restart_and_goes_out_first(removed_offline):
    net, a, b, gid, home_a = removed_offline
    net.on(a)
    await a.service.remove_group_member(gid, B_ID)
    assert net.sent == [], "B was offline; nothing could be sent"

    # Restart: a new manager and service over the same home.
    net.on(a)
    a = _Node(net, A_ID, home_a, _manager(home_a, A_ID))
    assert a.service.group_manager.removals_owed_to(B_ID) == [gid]
    net.nodes = {A_ID: a, B_ID: b}

    a.service.p2p_manager.peers = {B_ID: object()}
    b.service.p2p_manager.peers = {A_ID: object()}
    await net.connect(a)

    to_b = [m for f, t, m in net.sent if f == A_ID and t == B_ID]
    assert to_b, "the removed node is told something on connect"
    first = to_b[0]
    assert first["command"] == "GROUP_SYNC"
    assert first["payload"]["group_id"] == gid
    assert B_ID not in first["payload"]["members"]

    await net.pump()
    assert B_ID not in b.service.group_manager.get_group(gid).members
    assert b.service.group_manager.get_groups_for_peer(A_ID) == []
    assert a.service.group_manager.removals_owed_to(B_ID) == []

    # B's own connect loop now has nothing to ask about, so nothing is refused.
    net.sent.clear()
    await net.connect(b)
    await net.pump()
    assert not [m for _, _, m in net.sent if m["command"] == "GROUP_ACCESS_DENIED"]

    # And the news is delivered once, not on every connect.
    net.sent.clear()
    await net.connect(a)
    assert not [m for f, t, m in net.sent if t == B_ID and m["command"] == "GROUP_SYNC"]


@pytest.mark.asyncio
async def test_a_member_added_back_before_it_connects_is_owed_nothing(removed_offline):
    net, a, b, gid, home_a = removed_offline
    net.on(a)
    await a.service.remove_group_member(gid, B_ID)
    a.service.group_manager.add_member(gid, B_ID)

    reloaded = _manager(home_a, A_ID)
    assert reloaded.removals_owed_to(B_ID) == []
