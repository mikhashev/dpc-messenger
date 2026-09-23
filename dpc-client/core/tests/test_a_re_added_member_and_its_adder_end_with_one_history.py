"""A re-added member and the node that added it end with one group history.

The re-add used to push the adder's whole history as a CHAT_HISTORY_RESPONSE
with no request id. The receiver discarded it as unsolicited, and had it landed
it would have replaced the member's history rather than merged with it. No
status exchange followed either: GROUP_SYNC had applied the new version a moment
earlier, so the GROUP_CREATE applied nothing and its handler stayed silent
(ONE-RECONNECT-MUST-LEAVE-BOTH-NODES-WITH-ONE-GROUP-HISTORY-..., steps 1-3).

Two nodes run the real handlers and the real `add_group_member` against their
own home directories; `Path.home` follows whichever node is handling a message.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.chat_history_handlers import (
    ChatHistoryResponseHandler,
    HistoryRequestRegistry,
)
from dpc_client_core.message_handlers.group_handler import (
    GroupCreateHandler,
    GroupHistoryRequestHandler,
    GroupHistoryResponseHandler,
    GroupHistoryStatusHandler,
    GroupSyncHandler,
)
from dpc_client_core.service import CoreService

A_ID = "dpc-node-" + "a" * 32
B_ID = "dpc-node-" + "b" * 32
C_ID = "dpc-node-" + "c" * 32


def _rec(rid: str, author: str, day: int) -> dict:
    return {"id": rid, "role": "user", "content": f"text {rid}",
            "timestamp": f"2026-09-{day:02d}T12:00:00+00:00",
            "sender_node_id": author, "sender_name": author[-4:]}


class _Api:
    async def broadcast_event(self, name, payload):
        pass


class _P2P:
    def __init__(self, net, node_id):
        self.net = net
        self.node_id = node_id
        self.peers = {}

    async def send_message_to_peer(self, node_id, message):
        self.net.queue.append((node_id, self.node_id, message))
        self.net.sent.append((self.node_id, node_id, message))


class _Service:
    """The attributes the real factory, the handlers and add/remove read."""

    add_group_member = CoreService.add_group_member
    remove_group_member = CoreService.remove_group_member
    _broadcast_to_group = CoreService._broadcast_to_group

    def __init__(self, net, node_id, group_manager):
        self.group_manager = group_manager
        self.local_api = _Api()
        self.p2p_manager = _P2P(net, node_id)
        self.conversation_monitors = {}
        self.history_requests = HistoryRequestRegistry()
        self.knowledge_service = None
        self.llm_manager = None
        self.settings = None
        self.peer_metadata = {}
        self.instruction_set = SimpleNamespace(default="general")
        self._send_ai_query = None
        self._build_participants = lambda cid: [
            {"node_id": m, "context": "local" if m == node_id else "peer"}
            for m in group_manager.get_group(cid).members
        ]
        self._announce_history_after_repair = lambda group_id: None

    def _get_or_create_conversation_monitor(self, conversation_id, instruction_set_name=None):
        return KnowledgeService._get_or_create_conversation_monitor(
            self, conversation_id, instruction_set_name
        )

    def clear_group_access_denied(self, peer_id, group_id):
        pass

    def note_group_access_denied(self, peer_id, group_id):
        pass


class _Node:
    def __init__(self, net, node_id, home, group_manager):
        self.node_id = node_id
        self.home = home
        self.service = _Service(net, node_id, group_manager)
        self.handlers = {
            h.command_name: h for h in (
                GroupCreateHandler(self.service),
                GroupSyncHandler(self.service),
                GroupHistoryStatusHandler(self.service),
                GroupHistoryRequestHandler(self.service),
                GroupHistoryResponseHandler(self.service),
                ChatHistoryResponseHandler(self.service),
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

    def monitor(self, node, gid):
        self.on(node)
        return node.service._get_or_create_conversation_monitor(gid)

    async def pump(self, limit=40):
        delivered = 0
        while self.queue:
            to, frm, message = self.queue.pop(0)
            node = self.nodes.get(to)
            if node is None:
                continue
            self.on(node)
            await node.handlers[message["command"]].handle(frm, message["payload"])
            delivered += 1
            assert delivered <= limit, "the exchange does not stop"

    def sent_by(self, frm, to=None):
        return [m for f, t, m in self.sent if f == frm and (to is None or t == to)]


@pytest.fixture
def pair(tmp_path, monkeypatch):
    """A created the group with B and C; B kept its copy at the first version."""
    net = _Net(monkeypatch)
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()

    manager_a = GroupManager(dpc_home=home_a / ".dpc", node_id=A_ID)
    a = _Node(net, A_ID, home_a, manager_a)
    net.on(a)
    group = manager_a.create_group("work", "topic", [B_ID, C_ID])

    manager_b = GroupManager(dpc_home=home_b / ".dpc", node_id=B_ID)
    b = _Node(net, B_ID, home_b, manager_b)
    net.on(b)
    manager_b.apply_sync(group.to_dict())

    net.nodes = {A_ID: a, B_ID: b}
    return net, a, b, group.group_id


def _seed(net, node, gid, records):
    monitor = net.monitor(node, gid)
    monitor.message_history = [dict(r) for r in records]
    monitor.rebuild_message_ids()
    monitor._history_dirty = True
    monitor.save_history()
    return monitor


def _ids(net, node, gid):
    return sorted(m["id"] for m in net.monitor(node, gid).message_history)


async def _remove_offline_then_re_add(net, a, b, gid):
    """B is removed while offline, then connects and is added back."""
    net.on(a)
    await a.service.remove_group_member(gid, B_ID)
    net.sent.clear()
    a.service.p2p_manager.peers = {B_ID: object()}
    b.service.p2p_manager.peers = {A_ID: object()}
    net.on(a)
    result = await a.service.add_group_member(gid, B_ID)
    assert result["status"] == "success"


SHARED = [_rec("s1", A_ID, 1), _rec("s2", B_ID, 2)]
ONLY_A = [_rec("a3", A_ID, 3), _rec("c4", C_ID, 4)]
ONLY_B = [_rec("b5", B_ID, 5), _rec("b6", B_ID, 6), _rec("b7", B_ID, 7)]


@pytest.mark.asyncio
async def test_after_a_re_add_both_nodes_hold_the_union_and_nothing_is_replaced(pair):
    net, a, b, gid = pair
    _seed(net, a, gid, SHARED + ONLY_A)
    _seed(net, b, gid, SHARED + ONLY_B)

    await _remove_offline_then_re_add(net, a, b, gid)
    await net.pump()

    union = sorted(r["id"] for r in SHARED + ONLY_A + ONLY_B)
    assert _ids(net, a, gid) == union
    assert _ids(net, b, gid) == union
    # Both sides opened the exchange, and each still asked only once.
    requests = [(f, t) for f, t, m in net.sent if m["command"] == "GROUP_HISTORY_REQUEST"]
    assert sorted(requests) == [(A_ID, B_ID), (B_ID, A_ID)]


@pytest.mark.asyncio
async def test_a_re_add_opens_the_status_exchange_and_pushes_no_history(pair):
    net, a, b, gid = pair
    _seed(net, a, gid, SHARED + ONLY_A)

    await _remove_offline_then_re_add(net, a, b, gid)

    to_b = [m["command"] for m in net.sent_by(A_ID, B_ID)]
    assert "GROUP_CREATE" in to_b
    assert "GROUP_HISTORY_STATUS" in to_b[to_b.index("GROUP_CREATE"):]
    assert "CHAT_HISTORY_RESPONSE" not in to_b
    status = next(m for m in net.sent_by(A_ID, B_ID) if m["command"] == "GROUP_HISTORY_STATUS")
    assert status["payload"]["group_id"] == gid
    assert status["payload"]["message_count"] == 4
    # Added back: the removal it missed is no longer owed.
    assert a.service.group_manager.removals_owed_to(B_ID) == []


@pytest.mark.asyncio
async def test_a_group_create_for_a_version_already_applied_still_opens_the_exchange(pair):
    net, a, b, gid = pair
    _seed(net, b, gid, ONLY_B)
    current = a.service.group_manager.get_group(gid).to_dict()
    net.on(b)
    assert b.service.group_manager.apply_sync(current) is None, "already at this version"

    await b.handlers["GROUP_CREATE"].handle(A_ID, current)

    to_a = [m["command"] for m in net.sent_by(B_ID, A_ID)]
    assert to_a == ["GROUP_HISTORY_STATUS"]


@pytest.mark.asyncio
async def test_a_member_other_than_the_adder_cannot_overwrite_our_history_by_push(pair):
    net, a, b, gid = pair
    _seed(net, b, gid, SHARED + ONLY_B)
    await _remove_offline_then_re_add(net, a, b, gid)
    await net.pump()
    before = _ids(net, b, gid)

    net.on(b)
    forged = [_rec("x1", C_ID, 9)]
    handler = b.handlers["CHAT_HISTORY_RESPONSE"]
    for request_id in (None, "guessed"):
        await handler.handle(C_ID, {"conversation_id": gid, "request_id": request_id,
                                    "messages": forged})
    assert _ids(net, b, gid) == before

    # Even an answer we did ask for adds to the group history, never replaces it.
    b.service.history_requests.note(C_ID, gid, "asked")
    await handler.handle(C_ID, {"conversation_id": gid, "request_id": "asked",
                                "messages": forged})
    assert _ids(net, b, gid) == sorted(before + ["x1"])
