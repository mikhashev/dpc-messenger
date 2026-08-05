"""The author signs; everyone else checks and keeps what they checked.

Three things have to hold together, and any one of them alone is useless:

  - the signature is made at the author and rides the wire;
  - the receiver takes the author from that signature, not from the socket it
    arrived on — which is what makes a relayed message keep its author;
  - the receiver stores the signature it verified instead of stamping its own,
    or verification is undone by the very act of saving.

The third is the one that looks redundant and is not: `add_message` used to
sign every message it stored with the local key, so a checked signature was
replaced by the checker's own on its way to disk.
"""

import pytest

from dpc_protocol.message_signing import message_content_hash
from dpc_client_core.conversation_monitor import ConversationMonitor, Message


GROUP = "group-1234"
AUTHOR = "dpc-node-6d218e95dee9cfeebfc3caa705ae8c95"
RELAY = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"


def _monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(ConversationMonitor, "_get_signer", lambda self: None)
    return ConversationMonitor(
        conversation_id=GROUP,
        participants=[{"node_id": RELAY, "name": "self", "context": "local"}],
        llm_manager=None,
    )


def _fields(content="hello", timestamp="2026-08-06T00:00:00+00:00"):
    """What an author would put on the wire beside the message."""
    return {
        "content_hash": message_content_hash(
            conversation_id=GROUP,
            message_id="m-1",
            sender_node_id=AUTHOR,
            sender_name="Mike (linux)",
            sender_type="human",
            agent_owner=None,
            timestamp=timestamp,
            content=content,
            tool_calls=None,
        ),
        "signature": "not-checked-here",
        "signer_node_id": AUTHOR,
    }


def test_the_authors_signature_is_stored_not_replaced(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    supplied = _fields()

    monitor.add_message(
        "peer", "hello", timestamp="2026-08-06T00:00:00+00:00",
        sender_node_id=AUTHOR, sender_name="Mike (linux)", message_id="m-1",
        sender_type="human", signature_fields=supplied,
    )

    stored = monitor.message_history[-1]
    assert stored["signature"] == supplied["signature"]
    assert stored["signer_node_id"] == AUTHOR
    assert stored["content_hash"] == supplied["content_hash"]


def test_without_supplied_fields_the_local_key_still_signs(tmp_path, monkeypatch):
    """Our own messages are still signed here — that path is unchanged."""
    monitor = _monitor(tmp_path, monkeypatch)

    monitor.add_message("user", "mine", message_id="m-2", sender_node_id=RELAY)

    stored = monitor.message_history[-1]
    assert "signature" not in stored          # no signer available in this test
    assert stored["content_hash"]             # but the hash is still computed


def test_a_conv_message_carries_the_fields_through(tmp_path, monkeypatch):
    """on_message is the only door group messages come through."""
    import asyncio

    monitor = _monitor(tmp_path, monkeypatch)
    supplied = _fields()
    msg = Message(
        message_id="m-1", conversation_id=GROUP, sender_node_id=AUTHOR,
        sender_name="Mike (linux)", text="hello",
        timestamp="2026-08-06T00:00:00+00:00", sender_type="human",
        signature_fields=supplied,
    )

    asyncio.run(monitor.on_message(msg))

    assert monitor.message_history[-1]["signer_node_id"] == AUTHOR


def test_the_hash_binds_the_text(tmp_path, monkeypatch):
    """The point of all of it: altered text no longer matches the hash."""
    honest = _fields(content="transfer approved")
    tampered = message_content_hash(
        conversation_id=GROUP, message_id="m-1", sender_node_id=AUTHOR,
        sender_name="Mike (linux)", sender_type="human", agent_owner=None,
        timestamp="2026-08-06T00:00:00+00:00", content="transfer denied",
        tool_calls=None,
    )

    assert honest["content_hash"] != tampered


def test_the_hash_binds_the_room(tmp_path, monkeypatch):
    """A signed message from one room must not verify inside another."""
    here = _fields()["content_hash"]
    elsewhere = message_content_hash(
        conversation_id="group-other", message_id="m-1", sender_node_id=AUTHOR,
        sender_name="Mike (linux)", sender_type="human", agent_owner=None,
        timestamp="2026-08-06T00:00:00+00:00", content="hello", tool_calls=None,
    )

    assert here != elsewhere
