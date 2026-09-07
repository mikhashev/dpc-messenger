"""A note this node wrote about a peer's file must not be sent to that peer.

The note is attributed to the peer and signed by us, which is the one shape
every other node refuses on arrival. Exporting it can only produce a rejection,
and the cleanup that should have removed it was gated on the sender's record
being new — so a note written after that record first arrived had no way out,
and one group re-synced twenty-eight times a day merging nothing.
"""

import io
import json
from types import SimpleNamespace

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor

ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
FILE = {"type": "image", "filename": "paste.png", "size_bytes": 98889, "hash": "3aea5d86"}


def _monitor(tmp_path):
    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.message_history = []
    monitor.message_ids = set()
    monitor._local_file_notes = set()
    monitor.last_merge_rejected = []
    monitor._history_dirty = False
    monitor._local_node_id = lambda: ME
    return monitor


def _note():
    """What this node writes when a peer's file lands: theirs by name, ours by key."""
    return {
        "id": "note-1", "role": "assistant",
        "content": "Received screenshot: paste.png (0.09 MB)",
        "sender_node_id": PEER, "signer_node_id": ME,
        "attachments": [FILE], "timestamp": "2026-09-03T13:46:09Z",
    }


def _their_record():
    return {
        "id": "theirs-1", "role": "user",
        "content": "Sent screenshot: paste.png (0.09 MB)",
        "sender_node_id": PEER, "signer_node_id": PEER,
        "attachments": [FILE], "timestamp": "2026-09-03T13:46:09Z",
    }


def _mine():
    return {
        "id": "mine-1", "role": "user", "content": "hello",
        "sender_node_id": ME, "signer_node_id": ME,
        "timestamp": "2026-09-03T13:47:00Z",
    }


# --------------------------------------------------------------------------
# The export
# --------------------------------------------------------------------------

def test_the_note_is_held_back_from_the_export(tmp_path):
    monitor = _monitor(tmp_path)
    monitor.message_history = [_mine(), _note(), _their_record()]

    exported = ConversationMonitor.export_history(monitor)

    assert [m["id"] for m in exported] == ["mine-1", "theirs-1"]


def test_everything_else_still_travels(tmp_path):
    monitor = _monitor(tmp_path)
    monitor.message_history = [_mine(), _their_record()]

    exported = ConversationMonitor.export_history(monitor)

    assert len(exported) == 2


def test_the_shape_decides_not_the_remembered_ids(tmp_path):
    """That set lives in memory and is empty after a restart."""
    monitor = _monitor(tmp_path)
    monitor._local_file_notes = set()          # as it is on every fresh process
    monitor.message_history = [_note()]

    assert ConversationMonitor.export_history(monitor) == []


def test_a_peers_own_record_is_never_mistaken_for_a_note(tmp_path):
    monitor = _monitor(tmp_path)
    monitor.message_history = [_their_record()]

    assert len(ConversationMonitor.export_history(monitor)) == 1


# --------------------------------------------------------------------------
# The cleanup, which used to get exactly one chance
# --------------------------------------------------------------------------

def test_the_note_is_dropped_when_the_senders_record_is_present(tmp_path):
    monitor = _monitor(tmp_path)
    monitor.message_history = [_mine(), _note()]
    monitor.restore_chronological_order = lambda: None

    assert ConversationMonitor._drop_local_file_note(monitor, _their_record()) is True
    assert [m["id"] for m in monitor.message_history] == ["mine-1"]


def test_a_record_about_another_file_drops_nothing(tmp_path):
    monitor = _monitor(tmp_path)
    monitor.message_history = [_note()]
    monitor.restore_chronological_order = lambda: None

    other = dict(_their_record(),
                 attachments=[{"filename": "other.png", "size_bytes": 1, "hash": "zz"}])

    assert ConversationMonitor._drop_local_file_note(monitor, other) is False
    assert len(monitor.message_history) == 1


def test_the_cleanup_is_not_gated_on_the_record_being_new():
    """It had one chance — the record's first arrival — and a note written
    after that could never be removed."""
    import inspect

    source = inspect.getsource(ConversationMonitor.merge_history)
    call = source.index("_drop_local_file_note(checked)")
    guard = source.rindex("if checked.get(\"attachments\")", 0, call)

    assert "not in self.message_ids" not in source[guard:call]
