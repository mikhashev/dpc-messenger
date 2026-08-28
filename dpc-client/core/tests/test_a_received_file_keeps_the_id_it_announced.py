"""A file announced to the screen and stored on disk must be one message.

The receiver does two things with an incoming file: it broadcasts a chat bubble
to the UI and it writes a record into the conversation. Those two carried
different ids — the broadcast used the id computed here, the record got a fresh
one from `add_message` — and the history backfill, which joins the live bubble
to the stored record by id, could never match them. Opening the chat drew every
received file twice.

Measured 2026-08-28 in Mike's 1:1 chat with the Linux node: two records on the
backend, four bubbles on screen.
"""
from pathlib import Path

import pytest

from dpc_client_core.managers.file_transfer_manager import (
    FileTransfer,
    FileTransferManager,
    TransferStatus,
)


class _P2P:
    """Records what would have gone to the peer."""

    def __init__(self):
        self.sent = []

    async def send_message_to_peer(self, node_id, message):
        self.sent.append((node_id, message))


class _LocalApi:
    """Records what the UI would have been told."""

    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))

    def event(self, name):
        matches = [p for n, p in self.events if n == name]
        assert matches, f"no {name} event among {[n for n, _ in self.events]}"
        return matches[0]


class _Monitor:
    """Records what was written into the conversation."""

    def __init__(self, root: Path):
        self.added = []
        self._root = root

    def _get_conversation_dir(self):
        return self._root

    def add_message(self, role, content, attachments=None, **kwargs):
        self.added.append({"role": role, "content": content,
                           "attachments": attachments, **kwargs})


class _Service:
    def __init__(self, monitor):
        self.peer_metadata = {"dpc-node-peer": {"name": "Mike (linux)"}}
        self._monitor = monitor

    def _get_or_create_conversation_monitor(self, key):
        return self._monitor


class _Firewall:
    rules: dict = {}


class _Settings:
    """Answers with the default the caller already names."""

    def get(self, section, key, default=None):
        return default


@pytest.fixture
def received_file(tmp_path):
    """One completed inbound transfer, everything around it real but small."""
    monitor = _Monitor(tmp_path / "conversation")
    api = _LocalApi()
    manager = FileTransferManager(
        p2p_manager=_P2P(),
        firewall=_Firewall(),
        settings=_Settings(),
        local_api=api,
        storage_base_path=tmp_path,
        service=_Service(monitor),
    )
    payload = b"a file that arrived"
    transfer = FileTransfer(
        transfer_id="t-1",
        filename="paste_1787912268575.png",
        size_bytes=len(payload),
        # "none" is what a sender writes when it asks for no verification, and
        # it keeps this test off the hashing path, which is not what is under
        # test here.
        hash="none",
        mime_type="image/png",
        chunk_size=len(payload),
        node_id="dpc-node-peer",
        direction="download",
        status=TransferStatus.TRANSFERRING,
        chunks_received={0},
        total_chunks=1,
        chunk_data={0: payload},
        image_metadata={"dimensions": {"width": 1, "height": 1}},
    )
    # Tracked the way the chunk handler tracks it; finalisation drops it from
    # here on the way out and raises without it.
    manager.active_transfers[transfer.transfer_id] = transfer
    return manager, transfer, api, monitor


@pytest.mark.asyncio
async def test_the_bubble_and_the_record_carry_one_id(received_file):
    manager, transfer, api, monitor = received_file

    await manager._finalize_download("dpc-node-peer", transfer)

    broadcast = api.event("new_p2p_message")
    assert len(monitor.added) == 1, monitor.added
    stored = monitor.added[0]

    assert broadcast["message_id"], "the broadcast must carry an id at all"
    assert stored.get("message_id") == broadcast["message_id"], (
        "the stored record and the bubble on screen are the same message; "
        "two ids make the history backfill draw it twice"
    )


@pytest.mark.asyncio
async def test_the_record_says_who_sent_it_and_what_arrived(received_file):
    """The fields the chat renders, so a later edit cannot quietly drop them."""
    manager, transfer, api, monitor = received_file

    await manager._finalize_download("dpc-node-peer", transfer)

    stored = monitor.added[0]
    assert stored["role"] == "assistant"
    assert transfer.filename in stored["content"]
    assert stored["sender_node_id"] == "dpc-node-peer"
    assert stored["sender_name"] == "Mike (linux)"
    assert stored["attachments"][0]["filename"] == transfer.filename
