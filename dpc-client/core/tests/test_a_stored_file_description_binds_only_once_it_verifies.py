"""A file description this node could not verify binds nothing, travels nowhere, and does not last.

Found in review of ADR-047 phase 1 by a fresh subagent, 2026-11-20. A
description that arrived before its author's certificate (with a synced
record, or on a push) was stored as if it were the author's: a FILE_REQUEST
took no offer but one carrying exactly it, holders and relays re-served it,
history sync exported it, and nothing looked at it again once the certificate
came. So a member could plant a junk-signature description under the right
`message_id` and author in the history it exported, and every honest holder
of the file was then refused as `not_the_requested_file`, by the victim and
by every member the victim synced it on to. Now:

- a stored description binds a request only when it verifies here; one that
  does not yet gives way to an offered one that fits the record (its id, its
  author, its hash and size) and whose signature the offer handler checks;
- when the author's certificate arrives, its stored descriptions are checked
  again: one that verifies is marked so, one that does not is dropped;
- a merge keeps the verified description over an unverified one;
- an unverified description is not re-served on FILE_REQUEST, relayed with a
  group file, or exported with history.
"""

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor, merge_received_attachment
from dpc_client_core.managers.file_request import NOT_THE_REQUESTED_FILE_REASON
from dpc_client_core.message_handlers.certificate_handler import CertificateResponseHandler
from dpc_client_core.service import CoreService
from dpc_client_core.signing import FILE_LEGACY, FILE_UNVERIFIED, FILE_VERIFIED
from tests.test_a_file_description_is_signed_by_its_author_and_checked_on_arrival import (  # noqa: F401
    A,
    B,
    CONTENT,
    CONTENT_HASH,
    _AlteringNetwork,
    _descriptions_on_the_wire,
    _send_signed,
    _signed,
    identities,
    root,
)
from tests.test_a_group_file_crosses_a_full_mesh_once import GROUP, records, star
from tests.test_a_requested_file_comes_with_its_authors_description_or_not_at_all import (
    _cancels_to,
    _holders_verify,
    _received,
    _setup,
)
from tests.test_a_requested_file_is_taken_only_when_it_is_the_file_requested import _outcome
from tests.test_a_synced_record_without_its_file_can_be_requested_from_a_peer_online import _synced_copy


def _junk(desc):
    """The author's description with a signature that is nobody's: what a member can plant."""
    return dict(desc, file_signature=base64.b64encode(b"\x00" * 256).decode())


# --- a planted description does not block an honest holder ---------------------------

@pytest.mark.asyncio
async def test_a_description_planted_on_import_does_not_block_an_honest_verified_offer(root, identities, monkeypatch):
    net, a, b, c, rec = await _setup(identities, root, certificate_known=False)
    _holders_verify(identities, a, b)  # A and B hold A's certificate; C does not
    net.certificates_from[a.me] = (identities, {a.me})
    c.manager.file_requests.answer_timeout_s = 5.0
    real = records(a)[0]["attachments"][0]["file_description"]
    # A member exports C a history in which A's record carries a junk signature.
    planted = _synced_copy(records(a)[0], root)
    planted["attachments"] = [dict(planted["attachments"][0], file_description=_junk(real))]
    planted["attachments"][0].pop("file_verdict", None)
    # The real import (this harness's monitors are stand-ins), its result put in C's history.
    importer = _monitor(monkeypatch, c.me)
    importer.import_history([planted])
    (imported,) = importer.message_history
    assert imported["attachments"][0]["file_description"] == _junk(real), \
        "no certificate for A at the import: the junk cannot be told from the real one yet"
    c.service._get_or_create_conversation_monitor(GROUP).message_history[:] = [imported]

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    assert not net.cancels(NOT_THE_REQUESTED_FILE_REASON), "the honest author was not refused"
    assert [f[0] for f in net.named("FILE_REQUEST")] == [a.me]
    att = _received(c, rec)
    assert att["file_description"] == real, "the verified description replaced the planted one"
    assert att["file_verdict"] == FILE_VERIFIED


@pytest.mark.asyncio
async def test_a_description_that_verifies_here_still_binds_the_request(root, identities):
    """The fix lifts the binding from an unverified description only: another one is refused."""
    net, a, b, c, rec = await _setup(identities, root)
    net.mutate[(a.me, c.me, "FILE_OFFER")] = lambda p: dict(p, file_description=_junk(p["file_description"]))

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "received", final
    assert len(_cancels_to(net, a.me, NOT_THE_REQUESTED_FILE_REASON)) == 1


