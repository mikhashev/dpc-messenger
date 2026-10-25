"""Sending a very large file: one read, a bounded progress stream, one hash per group send.

Found 2026-10-20 sending a 47 GB file: the local UI websocket died with a 1011
keepalive ping timeout while the file was being prepared. Three causes sat in the
same few functions of `file_transfer_manager`:

- progress went out every 100 MB, which at disk speed is ~13 events a second;
- the file was read twice (SHA-256, then a Python loop of zlib.crc32 per 64 KB);
- the group send called `send_file` once per member, and each call hashed the whole
  file again.

Plus one wire fact: a receiver's firewall refusal carried only the word
`firewall_denied`, while the sentence behind it (which rule, which limit) stayed on
the receiver's side.
"""

import asyncio
import hashlib
import itertools
import os
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import file_transfer_manager as ftm
from dpc_client_core.managers.file_transfer_manager import FileTransferManager
from dpc_client_core.message_handlers.file_cancel_handler import FileCancelHandler
from dpc_client_core.message_handlers.file_offer_handler import FileOfferHandler
from dpc_client_core.service import CoreService

SELF = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32


def _manager(tmp_path, local_api=None, chunk_size=64 * 1024, sent=None):
    async def _send(node_id, message):
        if sent is not None:
            sent.append((node_id, message))

    manager = FileTransferManager(
        p2p_manager=SimpleNamespace(node_id=SELF, send_message_to_peer=_send),
        firewall=SimpleNamespace(rules={}),
        settings=SimpleNamespace(get=lambda section, key, default=None: default),
        local_api=local_api,
        storage_base_path=tmp_path,
    )
    manager.chunk_size = chunk_size
    manager.verify_hash = True
    return manager


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))

    def progress(self):
        return [p for n, p in self.events if n == "file_preparation_progress"]


def _random_file(tmp_path, size, name="blob.bin"):
    path = tmp_path / name
    path.write_bytes(os.urandom(size))
    return path


# --- one read, same values ---------------------------------------------------


@pytest.mark.parametrize("block_chunks", [None, 1, 3, 7])
def test_one_pass_gives_the_values_the_two_passes_gave(tmp_path, monkeypatch, block_chunks):
    """Byte-identical to today's `_compute_file_hash` + `_compute_chunk_hashes`,
    on a file whose last chunk is partial and which spans several read blocks."""
    manager = _manager(tmp_path)
    path = _random_file(tmp_path, 64 * 1024 * 9 + 12345)
    if block_chunks is not None:
        monkeypatch.setattr(ftm, "HASH_READ_BLOCK_BYTES", manager.chunk_size * block_chunks)

    old_hash = manager._compute_file_hash(path)
    old_chunks = manager._compute_chunk_hashes(path)

    new_hash, new_chunks = ftm.hash_file_in_one_pass(path, manager.chunk_size)

    assert new_hash == old_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert new_chunks == old_chunks
    assert len(new_chunks) == 10  # nine full chunks and a partial one
    assert all(len(c) == 8 for c in new_chunks)


def test_a_block_larger_than_the_file_still_slices_on_chunk_boundaries(tmp_path):
    manager = _manager(tmp_path)
    path = _random_file(tmp_path, 100)  # smaller than one chunk
    new_hash, new_chunks = ftm.hash_file_in_one_pass(path, manager.chunk_size)
    assert new_chunks == manager._compute_chunk_hashes(path)
    assert new_hash == manager._compute_file_hash(path)


def test_an_empty_file_has_no_chunks_and_the_empty_digest(tmp_path):
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    new_hash, new_chunks = ftm.hash_file_in_one_pass(path, 64 * 1024)
    assert new_hash == hashlib.sha256(b"").hexdigest()
    assert new_chunks == []


@pytest.mark.asyncio
async def test_the_fingerprint_opens_the_file_once(tmp_path, monkeypatch):
    manager = _manager(tmp_path, local_api=_Api())
    path = _random_file(tmp_path, 64 * 1024 * 3 + 5)
    opened = []
    real_open = open

    def spy(file, *args, **kwargs):
        if str(file) == str(path):
            opened.append(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(ftm, "open", spy, raising=False)

    file_hash, chunk_hashes = await manager.compute_file_fingerprint(path)

    assert len(opened) == 1
    assert file_hash == manager._compute_file_hash(path)
    assert chunk_hashes == manager._compute_chunk_hashes(path)


@pytest.mark.asyncio
async def test_without_hash_verification_the_hash_is_none_and_chunks_remain(tmp_path):
    manager = _manager(tmp_path, local_api=_Api())
    manager.verify_hash = False
    path = _random_file(tmp_path, 64 * 1024 * 2 + 1)
    file_hash, chunk_hashes = await manager.compute_file_fingerprint(path)
    assert file_hash == "none"
    assert chunk_hashes == manager._compute_chunk_hashes(path)


# --- progress by the clock ---------------------------------------------------


def _fake_clock(monkeypatch, step):
    ticks = itertools.count()
    monkeypatch.setattr(ftm, "_monotonic", lambda: next(ticks) * step)


def test_progress_is_bounded_by_time_not_by_volume(tmp_path, monkeypatch):
    """A long stream of blocks (the 47 GB case in miniature) emits per half-second."""
    path = _random_file(tmp_path, 2000 * 1024)
    monkeypatch.setattr(ftm, "HASH_READ_BLOCK_BYTES", 1024)
    _fake_clock(monkeypatch, 0.01)  # each block "takes" 10 ms: 2000 blocks = 20 s
    seen = []

    ftm.hash_file_in_one_pass(path, 1024, on_progress=lambda done, total: seen.append((done, total)))

    assert len(seen) <= 20 * 2 + 1  # at most 2 a second, plus the final one
    assert len(seen) >= 10  # and it does report
    assert seen[-1] == (2000 * 1024, 2000 * 1024)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)


