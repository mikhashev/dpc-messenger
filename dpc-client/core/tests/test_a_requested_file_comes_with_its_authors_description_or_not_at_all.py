"""A file asked for comes with its author's description, from whichever holder serves it.

ADR-047 phase 1 on the FILE_REQUEST path (managers/file_request.py), where a
file most often comes from a node that is not its author:

- a second source that changes the described hash is refused with
  `file_signature_rejected`, and the next holder is asked;
- a record that holds the author's description takes no offer without it,
  or with another (`not_the_requested_file`): a stripped signature cannot
  downgrade the file to legacy;
- a holder re-serves the description its record holds, byte for byte;
- W2: an offer whose author's certificate the requester lacks is accepted only
  once the certificate has come; a holder that withholds it is given up after
  the answer wait (`no_answer`) and the next holder is asked; the last holder's
  offer is taken single-source and checked again at the end;
- a one-to-one file is signed for its recipient, and the author's record
  carries the description under the id it names.

Over the in-process FILE_REQUEST network of the earlier request tests, with
the real keys and certificates of
test_a_file_description_is_signed_by_its_author_and_checked_on_arrival.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.managers.file_request import FILE_REQUEST, NOT_THE_REQUESTED_FILE_REASON
from dpc_client_core.managers.file_transfer_manager import (
    FILE_SIGNATURE_REJECTED_REASON,
    NO_ANSWER_REASON,
)
from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.service import CoreService
from dpc_client_core.signing import FILE_UNVERIFIED, FILE_VERIFIED
from tests.test_a_file_description_is_signed_by_its_author_and_checked_on_arrival import (  # noqa: F401
    CONTENT,
    _AlteringNetwork,
    _altered_description,
    identities,
    root,
)
from tests.test_a_group_file_crosses_a_full_mesh_once import GROUP, _ids, full_mesh, records
from tests.test_a_requested_file_is_taken_only_when_it_is_the_file_requested import _outcome
from tests.test_a_synced_record_without_its_file_can_be_requested_from_a_peer_online import (
    _a_file_sent_while_c_was_away,
    _RequestNetwork,
)


class _Net(_AlteringNetwork, _RequestNetwork):
    """FILE_REQUEST, frames that can be altered, and CERT_REQUEST answered where told."""


async def _setup(identities, root, *, certificate_known=True):
    """A sends a signed notes.txt to B while C is away; C syncs A's record (with the description)."""
    net = _Net()
    a_id = _ids(1)[0]
    if certificate_known:
        identities.know(a_id)
    identities.signing_as = a_id
    try:
        a, b, c, _ = await _a_file_sent_while_c_was_away(net, root)
    finally:
        identities.signing_as = None
    (authors,) = records(a)
    (synced,) = records(c)
    assert synced["attachments"][0]["file_description"] == authors["attachments"][0]["file_description"]
    assert records(b)[0]["attachments"][0]["file_description"] == authors["attachments"][0]["file_description"]
    net.frames.clear()
    return net, a, b, c, synced


def _holders_verify(*nodes):
    """Each of `nodes` holds the certificate that checks the author's description.

    A holder re-serves only a description that verifies on it. This harness
    keeps one certificates folder for every node, so "C lacks the author's
    certificate, A and B hold it" (A by its own key, B from its handshake with
    A) is said here, per holder, rather than by the folder.
    """
    from dpc_client_core.managers.file_transfer_manager import wire_file_description
    for node in nodes:
        node.manager.file_requests._served_description = (
            lambda att, group_id, requester: wire_file_description(att.get("file_description")))


def _received(c, rec):
    (after,) = records(c)
    assert after["id"] == rec["id"]
    att = after["attachments"][0]
    assert Path(att["file_path"]).read_bytes() == CONTENT
    return att


def _cancels_to(net, node_id, reason):
    return [f for f in net.cancels(reason) if f[0] == node_id]


@pytest.mark.asyncio
async def test_a_second_source_cannot_forge_a_files_hash(root, identities):
    net, a, b, c, rec = await _setup(identities, root)
    # A record from before hashes and descriptions: only the name and size to go by,
    # so the description is all that tells a forged offer from the file.
    rec["attachments"][0].pop("hash")
    rec["attachments"][0].pop("file_description")
    net.mutate[(a.me, c.me, "FILE_OFFER")] = _altered_description(hash="0" * 64)

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    assert len(_cancels_to(net, a.me, FILE_SIGNATURE_REJECTED_REASON)) == 1
    assert [f[0] for f in net.named(FILE_REQUEST)] == [a.me, b.me], "the forger refused, B asked next"
    att = _received(c, rec)
    assert att["file_verdict"] == FILE_VERIFIED
    assert "content_verified" not in att, "the author's key ties the bytes to the record"


@pytest.mark.asyncio
async def test_a_stripped_description_is_refused_as_not_the_requested_file(root, identities):
    net, a, b, c, rec = await _setup(identities, root)
    net.mutate[(a.me, c.me, "FILE_OFFER")] = lambda p: {k: v for k, v in p.items() if k != "file_description"}

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "received", final
    assert len(_cancels_to(net, a.me, NOT_THE_REQUESTED_FILE_REASON)) == 1
    assert [f[0] for f in net.named(FILE_REQUEST)] == [a.me, b.me]
    assert _received(c, rec)["file_verdict"] == FILE_VERIFIED


