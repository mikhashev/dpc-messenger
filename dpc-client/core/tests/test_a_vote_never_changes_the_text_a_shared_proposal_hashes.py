"""A vote must not change the text of a proposal that other nodes also hold.

The card ONE-KNOWLEDGE-PROPOSAL-BECOMES-TWO-COMMITS-WHEN-A-VOTE-CARRIES-REBUILT-ENTRIES: the
vote dialog sent the entries back with every click, and the backend rebuilt each one naming
seven fields, so `cultural_specific` (a hash input) fell back to False on the node voting from
the UI while a node voting by the Telegram button, which sends no text, kept True. Two commit
ids, and neither node accepts the other's signature.

The rules now: an unchanged text is no edit and leaves the hash alone, an edit keeps every
field it did not touch, and an edit is refused outright when anyone else takes part in the
vote, because an edit lives on this node's copy only.
"""

import copy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from dpc_client_core import knowledge_service as ks
from dpc_client_core.consensus_manager import ConsensusManager, VotingSession
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_protocol.knowledge_commit import KnowledgeCommitProposal
from dpc_protocol.pcm_core import KnowledgeEntry, KnowledgeSource

ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
PID = "proposal-edit01"


def _proposal(participants):
    source = KnowledgeSource(type="ai_summary", conversation_id="group-x", participants=participants,
                             extraction_model="m", extraction_host="local")
    return KnowledgeCommitProposal(
        proposal_id=PID, conversation_id="group-x", topic="t", summary="the summary",
        entries=[
            KnowledgeEntry(
                content="a culturally specific fact", tags=["b", "a"], source=source, confidence=0.9,
                cultural_specific=True, requires_context=["Western workplace"],
                alternative_viewpoints=["another view"], usage_count=3, effectiveness_score=0.5,
            ),
            KnowledgeEntry(content="a plain fact", tags=["c"], source=source, confidence=0.8),
        ],
        participants=participants, parent_commit_id="commit-0123456789abcdef",
        timestamp="2026-11-16T10:00:00+00:00",
    )


def _node(proposal, node_id=ME):
    """A service over a real ConsensusManager whose one session is `proposal`."""
    consensus = ConsensusManager(node_id, SimpleNamespace(), consensus_threshold=0.75)
    consensus.commits = []

    async def _apply(commit, origin="local"):
        commit.compute_hash()
        consensus.commits.append(commit)
        return True

    consensus._apply_commit = _apply
    consensus.sessions[proposal.proposal_id] = VotingSession(proposal=proposal)

    async def _nobody(message):
        pass

    svc = KnowledgeService.__new__(KnowledgeService)
    svc.p2p_manager = SimpleNamespace(node_id=node_id, peers={})
    svc.consensus_manager = consensus
    svc.conversation_monitors = {}
    svc._judged_proposals = {}
    svc._broadcast_to_peers = _nobody
    return svc, consensus


def _as_the_dialog_sends_them(proposal):
    """The entries as the UI holds and returns them: the proposal's own JSON."""
    return copy.deepcopy(proposal.to_dict()["entries"])


# --- one proposal, one hash, whichever way the vote arrived ---------------------------------


@pytest.mark.asyncio
async def test_a_vote_that_sends_the_text_back_hashes_as_one_that_sends_none():
    via_ui, ui_consensus = _node(_proposal([ME]))
    via_button, button_consensus = _node(_proposal([ME]))
    proposal = ui_consensus.sessions[PID].proposal

    await via_ui.vote_knowledge_commit(
        PID, "approve", entries=_as_the_dialog_sends_them(proposal), summary=proposal.summary
    )
    await via_button.vote_knowledge_commit(PID, "approve")  # the Telegram button sends no text

    [from_ui] = ui_consensus.commits
    [from_button] = button_consensus.commits
    assert from_ui.entries[0].cultural_specific is True
    assert from_ui.commit_hash == from_button.commit_hash
    assert from_ui.commit_id == from_button.commit_id


