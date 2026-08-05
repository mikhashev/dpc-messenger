"""A message that crossed a relay must still name the node that wrote it.

Measured 2026-08-06 on a three-node star: seven of nine records on the edges
named the relay as author, because the receiver took the author from the socket
the message arrived on. This is the criterion ADR-036 is judged by, so it is
tested against the real handler with a real signature rather than a stand-in.
"""

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol import commit_integrity
from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.crypto import generate_node_id
from dpc_protocol.message_signing import PREIMAGE_VERSION, message_content_hash
from dpc_client_core.message_handlers.group_handler import GroupTextHandler


GROUP = "group-1234"
TIMESTAMP = "2026-08-06T00:00:00+00:00"


@pytest.fixture
def author(tmp_path, monkeypatch):
    """A real key, a real certificate, cached where verification looks."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    peers = tmp_path / "peers"
    peers.mkdir()
    (peers / f"{node_id}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    return node_id, CommitSigner(node_id, key)


def _signed_payload(author_node_id, signer, text="Это Linux", **overrides):
    payload = {
        "group_id": GROUP,
        "text": text,
        "sender_name": "Mike (linux)",
        "sender_type": "human",
        "sender_node_id": author_node_id,
        "agent_owner": None,
        "message_id": "m-1",
        "timestamp": TIMESTAMP,
        "mentions": [],
    }
    payload["content_hash"] = message_content_hash(
        conversation_id=GROUP, message_id=payload["message_id"],
        sender_node_id=author_node_id, sender_name=payload["sender_name"],
        sender_type=payload["sender_type"], agent_owner=None,
        timestamp=TIMESTAMP, content=text, tool_calls=None,
    )
    payload["signature"] = signer.sign_commit(payload["content_hash"])
    payload["signer_node_id"] = author_node_id
    payload["preimage_version"] = PREIMAGE_VERSION
    payload.update(overrides)
    return payload


RELAY = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"


def _handler():
    return GroupTextHandler(service=None)


def test_the_author_is_taken_from_the_signature_not_the_socket(author):
    author_node_id, signer = author
    payload = _signed_payload(author_node_id, signer)

    who, verdict, fields = _handler()._authenticate_author(RELAY, payload)

    assert who == author_node_id, "a relayed message kept the relay as its author"
    assert verdict == "verified"
    assert fields["signer_node_id"] == author_node_id


def test_altered_text_is_rejected(author):
    author_node_id, signer = author
    payload = _signed_payload(author_node_id, signer)
    payload["text"] = "transfer denied"  # hash and signature left intact

    who, verdict, fields = _handler()._authenticate_author(RELAY, payload)

    assert verdict == "rejected"
    assert fields is None


def test_a_signature_by_someone_other_than_the_claimed_author_is_rejected(author):
    author_node_id, signer = author
    payload = _signed_payload(author_node_id, signer)
    payload["sender_node_id"] = RELAY  # claims the relay wrote it

    _, verdict, _ = _handler()._authenticate_author(RELAY, payload)

    assert verdict == "rejected"


def test_a_message_from_an_old_node_is_kept_as_legacy(author):
    """Rejecting these would cut the network in half mid-upgrade."""
    author_node_id, signer = author
    payload = _signed_payload(author_node_id, signer)
    for field in ("content_hash", "signature", "signer_node_id"):
        payload.pop(field)

    who, verdict, fields = _handler()._authenticate_author(RELAY, payload)

    assert verdict == "legacy"
    assert who == RELAY, "an unbacked claim is worth less than the socket"
    assert fields is None


def test_an_uncacheable_certificate_yields_unverified_not_rejected(author, monkeypatch, tmp_path):
    """First contact must not be a denial of service against ourselves."""
    author_node_id, signer = author
    payload = _signed_payload(author_node_id, signer)
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path / "empty")

    who, verdict, fields = _handler()._authenticate_author(RELAY, payload)

    assert verdict == "unverified"
    assert who == author_node_id
    assert fields is not None


def test_a_signature_over_an_unknown_preimage_is_legacy_not_rejected(author):
    """A node one version off is an upgrade in progress, not an attacker."""
    author_node_id, signer = author
    payload = _signed_payload(author_node_id, signer, preimage_version="dptp-msg-v99")

    who, verdict, fields = _handler()._authenticate_author(RELAY, payload)

    assert verdict == "legacy"
    assert who == RELAY


def test_a_message_signed_for_another_room_does_not_verify_here(author):
    author_node_id, signer = author
    payload = _signed_payload(author_node_id, signer)
    payload["group_id"] = "group-elsewhere"

    _, verdict, _ = _handler()._authenticate_author(RELAY, payload)

    assert verdict == "rejected"