@pytest.mark.asyncio
async def test_another_valid_description_is_refused_as_not_the_requested_file(root, identities):
    """Signed by the author all right, but for another message: not this record's file."""
    net, a, b, c, rec = await _setup(identities, root)
    desc = rec["attachments"][0]["file_description"]
    identities.signing_as = a.me
    from dpc_client_core.signing import sign_file_description
    other = sign_file_description(conversation_id=GROUP, **dict(
        {k: v for k, v in desc.items() if k not in ("file_preimage_version", "file_signature")},
        message_id="another-message"))
    identities.signing_as = None
    net.mutate[(a.me, c.me, "FILE_OFFER")] = lambda p: dict(p, file_description=other,
                                                            message_id="another-message")

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "received", final
    assert len(_cancels_to(net, a.me, NOT_THE_REQUESTED_FILE_REASON)) == 1


@pytest.mark.asyncio
async def test_a_holder_sends_the_authors_description_byte_for_byte(root, identities):
    net, a, b, c, rec = await _setup(identities, root)
    c.service.p2p_coordinator = SimpleNamespace(get_connected_peers=lambda: [b.me])  # A is away

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "received", final
    (offer,) = net.offers_to(c.me)
    assert offer[1] == b.me
    authors = records(a)[0]["attachments"][0]["file_description"]
    assert json.dumps(offer[3]["file_description"]) == json.dumps(authors)
    assert _received(c, rec)["file_verdict"] == FILE_VERIFIED


@pytest.mark.asyncio
async def test_a_withheld_certificate_times_out_and_the_next_candidate_is_asked(root, identities):
    net, a, b, c, rec = await _setup(identities, root, certificate_known=False)
    _holders_verify(a, b)
    c.manager.file_requests.answer_timeout_s = 0.3
    net.certificates_from[b.me] = (identities, {a.me})  # A withholds its own; B answers

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    assert [f[0] for f in net.named(FILE_REQUEST)] == [a.me, b.me]
    certificate_asks = [(f[0], f[3]["node_id"]) for f in net.frames if f[2] == "CERT_REQUEST"]
    assert certificate_asks == [(a.me, a.me), (b.me, a.me)], "each holder is asked for the author's"
    assert not [f for f in net.frames if f[2] == "FILE_ACCEPT" and f[0] == a.me], "A's offer never accepted"
    assert len(_cancels_to(net, a.me, NO_ANSWER_REASON)) == 1
    assert not a.manager.active_transfers, "A's upload slot is freed"
    att = _received(c, rec)
    assert att["file_verdict"] == FILE_VERIFIED, "the last holder's offer, checked again at the end"


@pytest.mark.asyncio
async def test_a_held_offer_is_accepted_as_soon_as_its_certificate_comes(root, identities):
    net, a, b, c, rec = await _setup(identities, root, certificate_known=False)
    _holders_verify(a, b)
    c.manager.file_requests.answer_timeout_s = 5.0
    net.certificates_from[a.me] = (identities, {a.me})

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "received", final
    assert [f[0] for f in net.named(FILE_REQUEST)] == [a.me], "B was never needed"
    assert _received(c, rec)["file_verdict"] == FILE_VERIFIED


@pytest.mark.asyncio
async def test_when_no_holder_sends_the_certificate_the_last_one_serves_unverified(root, identities):
    net, a, b, c, rec = await _setup(identities, root, certificate_known=False)
    _holders_verify(a, b)
    c.manager.file_requests.answer_timeout_s = 0.2

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "received", final
    assert [f[0] for f in net.named(FILE_REQUEST)] == [a.me, b.me]
    att = _received(c, rec)
    assert att["file_verdict"] == FILE_UNVERIFIED
    assert att["content_verified"] is False, "kept, never content-verified"


# --- one to one -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_one_to_one_file_is_signed_for_its_recipient_and_both_records_keep_it(root, identities):
    net = _Net()
    author, peer = full_mesh(net, root, 2)
    identities.know(author.me)
    path = root / "outbox" / "notes.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(CONTENT)

    identities.signing_as = author.me
    try:
        await P2PCoordinator.send_file(SimpleNamespace(service=author.service), peer.me, str(path))
    finally:
        identities.signing_as = None
    await net.settle()
    (shown,) = peer.api.named("file_transfer_offered")
    await P2PCoordinator.accept_file_transfer(
        SimpleNamespace(service=peer.service, p2p_manager=peer.service.p2p_manager), shown["transfer_id"])
    await net.settle()

    assert not net.errors, net.errors
    (offer,) = net.offers_to(peer.me)
    desc = offer[3]["file_description"]
    assert desc["sender_node_id"] == author.me and "group_id" not in offer[3]
    (authors,) = author.monitors[peer.me].message_history
    assert authors["id"] == desc["message_id"], "the record carries the id the description names"
    assert authors["attachments"][0]["file_description"] == desc
    (theirs,) = peer.monitors[author.me].message_history
    assert theirs["attachments"][0]["file_description"] == desc
    assert theirs["attachments"][0]["file_verdict"] == FILE_VERIFIED
    # The same description handed to a third node in a one-to-one chat with the
    # recipient is another chat's: rejected (§4.2 rule 5, by the recipient's id).
    third = _ids(3)[2]
    replay = {"transfer_id": "t-replay", **{k: offer[3][k] for k in
                                             ("filename", "size_bytes", "hash", "mime_type")},
              "file_description": desc}
    from dpc_client_core.managers.file_transfer_manager import FileTransferManager
    fake = SimpleNamespace(p2p_manager=SimpleNamespace(node_id=third))
    _, verdict, _ = FileTransferManager.offer_file_description(fake, peer.me, replay)
    assert verdict == "rejected"