def test_a_fast_file_still_gets_its_final_event(tmp_path, monkeypatch):
    path = _random_file(tmp_path, 5 * 1024)
    monkeypatch.setattr(ftm, "HASH_READ_BLOCK_BYTES", 1024)
    _fake_clock(monkeypatch, 0.0)  # the clock never moves: no interval ever elapses
    seen = []

    ftm.hash_file_in_one_pass(path, 1024, on_progress=lambda d, t: seen.append((d, t)))

    assert seen == [(5 * 1024, 5 * 1024)]


def test_the_final_event_is_not_sent_twice(tmp_path, monkeypatch):
    path = _random_file(tmp_path, 4 * 1024)
    monkeypatch.setattr(ftm, "HASH_READ_BLOCK_BYTES", 1024)
    _fake_clock(monkeypatch, 10.0)  # every block is past the interval
    seen = []

    ftm.hash_file_in_one_pass(path, 1024, on_progress=lambda d, t: seen.append(d))

    assert seen == [1024, 2048, 3072, 4096]


@pytest.mark.asyncio
async def test_the_ui_receives_a_bounded_stream_ending_at_one_hundred(tmp_path, monkeypatch):
    api = _Api()
    manager = _manager(tmp_path, local_api=api, chunk_size=1024)
    path = _random_file(tmp_path, 3000 * 1024)
    monkeypatch.setattr(ftm, "HASH_READ_BLOCK_BYTES", 1024)
    _fake_clock(monkeypatch, 0.01)  # 30 s of file time

    await manager.compute_file_fingerprint(path)
    await asyncio.sleep(0.1)  # let the thread-scheduled broadcasts run

    progress = api.progress()
    assert 10 <= len(progress) <= 30 * 2 + 1
    assert progress[-1]["percent"] == 100
    assert {p["phase"] for p in progress} == {"hashing_file"}
    names = [n for n, _ in api.events]
    assert names[0] == "file_preparation_started"
    assert names[-1] == "file_preparation_completed"


# --- one hash per group send -------------------------------------------------


def _group_stub(manager, members):
    return SimpleNamespace(
        group_manager=SimpleNamespace(get_group=lambda gid: SimpleNamespace(members=members)),
        p2p_coordinator=SimpleNamespace(get_connected_peers=lambda: set(members)),
        p2p_manager=SimpleNamespace(node_id=SELF),
        file_transfer_manager=manager,
    )


@pytest.mark.asyncio
async def test_a_group_send_hashes_the_file_once_for_all_members(tmp_path, monkeypatch):
    sent = []
    manager = _manager(tmp_path, local_api=_Api(), sent=sent)
    path = _random_file(tmp_path, 64 * 1024 * 4 + 7)
    members = [SELF] + ["dpc-node-" + c * 32 for c in "bcd"]

    calls = []
    real = ftm.hash_file_in_one_pass

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(ftm, "hash_file_in_one_pass", counting)

    result = await CoreService.send_group_file(_group_stub(manager, members), "group-x", str(path))

    assert result["status"] == "success"
    assert len(result["transfer_ids"]) == 3
    assert len(calls) == 1  # was 3: once per member
    offers = [m["payload"] for _, m in sent if m["command"] == "FILE_OFFER"]
    assert len(offers) == 3
    assert {o["hash"] for o in offers} == {hashlib.sha256(path.read_bytes()).hexdigest()}
    assert all(o["chunk_hashes"] == manager._compute_chunk_hashes(path) for o in offers)
    assert all(o["group_id"] == "group-x" for o in offers)


@pytest.mark.asyncio
async def test_a_direct_send_still_hashes_inside_send_file(tmp_path, monkeypatch):
    """Existing callers pass no precomputed pair and must get the same offer as before."""
    sent = []
    manager = _manager(tmp_path, local_api=_Api(), sent=sent)
    path = _random_file(tmp_path, 64 * 1024 * 2 + 3)

    await manager.send_file(PEER, path)

    offer = sent[0][1]["payload"]
    assert offer["hash"] == manager._compute_file_hash(path)
    assert offer["chunk_hashes"] == manager._compute_chunk_hashes(path)


