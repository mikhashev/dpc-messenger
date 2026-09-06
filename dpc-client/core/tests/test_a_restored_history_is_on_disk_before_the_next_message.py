"""A restored history reaches the disk with the restore, not with the next message.

Observed on the Linux node, 2026-09-06 20:52:37 local: CHAT_HISTORY_RESPONSE
carried 41 records, the log said "Chat history restored: 41 messages", and the
conversation folder held only `metadata.json`. `import_history` replaced the
three in-memory buffers and stopped; the handler broadcast `history_restored`
and stopped. The file appeared only when an unrelated `add_message` later saved
42. A backend restart in between would have lost all 41.

`merge_history`, the group path, has saved at its end since v0.20.0. The 1:1
path now does the same.
"""

import json

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor

PARTICIPANTS = [{"node_id": "n1", "name": "User", "context": "local"}]


@pytest.fixture(autouse=True)
def _sign_with_a_test_key(signing_identity):
    """Signed records, so the import takes the path a live sync takes."""


@pytest.fixture(autouse=True)
def _always_persist(monkeypatch):
    """persist_history is a property backed by an on-disk settings file."""
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))


CID = "group-src"  # the same room on both sides: the content hash is bound to it


def _monitor(root, cid=CID):
    m = ConversationMonitor(conversation_id=cid, participants=PARTICIPANTS, llm_manager=None)
    m._get_history_path = lambda: root / cid / "history.json"
    return m


def _exported(tmp_path):
    sender = _monitor(tmp_path / "sender")
    author = sender._get_signer().node_id
    for text in ("first", "second", "third"):
        sender.add_message(role="user", content=text, sender_node_id=author, sender_name="Mike")
    return sender.export_history()


def test_a_restored_history_is_on_disk_before_the_next_message(tmp_path):
    exported = _exported(tmp_path)
    receiver = _monitor(tmp_path / "receiver")
    path = receiver._get_history_path()
    assert not path.exists(), "the folder starts empty, as it did on the node"

    receiver.import_history(exported)

    assert path.exists(), "the restore itself must write the file"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["message_count"] == len(exported) == 3
    assert [m["id"] for m in on_disk["messages"]] == [m["id"] for m in exported]
    assert receiver._history_dirty is False


def test_a_new_monitor_over_the_same_folder_loads_the_restored_history(tmp_path):
    """What a backend restart between the restore and the next message sees."""
    exported = _exported(tmp_path)
    _monitor(tmp_path / "receiver").import_history(exported)

    fresh = _monitor(tmp_path / "receiver")
    assert fresh.load_history() is True
    assert [m["id"] for m in fresh.message_history] == [m["id"] for m in exported]
    assert [m["content"] for m in fresh.message_history] == ["first", "second", "third"]
