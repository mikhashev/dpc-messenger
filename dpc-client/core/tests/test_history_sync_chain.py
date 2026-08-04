"""History that crosses the wire must carry its own integrity, not borrow ours.

`export_history` dropped `msg_index` and `chain_hash`. The receiver's loader
mints a hash for any message that has none and then logs "chain integrity
verified" — so a chain whose entire purpose is detecting tampering in transit
was re-blessing whatever came off the wire, and the two nodes ended up holding
different hashes for the same messages.
"""

import logging

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor

PARTICIPANTS = [{"node_id": "n1", "name": "User", "context": "local"}]


@pytest.fixture(autouse=True)
def _always_persist(monkeypatch):
    """persist_history is a property backed by an on-disk settings file.

    Patched per-test rather than on the class, so nothing leaks into the rest
    of the suite. The subject here is the chain, not the settings plumbing.
    """
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))


def _monitor(tmp_path, cid="group-test"):
    m = ConversationMonitor(conversation_id=cid, participants=PARTICIPANTS, llm_manager=None)
    m._get_history_path = lambda: tmp_path / cid / "history.json"
    return m


def _sender(tmp_path):
    m = _monitor(tmp_path, "group-src")
    for text in ("first", "second", "third"):
        m.add_message(role="user", content=text, sender_node_id="n1", sender_name="Mike")
    return m


def test_export_carries_the_integrity_pair(tmp_path):
    exported = _sender(tmp_path).export_history()

    assert exported, "nothing to assert on"
    for msg in exported:
        assert "msg_index" in msg, "index is part of the hash input; without it nothing verifies"
        assert msg.get("chain_hash"), "the hash must travel with the message"


def test_import_preserves_the_origin_chain(tmp_path):
    exported = _sender(tmp_path).export_history()

    receiver = _monitor(tmp_path, "group-dst")
    receiver.import_history(exported)

    assert [m["msg_index"] for m in receiver.message_history] == [m["msg_index"] for m in exported]
    assert [m["chain_hash"] for m in receiver.message_history] == [m["chain_hash"] for m in exported]


def test_loader_does_not_report_a_minted_hash_as_verified(tmp_path, caplog):
    """The defect verbatim: a message with no hash used to become 'verified'."""
    sender = _sender(tmp_path)
    stripped = [
        {k: v for k, v in m.items() if k not in ("chain_hash", "msg_index")}
        for m in sender.export_history()
    ]

    receiver = _monitor(tmp_path, "group-legacy")
    receiver.import_history(stripped)
    receiver.save_history()

    with caplog.at_level(logging.INFO):
        _monitor(tmp_path, "group-legacy").load_history()

    text = caplog.text
    assert "minted a hash" in text, "silently inventing a hash is what hid the defect"
    assert "unverified, not verified" in text


def test_a_tampered_message_is_reported_after_a_sync(tmp_path, caplog):
    """With the pair carried, the existing check finally runs on synced data."""
    exported = _sender(tmp_path).export_history()
    exported[1]["content"] = "tampered in transit"

    receiver = _monitor(tmp_path, "group-tampered")
    receiver.import_history(exported)
    receiver.save_history()

    with caplog.at_level(logging.INFO):
        _monitor(tmp_path, "group-tampered").load_history()

    assert "Chain broken" in caplog.text
