"""The chain describes this node's copy, so it does not travel.

This supersedes the decision in `d40eea6d`, which made `msg_index` and
`chain_hash` cross the wire so tampering in transit could be caught. Two things
have changed since, and both are measured rather than argued:

  - the chain cannot agree between nodes at all. `chain_hash` covers
    `msg_index`, `prev_hash` and `role`; the first two follow arrival order and
    `role` is a rendering — each node calls its own messages `user` and
    everyone else's `peer`. Across three nodes holding an identical nine
    messages (2026-08-06) all three tips differed. Carrying it produced a
    permanent "Chain broken" on every load after any sync;

  - transit tampering is now caught by the author's signature (ADR-036), which
    is keyed to the author and computed over a field set every reader agrees
    on. That is strictly the better instrument for the job the chain was
    borrowed for.

So the chain returns to what it can actually do: detect a local file edited
underneath us. The test that mattered most from the old set survives unchanged
— a hash this node invented must never be reported as verified.
"""

import logging

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor

PARTICIPANTS = [{"node_id": "n1", "name": "User", "context": "local"}]


@pytest.fixture(autouse=True)
def _sign_with_a_test_key(signing_identity):
    """Every test here is about signed history, so all of them need a key.

    Without it the suite was machine-dependent: green on a box with
    ~/.dpc/node.key, and on a runner without one it asserted nothing, because
    an unsigned record used to skip verification entirely.
    """


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


def _sender(tmp_path, cid="group-src"):
    """An author signing its own messages, which is the only case that occurs.

    sender_node_id has to be the signing node's: a record claiming one author
    and signed by another is exactly what a verifier must refuse, and there is
    a test for that elsewhere.
    """
    m = _monitor(tmp_path, cid)
    author = m._get_signer().node_id
    for text in ("first", "second", "third"):
        m.add_message(role="user", content=text, sender_node_id=author, sender_name="Mike")
    return m


def test_export_does_not_carry_the_local_chain(tmp_path):
    """Sending it invited the receiver to adopt a sequence that is not its own."""
    exported = _sender(tmp_path).export_history()

    assert exported, "nothing to assert on"
    for msg in exported:
        assert "chain_hash" not in msg
        assert "msg_index" not in msg


def test_export_carries_what_does_travel(tmp_path):
    """The signature is the part that means the same thing on both sides."""
    exported = _sender(tmp_path).export_history()

    for msg in exported:
        assert msg.get("id")
        assert msg.get("content")


def test_import_builds_its_own_chain(tmp_path):
    """Receiver in the sender's room: a history sync is same-room on both sides,
    and the content hash is bound to it."""
    exported = _sender(tmp_path).export_history()

    receiver = _monitor(tmp_path, "group-src")
    receiver.import_history(exported)

    assert [m["msg_index"] for m in receiver.message_history] == [1, 2, 3]
    assert all(m.get("chain_hash") for m in receiver.message_history)


def test_an_imported_history_loads_without_a_broken_chain(tmp_path, caplog):
    """The symptom this change exists to remove."""
    exported = _sender(tmp_path).export_history()

    receiver = _monitor(tmp_path, "group-src")
    receiver.import_history(exported)
    receiver.save_history()

    with caplog.at_level(logging.INFO):
        _monitor(tmp_path, "group-src").load_history()

    assert "Chain broken" not in caplog.text


def test_a_merged_message_is_chained_locally(tmp_path):
    """merge_history appended foreign values verbatim; that broke the chain."""
    receiver = _monitor(tmp_path, "group-merge")
    receiver.add_message(role="user", content="mine", sender_node_id="n1", sender_name="Mike")

    receiver.merge_history([
        {"id": "from-peer", "role": "peer", "content": "theirs",
         "msg_index": 99, "chain_hash": "a" * 64},
    ])

    last = receiver.message_history[-1]
    assert last["msg_index"] == 2, "the peer's position in its own history is not ours"
    assert last["chain_hash"] != "a" * 64


def test_loader_does_not_report_a_minted_hash_as_verified(tmp_path, caplog):
    """The defect verbatim: a message with no hash used to become 'verified'."""
    sender = _sender(tmp_path)
    stripped = [
        {k: v for k, v in m.items() if k not in ("chain_hash", "msg_index")}
        for m in sender.export_history()
    ]

    receiver = _monitor(tmp_path, "group-src")
    receiver.import_history(stripped)
    # Written straight to disk without the local chain, as an older build left it.
    for m in receiver.message_history:
        m.pop("chain_hash", None)
    receiver.save_history()

    with caplog.at_level(logging.INFO):
        _monitor(tmp_path, "group-src").load_history()

    text = caplog.text
    assert "minted a hash" in text, "silently inventing a hash is what hid the defect"
    assert "unverified, not verified" in text


def test_tampering_in_transit_is_caught_by_the_signature_now(tmp_path):
    """What the travelling chain was standing in for, done by the right tool.

    Same room on both sides, as a group history sync always is — the hash is
    bound to the room, and the next test is about that.
    """
    exported = _sender(tmp_path).export_history()
    exported[1]["content"] = "tampered in transit"

    receiver = _monitor(tmp_path, "group-src")
    added = receiver.merge_history(exported)

    assert added == 2, "the altered message must not be merged"
    assert all(m["content"] != "tampered in transit" for m in receiver.message_history)


def test_a_history_from_another_room_does_not_merge(tmp_path):
    """The hash covers conversation_id, and the verifier uses its own."""
    exported = _sender(tmp_path).export_history()  # signed for group-src

    receiver = _monitor(tmp_path, "group-elsewhere")
    added = receiver.merge_history(exported)

    assert added == 0


def test_a_history_broken_by_the_old_merge_is_repaired_once(tmp_path, caplog):
    """Files already on disk carry foreign chains; the fix alone leaves them.

    Without this, "Chain broken" fires on every start for every group synced
    before the chain became local — the alarm stays on and stops being read.
    """
    receiver = _monitor(tmp_path, "group-was-broken")
    receiver.add_message(role="user", content="mine", sender_node_id="n1", sender_name="Mike")
    # What the old merge produced: a foreign index and hash appended verbatim.
    receiver.message_history.append({
        "id": "from-peer", "role": "peer", "content": "theirs",
        "msg_index": 99, "chain_hash": "a" * 64,
    })
    receiver.save_history()

    with caplog.at_level(logging.INFO):
        reloaded = _monitor(tmp_path, "group-was-broken")
        reloaded.load_history()

    assert "rebuilding locally" in caplog.text
    assert [m["msg_index"] for m in reloaded.message_history] == [1, 2]

    caplog.clear()
    with caplog.at_level(logging.INFO):
        again = _monitor(tmp_path, "group-was-broken")
        again.load_history()

    assert "Chain broken" not in caplog.text, "the repair must be persisted, not repeated"
    assert "rebuilding locally" not in caplog.text
