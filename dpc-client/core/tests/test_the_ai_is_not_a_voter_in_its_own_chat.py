"""Only people vote on Extract Knowledge; the AI in its own chat is not a voter.

ADR-009 says the AI provider in a Local AI chat is not a voter, but the code gave it a
second vote: after the owner voted, a model call decided and cast one by swapping the
shared `consensus_manager.node_id` across an await, so an owner's vote in that window was
credited to the AI. The vote is gone, and with it the reason to list the AI as a
participant: a proposal that named it would wait for a vote that never comes.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.consensus_manager import ConsensusManager
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_protocol.knowledge_commit import KnowledgeCommitProposal, KnowledgeEntry

ME = "dpc-node-" + "a" * 32


def test_no_ai_vote_is_left_in_the_code():
    for name in ("_ai_agent_vote_on_proposal", "_evaluate_knowledge_proposal_for_ai_vote"):
        assert not hasattr(KnowledgeService, name)


def _stop_deadline_timers():
    # propose_commit starts a ten-minute deadline timer; a test must not leave it behind.
    for task in asyncio.all_tasks() - {asyncio.current_task()}:
        task.cancel()


def _service(chat_id, proposal):
    """A service that gets a real extraction through to a real ConsensusManager."""
    consensus = ConsensusManager(ME, MagicMock(), consensus_threshold=0.75)
    consensus.pcm_core.load_context.side_effect = RuntimeError("no context in this test")
    consensus.on_commit_approved = AsyncMock()
    consensus._apply_commit = AsyncMock(return_value=True)  # the disk write is not what is tested

    monitor = SimpleNamespace(
        full_conversation=[], message_buffer=["a message"], knowledge_score=0.0,
        rebuild_extraction_buffers_from_history=lambda: None,
        generate_commit_proposal=AsyncMock(return_value=proposal),
        last_compute_refusal=None,
    )
    svc = KnowledgeService.__new__(KnowledgeService)
    svc.p2p_manager = SimpleNamespace(node_id=ME, peers={})
    svc.consensus_manager = consensus
    svc.conversation_monitors = {}
    svc.llm_manager = SimpleNamespace(providers={})
    svc._get_or_create_conversation_monitor = lambda cid: monitor
    svc._resolve_extraction_target = lambda m: None
    svc._judged_proposals = {}
    svc.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    svc._broadcast_to_peers = AsyncMock()
    svc._broadcast_to_group = AsyncMock()
    return svc, consensus


def _proposal(chat_id, participants=None):
    # What `_build_participants` hands the monitor for an AI chat: the owner and the chat id.
    return KnowledgeCommitProposal(
        proposal_id="proposal-ai01", conversation_id=chat_id, topic="t", summary="s",
        entries=[KnowledgeEntry(content="a fact", tags=["x"])],
        participants=participants if participants is not None else [ME, chat_id],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id", ["ai_chat_1700000000_ab12cd", "local_ai", "agent_001"])
async def test_the_owners_single_approve_decides_an_ai_chat_proposal(chat_id):
    svc, consensus = _service(chat_id, _proposal(chat_id))
    try:
        await _the_owners_single_approve_decides(svc, consensus, chat_id)
    finally:
        _stop_deadline_timers()


async def _the_owners_single_approve_decides(svc, consensus, chat_id):
    opened = await svc.end_conversation_session(chat_id, extract_consent=True)
    assert opened["status"] == "success", opened
    session = consensus.sessions["proposal-ai01"]
    assert session.proposal.participants == [ME], "the AI chat must not be a seat in the vote"

    result = await svc.vote_knowledge_commit("proposal-ai01", "approve")

    assert result["status"] == "success"
    assert session.status == "approved", "waited for a second vote or the deadline"
    consensus.on_commit_approved.assert_awaited_once()
    assert list(session.votes) == [ME]
    assert consensus.node_id == ME


@pytest.mark.asyncio
async def test_a_vote_in_an_ai_chat_starts_no_model_call():
    chat_id = "ai_chat_1700000000_ab12cd"
    svc, consensus = _service(chat_id, _proposal(chat_id))
    svc.llm_manager = SimpleNamespace(providers={}, query=AsyncMock())
    try:
        await svc.end_conversation_session(chat_id, extract_consent=True)

        await svc.vote_knowledge_commit("proposal-ai01", "approve")
        await asyncio.sleep(0)

        svc.llm_manager.query.assert_not_awaited()
    finally:
        _stop_deadline_timers()



# --- Telegram chats are solo too (card G8) --------------------------------------------------
# `telegram-<chat>` is a plain Telegram chat, `agent-<agent_id>` one linked to an agent (hyphen;
# DPC's own agent chats are `agent_...`). The coordinator names only the bot as participant, and
# the bot never votes, so the proposal used to hang until the deadline, or leak to every peer.

TELEGRAM_CHATS = [
    ("telegram-123", ["telegram-bot-123"]),
    ("agent-agent_001", ["telegram-bot-123"]),
    ("telegram-123", [ME, "telegram-123"]),
    ("agent-agent_001", [ME, "agent-agent_001"]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id,roster", TELEGRAM_CHATS)
async def test_the_owners_single_approve_decides_a_telegram_chat_proposal(chat_id, roster):
    svc, consensus = _service(chat_id, _proposal(chat_id, roster))
    try:
        await _the_owners_single_approve_decides(svc, consensus, chat_id)
    finally:
        _stop_deadline_timers()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id,roster", TELEGRAM_CHATS)
async def test_a_telegram_chat_proposal_and_its_vote_never_reach_a_peer(chat_id, roster):
    svc, consensus = _service(chat_id, _proposal(chat_id, roster))
    try:
        await svc.end_conversation_session(chat_id, extract_consent=True)
        await svc.vote_knowledge_commit("proposal-ai01", "approve")
    finally:
        _stop_deadline_timers()

    svc._broadcast_to_peers.assert_not_awaited()
    svc._broadcast_to_group.assert_not_awaited()
