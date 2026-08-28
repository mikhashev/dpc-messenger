"""A record with no signature used to skip the check instead of failing it.

`merge_history` held the whole verification block behind
`if sig and content_hash and signer:`, so removing the three fields — not
forging them — was enough to walk past every check and be stored beside
records that had passed. `import_history`, which the 1:1 path uses and which
replaces the entire local history, had no check at all.

Neither is fixed by refusing unsigned records outright: history written before
ADR-036 carries no signature, `export_history` ships none for it, and a third
of what is on disk here is that old (175 of 535 records, 2026-08-28). So the
rule is that every arriving record leaves with a verdict on it, and
`reject_unsigned` turns the label into a refusal once the legacy ones are gone.
"""

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor


PARTICIPANTS = [{"node_id": "n1", "name": "User", "context": "local"}]


def _monitor(tmp_path, cid="group-test", settings=None):
    m = ConversationMonitor(
        conversation_id=cid, participants=PARTICIPANTS, llm_manager=None, settings=settings
    )
    m._get_history_path = lambda: tmp_path / cid / "history.json"
    return m


def _signed_export(tmp_path, cid="group-test"):
    sender = _monitor(tmp_path, cid)
    for text in ("first", "second"):
        sender.add_message(
            role="user", content=text,
            sender_node_id=sender._get_signer().node_id, sender_name="Mike",
        )
    return sender.export_history()


class _Settings:
    def __init__(self, reject):
        self._reject = reject

    def get_reject_unsigned_history(self) -> bool:
        return self._reject


def test_an_unsigned_record_is_kept_but_labelled(tmp_path, signing_identity):
    receiver = _monitor(tmp_path, "group-merge")

    added = receiver.merge_history([{"id": "x", "role": "peer", "content": "no signature"}])

    assert added == 1, "legacy history must still sync"
    assert receiver.message_history[-1]["verification"] == "legacy"


def test_a_peer_cannot_label_its_own_record_verified(tmp_path, signing_identity):
    """The verdict is written by the receiver or it is worth nothing."""
    receiver = _monitor(tmp_path, "group-merge")

    receiver.merge_history([
        {"id": "x", "role": "peer", "content": "trust me", "verification": "verified"},
    ])

    assert receiver.message_history[-1]["verification"] == "legacy"


def test_a_signature_with_a_field_removed_stops_being_a_signature(tmp_path, signing_identity):
    """Taking one field out cannot leave a half-checked record behind.

    The live path calls a record with any of the three missing `legacy` and so
    does this one: same absence, same word, and `reject_unsigned` refuses both.
    A node holding no key writes exactly this shape — a hash with nothing
    signing it — so refusing it outright would strand its own history.
    """
    exported = _signed_export(tmp_path)
    exported[0].pop("signature")

    receiver = _monitor(tmp_path, "group-test")
    added = receiver.merge_history(exported)

    assert added == 2
    first = [m for m in receiver.message_history if m["content"] == "first"][0]
    assert first["verification"] == "legacy"


def test_reject_unsigned_refuses_instead_of_labelling(tmp_path, signing_identity):
    receiver = _monitor(tmp_path, "group-merge", settings=_Settings(True))

    added = receiver.merge_history([{"id": "x", "role": "peer", "content": "no signature"}])

    assert added == 0


def test_import_refuses_a_record_whose_content_moved(tmp_path, signing_identity):
    """The 1:1 path ran no check at all, and it replaces the whole history."""
    exported = _signed_export(tmp_path)
    exported[1]["content"] = "tampered in transit"

    receiver = _monitor(tmp_path, "group-test")
    receiver.import_history(exported)

    assert [m["content"] for m in receiver.message_history] == ["first"]


def test_an_import_that_survives_nothing_leaves_the_history_alone(tmp_path, signing_identity):
    """Otherwise a peer empties a conversation by answering with forgeries."""
    exported = _signed_export(tmp_path)
    for msg in exported:
        msg["content"] = "rewritten"

    receiver = _monitor(tmp_path, "group-test")
    receiver.add_message(role="user", content="mine", sender_node_id="n1", sender_name="Mike")
    receiver.import_history(exported)

    assert [m["content"] for m in receiver.message_history] == ["mine"]


def test_import_keeps_the_verdict_it_computed(tmp_path, signing_identity):
    exported = _signed_export(tmp_path)

    receiver = _monitor(tmp_path, "group-test")
    receiver.import_history(exported)

    assert len(receiver.message_history) == 2
    # No certificate for the test key, so signed-but-uncheckable, not verified.
    assert {m["verification"] for m in receiver.message_history} == {"unverified"}


def test_a_preimage_we_cannot_recompute_is_legacy_not_a_forgery(tmp_path, signing_identity):
    """A node one version ahead is an outage to refuse, not an attack."""
    exported = _signed_export(tmp_path)
    for msg in exported:
        msg["preimage_version"] = "dptp-msg-v99"

    receiver = _monitor(tmp_path, "group-test")
    added = receiver.merge_history(exported)

    assert added == 2
    assert {m["verification"] for m in receiver.message_history} == {"legacy"}


def test_reject_unsigned_also_refuses_a_preimage_it_cannot_recompute(tmp_path, signing_identity):
    exported = _signed_export(tmp_path)
    for msg in exported:
        msg["preimage_version"] = "dptp-msg-v99"

    receiver = _monitor(tmp_path, "group-test", settings=_Settings(True))

    assert receiver.merge_history(exported) == 0


# --- 1:1, where the two sides do not agree on the name of the room ----------

def _one_to_one(tmp_path, me, peer):
    """A 1:1 monitor as knowledge_service._build_participants builds it.

    The conversation is keyed by the *other* node, and `participants[0]` is
    always this node — so the two sides sign under different room names for
    the same conversation. Nothing in the group tests has this shape: there
    `conversation_id` matches on both sides and the second candidate never
    carries a verification.
    """
    m = ConversationMonitor(
        conversation_id=peer,
        participants=[
            {"node_id": me, "name": "User", "context": "local"},
            {"node_id": peer, "name": "Peer", "context": "peer"},
        ],
        llm_manager=None,
    )
    m._get_history_path = lambda: tmp_path / peer / "history.json"
    return m


def test_a_one_to_one_history_verifies_under_the_receivers_own_node_id(tmp_path, signing_identity):
    """Alice signs under Bob's name for the room; Bob holds Alice's."""
    alice = signing_identity.node_id
    bob = "dpc-node-" + "b" * 32

    sender = _one_to_one(tmp_path, me=alice, peer=bob)
    sender.add_message(role="user", content="hello bob", sender_node_id=alice, sender_name="Alice")
    exported = sender.export_history()
    assert exported[0]["content_hash"], "the fixture must actually sign"

    receiver = _one_to_one(tmp_path, me=bob, peer=alice)
    added = receiver.merge_history(exported)

    assert added == 1, "a 1:1 history must not be refused for the room it names"
    assert receiver.message_history[-1]["verification"] == "unverified"


def test_a_third_node_cannot_take_that_history_as_its_own(tmp_path, signing_identity):
    """The second candidate is the receiver's own id, so it opens no door."""
    alice = signing_identity.node_id
    bob = "dpc-node-" + "b" * 32
    carol = "dpc-node-" + "c" * 32

    sender = _one_to_one(tmp_path, me=alice, peer=bob)
    sender.add_message(role="user", content="hello bob", sender_node_id=alice, sender_name="Alice")
    exported = sender.export_history()

    eavesdropper = _one_to_one(tmp_path, me=carol, peer=alice)

    assert eavesdropper.merge_history(exported) == 0
