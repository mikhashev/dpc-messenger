"""ADR-053 point 3, acceptance check 16.

A peer's APPLY_KNOWLEDGE_COMMIT for a proposal this node never voted on waits for an owner
card and is not applied on refusal; a 1:1 proposal counts only from one of the owner's own
nodes (the union of every agent profile's own_node_group, since knowledge is the node's).
A revision turn that pastes a foreign voter's comment has that voter as its asker.
"""
import asyncio
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent.tools import core as tools_core
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.message_handlers.knowledge_handler import ApplyKnowledgeCommitHandler
from dpc_protocol.knowledge_commit import CommitProvenance, KnowledgeCommit
from dpc_protocol.pcm_core import KnowledgeEntry, KnowledgeSource

ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
OWN = "dpc-node-" + "d" * 32
GROUP = "group-0a52389f2bb6"


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(KnowledgeCommit, "verify_provenance",
                        lambda self, peers_dir=None: CommitProvenance("verified", "test"))
    monkeypatch.setattr(tools_core, "_SCHEDULE_APPROVAL_TTL_SECONDS", 0.3)


def _commit():
    commit = KnowledgeCommit(
        topic="hardware", summary="12 GB", conversation_id=GROUP,
        entries=[KnowledgeEntry(content="12 GB VRAM", tags=["gpu"],
                                source=KnowledgeSource(type="ai_summary"))],
        participants=[ME, PEER], approved_by=[ME, PEER], proposed_by=PEER,
        timestamp="2026-11-25T10:00:00+00:00",
    )
    commit.compute_hash()
    return commit


class _LocalApi:
    has_clients = True

    async def broadcast_event(self, name, payload):
        pass


def _handler(policy, judged=False):
    applied, cards = [], []

    async def _apply(commit, origin="local", judged_here=True):
        applied.append(judged_here)
        return True

    async def announce(**kwargs):
        cards.append(kwargs)
        if policy in ("approve", "refuse"):
            tools_core.resolve_schedule_approval(kwargs["request_id"], policy == "approve")

    async def closed(**kwargs):
        pass

    def roster(conversation_id, sender):
        return [ME, PEER] if conversation_id == GROUP else None

    service = SimpleNamespace(
        consensus_manager=SimpleNamespace(_apply_commit=_apply, peer_proposal_roster=roster),
        knowledge_service=SimpleNamespace(judged_proposal=lambda pid: judged),
        pcm_core=SimpleNamespace(load_context=lambda: SimpleNamespace(commit_history=[])),
        pending_certificate_requests=set(),
        p2p_manager=SimpleNamespace(node_id=ME, peers={PEER: object()}, send_message_to_peer=AsyncMock(),
                                    get_display_name=lambda: "Mike"),
        local_api=_LocalApi(),
        announce_schedule_approval_request=announce,
        announce_schedule_approval_closed=closed,
        _get_agent_telegram_bridge=lambda agent_id: None,
        _peer_name=lambda node: "Miron",
    )
    return ApplyKnowledgeCommitHandler(service), applied, cards


@pytest.mark.asyncio
async def test_an_unjudged_commit_waits_for_a_card_and_a_refusal_applies_nothing():
    handler, applied, cards = _handler("refuse")
    await handler.handle(PEER, _commit().to_dict())
    assert applied == []
    assert len(cards) == 1 and cards[0]["task_type"] == "knowledge_commit"
    assert cards[0]["asker"]["node_id"] == PEER and "hardware" in cards[0]["details"]


@pytest.mark.asyncio
async def test_an_unanswered_card_applies_nothing():
    handler, applied, cards = _handler("none")
    await handler.handle(PEER, _commit().to_dict())
    assert applied == [] and len(cards) == 1


@pytest.mark.asyncio
async def test_an_approved_unjudged_commit_is_applied_unsigned():
    handler, applied, cards = _handler("approve")
    await handler.handle(PEER, _commit().to_dict())
    assert applied == [False] and len(cards) == 1


@pytest.mark.asyncio
async def test_a_commit_this_node_voted_on_needs_no_card():
    handler, applied, cards = _handler("none", judged=True)
    await handler.handle(PEER, _commit().to_dict())
    assert applied == [True] and cards == []


def _knowledge_service(own_group_members):
    fake = types.SimpleNamespace(
        p2p_manager=SimpleNamespace(node_id=ME),
        group_manager=SimpleNamespace(get_group=lambda gid: None),
        firewall=SimpleNamespace(
            node_groups={"my_machines": list(own_group_members), "friends": [PEER]},
            rules={"agent_profiles": {
                "agent_ark": {"memory": {"own_node_group": "my_machines"}},
                "agent_johnny": {"memory": {}},
            }},
        ),
    )
    fake._own_nodes = lambda: KnowledgeService._own_nodes(fake)
    return fake


def test_a_one_to_one_proposal_from_a_node_outside_every_own_group_is_refused():
    fake = _knowledge_service([OWN])
    assert KnowledgeService._local_roster_for_peer_proposal(fake, ME, PEER) is None


def test_a_one_to_one_proposal_from_an_own_node_opens_its_vote():
    fake = _knowledge_service([OWN])
    assert KnowledgeService._local_roster_for_peer_proposal(fake, ME, OWN) == [OWN, ME]


def test_no_own_node_group_means_no_one_to_one_proposal_counts():
    fake = _knowledge_service([])
    fake.firewall.rules = {"agent_profiles": {}}
    assert KnowledgeService._local_roster_for_peer_proposal(fake, ME, OWN) is None


@pytest.mark.asyncio
async def test_a_revision_that_pastes_a_foreign_comment_has_that_voter_as_its_asker():
    seen = {}

    class _Manager:
        agent_id = "agent_ark"

        async def process_message(self, **kwargs):
            seen.update(kwargs)
            return ""

    fake = _knowledge_service([OWN])
    fake.consensus_manager = SimpleNamespace(is_own_proposal=lambda pid: True)
    proposal = SimpleNamespace(proposal_id="p1", topic="t", conversation_id=GROUP)
    await KnowledgeService._agent_auto_revise_proposal(
        fake, _Manager(), proposal, [{"node_id": PEER, "comment": "please add X"}])
    assert seen["asker"].node_id == PEER and not seen["asker"].is_owner

    seen.clear()
    await KnowledgeService._agent_auto_revise_proposal(
        fake, _Manager(), proposal, [{"node_id": OWN, "comment": "fine"}])
    assert "asker" not in seen
