"""A proven session marker trims history even when the group's monitor is unloaded.

Monitors are created lazily, and `_honour_session_marker` used to look one up
and return when it found none — after `apply_sync` had already stored the newer
`session_started_at`. Nothing else trims by that field, so the records before
the boundary stayed for good (A-SESSION-MARKER-THAT-ARRIVES-WHILE-THE-GROUP-
MONITOR-IS-UNLOADED-IS-STORED-AND-NEVER-APPLIED).

The monitor here comes from the real `KnowledgeService` factory, so the test
runs the same load path the service does.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.chat_history_handlers import HistoryRequestRegistry
from dpc_client_core.message_handlers.group_handler import GroupSyncHandler

SELF = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
EARLIER = "2026-09-20T01:00:00+00:00"
BOUNDARY = "2026-09-22T09:27:57+00:00"
LATER = "2026-09-22T10:00:00+00:00"


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _P2P:
    node_id = SELF
    peers = {}

    def __init__(self):
        self.sent = []

    async def send_message_to_peer(self, node_id, message):
        self.sent.append((node_id, message))


class _Service:
    def __init__(self, group_manager):
        self.group_manager = group_manager
        self.local_api = _Api()
        self.p2p_manager = _P2P()
        self.conversation_monitors = {}
        self.history_requests = HistoryRequestRegistry()
        # The attributes the real factory reads, nothing more.
        self.llm_manager = None
        self.settings = None
        self.peer_metadata = {}
        self.instruction_set = SimpleNamespace(default="general")
        self._send_ai_query = None
        self._build_participants = lambda conversation_id: [
            {"node_id": m} for m in group_manager.get_group(conversation_id).members
        ]
        self._announce_history_after_repair = lambda group_id: None

    def _get_or_create_conversation_monitor(self, conversation_id, instruction_set_name=None):
        return KnowledgeService._get_or_create_conversation_monitor(
            self, conversation_id, instruction_set_name
        )

    def clear_group_access_denied(self, peer_id, group_id):
        pass


def _signed(node_id, group_id, proposal_id="p1"):
    from dpc_protocol.message_signing import VOTE_PREIMAGE_VERSION, vote_content_hash

    return {
        "vote": True,
        "signer_node_id": node_id,
        "vote_preimage_version": VOTE_PREIMAGE_VERSION,
        "timestamp": BOUNDARY,
        "vote_hash": vote_content_hash(
            proposal_id=proposal_id, conversation_id=group_id,
            voter_node_id=node_id, vote=True, timestamp=BOUNDARY,
        ),
        "signature": "sig",
    }


@pytest.fixture
def verifier(monkeypatch):
    from dpc_protocol.commit_integrity import CommitSigner

    answer = {"value": True}
    monkeypatch.setattr(
        CommitSigner, "verify_signature", staticmethod(lambda *a, **k: answer["value"])
    )
    return answer


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A group on disk with three records, two of them before the boundary."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manager = GroupManager(dpc_home=tmp_path / ".dpc", node_id=SELF)
    group = manager.create_group("work", "topic", [PEER])
    service = _Service(manager)

    # Written through a monitor, then dropped: the ordinary state of a group
    # nobody has opened since the service started.
    monitor = service._get_or_create_conversation_monitor(group.group_id)
    monitor.message_history = [
        {"id": f"m{i}", "timestamp": ts, "content": "x", "sender_node_id": PEER}
        for i, ts in enumerate([EARLIER, EARLIER, LATER])
    ]
    monitor.save_history()
    history_path = monitor._get_history_path()
    service.conversation_monitors.clear()
    return service, manager, group, history_path


def _sync_with_marker(group, evidence):
    payload = group.to_dict()
    payload["version"] = group.version + 1
    payload["session_started_at"] = BOUNDARY
    payload["session_reset_evidence"] = evidence
    return payload


def _evidence(group_id, votes):
    return {
        "proposal_id": "p1",
        "conversation_id": group_id,
        "participants": [SELF, PEER],
        "votes": votes,
    }


def _timestamps_on_disk(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return [m["timestamp"] for m in data["messages"]]


@pytest.mark.asyncio
async def test_a_proven_marker_trims_history_with_no_monitor_loaded(world, verifier):
    service, manager, group, history_path = world
    gid = group.group_id
    evidence = _evidence(gid, {SELF: _signed(SELF, gid), PEER: _signed(PEER, gid)})

    await GroupSyncHandler(service).handle(PEER, _sync_with_marker(group, evidence))

    assert manager.get_group(gid).session_started_at == BOUNDARY
    assert _timestamps_on_disk(history_path) == [LATER]
    assert ("conversation_reset", {"conversation_id": gid}) in service.local_api.events


@pytest.mark.asyncio
async def test_a_later_load_does_not_bring_the_trimmed_records_back(world, verifier):
    service, _, group, _ = world
    gid = group.group_id
    evidence = _evidence(gid, {SELF: _signed(SELF, gid), PEER: _signed(PEER, gid)})
    await GroupSyncHandler(service).handle(PEER, _sync_with_marker(group, evidence))

    service.conversation_monitors.clear()
    reloaded = service._get_or_create_conversation_monitor(gid)

    assert [m["timestamp"] for m in reloaded.message_history] == [LATER]


@pytest.mark.asyncio
async def test_an_unproven_marker_leaves_history_untouched(world, verifier):
    service, _, group, history_path = world
    gid = group.group_id
    evidence = _evidence(gid, {SELF: _signed(SELF, gid)})  # the peer never voted

    await GroupSyncHandler(service).handle(PEER, _sync_with_marker(group, evidence))

    assert _timestamps_on_disk(history_path) == [EARLIER, EARLIER, LATER]
    assert not any(name == "conversation_reset" for name, _ in service.local_api.events)


@pytest.mark.asyncio
async def test_an_uncheckable_signature_leaves_history_untouched(world, verifier):
    service, _, group, history_path = world
    gid = group.group_id
    verifier["value"] = None
    evidence = _evidence(gid, {SELF: _signed(SELF, gid), PEER: _signed(PEER, gid)})

    await GroupSyncHandler(service).handle(PEER, _sync_with_marker(group, evidence))

    assert _timestamps_on_disk(history_path) == [EARLIER, EARLIER, LATER]


@pytest.mark.asyncio
async def test_a_loaded_monitor_is_trimmed_in_place(world, verifier):
    service, _, group, history_path = world
    gid = group.group_id
    loaded = service._get_or_create_conversation_monitor(gid)
    evidence = _evidence(gid, {SELF: _signed(SELF, gid), PEER: _signed(PEER, gid)})

    await GroupSyncHandler(service).handle(PEER, _sync_with_marker(group, evidence))

    assert service.conversation_monitors[gid] is loaded
    assert [m["timestamp"] for m in loaded.message_history] == [LATER]
    assert _timestamps_on_disk(history_path) == [LATER]
