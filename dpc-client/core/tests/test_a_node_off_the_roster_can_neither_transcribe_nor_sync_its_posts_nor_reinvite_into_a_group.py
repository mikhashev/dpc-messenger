"""A node off a group's roster reaches it neither by transcription, by history sync, nor by invitation.

Follow-up 10a to ca4cf45a (card
A-NODE-REMOVED-FROM-A-GROUP-CAN-STILL-POST-INTO-IT-AND-THE-OWNER-STORES-THE-POST),
three holes found in its review:

1. VOICE_TRANSCRIPTION checked nobody: any connected peer that knew a transfer
   id or a file name appended text to a group's history, its UI and its relay.
2. GROUP_HISTORY_RESPONSE merged records by authors off the roster, and their
   mentions could wake agents: a removed node's post, stored by a member whose
   roster was stale, reached the creator by sync.
3. GROUP_CREATE for a group held here was applied from any peer: `topic`,
   `agents` and the version rode in at a higher version.
"""
import datetime
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_protocol import commit_integrity
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.group_handler import (
    GroupCreateHandler,
    GroupHistoryResponseHandler,
    GroupHistoryStatusHandler,
    GroupSyncHandler,
)
from dpc_client_core.message_handlers.voice_transcription_handler import VoiceTranscriptionHandler
from tests.test_a_mention_that_arrives_by_history_sync_wakes_once import (
    GROUP as SYNC_GROUP,
    _Service as _SyncService,
    _key,
    _record,
    _sync,
)
from tests.test_group_roster_and_history_gate import SELF, _Service as _GateService

ME = "dpc-node-" + "a" * 32
MEMBER = "dpc-node-" + "b" * 32
OTHER_MEMBER = "dpc-node-" + "c" * 32
REMOVED = "dpc-node-" + "d" * 32
VOICE_GROUP = "group-0f0f0f0f0f0f"
VOICE = "voice_2026-11-24_10-00-00.webm"
TRANSFER = "transfer-from-the-author-to-the-transcriber"


# --- 1. VOICE_TRANSCRIPTION -------------------------------------------------


class _VoiceService:
    """This node: holds VOICE_GROUP (members ME, MEMBER, OTHER_MEMBER) with one voice record."""

    def __init__(self):
        self.attachment = {"type": "voice", "filename": VOICE, "transfer_id": "our-own-transfer"}
        self.monitor = SimpleNamespace(
            message_history=[{"id": "v-1", "role": "user", "sender_node_id": MEMBER,
                              "attachments": [self.attachment]}],
            message_buffer=[], full_conversation=[],
        )
        self.group = SimpleNamespace(group_id=VOICE_GROUP, created_by=ME,
                                     members=[ME, MEMBER, OTHER_MEMBER])
        self.group_manager = SimpleNamespace(
            get_group=lambda gid: self.group if gid == VOICE_GROUP else None)
        self.conversation_monitors = {VOICE_GROUP: self.monitor, REMOVED: self._one_to_one()}
        self.p2p_manager = SimpleNamespace(
            node_id=ME, peers={MEMBER: object(), OTHER_MEMBER: object(), REMOVED: object()},
            send_message_to_peer=AsyncMock())
        self.local_api = SimpleNamespace(broadcast_event=AsyncMock())
        self._voice_transcriptions = {}
        self._processed_message_ids = set()

    def _one_to_one(self):
        self.one_to_one_attachment = {"type": "voice", "filename": VOICE, "transfer_id": TRANSFER}
        return SimpleNamespace(message_history=[{"attachments": [self.one_to_one_attachment]}],
                               message_buffer=[], full_conversation=[])

    def sent(self, command):
        return [c.args for c in self.p2p_manager.send_message_to_peer.await_args_list
                if c.args[1]["command"] == command]

    def stored_nothing(self):
        return (not self._voice_transcriptions
                and "transcription" not in self.attachment
                and "transcription" not in self.one_to_one_attachment
                and self.local_api.broadcast_event.await_count == 0
                and not self.sent("VOICE_TRANSCRIPTION"))


def _transcription(transcriber, **over):
    payload = {
        "transfer_id": TRANSFER, "group_id": VOICE_GROUP, "filename": VOICE,
        "transcription_text": "text nobody said", "provider": "local_whisper",
        "transcriber_node_id": transcriber, "confidence": 0.9, "language": "en",
    }
    payload.update(over)
    return payload


