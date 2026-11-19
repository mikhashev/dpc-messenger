"""A file offer cannot choose how much this node reads at once, or how much it remembers.

Found in review of ADR-047 phase 1 by a fresh subagent, 2026-11-20:

- the description's `chunk_size` was the read size at the end of a download
  (`hash_file_in_one_pass` read a whole multiple of it per block), and only
  the chunk count was bounded, so a 4 GB file described in one 2^40-byte
  chunk was read into memory whole. Now `chunk_size` must be
  `description_chunk_size(size_bytes)`, the one value the author computes, at
  the offer and in the verdict, and a read is never larger than
  HASH_READ_BLOCK_BYTES whatever chunk size it is handed;
- the waits for a file author's certificate were kept per author id until
  the certificate came, and an author id is the offer's to choose. Both
  tables now expire and hold at most CERTIFICATE_WAITS_MAX entries;
- a certificate that came between the offer and the start of the wait was
  waited for anyway, for the whole answer wait. The verdict is checked first.
"""

import asyncio
import hashlib
import zlib
from types import SimpleNamespace

import pytest

from dpc_protocol.message_signing import FILE_PREIMAGE_VERSION
from dpc_client_core.managers import file_transfer_manager as ftm
from dpc_client_core.managers.chunk_window import description_chunk_size
from dpc_client_core.managers.file_request import FileRequests, OpenRequest
from dpc_client_core.managers.file_transfer_manager import (
    CERTIFICATE_WAITS_MAX,
    HASH_READ_BLOCK_BYTES,
    FileTransfer,
    FileTransferManager,
    hash_file_in_one_pass,
)
from dpc_client_core.signing import FILE_REJECTED, FILE_VERIFIED, file_description_verdict
from tests.test_a_file_description_is_signed_by_its_author_and_checked_on_arrival import (  # noqa: F401
    A,
    B,
    _signed,
    identities,
)
from tests.test_a_group_file_crosses_a_full_mesh_once import GROUP

FOUR_GB = 4 * 1024 ** 3


def _offer(desc):
    return {"transfer_id": "t-1", "filename": desc["filename"], "size_bytes": desc["size_bytes"],
            "hash": desc["hash"], "mime_type": desc["mime_type"], "group_id": GROUP,
            "message_id": desc["message_id"], "sender_node_id": desc["sender_node_id"],
            "file_description": desc}


def _description(**overrides):
    desc = {"file_preimage_version": FILE_PREIMAGE_VERSION, "message_id": "m-1",
            "sender_node_id": A, "size_bytes": FOUR_GB, "chunk_size": 1 << 40,
            "hash": "a" * 64, "chunks_hash": "b" * 64, "filename": "big.bin",
            "mime_type": "application/octet-stream", "file_signature": "c2ln"}
    desc.update(overrides)
    return desc


# --- (1) the chunk size is the author's one, by the file's size ---------------------

@pytest.mark.parametrize("chunk_size", [1 << 40, FOUR_GB, 8 << 20, 1, 65537])
def test_an_offer_whose_chunk_size_is_not_the_files_is_rejected_before_any_certificate(chunk_size):
    """No certificate is needed to refuse it: an unverified offer is read at the end too."""
    fake = SimpleNamespace(p2p_manager=SimpleNamespace(node_id=B))
    desc = _description(chunk_size=chunk_size)
    assert chunk_size != description_chunk_size(FOUR_GB)

    _, verdict, why = FileTransferManager.offer_file_description(fake, A, _offer(desc))

    assert verdict == FILE_REJECTED
    assert "chunk_size" in why


def test_an_offer_with_the_files_own_chunk_size_goes_on_to_the_signature():
    fake = SimpleNamespace(p2p_manager=SimpleNamespace(node_id=B))
    desc = _description(chunk_size=description_chunk_size(FOUR_GB))

    _, verdict, why = FileTransferManager.offer_file_description(fake, A, _offer(desc))

    assert why is None or "chunk_size" not in why, why


def test_an_author_that_signs_a_chunk_size_of_its_own_choosing_is_rejected(identities):
    """A valid signature does not make a forged chunk size the file's."""
    identities.know(A)
    honest = _signed(identities)
    assert file_description_verdict(honest, GROUP) == FILE_VERIFIED
    forged = _signed(identities, chunk_size=1 << 40)

    assert file_description_verdict(forged, GROUP) == FILE_REJECTED


def test_no_read_is_larger_than_the_block_whatever_chunk_size_it_is_handed(tmp_path, monkeypatch):
    data = bytes(range(256)) * 200  # 51 200 bytes
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    reads = []
    real_open = open

    class _Spy:
        def __init__(self, f):
            self.f = f

        def read(self, n=-1):
            reads.append(n)
            return self.f.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

    monkeypatch.setattr(ftm, "open", lambda p, mode="r": _Spy(real_open(p, mode)), raising=False)

    file_hash, _, chunks_hash = hash_file_in_one_pass(
        path, 1 << 40, with_chunk_crcs=False, description_chunk_size=1 << 40)

    assert reads and max(reads) <= HASH_READ_BLOCK_BYTES, reads
    assert file_hash == hashlib.sha256(data).hexdigest()
    assert chunks_hash == hashlib.sha256(hashlib.sha256(data).digest()).hexdigest()


