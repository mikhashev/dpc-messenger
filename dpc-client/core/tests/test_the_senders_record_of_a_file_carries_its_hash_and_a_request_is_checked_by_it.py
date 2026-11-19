"""The sender's own record of a file carries the file's content hash.

`send_group_file` writes the sender's record before any offer (3837b3a5) and
used to leave the hash off it, because hashing a large file is a pass of its
own. A member that synced that record and later asked for the file
(FILE_REQUEST, 41008ab3) could then check an answer only by its name and size,
so any bytes of the right length completed the record (`content_verified:
false`). Mike's call, 2026-11-19: note the hash the send computes anyway on the
record, so a request is checked by hash.

- After a fan-out the record gains the fan-out's hash: no second read.
- With nobody offered the file (a group of one node, every other member
  offline) the file is hashed in the background and the send answers first.
- A member that syncs the record gets the hash with it, and a request for the
  file is then checked by hash: other bytes of the same size are refused.
- Screenshots and voice messages are in memory at send time; their records
  carry the hash from the start. A one-to-one sender record, written at
  FILE_COMPLETE, already carried the transfer's hash.
- No attachment field is inside the signed preimage or the local chain, so the
  record keeps verifying after the hash is noted.
- A holder that answers a request sends the SHA-256 even with `verify_hash`
  off; otherwise it could never serve a record that has a hash.

Card: A-NODE-THAT-SYNCS-A-RECORD-NEVER-FETCHES-THE-FILE-IT-LACKS.
"""

import asyncio
import hashlib
import logging
import threading
from types import SimpleNamespace

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core.conversation_monitor import ConversationMonitor, note_attachment_hash
from dpc_client_core.managers import file_transfer_manager as ftm
from dpc_client_core.managers.file_request import FILE_REQUEST, NOT_THE_REQUESTED_FILE_REASON
from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.service import PENDING_RECORD_HASHES, CoreService
from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.message_signing import message_content_hash
from tests.test_a_group_file_crosses_a_full_mesh_once import (
    GROUP, _ids, _make_node, _Network as _MeshNetwork, full_mesh, records, send,
)
from tests.test_a_requested_file_is_taken_only_when_it_is_the_file_requested import (
    CONTENT, CONTENT_HASH, _a_answers_with, _cancels_to, _Network, _other_file, _outcome,
)
from tests.test_a_synced_record_without_its_file_can_be_requested_from_a_peer_online import (
    _a_file_sent_while_c_was_away,
)

SONG = b"ID3" + bytes(range(256)) * 40
SONG_HASH = hashlib.sha256(SONG).hexdigest()


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path / "home")
    return tmp_path


def _picked(root, data=SONG, name="song.mp3"):
    f = root / "outbox" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


@pytest.fixture(autouse=True)
def _no_pass_left_by_another_test():
    # A test elsewhere that ends before its background pass leaves the task in
    # the set, pending for good on its closed loop.
    PENDING_RECORD_HASHES.clear()
    yield


async def _record_hashes_done():
    if PENDING_RECORD_HASHES:
        await asyncio.wait_for(asyncio.gather(*list(PENDING_RECORD_HASHES)), 10)


# --- (1) nobody offered: hashed in the background, after the answer -----------------

@pytest.mark.asyncio
@pytest.mark.parametrize("others", ["none", "offline"])
async def test_a_file_nobody_was_offered_gains_its_hash_after_the_answer(root, monkeypatch, others):
    net = _MeshNetwork()
    me, away = _ids(2)
    node = _make_node(net, root, me, [], [me] if others == "none" else [me, away])
    monitor = node.service._get_or_create_conversation_monitor(GROUP)
    saves = []
    monitor.save_history = lambda: saves.append(
        [a.get("hash") for r in monitor.message_history for a in r["attachments"]]) or True

    # The hasher waits until the test lets it go: a stand-in for a file that
    # takes seconds to read. The send must not wait for it.
    release = threading.Event()
    started = threading.Event()
    real = ftm.hash_file_in_one_pass

    def slow_hasher(*args, **kwargs):
        started.set()
        assert release.wait(10), "the test never released the hasher"
        return real(*args, **kwargs)

    monkeypatch.setattr(service_mod, "hash_file_in_one_pass", slow_hasher)

    result = await asyncio.wait_for(
        CoreService.send_group_file(node.service, GROUP, str(_picked(root))), 5)

    assert result["status"] == "success", result
    assert result["transfer_ids"] == []
    (rec,) = records(node)
    assert "hash" not in rec["attachments"][0], "the answer came before the hash"
    assert len(PENDING_RECORD_HASHES) == 1
    assert await asyncio.to_thread(started.wait, 5), "the hash runs off the event loop"

    release.set()
    await _record_hashes_done()

    assert rec["attachments"][0]["hash"] == SONG_HASH
    assert saves[-1] == [SONG_HASH], "the history was saved with the hash"
    assert rec["id"] == result["message_id"]


