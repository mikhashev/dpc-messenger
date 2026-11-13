"""
Tests for DHT certificate discovery in gossip protocol.

Validates that:
- Local certificate is published to DHT on startup
- Peer certificates can be queried from DHT
- Certificate retrieval falls back to DHT when not in cache
"""

import base64
import json

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, MagicMock, patch
from pathlib import Path
import tempfile

from cryptography.hazmat.primitives import serialization

from dpc_client_core.managers.gossip_manager import GossipManager
from dpc_client_core.dht.routing import DHTNode


def generate_test_certificate():
    """Helper to generate valid test certificate with matching node_id.

    Computes the node_id from the generated key so that gossip_manager's
    fingerprint validation (Sprint 2 DHT cert spoofing fix) accepts the cert.
    Returns (cert_pem, cert, private_key, node_id).
    """
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from datetime import datetime, timedelta, timezone
    from dpc_protocol.crypto import generate_node_id

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    # Derive node_id from the key so the fingerprint check passes
    node_id = generate_node_id(public_key)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, node_id)
    ])

    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        public_key
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.now(timezone.utc)
    ).not_valid_after(
        datetime.now(timezone.utc) + timedelta(days=365)
    ).sign(private_key, hashes.SHA256())

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode('utf-8')
    return cert_pem, cert, private_key, node_id


