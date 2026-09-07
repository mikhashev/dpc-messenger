"""A proposal is offered again to a participant that was not there for it.

Measured on the pair, 2026-09-07: the Linux node lost the link at 13:30:52,
`proposal-ff7b1a84` was broadcast at 14:09:49 while it was away, it came back at
14:11:17 — eight minutes before the deadline — and was never told. The only line
about that proposal in its whole log is the result: `status=timeout`. It learned
the outcome of a vote it was never invited to.

Mike's call, 2026-09-07: «если не голосовала еще надо переслать предложение».

Re-offering is the option that hands back a vote instead of taking away the
chance of one — the alternative was to end the vote early, which would have cost
the returning node its say and the voter the right to change a cast one.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.knowledge_service import KnowledgeService

ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
STRANGER = "dpc-node-" + "c" * 32


def _service(sessions):
    sent = []

    async def _send(node_id, message):
        sent.append((node_id, message))

    svc = KnowledgeService.__new__(KnowledgeService)
    svc.consensus_manager = SimpleNamespace(sessions=sessions)
    svc.p2p_manager = SimpleNamespace(node_id=ME, send_message_to_peer=_send)
    return svc, sent


def _session(status="voting", participants=(ME, PEER), votes=(), proposal_id="p1"):
    return SimpleNamespace(
        status=status,
        votes={v: object() for v in votes},
        proposal=SimpleNamespace(
            proposal_id=proposal_id,
            conversation_id="group-b88b65076b85",
            participants=list(participants),
            to_dict=lambda: {"proposal_id": proposal_id},
        ),
    )


@pytest.mark.asyncio
async def test_a_participant_that_never_answered_is_offered_the_proposal():
    svc, sent = _service({"p1": _session()})

    assert await svc.resend_open_proposals(PEER) == 1
    assert [m["command"] for _peer, m in sent] == ["PROPOSE_KNOWLEDGE_COMMIT"]
    assert sent[0][0] == PEER


@pytest.mark.asyncio
async def test_a_participant_whose_vote_arrived_is_left_alone():
    """The receiver rebuilds its session from the proposal, losing its own vote."""
    svc, sent = _service({"p1": _session(votes=(PEER,))})

    assert await svc.resend_open_proposals(PEER) == 0
    assert sent == []


@pytest.mark.asyncio
async def test_a_closed_proposal_is_not_re_offered():
    svc, sent = _service({"p1": _session(status="timeout")})

    assert await svc.resend_open_proposals(PEER) == 0
    assert sent == []


@pytest.mark.asyncio
async def test_a_node_outside_the_roster_is_not_invited():
    svc, sent = _service({"p1": _session()})

    assert await svc.resend_open_proposals(STRANGER) == 0
    assert sent == []


@pytest.mark.asyncio
async def test_the_same_proposal_is_not_offered_twice():
    """`on_peer_list_change` fires on every change, not only on a reconnect."""
    svc, sent = _service({"p1": _session()})

    first = await svc.resend_open_proposals(PEER)
    second = await svc.resend_open_proposals(PEER)

    assert (first, second) == (1, 0)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_new_proposal_is_offered_even_after_an_earlier_one():
    sessions = {"p1": _session()}
    svc, sent = _service(sessions)
    await svc.resend_open_proposals(PEER)

    sessions["p2"] = _session(proposal_id="p2")

    assert await svc.resend_open_proposals(PEER) == 1
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_a_send_that_fails_is_not_remembered_as_offered():
    """The peer went away again mid-send; the next connect must try once more."""
    svc, sent = _service({"p1": _session()})
    fail = True

    async def _send(node_id, message):
        if fail:
            raise ConnectionError("peer gone")
        sent.append((node_id, message))

    svc.p2p_manager.send_message_to_peer = _send
    assert await svc.resend_open_proposals(PEER) == 0

    fail = False
    assert await svc.resend_open_proposals(PEER) == 1
    assert len(sent) == 1


def test_the_reconnect_path_actually_calls_it():
    """A helper nobody calls is the shape this board sees most often."""
    import inspect
    from dpc_client_core.service import CoreService

    src = inspect.getsource(CoreService.on_peer_list_change)
    assert "resend_open_proposals(" in src, "nothing re-offers on a peer list change"
