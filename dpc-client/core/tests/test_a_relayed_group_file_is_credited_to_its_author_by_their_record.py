"""A group file that came through a relay ends up under its author, and only their record says so.

In a star a leaf receives the file from the hub, so the transfer names the hub.
The offer also carries the author's `sender_node_id` and `message_id`, but
unsigned: by the project's signing rule (GROUP_TEXT's legacy case) a claimed
author with nothing behind it is worth less than the socket, so the claim alone
credits nobody. What credits the author is their own signed record of the file,
which history sync brings, found by that `message_id`:

  - record first: the transfer completes it in place, so the file shows under
    the author at once;
  - transfer first: the note this node writes names the hub, as before, but
    is keyed by the author's `message_id`, so when the record arrives it takes
    the note's place instead of standing beside it.

Before 2026-10-25 the note had an id of its own and named the hub, and the
merge looked for a note by the author: the leaf kept two records of one file,
one of them under the hub.
"""

import base64
from pathlib import Path

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.managers.file_transfer_manager import (
    FileTransfer,
    FileTransferManager,
    TransferStatus,
)

GROUP = "group-0b52389f2bb6"
FILENAME = "shot_1788443154187.png"
PAYLOAD = b"a screenshot that crossed the hub"
FOREIGN = f"/home/mike/.dpc/conversations/{GROUP}/files/screenshots/{FILENAME}"
MESSAGE_ID = "5e1f0c9b2a7d4e31"
HUB = "dpc-node-" + "d" * 32  # not the signing identity's "a" node
LEAF = "dpc-node-" + "c" * 32


class _P2P:
    node_id = LEAF
    peers = {HUB: object()}

    def __init__(self):
        self.sent = []

    async def send_message_to_peer(self, node_id, message):
        self.sent.append((node_id, message))


class _LocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))

    def shown(self):
        return [p for n, p in self.events if n == "group_file_received"]


class _Service:
    def __init__(self, monitor):
        self.peer_metadata = {HUB: {"name": "Hub"}}
        self._monitor = monitor

    def _get_or_create_conversation_monitor(self, key):
        return self._monitor


class _Settings:
    def get(self, section, key, default=None):
        return default


@pytest.fixture
def leaf(tmp_path, monkeypatch, signing_identity, _test_signing_key):
    """The author signs its record; the leaf receives the file from the hub."""
    from dpc_protocol.commit_integrity import CommitSigner

    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))
    author = signing_identity.node_id
    assert author not in (HUB, LEAF), "the author must be a third node, or nothing is tested"

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "author"))
    sender = ConversationMonitor(conversation_id=GROUP, participants=[
        {"node_id": author, "name": "User", "context": "local"},
        {"node_id": HUB, "name": "Hub", "context": "peer"},
        {"node_id": LEAF, "name": "Leaf", "context": "peer"},
    ], llm_manager=None)
    sender.add_message(
        "user", "", [{
            "type": "image", "filename": FILENAME, "file_path": FOREIGN,
            "mime_type": "image/png", "size_bytes": len(PAYLOAD),
            "thumbnail": "data:image/png;base64,thumb", "status": "completed",
        }],
        timestamp="2026-10-25T10:00:00+00:00", sender_node_id=author, sender_name="Author",
        message_id=MESSAGE_ID,
    )
    exported = sender.export_history()
    assert [m["id"] for m in exported] == [MESSAGE_ID]

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "leaf"))
    receiver = ConversationMonitor(conversation_id=GROUP, participants=[
        {"node_id": author, "name": "Author", "context": "peer"},
        {"node_id": HUB, "name": "Hub", "context": "peer"},
        {"node_id": LEAF, "name": "User", "context": "local"},
    ], llm_manager=None)
    monkeypatch.setattr(receiver, "_get_signer", lambda: CommitSigner(LEAF, _test_signing_key))

    api = _LocalApi()
    manager = FileTransferManager(
        p2p_manager=_P2P(), firewall=type("F", (), {"rules": {}})(), settings=_Settings(),
        local_api=api, storage_base_path=tmp_path / "leaf" / "unused", service=_Service(receiver),
    )
    transfer = FileTransfer(
        transfer_id="t-relayed", filename=FILENAME, size_bytes=len(PAYLOAD), hash="none",
        mime_type="image/png", chunk_size=len(PAYLOAD), node_id=HUB, direction="download",
        status=TransferStatus.TRANSFERRING, chunks_received=set(), total_chunks=1,
        image_metadata={"dimensions": {"width": 1, "height": 1}},
        group_id=GROUP, sender_node_id=author, message_id=MESSAGE_ID,
    )
    manager.active_transfers[transfer.transfer_id] = transfer
    return author, exported, receiver, manager, transfer, api


async def _arrive_from_the_hub(manager, transfer):
    await manager.handle_file_chunk(HUB, {
        "transfer_id": transfer.transfer_id,
        "chunk_index": 0,
        "data": base64.b64encode(PAYLOAD).decode(),
    })


def _local_file(receiver):
    return receiver._get_conversation_dir() / "files" / "screenshots" / FILENAME


def _the_authors_record(receiver, author):
    assert len(receiver.message_history) == 1, [
        (m["id"], m["sender_node_id"]) for m in receiver.message_history]
    record = receiver.message_history[0]
    assert record["id"] == MESSAGE_ID
    assert record["sender_node_id"] == author
    assert record["signer_node_id"] == author, "credited by the author's own signature"
    assert record["attachments"][0]["file_path"] == str(_local_file(receiver))
    return record


@pytest.mark.asyncio
async def test_record_first_a_file_from_the_hub_completes_the_authors_record(leaf):
    author, exported, receiver, manager, transfer, api = leaf
    assert receiver.merge_history(exported) == 1

    await _arrive_from_the_hub(manager, transfer)

    _the_authors_record(receiver, author)
    assert [e["message_id"] for e in api.shown()] == [MESSAGE_ID]


@pytest.mark.asyncio
async def test_transfer_first_the_unsigned_claim_credits_nobody(leaf):
    author, exported, receiver, manager, transfer, api = leaf

    await _arrive_from_the_hub(manager, transfer)

    [note] = receiver.message_history
    assert note["sender_node_id"] == HUB, "the socket, not the offer's claim"
    assert note["signer_node_id"] == LEAF
    assert note["id"] == MESSAGE_ID, "keyed by the author's message_id"


@pytest.mark.asyncio
async def test_transfer_first_the_authors_record_takes_the_notes_place(leaf):
    author, exported, receiver, manager, transfer, api = leaf
    await _arrive_from_the_hub(manager, transfer)

    receiver.merge_history(exported)

    record = _the_authors_record(receiver, author)
    assert record["attachments"][0]["transfer_id"] == "t-relayed", "what the transfer knew moved over"
