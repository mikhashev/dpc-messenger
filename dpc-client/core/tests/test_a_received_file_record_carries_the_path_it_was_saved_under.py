"""A plain file received in a chat keeps its saved path on the chat record.

The bubble offers "Show in folder" only when the attachment carries
`file_path`. Image and voice records already did; a plain file's record named
the file, its size and type and nothing about where it landed, so the person
went looking under ~/.dpc/conversations/<peer>/files by hand.
"""

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.managers.file_transfer_manager import (
    FileTransfer,
    FileTransferManager,
    TransferStatus,
)

PEER = "dpc-node-" + "b" * 32
PAYLOAD = b"# notes\n"


class _P2P:
    node_id = "dpc-node-" + "a" * 32

    async def send_message_to_peer(self, node_id, message):
        pass


class _LocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


async def _receive(tmp_path, filename, mime_type, **extra):
    api = _LocalApi()
    manager = FileTransferManager(
        p2p_manager=_P2P(), firewall=SimpleNamespace(rules={}),
        settings=SimpleNamespace(get=lambda section, key, default=None: default),
        local_api=api, storage_base_path=tmp_path,
    )
    transfer = FileTransfer(
        transfer_id="t-1", filename=filename, size_bytes=len(PAYLOAD), hash="none",
        mime_type=mime_type, chunk_size=len(PAYLOAD), node_id=PEER, direction="download",
        status=TransferStatus.TRANSFERRING, chunks_received=set(), total_chunks=1, **extra,
    )
    manager.active_transfers[transfer.transfer_id] = transfer
    await manager.handle_file_chunk(PEER, {
        "transfer_id": "t-1", "chunk_index": 0,
        "data": base64.b64encode(PAYLOAD).decode(),
    })
    return api


def _attachment(api, event):
    matches = [p for n, p in api.events if n == event]
    assert matches, f"no {event} among {[n for n, _ in api.events]}"
    return matches[0]["attachments"][0]


@pytest.mark.asyncio
async def test_a_plain_file_record_names_the_file_on_disk(tmp_path):
    api = await _receive(tmp_path, "notes.md", "text/markdown")

    att = _attachment(api, "new_p2p_message")

    assert att["type"] == "file"
    assert att["status"] == "completed"
    assert att["file_path"], "the record does not say where the file is"
    saved = Path(att["file_path"])
    assert saved.is_absolute()
    assert saved.name == "notes.md"
    assert saved.read_bytes() == PAYLOAD


@pytest.mark.asyncio
async def test_a_group_file_record_names_the_file_on_disk_too(tmp_path):
    api = await _receive(tmp_path, "notes.md", "text/markdown", group_id="group-0a52389f2bb6")

    att = _attachment(api, "group_file_received")

    assert Path(att["file_path"]).read_bytes() == PAYLOAD
