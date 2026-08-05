"""A verified peer certificate must outlive the handshake that verified it.

`_verify_hello_identity` runs the strongest check in the project — CN, key
fingerprint, nonce proof — and then the cert went out of scope. With nothing
in ~/.dpc/peers/, `verify_signature` answers None ("cannot verify") for every
peer forever, so every message a peer ever signs is unverifiable.
"""

import base64
import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.crypto import generate_node_id
from dpc_protocol.commit_integrity import CommitSigner
from dpc_client_core.p2p_manager import P2PManager


def _identity(cn=None):
    """A node identity: private key, node_id derived from it, and a cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn or node_id)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key, node_id, pem


def _manager():
    """A P2PManager without touching ~/.dpc — only the cert methods are used."""
    return P2PManager.__new__(P2PManager)


def test_a_peer_signature_is_verifiable_after_the_handshake(tmp_path):
    """The whole point: what the handshake proved must still be usable later."""
    peer_key, peer_node_id, peer_cert_pem = _identity()
    peers_dir = tmp_path / "peers"

    signer = CommitSigner(peer_node_id, peer_key)
    signature = signer.sign_commit("a" * 64)

    # Before the handshake is remembered, the peer is a stranger.
    assert CommitSigner.verify_signature(
        peer_node_id, "a" * 64, signature, peers_dir=peers_dir
    ) is None

    stored = _manager()._persist_peer_certificate(peer_node_id, peer_cert_pem,
                                                  peers_dir=peers_dir)

    assert stored is True
    assert CommitSigner.verify_signature(
        peer_node_id, "a" * 64, signature, peers_dir=peers_dir
    ) is True


def test_a_cert_whose_key_does_not_hash_to_the_node_id_is_refused(tmp_path):
    """CN is a claim; the key fingerprint is the proof. Store only on proof.

    The outbound path validates CN alone (`_validate_peer_certificate`), so the
    store cannot inherit its caller's rigour — it has to re-derive the identity
    itself, or one weak caller poisons every later verification.
    """
    _, victim_node_id, _ = _identity()
    _, _, attacker_cert_pem = _identity(cn=victim_node_id)  # CN lies, key does not
    peers_dir = tmp_path / "peers"

    stored = _manager()._persist_peer_certificate(victim_node_id, attacker_cert_pem,
                                                  peers_dir=peers_dir)

    assert stored is False
    assert not (peers_dir / f"{victim_node_id}.crt").exists()


def test_a_reissued_cert_for_the_same_key_replaces_the_stored_one(tmp_path):
    """node_id is the key's fingerprint, so a same-key cert is the same peer."""
    key, node_id, first_pem = _identity()
    peers_dir = tmp_path / "peers"
    manager = _manager()

    assert manager._persist_peer_certificate(node_id, first_pem, peers_dir=peers_dir) is True

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    now = datetime.datetime.now(datetime.timezone.utc)
    reissued = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=730))
        .sign(key, hashes.SHA256())
    )
    reissued_pem = reissued.public_bytes(serialization.Encoding.PEM).decode()

    assert manager._persist_peer_certificate(node_id, reissued_pem, peers_dir=peers_dir) is True
    assert (peers_dir / f"{node_id}.crt").read_text() == reissued_pem


def test_garbage_is_refused_without_raising(tmp_path):
    """A malformed cert on the wire must not take the connection down with it."""
    _, node_id, _ = _identity()
    peers_dir = tmp_path / "peers"

    assert _manager()._persist_peer_certificate(node_id, "not a certificate",
                                                peers_dir=peers_dir) is False
    assert not peers_dir.exists() or not any(peers_dir.iterdir())
