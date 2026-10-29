"""Transfer progress goes out once a second, with the speed and the time left.

Card THE-ACTIVE-TRANSFERS-PANEL-SHOWS-NO-SPEED-OR-TIME-LEFT-AND-A-LARGE-DOWNLOAD-
SITS-AT-0-PERCENT-FOR-MINUTES: the receiver sent `file_transfer_progress` every
total_chunks // 20 chunks (six minutes apart on an 8 GB file), the sender every
10 chunks, and neither said how fast. Now both send it on a time cadence (the
first chunk, then at most once per PROGRESS_INTERVAL_S, then the last chunk)
with `bytes_per_second` over a 5 s window and `seconds_left` (Mike's names,
2026-10-29), null while the speed is not known.

Real managers and the real FILE_OFFER handler; the clock is the manager's
`_monotonic` indirection. Nothing touches ~/.dpc.
"""

import base64
import hashlib
import zlib
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import file_transfer_manager as ftm
from dpc_client_core.managers.file_transfer_manager import (
    FileTransfer, FileTransferManager, TransferStatus,
)
from dpc_client_core.managers.transfer_speed import TransferSpeed
from dpc_client_core.message_handlers.file_offer_handler import FileOfferHandler

PEER = "dpc-node-" + "b" * 32
US = "dpc-node-" + "a" * 32
CHUNK = 1024
STEP_S = 0.125  # exact in binary, so the rates below are exact integers


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _Wire:
    def __init__(self, clock=None):
        self.node_id = US
        self.sent = []
        self.peers = {}
        self.clock = clock  # advanced by STEP_S per chunk sent, when given

    async def send_message_to_peer(self, node_id, message):
        self.sent.append(message)
        if message["command"] == "FILE_CHUNK" and self.clock is not None:
            self.clock.now += STEP_S


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))

    def named(self, name):
        return [p for n, p in self.events if n == name]


def _manager(tmp_path, wire, api):
    return FileTransferManager(
        p2p_manager=wire,
        firewall=SimpleNamespace(rules={"file_transfer": {"nodes": {PEER: {"file_transfer.allow": "allow"}}}}),
        settings=SimpleNamespace(get=lambda section, key, default=None: "0" if key == "chunk_delay" else default),
        local_api=api,
        storage_base_path=tmp_path,
    )


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(ftm, "_monotonic", c)
    return c


# --- the helper -----------------------------------------------------------------


def test_the_speed_is_unknown_before_a_second_of_samples():
    speed = TransferSpeed(0.0)
    speed.add(CHUNK, 0.0)
    speed.add(CHUNK, 0.5)
    assert speed.bytes_per_second() == 0
    assert speed.seconds_left(10 * CHUNK) is None
    assert speed.fields(10 * CHUNK) == {"bytes_per_second": 0, "seconds_left": None}


def test_the_speed_is_the_rate_over_the_last_five_seconds():
    speed = TransferSpeed(0.0)
    t = 0.0
    for _ in range(40):          # 10 s at 4000 B/s
        t += 0.25
        speed.add(1000, t)
    for _ in range(20):          # then 5 s at 8000 B/s
        t += 0.25
        speed.add(2000, t)
    assert speed.bytes_per_second() == 8000
    assert speed.done == 80000
    assert speed.seconds_left(80000 + 8000 * 3 + 1) == 4  # rounded up, never 0 while bytes remain
    assert speed.seconds_left(80000) == 0


def test_an_event_is_due_on_the_first_chunk_then_once_a_second_and_on_the_last():
    speed = TransferSpeed(0.0)
    assert speed.event_due(0.0, last_chunk=False)
    assert not speed.event_due(0.5, last_chunk=False)
    assert not speed.event_due(0.99, last_chunk=False)
    assert speed.event_due(1.0, last_chunk=False)
    assert speed.event_due(1.01, last_chunk=True)


# --- the sender -----------------------------------------------------------------


def _upload(manager, tmp_path, chunks):
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (CHUNK * chunks))
    transfer = FileTransfer(
        transfer_id="up-1", filename="big.bin", size_bytes=path.stat().st_size,
        hash="sha256:x", mime_type="application/octet-stream", chunk_size=CHUNK,
        node_id=PEER, direction="upload", status=TransferStatus.PENDING,
        chunks_received=set(), total_chunks=chunks, file_path=path,
    )
    manager.chunk_size = CHUNK
    manager.active_transfers["up-1"] = transfer


