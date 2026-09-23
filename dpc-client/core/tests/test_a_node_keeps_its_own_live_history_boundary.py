"""A node keeps its own live-history boundary, and two nodes compare inside the later one.

ADR-038 amendment 2026-09-23 and ADR-037 amendment of the same day. A node that
reset alone used to get every dropped record merged back at the next reconnect
("Merged 139 new messages"). The boundary keeps older arrivals out of the live
history, and the status exchange narrows the comparison to the pair's window so
the records kept out are not asked for again on every connect.

Two nodes run the real history handlers against their own home directories;
`Path.home` follows whichever node is handling the message.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.conversation_monitor import (
    digest_for,
    history_status_for,
    live_history_boundary_of,
)
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.chat_history_handlers import HistoryRequestRegistry
from dpc_client_core.message_handlers.group_handler import (
    GroupHistoryRequestHandler,
    GroupHistoryResponseHandler,
    GroupHistoryStatusHandler,
)

A_ID = "dpc-node-" + "a" * 32
B_ID = "dpc-node-" + "b" * 32


def _t(day: int) -> str:
    return f"2026-09-{day:02d}T12:00:00+00:00"


def _rec(rid: str, author: str, day: int) -> dict:
    return {"id": rid, "role": "user", "content": f"text {rid}",
            "timestamp": _t(day), "sender_node_id": author, "sender_name": author[-4:]}


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
    """The attributes the real monitor factory and the history handlers read."""

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


class _Node:
    def __init__(self, net, node_id, home, group_manager):
        self.node_id = node_id
        self.home = home
        self.service = _Service(net, node_id, group_manager)
        self.handlers = {
            h.command_name: h for h in (
                GroupHistoryStatusHandler(self.service),
                GroupHistoryRequestHandler(self.service),
                GroupHistoryResponseHandler(self.service),
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

    async def connect(self, initiator, other, gid, limit=20):
        """What `on_peer_list_change` sends, then every answer until quiet."""
        self.on(initiator)
        status = history_status_for(gid, initiator.service.conversation_monitors.get(gid))
        await initiator.service.p2p_manager.send_message_to_peer(other.node_id, {
            "command": "GROUP_HISTORY_STATUS", "payload": status,
        })
        delivered = 0
        while self.queue:
            to, frm, message = self.queue.pop(0)
            node = self.on(self.nodes[to])
            await node.handlers[message["command"]].handle(frm, message["payload"])
            delivered += 1
            assert delivered <= limit, "the exchange does not stop"

    def commands(self, name):
        return [m for _, _, m in self.sent if m["command"] == name]


@pytest.fixture
def pair(tmp_path, monkeypatch):
    net = _Net(monkeypatch)
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()

    manager_a = GroupManager(dpc_home=home_a / ".dpc", node_id=A_ID)
    a = _Node(net, A_ID, home_a, manager_a)
    net.on(a)
    group = manager_a.create_group("work", "topic", [B_ID])

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


def _live_ids(net, node, gid):
    return sorted(m["id"] for m in net.monitor(node, gid).message_history)


def _archived_ids(net, node, gid):
    monitor = net.monitor(node, gid)
    ids = []
    for path in (monitor._get_history_path().parent / "archive").rglob("*_session.json"):
        ids += [m.get("id") for m in json.loads(path.read_text(encoding="utf-8"))["messages"]]
    return sorted(ids)


def _archive_files(net, node, gid):
    monitor = net.monitor(node, gid)
    return list((monitor._get_history_path().parent / "archive").rglob("*_session.json"))


OLD = [_rec("r1", A_ID, 1), _rec("r2", B_ID, 2), _rec("r3", A_ID, 3)]


@pytest.mark.asyncio
async def test_a_node_that_resets_alone_keeps_the_old_records_in_its_archive(pair):
    net, a, b, gid = pair
    _seed(net, a, gid, OLD)
    _seed(net, b, gid, OLD)

    net.monitor(a, gid).reset_conversation()
    await net.connect(a, b, gid)
    await net.connect(b, a, gid)

    assert _live_ids(net, a, gid) == []
    assert _archived_ids(net, a, gid) == ["r1", "r2", "r3"]
    assert _live_ids(net, b, gid) == ["r1", "r2", "r3"]
    assert net.commands("GROUP_HISTORY_REQUEST") == []

    # And the records B still ships, whatever the path, are archived, not merged.
    net.on(b)
    everything = net.monitor(b, gid).export_history()
    monitor_a = net.monitor(a, gid)
    assert monitor_a.merge_history(everything) == 0
    assert monitor_a.message_history == []
    assert _archived_ids(net, a, gid) == ["r1", "r2", "r3"]


@pytest.mark.asyncio
async def test_two_boundaries_reconcile_and_a_second_connect_asks_for_nothing(pair):
    net, a, b, gid = pair
    shared = [_rec(f"s{d}", A_ID if d % 2 else B_ID, d) for d in range(1, 7)]
    # A lacks s6; each holds one record the other has not seen.
    _seed(net, a, gid, shared[:5] + [_rec("a7", A_ID, 7)])
    _seed(net, b, gid, shared + [_rec("b8", B_ID, 8)])
    net.monitor(a, gid).clear_before(_t(3))
    net.monitor(b, gid).clear_before(_t(5))

    await net.connect(a, b, gid)

    assert net.commands("GROUP_HISTORY_REQUEST"), "the first connect has to reconcile"
    for request in net.commands("GROUP_HISTORY_REQUEST"):
        assert request["payload"]["since"] == _t(5)
    assert _live_ids(net, a, gid) == ["a7", "b8", "s3", "s4", "s5", "s6"]
    assert _live_ids(net, b, gid) == ["a7", "b8", "s5", "s6"]

    # A second connect, with both monitors unloaded so the disk path answers.
    net.sent.clear()
    a.service.conversation_monitors.clear()
    b.service.conversation_monitors.clear()
    await net.connect(a, b, gid)

    assert net.commands("GROUP_HISTORY_REQUEST") == []
    assert len(net.commands("GROUP_HISTORY_STATUS")) <= 3


@pytest.mark.asyncio
async def test_a_second_connect_from_the_other_side_asks_for_nothing_either(pair):
    net, a, b, gid = pair
    shared = [_rec(f"s{d}", A_ID if d % 2 else B_ID, d) for d in range(1, 7)]
    _seed(net, a, gid, shared)
    _seed(net, b, gid, shared + [_rec("b8", B_ID, 8)])
    net.monitor(a, gid).clear_before(_t(3))
    net.monitor(b, gid).clear_before(_t(5))

    await net.connect(b, a, gid)
    net.sent.clear()
    await net.connect(b, a, gid)

    assert net.commands("GROUP_HISTORY_REQUEST") == []


def test_a_peers_boundary_does_not_change_our_own_digest(pair):
    net, a, b, gid = pair
    records = OLD + [_rec("r9", B_ID, 9)]
    _seed(net, a, gid, records)
    monitor_b = _seed(net, b, gid, records)
    net.monitor(a, gid).clear_before(_t(5))

    net.on(b)
    status = history_status_for(gid, monitor_b)
    assert status["history_digest"] == digest_for(monitor_b.message_history, B_ID)
    assert status["live_history_boundary"] is None
    assert status["digest_since"] is None


@pytest.mark.asyncio
async def test_a_member_with_no_boundary_receives_the_whole_history(pair):
    net, a, b, gid = pair
    records = OLD + [_rec("r4", B_ID, 4), _rec("r5", A_ID, 5)]
    _seed(net, a, gid, records)

    await net.connect(b, a, gid)

    assert _live_ids(net, b, gid) == ["r1", "r2", "r3", "r4", "r5"]
    net.on(b)
    assert live_history_boundary_of(gid) is None
    assert _archive_files(net, b, gid) == []


@pytest.mark.asyncio
async def test_a_boundary_from_the_future_is_ignored(pair, caplog):
    net, a, b, gid = pair
    _seed(net, b, gid, OLD)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    with caplog.at_level(logging.WARNING):
        net.on(b)
        await b.handlers["GROUP_HISTORY_STATUS"].handle(A_ID, {
            "group_id": gid, "history_hash": "sha256:x", "message_count": 0,
            "history_digest": {"authors": {}, "digest": "sha256:empty"},
            "live_history_boundary": future, "digest_since": future,
        })

    assert "lies in the future" in caplog.text
    reply = net.commands("GROUP_HISTORY_STATUS")[0]["payload"]
    assert reply["digest_since"] is None
    assert reply["history_digest"] == digest_for(OLD, B_ID)


def test_the_boundary_only_moves_forward_and_sync_never_touches_it(pair):
    net, a, b, gid = pair
    monitor = _seed(net, a, gid, OLD)

    monitor.clear_before(_t(2))
    assert monitor.live_history_boundary == _t(2)
    monitor.clear_before(_t(1))
    assert monitor.live_history_boundary == _t(2), "it never moves back"

    monitor.reset_conversation()
    after_reset = monitor.live_history_boundary
    assert after_reset > _t(2)

    monitor.clear_history()
    assert monitor.live_history_boundary >= after_reset
    monitor.clear_before(_t(3))
    assert monitor.live_history_boundary >= after_reset

    manager = a.service.group_manager
    group = manager.get_group(gid).to_dict()
    group["version"] += 1
    group["session_started_at"] = _t(28)
    manager.apply_sync(group)
    assert monitor.live_history_boundary >= after_reset
    assert monitor.live_history_boundary != _t(28)
    metadata = manager._get_group_metadata_path(gid).read_text(encoding="utf-8")
    assert "live_history_boundary" not in metadata


def test_older_arrivals_are_archived_once(pair):
    net, a, b, gid = pair
    monitor = _seed(net, a, gid, [_rec("r9", A_ID, 9)])
    monitor.clear_before(_t(5))

    older = [_rec("r1", B_ID, 1), _rec("r2", B_ID, 2)]
    newer = _rec("r7", B_ID, 7)
    assert monitor.merge_history(older + [newer]) == 1
    assert monitor.last_merge_archived == 2
    assert monitor.merge_history(older) == 0
    assert monitor.last_merge_archived == 0

    assert len(_archive_files(net, a, gid)) == 1
    assert _archived_ids(net, a, gid) == ["r1", "r2"]
    assert _live_ids(net, a, gid) == ["r7", "r9"]


def test_a_record_without_a_timestamp_is_inside_every_window():
    undated = {"id": "u", "sender_node_id": A_ID, "content": "x"}
    dated = _rec("d", A_ID, 1)

    narrowed = digest_for([undated, dated], A_ID, since=_t(5))

    assert narrowed == digest_for([undated], A_ID)


def test_the_export_leaves_out_what_the_asker_would_only_archive(pair):
    net, a, b, gid = pair
    monitor = _seed(net, a, gid, OLD + [_rec("r7", A_ID, 7)])

    assert [m["id"] for m in monitor.export_history(since=_t(3))] == ["r3", "r7"]
    assert len(monitor.export_history()) == 4


def test_the_replacing_import_obeys_the_boundary_too(pair):
    """REQUEST_CHAT_HISTORY answers replace the history; they must not undo a reset."""
    net, a, b, gid = pair
    monitor = _seed(net, a, gid, OLD)
    monitor.reset_conversation()

    monitor.import_history([dict(r) for r in OLD])

    assert monitor.message_history == []
    assert _archived_ids(net, a, gid) == ["r1", "r2", "r3"]
