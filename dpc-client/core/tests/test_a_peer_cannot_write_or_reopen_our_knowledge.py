"""The two other doors of the knowledge hole closed in 702b0510.

APPLY_KNOWLEDGE_COMMIT applied any commit a peer sent, for any chat, so one
message put knowledge into the PCM context. KNOWLEDGE_COMMIT_RESULT set the
status of our session to whatever any peer wrote, "voting" included, and a
reopened session counts the next vote to a second finalize and a second apply.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dpc_client_core.consensus_manager import ConsensusManager, VotingSession
from dpc_client_core.message_handlers.knowledge_handler import (
    ApplyKnowledgeCommitHandler,
    KnowledgeCommitResultHandler,
)
from dpc_protocol.knowledge_commit import (
    CommitProvenance,
    KnowledgeCommit,
    KnowledgeCommitProposal,
)
from dpc_protocol.pcm_core import KnowledgeEntry, KnowledgeSource, PCMCore

ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
OTHER = "dpc-node-" + "c" * 32
GROUP = "group-0a52389f2bb6"


@pytest.fixture(autouse=True)
def _votes_are_unsigned(monkeypatch):
    from dpc_client_core import signing
    monkeypatch.setattr(signing, "node_signer", lambda: None)


@pytest.fixture
def every_commit_verifies(monkeypatch):
    """Provenance out of the way, so a refusal below is the membership gate's."""
    monkeypatch.setattr(
        KnowledgeCommit, "verify_provenance",
        lambda self, peers_dir=None: CommitProvenance("verified", "test"),
    )


def _roster(groups):
    def resolve(conversation_id, sender):
        members = groups.get(conversation_id)
        if members and ME in members and sender in members:
            return list(members)
        return None
    return resolve


# --- APPLY_KNOWLEDGE_COMMIT ----------------------------------------------

def _commit(conversation_id=GROUP, participants=(ME, PEER)):
    commit = KnowledgeCommit(
        topic="hardware", summary="12 GB", conversation_id=conversation_id,
        entries=[KnowledgeEntry(content="12 GB VRAM", tags=["gpu"],
                                source=KnowledgeSource(type="ai_summary"))],
        participants=list(participants), approved_by=list(participants),
        proposed_by=PEER, timestamp="2026-11-25T10:00:00+00:00",
    )
    commit.compute_hash()
    return commit


def _apply_handler(groups=None, resolver=True):
    applied = []

    async def _apply(commit, origin="local", judged_here=True):
        applied.append((commit.commit_id, origin))
        return True

    consensus = SimpleNamespace(_apply_commit=_apply)
    if resolver:
        consensus.peer_proposal_roster = _roster(groups if groups is not None else {GROUP: [ME, PEER]})
    service = SimpleNamespace(
        consensus_manager=consensus,
        pcm_core=SimpleNamespace(load_context=lambda: SimpleNamespace(commit_history=[])),
        pending_certificate_requests=set(),
        p2p_manager=SimpleNamespace(node_id=ME, peers={PEER: object()},
                                    send_message_to_peer=AsyncMock()),
    )
    return ApplyKnowledgeCommitHandler(service), applied


@pytest.mark.asyncio
async def test_a_retransmit_of_a_shared_chat_commit_is_applied(every_commit_verifies):
    handler, applied = _apply_handler()
    commit = _commit()

    await handler.handle(PEER, commit.to_dict())

    assert applied == [(commit.commit_id, "verified")]


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", ["agent_001", "local_ai", "telegram-42", "group-unknown00"])
async def test_a_commit_for_a_chat_we_do_not_share_is_refused(chat, every_commit_verifies, caplog):
    handler, applied = _apply_handler()

    await handler.handle(PEER, _commit(chat).to_dict())

    assert applied == []
    assert any(PEER in r.getMessage() and chat in r.getMessage()
               for r in caplog.records if r.levelname == "WARNING")


@pytest.mark.asyncio
async def test_a_commit_from_a_non_member_of_the_group_is_refused(every_commit_verifies):
    handler, applied = _apply_handler({GROUP: [ME, OTHER]})

    await handler.handle(PEER, _commit(participants=(ME, OTHER, PEER)).to_dict())

    assert applied == []