@pytest.mark.asyncio
async def test_an_upload_within_one_second_sends_one_progress_event_and_the_last(tmp_path, clock):
    wire, api = _Wire(), _Api()
    manager = _manager(tmp_path, wire, api)
    _upload(manager, tmp_path, 30)

    await manager.handle_file_accept(PEER, "up-1")

    progress = api.named("file_transfer_progress")
    assert [p["chunks_sent"] for p in progress] == [1, 30]
    assert progress[0]["bytes_per_second"] == 0
    assert progress[0]["seconds_left"] is None
    assert progress[-1]["progress_percent"] == 100


@pytest.mark.asyncio
async def test_an_upload_reports_its_speed_and_time_left_once_a_second(tmp_path, clock):
    wire, api = _Wire(clock), _Api()
    manager = _manager(tmp_path, wire, api)
    _upload(manager, tmp_path, 30)

    await manager.handle_file_accept(PEER, "up-1")

    progress = api.named("file_transfer_progress")
    # Chunk k leaves at (k + 1) * 0.125 s: events at 0.125, 1.125, 2.125, 3.125 s, and the last
    assert [p["chunks_sent"] for p in progress] == [1, 9, 17, 25, 30]
    assert progress[0]["seconds_left"] is None
    second = progress[1]
    assert second["bytes_per_second"] == CHUNK / STEP_S == 8192
    assert second["seconds_left"] == 3  # 21 KB left at 8 KB/s, rounded up
    assert {"transfer_id", "node_id", "filename", "direction", "total_chunks", "progress_percent"} <= set(second)


# --- the receiver ---------------------------------------------------------------


def _slices(data):
    return [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]


async def _receive(tmp_path, clock, chunks, step_s):
    wire, api = _Wire(), _Api()
    manager = _manager(tmp_path, wire, api)
    service = SimpleNamespace(file_transfer_manager=manager, p2p_manager=wire, peer_metadata={}, local_api=api)
    data = bytes(range(256)) * (CHUNK * chunks // 256)
    await FileOfferHandler(service).handle(PEER, {
        "transfer_id": "down-1", "filename": "stage.dump", "size_bytes": len(data),
        "hash": hashlib.sha256(data).hexdigest(), "mime_type": "application/octet-stream",
        "chunk_size": CHUNK,
        "chunk_hashes": [f"{zlib.crc32(s) & 0xffffffff:08x}" for s in _slices(data)],
    })
    manager.begin_receiving("down-1")
    for index, piece in enumerate(_slices(data)):
        clock.now = index * step_s
        await manager.handle_file_chunk(PEER, {
            "transfer_id": "down-1", "chunk_index": index,
            "data": base64.b64encode(piece).decode(),
        })
    return api


@pytest.mark.asyncio
async def test_a_download_within_one_second_sends_one_progress_event_and_the_last(tmp_path, clock):
    api = await _receive(tmp_path, clock, 30, step_s=0.0)

    progress = api.named("file_transfer_progress")
    assert [p["chunks_received"] for p in progress] == [1, 30]
    assert progress[0]["seconds_left"] is None
    assert progress[0]["bytes_per_second"] == 0
    assert progress[-1]["progress_percent"] == 100.0
    # The last one goes out before the finalize takes the transfer away
    names = [n for n, _ in api.events]
    last_progress = max(i for i, n in enumerate(names) if n == "file_transfer_progress")
    assert names.index("file_transfer_complete") > last_progress


@pytest.mark.asyncio
async def test_a_download_reports_its_speed_and_time_left_once_a_second(tmp_path, clock):
    api = await _receive(tmp_path, clock, 60, step_s=STEP_S)

    progress = api.named("file_transfer_progress")
    # Chunk k arrives at k * 0.125 s: events at 0, 1, 2, ... 7 s, and the last
    assert [p["chunks_received"] for p in progress] == [1, 9, 17, 25, 33, 41, 49, 57, 60]
    assert progress[0]["seconds_left"] is None
    # At 7 s the 5 s window holds chunks 17..57 only: exactly 8 KB/s
    at_seven = progress[7]
    assert at_seven["bytes_per_second"] == 8192
    assert at_seven["seconds_left"] == 1  # 3 KB left
    assert at_seven["status"] == "transferring"
    assert len(api.named("file_transfer_complete")) == 1
