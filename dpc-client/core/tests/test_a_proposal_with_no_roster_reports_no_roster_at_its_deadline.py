"""An empty roster is reported as empty: not as the number of votes cast.

`participant_count = len(participants) or total_votes` made a roster-less
proposal that reached its deadline log "3 of 3 participants answered" and send
`vote_tally.participants == 3` to peers. The receiver only relays and displays
the payload (KnowledgeCommitResultHandler), so 0 is safe on the wire.
"""

import asyncio
import logging
from types import SimpleNamespace

from dpc_client_core.consensus_manager import ConsensusManager
from dpc_protocol.knowledge_commit import KnowledgeCommitProposal, KnowledgeEntry, CommitVote

NODES = [f"dpc-node-{c * 32}" for c in "abc"]
A, B, C = NODES


def _proposal(participants):
    return KnowledgeCommitProposal(
        proposal_id="proposal-no-roster",
        conversation_id="group-9f1c2e7a5b3d",
        topic="roster",
        summary="a proposal that names nobody",
        entries=[KnowledgeEntry(content="x", tags=["consensus"])],
        participants=list(participants),
        timestamp="2026-11-13T06:00:00+00:00",
    )


def _run(participants, voters):
    m = ConsensusManager.__new__(ConsensusManager)
    m.node_id = A
    m.consensus_threshold = 0.75
    m.sessions = {}
    m.results = []
    m.on_commit_approved = None
    m.on_vote_received = None
    m.on_commit_revision_needed = None

    async def _apply(commit, origin="local"):
        return True

    async def _rejected(proposal, votes):
        pass

    async def _result(payload, parts):
        m.results.append(payload)

    m._apply_commit = _apply
    m.on_commit_rejected = _rejected
    m.on_result_broadcast = _result
    p = _proposal(participants)
    votes = {n: CommitVote(proposal_id=p.proposal_id, voter_node_id=n, vote="approve") for n in voters}
    session = SimpleNamespace(proposal=p, status="timeout", votes=votes)
    asyncio.run(m._finalize_vote(session))
    return m, session


def test_an_empty_roster_at_the_deadline_sends_zero_and_logs_no_roster(caplog):
    with caplog.at_level(logging.INFO, logger="dpc_client_core.consensus_manager"):
        m, session = _run((), (A, B, C))

    tally = m.results[-1]["vote_tally"]
    assert tally["participants"] == 0
    assert tally["total"] == 3
    assert tally["approval_rate"] == 0
    assert m.results[-1]["status"] == "timeout"
    text = caplog.text
    assert "3 vote(s), no roster" in text
    assert "3 of 3" not in text


def test_a_normal_proposal_at_the_deadline_keeps_its_fraction(caplog):
    with caplog.at_level(logging.INFO, logger="dpc_client_core.consensus_manager"):
        m, _ = _run((A, B, C), (A, B))

    assert m.results[-1]["vote_tally"]["participants"] == 3
    assert "2 of 3 participants answered" in caplog.text
    assert "no roster" not in caplog.text
