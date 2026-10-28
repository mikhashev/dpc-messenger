"""A cancel ends the send, and a chunk of a cancelled id is answered once, not once per chunk.

Live, 2026-10-28: a receiver cancelled an 8.3 GB group download at 1.6 %, and the
sender's chunk loop (`handle_file_accept`) kept reading and sending every chunk to
the end, about 17 a second, while the receiver logged `FILE_CHUNK for unknown
download` for each (card THE-SENDERS-CHUNK-LOOP-IGNORES-A-CANCEL-SO-A-CANCELLED-
FILE-IS-STILL-SENT-WHOLE). Now the loop looks at its transfer before each chunk,
and the receiver tells a sender that kept streaming, once per window, with the
reason it cancelled for.

Real managers; only the wire is collected. Nothing touches ~/.dpc.
"""

import base64
import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import file_transfer_manager as ftm
from dpc_client_core.managers.file_transfer_manager import (
    FileTransfer, FileTransferManager, TransferStatus,
)

PEER = "dpc-node-" + "b" * 32
US = "dpc-node-" + "a" * 32
CHUNK = 1024
CHUNKS = 20
LOGGER = "dpc_client_core.managers.file_transfer_manager"


class _Wire:
    """Collects what is sent; `on_chunk(index)` may act while a chunk is on its way."""

    def __init__(self):
        self.node_id = US
        self.sent = []
        self.peers = {}
        self.on_chunk = None

    async def send_message_to_peer(self, node_id, message):
        self.sent.append(message)
        if message["command"] == "FILE_CHUNK" and self.on_chunk:
            await self.on_chunk(message["payload"]["chunk_index"])

    def commands(self, name):
        return [m["payload"] for m in self.sent if m["command"] == name]


def _manager(tmp_path):
    wire = _Wire()
    manager = FileTransferManager(
        p2p_manager=wire,
        firewall=SimpleNamespace(rules={}),
        settings=SimpleNamespace(get=lambda section, key, default=None: "0" if key == "chunk_delay" else default),
        local_api=None,
        storage_base_path=tmp_path,
    )
    return manager, wire


def _upload(manager, tmp_path, transfer_id="up-1"):
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (CHUNK * CHUNKS))
    transfer = FileTransfer(
        transfer_id=transfer_id, filename="big.bin", size_bytes=path.stat().st_size,
        hash="sha256:x", mime_type="application/octet-stream", chunk_size=CHUNK,
        node_id=PEER, direction="upload", status=TransferStatus.PENDING,
        chunks_received=set(), total_chunks=CHUNKS, file_path=path,
    )
    manager.chunk_size = CHUNK
    manager.active_transfers[transfer_id] = transfer
    return transfer


def _download(manager, transfer_id="down-1"):
    transfer = FileTransfer(
        transfer_id=transfer_id, filename="stage.dump", size_bytes=CHUNK * CHUNKS,
        hash="sha256:x", mime_type="application/octet-stream", chunk_size=CHUNK,
        node_id=PEER, direction="download", status=TransferStatus.TRANSFERRING,
        chunks_received=set(), total_chunks=CHUNKS,
    )
    manager.active_transfers[transfer_id] = transfer
    return transfer


def _chunk_payload(transfer_id, index):
    return {"transfer_id": transfer_id, "chunk_index": index,
            "data": base64.b64encode(b"x" * CHUNK).decode()}


# --- the sender -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_uncancelled_upload_sends_every_chunk(tmp_path):
    manager, wire = _manager(tmp_path)
    _upload(manager, tmp_path)

    await manager.handle_file_accept(PEER, "up-1")

    assert [c["chunk_index"] for c in wire.commands("FILE_CHUNK")] == list(range(CHUNKS))


