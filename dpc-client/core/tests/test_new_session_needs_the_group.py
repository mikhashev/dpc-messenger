"""A group reset is a group decision, and the majority was counted over the wrong set.

`is_approved` divided by the number of votes *cast* rather than the number of
participants. The initiator's own vote is recorded as approve the moment it
proposes, and the 60-second timeout finalises with whatever arrived instead of
cancelling. One approve out of one cast is a majority of one, so a proposal
nobody answered was approved, and every node that later received the result
archived its history and deleted the file.

The two-participant rule was written correctly (`approve == 2 and total == 2`),
which is why this never showed: it was exercised on the case it got right.

Counting over participants also gives the timeout the right ending without a
second rule — an unanswered proposal simply fails to reach a majority.

What this does NOT fix, stated so nobody reads more into it: a member who was
offline for the vote never learns of the reset, and hands its history back at
the next sync. That needs a decision about what a reset means for an absent
member, not a counting change.
"""

import asyncio
from types import SimpleNamespace

import pytest

from dpc_client_core.session_manager import (
    NewSessionProposal,
    NewSessionProposalManager,
    VotingSession,
)

ME = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32
CAROL = "dpc-node-" + "c" * 32
DAVE = "dpc-node-" + "d" * 32
GROUP = "group-1234567890ab"


def _session(participants, votes):
    proposal = NewSessionProposal(
        proposal_id="p1",
        initiator_node_id=ME,
        conversation_id=GROUP,
        timestamp="2026-08-06T00:00:00Z",
        participants=set(participants),
        votes=dict(votes),
    )
    return VotingSession(proposal=proposal, is_initiator=True)


# --- the counting rule ------------------------------------------------------


def test_the_initiator_alone_does_not_carry_a_group_of_three():
    """The defect verbatim: one auto-vote, nobody else answers."""
    assert not _session([ME, BOB, CAROL], {ME: True}).is_approved()


def test_two_of_three_is_a_majority():
    assert _session([ME, BOB, CAROL], {ME: True, BOB: True}).is_approved()


def test_a_dissenter_does_not_block_a_real_majority():
    assert _session([ME, BOB, CAROL], {ME: True, BOB: True, CAROL: False}).is_approved()


def test_one_approve_against_two_rejects_fails():
    assert not _session([ME, BOB, CAROL], {ME: True, BOB: False, CAROL: False}).is_approved()


def test_half_of_four_is_not_a_majority():
    """`>` and not `>=`: a tie leaves the history alone."""
    assert not _session([ME, BOB, CAROL, DAVE], {ME: True, BOB: True, CAROL: False, DAVE: False}).is_approved()


def test_three_of_four_is_a_majority():
    assert _session([ME, BOB, CAROL, DAVE], {ME: True, BOB: True, CAROL: True}).is_approved()


def test_silence_from_the_others_is_not_consent_at_any_size():
    for others in ([BOB], [BOB, CAROL], [BOB, CAROL, DAVE]):
        assert not _session([ME] + others, {ME: True}).is_approved()


# --- the pair rule, which was already right and must stay right -------------


def test_two_participants_still_need_both():
    assert _session([ME, BOB], {ME: True, BOB: True}).is_approved()


def test_two_participants_are_not_carried_by_one():
    assert not _session([ME, BOB], {ME: True}).is_approved()


def test_two_participants_with_a_reject_fail():
    assert not _session([ME, BOB], {ME: True, BOB: False}).is_approved()


# --- the timeout, which is where the defect actually fired ------------------


@pytest.mark.asyncio
async def test_an_unanswered_proposal_times_out_without_clearing_anything():
    """The whole point: no answer, no reset, and the file survives."""
    cleared = []
    manager, results = _manager(cleared)
    manager.active_sessions["p1"] = _session([ME, BOB, CAROL], {ME: True})

    await manager._finalize_proposal("p1")

    assert cleared == []
    assert [r["result"] for r in results] == ["rejected"]
    assert results[0]["clear_history"] is False


@pytest.mark.asyncio
async def test_an_answered_proposal_still_clears():
    """The regression half: refusing everything would also satisfy the test above."""
    cleared = []
    manager, results = _manager(cleared)
    manager.active_sessions["p1"] = _session([ME, BOB, CAROL], {ME: True, BOB: True})

    await manager._finalize_proposal("p1")

    assert cleared == [GROUP]
    assert [r["result"] for r in results] == ["approved"]


# --- the node that only receives the result ---------------------------------


@pytest.mark.asyncio
async def test_a_receiving_node_applies_its_own_archive_settings():
    """The initiator read them and every other node used the defaults.

    A node told not to archive archived anyway, and a retention limit set on
    that node was ignored — the reset obeyed whoever pressed the button.
    """
    from dpc_client_core.message_handlers.session_handler import NewSessionResultHandler

    applied = []
    service = SimpleNamespace(
        _get_or_create_conversation_monitor=lambda cid: SimpleNamespace(
            reset_conversation=lambda preserve=True, max_sessions=0: applied.append(
                (preserve, max_sessions)
            )
        ),
        firewall=SimpleNamespace(get_history_settings=lambda cid: (False, 7)),
        _group_agent_context={},
        session_manager=SimpleNamespace(get_session=lambda pid: None, active_sessions={}),
        local_api=SimpleNamespace(broadcast_event=_noop),
        _processed_message_ids=set(),
        # The handler relays the result on to members that cannot reach the
        # sender directly; an empty roster keeps that out of this test's way.
        group_manager=SimpleNamespace(get_group=lambda gid: None),
    )

    await NewSessionResultHandler(service).handle(
        BOB,
        {
            "proposal_id": "p1",
            "result": "approved",
            "clear_history": True,
            "conversation_id": GROUP,
        },
    )

    assert applied == [(False, 7)]


# --- helpers ---------------------------------------------------------------


def _manager(cleared):
    """A manager over a service stub that records what got cleared.

    The subject here is the counting rule and what it triggers, so the monitor
    is a recorder — but it records the same call the real one receives.
    """
    results = []

    def _monitor_for(conversation_id):
        return SimpleNamespace(
            reset_conversation=lambda preserve=True, max_sessions=0: cleared.append(conversation_id)
        )

    core = SimpleNamespace(
        p2p_manager=SimpleNamespace(node_id=ME),
        _get_or_create_conversation_monitor=_monitor_for,
        firewall=SimpleNamespace(get_history_settings=lambda cid: (True, 0)),
        _group_agent_context={},
        local_api=SimpleNamespace(broadcast_event=_noop),
        group_manager=SimpleNamespace(get_group=lambda gid: SimpleNamespace(is_discord_bridge=True)),
    )
    manager = NewSessionProposalManager(core)
    manager.on_result_broadcast = _record(results)
    return manager, results


def _record(sink):
    async def _broadcast(payload, participants):
        sink.append(payload)
    return _broadcast


async def _noop(*args, **kwargs):
    return None
