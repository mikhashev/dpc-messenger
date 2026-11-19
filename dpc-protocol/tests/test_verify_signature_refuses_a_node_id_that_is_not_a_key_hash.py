"""A signer's node id names a certificate file, so only a real node id may name one.

`CommitSigner.verify_signature(node_id, ...)` loads `peers/<node_id>.crt`, and
`node_id` comes from whoever signed: a group message's `signer_node_id`, a
vote's, a knowledge commit's signature keys, a file description's author. An
id such as `dpc-node-<victim>/../dpc-node-<attacker>` loaded the attacker's
own certificate and came back verified under a name that begins with the
victim's. Windows collapses `..` before it looks at the disk; elsewhere it
works once a directory of the first name exists, which this test makes. An
upper-case copy of an id names the same file on the case-folding volumes of
Windows and macOS. Found in review by a fresh subagent, 2026-11-20.

Now `is_valid_node_id` (exactly `generate_node_id`'s shape: `dpc-node-` and 32
lowercase hex characters) gates `verify_signature` before any path is built,
and anything else is False: not a node, so not a signature.
"""

from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.crypto import NODE_ID_RE, generate_node_id, is_valid_node_id

HASH = "ab" * 32


def _identity(peers_dir):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
            .serial_number(x509.random_serial_number()).public_key(key.public_key())
            .sign(private_key=key, algorithm=hashes.SHA256()))
    (peers_dir / f"{node_id}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return node_id, key


@pytest.fixture
def peers(tmp_path):
    peers = tmp_path / "peers"
    peers.mkdir()
    return peers


def test_a_real_node_id_still_verifies(peers):
    node_id, key = _identity(peers)
    signature = CommitSigner(node_id, key).sign_commit(HASH)

    assert is_valid_node_id(node_id)
    assert CommitSigner.verify_signature(node_id, HASH, signature, peers) is True


def test_a_traversing_id_does_not_load_the_attackers_certificate(peers):
    attacker, key = _identity(peers)
    victim = "dpc-node-" + "1" * 32
    (peers / victim).mkdir()  # so `..` resolves on every OS, not only on Windows
    forged = f"{victim}/../{attacker}"
    signature = CommitSigner(attacker, key).sign_commit(HASH)
    assert (peers / f"{forged}.crt").exists(), "the path does reach the attacker's certificate"

    assert CommitSigner.verify_signature(forged, HASH, signature, peers) is False
    assert CommitSigner.verify_signature(forged.replace("/", "\\"), HASH, signature, peers) is False


def test_an_upper_case_copy_of_an_id_is_not_that_node(peers):
    node_id, key = _identity(peers)
    signature = CommitSigner(node_id, key).sign_commit(HASH)
    upper = "dpc-node-" + node_id[len("dpc-node-"):].upper()

    assert CommitSigner.verify_signature(upper, HASH, signature, peers) is False
    assert CommitSigner.verify_signature(node_id.upper(), HASH, signature, peers) is False


@pytest.mark.parametrize("value", [
    None, 42, b"dpc-node-" + b"a" * 32, "", "dpc-node-", "dpc-node-" + "a" * 31,
    "dpc-node-" + "a" * 33, "dpc-node-" + "g" * 32, "dpc-node-" + "A" * 32,
    "DPC-NODE-" + "a" * 32, "dpc-node-" + "a" * 32 + "\n", " dpc-node-" + "a" * 32,
    "dpc-node-" + "a" * 32 + ".", "dpc-node-" + "a" * 16, "../dpc-node-" + "a" * 32,
    "dpc-node-" + "a" * 32 + "/../x", "C:\\dpc-node-" + "a" * 32,
])
def test_only_the_shape_generate_node_id_writes_is_a_node_id(value):
    assert not is_valid_node_id(value)


def test_the_shape_is_the_one_generate_node_id_writes():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    assert is_valid_node_id(node_id) and NODE_ID_RE.fullmatch(node_id)
    assert is_valid_node_id("dpc-node-" + "0123456789abcdef" * 2)