@pytest.fixture
def temp_dpc_dir():
    """Create temporary .dpc directory with test certificate."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from datetime import datetime, timedelta, timezone

    with tempfile.TemporaryDirectory() as tmpdir:
        dpc_dir = Path(tmpdir)

        # Generate real test certificate
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "dpc-node-alice123")
        ])

        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            public_key
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            datetime.now(timezone.utc)
        ).not_valid_after(
            datetime.now(timezone.utc) + timedelta(days=365)
        ).sign(private_key, hashes.SHA256())

        # Save certificate as PEM
        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode('utf-8')
        cert_path = dpc_dir / "node.crt"
        cert_path.write_text(cert_pem)

        yield dpc_dir


@pytest.fixture(autouse=True)
def peers_dir(tmp_path, monkeypatch):
    """An empty peers store; the gossip lookup reads it through p2p_manager.DPC_HOME_DIR."""
    import dpc_client_core.p2p_manager as p2p_module

    monkeypatch.setattr(p2p_module, "DPC_HOME_DIR", tmp_path)
    store = tmp_path / "peers"
    store.mkdir()
    return store


@pytest.fixture
def mock_dht_manager():
    """Create mock DHT manager."""
    dht = Mock()
    dht.find_node = AsyncMock(return_value=[
        DHTNode(node_id="dpc-node-dht1", ip="10.0.0.1", port=8889),
        DHTNode(node_id="dpc-node-dht2", ip="10.0.0.2", port=8889),
        DHTNode(node_id="dpc-node-dht3", ip="10.0.0.3", port=8889),
    ])
    dht.rpc_handler = Mock()
    dht.rpc_handler.store = AsyncMock(return_value=True)
    dht.rpc_handler.find_value = AsyncMock()

    return dht


@pytest.fixture
def mock_p2p_manager(mock_dht_manager):
    """Create mock P2P manager with DHT."""
    p2p = Mock()
    p2p.dht_manager = mock_dht_manager
    p2p.peer_cache = None
    p2p.peers = {}
    return p2p


@pytest.fixture
def gossip_manager(mock_p2p_manager):
    """Create GossipManager instance."""
    return GossipManager(
        p2p_manager=mock_p2p_manager,
        node_id="dpc-node-alice123"
    )


class TestCertificatePublishing:
    """Test publishing local certificate to DHT."""

    @pytest.mark.asyncio
    async def test_publish_certificate_on_startup(self, gossip_manager, mock_dht_manager, temp_dpc_dir):
        """Test that certificate is published to DHT when GossipManager starts."""

        # Mock environment variable
        with patch.dict('os.environ', {'DPC_DIR': str(temp_dpc_dir)}):
            await gossip_manager._publish_certificate_to_dht()

        # Should find k closest nodes
        mock_dht_manager.find_node.assert_called_once_with("dpc-node-alice123")

        # Should store certificate on all nodes
        assert mock_dht_manager.rpc_handler.store.call_count == 3

        # Verify store calls
        for call in mock_dht_manager.rpc_handler.store.call_args_list:
            args = call[0]
            # args: (ip, port, key, value)
            assert args[2] == "cert:dpc-node-alice123"  # Key format
            assert "BEGIN CERTIFICATE" in args[3]  # PEM format

    @pytest.mark.asyncio
    async def test_publish_handles_missing_dht(self, gossip_manager, mock_p2p_manager):
        """Test graceful handling when DHT not available."""

        mock_p2p_manager.dht_manager = None

        # Should not crash
        await gossip_manager._publish_certificate_to_dht()

    @pytest.mark.asyncio
    async def test_publish_handles_missing_certificate(self, gossip_manager):
        """Test graceful handling when certificate file missing."""

        with tempfile.TemporaryDirectory() as tmpdir:
            # Empty directory (no certificate)
            with patch.dict('os.environ', {'DPC_DIR': tmpdir}):
                await gossip_manager._publish_certificate_to_dht()

        # Should not crash


class TestCertificateQuerying:
    """Test querying DHT for peer certificates."""

    @pytest.mark.asyncio
    async def test_query_dht_for_certificate(self, gossip_manager, mock_dht_manager):
        """Test successful certificate retrieval from DHT."""

        # Generate valid test certificate with matching node_id
        cert_pem, cert_obj, private_key, node_id = generate_test_certificate()

        # First DHT node returns the certificate
        mock_dht_manager.rpc_handler.find_value.return_value = {
            "value": cert_pem
        }

        cert = await gossip_manager._query_dht_for_certificate(node_id)

        # Should find k closest nodes
        mock_dht_manager.find_node.assert_called_once_with(node_id)

        # Should query DHT nodes
        mock_dht_manager.rpc_handler.find_value.assert_called()

        # Should return X.509 certificate object
        assert cert is not None
        from cryptography import x509
        assert isinstance(cert, x509.Certificate)

    @pytest.mark.asyncio
    async def test_query_dht_certificate_not_found(self, gossip_manager, mock_dht_manager):
        """Test when certificate not found in DHT."""

        # DHT returns no value
        mock_dht_manager.rpc_handler.find_value.return_value = {}

        cert = await gossip_manager._query_dht_for_certificate("dpc-node-unknown")

        # Should return None
        assert cert is None

    @pytest.mark.asyncio
    async def test_query_dht_handles_no_nodes(self, gossip_manager, mock_dht_manager):
        """Test when no DHT nodes available."""

        mock_dht_manager.find_node.return_value = []

        cert = await gossip_manager._query_dht_for_certificate("dpc-node-bob456")

        # Should return None gracefully
        assert cert is None


class TestCertificateRetrievalFlow:
    """Test complete certificate retrieval flow with fallback."""

    @pytest.mark.asyncio
    async def test_get_certificate_from_the_peers_store(self, gossip_manager, mock_p2p_manager, peers_dir):
        """A certificate P2PManager has proved and stored is found without the DHT."""
        from dpc_client_core.p2p_manager import P2PManager

        cert_pem, _cert, _key, node_id = generate_test_certificate()
        stored = P2PManager._persist_peer_certificate(
            Mock(), node_id, cert_pem, peers_dir=peers_dir
        )
        assert stored is True
        assert (peers_dir / f"{node_id}.crt").is_file()

        cert = await gossip_manager._get_peer_certificate(node_id)

        assert cert is not None
        assert cert.public_bytes(serialization.Encoding.PEM).decode("utf-8") == cert_pem
        assert not mock_p2p_manager.dht_manager.find_node.called

    @pytest.mark.asyncio
    async def test_a_stored_certificate_of_another_node_is_refused(
        self, gossip_manager, mock_p2p_manager, mock_dht_manager, peers_dir
    ):
        """A file named for one node but holding another node's key is not used."""
        cert_pem, _cert, _key, _other_id = generate_test_certificate()
        victim_id = "dpc-node-" + "ab" * 16
        (peers_dir / f"{victim_id}.crt").write_text(cert_pem, encoding="utf-8")
        mock_dht_manager.rpc_handler.find_value.return_value = {}

        cert = await gossip_manager._get_peer_certificate(victim_id)

        assert cert is None
        # it fell through to the DHT instead
        mock_dht_manager.find_node.assert_called_once_with(victim_id)

    @pytest.mark.asyncio
    async def test_an_unreadable_stored_certificate_falls_through_to_the_dht(
        self, gossip_manager, mock_dht_manager, peers_dir
    ):
        node_id = "dpc-node-" + "cd" * 16
        (peers_dir / f"{node_id}.crt").write_text("not a certificate", encoding="utf-8")
        mock_dht_manager.rpc_handler.find_value.return_value = {}

        assert await gossip_manager._get_peer_certificate(node_id) is None
        mock_dht_manager.find_node.assert_called_once_with(node_id)

    @pytest.mark.asyncio
    async def test_payload_is_encrypted_with_dht_empty_and_certificate_on_disk(
        self, gossip_manager, mock_dht_manager, peers_dir
    ):
        """Falsifier of the card: nothing from the DHT, the cert only in the peers store."""
        from dpc_client_core.p2p_manager import P2PManager
        from dpc_protocol.crypto import decrypt_with_private_key_hybrid

        cert_pem, _cert, private_key, node_id = generate_test_certificate()
        assert P2PManager._persist_peer_certificate(
            Mock(), node_id, cert_pem, peers_dir=peers_dir
        )
        mock_dht_manager.find_node.return_value = []

        encrypted = await gossip_manager._encrypt_payload({"text": "hello"}, node_id)

        assert isinstance(encrypted, str) and encrypted
        plain = decrypt_with_private_key_hybrid(base64.b64decode(encrypted), private_key)
        assert json.loads(plain.decode("utf-8")) == {"text": "hello"}
        assert not mock_dht_manager.rpc_handler.find_value.called

    @pytest.mark.asyncio
    async def test_get_certificate_falls_back_to_dht(self, gossip_manager, mock_dht_manager):
        """Test fallback to DHT when certificate not in cache."""

        # Generate valid test certificate with matching node_id
        cert_pem, cert_obj, private_key, node_id = generate_test_certificate()

        mock_dht_manager.rpc_handler.find_value.return_value = {
            "value": cert_pem
        }

        cert = await gossip_manager._get_peer_certificate(node_id)

        # Should query DHT
        mock_dht_manager.find_node.assert_called_once_with(node_id)

        # Should return certificate from DHT
        assert cert is not None

    @pytest.mark.asyncio
    async def test_get_certificate_returns_none_when_all_fail(self, gossip_manager, mock_dht_manager):
        """Test when certificate not found anywhere."""

        # DHT returns no value
        mock_dht_manager.rpc_handler.find_value.return_value = {}

        cert = await gossip_manager._get_peer_certificate("dpc-node-unknown")

        # Should return None
        assert cert is None


