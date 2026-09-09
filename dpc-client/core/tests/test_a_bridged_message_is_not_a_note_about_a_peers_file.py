"""The note predicate must name a note, not everything this node signed for a peer.

`is_local_file_note` calls a record a note when it is signed by us and
attributed to somebody else. Two records this node writes have that shape and
are not notes about a file:

  * a message bridged in from Telegram — `telegram_coordinator` attributes it to
    `telegram-bot-<chat_id>` and `add_message` signs it with this node's key;
  * a peer's group message that arrived unsigned or under a preimage this build
    cannot recompute — `group_handler._authenticate_author` hands it on with no
    signature fields, so `add_message` signs it here and leaves the author alone.

Neither is a note about a file, and neither carries an attachment. The digest
that drops them promises less than the export can send, which is the same
disagreement between the two sides — the other way round — that
`test_the_digest_advertises_only_what_the_export_can_send` exists to prevent.
"""

from pathlib import Path

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor

ME = "dpc-node-" + "a" * 32          # what `signing_identity` signs as
PEER = "dpc-node-" + "b" * 32
CHAT = "telegram-bot-123456789"
FILE = {"type": "image", "filename": "paste.png", "size_bytes": 98889, "hash": "3aea5d86"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A `~/.dpc` of this test's own, holding this node's id and nothing else."""
    (tmp_path / ".dpc").mkdir(parents=True)
    (tmp_path / ".dpc" / "node.id").write_text(ME, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _monitor(conversation_id, participants):
    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.conversation_id = conversation_id
    monitor.participants = participants
    monitor.message_history = []
    monitor.message_ids = set()
    monitor.message_buffer = []
    monitor.full_conversation = []
    monitor._local_file_notes = set()
    monitor._history_dirty = False
    monitor._signer = None
    return monitor


def _telegram_monitor():
    """`coordinators/telegram_coordinator.py:370` — one participant, no context."""
    return _monitor("telegram-123456789",
                    [{"node_id": CHAT, "name": "Sasha"}])


def _group_monitor():
    """`knowledge_service._build_participants` for a group we are a member of."""
    return _monitor("group-b88b65076b85", [
        {"node_id": ME, "name": "User", "context": "local"},
        {"node_id": PEER, "name": "Sasha", "context": "peer"},
    ])


def test_a_bridged_telegram_message_is_still_counted(home, signing_identity):
    """Signed by us because we stored it, attributed to the bot because it wrote
    it. Nothing about it concerns a file."""
    monitor = _telegram_monitor()

    monitor.add_message("user", "when is the call?", sender_node_id=CHAT,
                        sender_name="Sasha", message_id="telegram-11",
                        timestamp="2026-09-09T09:00:00Z")

    stored = monitor.message_history[-1]
    assert stored["signer_node_id"] == ME and stored["sender_node_id"] == CHAT
    assert monitor.history_digest()["authors"].get(CHAT, {}).get("count") == 1


def test_a_peers_unsigned_group_message_is_still_counted(home, signing_identity):
    """`_authenticate_author` returns no signature fields for a legacy record,
    so this node signs it — the author stays the peer."""
    monitor = _group_monitor()

    monitor.add_message("peer", "the meeting moved to nine", sender_node_id=PEER,
                        sender_name="Sasha", message_id="grp-7",
                        timestamp="2026-09-09T09:01:00Z", signature_fields=None)

    assert monitor.history_digest()["authors"].get(PEER, {}).get("count") == 1


def test_what_the_digest_names_the_export_still_sends(home, signing_identity):
    """The invariant the note predicate exists to hold, read the other way: a
    record the export ships must be advertised, or the peer never asks."""
    monitor = _group_monitor()
    monitor.add_message("peer", "the meeting moved to nine", sender_node_id=PEER,
                        sender_name="Sasha", message_id="grp-7",
                        timestamp="2026-09-09T09:01:00Z")

    advertised = sum(a["count"] for a in monitor.history_digest()["authors"].values())
    exported = monitor.export_history()

    assert len(exported) == advertised == 1


def test_a_real_note_about_a_peers_file_is_still_dropped(home, signing_identity):
    """The original defect must stay closed: a note carries the peer's name, our
    key and the file it is about."""
    monitor = _group_monitor()
    monitor.add_message("assistant", "Received screenshot: paste.png (0.09 MB)",
                        [dict(FILE)], sender_node_id=PEER, sender_name="Sasha",
                        message_id="note-1", timestamp="2026-09-09T09:02:00Z")

    assert monitor.history_digest()["authors"] == {}
    assert monitor.export_history() == []
