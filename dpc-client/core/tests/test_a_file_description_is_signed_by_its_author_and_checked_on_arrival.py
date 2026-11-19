"""A file carries its author's signed description, and no hop can change what it says.

ADR-047 phase 1 (`dptp-file-v1`, specs/dptp_v1.md §4.4). Before it, a file's
hash, name and MIME type were whatever the node that offered it said: a relay
or a second holder could serve other bytes under the author's name, or the
right bytes as `invoice.exe`. Now the author signs, once per message, the ten
fields of the file preimage, and the description travels on the offer:

- every field is inside the signature, the signer must be the author, and the
  conversation is the receiver's own, so a description replayed into another
  group is rejected (unit, real RSA keys);
- an offer whose name, size, hash, MIME type or group identity is not the
  described one is refused with FILE_CANCEL `file_signature_rejected`;
- a group file arrives `verified`, its description stored on the receiver's
  record, the author's on the author's record; a relay re-serves it byte for
  byte, and a relay that alters it is refused;
- a description that holds binds the bytes even with `verify_hash` off
  (`hash_mismatch`), and the `chunks_hash` is recomputed at the end, whatever
  wire chunk carried the file (a pinned 65537 included);
- no certificate for the author: `unverified`, a CERT_REQUEST to the hop, and
  checked again at the end (`verified` once the certificate came, kept but
  marked `unverified` when it did not);
- an offer without a description is legacy and accepted as today, with nothing
  stored (M2's default);
- a history round trip keeps the description and drops the local verdict.

Real FileTransferManager and handlers over the in-process network of
test_a_group_file_crosses_a_full_mesh_once; `CommitSigner` signs and verifies
for real against certificates in a temporary peers folder.
"""

import asyncio
import functools
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol import commit_integrity
from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.message_signing import FILE_PREIMAGE_VERSION, file_content_hash
from dpc_client_core import signing
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.managers.chunk_window import description_chunk_size
from dpc_client_core.managers.file_transfer_manager import (
    FILE_SIGNATURE_REJECTED_REASON,
    hash_file_in_one_pass,
)
from dpc_client_core.signing import (
    FILE_LEGACY,
    FILE_REJECTED,
    FILE_UNVERIFIED,
    FILE_VERIFIED,
    file_description_verdict,
    sign_file_description,
)
from tests.test_a_group_file_crosses_a_full_mesh_once import (
    GROUP,
    _Network,
    full_mesh,
    records,
    send,
    star,
)

CONTENT = b"x" * 1000  # what the harness sends as notes.txt
CONTENT_HASH = hashlib.sha256(CONTENT).hexdigest()


# --- identities: real keys, certificates in a temporary peers folder ------------------

@functools.lru_cache(maxsize=None)
def _key(node_id: str):
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _certificate_pem(node_id: str) -> bytes:
    key = _key(node_id)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
            .serial_number(x509.random_serial_number()).public_key(key.public_key())
            .sign(private_key=key, algorithm=hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


class Identities:
    """Which node signs now, and which certificates this process 'holds'."""

    def __init__(self, home: Path):
        self.peers = home / "peers"
        self.peers.mkdir(parents=True)
        self.signing_as = None  # node id whose key `node_signer` returns, or None (no key)

    def signer(self):
        if self.signing_as is None:
            return None
        return CommitSigner(self.signing_as, _key(self.signing_as))

    def know(self, *node_ids):
        for node_id in node_ids:
            (self.peers / f"{node_id}.crt").write_bytes(_certificate_pem(node_id))

    def forget(self, *node_ids):
        for node_id in node_ids:
            (self.peers / f"{node_id}.crt").unlink(missing_ok=True)


@pytest.fixture
def identities(tmp_path, monkeypatch):
    ids = Identities(tmp_path / "dpc-home")
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path / "dpc-home")
    monkeypatch.setattr(signing, "node_signer", ids.signer)
    return ids


@pytest.fixture
def root(tmp_path, monkeypatch):
    from dpc_client_core import service as service_mod
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path / "home")
    return tmp_path


A = "dpc-node-" + "a" * 32
B = "dpc-node-" + "b" * 32
OTHER_GROUP = "group-ffffffffffff"


