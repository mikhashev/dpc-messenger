"""A reset's outcome must not leave before the vote it was computed from.

`vote_new_session` recorded the vote locally and then sent it. Recording can end
the vote — it completes the tally whenever this node answers last — and
finalising broadcasts NEW_SESSION_RESULT, so the outcome always reached the peer
first. The peer applied it, deleted its session, and dropped the signed vote
that arrived milliseconds later; never having counted a vote it never reached
`_finalize_proposal`, the only writer of the ADR-038 session marker, so the node
that asked for the reset kept no boundary.

Sending first makes every participant reach the decision from evidence it
verified, which turns a peer's result into a confirmation of what this node
already decided.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.message_handlers.session_handler import NewSessionResultHandler
from dpc_client_core.session_manager import NewSessionProposalManager

ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
STRANGER = "dpc-node-" + "e" * 32
GROUP = "group-1234567890ab"
PROPOSAL = "p-68bb2545"


# --------------------------------------------------------------------------
# The order itself
# --------------------------------------------------------------------------

def _voting_service(record_finalizes: bool):
    """A CoreService stand-in that logs what left the node, in order."""
    trace = []

    async def _send(node_id, message):
        trace.append(("sent", message["command"], node_id))

    async def _record_vote(proposal_id, voter, vote, signed_payload=None):
        trace.append(("recorded", voter, vote))
        if record_finalizes:
            # What record_vote really does when this vote completes the tally.
            trace.append(("sent", "NEW_SESSION_RESULT", PEER))

    proposal = SimpleNamespace(
        proposal_id=PROPOSAL,
        conversation_id=GROUP,
        participants={ME, PEER},
    )
    service = SimpleNamespace(
        session_manager=SimpleNamespace(
            get_session=lambda pid: SimpleNamespace(proposal=proposal),
            record_vote=_record_vote,
        ),
        p2p_manager=SimpleNamespace(node_id=ME, peers={PEER: object()}, send_message_to_peer=_send),
    )
    service.trace = trace
    return service


@pytest.mark.asyncio
async def test_the_vote_is_on_the_wire_before_the_tally_can_end_it():
    from dpc_client_core.service import CoreService

    service = _voting_service(record_finalizes=True)
    result = await CoreService.vote_new_session(service, PROPOSAL, True)

    assert result["status"] == "success"
    sent = [t for t in service.trace if t[0] == "sent"]
    assert sent[0][1] == "VOTE_NEW_SESSION", service.trace
    assert sent[1][1] == "NEW_SESSION_RESULT", service.trace


@pytest.mark.asyncio
async def test_the_vote_is_still_recorded_when_no_peer_can_be_reached():
    from dpc_client_core.service import CoreService

    service = _voting_service(record_finalizes=False)
    service.p2p_manager.peers = {}

    await CoreService.vote_new_session(service, PROPOSAL, True)

    assert ("recorded", ME, True) in service.trace


@pytest.mark.asyncio
async def test_a_failing_send_does_not_swallow_the_local_record():
    from dpc_client_core.service import CoreService

    service = _voting_service(record_finalizes=False)

    async def _explode(node_id, message):
        raise ConnectionError("peer went away mid-vote")

    service.p2p_manager.send_message_to_peer = _explode

    await CoreService.vote_new_session(service, PROPOSAL, True)

    assert ("recorded", ME, True) in service.trace


# --------------------------------------------------------------------------
# What the peer's result means once we have decided for ourselves
# --------------------------------------------------------------------------

def _result_service(finalized):
    cleared = []
    events = []
    relayed = []

    async def _broadcast(event, payload):
        events.append(event)

    manager = SimpleNamespace(
        get_session=lambda pid: None,
        active_sessions={},
        finalized_proposals=dict(finalized),
    )
    manager.confirms_our_own_decision = (
        lambda pid, cid, sender: NewSessionProposalManager.confirms_our_own_decision(
            manager, pid, cid, sender
        )
    )
    service = SimpleNamespace(
        _get_or_create_conversation_monitor=lambda cid: SimpleNamespace(
            reset_conversation=lambda preserve=True, max_sessions=0: cleared.append(cid)
        ),
        firewall=SimpleNamespace(get_history_settings=lambda cid: (True, 0)),
        _group_agent_context={},
        session_manager=manager,
        local_api=SimpleNamespace(broadcast_event=_broadcast),
        _processed_message_ids=set(),
        group_manager=SimpleNamespace(get_group=lambda gid: None),
    )
    service.cleared = cleared
    service.events = events
    service.relayed = relayed
    return service


DECIDED = {PROPOSAL: {"conversation_id": GROUP, "participants": {ME, PEER}, "result": "approved"}}


def _payload(**over):
    payload = {
        "proposal_id": PROPOSAL,
        "conversation_id": GROUP,
        "result": "approved",
        "clear_history": True,
    }
    payload.update(over)
    return payload


async def _handle(service, sender, payload):
    handler = NewSessionResultHandler(service)
    async def _relay(command, *rest):
        service.relayed.append(command)

    handler._relay_to_group = _relay
    return await handler.handle(sender, payload)


@pytest.mark.asyncio
async def test_a_result_for_what_we_already_decided_clears_nothing_twice():
    service = _result_service(DECIDED)

    await _handle(service, PEER, _payload())

    assert service.cleared == []


@pytest.mark.asyncio
async def test_a_confirming_result_is_still_relayed_for_the_far_edge():
    service = _result_service(DECIDED)

    await _handle(service, PEER, _payload())

    assert service.relayed == ["NEW_SESSION_RESULT"]


@pytest.mark.asyncio
async def test_a_stranger_cannot_ride_in_on_a_proposal_we_decided():
    service = _result_service(DECIDED)

    await _handle(service, STRANGER, _payload())

    assert service.cleared == []
    assert service.events == []


@pytest.mark.asyncio
async def test_a_decided_proposal_does_not_license_erasing_another_chat():
    service = _result_service(DECIDED)

    await _handle(service, PEER, _payload(conversation_id="group-ffffffffffff"))

    assert service.cleared == []
    assert service.events == []


@pytest.mark.asyncio
async def test_an_unknown_proposal_is_refused_as_before():
    """Refused and confirmed both clear nothing; only the relay tells them apart.

    A result we will not act on must not be repeated to anyone else, so
    mistaking a proposal we never heard of for one we decided would put a
    stranger's result on the wire under our name.
    """
    service = _result_service({})

    await _handle(service, PEER, _payload())

    assert service.cleared == []
    assert service.events == []
    assert service.relayed == []


@pytest.mark.asyncio
async def test_a_stranger_naming_an_unknown_proposal_is_not_repeated_either():
    service = _result_service({})

    await _handle(service, STRANGER, _payload())

    assert service.cleared == []
    assert service.relayed == []


# --------------------------------------------------------------------------
# The record the confirmation is read from
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalising_leaves_the_decision_behind_for_the_confirmation_to_find():
    """Run the real finalize: the record the gate reads has to be written by it."""
    from dpc_client_core.session_manager import (
        NewSessionProposal, VotingSession, _FINALIZED_KEPT,
    )

    manager = NewSessionProposalManager.__new__(NewSessionProposalManager)
    manager.logger = logging.getLogger("test")
    manager.active_sessions = {}
    manager.finalized_proposals = {}
    manager.on_result_broadcast = None

    async def _broadcast(event, payload):
        pass

    manager.core_service = SimpleNamespace(
        group_manager=SimpleNamespace(get_group=lambda gid: None, set_session_marker=lambda *a, **k: None),
        _get_or_create_conversation_monitor=lambda cid: SimpleNamespace(
            reset_conversation=lambda preserve=True, max_sessions=0: None
        ),
        firewall=SimpleNamespace(get_history_settings=lambda cid: (True, 0)),
        _group_agent_context={},
        local_api=SimpleNamespace(broadcast_event=_broadcast),
    )

    for i in range(_FINALIZED_KEPT + 3):
        pid = f"p{i}"
        manager.active_sessions[pid] = VotingSession(
            proposal=NewSessionProposal(
                proposal_id=pid,
                initiator_node_id=ME,
                conversation_id=GROUP,
                timestamp="2026-09-07T18:01:46Z",
                participants={ME, PEER},
                votes={ME: True, PEER: True},
                deadline=0,
            ),
            is_initiator=True,
        )
        await manager._finalize_proposal(pid)

    last = f"p{_FINALIZED_KEPT + 2}"
    assert last not in manager.active_sessions
    assert manager.confirms_our_own_decision(last, GROUP, PEER) is True
    assert manager.confirms_our_own_decision(last, GROUP, STRANGER) is False
    assert manager.confirms_our_own_decision(last, "group-ffffffffffff", PEER) is False
    assert len(manager.finalized_proposals) == _FINALIZED_KEPT
    assert "p0" not in manager.finalized_proposals


# --------------------------------------------------------------------------
# Who sleeps after a reset
# --------------------------------------------------------------------------

def _finalising_manager(is_initiator):
    """A manager wired just enough to reach the sleep decision."""
    from dpc_client_core.session_manager import NewSessionProposal, VotingSession

    slept = []

    async def _sleep(cid):
        slept.append(cid)

    async def _broadcast(event, payload):
        pass

    manager = NewSessionProposalManager.__new__(NewSessionProposalManager)
    manager.logger = logging.getLogger("test")
    manager.finalized_proposals = {}
    manager.on_result_broadcast = None
    manager.core_service = SimpleNamespace(
        group_manager=SimpleNamespace(
            get_group=lambda gid: SimpleNamespace(is_discord_bridge=False),
            set_session_marker=lambda *a, **k: None,
        ),
        _get_or_create_conversation_monitor=lambda cid: SimpleNamespace(
            reset_conversation=lambda preserve=True, max_sessions=0: None
        ),
        firewall=SimpleNamespace(get_history_settings=lambda cid: (True, 0)),
        _group_agent_context={},
        local_api=SimpleNamespace(broadcast_event=_broadcast),
        trigger_group_sleep=_sleep,
    )
    manager.active_sessions = {
        PROPOSAL: VotingSession(
            proposal=NewSessionProposal(
                proposal_id=PROPOSAL,
                initiator_node_id=ME if is_initiator else PEER,
                conversation_id=GROUP,
                timestamp="2026-09-07T21:04:46Z",
                participants={ME, PEER},
                votes={ME: True, PEER: True},
                deadline=0,
            ),
            is_initiator=is_initiator,
        )
    }
    manager.slept = slept
    return manager


@pytest.mark.asyncio
async def test_the_node_that_asked_for_the_reset_does_not_sleep_its_agents():
    """Mike's call: the person who pressed the button is at the keyboard."""
    manager = _finalising_manager(is_initiator=True)

    await manager._finalize_proposal(PROPOSAL)
    await asyncio.sleep(0)

    assert manager.slept == []


@pytest.mark.asyncio
async def test_the_other_participant_still_sleeps():
    manager = _finalising_manager(is_initiator=False)

    await manager._finalize_proposal(PROPOSAL)
    await asyncio.sleep(0)

    assert manager.slept == [GROUP]


def test_a_lone_member_ending_its_own_session_does_not_sleep_either():
    """The second door: no vote, no initiator field — and the node that pressed
    the button is the only one there, which is the whole of Mike's reason."""
    import inspect

    from dpc_client_core.service import CoreService

    source = inspect.getsource(CoreService.propose_new_session)
    assert "trigger_group_sleep" not in source
    assert "this node asked for the reset" in source
