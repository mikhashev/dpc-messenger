"""The two barriers on the receiving side of a file transfer, neither of which held.

Found 2026-08-06 while checking @Ark's file-name-collision analysis against the
code, and both are in the same few lines:

- `_check_file_transfer_permission` returns `(allowed, error_message)`, and the
  one caller tested the tuple itself. A non-empty tuple is always truthy, so the
  deny branch was unreachable: no allow-list, no size cap, no MIME filter ever
  refused an incoming file.
- the only handling of a peer-supplied filename replaced `/` and nothing else.
  On Windows `..\\..\\x` still traverses, and an absolute path replaces the
  storage directory outright, because `Path("base") / "C:\\\\x"` is `C:\\x`.

The two compose: a peer that declares `group_id` (or voice/image metadata) is
auto-accepted with no dialog, so neither the firewall nor the user stood between
the wire and an arbitrary path.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.managers.file_transfer_manager import (
    FileTransfer,
    FileTransferManager,
    TransferStatus,
)
from dpc_client_core.message_handlers.file_offer_handler import FileOfferHandler

PEER = "dpc-node-" + "b" * 32


def _firewall(tmp_path, rules):
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    return ContextFirewall(path)


def _settings():
    return SimpleNamespace(get=lambda section, key, default=None: default)


def _manager(tmp_path, rules):
    """A real FileTransferManager over a real ContextFirewall.

    The firewall is the object whose answer the fix depends on, so it is not
    faked: a double shaped like the call site would agree with the call site.
    """
    return FileTransferManager(
        p2p_manager=SimpleNamespace(node_id="dpc-node-" + "a" * 32),
        firewall=_firewall(tmp_path, rules),
        settings=_settings(),
        storage_base_path=tmp_path,
    )


DENY_ALL = {"file_transfer": {"nodes": {}, "groups": {}}}
ALLOW_PEER = {
    "file_transfer": {
        "nodes": {PEER: {"file_transfer.allow": "allow", "file_transfer.max_size_mb": 1}}
    }
}


# --- barrier 1: the firewall answer must be read, not just received ----------


@pytest.mark.asyncio
async def test_the_permission_answer_is_a_pair_and_says_no(tmp_path):
    """Guards the shape the caller depends on: truthiness is not the answer."""
    manager = _manager(tmp_path, DENY_ALL)
    allowed, error = await manager._check_file_transfer_permission(PEER, "x.txt", 10, "text/plain")
    assert allowed is False
    assert error


@pytest.mark.asyncio
async def test_an_unlisted_peer_is_refused(tmp_path):
    sent = []
    handler = _offer_handler(tmp_path, DENY_ALL, sent)

    await handler.handle(PEER, _offer(filename="x.txt"))

    assert [m["command"] for m in sent] == ["FILE_CANCEL"]
    assert sent[0]["payload"]["reason"] == "firewall_denied"


@pytest.mark.asyncio
async def test_a_file_over_the_size_limit_is_refused(tmp_path):
    sent = []
    handler = _offer_handler(tmp_path, ALLOW_PEER, sent)

    await handler.handle(PEER, _offer(filename="x.txt", size_bytes=5 * 1024 * 1024))

    assert [m["command"] for m in sent] == ["FILE_CANCEL"]


@pytest.mark.asyncio
async def test_a_permitted_peer_still_gets_through(tmp_path):
    """The regression half: refusing everything would also pass the tests above."""
    sent = []
    handler = _offer_handler(tmp_path, ALLOW_PEER, sent)

    await handler.handle(PEER, _offer(filename="x.txt"))

    assert "FILE_CANCEL" not in [m["command"] for m in sent]


# --- barrier 2: a filename is a name, never a path --------------------------


@pytest.mark.parametrize(
    "filename",
    [
        r"..\..\..\evil.txt",
        "../../../evil.txt",
        r"C:\Windows\Temp\evil.txt",
        "/etc/cron.d/evil",
        r"sub\dir\evil.txt",
    ],
)
def test_a_sent_name_cannot_leave_the_peer_directory(tmp_path, filename):
    manager = _manager(tmp_path, ALLOW_PEER)
    storage = manager._get_peer_storage_path(PEER)

    resolved = (storage / manager._safe_incoming_name(filename)).resolve()

    assert resolved.parent == storage.resolve()
    assert resolved.name


def test_an_ordinary_name_survives_intact(tmp_path):
    manager = _manager(tmp_path, ALLOW_PEER)
    assert manager._safe_incoming_name("voice_2026-01-04.webm") == "voice_2026-01-04.webm"


def test_a_name_that_sanitises_to_nothing_still_yields_a_file(tmp_path):
    """`..`, `.` and an empty string must not produce a directory write."""
    manager = _manager(tmp_path, ALLOW_PEER)
    storage = manager._get_peer_storage_path(PEER)
    for filename in ("..", ".", "", r"..\\", "/"):
        resolved = (storage / manager._safe_incoming_name(filename)).resolve()
        assert resolved.parent == storage.resolve()
        assert resolved.name not in ("", ".", "..")


@pytest.mark.asyncio
async def test_the_written_file_lands_in_the_peer_directory(tmp_path):
    """End to end through `_finalize_download`, because the name is used there."""
    manager = _manager(tmp_path, ALLOW_PEER)
    manager.verify_hash = False
    sent = []
    manager.p2p_manager = SimpleNamespace(
        node_id="dpc-node-" + "a" * 32,
        send_message_to_peer=_collect(sent),
    )

    transfer = FileTransfer(
        transfer_id="t1",
        filename=r"..\..\..\stolen.txt",
        size_bytes=5,
        hash="none",
        mime_type="text/plain",
        chunk_size=64,
        node_id=PEER,
        direction="download",
        status=TransferStatus.TRANSFERRING,
        chunks_received={0},
        total_chunks=1,
    )
    transfer.chunk_data = {0: b"hello"}
    manager.active_transfers[transfer.transfer_id] = transfer

    await manager._finalize_download(PEER, transfer)

    storage = manager._get_peer_storage_path(PEER)
    written = [p for p in storage.iterdir() if p.is_file()]
    assert [p.name for p in written] == ["stolen.txt"]
    assert not (tmp_path / "stolen.txt").exists()


# --- helpers ---------------------------------------------------------------


def _offer(filename, size_bytes=1024):
    return {
        "transfer_id": "t1",
        "filename": filename,
        "size_bytes": size_bytes,
        "hash": "none",
        "mime_type": "text/plain",
        "chunk_size": 65536,
    }


def _offer_handler(tmp_path, rules, sent):
    manager = _manager(tmp_path, rules)
    service = SimpleNamespace(
        file_transfer_manager=manager,
        p2p_manager=SimpleNamespace(send_message_to_peer=_collect(sent)),
        peer_metadata={},
        local_api=SimpleNamespace(broadcast_event=_noop),
    )
    manager.p2p_manager = service.p2p_manager
    return FileOfferHandler(service)


def _collect(sink):
    async def _send(node_id, message):
        sink.append(message)
    return _send


async def _noop(*args, **kwargs):
    return None
