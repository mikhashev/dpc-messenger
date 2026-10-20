"""A received file goes to disk chunk by chunk, and a chunk that never came is asked again.

The receiver used to keep every chunk in a dict and write the file only when the
last one arrived, so a 47 GB file needed 47 GB of RAM (card
A-RECEIVED-FILE-IS-HELD-WHOLE-IN-MEMORY-UNTIL-THE-LAST-CHUNK, Mike's call
2026-10-20). Its "missing chunks" branch could never fire, because a chunk was
counted only together with its dict entry, and nothing in the module had a
timeout: a chunk lost on the way left the transfer waiting forever.

Now each verified chunk is written at index * the offer's chunk_size into a
temporary file; a silence with chunks missing re-asks them through
FILE_CHUNK_RETRY; after RECEIVE_STALL_ROUNDS fruitless rounds the receiver gives
up with `missing_chunks`; every way out deletes the temporary file.
"""

import asyncio
import base64
import hashlib
import random
import time
import tracemalloc
import zlib
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import file_transfer_manager as ftm
from dpc_client_core.managers.file_transfer_manager import FileTransferManager, TransferStatus
from dpc_client_core.message_handlers.file_offer_handler import FileOfferHandler

PEER = "dpc-node-" + "b" * 32
US = "dpc-node-" + "a" * 32


class _Wire:
    """What the receiver sends to the peer, and a sender that may answer re-asks."""

    def __init__(self):
        self.node_id = US
        self.sent = []
        self.answer_retries = None  # async callable(index) or None
        self.peers = {}

    async def send_message_to_peer(self, node_id, message):
        self.sent.append(message)
        if message["command"] == "FILE_CHUNK_RETRY" and self.answer_retries:
            asyncio.get_running_loop().create_task(
                self.answer_retries(message["payload"]["chunk_index"])
            )

    def commands(self, name):
        return [m["payload"] for m in self.sent if m["command"] == name]


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))

    def named(self, name):
        return [p for n, p in self.events if n == name]


def _rules(save_screenshots=True):
    return {
        "file_transfer": {"nodes": {PEER: {"file_transfer.allow": "allow"}}},
        "image_transfer": {"save_screenshots_to_disk": save_screenshots},
    }


@pytest.fixture
def node(tmp_path):
    wire, api = _Wire(), _Api()
    manager = FileTransferManager(
        p2p_manager=wire,
        firewall=SimpleNamespace(rules=_rules()),
        settings=SimpleNamespace(get=lambda section, key, default=None: default),
        local_api=api,
        storage_base_path=tmp_path,
    )
    service = SimpleNamespace(
        file_transfer_manager=manager, p2p_manager=wire, peer_metadata={}, local_api=api,
    )
    return SimpleNamespace(
        manager=manager, wire=wire, api=api, handler=FileOfferHandler(service), tmp=tmp_path,
    )


def _slices(data, chunk_size):
    return [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]


async def _offer(node, data, chunk_size, transfer_id="t-1", filename="model.bin",
                 mime_type="application/octet-stream", image_metadata=None):
    """Through the real FILE_OFFER handler, then accepted the way the UI accepts."""
    payload = {
        "transfer_id": transfer_id,
        "filename": filename,
        "size_bytes": len(data),
        "hash": hashlib.sha256(data).hexdigest(),
        "mime_type": mime_type,
        "chunk_size": chunk_size,
        "chunk_hashes": [f"{zlib.crc32(s) & 0xffffffff:08x}" for s in _slices(data, chunk_size)],
    }
    if image_metadata is not None:
        payload["image_metadata"] = image_metadata
    await node.handler.handle(PEER, payload)
    transfer = node.manager.active_transfers[transfer_id]
    if transfer.status == TransferStatus.PENDING:
        node.manager.begin_receiving(transfer_id)  # what P2PCoordinator.accept_file_transfer does
    return transfer


async def _chunk(node, transfer_id, index, piece):
    await node.manager.handle_file_chunk(PEER, {
        "transfer_id": transfer_id,
        "chunk_index": index,
        "data": base64.b64encode(piece).decode(),
    })


def _partial_files(root):
    return [p for p in root.rglob("*.tmp")]