def _fields(**overrides):
    fields = dict(
        message_id="m-1", sender_node_id=A, size_bytes=len(CONTENT),
        chunk_size=description_chunk_size(len(CONTENT)), hash=CONTENT_HASH,
        chunks_hash=hashlib.sha256(hashlib.sha256(CONTENT).digest()).hexdigest(),
        filename="notes.txt", mime_type="text/plain",
    )
    fields.update(overrides)
    return fields


def _signed(identities, conversation_id=GROUP, signer=A, **overrides):
    identities.signing_as = signer
    desc = sign_file_description(conversation_id=conversation_id, **_fields(**overrides))
    identities.signing_as = None
    return desc


# --- (1) the description and its verdict, for real ---------------------------------

def test_a_description_signed_by_its_author_verifies_in_its_own_group(identities):
    identities.know(A)
    desc = _signed(identities)

    assert list(desc) == list(signing.FILE_DESCRIPTION_FIELDS)
    assert desc["file_preimage_version"] == FILE_PREIMAGE_VERSION == "dptp-file-v1"
    assert file_description_verdict(desc, GROUP) == FILE_VERIFIED


@pytest.mark.parametrize("field,value", [
    ("message_id", "m-2"),
    ("sender_node_id", B),
    ("size_bytes", len(CONTENT) + 1),
    ("chunk_size", 1 << 20),
    ("hash", "0" * 64),
    ("chunks_hash", "1" * 64),
    ("filename", "invoice.exe"),
    ("mime_type", "application/x-msdownload"),
])
def test_a_tampered_file_description_is_refused_for_every_field(identities, field, value):
    identities.know(A, B)
    desc = dict(_signed(identities), **{field: value})

    assert file_description_verdict(desc, GROUP) == FILE_REJECTED


def test_a_description_signed_by_anyone_but_its_author_is_refused(identities):
    """B signs a description that names A as the author."""
    identities.know(A, B)
    fields = _fields()
    forged = {"file_preimage_version": FILE_PREIMAGE_VERSION, **fields,
              "file_signature": CommitSigner(B, _key(B)).sign_commit(
                  file_content_hash(conversation_id=GROUP, **fields))}

    assert file_description_verdict(forged, GROUP) == FILE_REJECTED


def test_a_description_replayed_into_another_group_is_refused(identities):
    identities.know(A)
    desc = _signed(identities)

    assert file_description_verdict(desc, OTHER_GROUP) == FILE_REJECTED
    assert file_description_verdict(desc, None) == FILE_REJECTED, "a 1:1 chat it is not a party of"


def test_no_certificate_is_unverified_and_no_description_or_another_version_is_legacy(identities):
    desc = _signed(identities)

    assert file_description_verdict(desc, GROUP) == FILE_UNVERIFIED
    assert file_description_verdict(None, GROUP) == FILE_LEGACY
    assert file_description_verdict(dict(desc, file_preimage_version="dptp-file-v2"), GROUP) == FILE_LEGACY
    assert file_description_verdict(dict(desc, hash=CONTENT_HASH.upper()), GROUP) == FILE_REJECTED


def test_a_node_whose_key_is_another_nodes_signs_nothing(identities):
    identities.signing_as = B
    assert sign_file_description(conversation_id=GROUP, **_fields()) == {}
    identities.signing_as = None
    assert sign_file_description(conversation_id=GROUP, **_fields()) == {}, "no key: legacy"


