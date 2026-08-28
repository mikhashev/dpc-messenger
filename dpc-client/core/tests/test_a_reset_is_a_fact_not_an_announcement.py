"""A reset used to be a message, and a node that missed it undid the reset.

ADR-038 Q3, in the ADR's own words: three nodes agree a New Session, all vote
yes, all clear. One node's process dies a second after voting and before
`NEW_SESSION_RESULT` reaches it. It returns holding the whole history while the
others hold none — and the next sync hands its copy back to them, undoing the
reset with nobody noticing.

`session_started_at` turns the reset into a fact about the group. The returning
node reads the boundary and clears what predates it; whoever was present does
nothing. Written by *any* participant able to show the quorum, which is what
closes the other half — the initiator can die between the last vote and the
announcement, and the boundary still gets written.

The evidence travels inside the marker because the votes that authorised the
reset live in the history the reset destroys. So the check a returning node
performs needs nothing it no longer has.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.managers.group_manager import GroupMetadata, GroupManager

ME = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32
GROUP = "group-1234567890ab"
EARLIER = "2026-08-07T02:00:00+00:00"
BOUNDARY = "2026-08-07T02:17:45+00:00"
LATER = "2026-08-07T02:20:00+00:00"


def _manager(tmp_path, **fields):
    manager = GroupManager(dpc_home=tmp_path, node_id=ME)
    group = GroupMetadata(
        group_id=GROUP, name="1234", created_by=ME, members=[ME, BOB], **fields
    )
    manager._groups[GROUP] = group
    return manager


# --- the marker itself ------------------------------------------------------


def test_any_participant_may_write_the_boundary(tmp_path):
    manager = _manager(tmp_path)

    manager.set_session_marker(GROUP, BOUNDARY, evidence={"proposal_id": "p1"})

    assert manager.get_group(GROUP).session_started_at == BOUNDARY


def test_two_nodes_writing_the_same_reset_do_not_fight(tmp_path):
    """Idempotence is what makes "anyone may write it" safe."""
    manager = _manager(tmp_path)
    manager.set_session_marker(GROUP, BOUNDARY, evidence={"proposal_id": "p1"})
    version_after_first = manager.get_group(GROUP).version

    assert manager.set_session_marker(GROUP, BOUNDARY, evidence={"proposal_id": "p1"}) is None
    assert manager.get_group(GROUP).version == version_after_first


def test_an_older_boundary_never_displaces_a_newer_one(tmp_path):
    manager = _manager(tmp_path, session_started_at=LATER)

    assert manager.set_session_marker(GROUP, BOUNDARY) is None
    assert manager.get_group(GROUP).session_started_at == LATER


def test_writing_the_boundary_bumps_the_version_so_it_travels(tmp_path):
    manager = _manager(tmp_path)
    before = manager.get_group(GROUP).version

    manager.set_session_marker(GROUP, BOUNDARY)

    assert manager.get_group(GROUP).version == before + 1


# --- a peer that has not heard about the reset ------------------------------


def test_a_stale_peer_cannot_erase_the_boundary(tmp_path):
    """The defect this guard exists for: version ordering answers "who wrote
    last", which is not the same question as "when did the group start over"."""
    manager = _manager(tmp_path, session_started_at=BOUNDARY, version=2)

    manager.apply_sync(
        {"group_id": GROUP, "name": "1234", "created_by": ME,
         "members": [ME, BOB], "version": 9}
    )

    assert manager.get_group(GROUP).session_started_at == BOUNDARY


def test_a_newer_boundary_from_a_peer_is_taken(tmp_path):
    manager = _manager(tmp_path, session_started_at=BOUNDARY, version=2)

    manager.apply_sync(
        {"group_id": GROUP, "name": "1234", "created_by": ME, "members": [ME, BOB],
         "version": 3, "session_started_at": LATER}
    )

    assert manager.get_group(GROUP).session_started_at == LATER