@pytest.mark.asyncio
async def test_a_commit_that_does_not_name_us_is_refused(every_commit_verifies):
    handler, applied = _apply_handler({GROUP: [ME, PEER, OTHER]})

    await handler.handle(PEER, _commit(participants=(PEER, OTHER)).to_dict())

    assert applied == []


@pytest.mark.asyncio
async def test_without_a_resolver_nothing_is_applied(every_commit_verifies):
    handler, applied = _apply_handler(resolver=False)

    await handler.handle(PEER, _commit().to_dict())

    assert applied == []


# --- KNOWLEDGE_COMMIT_RESULT ---------------------------------------------

def _proposal(participants=(ME, PEER)):
    return KnowledgeCommitProposal(
        proposal_id="proposal-0001", conversation_id=GROUP, topic="t", summary="s",
        entries=[KnowledgeEntry(content="a fact", tags=["x"])],
        participants=list(participants), proposed_by=ME,
        parent_commit_id="commit-0123456789abcdef",
        timestamp="2026-11-25T10:00:00+00:00",
    )


def _consensus():
    consensus = ConsensusManager(ME, SimpleNamespace(), consensus_threshold=0.75)
    consensus.applied = []

    async def _apply(commit, origin="local", judged_here=True):
        commit.compute_hash()
        consensus.applied.append(commit.commit_id)
        return True

    consensus._apply_commit = _apply
    consensus.on_result_broadcast = AsyncMock()
    return consensus


def _result_handler(consensus, groups=None):
    sent = []

    async def _send(node_id, message):
        sent.append(node_id)

    service = SimpleNamespace(
        consensus_manager=consensus,
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        _processed_message_ids=set(),
        group_manager=SimpleNamespace(get_group=lambda gid: SimpleNamespace(
            members=list((groups or {GROUP: [ME, PEER, OTHER]}).get(gid, ())))),
        p2p_manager=SimpleNamespace(node_id=ME, peers={PEER: object(), OTHER: object()},
                                    send_message_to_peer=_send),
    )
    return KnowledgeCommitResultHandler(service), service, sent


def _result(status):
    return {"proposal_id": "proposal-0001", "status": status}


@pytest.mark.asyncio
async def test_a_result_from_a_non_participant_changes_nothing_and_goes_nowhere():
    consensus = _consensus()
    consensus.sessions["proposal-0001"] = VotingSession(proposal=_proposal())
    handler, service, sent = _result_handler(consensus)

    await handler.handle(OTHER, _result("rejected"))

    assert consensus.sessions["proposal-0001"].status == "voting"
    service.local_api.broadcast_event.assert_not_awaited()
    assert sent == []


@pytest.mark.asyncio
async def test_a_participants_result_closes_an_open_session():
    consensus = _consensus()
    consensus.sessions["proposal-0001"] = VotingSession(proposal=_proposal())
    handler, service, _sent = _result_handler(consensus)

    await handler.handle(PEER, _result("rejected"))

    assert consensus.sessions["proposal-0001"].status == "rejected"
    service.local_api.broadcast_event.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", ["approved", "rejected", "timeout", "apply_failed"])
@pytest.mark.parametrize("claimed", ["voting", "approved", "rejected", "finalizing"])
async def test_a_result_never_moves_a_closed_session(closed, claimed):
    consensus = _consensus()
    consensus.sessions["proposal-0001"] = VotingSession(proposal=_proposal(), status=closed)
    handler, _service, _sent = _result_handler(consensus)

    await handler.handle(PEER, _result(claimed))

    assert consensus.sessions["proposal-0001"].status == closed


@pytest.mark.asyncio
async def test_a_result_cannot_set_an_open_session_to_something_that_is_not_an_outcome():
    consensus = _consensus()
    consensus.sessions["proposal-0001"] = VotingSession(proposal=_proposal())
    handler, _service, _sent = _result_handler(consensus)

    await handler.handle(PEER, _result("finalizing"))

    assert consensus.sessions["proposal-0001"].status == "voting"