@pytest.mark.parametrize("chunk_size", [2500, 1000, 999, 7000, 10_000])
def test_a_chunk_larger_than_the_block_still_gives_each_chunks_crc_and_digest(tmp_path, monkeypatch, chunk_size):
    monkeypatch.setattr(ftm, "HASH_READ_BLOCK_BYTES", 1000)
    data = bytes((i * 7) % 251 for i in range(7000))
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    slices = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]

    file_hash, crcs, chunks_hash = hash_file_in_one_pass(path, chunk_size, description_chunk_size=chunk_size)

    assert crcs == [f"{zlib.crc32(s) & 0xffffffff:08x}" for s in slices]
    assert chunks_hash == hashlib.sha256(b"".join(hashlib.sha256(s).digest() for s in slices)).hexdigest()
    assert file_hash == hashlib.sha256(data).hexdigest()


# --- (5) the certificate waits are bounded and expire --------------------------------

def _manager(clock):
    sent = []

    async def _send(node_id, message):
        sent.append((node_id, message["command"], message["payload"]))

    manager = FileTransferManager.__new__(FileTransferManager)
    manager.p2p_manager = SimpleNamespace(node_id=B, send_message_to_peer=_send)
    manager.service = SimpleNamespace(pending_certificate_requests=set())
    manager.file_requests = SimpleNamespace(answer_timeout_s=10.0)
    manager._certificates_asked = {}
    manager._certificate_arrivals = {}
    manager.sent = sent
    return manager


def _made_up(i):
    return "dpc-node-" + f"{i:032x}"


@pytest.mark.asyncio
async def test_made_up_authors_cannot_grow_the_certificate_waits(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ftm, "_monotonic", lambda: clock[0])
    manager = _manager(clock)

    for i in range(CERTIFICATE_WAITS_MAX + 100):
        await manager.ask_for_certificate(_made_up(i), A)
        clock[0] += 0.001

    assert len(manager._certificates_asked) <= CERTIFICATE_WAITS_MAX
    assert len(manager._certificate_arrivals) <= CERTIFICATE_WAITS_MAX
    assert (_made_up(CERTIFICATE_WAITS_MAX + 99), A) in manager._certificates_asked, "the newest kept"

    clock[0] += 2 * 10.0  # past the answer wait, twice
    await manager.ask_for_certificate(_made_up(10 ** 6), A)
    assert list(manager._certificates_asked) == [(_made_up(10 ** 6), A)]
    assert list(manager._certificate_arrivals) == [_made_up(10 ** 6)]


@pytest.mark.asyncio
async def test_an_author_id_that_is_not_a_node_id_is_never_asked_about(monkeypatch):
    manager = _manager([0.0])

    await manager.ask_for_certificate(f"dpc-node-{'1' * 32}/../{A}", A)
    await manager.ask_for_certificate(A.upper(), A)

    assert manager.sent == []
    assert manager._certificate_arrivals == {} and manager._certificates_asked == {}


@pytest.mark.asyncio
async def test_a_wait_that_times_out_leaves_nothing_behind():
    manager = _manager([0.0])

    assert await manager.wait_for_certificate(A, 0.01) is False
    assert manager._certificate_arrivals == {}


# --- (5) W2: a certificate that came before the wait is not waited for ---------------

@pytest.mark.asyncio
async def test_a_certificate_that_beat_the_wait_is_not_waited_for():
    waited = []
    sent = []

    async def _send(node_id, message):
        sent.append((node_id, message["command"]))

    async def _wait(node_id, timeout_s):
        waited.append((node_id, timeout_s))
        await asyncio.sleep(timeout_s)
        return False

    transfer = FileTransfer.__new__(FileTransfer)
    transfer.transfer_id = "t-1"
    transfer.file_description = {"sender_node_id": A}
    transfer.file_verdict = "unverified"
    transfer.status = None
    begun = []
    manager = SimpleNamespace(
        active_transfers={"t-1": transfer},
        file_description_verdict_now=lambda node_id, t: FILE_VERIFIED,  # the certificate is here
        wait_for_certificate=_wait,
        p2p_manager=SimpleNamespace(send_message_to_peer=_send),
        begin_receiving=begun.append,
    )
    requests = FileRequests(manager)
    requests.answer_timeout_s = 30.0
    request = OpenRequest(request_id="r-1", conversation_id=GROUP, group_id=GROUP, message_id="m-1",
                          filename="notes.txt", size_bytes=1000, file_hash=None, candidates=[A, B])
    request.transfer_id = "t-1"

    await asyncio.wait_for(requests._accept_once_verified(request, A, transfer), 2.0)

    assert waited == [], "verified before the wait: nothing to wait for"
    assert sent == [(A, "FILE_ACCEPT")] and begun == ["t-1"]
    assert transfer.file_verdict == FILE_VERIFIED