@pytest.mark.asyncio
async def test_an_offered_description_that_is_not_the_records_does_not_replace_an_unverified_one(root, identities):
    """Non-binding is not anything-goes: another author's or another message's is still refused."""
    net, a, b, c, rec = await _setup(identities, root, certificate_known=False)
    _holders_verify(identities, a, b)
    real = records(a)[0]["attachments"][0]["file_description"]
    net.mutate[(a.me, c.me, "FILE_OFFER")] = lambda p: dict(
        p, file_description=dict(real, sender_node_id=B))

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    await _outcome(net, c, answer["request_id"])

    assert len(_cancels_to(net, a.me, NOT_THE_REQUESTED_FILE_REASON)) == 1


# --- the certificate arrives -------------------------------------------------------

def _monitor(monkeypatch, local):
    monkeypatch.setattr(ConversationMonitor, "_get_signer", lambda self: None)
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: False))
    return ConversationMonitor(
        conversation_id=GROUP,
        participants=[{"node_id": local, "name": "self", "context": "local"}],
        llm_manager=None,
    )


def _attachment(**extra):
    return dict({"type": "file", "filename": "notes.txt", "size_bytes": len(CONTENT),
                 "hash": CONTENT_HASH, "status": "completed"}, **extra)


@pytest.mark.asyncio
async def test_a_certificate_arrival_drops_a_stored_description_that_no_longer_holds(identities, monkeypatch):
    real = _signed(identities, message_id="m-1")
    other = _signed(identities, message_id="m-2")
    monitor = _monitor(monkeypatch, B)
    monitor.message_history = [
        {"id": "m-1", "role": "assistant", "content": "", "sender_node_id": A,
         "attachments": [_attachment(file_description=_junk(real), file_verdict=FILE_UNVERIFIED)]},
        {"id": "m-2", "role": "assistant", "content": "", "sender_node_id": A,
         "attachments": [_attachment(file_description=other)]},
    ]
    events = []

    async def _broadcast(event, payload):
        events.append(event)

    service = SimpleNamespace(
        p2p_manager=SimpleNamespace(
            _persist_peer_certificate=lambda node_id, pem: (identities.know(node_id), True)[1]),
        pending_certificate_requests=set(),
        conversation_monitors={GROUP: monitor},
        local_api=SimpleNamespace(broadcast_event=_broadcast),
    )

    await CertificateResponseHandler(service).handle(B, {"node_id": A, "certificate": "PEM"})

    planted, honest = monitor.message_history
    assert "file_description" not in planted["attachments"][0], "the junk is gone"
    assert "file_verdict" not in planted["attachments"][0]
    assert honest["attachments"][0]["file_description"] == other
    assert honest["attachments"][0]["file_verdict"] == FILE_VERIFIED


def test_a_descriptions_by_another_author_are_left_for_their_own_certificate(identities, monkeypatch):
    identities.know(A)
    by_b = _signed(identities, signer=B, sender_node_id=B)
    monitor = _monitor(monkeypatch, A)
    monitor.message_history = [
        {"id": "m-1", "role": "assistant", "content": "", "sender_node_id": B,
         "attachments": [_attachment(file_description=by_b)]}]

    assert monitor.reverify_file_descriptions(A) == 0
    assert monitor.message_history[0]["attachments"][0]["file_description"] == by_b


# --- merges ------------------------------------------------------------------------

def test_a_merge_keeps_the_verified_description_over_an_unverified_one(identities):
    real = _signed(identities, message_id="m-1")
    record = {"id": "m-1", "sender_node_id": A}

    target = _attachment(file_description=_junk(real))  # came with the synced record
    merge_received_attachment(target, _attachment(file_description=real, file_verdict=FILE_VERIFIED),
                              record=record)
    assert target["file_description"] == real and target["file_verdict"] == FILE_VERIFIED

    kept = _attachment(file_description=real, file_verdict=FILE_VERIFIED)
    merge_received_attachment(kept, _attachment(file_description=_junk(real), file_verdict=FILE_UNVERIFIED),
                              record=record)
    assert kept["file_description"] == real and kept["file_verdict"] == FILE_VERIFIED, \
        "an unverified one never replaces a verified one"


def _local_note(local, **attachment):
    """This node's own "Received file" note for A's file: attributed to A, signed by `local`."""
    return {"id": "note-1", "role": "user", "content": "Received file notes.txt",
            "sender_node_id": A, "signer_node_id": local, "timestamp": "2026-11-20T01:00:00+00:00",
            "attachments": [_attachment(**attachment)]}


