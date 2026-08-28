"""A file note is a message, and it was written without a time or an author.

Found 2026-08-07 from three screenshots: Mike reported the group history "broke"
when a third node connected. It had not. Two records in that group — "Received
file: history.json" and "Sent file: history.json" — carried `timestamp: null`
and `sender_node_id: null`, and had since they were written. Across every
conversation on that machine: 3 records of 642 with no timestamp, 6 with no
sender.

`add_message` stores a field only when it is passed and defaults nothing, and
the file-note callers passed neither. Everything downstream then had nothing to
work with:

- the UI invents a time for an undated record — `Date.now()` minus a second per
  row — so it sorts below every real message, at a position that changes on
  every reload. `cb5dee81` made the list reload on every sync, which turned a
  hidden defect into a visible, repeating one;
- with no `sender_node_id` the same mapper renders the row as "You", so a file
  *received* from a peer was shown as the reader's own on all three nodes;
- with no author the record joins the per-author digest under `""`, and since
  each node has its own transfer notes, that author differs forever.

The default in `add_message` is the half that matters longest: the next caller
that forgets should not be able to produce an unorderable record at all.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor

PARTICIPANTS = [{"node_id": "n1", "name": "User", "context": "local"}]
US = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
GROUP = "group-1234567890ab"


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ConversationMonitor, "persist_history", property(lambda self: False)
    )
    m = ConversationMonitor(
        conversation_id=GROUP, participants=PARTICIPANTS, llm_manager=None
    )
    m._get_history_path = lambda: tmp_path / GROUP / "history.json"
    return m


# --- the backstop: no caller can produce an undated record ------------------


def test_a_message_added_without_a_time_still_has_one(monitor):
    monitor.add_message("assistant", "Received file: x.bin (0.1 MB)")

    stamped = monitor.message_history[0].get("timestamp")
    assert stamped, "a record with no timestamp cannot be ordered by anything"
    parsed = datetime.fromisoformat(stamped.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, "an ambiguous instant is barely better than none"


def test_a_supplied_time_is_left_alone(monitor):
    """The default fills a gap; it must never overwrite what the caller knows.

    A peer's message carries the author's own timestamp, and that timestamp is
    covered by the signature — replacing it would make an untouched message
    verify as tampered.
    """
    theirs = "2026-08-05T17:25:57.047755+00:00"
    monitor.add_message("user", "hi", timestamp=theirs, sender_node_id=PEER)

    assert monitor.message_history[0]["timestamp"] == theirs


def test_the_default_time_orders_after_what_came_before(monitor):
    monitor.add_message("user", "first", timestamp="2020-01-01T00:00:00+00:00")
    monitor.add_message("assistant", "file note with no time")

    first, second = monitor.message_history
    assert first["timestamp"] < second["timestamp"]


# --- the file notes themselves ----------------------------------------------


@pytest.mark.asyncio
async def test_a_received_file_is_attributed_to_the_peer_that_sent_it(tmp_path, monitor):
    """It was rendered as "You" on the node that received it, which is backwards."""
    manager = _manager(tmp_path, monitor)
    await manager._finalize_download(PEER, _transfer())

    note = monitor.message_history[-1]
    assert note["sender_node_id"] == PEER
    assert note["timestamp"]
    assert "Received file" in note["content"]


@pytest.mark.asyncio
async def test_a_received_file_note_is_not_ours(tmp_path, monitor):
    """The regression half: stamping our own id would look fixed and read wrong."""
    manager = _manager(tmp_path, monitor)
    await manager._finalize_download(PEER, _transfer())

    assert monitor.message_history[-1]["sender_node_id"] != US


# --- the digest, which is where a missing author did the quiet damage -------


def test_two_notes_from_different_peers_are_two_authors(monitor):
    """Undated, unattributed notes all collapsed into the author "", whose
    contents differ on every node — an author that can never agree."""
    monitor.add_message("assistant", "Received file: a.bin", sender_node_id=PEER)
    monitor.add_message("user", "Sent file: b.bin", sender_node_id=US)

    authors = monitor.history_digest()["authors"]
    assert set(authors) == {PEER, US}
    assert "" not in authors


# --- helpers ---------------------------------------------------------------


def _transfer():
    from dpc_client_core.managers.file_transfer_manager import (
        FileTransfer,
        TransferStatus,
    )

    t = FileTransfer(
        transfer_id="t1",
        filename="history.json",
        size_bytes=5,
        hash="none",
        mime_type="application/json",
        chunk_size=64,
        node_id=PEER,
        direction="download",
        status=TransferStatus.TRANSFERRING,
        chunks_received={0},
        total_chunks=1,
    )
    t.chunk_data = {0: b"hello"}
    return t


def _manager(tmp_path, monitor):
    from dpc_client_core.managers.file_transfer_manager import FileTransferManager

    service = SimpleNamespace(
        _get_or_create_conversation_monitor=lambda cid: monitor,
        peer_metadata={PEER: {"name": "Mike (linux)"}},
    )
    manager = FileTransferManager(
        p2p_manager=SimpleNamespace(node_id=US, send_message_to_peer=_noop),
        firewall=SimpleNamespace(rules={}),
        settings=SimpleNamespace(get=lambda s, k, d=None: d),
        storage_base_path=tmp_path,
        service=service,
    )
    manager.verify_hash = False
    manager.local_api = SimpleNamespace(broadcast_event=_noop)
    manager.active_transfers["t1"] = None
    return manager


async def _noop(*args, **kwargs):
    return None
