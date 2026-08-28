"""A vote used to belong to whoever handed it over.

`handle_vote_message` recorded `sender_node_id` — the transport hop — even
though the payload already carried `voter_node_id`. In a star that is always
the wrong node: on 2026-08-07 at 02:16 the middle node relayed Linux's reject
to macOS, and macOS wrote it down as Windows's. Worse than a wrong name in a
log: `votes` is a dict, so the relayed vote overwrote the relayer's real vote
and the tally on that node could never be right again.

The obvious repair — trust `voter_node_id` — was refused by both external
reviewers on 2026-08-07, and correctly: ADR-036 §5 says an unsigned message on
a relay path is never attributed to the identity it claims. Taking the payload
at its word would have replaced misattribution with forgery available to anyone
connected. So the vote is signed by its author and the identity comes out of the
signature.

The policy, and what each branch buys:

  verified        signature holds  → counted as the claimed voter
  unverified      signed, no cached certificate → not counted, still relayed,
                  because a node that holds the certificate must get its chance
  legacy          unsigned, first-hand → counted; this is an un-upgraded
                  neighbour voting directly, and the transport *is* the voter
  legacy_relayed  unsigned, second-hand → not counted, still relayed. Exactly
                  the case that used to be credited to the relayer
  rejected        signed by someone else, or altered → dropped entirely

What is stubbed and why: `CommitSigner.verify_signature` is replaced here, so
these tests are about the policy built on its three-way answer, not about RSA.
The preimage itself is exercised for real below — it is a pure function, and it
is the part two nodes have to agree on byte for byte.
"""

from types import SimpleNamespace

import pytest

from dpc_protocol.message_signing import VOTE_PREIMAGE_VERSION, vote_content_hash
from dpc_client_core.message_handlers.session_handler import VoteNewSessionHandler

ME = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32
CAROL = "dpc-node-" + "c" * 32
GROUP = "group-1234567890ab"


# --- the preimage, for real -------------------------------------------------


def test_the_same_vote_hashes_the_same_on_any_node():
    args = dict(
        proposal_id="p1", conversation_id=GROUP, voter_node_id=CAROL,
        vote=True, timestamp="2026-08-07T02:16:04Z",
    )
    assert vote_content_hash(**args) == vote_content_hash(**args)


def test_true_and_approve_are_the_same_decision():
    """Two spellings of yes must not produce two signatures."""
    base = dict(proposal_id="p1", conversation_id=GROUP, voter_node_id=CAROL,
                timestamp="2026-08-07T02:16:04Z")
    assert vote_content_hash(vote=True, **base) == vote_content_hash(vote="approve", **base)
    assert vote_content_hash(vote=False, **base) != vote_content_hash(vote=True, **base)


def test_the_conversation_is_inside_the_signature():
    """Otherwise a vote could be replayed into another group reusing the id."""
    base = dict(proposal_id="p1", voter_node_id=CAROL, vote=True,
                timestamp="2026-08-07T02:16:04Z")
    assert vote_content_hash(conversation_id=GROUP, **base) != vote_content_hash(
        conversation_id="group-ffffffffffff", **base
    )


def test_the_voter_is_inside_the_signature():
    base = dict(proposal_id="p1", conversation_id=GROUP, vote=True,
                timestamp="2026-08-07T02:16:04Z")
    assert vote_content_hash(voter_node_id=CAROL, **base) != vote_content_hash(
        voter_node_id=BOB, **base
    )


# --- the policy -------------------------------------------------------------


def _service(session=None):
    recorded = []
    relayed = []

    async def _handle_vote(sender_node_id, payload, voter_node_id=None):
        recorded.append((voter_node_id or sender_node_id, payload.get("vote")))

    async def _send(node_id, message):
        relayed.append((node_id, message["payload"].get("voter_node_id")))

    service = SimpleNamespace(
        session_manager=SimpleNamespace(
            handle_vote_message=_handle_vote,
            active_sessions={"p1": session} if session else {},
        ),
        _processed_message_ids=set(),
        group_manager=SimpleNamespace(
            get_group=lambda gid: SimpleNamespace(members=[ME, BOB, CAROL])
        ),
        p2p_manager=SimpleNamespace(
            node_id=ME, peers={BOB: object(), CAROL: object()},
            send_message_to_peer=_send,
        ),
    )
    service.recorded = recorded
    service.relayed = relayed
    return service


