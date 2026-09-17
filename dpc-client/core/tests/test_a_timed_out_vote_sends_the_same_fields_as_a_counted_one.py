"""The no-vote timeout payload used to skip the fields the counted path sends.

consensus_manager.py built KNOWLEDGE_COMMIT_RESULT (spec §3.7) for a normal
finalize with `timestamp`, `vote_tally.abstain` and `vote_tally.participants`,
but built the no-vote-cast timeout branch (`_finalize_vote`, the
`total_votes == 0` case) from a separate dict literal that carried none of
the three. A receiver had no way to tell a timed-out proposal's roster size
from the payload alone.

Fix (THE-SPEC-REQUIRES-A-TIMESTAMP-NOBODY-SENDS-LISTS-CANCEL-REASONS-NOBODY-
USES-AND-DESCRIBES-AN-ENCRYPTION-THE-CODE-REPLACED, 2026-09-14): both paths
now call one `_build_result_payload` helper, so the two cannot drift again.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from dpc_client_core.consensus_manager import ConsensusManager
from dpc_client_core.message_handlers.knowledge_handler import KnowledgeCommitResultHandler
from dpc_protocol.knowledge_commit import KnowledgeCommitProposal, KnowledgeEntry, CommitVote

NODES = [f"dpc-node-{c * 32}" for c in "ab"]
A, B = NODES


def _proposal(participants=(A, B)):
    return KnowledgeCommitProposal(
        proposal_id="proposal-timeout-payload",
        conversation_id="group-9f1c2e7a5b3d",
        topic="parity",
        summary="the timeout payload should carry what the counted one does",
        entries=[KnowledgeEntry(content="a helper both paths call", tags=["consensus"])],
        participants=list(participants),
        timestamp="2026-09-14T06:00:00+00:00",
    )


def _manager():
    m = ConsensusManager.__new__(ConsensusManager)
    m.node_id = A
    m.consensus_threshold = 0.75
    m.sessions = {}
    m.results = []
    m.rejected = []
    m.on_commit_approved = None
    m.on_vote_received = None
    m.on_commit_revision_needed = None

    async def _apply(commit, origin="local"):
        return True

    async def _rejected(proposal, votes):
        m.rejected.append(proposal)

    async def _result(payload, participants):
        m.results.append(payload)

    m._apply_commit = _apply
    m.on_commit_rejected = _rejected
    m.on_result_broadcast = _result
    return m


def _finalize_with_no_votes(participants=(A, B)):
    """Run the no-vote-cast timeout branch; hand back the manager and session."""
    m = _manager()
    proposal = _proposal(participants)
    session = SimpleNamespace(proposal=proposal, status="voting", votes={})
    asyncio.run(m._finalize_vote(session))
    return m, session


# --- the timeout payload now matches the counted one -------------------------


def test_the_timeout_payload_carries_a_timestamp():
    m, _ = _finalize_with_no_votes()

    payload = m.results[-1]
    assert "timestamp" in payload, payload.keys()
    datetime.fromisoformat(payload["timestamp"])  # ISO 8601, same as the counted path


def test_the_timeout_payload_names_the_roster_size_and_no_abstentions():
    m, _ = _finalize_with_no_votes(participants=(A, B))

    tally = m.results[-1]["vote_tally"]
    assert tally["participants"] == 2
    assert tally["abstain"] == 0


def test_the_timeout_payload_names_a_three_person_roster():
    C = "dpc-node-" + "c" * 32
    m, _ = _finalize_with_no_votes(participants=(A, B, C))

    assert m.results[-1]["vote_tally"]["participants"] == 3


# --- the counted path is unchanged --------------------------------------------


def test_the_counted_path_payload_keeps_its_shape():
    """Pin the main path's keys so the refactor cannot silently drop or reorder one."""
    m = _manager()
    proposal = _proposal((A, B))
    session = SimpleNamespace(
        proposal=proposal,
        status="voting",
        votes={
            A: CommitVote(proposal_id=proposal.proposal_id, voter_node_id=A, vote="approve"),
            B: CommitVote(proposal_id=proposal.proposal_id, voter_node_id=B, vote="approve"),
        },
    )
    asyncio.run(m._finalize_vote(session))

    payload = m.results[-1]
    assert list(payload.keys()) == [
        "proposal_id", "topic", "summary", "status",
        "vote_tally", "votes", "timestamp", "commit_id",
    ]
    assert list(payload["vote_tally"].keys()) == [
        "approve", "reject", "request_changes", "abstain",
        "total", "participants", "threshold", "approval_rate",
    ]
    assert payload["status"] == "approved"
    assert payload["vote_tally"] == {
        "approve": 2, "reject": 0, "request_changes": 0, "abstain": 0,
        "total": 2, "participants": 2, "threshold": 0.75, "approval_rate": 1.0,
    }


# --- a receiver can read the timeout payload ----------------------------------


@pytest.mark.asyncio
async def test_a_receiver_accepts_the_timeout_payload_without_error():
    """KnowledgeCommitResultHandler must not choke on the new fields."""
    m = _manager()
    proposal = _proposal((A, B))
    session = SimpleNamespace(proposal=proposal, status="voting", votes={})
    await m._finalize_vote(session)
    payload = m.results[-1]

    events = []

    async def _broadcast_event(event, data):
        events.append((event, data))

    service = SimpleNamespace(
        consensus_manager=SimpleNamespace(get_session=lambda pid: None),
        local_api=SimpleNamespace(broadcast_event=_broadcast_event),
        _processed_message_ids=set(),
    )
    handler = KnowledgeCommitResultHandler(service)

    result = await handler.handle(A, payload)

    assert result is None
    assert events == [("knowledge_commit_result", payload)]
