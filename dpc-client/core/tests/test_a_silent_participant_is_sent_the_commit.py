"""A participant that never ACKs has to be sent the commit itself.

`ApplyKnowledgeCommitHandler` has existed since the Gap 3 work and says in its own
docstring what would trigger it: «after a COMMIT_ACK timeout, the proposer can
automatically retransmit via this command to nodes that did not ACK». Nothing did.
Measured 2026-09-02: `ApplyKnowledgeCommitMessage.create` had no caller in
`dpc_client_core` or `dpc-protocol` — definition and handler, no sender.

What that cost is not hypothetical. `THE-PROVENANCE-GATE-HAS-NEVER-SEEN-A-COMMIT-FROM-A-SECOND-NODE`
has been open since 2026-08-27 waiting for a commit to arrive over this path, and
Mike's live two-node runs of 2026-09-01 and 2026-09-02 both went PROPOSE → VOTE →
each node mints its own → COMMIT_ACK, with `APPLY_KNOWLEDGE_COMMIT` appearing zero
times. The entry looked like it was waiting for a second machine; it was waiting
for a sender.

The failure the path exists for: a node whose `_apply_commit` raised — disk full,
crash, power loss — never ACKs, and cannot ask for what it does not know exists.
"""

import asyncio
from types import SimpleNamespace

import pytest

from dpc_client_core.consensus_manager import ConsensusManager
from dpc_protocol.knowledge_commit import (
    ApplyKnowledgeCommitMessage,
    KnowledgeCommit,
    KnowledgeEntry,
)

A = "dpc-node-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "dpc-node-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
C = "dpc-node-cccccccccccccccccccccccccccccccc"


def _commit():
    return KnowledgeCommit(
        commit_id="commit-6bd4dcc3181a1f34",
        topic="DPC peer URIs and Russian greetings",
        entries=[KnowledgeEntry(content="a dpc:// URI is a peer endpoint", tags=["DPC"])],
        participants=[A, B, C],
        approved_by=[A, B, C],
        timestamp="2026-09-02T08:22:43.927874+00:00",
    )


def _manager(node_id=A):
    """A ConsensusManager reduced to the ACK bookkeeping and the window."""
    m = ConsensusManager.__new__(ConsensusManager)
    m.node_id = node_id
    m.commit_acks = {}
    m.ack_retransmit_seconds = 0  # the window is not what is under test
    m._retransmit_tasks = set()
    m.sent = []

    async def _retransmit(commit, node_ids):
        m.sent.append((commit, list(node_ids)))

    m.on_apply_retransmit = _retransmit
    return m


def test_the_participant_that_never_acked_is_the_one_retransmitted_to():
    commit = _commit()
    m = _manager()
    # B confirmed; C never did.
    m.record_commit_ack(commit.commit_id, B, commit.participants)

    asyncio.run(m._retransmit_after_ack_window(commit))

    assert m.sent, "the ACK window closed on a silent participant and nothing was sent"
    _, targets = m.sent[0]
    assert targets == [C], f"expected the silent participant only, got {targets}"


def test_a_fully_confirmed_commit_is_not_retransmitted():
    """The control. Without it, a retransmit to everybody would also pass above."""
    commit = _commit()
    m = _manager()
    m.record_commit_ack(commit.commit_id, B, commit.participants)
    m.record_commit_ack(commit.commit_id, C, commit.participants)

    asyncio.run(m._retransmit_after_ack_window(commit))

    assert not m.sent, f"every participant confirmed and we still sent {m.sent}"


def test_we_never_count_ourselves_as_silent():
    """This node applied the commit; it does not ACK to itself and is not a target."""
    commit = _commit()
    m = _manager(node_id=A)

    silent = m.silent_participants(commit)

    assert A not in silent
    assert set(silent) == {B, C}


def test_the_retransmitted_message_carries_a_commit_that_survives_the_wire():
    """What is sent has to be readable as the same commit on the far side.

    The receiving handler runs `KnowledgeCommit.from_dict(payload)` and then
    `verify_provenance()`, so a payload that loses the identity fields would be
    refused as somebody else's commit rather than applied.
    """
    commit = _commit()

    message = ApplyKnowledgeCommitMessage.create(commit)
    received = KnowledgeCommit.from_dict(message.payload)

    assert message.command == "APPLY_KNOWLEDGE_COMMIT"
    assert received.commit_id == commit.commit_id
    assert received.timestamp == commit.timestamp
    assert received.participants == commit.participants


def test_a_commit_that_arrived_over_the_recovery_path_does_not_re_arm_the_window():
    """Two nodes must not hand the same commit back and forth.

    `_apply_commit` arms the retransmit through `arm_ack_retransmit`, and that one
    only fires for a commit this node minted itself. A commit applied from an
    incoming APPLY_KNOWLEDGE_COMMIT carries the provenance verdict as its origin
    («verified» / «unverified») and must arm nothing.
    """
    commit = _commit()

    async def _run():
        m = _manager()
        # The real decision, not a copy of it in the test.
        received = m.arm_ack_retransmit(commit, origin="verified")
        minted = m.arm_ack_retransmit(commit, origin="local")
        await asyncio.sleep(0)  # let the armed task reach its first await
        return received, minted, m

    received, minted, m = asyncio.run(_run())

    assert received is False, "a commit received over the recovery path re-armed the retransmit"
    assert minted is True, "a commit we minted ourselves did not arm the window"


def test_an_armed_window_is_held_so_it_cannot_be_collected_mid_sleep():
    """A task nobody references may be garbage-collected before it fires."""
    commit = _commit()

    async def _run():
        m = _manager()
        m.ack_retransmit_seconds = 30  # long enough that it is still sleeping
        m.arm_ack_retransmit(commit, origin="local")
        held = len(m._retransmit_tasks)
        for task in list(m._retransmit_tasks):
            task.cancel()
        return held

    assert asyncio.run(_run()) == 1