@pytest.mark.parametrize("wire_chunk", [65536, 65537, 4096, 1 << 20, 3])
def test_a_misaligned_wire_chunk_gives_the_same_chunks_hash(tmp_path, wire_chunk):
    data = bytes(range(256)) * 900 + b"tail"  # 230 404 bytes: three full 64 KiB slices and a short one
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    slice_size = description_chunk_size(len(data))
    expected = hashlib.sha256(b"".join(
        hashlib.sha256(data[i:i + slice_size]).digest() for i in range(0, len(data), slice_size)
    )).hexdigest()

    file_hash, crcs, chunks_hash = hash_file_in_one_pass(
        path, wire_chunk, description_chunk_size=slice_size)

    assert chunks_hash == expected
    assert file_hash == hashlib.sha256(data).hexdigest()
    assert len(crcs) == -(-len(data) // wire_chunk), "the CRCs stay per wire chunk"
    assert hash_file_in_one_pass(path, wire_chunk)[:2] == (file_hash, crcs), "without it, as before"


def test_an_empty_file_has_the_chunks_hash_of_nothing(tmp_path):
    path = tmp_path / "empty"
    path.write_bytes(b"")
    assert hash_file_in_one_pass(path, 65536, description_chunk_size=65536)[2] == hashlib.sha256(b"").hexdigest()


def test_the_description_chunk_size_follows_the_file_size_alone():
    assert description_chunk_size(1000) == 64 * 1024
    assert description_chunk_size(500 * 1024 * 1024) == 1 << 20
    assert description_chunk_size(2 << 30) == 4 << 20
    # Raised to keep the list within 2^17 entries: a 1 TiB file in 8 MiB slices.
    assert description_chunk_size(1 << 40) == 8 << 20


# --- (2) a group file, over the in-process network ------------------------------------

def _descriptions_on_the_wire(net, to=None):
    return [f[3].get("file_description") for f in net.frames
            if f[2] == "FILE_OFFER" and (to is None or f[0] == to)]


async def _send_signed(identities, net, author, root):
    identities.signing_as = author.me
    try:
        await send(author, "file", root)
    finally:
        identities.signing_as = None
    await net.settle()


@pytest.mark.asyncio
async def test_a_group_file_arrives_verified_and_both_records_keep_the_authors_description(root, identities):
    net = _Network()
    author, b = full_mesh(net, root, 2)
    identities.know(author.me)

    await _send_signed(identities, net, author, root)

    assert not net.errors, net.errors
    (authors,) = records(author)
    desc = authors["attachments"][0]["file_description"]
    assert desc["sender_node_id"] == author.me and desc["message_id"] == authors["id"]
    assert desc["hash"] == CONTENT_HASH and desc["filename"] == "notes.txt"
    (theirs,) = records(b)
    att = theirs["attachments"][0]
    assert att["file_description"] == desc
    assert att["file_verdict"] == FILE_VERIFIED
    assert Path(att["file_path"]).read_bytes() == CONTENT
    (offer,) = net.offers_to(b.me)
    assert offer[3]["hash"] == desc["hash"]


@pytest.mark.asyncio
async def test_a_relay_sends_the_authors_description_byte_for_byte(root, identities):
    net = _Network()
    hub, author, leaf = star(net, root, leaves=2)
    identities.know(author.me)

    await _send_signed(identities, net, author, root)

    assert not net.errors, net.errors
    to_hub, = _descriptions_on_the_wire(net, hub.me)
    to_leaf, = _descriptions_on_the_wire(net, leaf.me)
    authors = records(author)[0]["attachments"][0]["file_description"]
    assert json.dumps(to_leaf) == json.dumps(to_hub) == json.dumps(authors), "same keys, same order, same values"
    assert records(leaf)[0]["attachments"][0]["file_verdict"] == FILE_VERIFIED


class _AlteringNetwork(_Network):
    """The harness network, where a frame from one node to another can be altered."""

    def __init__(self):
        super().__init__()
        self.mutate = {}  # (from, to, command) -> payload -> payload
        self.certificates_from = {}  # node -> (identities, author ids it answers CERT_REQUEST with)

    async def _deliver(self, to, frm, message):
        change = self.mutate.get((frm, to, message["command"]))
        if change is not None:
            message = dict(message, payload=change(dict(message["payload"])))
        if message["command"] == "CERT_REQUEST":
            try:
                await asyncio.sleep(0)
                self.frames.append((to, frm, "CERT_REQUEST", message["payload"]))
                ids, authors = self.certificates_from.get(to, (None, ()))
                wanted = message["payload"]["node_id"]
                if wanted in authors:  # CERT_RESPONSE, as certificate_handler stores and wakes it
                    ids.know(wanted)
                    self.nodes[frm].manager.certificate_arrived(wanted)
            finally:
                self.in_flight -= 1
            return
        return await super()._deliver(to, frm, message)


def _altered_description(**changes):
    def change(payload):
        desc = dict(payload["file_description"], **changes)
        payload = dict(payload, file_description=desc)
        for key in ("hash", "filename", "mime_type", "size_bytes"):
            if key in changes:
                payload[key] = changes[key]  # an offer that agrees with its altered description
        return payload
    return change


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("hash", "0" * 64), ("filename", "invoice.exe"), ("mime_type", "application/x-msdownload"),
])
async def test_a_relay_that_alters_the_description_is_refused(root, identities, field, value):
    net = _AlteringNetwork()
    hub, author, leaf = star(net, root, leaves=2)
    identities.know(author.me)
    net.mutate[(hub.me, leaf.me, "FILE_OFFER")] = _altered_description(**{field: value})

    await _send_signed(identities, net, author, root)

    refused = [f for f in net.cancels(FILE_SIGNATURE_REJECTED_REASON) if f[0] == hub.me]
    assert len(refused) == 1, net.cancels()
    assert records(leaf) == [], "nothing lands"
    assert not leaf.manager.active_transfers
    assert records(hub)[0]["attachments"][0]["file_verdict"] == FILE_VERIFIED, "the hub's copy is fine"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["filename", "size_bytes", "hash", "mime_type", "message_id"])
