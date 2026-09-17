"""One approved proposal has to become one commit, on every node that counts it.

Measured on Mike's two-node run of 2026-09-02, group-0a52389f2bb6: the vote was
clean — two approvals from two node ids, 2/2 against a 0.75 threshold, the
result exchanged in both directions — and then each node minted its own commit
id for it (`commit-3cb3bbd65304bbb8` here, `commit-705bcabf8972919d` there).
The log says the consequence out loud: «no markdown file found for commit
commit-705bc», then «COMMIT_ACK … (1/2 participants confirmed)». The stored
file lists both approvers and carries one signature.

The cause is that `timestamp` is part of the hash input and defaults to the
clock of whichever node finishes counting first. The proposal is the thing both
nodes already hold — the same argument the file already makes for
`parent_commit_id` two lines above.
"""

import asyncio
from types import SimpleNamespace

import pytest

from dpc_client_core.consensus_manager import ConsensusManager
from dpc_protocol.knowledge_commit import (
    KnowledgeCommitProposal,
    KnowledgeEntry,
    CommitVote,
)

A = "dpc-node-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "dpc-node-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _proposal():
    """One proposal, as both nodes hold it: it travels whole over the wire."""
    return KnowledgeCommitProposal(
        proposal_id="proposal-6f0cc1a2",
        conversation_id="group-0a52389f2bb6",
        topic="DPC peer URIs and Russian greetings",
        summary="Two DPC URIs were shared along with casual greetings.",
        entries=[KnowledgeEntry(content="a dpc:// URI is a peer endpoint", tags=["DPC"])],
        participants=[A, B],
        parent_commit_id="commit-838f2353dc44144e",
        timestamp="2026-09-01T20:52:43.927874+00:00",
    )


def _manager_that_captures(node_id):
    """A ConsensusManager reduced to the one path under test."""
    m = ConsensusManager.__new__(ConsensusManager)
    m.node_id = node_id
    m.consensus_threshold = 0.75
    m.sessions = {}
    m.on_result_broadcast = None
    m.on_commit_approved = None
    m.on_vote_received = None
    m.commits = []

    async def _apply(commit, origin="local"):
        # The one step of the real _apply_commit that decides identity
        # (consensus_manager.py:507-509): a local commit hashes itself.
        if origin == "local":
            commit.compute_hash()
        m.commits.append(commit)
        return True

    m._apply_commit = _apply
    return m


def _commit_on(node_id, proposal):
    """Run the finalisation this node would run, and return the commit it made."""
    m = _manager_that_captures(node_id)
    session = SimpleNamespace(
        proposal=proposal,
        status="voting",
        votes={
            A: CommitVote(proposal_id=proposal.proposal_id, voter_node_id=A, vote="approve"),
            B: CommitVote(proposal_id=proposal.proposal_id, voter_node_id=B, vote="approve"),
        },
    )
    asyncio.run(m._finalize_vote(session))
    assert m.commits, "the approved proposal produced no commit"
    return m.commits[0]


def test_both_nodes_mint_the_same_commit_for_one_proposal():
    proposal = _proposal()

    here = _commit_on(A, proposal)
    there = _commit_on(B, proposal)

    assert here.commit_id == there.commit_id, (
        f"one proposal, two commit ids: {here.commit_id} and {there.commit_id}"
    )
    assert here.commit_hash == there.commit_hash


def test_the_identity_comes_from_the_proposal_and_not_from_the_clock():
    """The control: a commit whose timestamp is its own is a different commit."""
    proposal = _proposal()

    here = _commit_on(A, proposal)
    later = _proposal()
    later.timestamp = "2026-09-01T20:53:09.584030+00:00"
    there = _commit_on(B, later)

    assert here.commit_id != there.commit_id, (
        "two proposals with different timestamps must not collapse into one commit"
    )


def test_the_timestamp_survives_the_wire():
    """The fix rests on the peer holding the proposer's timestamp.

    The tests above build the proposal directly on both sides, so they pin the
    hashing rule and not the thing it depends on — Ark's note. The peer gets
    the proposal through `to_dict` and `from_dict`; this is that path.
    """
    sent = _proposal()

    received = KnowledgeCommitProposal.from_dict(sent.to_dict())

    assert received.timestamp == sent.timestamp
    assert _commit_on(A, sent).commit_id == _commit_on(B, received).commit_id
