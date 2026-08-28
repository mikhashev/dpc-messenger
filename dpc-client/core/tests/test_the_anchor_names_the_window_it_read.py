"""A knowledge proposal used to be anchored to a value no other node could hold.

It carried `(msg_index, chain_hash)` of the last message, and the voter compared
the chain hash at that index with its own. But `chain_hash` covers `role`, and
`role` is a rendering — every node calls its own messages "user" and everyone
else's "peer" — so the same five messages produced three different chains on
2026-08-07: `855c…` on Windows, `1a05…` on Linux, `903d…` on macOS. The check
therefore refused every vote except the proposer's own; both peers were turned
away at index 3, and the commit could never be finalised by anyone.

The replacement names the extraction window by `content_hash`, which is the
value the author signed and is identical wherever the message is held. A set,
not a position, on Fable 5's objection (B9): a single hash of the last message
proves only that the voter has *that* message. Somebody missing a message in
the middle of the window would pass and approve knowledge drawn from text they
never saw.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor

GROUP = "group-1234567890ab"


def _monitor(stored):
    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.message_history = stored
    return monitor


def _stored(msg_id, content_hash, **extra):
    row = {"id": msg_id, "content_hash": content_hash}
    row.update(extra)
    return row


def _msg(msg_id):
    return SimpleNamespace(message_id=msg_id)


# --- what the proposer puts in the proposal ---------------------------------


def test_the_window_is_the_messages_the_extraction_read():
    monitor = _monitor([_stored("m1", "h1"), _stored("m2", "h2"), _stored("m3", "h3")])

    assert monitor.window_content_hashes([_msg("m1"), _msg("m3")]) == ["h1", "h3"]


def test_the_window_keeps_the_order_the_extraction_saw():
    monitor = _monitor([_stored("m1", "h1"), _stored("m2", "h2")])

    assert monitor.window_content_hashes([_msg("m2"), _msg("m1")]) == ["h2", "h1"]


def test_a_message_named_twice_is_claimed_once():
    monitor = _monitor([_stored("m1", "h1")])

    assert monitor.window_content_hashes([_msg("m1"), _msg("m1")]) == ["h1"]


def test_messages_written_before_signing_are_left_out_not_faked():
    """Claiming a message that has no hash would refuse honest voters forever."""
    monitor = _monitor([_stored("m1", "h1"), {"id": "m2"}])

    assert monitor.window_content_hashes([_msg("m1"), _msg("m2")]) == ["h1"]


def test_an_empty_extraction_anchors_nothing():
    assert _monitor([]).window_content_hashes([]) == []


def test_the_old_positional_anchor_is_gone():
    """It could not be right between nodes, so it should not be reachable."""
    assert not hasattr(ConversationMonitor, "history_anchor")


# --- what the voter checks --------------------------------------------------


def _service(window, held):
    from dpc_client_core.knowledge_service import KnowledgeService

    service = KnowledgeService.__new__(KnowledgeService)
    session = SimpleNamespace(
        proposal=SimpleNamespace(
            conversation_id=GROUP, based_on_content_hashes=window
        )
    )
    service.consensus_manager = SimpleNamespace(sessions={"p1": session})
    service.conversation_monitors = {
        GROUP: SimpleNamespace(
            message_history=[_stored(f"m{i}", h) for i, h in enumerate(held)]
        )
    }
    return service


def test_a_voter_holding_the_whole_window_may_vote():
    """The case that was refused on every node but the proposer's."""
    service = _service(window=["h1", "h2", "h3"], held=["h1", "h2", "h3", "h4"])

    assert service._history_drift("p1") is None


def test_a_hole_in_the_middle_of_the_window_is_refused():
    """What a single trailing hash would have let through."""
    service = _service(window=["h1", "h2", "h3"], held=["h1", "h3"])

    drift = service._history_drift("p1")

    assert drift["reason"] == "history_drift"
    assert drift["missing_messages"] == 1
    assert drift["window_size"] == 3


def test_the_refusal_says_how_much_is_missing():
    service = _service(window=["h1", "h2"], held=[])

    assert "2 of 2" in service._history_drift("p1")["message"]


def test_a_proposal_without_a_window_is_not_a_mismatch():
    """An older proposer sends no window; refusing it would be inventing a fault."""
    service = _service(window=None, held=["h1"])

    assert service._history_drift("p1") is None


def test_order_is_not_what_is_being_checked():
    """The voter must hold the messages; the sequence they arrived in is local."""
    service = _service(window=["h1", "h2"], held=["h2", "h1"])

    assert service._history_drift("p1") is None


# --- the third answer knowledge voting has ----------------------------------


def test_request_changes_cannot_be_rewritten_as_reject():
    """Folding it into "no" would let a relay change the decision in flight."""
    from dpc_protocol.message_signing import vote_content_hash

    base = dict(proposal_id="p1", conversation_id=GROUP,
                voter_node_id="dpc-node-" + "c" * 32, timestamp="2026-08-07T02:21:17Z")
    assert vote_content_hash(vote="request_changes", **base) != vote_content_hash(
        vote="reject", **base
    )


def test_the_two_spellings_of_yes_still_agree():
    from dpc_protocol.message_signing import vote_content_hash

    base = dict(proposal_id="p1", conversation_id=GROUP,
                voter_node_id="dpc-node-" + "c" * 32, timestamp="2026-08-07T02:21:17Z")
    assert vote_content_hash(vote=True, **base) == vote_content_hash(vote="approve", **base)
    assert vote_content_hash(vote=False, **base) == vote_content_hash(vote="reject", **base)