@pytest.mark.asyncio
async def test_an_unchanged_text_leaves_every_field_of_every_entry_alone():
    svc, consensus = _node(_proposal([ME]))
    proposal = consensus.sessions[PID].proposal
    before = [asdict(e) for e in proposal.entries]

    await svc.vote_knowledge_commit(PID, "approve", entries=_as_the_dialog_sends_them(proposal))

    assert [asdict(e) for e in proposal.entries] == before


@pytest.mark.asyncio
async def test_an_unchanged_text_is_accepted_when_others_take_part():
    """A dialog from before this change still returns the text with every vote."""
    svc, consensus = _node(_proposal([ME, PEER]))
    proposal = consensus.sessions[PID].proposal

    result = await svc.vote_knowledge_commit(
        PID, "approve", entries=_as_the_dialog_sends_them(proposal), summary=proposal.summary
    )

    assert result["status"] == "success"
    assert ME in consensus.sessions[PID].votes


# --- an edit is for a proposal this node votes on alone --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["entry", "summary"])
async def test_a_proposal_with_other_participants_refuses_an_edited_text(edit):
    svc, consensus = _node(_proposal([ME, PEER]))
    proposal = consensus.sessions[PID].proposal
    before = copy.deepcopy(proposal)
    entries = _as_the_dialog_sends_them(proposal)
    summary = proposal.summary
    if edit == "entry":
        entries[0]["content"] = "an edited fact"
    else:
        summary = "an edited summary"

    result = await svc.vote_knowledge_commit(PID, "approve", entries=entries, summary=summary)

    assert result["status"] == "error"
    assert result["reason"] == ks.EDIT_REFUSED_REASON
    assert result["message"] == ks.EDIT_REFUSED_MESSAGE
    assert consensus.sessions[PID].votes == {}, "a refused vote must not be cast"
    assert proposal == before, "a refused edit must not touch the proposal"


@pytest.mark.asyncio
async def test_the_refusal_covers_every_kind_of_vote():
    for vote in ("approve", "reject", "request_changes"):
        svc, consensus = _node(_proposal([ME, PEER]))
        entries = _as_the_dialog_sends_them(consensus.sessions[PID].proposal)
        entries[1]["tags"] = ["changed"]

        result = await svc.vote_knowledge_commit(PID, vote, entries=entries)

        assert result["reason"] == ks.EDIT_REFUSED_REASON, vote


@pytest.mark.asyncio
async def test_a_solo_proposal_takes_an_edit_and_commits_the_edited_text():
    svc, consensus = _node(_proposal([ME]))
    proposal = consensus.sessions[PID].proposal
    entries = _as_the_dialog_sends_them(proposal)
    entries[0]["content"] = "an edited fact"
    entries[0]["edited_by"] = ME
    entries[0]["edited_at"] = "2026-11-16T11:00:00+00:00"

    result = await svc.vote_knowledge_commit(
        PID, "approve", entries=entries, summary="an edited summary"
    )

    assert result["status"] == "success"
    [commit] = consensus.commits
    assert commit.summary == "an edited summary"
    assert [e.content for e in commit.entries] == ["an edited fact", "a plain fact"]
    # What the edit did not touch is still what was proposed.
    edited = commit.entries[0]
    assert edited.cultural_specific is True
    assert edited.requires_context == ["Western workplace"]
    assert edited.alternative_viewpoints == ["another view"]
    assert edited.usage_count == 3 and edited.effectiveness_score == 0.5
    assert edited.edited_by == ME
    assert isinstance(edited.source, KnowledgeSource) and edited.source.extraction_model == "m"


# --- a closed vote says so, whatever text the late vote carries ------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("participants", [[ME], [ME, PEER]])
async def test_an_edited_vote_on_a_closed_session_is_answered_already_decided(participants):
    svc, consensus = _node(_proposal(participants))
    session = consensus.sessions[PID]
    session.status = "rejected"
    before = copy.deepcopy(session.proposal)
    entries = _as_the_dialog_sends_them(session.proposal)
    entries[0]["content"] = "an edited fact"

    result = await svc.vote_knowledge_commit(PID, "approve", entries=entries, summary="an edited summary")

    assert result["status"] == "error"
    assert result["reason"] == "already_decided"
    assert result["decided_as"] == "rejected"
    assert session.proposal == before, "a decided proposal must not be edited"
