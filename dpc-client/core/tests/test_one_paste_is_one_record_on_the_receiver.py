"""One pasted screenshot must be one record on the node that receives it.

The receiver learns of a paste twice: group history sync brings the sender's
own signed record (foreign `file_path`, no hash, no transfer id), and the
transfer itself ends in `_finalize_download`, which used to append a second
record of its own — «Received screenshot: …», attributed to the sender, signed
by us. Three records for one paste on 2026-09-03; the UI rendered only ours.

Whichever arrives first, the sender's record is the one that survives:

  - sync first: the transfer completes that record in place (the signature
    covers no attachment field, so it stays the sender's and verifiable);
  - transfer first: our note is dropped when the sender's record arrives.
    It could not stay — signed by us and attributed to them, it is what every
    other node rejects, and its hash sits in the sender's author digest, so
    the two copies would disagree about that author on every connect.
    Dropping is safe for the chain, which describes this node's copy and is
    already recomputed from genesis on every reorder.
"""

from pathlib import Path

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.managers.file_transfer_manager import (
    FileTransfer,
    FileTransferManager,
    TransferStatus,
)

GROUP = "group-0a52389f2bb6"
FILENAME = "paste_1788443154187.png"
PAYLOAD = b"a screenshot that arrived"
FOREIGN = f"/home/mike/.dpc/conversations/{GROUP}-1234/files/screenshots/{FILENAME}"


class _P2P:
    def __init__(self):
        self.sent = []

    async def send_message_to_peer(self, node_id, message):
        self.sent.append((node_id, message))


class _LocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))

    def event(self, name):
        matches = [p for n, p in self.events if n == name]
        assert matches, f"no {name} event among {[n for n, _ in self.events]}"
        return matches[0]


class _Service:
    def __init__(self, monitor, peer):
        self.peer_metadata = {peer: {"name": "Mike (linux)"}}
        self._monitor = monitor

    def _get_or_create_conversation_monitor(self, key):
        return self._monitor


class _Firewall:
    rules: dict = {}


class _Settings:
    def get(self, section, key, default=None):
        return default


@pytest.fixture
def two_nodes(tmp_path, monkeypatch, signing_identity, _test_signing_key):
    """A sender that signs as itself and a receiver that signs as itself."""
    from dpc_protocol.commit_integrity import CommitSigner

    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))
    alice = signing_identity.node_id
    bob = "dpc-node-" + "b" * 32
    roster = [
        {"node_id": alice, "name": "Mike (linux)", "context": "peer"},
        {"node_id": bob, "name": "User", "context": "local"},
    ]

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "alice"))
    sender = ConversationMonitor(conversation_id=GROUP, participants=[
        {"node_id": alice, "name": "User", "context": "local"},
        {"node_id": bob, "name": "Windows", "context": "peer"},
    ], llm_manager=None)
    sender.add_message(
        "user", "", [{
            "type": "image", "filename": FILENAME, "file_path": FOREIGN,
            "mime_type": "image/png", "size_bytes": len(PAYLOAD),
            "thumbnail": "data:image/png;base64,thumb", "status": "completed",
        }],
        timestamp="2026-09-03T10:00:00+00:00", sender_node_id=alice, sender_name="Mike (linux)",
    )
    exported = sender.export_history()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "bob"))
    receiver = ConversationMonitor(conversation_id=GROUP, participants=roster, llm_manager=None)
    monkeypatch.setattr(receiver, "_get_signer", lambda: CommitSigner(bob, _test_signing_key))

    api = _LocalApi()
    manager = FileTransferManager(
        p2p_manager=_P2P(), firewall=_Firewall(), settings=_Settings(), local_api=api,
        storage_base_path=tmp_path / "bob" / "unused", service=_Service(receiver, alice),
    )
    transfer = FileTransfer(
        transfer_id="t-1", filename=FILENAME, size_bytes=len(PAYLOAD), hash="none",
        mime_type="image/png", chunk_size=len(PAYLOAD), node_id=alice, direction="download",
        status=TransferStatus.TRANSFERRING, chunks_received={0}, total_chunks=1,
        chunk_data={0: PAYLOAD}, image_metadata={"dimensions": {"width": 1, "height": 1}},
        group_id=GROUP,
    )
    manager.active_transfers[transfer.transfer_id] = transfer
    return alice, exported, receiver, manager, transfer, api


def _the_one_record(receiver, alice):
    assert len(receiver.message_history) == 1, [m["id"] for m in receiver.message_history]
    record = receiver.message_history[0]
    assert record["sender_node_id"] == alice
    assert record["signer_node_id"] == alice, "the sender's record, still theirs"
    return record


def _local_file(receiver):
    return receiver._get_conversation_dir() / "files" / "screenshots" / FILENAME


@pytest.mark.asyncio
async def test_sync_first_the_transfer_completes_the_senders_record(two_nodes):
    alice, exported, receiver, manager, transfer, api = two_nodes
    assert receiver.merge_history(exported) == 1
    sender_id = receiver.message_history[0]["id"]
    content_hash = receiver.message_history[0]["content_hash"]

    await manager._finalize_download(alice, transfer)

    record = _the_one_record(receiver, alice)
    att = record["attachments"][0]
    assert record["id"] == sender_id
    assert record["content_hash"] == content_hash, "nothing the signature covers moved"
    assert att["file_path"] == str(_local_file(receiver))
    assert att["transfer_id"] == "t-1"
    assert att["thumbnail"] == "data:image/png;base64,thumb", "the sender's thumbnail is kept"
    assert api.event("group_file_received")["message_id"] == sender_id


@pytest.mark.asyncio
async def test_sync_first_the_completed_record_is_what_is_on_disk(two_nodes):
    alice, exported, receiver, manager, transfer, api = two_nodes
    receiver.merge_history(exported)

    await manager._finalize_download(alice, transfer)

    import json
    stored = json.loads(receiver._get_history_path().read_text(encoding="utf-8"))["messages"]
    assert len(stored) == 1
    assert stored[0]["attachments"][0]["file_path"] == str(_local_file(receiver))


@pytest.mark.asyncio
async def test_transfer_first_our_note_gives_way_to_the_senders_record(two_nodes):
    alice, exported, receiver, manager, transfer, api = two_nodes

    await manager._finalize_download(alice, transfer)
    ours = receiver.message_history[0]["id"]
    assert receiver.message_history[0]["signer_node_id"] != alice, "the note is ours"

    assert receiver.merge_history(exported) == 1

    record = _the_one_record(receiver, alice)
    att = record["attachments"][0]
    assert record["id"] != ours
    assert ours not in receiver.message_ids
    assert att["file_path"] == str(_local_file(receiver))
    assert att["transfer_id"] == "t-1", "what the transfer knew moved over"
    assert att["thumbnail"] == "data:image/png;base64,thumb"
    assert record["msg_index"] == 1 and record["chain_hash"], "rechained from genesis"


@pytest.mark.asyncio
async def test_transfer_first_the_dropped_note_is_gone_from_disk(two_nodes):
    alice, exported, receiver, manager, transfer, api = two_nodes
    await manager._finalize_download(alice, transfer)

    receiver.merge_history(exported)

    import json
    stored = json.loads(receiver._get_history_path().read_text(encoding="utf-8"))["messages"]
    assert [m["sender_node_id"] for m in stored] == [alice]
    assert stored[0]["signer_node_id"] == alice