@pytest.mark.asyncio
async def test_an_approved_proposal_is_not_applied_a_second_time():
    """The PoC shape: approve, then a result saying «voting», then a vote again."""
    consensus = _consensus()
    proposal = _proposal()
    consensus.sessions[proposal.proposal_id] = VotingSession(proposal=proposal)
    await consensus.cast_vote(proposal.proposal_id, "approve", broadcast_func=AsyncMock())
    await consensus.handle_vote_message(PEER, {"proposal_id": proposal.proposal_id,
                                               "voter_node_id": PEER, "vote": "approve"})
    assert consensus.applied and consensus.sessions[proposal.proposal_id].status == "approved"
    handler, _service, _sent = _result_handler(consensus)

    await handler.handle(PEER, _result("voting"))
    await consensus.handle_vote_message(PEER, {"proposal_id": proposal.proposal_id,
                                               "voter_node_id": PEER, "vote": "approve"})

    assert len(consensus.applied) == 1


@pytest.mark.asyncio
async def test_a_closed_peer_proposal_is_not_reopened_by_proposing_it_again():
    consensus = _consensus()
    consensus.peer_proposal_roster = _roster({GROUP: [ME, PEER]})
    proposal = _proposal()
    proposal.proposed_by = PEER
    assert await consensus.handle_proposal_message(PEER, proposal.to_dict()) is True
    consensus.sessions[proposal.proposal_id].status = "approved"

    assert await consensus.handle_proposal_message(PEER, proposal.to_dict()) is False
    assert consensus.sessions[proposal.proposal_id].status == "approved"


@pytest.mark.asyncio
async def test_a_revision_still_reopens_a_session_sent_back_for_changes():
    consensus = _consensus()
    consensus.peer_proposal_roster = _roster({GROUP: [ME, PEER]})
    proposal = _proposal()
    proposal.proposed_by = PEER
    await consensus.handle_proposal_message(PEER, proposal.to_dict())
    consensus.sessions[proposal.proposal_id].status = "revision_needed"

    revised = proposal.to_dict()
    revised["summary"] = "s, revised"

    assert await consensus.handle_proposal_message(PEER, revised) is True
    assert consensus.sessions[proposal.proposal_id].status == "voting"
    assert consensus.sessions[proposal.proposal_id].proposal.summary == "s, revised"


# --- the write itself is idempotent --------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    """A DPC home of our own, so nothing here reads or writes the real one."""
    import dpc_protocol.commit_integrity as integrity
    import dpc_protocol.markdown_manager as markdown

    monkeypatch.setattr(integrity, "DPC_HOME_DIR", tmp_path)
    monkeypatch.setattr(markdown, "DPC_HOME_DIR", tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_the_same_commit_applied_twice_is_written_once(home, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = home / "node.key"
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    import dpc_protocol.crypto as crypto
    monkeypatch.setattr(crypto, "load_identity", lambda: (ME, key_path, home / "node.crt"))
    pcm = PCMCore(home / "personal.json")
    pcm.ensure_context_file_exists()
    manager = ConsensusManager(node_id=ME, pcm_core=pcm)
    applied = AsyncMock()
    manager.on_commit_applied = applied

    def _fresh():
        return KnowledgeCommit(
            topic="hardware", summary="12 GB", conversation_id=GROUP,
            entries=[KnowledgeEntry(content="12 GB VRAM", tags=["gpu"],
                                    source=KnowledgeSource(type="ai_summary"))],
            participants=[ME, PEER], approved_by=[ME, PEER], proposed_by=ME,
            parent_commit_id="commit-0123456789abcdef",
            timestamp="2026-11-25T10:00:00+00:00",
        )

    assert await manager._apply_commit(_fresh()) is True
    first = pcm.load_context()
    assert await manager._apply_commit(_fresh()) is True
    second = pcm.load_context()

    assert len(second.commit_history) == len(first.commit_history) == 1
    assert second.knowledge["hardware"].version == first.knowledge["hardware"].version
    assert applied.await_count == 1
