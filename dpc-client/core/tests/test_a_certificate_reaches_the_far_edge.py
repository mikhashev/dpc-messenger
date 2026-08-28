"""A signature is worth what the receiver can check, and it could check nothing.

Certificates reached a node one way: a direct TLS handshake. On the far side of
a star there is no handshake, so every message from the far node was stored
`unverified` — 55 of them on the Linux node on 2026-08-07 — and stayed that way
for good, because nothing ever revisited the flag. ADR-036 listed both halves,
the fetch and the re-check, and both stayed Pending.

That left everything signed in this round unverifiable exactly where the signing
was introduced for: on the edges. A vote from the far edge could not be counted;
a session marker from the far edge could not be obeyed.

Asking a neighbour is safe without trusting the neighbour. `node_id` is the
SHA256 of the public key, so `_persist_peer_certificate` re-derives it and
refuses anything that does not hash to the id asked about. A relay can withhold
a certificate or send rubbish; it cannot substitute one.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.message_handlers.certificate_handler import (
    CertificateRequestHandler,
    CertificateResponseHandler,
)
from dpc_client_core.message_handlers.session_handler import VoteNewSessionHandler

ME = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32
CAROL = "dpc-node-" + "c" * 32
GROUP = "group-1234567890ab"


def _service(**overrides):
    sent = []
    stored = []
    events = []

    async def _send(node_id, message):
        sent.append((node_id, message["command"], message["payload"]))

    async def _broadcast(event, payload):
        events.append((event, payload))

    service = SimpleNamespace(
        p2p_manager=SimpleNamespace(
            node_id=ME,
            peers={BOB: object()},
            send_message_to_peer=_send,
            _persist_peer_certificate=lambda nid, pem: (stored.append((nid, pem)), True)[1],
        ),
        pending_certificate_requests=set(),
        conversation_monitors={},
        local_api=SimpleNamespace(broadcast_event=_broadcast),
        session_manager=SimpleNamespace(handle_vote_message=_noop, active_sessions={}),
        _processed_message_ids=set(),
        group_manager=SimpleNamespace(get_group=lambda gid: None),
    )
    for key, value in overrides.items():
        setattr(service, key, value)
    service.sent = sent
    service.stored = stored
    service.events = events
    return service


async def _noop(*args, **kwargs):
    return None


# --- asking ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unverifiable_vote_makes_us_ask_the_relay():
    """The relay could check the signature we could not — so it has the cert."""
    from dpc_protocol.commit_integrity import CommitSigner
    from dpc_protocol.message_signing import VOTE_PREIMAGE_VERSION, vote_content_hash

    service = _service()
    payload = {
        "proposal_id": "p1", "vote": True, "voter_node_id": CAROL,
        "conversation_id": GROUP, "timestamp": "2026-08-07T02:16:04Z",
        "signature": "sig", "signer_node_id": CAROL,
        "vote_preimage_version": VOTE_PREIMAGE_VERSION,
    }
    payload["vote_hash"] = vote_content_hash(
        proposal_id="p1", conversation_id=GROUP, voter_node_id=CAROL,
        vote=True, timestamp=payload["timestamp"],
    )

    original = CommitSigner.verify_signature
    CommitSigner.verify_signature = staticmethod(lambda *a, **k: None)  # no cert
    try:
        await VoteNewSessionHandler(service).handle(BOB, payload)
    finally:
        CommitSigner.verify_signature = original

    assert (BOB, "CERT_REQUEST", {"node_id": CAROL}) in service.sent


@pytest.mark.asyncio
async def test_we_ask_once_not_once_per_message():
    """A busy group must not turn one missing certificate into a flood."""
    service = _service()
    handler = VoteNewSessionHandler(service)

    await handler._ask_for_certificate(CAROL, BOB)
    await handler._ask_for_certificate(CAROL, BOB)

    assert len([s for s in service.sent if s[1] == "CERT_REQUEST"]) == 1


@pytest.mark.asyncio
async def test_we_do_not_ask_a_peer_we_are_not_connected_to():
    service = _service()

    await VoteNewSessionHandler(service)._ask_for_certificate(CAROL, "dpc-node-" + "f" * 32)

    assert service.sent == []
    assert service.pending_certificate_requests == set()


@pytest.mark.asyncio
async def test_we_never_ask_for_our_own_certificate():
    service = _service()

    await VoteNewSessionHandler(service)._ask_for_certificate(ME, BOB)

    assert service.sent == []


# --- answering -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_for_a_certificate_we_hold_is_answered(tmp_path, monkeypatch):
    import dpc_client_core.message_handlers.certificate_handler as mod

    monkeypatch.setattr(mod, "DPC_HOME_DIR", tmp_path)
    (tmp_path / "peers").mkdir()
    (tmp_path / "peers" / f"{CAROL}.crt").write_text("PEM-CAROL", encoding="utf-8")
    service = _service()

    await CertificateRequestHandler(service).handle(BOB, {"node_id": CAROL})

    assert service.sent == [(BOB, "CERT_RESPONSE", {"node_id": CAROL, "certificate": "PEM-CAROL"})]


@pytest.mark.asyncio
async def test_our_own_certificate_comes_from_our_own_file(tmp_path, monkeypatch):
    import dpc_client_core.message_handlers.certificate_handler as mod

    monkeypatch.setattr(mod, "DPC_HOME_DIR", tmp_path)
    (tmp_path / "node.crt").write_text("PEM-ME", encoding="utf-8")
    service = _service()

    await CertificateRequestHandler(service).handle(BOB, {"node_id": ME})

    assert service.sent == [(BOB, "CERT_RESPONSE", {"node_id": ME, "certificate": "PEM-ME"})]


@pytest.mark.asyncio
async def test_a_request_we_cannot_answer_is_simply_not_answered(tmp_path, monkeypatch):
    import dpc_client_core.message_handlers.certificate_handler as mod

    monkeypatch.setattr(mod, "DPC_HOME_DIR", tmp_path)
    service = _service()

    await CertificateRequestHandler(service).handle(BOB, {"node_id": CAROL})

    assert service.sent == []


# --- receiving --------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_accepted_certificate_reverifies_what_was_parked():
    """The half ADR-036 named and nobody built: `unverified` stops being final."""
    rechecked = []
    service = _service(
        conversation_monitors={
            GROUP: SimpleNamespace(
                reverify_author=lambda nid: (rechecked.append(nid), 3)[1]
            )
        }
    )
    service.pending_certificate_requests.add(CAROL)

    await CertificateResponseHandler(service).handle(BOB, {"node_id": CAROL, "certificate": "PEM"})

    assert service.stored == [(CAROL, "PEM")]
    assert rechecked == [CAROL]
    assert ("messages_reverified", {"node_id": CAROL, "count": 3}) in service.events
    assert CAROL not in service.pending_certificate_requests


@pytest.mark.asyncio
async def test_a_certificate_whose_key_is_not_that_node_changes_nothing():
    """A relay may send rubbish; it may not substitute an identity."""
    service = _service()
    service.p2p_manager._persist_peer_certificate = lambda nid, pem: False
    service.conversation_monitors = {
        GROUP: SimpleNamespace(reverify_author=lambda nid: 1 / 0)
    }

    await CertificateResponseHandler(service).handle(BOB, {"node_id": CAROL, "certificate": "junk"})

    assert service.events == []


@pytest.mark.asyncio
async def test_nothing_to_recheck_is_not_announced():
    service = _service(
        conversation_monitors={GROUP: SimpleNamespace(reverify_author=lambda nid: 0)}
    )

    await CertificateResponseHandler(service).handle(BOB, {"node_id": CAROL, "certificate": "PEM"})

    assert service.events == []


@pytest.mark.asyncio
async def test_one_unreadable_conversation_does_not_stop_the_others():
    def _boom(_nid):
        raise RuntimeError("history file is a directory")

    service = _service(
        conversation_monitors={
            "bad": SimpleNamespace(reverify_author=_boom),
            GROUP: SimpleNamespace(reverify_author=lambda nid: 2),
        }
    )

    await CertificateResponseHandler(service).handle(BOB, {"node_id": CAROL, "certificate": "PEM"})

    assert ("messages_reverified", {"node_id": CAROL, "count": 2}) in service.events


# --- the re-check itself ----------------------------------------------------


def _monitor(rows):
    from dpc_client_core.conversation_monitor import ConversationMonitor

    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.conversation_id = GROUP
    monitor.message_history = rows
    monitor._history_dirty = False
    monitor.save_history = lambda: True
    return monitor


def test_a_parked_message_becomes_verified_when_the_signature_holds(monkeypatch):
    from dpc_protocol.commit_integrity import CommitSigner

    monkeypatch.setattr(CommitSigner, "verify_signature", staticmethod(lambda *a, **k: True))
    monitor = _monitor([
        {"id": "m1", "signer_node_id": CAROL, "content_hash": "h", "signature": "s",
         "verification": "unverified"},
    ])

    assert monitor.reverify_author(CAROL) == 1
    assert monitor.message_history[0]["verification"] == "verified"


def test_a_message_written_by_the_live_path_gains_a_verdict(monkeypatch):
    """That path stored the signature and no verdict at all (Fable 5, B8)."""
    from dpc_protocol.commit_integrity import CommitSigner

    monkeypatch.setattr(CommitSigner, "verify_signature", staticmethod(lambda *a, **k: True))
    monitor = _monitor([
        {"id": "m1", "signer_node_id": CAROL, "content_hash": "h", "signature": "s"},
    ])

    assert monitor.reverify_author(CAROL) == 1


def test_a_signature_that_fails_is_marked_rejected_not_left_alone(monkeypatch):
    from dpc_protocol.commit_integrity import CommitSigner

    monkeypatch.setattr(CommitSigner, "verify_signature", staticmethod(lambda *a, **k: False))
    monitor = _monitor([
        {"id": "m1", "signer_node_id": CAROL, "content_hash": "h", "signature": "s",
         "verification": "unverified"},
    ])

    assert monitor.reverify_author(CAROL) == 1
    assert monitor.message_history[0]["verification"] == "rejected"


def test_still_no_certificate_leaves_the_record_parked(monkeypatch):
    from dpc_protocol.commit_integrity import CommitSigner

    monkeypatch.setattr(CommitSigner, "verify_signature", staticmethod(lambda *a, **k: None))
    monitor = _monitor([
        {"id": "m1", "signer_node_id": CAROL, "content_hash": "h", "signature": "s",
         "verification": "unverified"},
    ])

    assert monitor.reverify_author(CAROL) == 0
    assert monitor.message_history[0]["verification"] == "unverified"


def test_other_authors_are_not_touched(monkeypatch):
    from dpc_protocol.commit_integrity import CommitSigner

    monkeypatch.setattr(CommitSigner, "verify_signature", staticmethod(lambda *a, **k: True))
    monitor = _monitor([
        {"id": "m1", "signer_node_id": BOB, "content_hash": "h", "signature": "s",
         "verification": "unverified"},
    ])

    assert monitor.reverify_author(CAROL) == 0
    assert monitor.message_history[0]["verification"] == "unverified"


def test_unsigned_records_are_left_alone(monkeypatch):
    from dpc_protocol.commit_integrity import CommitSigner

    monkeypatch.setattr(CommitSigner, "verify_signature", staticmethod(lambda *a, **k: True))
    monitor = _monitor([{"id": "m1", "signer_node_id": CAROL}])

    assert monitor.reverify_author(CAROL) == 0