@pytest.mark.asyncio
async def test_a_precomputed_pair_that_does_not_fit_the_file_is_not_trusted(tmp_path):
    sent = []
    manager = _manager(tmp_path, local_api=_Api(), sent=sent)
    path = _random_file(tmp_path, 64 * 1024 * 2 + 3)

    await manager.send_file(PEER, path, file_hash="deadbeef", chunk_hashes=["00000000"])

    offer = sent[0][1]["payload"]
    assert offer["hash"] == manager._compute_file_hash(path)
    assert len(offer["chunk_hashes"]) == 3


# --- the refusal carries its sentence ----------------------------------------


def _offer_handler(tmp_path, rules, sent):
    manager = _manager(tmp_path, sent=None)
    manager.firewall = SimpleNamespace(rules=rules, _get_groups_for_node=lambda n: [])

    async def _send(node_id, message):
        sent.append(message)

    service = SimpleNamespace(
        file_transfer_manager=manager,
        p2p_manager=SimpleNamespace(send_message_to_peer=_send),
        peer_metadata={},
        local_api=_Api(),
    )
    return FileOfferHandler(service)


def _offer():
    return {
        "transfer_id": "t1", "filename": "x.bin", "size_bytes": 10,
        "hash": "none", "mime_type": "application/octet-stream", "chunk_size": 65536,
    }


@pytest.mark.asyncio
async def test_a_firewall_refusal_carries_the_receivers_sentence(tmp_path):
    sent = []
    # A deny entry: a sender no rule names is asked, not refused (2026-10-25)
    rules = {"file_transfer": {"nodes": {PEER: {"file_transfer.allow": "deny"}}, "groups": {}}}
    handler = _offer_handler(tmp_path, rules, sent)

    await handler.handle(PEER, _offer())

    assert [m["command"] for m in sent] == ["FILE_CANCEL"]
    assert sent[0]["payload"]["reason"] == "firewall_denied"
    assert sent[0]["payload"]["denial_reason"] == "File transfer not permitted by firewall rules"


@pytest.mark.asyncio
async def test_a_size_refusal_names_the_limit(tmp_path):
    sent = []
    rules = {"file_transfer": {"nodes": {PEER: {
        "file_transfer.allow": "allow", "file_transfer.max_size_mb": 1}}}}
    handler = _offer_handler(tmp_path, rules, sent)
    offer = _offer()
    offer["size_bytes"] = 5 * 1024 * 1024

    await handler.handle(PEER, offer)

    assert "exceeds 1 MB limit" in sent[0]["payload"]["denial_reason"]


def _cancel_handler(events, transfer=None):
    async def _handle_file_cancel(node_id, payload):
        return None

    async def _broadcast(name, payload):
        events.append((name, payload))

    manager = SimpleNamespace(
        active_transfers={"t1": transfer} if transfer else {},
        handle_file_cancel=_handle_file_cancel,
    )
    service = SimpleNamespace(
        file_transfer_manager=manager,
        local_api=SimpleNamespace(broadcast_event=_broadcast),
    )
    return FileCancelHandler(service)


_UPLOAD = SimpleNamespace(filename="big.iso", direction="upload", group_id="group-x")


@pytest.mark.asyncio
async def test_the_sender_ui_event_carries_the_denial_reason_and_the_group():
    events = []
    handler = _cancel_handler(events, _UPLOAD)

    await handler.handle(PEER, {
        "transfer_id": "t1", "reason": "firewall_denied",
        "denial_reason": "File too large: 900.0 MB exceeds 100 MB limit",
    })

    name, event = events[0]
    assert name == "file_transfer_cancelled"
    assert event["reason"] == "firewall_denied"
    assert event["denial_reason"] == "File too large: 900.0 MB exceeds 100 MB limit"
    assert event["group_id"] == "group-x"
    assert event["node_id"] == PEER


@pytest.mark.asyncio
async def test_a_cancel_from_an_older_peer_without_the_field_behaves_as_before():
    events = []
    handler = _cancel_handler(events, SimpleNamespace(filename="a", direction="upload", group_id=None))

    await handler.handle(PEER, {"transfer_id": "t1", "reason": "firewall_denied"})

    _, event = events[0]
    assert event["reason"] == "firewall_denied"
    assert "denial_reason" not in event
    assert "group_id" not in event


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "", "   ", 42, ["x"], {"a": 1}])
async def test_a_denial_reason_that_is_not_text_is_dropped(bad):
    events = []
    handler = _cancel_handler(events)

    await handler.handle(PEER, {"transfer_id": "t1", "reason": "firewall_denied", "denial_reason": bad})

    assert "denial_reason" not in events[0][1]


@pytest.mark.asyncio
async def test_a_denial_reason_is_bounded_before_it_reaches_the_screen():
    events = []
    handler = _cancel_handler(events)

    await handler.handle(PEER, {"transfer_id": "t1", "reason": "firewall_denied", "denial_reason": "x" * 5000})

    assert len(events[0][1]["denial_reason"]) == FileCancelHandler.MAX_DENIAL_REASON_CHARS