@pytest.mark.asyncio
async def test_a_transcription_from_a_node_off_the_roster_is_refused_and_stores_nothing():
    service = _VoiceService()

    await VoiceTranscriptionHandler(service).handle(REMOVED, _transcription(REMOVED))

    assert service.stored_nothing()
    [(to, message)] = service.sent("GROUP_ACCESS_DENIED")
    assert to == REMOVED
    assert message["payload"] == {"group_id": VOICE_GROUP, "reason": "not_a_member"}


@pytest.mark.asyncio
async def test_a_member_relaying_a_transcription_by_a_node_off_the_roster_is_dropped_untold():
    service = _VoiceService()

    await VoiceTranscriptionHandler(service).handle(MEMBER, _transcription(REMOVED))

    assert service.stored_nothing()
    assert not service.sent("GROUP_ACCESS_DENIED"), "the relaying member is not the one refused"


@pytest.mark.asyncio
async def test_a_transcription_for_a_voice_message_the_group_does_not_hold_stores_nothing():
    service = _VoiceService()

    await VoiceTranscriptionHandler(service).handle(
        MEMBER, _transcription(MEMBER, transfer_id="made-up", filename="voice_made_up.webm"))

    assert service.stored_nothing()


@pytest.mark.asyncio
async def test_a_transcription_naming_a_group_not_held_or_a_one_to_one_id_stores_nothing():
    """A group id that names a peer's one-to-one conversation reached its monitor."""
    service = _VoiceService()

    await VoiceTranscriptionHandler(service).handle(
        MEMBER, _transcription(MEMBER, group_id=REMOVED))
    await VoiceTranscriptionHandler(service).handle(
        MEMBER, _transcription(MEMBER, group_id="group-000000000000"))

    assert service.stored_nothing()


@pytest.mark.asyncio
async def test_a_members_transcription_lands_and_is_relayed():
    """Control: any member holding the voice message may transcribe it."""
    service = _VoiceService()

    await VoiceTranscriptionHandler(service).handle(MEMBER, _transcription(MEMBER))

    assert service.attachment["transcription"]["text"] == "text nobody said"
    assert service._voice_transcriptions[TRANSFER]["transcriber_node_id"] == MEMBER
    assert service.local_api.broadcast_event.await_count == 1
    assert [to for to, _ in service.sent("VOICE_TRANSCRIPTION")] == [OTHER_MEMBER]


# --- 2. GROUP_HISTORY_RESPONSE ----------------------------------------------


@pytest.fixture
def nodes(tmp_path, monkeypatch):
    """A member and a removed node, both signing with cached certificates."""
    peers = tmp_path / "peers"
    peers.mkdir()
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(ConversationMonitor, "save_history", lambda self: True)
    return SimpleNamespace(member=_key(peers), removed=_key(peers))


@pytest.mark.asyncio
async def test_a_removed_nodes_post_handed_over_by_a_member_is_not_merged_and_wakes_nobody(nodes):
    """The incident by the sync door: a stale member stored the removed node's
    post, and its history answer would hand it to the creator."""
    member = nodes.member[0]
    service = _SyncService([member])  # the removed node is not on the roster
    post = _record(nodes.removed, "@Ark status from the removed node")

    await _sync(service, member, [post])

    assert post["id"] not in service.monitor.message_ids
    assert service.run.await_count == 0


@pytest.mark.asyncio
async def test_the_members_posts_in_the_same_answer_still_merge_and_wake(nodes):
    """Control: the gate takes only the outsider's record out of the answer."""
    member = nodes.member[0]
    service = _SyncService([member])
    theirs = _record(nodes.removed, "@Ark from the removed node")
    ours = _record(nodes.member, "@Ark from a member", name="Zcode")

    await _sync(service, member, [theirs, ours])

    assert ours["id"] in service.monitor.message_ids
    assert theirs["id"] not in service.monitor.message_ids
    assert service.run.await_count == 1
    assert service.run.await_args.kwargs["trigger_message_id"] == ours["id"]


@pytest.mark.asyncio
async def test_a_former_members_post_already_held_is_kept(nodes):
    """Control: history made while it was a member is never deleted."""
    member = nodes.member[0]
    service = _SyncService([member])
    old = _record(nodes.removed, "posted while it was a member",
                  age=datetime.timedelta(days=3))
    service.monitor.merge_history([dict(old)])

    await _sync(service, member, [old])

    assert old["id"] in service.monitor.message_ids