@pytest.mark.asyncio
async def test_a_group_left_while_its_file_was_hashed_gets_no_history_written(root):
    net = _MeshNetwork()
    me = _ids(1)[0]
    node = _make_node(net, root, me, [], [me])
    monitor = node.service._get_or_create_conversation_monitor(GROUP)
    saves = []

    result = await CoreService.send_group_file(node.service, GROUP, str(_picked(root)))
    monitor.save_history = lambda: saves.append(1)
    node.service.group_manager = type("Gone", (), {"get_group": staticmethod(lambda g: None)})()
    await _record_hashes_done()

    assert result["saved_locally"] is True
    assert saves == [], "no save into a group that is gone"


# --- (2) a fan-out: the record takes the fan-out's hash --------------------------------

@pytest.mark.asyncio
async def test_after_a_fan_out_the_senders_record_carries_the_hash_from_the_one_read(root, monkeypatch):
    net = _MeshNetwork()
    author, peer = full_mesh(net, root, 2)
    reads = []
    real = ftm.hash_file_in_one_pass

    def counting(*args, **kwargs):
        reads.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(ftm, "hash_file_in_one_pass", counting)
    monkeypatch.setattr(service_mod, "hash_file_in_one_pass", counting)

    result = await CoreService.send_group_file(author.service, GROUP, str(_picked(root)))

    (rec,) = records(author)
    assert rec["attachments"][0]["hash"] == SONG_HASH, "noted before the answer"
    assert not PENDING_RECORD_HASHES, "no background pass"
    await net.settle()
    assert not net.errors, net.errors
    authors_reads = [p for p in reads if author.root in p.parents]
    assert len(authors_reads) == 1, "the fan-out's read, reused for the record"
    (peer_rec,) = records(peer)
    assert peer_rec["id"] == result["message_id"]
    assert peer_rec["attachments"][0]["hash"] == SONG_HASH


@pytest.mark.asyncio
async def test_a_member_that_syncs_the_record_while_away_gets_the_hash(root):
    net = _Network()
    a, b, c, _ = await _a_file_sent_while_c_was_away(net, root)

    (authors,) = records(a)
    (synced,) = records(c)
    assert authors["attachments"][0]["hash"] == CONTENT_HASH
    assert synced["attachments"][0]["hash"] == CONTENT_HASH


# --- (3) a request for such a record is checked by hash --------------------------------

@pytest.mark.asyncio
async def test_other_bytes_of_the_same_size_are_refused_and_the_next_member_serves(root):
    net = _Network()
    a, b, c, _ = await _a_file_sent_while_c_was_away(net, root)
    (rec,) = records(c)
    _a_answers_with(a, _other_file(root, b"y" * len(CONTENT)))  # same name, same size

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    assert [f[0] for f in net.named(FILE_REQUEST)] == [a.me, b.me], "A refused, B asked next"
    assert len(_cancels_to(net, a.me, NOT_THE_REQUESTED_FILE_REASON)) == 1
    (after,) = records(c)
    att = after["attachments"][0]
    with open(att["file_path"], "rb") as f:
        assert f.read() == CONTENT
    assert "content_verified" not in att, "checked by the record's hash"
    assert "content_verified" not in final["attachment"]


@pytest.mark.asyncio
async def test_when_only_a_member_with_other_bytes_is_online_nothing_lands(root):
    net = _Network()
    a, b, c, _ = await _a_file_sent_while_c_was_away(net, root)
    (rec,) = records(c)
    c.service.p2p_coordinator = type(
        "Only", (), {"get_connected_peers": staticmethod(lambda: [a.me])})()
    _a_answers_with(a, _other_file(root, b"y" * len(CONTENT)))

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "failed", final
    (after,) = records(c)
    assert "content_verified" not in after["attachments"][0]
    assert c.api.named("group_file_received") == []


@pytest.mark.asyncio
async def test_a_holder_with_verify_hash_off_still_serves_a_record_that_has_a_hash(root):
    net = _Network()
    a, b, c, _ = await _a_file_sent_while_c_was_away(net, root)
    (rec,) = records(c)
    rec["attachments"][0]["hash"] = CONTENT_HASH  # as synced from the author today
    a.manager.verify_hash = False
    b.manager.verify_hash = False

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    (offer,) = net.offers_to(c.me)
    assert offer[1] == a.me and offer[3]["hash"] == CONTENT_HASH
    assert net.cancels() == []


# --- (4) screenshots, voice messages, one-to-one files ---------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "voice"])
async def test_a_screenshot_or_voice_record_carries_its_hash_from_the_start(root, kind):
    net = _MeshNetwork()
    me = _ids(1)[0]
    node = _make_node(net, root, me, [], [me])

    result = await send(node, kind, root)

    (rec,) = records(node)
    with open(result["file_path"], "rb") as f:
        on_disk = f.read()
    assert rec["attachments"][0]["hash"] == hashlib.sha256(on_disk).hexdigest()
    assert not PENDING_RECORD_HASHES


