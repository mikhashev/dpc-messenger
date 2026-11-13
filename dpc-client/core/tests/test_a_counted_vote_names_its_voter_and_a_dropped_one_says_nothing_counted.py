"""The knowledge-vote INFO line says "Counted" only for a vote that entered the
tally, and names the voter (verified) beside the relay that carried it.
"""

import asyncio
import logging
from types import SimpleNamespace

from dpc_client_core.consensus_manager import ConsensusManager
from dpc_protocol.knowledge_commit import CommitVote, KnowledgeCommitProposal, KnowledgeEntry

A, B, C, RELAY = (f"dpc-node-{c * 32}" for c in "abcr")
PID = "proposal-log-line"


def _manager(participants=(A, B, C), status="voting"):
    m = ConsensusManager.__new__(ConsensusManager)
    m.node_id = A
    m.on_vote_received = None
    proposal = KnowledgeCommitProposal(
        proposal_id=PID, conversation_id="group-0a52389f2bb6", topic="t", summary="s",
        entries=[KnowledgeEntry(content="x", tags=[])],
        participants=list(participants), timestamp="2026-11-13T00:00:00+00:00",
    )
    m.sessions = {PID: SimpleNamespace(proposal=proposal, status=status, votes={})}
    return m


def _send(m, voter, verdict="approve", via=RELAY):
    payload = {"proposal_id": PID, "vote": verdict}
    asyncio.run(m.handle_vote_message(via, payload, voter_node_id=voter))


def _counted(caplog):
    return [r.getMessage() for r in caplog.records if "Counted" in r.getMessage()]


def test_a_counted_vote_prints_one_line_with_voter_relay_and_tally(caplog):
    m = _manager()
    with caplog.at_level(logging.DEBUG):
        _send(m, B)
    lines = _counted(caplog)
    assert len(lines) == 1
    assert f"by {B} via {RELAY} on {PID} (1/3)" in lines[0]
    assert lines[0].startswith("Counted approve vote")


def test_a_proposal_that_named_nobody_says_no_roster_instead_of_n_over_n(caplog):
    m = _manager(participants=())
    with caplog.at_level(logging.DEBUG):
        _send(m, B)
        _send(m, C)
    lines = _counted(caplog)
    assert len(lines) == 2
    assert f"on {PID} (1 vote, no roster)" in lines[0]
    assert f"on {PID} (2 votes, no roster)" in lines[1]
    assert "/" not in lines[1].rsplit("(", 1)[1]
    assert m.vote_tally(PID) == (2, 0)


def test_a_repeated_identical_vote_prints_no_counted_line(caplog):
    m = _manager()
    _send(m, B)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        _send(m, B)
    assert _counted(caplog) == []


def test_a_vote_after_the_session_closed_prints_no_counted_line(caplog):
    m = _manager(status="finalizing")
    with caplog.at_level(logging.DEBUG):
        _send(m, B)
    assert _counted(caplog) == []


def test_a_vote_from_outside_the_roster_prints_no_counted_line_and_says_why(caplog):
    m = _manager(participants=(A, B))
    with caplog.at_level(logging.DEBUG):
        _send(m, C)
    assert _counted(caplog) == []
    assert any("not a participant" in r.getMessage() for r in caplog.records)


def test_receive_vote_returns_whether_it_counted():
    m = _manager()
    v = CommitVote(proposal_id=PID, voter_node_id=B, vote="approve")
    assert asyncio.run(m.receive_vote(v)) is True
    assert asyncio.run(m.receive_vote(v)) is False
    other = CommitVote(proposal_id="nope", voter_node_id=B, vote="approve")
    assert asyncio.run(m.receive_vote(other)) is False