def test_a_planted_verdict_does_not_displace_the_description_this_node_verified(identities, monkeypatch):
    """Found in the second review by a fresh subagent, 2026-11-20.

    A member syncs A's real record carrying junk under A's name plus
    `file_verdict: verified`. The merge into this node's own note read the
    planted verdict as a checked one and kept the junk; the remap then dropped
    it, and with it the description this node verified.
    """
    identities.know(A)
    real = _signed(identities, message_id="m-1")
    me = "dpc-node-" + "c" * 32
    monitor = _monitor(monkeypatch, me)
    monitor.message_history = [_local_note(me, file_description=real, file_verdict=FILE_VERIFIED,
                                           file_path="/somewhere/notes.txt")]
    monitor.rebuild_message_ids()
    arriving = {"id": "m-1", "role": "user", "content": "", "sender_node_id": A,
                "timestamp": "2026-11-20T00:59:00+00:00",
                "attachments": [_attachment(file_description=_junk(real), file_verdict=FILE_VERIFIED)]}

    monitor.merge_history([arriving])

    (stored,) = monitor.message_history
    assert stored["id"] == "m-1", "the author's record replaced the note"
    assert stored["attachments"][0]["file_description"] == real, "the verified description stands"
    assert arriving["attachments"][0]["file_verdict"] == FILE_VERIFIED, "the caller's dict is not changed"


def test_a_record_claiming_this_nodes_id_keeps_no_planted_verdict(identities, monkeypatch):
    """A legacy record under this node's own id skips the remap; its verdict is dropped all the same."""
    identities.know(A)
    real = _signed(identities, message_id="m-2")
    me = "dpc-node-" + "c" * 32
    monitor = _monitor(monkeypatch, me)
    arriving = {"id": "m-2", "role": "user", "content": "", "sender_node_id": me,
                "timestamp": "2026-11-20T00:59:00+00:00",
                "attachments": [_attachment(file_description=_junk(real), file_verdict=FILE_VERIFIED)]}

    assert monitor.merge_history([arriving]) == 1

    (stored,) = monitor.message_history
    assert "file_verdict" not in stored["attachments"][0], "a verdict is never imported"
    from dpc_client_core.managers.file_request import _held_description
    assert _held_description(stored["attachments"][0], group_id=GROUP, my_node_id=me,
                             peer_node_id=None) == (None, None), "the junk binds nothing"


# --- an unverified description travels nowhere --------------------------------------

@pytest.mark.asyncio
async def test_a_holder_does_not_re_serve_a_description_it_cannot_verify(root, identities):
    net, a, b, c, rec = await _setup(identities, root, certificate_known=False)
    c.service.p2p_coordinator = SimpleNamespace(get_connected_peers=lambda: [b.me])  # A is away
    assert records(b)[0]["attachments"][0]["file_verdict"] == FILE_UNVERIFIED

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "received", final
    (offer,) = net.offers_to(c.me)
    assert offer[1] == b.me
    assert "file_description" not in offer[3], "B could not check it, so B does not vouch for it"
    assert Path(_received(c, rec)["file_path"]).read_bytes() == CONTENT


@pytest.mark.asyncio
async def test_a_relay_does_not_pass_on_a_description_it_cannot_verify(root, identities):
    net = _AlteringNetwork()
    hub, author, leaf = star(net, root, leaves=2)  # nobody holds the author's certificate

    await _send_signed(identities, net, author, root)

    assert not net.errors, net.errors
    (to_hub,) = _descriptions_on_the_wire(net, hub.me)
    assert to_hub is not None, "the author sends it"
    assert records(hub)[0]["attachments"][0]["file_verdict"] == FILE_UNVERIFIED
    assert _descriptions_on_the_wire(net, leaf.me) == [None], "the hub does not vouch for it"
    (theirs,) = records(leaf)
    assert theirs["attachments"][0]["file_verdict"] == FILE_LEGACY
    assert Path(theirs["attachments"][0]["file_path"]).read_bytes() == CONTENT


def test_history_sync_exports_a_description_only_once_it_verifies(identities, monkeypatch):
    real = _signed(identities, message_id="m-1")
    monitor = _monitor(monkeypatch, B)
    monitor.message_history = [
        {"id": "m-1", "role": "assistant", "content": "", "sender_node_id": A,
         "attachments": [_attachment(file_description=real, file_verdict=FILE_UNVERIFIED)]}]

    (exported,) = monitor.export_history()
    assert "file_description" not in exported["attachments"][0]
    assert monitor.message_history[0]["attachments"][0]["file_description"] == real, "kept here"

    identities.know(A)
    (exported,) = monitor.export_history()
    assert exported["attachments"][0]["file_description"] == real