@pytest.mark.asyncio
async def test_a_file_cancel_from_the_receiver_stops_the_send_within_one_chunk(tmp_path, caplog):
    manager, wire = _manager(tmp_path)
    _upload(manager, tmp_path)

    async def receiver_cancels(index):
        if index == 4:
            await manager.handle_file_cancel(PEER, {"transfer_id": "up-1", "reason": "user_cancelled"})

    wire.on_chunk = receiver_cancels
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await manager.handle_file_accept(PEER, "up-1")

    assert [c["chunk_index"] for c in wire.commands("FILE_CHUNK")] == [0, 1, 2, 3, 4]
    assert wire.commands("FILE_CANCEL") == []  # the peer knows already
    stopped = [r for r in caplog.records if "stopped sending big.bin" in r.getMessage()]
    assert len(stopped) == 1 and "5/20" in stopped[0].getMessage()
    assert not any("All chunks sent" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_senders_own_cancel_stops_the_send_within_one_chunk(tmp_path):
    manager, wire = _manager(tmp_path)
    _upload(manager, tmp_path)

    async def user_cancels(index):
        if index == 6:
            await manager.cancel_transfer("up-1")

    wire.on_chunk = user_cancels
    await manager.handle_file_accept(PEER, "up-1")

    assert [c["chunk_index"] for c in wire.commands("FILE_CHUNK")] == list(range(7))
    assert [c["reason"] for c in wire.commands("FILE_CANCEL")] == ["user_cancelled"]  # one, from cancel_transfer
    assert "up-1" not in manager.active_transfers


@pytest.mark.asyncio
async def test_a_cancelled_unsaved_screenshot_is_released_when_the_loop_stops(tmp_path):
    manager, wire = _manager(tmp_path)
    transfer = _upload(manager, tmp_path)
    transfer.save_to_disk = False

    async def receiver_cancels(index):
        if index == 1:
            await manager.handle_file_cancel(PEER, {"transfer_id": "up-1", "reason": "user_rejected"})

    wire.on_chunk = receiver_cancels
    await manager.handle_file_accept(PEER, "up-1")

    assert not transfer.file_path.exists()  # the finally still ran, once the file was closed
    assert len(wire.commands("FILE_CHUNK")) == 2


# --- the receiver ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_many_chunks_of_a_cancelled_download_get_one_warning_and_one_more_cancel(tmp_path, caplog):
    manager, wire = _manager(tmp_path)
    _download(manager)
    await manager.cancel_transfer("down-1")
    assert len(wire.commands("FILE_CANCEL")) == 1

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for i in range(50):
            await manager.handle_file_chunk(PEER, _chunk_payload("down-1", i))

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert [c["reason"] for c in wire.commands("FILE_CANCEL")] == ["user_cancelled", "user_cancelled"]
    assert sum("dropped" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG) == 49


@pytest.mark.asyncio
async def test_the_repeat_uses_the_reason_the_download_was_cancelled_for(tmp_path):
    manager, wire = _manager(tmp_path)
    transfer = _download(manager)
    await manager._fail_download(PEER, transfer, "missing_chunks")

    await manager.handle_file_chunk(PEER, _chunk_payload("down-1", 3))

    assert [c["reason"] for c in wire.commands("FILE_CANCEL")] == ["missing_chunks", "missing_chunks"]


@pytest.mark.asyncio
async def test_a_chunk_of_an_id_never_seen_is_logged_once_and_nothing_is_sent(tmp_path, caplog):
    manager, wire = _manager(tmp_path)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for i in range(30):
            await manager.handle_file_chunk(PEER, _chunk_payload("who-knows", i))

    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1
    assert wire.sent == []  # DPTP has no reason word for an unknown transfer


@pytest.mark.asyncio
async def test_the_answer_is_given_again_after_the_memory_window(tmp_path, monkeypatch):
    manager, wire = _manager(tmp_path)
    now = [1000.0]
    monkeypatch.setattr(ftm, "_monotonic", lambda: now[0])
    _download(manager)
    await manager.cancel_transfer("down-1")

    await manager.handle_file_chunk(PEER, _chunk_payload("down-1", 0))
    await manager.handle_file_chunk(PEER, _chunk_payload("down-1", 1))
    assert len(wire.commands("FILE_CANCEL")) == 2

    now[0] += ftm.CANCEL_MEMORY_S + 1
    await manager.handle_file_chunk(PEER, _chunk_payload("down-1", 2))
    # the original cancel is forgotten too, so this id is now just an unknown one
    assert len(wire.commands("FILE_CANCEL")) == 2
    assert "down-1" not in manager._cancels_sent


@pytest.mark.asyncio
async def test_what_a_receiver_remembers_is_bounded(tmp_path):
    manager, _wire_ = _manager(tmp_path)

    for i in range(ftm.CANCEL_MEMORY_MAX * 2):
        await manager._send_file_cancel(PEER, f"c-{i}", "user_cancelled")
        await manager.handle_file_chunk(PEER, _chunk_payload(f"s-{i}", 0))

    assert len(manager._cancels_sent) <= ftm.CANCEL_MEMORY_MAX
    assert len(manager._stray_chunk_ids) <= ftm.CANCEL_MEMORY_MAX
