"""A commit from another node is checked before it is written, and never
re-signed with our key unless we could check it.

Until this landed the receive path took `commit_id` on trust, recomputed the
hash from the sender's own content and signed the result with our key — so a
tampered commit arrived, was applied, and left carrying our attestation.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dpc_protocol.crypto import generate_node_id
from dpc_protocol.knowledge_commit import KnowledgeCommit
from dpc_protocol.pcm_core import KnowledgeEntry, KnowledgeSource, PCMCore
from dpc_client_core.consensus_manager import ConsensusManager
from dpc_client_core.message_handlers.knowledge_handler import ApplyKnowledgeCommitHandler

SENDER = "dpc-node-relaying-peer"


def _identity(peers_dir=None):
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


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A DPC home of our own, so nothing here reads or writes the real one."""
    import dpc_protocol.commit_integrity as integrity
    import dpc_protocol.markdown_manager as markdown

    monkeypatch.setattr(integrity, "DPC_HOME_DIR", tmp_path)
    monkeypatch.setattr(markdown, "DPC_HOME_DIR", tmp_path)
    return tmp_path


class _Applied:
    """Stands in for the consensus manager and remembers how it was called."""

    def __init__(self):
        self.calls = []

    async def _apply_commit(self, commit, origin="local"):
        self.calls.append((commit, origin))
        return True


def _handler(applied, sent):
    service = SimpleNamespace(
        consensus_manager=applied,
        pcm_core=SimpleNamespace(load_context=lambda: SimpleNamespace(commit_history=[])),
        pending_certificate_requests=set(),
        p2p_manager=SimpleNamespace(
            node_id="dpc-node-us",
            peers={SENDER: object()},
            send_message_to_peer=lambda peer, msg: sent.append((peer, msg)),
        ),
    )
    return ApplyKnowledgeCommitHandler(service)


@pytest.mark.asyncio
async def test_a_tampered_commit_is_refused_and_never_reaches_apply(home, caplog):
    signer, key = _identity(home / "peers")
    commit = _commit()
    commit.sign(signer, key)
    commit.summary = "An RTX 3060 carries 24 GB of VRAM"

    applied, sent = _Applied(), []
    with caplog.at_level("WARNING"):
        await _handler(applied, sent).handle(SENDER, commit.to_dict())

    assert applied.calls == []
    assert any("Refused" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_without_the_check_the_same_commit_would_be_applied(home, monkeypatch):
    """Neutralisation: with the verdict forced to `verified` the refusal above
    disappears, so the first test measures the gate and not the fixture."""
    signer, key = _identity(home / "peers")
    commit = _commit()
    commit.sign(signer, key)
    commit.summary = "An RTX 3060 carries 24 GB of VRAM"

    from dpc_protocol.knowledge_commit import CommitProvenance

    monkeypatch.setattr(
        KnowledgeCommit,
        "verify_provenance",
        lambda self, peers_dir=None: CommitProvenance("verified", "neutralised"),
    )

    applied, sent = _Applied(), []
    await _handler(applied, sent).handle(SENDER, commit.to_dict())

    assert len(applied.calls) == 1


@pytest.mark.asyncio
async def test_a_verified_commit_is_applied_and_says_so(home):
    signer, key = _identity(home / "peers")
    commit = _commit()
    commit.sign(signer, key)

    applied, sent = _Applied(), []
    await _handler(applied, sent).handle(SENDER, commit.to_dict())

    assert [origin for _, origin in applied.calls] == ["verified"]
    assert sent == []


@pytest.mark.asyncio
async def test_an_uncheckable_commit_is_flagged_and_its_signer_asked_for(home):
    (home / "peers").mkdir(parents=True)
    signer, key = _identity()  # certificate deliberately not cached
    commit = _commit()
    commit.sign(signer, key)

    applied, sent = _Applied(), []
    await _handler(applied, sent).handle(SENDER, commit.to_dict())

    assert [origin for _, origin in applied.calls] == ["unverified"]
    assert [(peer, msg["payload"]["node_id"]) for peer, msg in sent] == [(SENDER, signer)]


def _manager(home, monkeypatch, key=None):
    """A consensus manager whose disk is the temp home."""
    node_id = "dpc-node-us"
    if key is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = home / "node.key"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    import dpc_protocol.crypto as crypto

    monkeypatch.setattr(
        crypto, "load_identity", lambda: (node_id, key_path, home / "node.crt")
    )
    pcm = PCMCore(home / "personal.json")
    pcm.ensure_context_file_exists()
    return ConsensusManager(node_id=node_id, pcm_core=pcm)


@pytest.mark.asyncio
async def test_a_commit_we_could_not_check_is_not_signed_with_our_key(home, monkeypatch):
    manager = _manager(home, monkeypatch)
    signed = []

    async def _remember(commit):
        signed.append(commit)

    manager.on_commit_signed = _remember

    signer, key = _identity()
    commit = _commit()
    commit.sign(signer, key)
    arrived_as = commit.commit_hash

    assert await manager._apply_commit(commit, origin="unverified") is True

    assert list(commit.signatures) == [signer]
    assert commit.commit_hash == arrived_as
    assert signed == []


@pytest.mark.asyncio
async def test_a_received_commit_keeps_the_parent_it_arrived_with(home, monkeypatch):
    manager = _manager(home, monkeypatch)
    context = manager.pcm_core.load_context()
    context.last_commit_id = "commit-ours"
    manager.pcm_core.save_context(context)

    signer, key = _identity()
    commit = _commit()
    commit.sign(signer, key)

    await manager._apply_commit(commit, origin="unverified")

    assert commit.parent_commit_id is None


@pytest.mark.asyncio
async def test_our_own_commit_is_still_hashed_and_signed(home, monkeypatch):
    manager = _manager(home, monkeypatch)
    signed = []

    async def _remember(commit):
        signed.append(commit)

    manager.on_commit_signed = _remember

    commit = _commit()
    commit.commit_hash = None

    assert await manager._apply_commit(commit) is True

    assert commit.commit_hash
    assert list(commit.signatures) == ["dpc-node-us"]
    assert signed == [commit]