async def test_an_offer_that_is_not_the_file_it_describes_is_refused(root, identities, field):
    """The description intact, the offer around it changed: the receiver acts on the offer."""
    net = _AlteringNetwork()
    author, b = full_mesh(net, root, 2)
    identities.know(author.me)
    bad = {"filename": "invoice.exe", "size_bytes": 999, "hash": "0" * 64,
           "mime_type": "application/x-msdownload", "message_id": "another"}[field]
    net.mutate[(author.me, b.me, "FILE_OFFER")] = lambda p: dict(p, **{field: bad})

    await _send_signed(identities, net, author, root)

    assert len(net.cancels(FILE_SIGNATURE_REJECTED_REASON)) == 1
    assert records(b) == []


@pytest.mark.asyncio
async def test_a_valid_description_and_other_bytes_is_a_hash_mismatch_even_with_verify_hash_off(root, identities):
    net = _Network()
    author, b = full_mesh(net, root, 2)
    b.manager.verify_hash = False
    # A sender that does not hash what it sends (its own verify_hash off) cannot
    # notice that its copy is not the described file; an honest one leaves the
    # description off (`send_file`).
    author.manager.verify_hash = False
    identities.know(author.me)
    other = root / "outbox" / "other" / "notes.txt"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"y" * len(CONTENT))  # same name, same size, other bytes
    desc = _signed(identities, signer=author.me, sender_node_id=author.me)

    await author.manager.send_file(node_id=b.me, file_path=other, group_id=GROUP,
                                   message_id="m-1", file_description=desc)
    await net.settle()

    (offer,) = net.offers_to(b.me)
    assert offer[3]["hash"] == CONTENT_HASH, "the offer says what the description says"
    assert len(net.cancels("hash_mismatch")) == 1
    assert records(b) == [] and not b.manager.active_transfers


@pytest.mark.asyncio
async def test_a_signed_chunks_hash_that_is_not_the_files_keeps_the_file_and_rejects_the_description(root, identities):
    net = _Network()
    author, b = full_mesh(net, root, 2)
    identities.know(author.me)
    path = root / "outbox" / "notes.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(CONTENT)
    desc = _signed(identities, signer=author.me, sender_node_id=author.me, chunks_hash="2" * 64)

    await author.manager.send_file(node_id=b.me, file_path=path, group_id=GROUP,
                                   message_id="m-1", file_description=desc)
    await net.settle()

    (rec,) = records(b)
    att = rec["attachments"][0]
    assert Path(att["file_path"]).read_bytes() == CONTENT
    assert att["file_verdict"] == FILE_REJECTED
    assert "file_description" not in att, "never re-served"


@pytest.mark.asyncio
async def test_a_wire_chunk_of_65537_carries_a_file_whose_description_still_verifies(root, identities):
    net = _Network()
    author, b = full_mesh(net, root, 2)
    identities.know(author.me)
    data = bytes(range(256)) * 900 + b"tail"
    path = root / "outbox" / "odd.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    slice_size = description_chunk_size(len(data))
    file_hash, _, chunks_hash = hash_file_in_one_pass(path, slice_size, description_chunk_size=slice_size)
    desc = _signed(identities, signer=author.me, sender_node_id=author.me, size_bytes=len(data),
                   hash=file_hash, chunks_hash=chunks_hash, filename="odd.bin",
                   mime_type="application/octet-stream", chunk_size=slice_size)

    await author.manager.send_file(node_id=b.me, file_path=path, group_id=GROUP, message_id="m-1",
                                   chunk_size=65537, file_description=desc)
    await net.settle()

    assert not net.errors, net.errors
    (offer,) = net.offers_to(b.me)
    assert offer[3]["chunk_size"] == 65537 and offer[3]["file_description"]["chunk_size"] == 65536
    (rec,) = records(b)
    assert rec["attachments"][0]["file_verdict"] == FILE_VERIFIED
    assert Path(rec["attachments"][0]["file_path"]).read_bytes() == data