async def _until(predicate, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.fixture
def quick_stall(monkeypatch):
    """The named constants, shrunk so a test waits milliseconds, not minutes."""
    monkeypatch.setattr(ftm, "RECEIVE_STALL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(ftm, "RECEIVE_STALL_ROUNDS", 3)
    return SimpleNamespace(timeout=0.05, rounds=3)


# --- (a) memory does not grow with the file ---------------------------------


@pytest.mark.asyncio
async def test_receiving_a_file_holds_no_more_than_one_chunk_of_it_in_memory(node):
    chunk_size = 64 * 1024
    total = 256  # 16 MB, four times the budget below
    budget = 4 * 1024 * 1024
    rng = random.Random(1)
    first = bytes(rng.getrandbits(8) for _ in range(chunk_size))
    pieces = [bytes([i % 251]) * chunk_size if i else first for i in range(total)]
    data = b"".join(pieces)
    transfer = await _offer(node, data, chunk_size)
    del data

    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        for index in range(total - 1):
            await _chunk(node, "t-1", index, pieces[index])
        held = tracemalloc.get_traced_memory()[0] - baseline
    finally:
        tracemalloc.stop()

    assert held < budget, f"{held} bytes held after {total - 1} chunks of {chunk_size}"
    assert not hasattr(transfer, "chunk_data"), "no dict of chunk bytes on the transfer"
    held_bytes = [k for k, v in vars(transfer).items()
                  if isinstance(v, (bytes, bytearray, memoryview))
                  or (isinstance(v, dict) and any(isinstance(x, (bytes, bytearray)) for x in v.values()))]
    assert held_bytes == [], held_bytes

    await _chunk(node, "t-1", total - 1, pieces[-1])
    final = node.manager._get_peer_storage_path(PEER) / "model.bin"
    assert final.read_bytes() == b"".join(pieces)


# --- (b) out of order, right offsets, right hash ------------------------------


@pytest.mark.asyncio
async def test_chunks_out_of_order_land_at_their_offsets_and_the_hash_matches(node):
    data = bytes(random.Random(2).getrandbits(8) for _ in range(10_000))
    transfer = await _offer(node, data, 1024)
    order = list(range(transfer.total_chunks))
    random.Random(3).shuffle(order)
    pieces = _slices(data, 1024)

    for index in order:
        await _chunk(node, "t-1", index, pieces[index])

    final = node.manager._get_peer_storage_path(PEER) / "model.bin"
    assert final.read_bytes() == data
    [complete] = node.wire.commands("FILE_COMPLETE")
    assert complete["hash"] == hashlib.sha256(data).hexdigest()
    assert _partial_files(node.tmp) == []
    assert "t-1" not in node.manager.active_transfers
    assert transfer.stall_watch is None, "the stall watch ends with the transfer"


# --- (c) a chunk dropped in the middle is asked again ---------------------------


@pytest.mark.asyncio
async def test_a_chunk_dropped_in_the_middle_is_asked_again_and_the_file_completes(node, quick_stall):
    data = bytes(random.Random(4).getrandbits(8) for _ in range(12 * 512))
    pieces = _slices(data, 512)
    await _offer(node, data, 512)

    async def sender_resends(index):
        await _chunk(node, "t-1", index, pieces[index])
    node.wire.answer_retries = sender_resends

    for index, piece in enumerate(pieces):
        if index != 5:
            await _chunk(node, "t-1", index, piece)

    assert await _until(lambda: node.wire.commands("FILE_COMPLETE")), "the transfer never completed"
    assert [r["chunk_index"] for r in node.wire.commands("FILE_CHUNK_RETRY")] == [5]
    assert (node.manager._get_peer_storage_path(PEER) / "model.bin").read_bytes() == data
    assert node.wire.commands("FILE_CANCEL") == []


# --- (d) a chunk never sent ends in a refusal within the named time ----------------


@pytest.mark.asyncio
async def test_a_chunk_never_sent_ends_in_failed_and_file_cancel_within_the_named_time(node, quick_stall):
    data = bytes(range(256)) * 16
    pieces = _slices(data, 512)
    transfer = await _offer(node, data, 512)

    for index, piece in enumerate(pieces):
        if index != 3:
            await _chunk(node, "t-1", index, piece)
    assert _partial_files(node.tmp), "the partial file exists while the transfer waits"
    last_chunk_at = time.monotonic()

    assert await _until(lambda: node.wire.commands("FILE_CANCEL")), "the receiver never gave up"
    elapsed = time.monotonic() - last_chunk_at

    [cancel] = node.wire.commands("FILE_CANCEL")
    assert cancel["reason"] == "missing_chunks"
    assert elapsed < (quick_stall.rounds + 1) * quick_stall.timeout + 0.5
    retries = [r["chunk_index"] for r in node.wire.commands("FILE_CHUNK_RETRY")]
    assert retries == [3] * quick_stall.rounds, "one bounded re-ask per round, then stop"
    assert transfer.status == TransferStatus.FAILED
    assert "t-1" not in node.manager.active_transfers
    assert _partial_files(node.tmp) == []
    assert [e["reason"] for e in node.api.named("file_transfer_cancelled")] == ["missing_chunks"]


@pytest.mark.asyncio
async def test_a_re_ask_is_bounded_to_a_batch(node, quick_stall, monkeypatch):
    monkeypatch.setattr(ftm, "RECEIVE_REASK_BATCH", 4)
    data = bytes(64 * 100)
    await _offer(node, data, 64)
    await _chunk(node, "t-1", 0, data[:64])  # 99 missing

    assert await _until(lambda: node.wire.commands("FILE_CHUNK_RETRY"))
    await asyncio.sleep(0)
    assert [r["chunk_index"] for r in node.wire.commands("FILE_CHUNK_RETRY")][:4] == [1, 2, 3, 4]
    assert len(node.wire.commands("FILE_CHUNK_RETRY")) == 4
    await node.manager.cancel_transfer("t-1")


# --- (e) offsets follow the offer, not the local config ----------------------------


@pytest.mark.asyncio
async def test_offsets_follow_the_offers_chunk_size_not_the_local_one(node):
    assert node.manager.chunk_size == 65536
    data = bytes(random.Random(5).getrandbits(8) for _ in range(5 * 1000 + 17))
    transfer = await _offer(node, data, 1000)
    assert transfer.chunk_size == 1000 and transfer.total_chunks == 6
    pieces = _slices(data, 1000)

    for index in (4, 0, 5, 2, 1, 3):
        await _chunk(node, "t-1", index, pieces[index])

    assert (node.manager._get_peer_storage_path(PEER) / "model.bin").read_bytes() == data


# --- (f) every way out deletes the partial file ------------------------------------


async def _cancel_locally(node):
    await node.manager.cancel_transfer("t-1")


async def _cancel_by_peer(node):
    await node.manager.handle_file_cancel(PEER, {"transfer_id": "t-1", "reason": "user_cancelled"})


async def _peer_disconnected(node):
    # What CoreService._handle_peer_disconnected does: deletes the entry directly
    del node.manager.active_transfers["t-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("way_out", [_cancel_locally, _cancel_by_peer, _peer_disconnected])
async def test_a_cancelled_receive_deletes_its_partial_file_and_its_watch(node, way_out):
    data = bytes(4096)
    transfer = await _offer(node, data, 1024)
    await _chunk(node, "t-1", 0, data[:1024])
    [partial] = _partial_files(node.tmp)
    watch = transfer.stall_watch
    assert watch is not None and not watch.done()

    await way_out(node)
    await asyncio.sleep(0)

    assert not partial.exists()
    assert watch.done(), "the stall watch must not outlive the transfer"
    assert "t-1" not in node.manager.active_transfers


@pytest.mark.asyncio
async def test_a_hash_mismatch_fails_the_transfer_and_frees_it(node):
    data = bytes(3000)
    transfer = await _offer(node, data, 1000)
    transfer.hash = "0" * 64  # every chunk passes its CRC, the whole does not

    for index, piece in enumerate(_slices(data, 1000)):
        await _chunk(node, "t-1", index, piece)

    assert [c["reason"] for c in node.wire.commands("FILE_CANCEL")] == ["hash_mismatch"]
    assert transfer.status == TransferStatus.FAILED
    assert "t-1" not in node.manager.active_transfers
    assert _partial_files(node.tmp) == []
    assert not (node.manager._get_peer_storage_path(PEER) / "model.bin").exists()


# --- (g) images still arrive ------------------------------------------------------


IMAGE_METADATA = {"dimensions": {"width": 2, "height": 2}, "thumbnail_base64": "data:image/png;base64,th"}


@pytest.mark.asyncio
async def test_a_screenshot_is_received_into_the_screenshots_folder(node):
    data = b"\x89PNG" + bytes(range(200)) * 10
    await _offer(node, data, 512, filename="paste.png", mime_type="image/png",
                 image_metadata=IMAGE_METADATA)
    for index, piece in enumerate(_slices(data, 512)):
        await _chunk(node, "t-1", index, piece)

    saved = node.manager._get_peer_storage_path(PEER, "files/screenshots") / "paste.png"
    assert saved.read_bytes() == data
    [message] = node.api.named("new_p2p_message")
    attachment = message["attachments"][0]
    assert attachment["type"] == "image"
    assert attachment["file_path"] == str(saved)
    assert attachment["thumbnail"] == IMAGE_METADATA["thumbnail_base64"]


@pytest.mark.asyncio
async def test_a_screenshot_not_to_be_saved_never_touches_the_disk(node):
    node.manager.firewall.rules = _rules(save_screenshots=False)
    data = bytes(range(256)) * 8
    await _offer(node, data, 512, filename="paste.png", mime_type="image/png",
                 image_metadata=IMAGE_METADATA)
    pieces = _slices(data, 512)
    for index, piece in enumerate(pieces[:-1]):
        await _chunk(node, "t-1", index, piece)
    assert _partial_files(node.tmp) == []
    await _chunk(node, "t-1", len(pieces) - 1, pieces[-1])

    assert [c["hash"] for c in node.wire.commands("FILE_COMPLETE")] == ["none"]
    assert not any(p.is_file() for p in node.tmp.rglob("*"))
    [message] = node.api.named("new_p2p_message")
    assert "file_path" not in message["attachments"][0]
