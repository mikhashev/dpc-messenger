"""`NEW_SESSION_RESULT` used to erase a conversation for anyone who asked.

The handler cleared history on `result == "approved" and clear_history`, and
only *afterwards* looked for a local session — so the lookup never guarded
anything. There was no check that the sender belongs to the group, that this
node had ever heard of the vote, or that the conversation named in the payload
is the one the vote was about. A peer that could reach us at all could send

    {proposal_id: <anything>, result: "approved",
     clear_history: true, conversation_id: <any chat>}

and the history was gone. Not "trust the initiator" — trust whoever spoke first,
and about any conversation they cared to name. Found by Fable 5 in the external
review of 2026-08-07 (B7); the read is confirmed at `session_handler.py`, where
the clear stood at line 126 and the session lookup at line 141.

The gate here is deliberately made of things this node already knows, so it
needs neither signatures nor the ADR-038 marker to stand up today:

1. a local voting session for that `proposal_id` exists — we took part;
2. the conversation named matches the one that session is about — a live
   proposal cannot be aimed at a different chat;
3. the sender is one of that proposal's participants — non-members are out.

Point 3 is membership rather than "must be the initiator" on purpose: in a star
the result reaches the far edge **relayed** by the middle node, and requiring the
initiator would refuse the legitimate relay and break New Session exactly where
it currently works. Full closure — a result nobody has to be trusted for — comes
with `session_started_at` (ADR-038 Q3), where the marker carries its own
evidence.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.message_handlers.session_handler import NewSessionResultHandler

ME = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32
CAROL = "dpc-node-" + "c" * 32
MALLORY = "dpc-node-" + "e" * 32
GROUP = "group-1234567890ab"
OTHER_GROUP = "group-ffffffffffff"


def _service(session=None):
    cleared = []
    events = []

    def _monitor_for(conversation_id):
        return SimpleNamespace(
            reset_conversation=lambda preserve=True, max_sessions=0: cleared.append(
                conversation_id
            )
        )

    async def _broadcast(event, payload):
        events.append((event, payload))

    active = {"p1": session} if session else {}
    service = SimpleNamespace(
        _get_or_create_conversation_monitor=_monitor_for,
        firewall=SimpleNamespace(get_history_settings=lambda cid: (True, 0)),
        _group_agent_context={},
        session_manager=SimpleNamespace(
            get_session=lambda pid: active.get(pid), active_sessions=active
        ),
        local_api=SimpleNamespace(broadcast_event=_broadcast),
        _processed_message_ids=set(),
        group_manager=SimpleNamespace(get_group=lambda gid: None),
    )
    service.cleared = cleared
    service.events = events
    return service


def _session(participants=(ME, BOB, CAROL), conversation_id=GROUP, initiator=BOB):
    proposal = SimpleNamespace(
        proposal_id="p1",
        initiator_node_id=initiator,
        conversation_id=conversation_id,
        participants=set(participants),
        votes={},
    )
    return SimpleNamespace(proposal=proposal, is_initiator=False)


async def _deliver(service, sender, **overrides):
    payload = {
        "proposal_id": "p1",
        "result": "approved",
        "clear_history": True,
        "conversation_id": GROUP,
    }
    payload.update(overrides)
    await NewSessionResultHandler(service).handle(sender, payload)


# --- the hole itself --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stranger_cannot_erase_a_conversation():
    """The defect verbatim: no session, no membership, history gone anyway."""
    service = _service()

    await _deliver(service, MALLORY)

    assert service.cleared == []


@pytest.mark.asyncio
async def test_a_made_up_proposal_id_erases_nothing():
    """Even from a member: we never voted on `p-nonsense`, so it decides nothing."""
    service = _service(_session())

    await _deliver(service, BOB, proposal_id="p-nonsense")

    assert service.cleared == []


@pytest.mark.asyncio
async def test_a_real_vote_cannot_be_aimed_at_a_different_chat():
    """`p1` was about GROUP; naming another conversation must not borrow it."""
    service = _service(_session())

    await _deliver(service, BOB, conversation_id=OTHER_GROUP)

    assert service.cleared == []


@pytest.mark.asyncio
async def test_a_non_member_cannot_close_a_vote_we_do_know_about():
    service = _service(_session())

    await _deliver(service, MALLORY)

    assert service.cleared == []


# --- and the half that keeps it useful --------------------------------------


@pytest.mark.asyncio
async def test_the_initiator_still_closes_the_vote_it_started():
    """Refusing everything would satisfy every test above."""
    service = _service(_session())

    await _deliver(service, BOB)

    assert service.cleared == [GROUP]


@pytest.mark.asyncio
async def test_a_relayed_result_from_another_member_is_accepted():
    """In a star the far edge hears the result from the middle node, not the
    initiator. Requiring `sender == initiator` would break New Session exactly
    where it works today."""
    service = _service(_session(initiator=CAROL))

    await _deliver(service, BOB)  # BOB relayed CAROL's result

    assert service.cleared == [GROUP]


@pytest.mark.asyncio
async def test_a_rejected_result_clears_nothing_and_still_reaches_the_ui():
    service = _service(_session())

    await _deliver(service, BOB, result="rejected", clear_history=False)

    assert service.cleared == []
    assert [e for e, _ in service.events] == ["new_session_result"]


@pytest.mark.asyncio
async def test_a_refused_result_is_not_announced_to_the_ui_as_a_reset():
    """A refused wipe must not leave the interface believing one happened."""
    service = _service()

    await _deliver(service, MALLORY)

    assert service.events == []