@pytest.mark.asyncio
async def test_an_offer_without_a_description_is_legacy_accepted_and_nothing_is_stored(root, identities):
    net = _Network()
    author, b = full_mesh(net, root, 2)  # identities.signing_as stays None: the author has no key

    await send(author, "file", root)
    await net.settle()

    assert not net.errors, net.errors
    assert _descriptions_on_the_wire(net) == [None]
    (rec,) = records(b)
    att = rec["attachments"][0]
    assert Path(att["file_path"]).read_bytes() == CONTENT
    assert "file_description" not in att and "file_verdict" not in att
    assert "file_description" not in records(author)[0]["attachments"][0]


@pytest.mark.asyncio
async def test_an_unverified_description_becomes_verified_once_the_certificate_arrives(root, identities):
    net = _AlteringNetwork()
    author, b = full_mesh(net, root, 2)
    net.certificates_from[author.me] = (identities, {author.me})  # the hop answers CERT_REQUEST

    await _send_signed(identities, net, author, root)

    assert not net.errors, net.errors
    asked = [f for f in net.frames if f[2] == "CERT_REQUEST"]
    assert [(f[0], f[1], f[3]["node_id"]) for f in asked] == [(author.me, b.me, author.me)], \
        "unverified at the offer: the receiver asked its hop"
    (rec,) = records(b)
    assert rec["attachments"][0]["file_verdict"] == FILE_VERIFIED
    assert "content_verified" not in rec["attachments"][0]


@pytest.mark.asyncio
async def test_a_certificate_that_never_comes_leaves_the_file_kept_and_marked_unverified(root, identities):
    net = _AlteringNetwork()
    author, b = full_mesh(net, root, 2)  # nobody answers CERT_REQUEST

    await _send_signed(identities, net, author, root)

    (rec,) = records(b)
    att = rec["attachments"][0]
    assert Path(att["file_path"]).read_bytes() == CONTENT, "single source, as today"
    assert att["file_verdict"] == FILE_UNVERIFIED
    assert att["file_description"]["sender_node_id"] == author.me


# --- (3) history ---------------------------------------------------------------------

def _monitor(monkeypatch, local):
    monkeypatch.setattr(ConversationMonitor, "_get_signer", lambda self: None)
    return ConversationMonitor(
        conversation_id=GROUP,
        participants=[{"node_id": local, "name": "self", "context": "local"}],
        llm_manager=None,
    )


def test_a_history_round_trip_keeps_the_description_and_drops_the_verdict(identities, monkeypatch, tmp_path):
    identities.know(A)
    desc = _signed(identities, message_id="m-1")
    elsewhere = _signed(identities, message_id="m-other")
    sender = _monitor(monkeypatch, B)
    attachment = {"type": "file", "filename": "notes.txt", "size_bytes": len(CONTENT),
                  "hash": CONTENT_HASH, "status": "completed"}
    sender.message_history = [
        {"id": "m-1", "role": "assistant", "content": "", "sender_node_id": A,
         "attachments": [dict(attachment, file_description=desc, file_verdict=FILE_VERIFIED)]},
        {"id": "m-2", "role": "assistant", "content": "", "sender_node_id": A,
         "attachments": [dict(attachment, file_description=elsewhere, file_verdict=FILE_VERIFIED)]},
    ]
    exported = sender.export_history()

    receiver = _monitor(monkeypatch, "dpc-node-" + "c" * 32)
    receiver.import_history(exported)

    kept, misfiled = receiver.message_history
    assert kept["attachments"][0]["file_description"] == desc
    assert "file_verdict" not in kept["attachments"][0], "the verdict is the receiver's, computed on use"
    assert "file_description" not in misfiled["attachments"][0], "another record's description"
    assert "file_verdict" not in misfiled["attachments"][0]
