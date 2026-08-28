"""A proposal arriving twice used to wipe every vote already recorded.

`handle_proposal_message` rebuilt the session unconditionally — new object,
`votes={initiator: True}`, straight into `active_sessions` — plus a second
timeout task. So a copy arriving by another path reset the count to one and
took this node's own vote with it, silently. The dedup key that looks like it
guards this (`ses:{proposal_id}`) sits in the handler *after* the call and only
guards the relay.

Invisible on the current three-node star because a star has no cycles: every
message arrives once. It surfaces the moment two nodes can both reach a third
— which is what fixing the reachability pre-flight will produce.

Found by Fable 5 in the external review of 2026-08-07 (B5).
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.session_manager import NewSessionProposalManager

ME = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32
CAROL = "dpc-node-" + "c" * 32
GROUP = "group-1234567890ab"


def _manager():
    core = SimpleNamespace(
        p2p_manager=SimpleNamespace(node_id=ME),
        local_api=SimpleNamespace(broadcast_event=_noop),
    )
    manager = NewSessionProposalManager(core)
    manager.on_proposal_received = _noop
    return manager


def _payload():
    return {
        "proposal_id": "p1",
        "initiator_node_id": BOB,
        "conversation_id": GROUP,
        "timestamp": "2026-08-07T02:15:52Z",
        "participants": [ME, BOB, CAROL],
    }


@pytest.mark.asyncio
async def test_a_duplicate_proposal_does_not_reset_the_count():
    """The defect verbatim: our own vote is gone after the second copy."""
    manager = _manager()
    await manager.handle_proposal_message(BOB, _payload())
    await manager.record_vote("p1", ME, True)
    assert len(manager.active_sessions["p1"].proposal.votes) == 2

    await manager.handle_proposal_message(CAROL, _payload())  # relayed second copy

    assert manager.active_sessions["p1"].proposal.votes == {BOB: True, ME: True}


@pytest.mark.asyncio
async def test_a_duplicate_does_not_start_a_second_timeout():
    """Two timers on one proposal finalise it twice — the second on an empty
    session, which is how a finalised vote gets re-announced."""
    manager = _manager()
    await manager.handle_proposal_message(BOB, _payload())
    first_task = manager.active_sessions["p1"].timeout_task

    await manager.handle_proposal_message(CAROL, _payload())

    assert manager.active_sessions["p1"].timeout_task is first_task
    first_task.cancel()


@pytest.mark.asyncio
async def test_the_first_copy_still_creates_the_session():
    """Refusing every proposal would satisfy both tests above."""
    manager = _manager()

    await manager.handle_proposal_message(BOB, _payload())

    session = manager.active_sessions["p1"]
    assert session.proposal.votes == {BOB: True}
    assert session.proposal.participants == {ME, BOB, CAROL}
    session.timeout_task.cancel()


@pytest.mark.asyncio
async def test_a_different_proposal_is_not_mistaken_for_a_duplicate():
    manager = _manager()
    await manager.handle_proposal_message(BOB, _payload())

    second = _payload()
    second["proposal_id"] = "p2"
    await manager.handle_proposal_message(CAROL, second)

    assert set(manager.active_sessions) == {"p1", "p2"}
    for session in manager.active_sessions.values():
        session.timeout_task.cancel()


async def _noop(*args, **kwargs):
    return None
