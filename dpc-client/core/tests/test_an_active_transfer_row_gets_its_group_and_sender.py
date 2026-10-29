"""Every event that opens an Active Transfers row carries what the row names.

Card AN-ACTIVE-TRANSFER-ROW-DOES-NOT-SAY-WHICH-GROUP-OR-WHO-SENT-IT: the row
reads "in <group> · from <author> via <node>" for a relayed group file and
"in <group> · to <peer>" for an upload. Only the asked offer carried the
claimed author, and `file_transfer_started` carried no group at all. Now every
offer event of a group file carries `group_id` and `sender_node_id`, and
`file_transfer_started` carries `group_id`.

Real manager and the real FILE_OFFER handler; only the wire and the UI socket
are collected. Nothing touches ~/.dpc.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.managers.file_transfer_manager import (
    FileTransfer, FileTransferManager, TransferStatus,
)
from dpc_client_core.message_handlers.file_offer_handler import FileOfferHandler

HOP = "dpc-node-" + "b" * 32
AUTHOR = "dpc-node-" + "c" * 32
US = "dpc-node-" + "a" * 32
GROUP = "group-1234abcd"


class _Wire:
    def __init__(self):
        self.node_id = US
        self.sent = []
        self.peers = {}

    async def send_message_to_peer(self, node_id, message):
        self.sent.append(message)


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _node(tmp_path):
    wire, api = _Wire(), _Api()
    manager = FileTransferManager(
        p2p_manager=wire,
        firewall=SimpleNamespace(rules={"file_transfer": {"nodes": {HOP: {"file_transfer.allow": "allow"}}}}),
        settings=SimpleNamespace(get=lambda section, key, default=None: "0" if key == "chunk_delay" else default),
        local_api=api,
        storage_base_path=tmp_path,
    )
    service = SimpleNamespace(file_transfer_manager=manager, p2p_manager=wire,
                              peer_metadata={HOP: {"name": "Bob"}}, local_api=api)
    return manager, api, FileOfferHandler(service)


KINDS = {
    "file_transfer_offered": {"mime_type": "application/octet-stream"},
    "image_offer_received": {"mime_type": "image/png",
                             "image_metadata": {"dimensions": {"width": 1, "height": 1}, "thumbnail_base64": ""}},
    "voice_offer_received": {"mime_type": "audio/webm",
                             "voice_metadata": {"duration_seconds": 1.0, "codec": "opus"}},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("event", sorted(KINDS))
async def test_a_relayed_group_offer_names_its_group_and_its_author(tmp_path, event):
    manager, api, handler = _node(tmp_path)

    await handler.handle(HOP, {
        "transfer_id": "down-1", "filename": "f.bin", "size_bytes": 10, "hash": "sha256:x",
        "chunk_size": 1024, "group_id": GROUP, "message_id": "m-1", "sender_node_id": AUTHOR,
        **KINDS[event],
    })

    offers = [p for n, p in api.events if n == event]
    assert len(offers) == 1
    offer = offers[0]
    assert offer["node_id"] == HOP           # the hop: "via Bob"
    assert offer["sender_name"] == "Bob"
    assert offer["group_id"] == GROUP
    assert offer["sender_node_id"] == AUTHOR  # the claimed author: "from Cara"
    manager.active_transfers.clear()  # stops the receive's clocks


@pytest.mark.asyncio
async def test_a_one_to_one_offer_carries_no_group_fields(tmp_path):
    manager, api, handler = _node(tmp_path)

    await handler.handle(HOP, {
        "transfer_id": "down-1", "filename": "f.bin", "size_bytes": 10, "hash": "sha256:x",
        "chunk_size": 1024, "mime_type": "application/octet-stream", "sender_node_id": AUTHOR,
    })

    (offer,) = [p for n, p in api.events if n == "file_transfer_offered"]
    assert "group_id" not in offer and "sender_node_id" not in offer
    manager.active_transfers.clear()


def _upload(manager, tmp_path, group_id):
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * 2048)
    manager.chunk_size = 1024
    manager.active_transfers["up-1"] = FileTransfer(
        transfer_id="up-1", filename="big.bin", size_bytes=2048, hash="sha256:x",
        mime_type="application/octet-stream", chunk_size=1024, node_id=HOP, direction="upload",
        status=TransferStatus.PENDING, chunks_received=set(), total_chunks=2, file_path=path,
        group_id=group_id,
    )


@pytest.mark.asyncio
async def test_an_upload_row_names_the_group_it_is_sent_in(tmp_path):
    manager, api, _ = _node(tmp_path)
    _upload(manager, tmp_path, GROUP)

    await manager.handle_file_accept(HOP, "up-1")

    (started,) = [p for n, p in api.events if n == "file_transfer_started"]
    assert started["group_id"] == GROUP
    assert started["node_id"] == HOP


@pytest.mark.asyncio
async def test_a_one_to_one_upload_row_has_no_group(tmp_path):
    manager, api, _ = _node(tmp_path)
    _upload(manager, tmp_path, None)

    await manager.handle_file_accept(HOP, "up-1")

    (started,) = [p for n, p in api.events if n == "file_transfer_started"]
    assert "group_id" not in started
