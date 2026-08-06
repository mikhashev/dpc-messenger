"""A group reset takes everyone's yes, and it used to take one.

`is_approved` divided by the number of votes *cast* rather than by the people in
the conversation. The initiator's own vote is recorded as approve the moment it
proposes, and the 60-second timeout finalises with whatever arrived instead of
cancelling. One approve out of one cast is a majority of one, so a proposal
nobody answered was approved, and every node that later received the result
archived its history and deleted the file.

The first fix counted over participants — a real majority, two of three. Mike
rejected that on 2026-08-06 and the rule is now unanimity, for a reason that is
about the mechanism rather than about fairness: **the reset is undone by whoever
did not take part.** An outvoted or absent member keeps its history and hands it
back at the next sync, so a two-of-three reset is a pause, not a reset. Nothing
short of everyone makes it stick.

The price is deliberate and stated: a member gone for good makes a reset
impossible, with no override. The alternative was a reset that silently returns.

Proposing is refused up front when any member is unreachable, so the answer
arrives immediately and names who is missing, rather than after a minute of
silence with no reason given.

These expectations changed with the rule. The earlier majority cases are not
"fixed" here, they are reversed on purpose, and they are kept as tests so the
reversal is visible rather than deleted.
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


def test_everyone_saying_yes_is_the_only_way_through():
    assert _session([ME, BOB, CAROL], {ME: True, BOB: True, CAROL: True}).is_approved()


def test_two_of_three_is_no_longer_enough():
    """Reversed on purpose: this was approved under the majority rule.

    The third node keeps its history and returns it, so approving here bought
    nothing and hid that fact behind a success message.
    """
    assert not _session([ME, BOB, CAROL], {ME: True, BOB: True}).is_approved()


def test_one_dissenter_stops_it():
    assert not _session([ME, BOB, CAROL], {ME: True, BOB: True, CAROL: False}).is_approved()


def test_one_approve_against_two_rejects_fails():
    assert not _session([ME, BOB, CAROL], {ME: True, BOB: False, CAROL: False}).is_approved()


def test_three_of_four_is_no_longer_enough():
    assert not _session([ME, BOB, CAROL, DAVE], {ME: True, BOB: True, CAROL: True}).is_approved()


def test_four_of_four_passes():
    assert _session(
        [ME, BOB, CAROL, DAVE], {ME: True, BOB: True, CAROL: True, DAVE: True}
    ).is_approved()


def test_silence_from_the_others_is_not_consent_at_any_size():
    for others in ([BOB], [BOB, CAROL], [BOB, CAROL, DAVE]):
        assert not _session([ME] + others, {ME: True}).is_approved()


def test_a_vote_from_someone_outside_the_conversation_does_not_count():
    """Otherwise a stray VOTE_NEW_SESSION could stand in for a missing member."""
    assert not _session([ME, BOB, CAROL], {ME: True, BOB: True, DAVE: True}).is_approved()


# --- the pair, which needed both before and still does ----------------------


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
async def test_a_proposal_everyone_approved_still_clears():
    """The regression half: refusing everything would also satisfy the test above."""
    cleared = []
    manager, results = _manager(cleared)
    manager.active_sessions["p1"] = _session(
        [ME, BOB, CAROL], {ME: True, BOB: True, CAROL: True}
    )

    await manager._finalize_proposal("p1")

    assert cleared == [GROUP]
    assert [r["result"] for r in results] == ["approved"]


@pytest.mark.asyncio
async def test_a_partly_answered_proposal_clears_nothing():
    """Two of three used to clear everyone. Now it leaves every history alone."""
    cleared = []
    manager, results = _manager(cleared)
    manager.active_sessions["p1"] = _session([ME, BOB, CAROL], {ME: True, BOB: True})

    await manager._finalize_proposal("p1")

    assert cleared == []
    assert [r["result"] for r in results] == ["rejected"]


# --- refusing before the vote, so the answer is immediate and says who -------


@pytest.mark.asyncio
async def test_proposing_is_refused_while_a_member_is_unreachable():
    """A member who cannot answer cannot approve, so do not start the minute.

    The old check asked only whether *somebody* was online, which let the vote
    run to its timeout and come back "rejected" with no reason on screen.
    """
    from dpc_client_core.service import CoreService

    service = _proposing_service(connected=[BOB])
    result = await CoreService.propose_new_session(service, GROUP)

    assert result["status"] == "error"
    assert CAROL[:20] in result["message"]
    assert service.session_manager.calls == []


@pytest.mark.asyncio
async def test_proposing_goes_ahead_when_everyone_is_reachable():
    from dpc_client_core.service import CoreService

    service = _proposing_service(connected=[BOB, CAROL])
    result = await CoreService.propose_new_session(service, GROUP)

    assert result["status"] == "success"
    assert service.session_manager.calls == [{ME, BOB, CAROL}]


def _proposing_service(connected):
    async def _propose(conversation_id, participants):
        calls.append(set(participants))
        return {"status": "success", "proposal_id": "p1"}

    calls = []
    manager = SimpleNamespace(propose_new_session=_propose, calls=calls)
    return SimpleNamespace(
        group_manager=SimpleNamespace(
            get_group=lambda gid: SimpleNamespace(
                members=[ME, BOB, CAROL], is_discord_bridge=False
            )
        ),
        p2p_manager=SimpleNamespace(node_id=ME),
        p2p_coordinator=SimpleNamespace(get_connected_peers=lambda: list(connected)),
        session_manager=manager,
    )


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