def _payload(voter=CAROL, signed=True, **overrides):
    payload = {
        "proposal_id": "p1",
        "vote": False,
        "voter_node_id": voter,
        "conversation_id": GROUP,
        "timestamp": "2026-08-07T02:16:04Z",
    }
    if signed:
        payload.update(
            vote_hash=vote_content_hash(
                proposal_id="p1", conversation_id=GROUP, voter_node_id=voter,
                vote=payload["vote"], timestamp=payload["timestamp"],
            ),
            signature="sig",
            signer_node_id=voter,
            vote_preimage_version=VOTE_PREIMAGE_VERSION,
        )
    payload.update(overrides)
    return payload


@pytest.fixture
def verifier(monkeypatch):
    """Swap RSA for a dial: True, False or None, set per test."""
    from dpc_protocol.commit_integrity import CommitSigner

    answer = {"value": True}
    monkeypatch.setattr(
        CommitSigner, "verify_signature",
        staticmethod(lambda *a, **k: answer["value"]),
    )
    return answer


@pytest.mark.asyncio
async def test_a_relayed_signed_vote_is_credited_to_its_signer(verifier):
    """The defect verbatim: BOB relays CAROL's reject."""
    service = _service()

    await VoteNewSessionHandler(service).handle(BOB, _payload(voter=CAROL))

    assert service.recorded == [(CAROL, False)]


@pytest.mark.asyncio
async def test_an_unsigned_first_hand_vote_still_counts(verifier):
    """An un-upgraded neighbour voting directly must not be cut off."""
    service = _service()

    await VoteNewSessionHandler(service).handle(BOB, _payload(voter=BOB, signed=False))

    assert service.recorded == [(BOB, False)]


@pytest.mark.asyncio
async def test_an_unsigned_relayed_vote_is_not_credited_to_the_relayer(verifier):
    """This is the exact 02:16 case, and the tally must simply not gain a vote."""
    service = _service()

    await VoteNewSessionHandler(service).handle(BOB, _payload(voter=CAROL, signed=False))

    assert service.recorded == []


@pytest.mark.asyncio
async def test_an_unsigned_relayed_vote_still_travels(verifier):
    """Dropping it from our tally must not strand it: someone else may know."""
    service = _service()

    await VoteNewSessionHandler(service).handle(BOB, _payload(voter=CAROL, signed=False))

    assert service.relayed == [(CAROL, CAROL)]


@pytest.mark.asyncio
async def test_a_vote_we_cannot_check_is_not_counted_but_is_passed_on(verifier):
    verifier["value"] = None  # certificate not cached — the far side of a star
    service = _service()

    await VoteNewSessionHandler(service).handle(BOB, _payload(voter=CAROL))

    assert service.recorded == []
    assert service.relayed == [(CAROL, CAROL)]


@pytest.mark.asyncio
async def test_a_bad_signature_is_dropped_and_not_repeated(verifier):
    verifier["value"] = False
    service = _service()

    await VoteNewSessionHandler(service).handle(BOB, _payload(voter=CAROL))

    assert service.recorded == []
    assert service.relayed == []


@pytest.mark.asyncio
async def test_signing_for_someone_else_is_refused(verifier):
    """BOB signs, the payload claims CAROL: neither is counted."""
    service = _service()

    await VoteNewSessionHandler(service).handle(
        BOB, _payload(voter=CAROL, signer_node_id=BOB)
    )

    assert service.recorded == []
    assert service.relayed == []


@pytest.mark.asyncio
async def test_an_altered_vote_no_longer_matches_its_hash(verifier):
    """Flip reject to approve in flight; the hash was over the original."""
    service = _service()

    await VoteNewSessionHandler(service).handle(BOB, _payload(voter=CAROL, vote=True))

    assert service.recorded == []
    assert service.relayed == []


# --- relay no longer depends on local state (B6) ----------------------------


@pytest.mark.asyncio
async def test_a_vote_that_overtakes_its_proposal_is_still_relayed(verifier):
    """No local session at all — the conversation comes from the payload.

    Before, `conversation_id` was read off the session, so a vote arriving
    before its proposal (different paths through the graph) was dropped
    silently, and so was the one that arrived after finalisation deleted the
    session — which is every quorum-closing vote.
    """
    service = _service(session=None)

    await VoteNewSessionHandler(service).handle(BOB, _payload(voter=CAROL))

    assert service.relayed == [(CAROL, CAROL)]


@pytest.mark.asyncio
async def test_the_same_vote_by_two_paths_is_relayed_once(verifier):
    """Keyed on the voter, not on who carried it."""
    service = _service()
    handler = VoteNewSessionHandler(service)

    await handler.handle(BOB, _payload(voter=CAROL))
    await handler.handle(ME, _payload(voter=CAROL))

    assert len(service.relayed) == 1