class TestIntegration:
    """Integration tests for DHT certificate discovery."""

    @pytest.mark.asyncio
    async def test_full_flow_publish_and_retrieve(self, gossip_manager, mock_dht_manager, temp_dpc_dir):
        """Test complete flow: publish certificate then retrieve it."""

        # Publish Alice's certificate
        with patch.dict('os.environ', {'DPC_DIR': str(temp_dpc_dir)}):
            await gossip_manager._publish_certificate_to_dht()

        # Verify publish succeeded
        assert mock_dht_manager.rpc_handler.store.call_count == 3

        # Mock DHT to return the stored certificate (Alice's cert from publish)
        stored_cert_pem = mock_dht_manager.rpc_handler.store.call_args_list[0][0][3]

        # Query for another node's certificate (simulating cross-node retrieval)
        # Generate Bob's certificate with matching node_id
        bob_cert_pem, bob_cert_obj, bob_private_key, bob_node_id = generate_test_certificate()

        mock_dht_manager.rpc_handler.find_value.return_value = {
            "value": bob_cert_pem
        }

        # Query for Bob's certificate (will get from DHT)
        cert = await gossip_manager._get_peer_certificate(bob_node_id)

        # Should successfully retrieve certificate
        assert cert is not None

    @pytest.mark.asyncio
    async def test_encryption_works_with_dht_certificate(self, gossip_manager, mock_dht_manager):
        """Test that E2E encryption works with certificate retrieved from DHT."""

        # Create real test certificates
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from datetime import datetime, timedelta, timezone

        # Generate Bob's keypair
        bob_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        bob_public_key = bob_private_key.public_key()

        # Derive node_id from the key so the fingerprint check passes
        from dpc_protocol.crypto import generate_node_id
        bob_node_id = generate_node_id(bob_public_key)

        # Create Bob's certificate using the derived node_id as CN
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, bob_node_id)
        ])
        bob_cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            bob_public_key
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            datetime.now(timezone.utc)
        ).not_valid_after(
            datetime.now(timezone.utc) + timedelta(days=365)
        ).sign(bob_private_key, hashes.SHA256())

        # Store Bob's certificate in mock DHT
        bob_cert_pem = bob_cert.public_bytes(serialization.Encoding.PEM).decode('utf-8')
        mock_dht_manager.rpc_handler.find_value.return_value = {
            "value": bob_cert_pem
        }

        # Test encryption with DHT-retrieved certificate
        test_payload = {"command": "SEND_TEXT", "text": "Hello via DHT!"}

        # Retrieve Bob's cert from DHT
        cert = await gossip_manager._get_peer_certificate(bob_node_id)
        assert cert is not None

        # Encrypt payload (should work with DHT certificate)
        from dpc_protocol.crypto import encrypt_with_public_key_hybrid, decrypt_with_private_key_hybrid
        import json

        payload_json = json.dumps(test_payload)
        encrypted_bytes = encrypt_with_public_key_hybrid(
            payload_json.encode('utf-8'),
            cert.public_key()
        )

        # Bob can decrypt with his private key
        decrypted_bytes = decrypt_with_private_key_hybrid(encrypted_bytes, bob_private_key)
        decrypted_payload = json.loads(decrypted_bytes.decode('utf-8'))

        assert decrypted_payload == test_payload
