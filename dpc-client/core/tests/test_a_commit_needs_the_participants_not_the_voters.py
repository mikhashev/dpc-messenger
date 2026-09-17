"""Approval is counted over the participants, and a deadline never approves.

The rule the code had divided approvals by the votes cast, so a proposal with
two participants and one vote was «approved 1/1», recorded as unanimous and
applied. It happened twice in two days on the same pair of nodes — the second
time predicted in the channel four minutes before the deadline fired.

Mike's decision, 2026-09-07: the denominator is the participants, and an
abstention stays in it. An abstention is a vote — a node that cannot judge says
so, with a reason — and silence is not consent, so the deadline can only end a
proposal, never carry it.

The threshold is unchanged at 0.75, which is what the DPTP spec already said
about participants. Read the arithmetic before assuming «an abstention blocks»:
with two or three participants it does, with four it does not, and the case is
pinned below so the consequence is visible rather than discovered.
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

NODES = [f"dpc-node-{c * 32}" for c in "abcd"]
A, B, C, D = NODES


def _proposal(participants):
    return KnowledgeCommitProposal(
        proposal_id="proposal-deadline",
        conversation_id="group-0a52389f2bb6",
        topic="counting",
        summary="who has to answer before knowledge is written down",
        entries=[KnowledgeEntry(content="a denominator is a claim too", tags=["consensus"])],
        participants=list(participants),
        timestamp="2026-09-07T06:00:00+00:00",
    )


def _manager():
    m = ConsensusManager.__new__(ConsensusManager)
    m.node_id = A
    m.consensus_threshold = 0.75
    m.sessions = {}
    m.commits = []
    m.results = []
    m.rejected = []
    m.on_commit_approved = None
    m.on_vote_received = None
    m.on_commit_revision_needed = None

    async def _apply(commit, origin="local"):
        m.commits.append(commit)
        return True

    async def _rejected(proposal, votes):
        m.rejected.append(proposal)

    async def _result(payload, participants):
        m.results.append(payload)

    m._apply_commit = _apply
    m.on_commit_rejected = _rejected
    m.on_result_broadcast = _result
    return m


def _finalize(votes, participants=(A, B), status="voting"):
    """Run one finalisation and hand back the manager that ran it."""
    m = _manager()
    proposal = _proposal(participants)
    session = SimpleNamespace(
        proposal=proposal,
        status=status,
        votes={
            node: CommitVote(
                proposal_id=proposal.proposal_id,
                voter_node_id=node,
                vote=vote,
                comment="no records to judge by" if vote == "abstain" else None,
            )
            for node, vote in votes.items()
        },
    )
    asyncio.run(m._finalize_vote(session))
    return m, session


# --- the deadline -----------------------------------------------------------


def test_one_approval_of_two_participants_is_not_approved_at_the_deadline():
    """The incident, twice over: 1 of 2 used to be «unanimous» and applied."""
    m, session = _finalize({A: "approve"}, status="timeout")

    assert not m.commits
    assert session.status == "timeout"
    assert m.rejected, "nobody was told the proposal ended"


def test_a_deadline_ends_a_proposal_even_when_the_share_would_pass():
    """Otherwise the rule is bypassed by waiting, which is how both got in."""
    m, _ = _finalize({A: "approve", B: "approve", C: "approve"},
                     participants=(A, B, C, D), status="timeout")

    assert not m.commits


# --- the denominator --------------------------------------------------------


def test_every_participant_approving_still_approves():
    m, session = _finalize({A: "approve", B: "approve"})

    assert session.status == "approved"
    assert m.commits[0].consensus_type == "unanimous"
    assert m.commits[0].description == "Approved by 2/2 participants"


def test_an_abstention_stays_in_the_denominator_and_a_pair_cannot_pass():
    m, session = _finalize({A: "approve", B: "abstain"})

    assert not m.commits
    assert session.status != "approved"


def test_a_group_of_four_outvotes_one_abstention():
    """The consequence of «stays in the denominator», pinned rather than found.

    An abstention blocks a pair and a trio because 1/2 and 2/3 fall under the
    threshold. At four participants three approvals reach exactly 0.75 and the
    commit passes with one node abstaining — the rule is a threshold, not a veto.
    """
    m, session = _finalize({A: "approve", B: "approve", C: "approve", D: "abstain"},
                           participants=(A, B, C, D))

    assert session.status == "approved"
    assert m.commits[0].consensus_type == "majority"
    assert m.commits[0].description == "Approved by 3/4 participants"


def test_the_tally_names_the_abstentions_and_the_participants():
    m, _ = _finalize({A: "approve", B: "abstain"})

    tally = m.results[-1]["vote_tally"]
    assert tally["abstain"] == 1
    assert tally["participants"] == 2
    assert tally["total"] == 2


# --- what a vote may say ----------------------------------------------------


@pytest.mark.asyncio
async def test_an_abstention_without_a_reason_is_refused():
    """It blocks the group, so it has to say why."""
    m = _manager()
    proposal = _proposal((A, B))
    m.sessions["p"] = SimpleNamespace(proposal=proposal, status="voting", votes={},
                                      required_dissenter=None)

    assert not await m.cast_vote("p", "abstain", comment="   ")
    assert not m.sessions["p"].votes


@pytest.mark.asyncio
async def test_a_vote_value_nobody_counts_is_refused():
    """An unknown string used to inflate the denominator and count as nothing."""
    m = _manager()
    proposal = _proposal((A, B))
    m.sessions["p"] = SimpleNamespace(proposal=proposal, status="voting", votes={},
                                      required_dissenter=None)

    assert not await m.cast_vote("p", "maybe")
    assert not m.sessions["p"].votes


# --- who is counted ---------------------------------------------------------


def test_a_vote_from_outside_the_roster_decides_nothing():
    """The denominator is only honest if the numerator is the same people.

    A vote arrives relayed, or from a node that left the group. Counting it
    both approves faster and makes the fraction meaningless — 2 of 3 becomes
    «3 of 3, unanimous».
    """
    stranger = "dpc-node-" + "f" * 32
    m, session = _finalize(
        {A: "approve", B: "approve", stranger: "approve"},
        participants=(A, B, C),
    )

    assert not m.commits, "a stranger's vote carried the commit"
    assert session.status != "approved"


def test_the_roster_decides_unanimity_not_the_share():
    """«Unanimous» has to mean everyone, not «everyone who answered»."""
    m, session = _finalize({A: "approve", B: "approve", C: "approve"},
                           participants=(A, B, C))

    assert session.status == "approved"
    assert m.commits[0].consensus_type == "unanimous"
    assert m.commits[0].description == "Approved by 3/3 participants"


@pytest.mark.asyncio
async def test_an_arriving_vote_nobody_counts_is_discarded():
    """It would enter the tally, approve nothing, and block in silence."""
    m = _manager()
    proposal = _proposal((A, B))
    m.sessions[proposal.proposal_id] = SimpleNamespace(
        proposal=proposal, status="voting", votes={}, required_dissenter=None
    )

    await m.handle_vote_message(B, {"proposal_id": proposal.proposal_id, "vote": "yes"})

    assert not m.sessions[proposal.proposal_id].votes


@pytest.mark.asyncio
async def test_an_arriving_abstention_without_a_reason_is_discarded():
    m = _manager()
    proposal = _proposal((A, B))
    m.sessions[proposal.proposal_id] = SimpleNamespace(
        proposal=proposal, status="voting", votes={}, required_dissenter=None
    )

    await m.handle_vote_message(
        B, {"proposal_id": proposal.proposal_id, "vote": "abstain", "comment": ""}
    )

    assert not m.sessions[proposal.proposal_id].votes


def test_a_stranger_cannot_fill_a_participant_seat():
    """The early finish asks whether the roster answered, not how many votes.

    Counting votes made a proposal finalisable while a real participant was
    still silent, as soon as some other node's vote arrived — the same
    confusion that put strangers into the fraction.
    """
    from dpc_client_core.consensus_manager import _everyone_answered

    stranger = "dpc-node-" + "f" * 32
    proposal = _proposal((A, B))
    session = SimpleNamespace(
        proposal=proposal,
        votes={A: object(), stranger: object()},
    )

    assert not _everyone_answered(session)

    session.votes[B] = object()
    assert _everyone_answered(session)
