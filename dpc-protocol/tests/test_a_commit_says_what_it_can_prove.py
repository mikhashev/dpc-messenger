"""verify_provenance separates "checked and held" from "nothing was checked".

The old verify_signatures() answered True for an empty signature dict and for a
signer whose certificate is not cached — two passes nothing had earned.
"""

from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dpc_protocol.crypto import generate_node_id
from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.knowledge_commit import KnowledgeCommit
from dpc_protocol.pcm_core import KnowledgeEntry, KnowledgeSource


def _identity(peers_dir=None):
    """A node id with its key; its certificate is cached only if peers_dir is given."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())

    if peers_dir is not None:
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .sign(private_key=key, algorithm=hashes.SHA256())
        )
        peers_dir.mkdir(parents=True, exist_ok=True)
        (peers_dir / f"{node_id}.crt").write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
        )

    return node_id, key


def _commit(summary="An RTX 3060 carries 12 GB of VRAM"):
    commit = KnowledgeCommit(
        topic="hardware",
        summary=summary,
        description="from the device context",
        entries=[
            KnowledgeEntry(
                content="12 GB VRAM",
                tags=["gpu"],
                source=KnowledgeSource(type="ai_summary"),
            )
        ],
        participants=["node-a", "node-b"],
        approved_by=["node-a", "node-b"],
        timestamp="2026-08-26T00:00:00+00:00",
    )
    commit.compute_hash()
    return commit


def test_a_signature_we_can_check_is_verified(tmp_path):
    peers = tmp_path / "peers"
    node_id, key = _identity(peers)

    commit = _commit()
    commit.sign(node_id, key)

    result = commit.verify_provenance(peers_dir=peers)

    assert result.verdict == "verified"
    assert result.unverifiable_signers == ()
    assert commit.verify_signatures(peers_dir=peers) is True


def test_content_that_moved_after_signing_is_rejected(tmp_path):
    peers = tmp_path / "peers"
    node_id, key = _identity(peers)

    commit = _commit()
    commit.sign(node_id, key)
    commit.summary = "An RTX 3060 carries 24 GB of VRAM"

    result = commit.verify_provenance(peers_dir=peers)

    assert result.verdict == "rejected"
    assert result.is_rejected
    assert "hashes to" in result.detail


def test_a_signature_over_something_else_is_rejected(tmp_path):
    peers = tmp_path / "peers"
    node_id, key = _identity(peers)

    other = _commit(summary="something the signer actually signed")
    signature = CommitSigner(node_id, key).sign_commit(other.commit_hash)

    commit = _commit()
    commit.signatures[node_id] = signature

    result = commit.verify_provenance(peers_dir=peers)

    assert result.verdict == "rejected"
    assert node_id in result.detail


def test_an_uncached_certificate_is_unverified_and_never_verified(tmp_path):
    peers = tmp_path / "peers"
    peers.mkdir()
    node_id, key = _identity()  # certificate deliberately not written

    commit = _commit()
    commit.sign(node_id, key)

    result = commit.verify_provenance(peers_dir=peers)

    assert result.verdict == "unverified"
    assert result.unverifiable_signers == (node_id,)
    assert commit.verify_signatures(peers_dir=peers) is False


def test_no_signature_at_all_is_legacy_and_never_verified(tmp_path):
    peers = tmp_path / "peers"
    peers.mkdir()

    commit = _commit()
    assert commit.signatures == {}

    result = commit.verify_provenance(peers_dir=peers)

    assert result.verdict == "legacy"
    assert commit.verify_signatures(peers_dir=peers) is False


def test_a_hashless_commit_proves_nothing(tmp_path):
    peers = tmp_path / "peers"
    peers.mkdir()

    commit = _commit()
    commit.commit_hash = None

    assert commit.verify_provenance(peers_dir=peers).verdict == "rejected"


def test_one_bad_signature_rejects_a_commit_another_node_signed_well(tmp_path):
    peers = tmp_path / "peers"
    honest_id, honest_key = _identity(peers)
    liar_id, liar_key = _identity(peers)

    commit = _commit()
    commit.sign(honest_id, honest_key)
    other = _commit(summary="a different commit entirely")
    commit.signatures[liar_id] = CommitSigner(liar_id, liar_key).sign_commit(other.commit_hash)

    result = commit.verify_provenance(peers_dir=peers)

    assert result.verdict == "rejected"
    assert liar_id in result.detail
