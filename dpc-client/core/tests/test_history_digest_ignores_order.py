"""Two nodes holding the same messages must agree they do.

The old comparison hashed the last `chain_hash`, which covers `msg_index`,
`prev_hash` and `role`. The first two depend on arrival order; `role` is per
reader by construction — each node marks its own messages `user` and everyone
else's `peer`. Measured 2026-08-06 across three nodes holding an identical nine
messages: same ids, same order, same indices, same content, same timestamps,
and three different chain tips. So the alarm did not fire on a race — it could
never stop firing.

A digest over `content_hash` inherits the signing preimage's field set, which
excludes `role` on purpose (ADR-031), and sorting removes order.
"""

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor


ALICE = "dpc-node-6d218e95dee9cfeebfc3caa705ae8c95"
BOB = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
PARTICIPANTS = [{"node_id": BOB, "name": "self", "context": "local"}]


def _monitor(cid="group-1234"):
    return ConversationMonitor(conversation_id=cid, participants=PARTICIPANTS, llm_manager=None)


def _fill(monitor, order):
    for role, author, name, text in order:
        monitor.add_message(role=role, content=text, sender_node_id=author,
                            sender_name=name, message_id=f"m-{text}",
                            timestamp="2026-08-06T00:00:00+00:00")
    return monitor


CONVERSATION = [
    ("user", BOB, "Mike Windows PC", "first"),
    ("peer", ALICE, "Mike (linux)", "second"),
    ("user", BOB, "Mike Windows PC", "third"),
]


def test_the_same_messages_in_a_different_order_agree():
    one = _fill(_monitor(), CONVERSATION)
    other = _fill(_monitor(), list(reversed(CONVERSATION)))

    assert one.history_digest() == other.history_digest()


def test_the_same_messages_read_from_different_sides_agree():
    """The measured case: what differs is `role`, and only `role`."""
    mine = _fill(_monitor(), CONVERSATION)
    theirs = _fill(_monitor(), [
        ("peer", BOB, "Mike Windows PC", "first"),
        ("user", ALICE, "Mike (linux)", "second"),
        ("peer", BOB, "Mike Windows PC", "third"),
    ])

    assert mine.history_digest() == theirs.history_digest()


def test_a_missing_message_shows_up_under_its_author():
    full = _fill(_monitor(), CONVERSATION)
    partial = _fill(_monitor(), CONVERSATION[:2])

    a, b = full.history_digest(), partial.history_digest()

    assert a != b
    assert a["authors"][ALICE] == b["authors"][ALICE], "the untouched author must match"
    assert a["authors"][BOB] != b["authors"][BOB], "the gap must be attributable"


def test_altered_content_changes_the_digest():
    honest = _fill(_monitor(), CONVERSATION)
    tampered = _fill(_monitor(), [
        ("user", BOB, "Mike Windows PC", "first"),
        ("peer", ALICE, "Mike (linux)", "SECOND"),
        ("user", BOB, "Mike Windows PC", "third"),
    ])

    assert honest.history_digest() != tampered.history_digest()


def test_an_empty_history_has_a_stable_empty_digest():
    assert _monitor().history_digest() == {"authors": {}, "digest": "sha256:empty"}


def test_messages_without_a_content_hash_still_participate():
    """Legacy records must not silently vanish from the comparison."""
    monitor = _monitor()
    monitor.message_history.append({"id": "old-1", "content": "before all this",
                                    "sender_node_id": ALICE})

    digest = monitor.history_digest()

    assert ALICE in digest["authors"]
    assert digest["authors"][ALICE]["count"] == 1