@pytest.mark.asyncio
async def test_a_history_answer_from_a_node_off_the_roster_merges_nothing(nodes):
    """Control (built before 10a): the responder must be a member."""
    removed = nodes.removed[0]
    service = _SyncService([])
    request_id = uuid.uuid4().hex[:8]
    service.history_requests.note(removed, SYNC_GROUP, request_id)
    post = _record(nodes.removed, "@Ark from the removed node")

    await GroupHistoryResponseHandler(service).handle(
        removed, {"group_id": SYNC_GROUP, "history": [post], "request_id": request_id})

    assert post["id"] not in service.monitor.message_ids
    assert service.run.await_count == 0


@pytest.mark.asyncio
async def test_a_status_does_not_ask_for_the_posts_of_an_author_off_the_roster(nodes):
    """Asking would only bring records the answer withholds, at every status."""
    member, removed = nodes.member[0], nodes.removed[0]
    service = _SyncService([member])
    digest = {"authors": {
        member: {"count": 1, "digest": "sha256:" + "1" * 16},
        removed: {"count": 1, "digest": "sha256:" + "2" * 16},
    }, "digest": "sha256:" + "3" * 16}

    await GroupHistoryStatusHandler(service).handle(member, {
        "group_id": SYNC_GROUP, "history_hash": "x", "message_count": 2,
        "history_digest": digest, "is_reply": True,
    })

    requests = [c.args[1]["payload"] for c in service.p2p_manager.send_message_to_peer.await_args_list
                if c.args[1]["command"] == "GROUP_HISTORY_REQUEST"]
    assert [r["authors"] for r in requests] == [[member]]


# --- 3. GROUP_CREATE --------------------------------------------------------


@pytest.fixture
def held(tmp_path):
    """This node holds a group created by CREATOR, with MEMBER on it."""
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    group = manager.apply_sync({
        "group_id": "group-0123456789ab", "name": "work", "topic": "the creator's topic",
        "created_by": MEMBER, "members": [MEMBER, SELF, OTHER_MEMBER],
        "agents": {OTHER_MEMBER: ["agent_001"]}, "agent_names": {}, "version": 5,
    }, MEMBER)
    return manager, group


def _rewritten(group, **over):
    record = group.to_dict()
    record.update({"version": 50, "topic": "rewritten", "agents": {OTHER_MEMBER: ["forged"]}})
    record.update(over)
    return record


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", [OTHER_MEMBER, REMOVED], ids=["member", "outsider"])
async def test_a_group_create_for_a_held_group_from_a_non_creator_changes_nothing(held, sender, caplog):
    manager, group = held
    service = _GateService(manager)

    await GroupCreateHandler(service).handle(sender, _rewritten(group))

    kept = manager.get_group(group.group_id)
    assert (kept.topic, kept.agents, kept.version) == (
        "the creator's topic", {OTHER_MEMBER: ["agent_001"]}, 5)
    assert not service.local_api.events
    assert any("only its creator" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_creators_group_create_and_sync_still_update_topic_and_agents(held):
    """Control: the creator re-sending its record (a re-add) still applies."""
    manager, group = held
    service = _GateService(manager)

    await GroupCreateHandler(service).handle(MEMBER, _rewritten(group, version=6))
    assert manager.get_group(group.group_id).topic == "rewritten"

    await GroupSyncHandler(service).handle(
        MEMBER, _rewritten(manager.get_group(group.group_id), version=7, topic="again"))
    assert manager.get_group(group.group_id).topic == "again"
    assert manager.get_group(group.group_id).agents == {OTHER_MEMBER: ["forged"]}


@pytest.mark.asyncio
async def test_a_new_group_from_its_creator_and_a_re_add_still_arrive(tmp_path):
    """Control: first sight from the creator, then a re-add of a node that kept its copy."""
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    service = _GateService(manager)
    gid = "group-0123456789ab"
    record = {"group_id": gid, "name": "work", "topic": "", "created_by": MEMBER,
              "members": [MEMBER, SELF], "agents": {}, "agent_names": {}, "version": 1}

    await GroupCreateHandler(service).handle(MEMBER, dict(record))
    assert SELF in manager.get_group(gid).members

    await GroupSyncHandler(service).handle(MEMBER, dict(record, members=[MEMBER], version=2))
    assert SELF not in manager.get_group(gid).members
    await GroupCreateHandler(service).handle(MEMBER, dict(record, version=3))
    assert SELF in manager.get_group(gid).members