@pytest.mark.asyncio
async def test_a_one_to_one_senders_record_carries_the_transfers_hash(root):
    # Unchanged by this commit: FileCompleteHandler has always put the
    # transfer's hash on the record it writes at FILE_COMPLETE.
    net = _MeshNetwork()
    sender, peer = full_mesh(net, root, 2)

    await P2PCoordinator.send_file(SimpleNamespace(service=sender.service), peer.me,
                                   str(_picked(root)))
    await net.settle()
    (offer,) = peer.api.named("file_transfer_offered")  # the person on the peer accepts
    await P2PCoordinator.accept_file_transfer(
        SimpleNamespace(service=peer.service, p2p_manager=peer.service.p2p_manager),
        offer["transfer_id"])
    await net.settle()

    assert not net.errors, net.errors
    (rec,) = sender.monitors[peer.me].message_history
    assert rec["attachments"][0]["hash"] == SONG_HASH


# --- (5) the record keeps verifying ----------------------------------------------------

@pytest.fixture
def _persist(monkeypatch):
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))


def _real_monitor(tmp_path, cid=GROUP):
    m = ConversationMonitor(conversation_id=cid, participants=[
        {"node_id": "n1", "name": "Me", "context": "local"}], llm_manager=None)
    m._get_history_path = lambda: tmp_path / cid / "history.json"
    return m


def test_a_noted_hash_leaves_the_signature_and_the_chain_verifying_and_travels_on_sync(
        tmp_path, signing_identity, _persist, monkeypatch, caplog):
    sender = _real_monitor(tmp_path / "sender")
    author = signing_identity.node_id
    sender.add_message("user", "first", sender_node_id=author, sender_name="Mike",
                       message_id="m-0", sender_type="human")
    attachment = {"type": "file", "filename": "song.mp3", "size_bytes": len(SONG),
                  "mime_type": "audio/mpeg", "status": "completed",
                  "file_path": str(tmp_path / "files" / "song.mp3")}
    sender.add_message("user", "Sent file: song.mp3 (0.01 MB)", [attachment],
                       sender_node_id=author, sender_name="Mike", message_id="m-1",
                       sender_type="human")
    sender.add_message("user", "after", sender_node_id=author, sender_name="Mike",
                       message_id="m-2", sender_type="human")
    sender.save_history()
    rec = sender.message_history[1]
    signed = {k: rec[k] for k in ("content_hash", "signature", "chain_hash", "msg_index")}

    assert note_attachment_hash(sender.message_history, "m-1", "song.mp3", len(SONG), SONG_HASH)
    assert service_mod.note_record_hash(sender, "m-1", "song.mp3", len(SONG), SONG_HASH) is False, \
        "a hash already there is not written again"
    sender._history_dirty = True
    sender.save_history()

    # Nothing the signature or the chain covers moved.
    assert {k: rec[k] for k in signed} == signed
    assert rec["content_hash"] == message_content_hash(
        conversation_id=GROUP, message_id="m-1", sender_node_id=author, sender_name="Mike",
        sender_type="human", agent_owner=None, timestamp=rec["timestamp"],
        content=rec["content"], tool_calls=None)
    public_key = signing_identity.private_key.public_key()
    assert CommitSigner.verify_with_public_key(public_key, rec["content_hash"], rec["signature"])

    # The saved history loads with its chain intact, hash included.
    with caplog.at_level(logging.INFO):
        reloaded = _real_monitor(tmp_path / "sender")
        assert reloaded.load_history()
    assert "Chain broken" not in caplog.text
    assert "hash mismatch" not in caplog.text
    assert "Chain integrity verified: 3 of 3" in caplog.text
    assert reloaded.message_history[1]["attachments"][0]["hash"] == SONG_HASH

    # A member syncing the history gets the record verified, with the hash.
    monkeypatch.setattr(CommitSigner, "verify_signature", staticmethod(
        lambda node_id, h, s: CommitSigner.verify_with_public_key(public_key, h, s)))
    member = _real_monitor(tmp_path / "member")
    added = member.merge_history(reloaded.export_history())

    assert added == 3 and member.last_merge_rejected == []
    (got,) = [m for m in member.message_history if m["id"] == "m-1"]
    assert got["verification"] == "verified"
    assert got["attachments"][0]["hash"] == SONG_HASH


def test_a_record_of_another_size_or_name_is_left_alone():
    history = [{"id": "m-1", "attachments": [
        {"type": "file", "filename": "a.mp3", "size_bytes": 3}]}]

    assert not note_attachment_hash(history, "m-1", "a.mp3", 4, SONG_HASH)
    assert not note_attachment_hash(history, "m-1", "b.mp3", 3, SONG_HASH)
    assert not note_attachment_hash(history, "m-2", "a.mp3", 3, SONG_HASH)
    assert not note_attachment_hash(history, "m-1", "a.mp3", 3, "none")
    assert "hash" not in history[0]["attachments"][0]
    assert note_attachment_hash(history, "m-1", "a.mp3", 3, SONG_HASH)
    assert not note_attachment_hash(history, "m-1", "a.mp3", 3, "f" * 64), "never replaced"
    assert history[0]["attachments"][0]["hash"] == SONG_HASH