# --- what the returning node does to its own history ------------------------


def _monitor(timestamps):
    from dpc_client_core.conversation_monitor import ConversationMonitor

    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.conversation_id = GROUP
    monitor.message_history = [
        {"id": f"m{i}", "timestamp": ts, "content": "x"} for i, ts in enumerate(timestamps)
    ]
    monitor.full_conversation = []
    monitor.message_buffer = []
    monitor._history_dirty = False
    monitor.save_history = lambda: True
    return monitor


def test_only_what_predates_the_boundary_is_dropped():
    """A late arrival must not become a second reset for the messages after it."""
    monitor = _monitor([EARLIER, EARLIER, BOUNDARY, LATER])

    dropped = monitor.clear_before(BOUNDARY)

    assert dropped == 2
    assert [m["timestamp"] for m in monitor.message_history] == [BOUNDARY, LATER]


def test_a_node_that_was_present_drops_nothing():
    monitor = _monitor([LATER])

    assert monitor.clear_before(BOUNDARY) == 0


def test_no_boundary_means_no_clearing():
    monitor = _monitor([EARLIER])

    assert monitor.clear_before("") == 0
    assert len(monitor.message_history) == 1


# --- the evidence, which is what makes the marker safe to obey --------------


def _signed(node_id, proposal_id="p1", vote=True, timestamp=BOUNDARY):
    from dpc_protocol.message_signing import VOTE_PREIMAGE_VERSION, vote_content_hash

    return {
        "vote": vote,
        "signer_node_id": node_id,
        "vote_preimage_version": VOTE_PREIMAGE_VERSION,
        "timestamp": timestamp,
        "vote_hash": vote_content_hash(
            proposal_id=proposal_id, conversation_id=GROUP,
            voter_node_id=node_id, vote=vote, timestamp=timestamp,
        ),
        "signature": "sig",
    }


@pytest.fixture
def verifier(monkeypatch):
    from dpc_protocol.commit_integrity import CommitSigner

    answer = {"value": True}
    monkeypatch.setattr(
        CommitSigner, "verify_signature", staticmethod(lambda *a, **k: answer["value"])
    )
    return answer


def _proven(**kw):
    from dpc_client_core.signing import quorum_is_proven

    args = dict(
        proposal_id="p1", conversation_id=GROUP, participants=[ME, BOB],
        votes={ME: _signed(ME), BOB: _signed(BOB)},
    )
    args.update(kw)
    return quorum_is_proven(**args)


def test_a_unanimous_signed_vote_proves_the_reset(verifier):
    assert _proven() is True


def test_a_missing_participant_is_not_a_quorum(verifier):
    """Unanimity, because a member who did not agree hands its history back."""
    assert _proven(votes={ME: _signed(ME)}) is False


def test_a_reject_among_the_votes_proves_nothing(verifier):
    assert _proven(votes={ME: _signed(ME), BOB: _signed(BOB, vote=False)}) is False


def test_evidence_for_another_proposal_does_not_transfer(verifier):
    """Otherwise one agreed reset could authorise every later one."""
    assert _proven(votes={ME: _signed(ME, proposal_id="other"),
                          BOB: _signed(BOB, proposal_id="other")}) is False


def test_evidence_from_another_group_does_not_transfer(verifier):
    assert _proven(conversation_id="group-ffffffffffff") is False


def test_a_signature_signed_by_the_wrong_node_fails(verifier):
    forged = _signed(BOB)
    forged["signer_node_id"] = ME
    assert _proven(votes={ME: _signed(ME), BOB: forged}) is False


def test_an_invalid_signature_fails(verifier):
    verifier["value"] = False
    assert _proven() is False


def test_an_uncheckable_signature_is_not_proof(verifier):
    """No cached certificate: obey it later, when there is one — not now."""
    verifier["value"] = None
    assert _proven() is False


def test_evidence_with_no_participants_proves_nothing(verifier):
    assert _proven(participants=[]) is False
