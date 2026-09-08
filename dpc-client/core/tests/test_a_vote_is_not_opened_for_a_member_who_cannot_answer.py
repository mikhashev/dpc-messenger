"""A knowledge vote needs everyone who has to answer to be reachable.

Once approval is counted over the participants, a member that is offline cannot
be outvoted — only waited for. The proposal then spends its ten minutes and
ends as a timeout, after the extraction has already been paid for in tokens and
in a minute of the model's time.

The New Session vote learned this first (`c5abbad7`): proposing is refused up
front, naming who is missing, rather than letting the vote run and come back
rejected for no stated reason. This is the same rule for knowledge.

Two moments, because a member can leave inside the extraction: before it starts,
and again before the vote is opened.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.knowledge_service import KnowledgeService

GROUP = "group-b88b65076b85"
ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32


def _service(members, connected):
    svc = KnowledgeService.__new__(KnowledgeService)
    svc.p2p_manager = SimpleNamespace(
        node_id=ME, peers={p: object() for p in connected}
    )
    svc.group_manager = SimpleNamespace(
        get_group=lambda gid: SimpleNamespace(members=list(members)) if gid == GROUP else None
    )
    svc.peer_metadata = {PEER: {"name": "Mike Linux"}}
    return svc


def test_a_group_of_one_has_nobody_to_wait_for():
    """The lone member is the only voter, and is trivially present."""
    svc = _service(members=[ME], connected=())

    assert svc._offline_participants(GROUP) == {}


def test_a_connected_member_is_not_reported_missing():
    svc = _service(members=[ME, PEER], connected=(PEER,))

    assert svc._offline_participants(GROUP) == {}


def test_a_disconnected_member_is_named():
    svc = _service(members=[ME, PEER], connected=())

    assert svc._offline_participants(GROUP) == {PEER: "Mike Linux"}


def test_a_member_with_no_display_name_is_still_named():
    """A node id is worse to read than a name, and better than «somebody»."""
    svc = _service(members=[ME, PEER], connected=())
    svc.peer_metadata = {}

    assert list(svc._offline_participants(GROUP)) == [PEER]


def test_a_conversation_that_is_not_a_group_waits_for_nobody():
    svc = _service(members=[ME, PEER], connected=())

    assert svc._offline_participants("local_ai") == {}
    assert svc._offline_participants(PEER) == {}


def test_an_unknown_group_is_not_treated_as_deserted():
    """A group we cannot resolve would otherwise refuse every extraction."""
    svc = _service(members=[ME, PEER], connected=())

    assert svc._offline_participants("group-unknown") == {}


# --- the two places the rule is actually applied ----------------------------


def _extraction_service(members, connected, monitor):
    """Enough of the service for `end_conversation_session` to reach its guards."""
    svc = _service(members, connected)
    svc.consensus_manager = SimpleNamespace(sessions={})
    svc.llm_manager = SimpleNamespace(providers={})
    svc._get_or_create_conversation_monitor = lambda cid: monitor
    svc.events = []

    async def _broadcast(event, payload):
        svc.events.append((event, payload))

    svc.local_api = SimpleNamespace(broadcast_event=_broadcast)
    return svc


def _monitor_that_must_not_run():
    def _boom(*a, **kw):
        raise AssertionError("the extraction ran for a group that could not vote")

    return SimpleNamespace(
        full_conversation=[], message_buffer=[], knowledge_score=0.0,
        rebuild_extraction_buffers_from_history=lambda: None,
        generate_commit_proposal=_boom,
    )


@pytest.mark.asyncio
async def test_the_extraction_is_refused_before_a_token_is_spent():
    svc = _extraction_service([ME, PEER], connected=(), monitor=_monitor_that_must_not_run())

    result = await KnowledgeService.end_conversation_session(svc, GROUP)

    assert result["reason"] == "participants_offline"
    assert result["offline"] == [PEER]
    assert "Mike Linux" in result["message"]


@pytest.mark.asyncio
async def test_a_member_leaving_during_the_extraction_stops_the_vote():
    """A minute passes inside the extraction; the roster can change in it."""
    proposal = SimpleNamespace(
        proposal_id="p1",
        topic="t",
        entries=[object()],
        avg_confidence=0.9,
        participants=[ME, PEER],
        to_dict=lambda: {"proposal_id": "p1"},
    )

    async def _generate(**kwargs):
        svc.p2p_manager.peers.pop(PEER)  # the peer drops mid-extraction
        return proposal

    monitor = SimpleNamespace(
        full_conversation=[], message_buffer=[], knowledge_score=0.0,
        rebuild_extraction_buffers_from_history=lambda: None,
        generate_commit_proposal=_generate,
    )
    svc = _extraction_service([ME, PEER], connected=(PEER,), monitor=monitor)
    svc.p2p_manager.node_id = ME

    result = await KnowledgeService.end_conversation_session(svc, GROUP)

    assert result["reason"] == "participants_offline"
    # Only the refusal. The announcement used to go out first and could not be
    # taken back: the dialog it opened accepted votes on a proposal
    # `propose_commit` never received, and each was answered «Proposal not found».
    assert [name for name, _ in svc.events] == ["knowledge_extraction_failed"]


@pytest.mark.asyncio
async def test_a_refusal_before_the_extraction_is_also_an_event():
    """The response is not read by the caller; the button moves on events."""
    svc = _extraction_service([ME, PEER], connected=(), monitor=_monitor_that_must_not_run())

    result = await KnowledgeService.end_conversation_session(svc, GROUP)

    assert [name for name, _ in svc.events] == ["knowledge_extraction_failed"]
    event = svc.events[0][1]
    assert event["conversation_id"] == GROUP
    assert event["reason"] == "participants_offline"
    assert event["message"] == result["message"]
    assert "Mike Linux" in event["message"]


@pytest.mark.asyncio
async def test_an_open_vote_refuses_the_extraction_and_says_so_on_screen():
    """The other early refusal had the same silence and the same cost."""
    svc = _extraction_service([ME, PEER], connected=(PEER,),
                              monitor=_monitor_that_must_not_run())
    svc.consensus_manager.sessions = {
        "s1": SimpleNamespace(
            status="voting",
            proposal=SimpleNamespace(conversation_id=GROUP, proposal_id="p9"),
        )
    }

    result = await KnowledgeService.end_conversation_session(svc, GROUP)

    assert result["reason"] == "vote_in_progress"
    assert result["proposal_id"] == "p9"
    assert [name for name, _ in svc.events] == ["knowledge_extraction_failed"]
    assert svc.events[0][1]["message"] == result["message"]


def test_a_member_offline_since_the_restart_is_named_from_the_peer_cache():
    """`peer_metadata` is empty for exactly the peers this names."""
    svc = _service(members=[ME, PEER], connected=())
    svc.peer_metadata = {}
    svc.p2p_manager.peer_cache = SimpleNamespace(
        get_peer=lambda nid: SimpleNamespace(display_name="Mike (linux)") if nid == PEER else None
    )

    assert svc._offline_participants(GROUP) == {PEER: "Mike (linux)"}


def test_a_peer_the_cache_has_never_seen_falls_back_to_the_node_id():
    svc = _service(members=[ME, PEER], connected=())
    svc.peer_metadata = {}
    svc.p2p_manager.peer_cache = SimpleNamespace(get_peer=lambda nid: None)

    assert svc._offline_participants(GROUP) == {PEER: PEER[:20]}
