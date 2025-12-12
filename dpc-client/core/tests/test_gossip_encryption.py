"""
Tests for gossip protocol end-to-end encryption.

Validates that:
- Messages are encrypted before forwarding
- Only sender and recipient can decrypt
- Intermediate hops cannot read message content
- Encryption uses RSA-OAEP with proper key management
"""

import pytest
import json
import base64
from unittest.mock import Mock, AsyncMock, MagicMock
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.x509 import CertificateBuilder, Name, NameAttribute
from cryptography.x509.oid import NameOID
from datetime import datetime, timedelta, timezone

from dpc_protocol.crypto import (
    encrypt_with_public_key,
    decrypt_with_private_key,
    encrypt_with_public_key_hybrid,
    decrypt_with_private_key_hybrid
)
from dpc_client_core.managers.gossip_manager import GossipManager


@pytest.fixture
def alice_keypair():
    """Generate RSA key pair for Alice."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )
    public_key = private_key.public_key()

    # Create certificate
    builder = CertificateBuilder()
    builder = builder.subject_name(Name([
        NameAttribute(NameOID.COMMON_NAME, "dpc-node-alice123")
    ]))
    builder = builder.issuer_name(Name([
        NameAttribute(NameOID.COMMON_NAME, "dpc-node-alice123")
    ]))
    builder = builder.not_valid_before(datetime.now(timezone.utc))
    builder = builder.not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
    builder = builder.serial_number(1)
    builder = builder.public_key(public_key)

    certificate = builder.sign(private_key, hashes.SHA256())

    return {
        "private_key": private_key,
        "public_key": public_key,
        "certificate": certificate,
        "node_id": "dpc-node-alice123"
    }


@pytest.fixture
def bob_keypair():
    """Generate RSA key pair for Bob."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )
    public_key = private_key.public_key()

    # Create certificate
    builder = CertificateBuilder()
    builder = builder.subject_name(Name([
        NameAttribute(NameOID.COMMON_NAME, "dpc-node-bob456")
    ]))
    builder = builder.issuer_name(Name([
        NameAttribute(NameOID.COMMON_NAME, "dpc-node-bob456")
    ]))
    builder = builder.not_valid_before(datetime.now(timezone.utc))
    builder = builder.not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
    builder = builder.serial_number(2)
    builder = builder.public_key(public_key)

    certificate = builder.sign(private_key, hashes.SHA256())

    return {
        "private_key": private_key,
        "public_key": public_key,
        "certificate": certificate,
        "node_id": "dpc-node-bob456"
    }


@pytest.fixture
def charlie_keypair():
    """Generate RSA key pair for Charlie (intermediate hop)."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )
    public_key = private_key.public_key()

    # Create certificate
    builder = CertificateBuilder()
    builder = builder.subject_name(Name([
        NameAttribute(NameOID.COMMON_NAME, "dpc-node-charlie789")
    ]))
    builder = builder.issuer_name(Name([
        NameAttribute(NameOID.COMMON_NAME, "dpc-node-charlie789")
    ]))
    builder = builder.not_valid_before(datetime.now(timezone.utc))
    builder = builder.not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
    builder = builder.serial_number(3)
    builder = builder.public_key(public_key)

    certificate = builder.sign(private_key, hashes.SHA256())

    return {
        "private_key": private_key,
        "public_key": public_key,
        "certificate": certificate,
        "node_id": "dpc-node-charlie789"
    }


class TestCryptoUtilities:
    """Test low-level encryption/decryption functions."""

    def test_encrypt_decrypt_roundtrip(self, bob_keypair):
        """Test that encryption and decryption work correctly."""
        # Original message
        plaintext = b"Hello, this is a secret message!"

        # Encrypt with Bob's public key
        ciphertext = encrypt_with_public_key(plaintext, bob_keypair["public_key"])

        # Verify ciphertext is different from plaintext
        assert ciphertext != plaintext
        assert len(ciphertext) > 0

        # Decrypt with Bob's private key
        decrypted = decrypt_with_private_key(ciphertext, bob_keypair["private_key"])

        # Verify decryption succeeded
        assert decrypted == plaintext

    def test_wrong_key_cannot_decrypt(self, alice_keypair, bob_keypair):
        """Test that wrong private key cannot decrypt message."""
        plaintext = b"Secret message for Bob"

        # Encrypt with Bob's public key
        ciphertext = encrypt_with_public_key(plaintext, bob_keypair["public_key"])

        # Try to decrypt with Alice's private key (should fail)
        with pytest.raises(Exception):
            decrypt_with_private_key(ciphertext, alice_keypair["private_key"])

    def test_encryption_produces_different_output_each_time(self, bob_keypair):
        """Test that OAEP padding makes each encryption unique."""
        plaintext = b"Same message encrypted twice"

        # Encrypt twice
        ciphertext1 = encrypt_with_public_key(plaintext, bob_keypair["public_key"])
        ciphertext2 = encrypt_with_public_key(plaintext, bob_keypair["public_key"])

        # Ciphertexts should be different (due to random padding)
        assert ciphertext1 != ciphertext2

        # Both should decrypt to same plaintext
        decrypted1 = decrypt_with_private_key(ciphertext1, bob_keypair["private_key"])
        decrypted2 = decrypt_with_private_key(ciphertext2, bob_keypair["private_key"])
        assert decrypted1 == decrypted2 == plaintext


class TestHybridEncryption:
    """Test hybrid encryption (AES-GCM + RSA-OAEP) functions."""

    def test_hybrid_encrypt_decrypt_roundtrip(self, bob_keypair):
        """Test hybrid encryption/decryption with small payload."""
        # Original message
        plaintext = b"Hello, this is encrypted with hybrid encryption!"

        # Encrypt with Bob's public key (hybrid)
        ciphertext = encrypt_with_public_key_hybrid(plaintext, bob_keypair["public_key"])

        # Verify ciphertext is different from plaintext
        assert ciphertext != plaintext
        assert len(ciphertext) > 0

        # Decrypt with Bob's private key (hybrid)
        decrypted = decrypt_with_private_key_hybrid(ciphertext, bob_keypair["private_key"])

        # Verify decryption succeeded
        assert decrypted == plaintext

    def test_hybrid_large_payload(self, bob_keypair):
        """Test hybrid encryption with large payload (beyond RSA limit)."""
        # Create payload much larger than RSA limit (190 bytes)
        plaintext = b"x" * 10000  # 10KB payload

        # Encrypt with hybrid encryption
        ciphertext = encrypt_with_public_key_hybrid(plaintext, bob_keypair["public_key"])

        # Decrypt
        decrypted = decrypt_with_private_key_hybrid(ciphertext, bob_keypair["private_key"])

        # Verify
        assert decrypted == plaintext

    def test_hybrid_wrong_key_cannot_decrypt(self, alice_keypair, bob_keypair):
        """Test that wrong key cannot decrypt hybrid-encrypted message."""
        plaintext = b"Secret message for Bob (hybrid)"

        # Encrypt with Bob's public key
        ciphertext = encrypt_with_public_key_hybrid(plaintext, bob_keypair["public_key"])

        # Try to decrypt with Alice's private key (should fail)
        with pytest.raises(Exception):
            decrypt_with_private_key_hybrid(ciphertext, alice_keypair["private_key"])

    def test_hybrid_authentication_failure_on_tamper(self, bob_keypair):
        """Test that GCM authentication detects tampering."""
        plaintext = b"Authentic message"

        # Encrypt
        ciphertext = encrypt_with_public_key_hybrid(plaintext, bob_keypair["public_key"])

        # Tamper with ciphertext (flip a bit in the encrypted data portion)
        tampered = bytearray(ciphertext)
        tampered[-10] ^= 0xFF  # Flip bits near end (in encrypted data)
        tampered = bytes(tampered)

        # Decryption should fail due to authentication tag mismatch
        with pytest.raises(Exception):
            decrypt_with_private_key_hybrid(tampered, bob_keypair["private_key"])

    def test_hybrid_different_ciphertext_each_time(self, bob_keypair):
        """Test that hybrid encryption produces different output each time (random AES key + nonce)."""
        plaintext = b"Same message encrypted twice with hybrid"

        # Encrypt twice
        ciphertext1 = encrypt_with_public_key_hybrid(plaintext, bob_keypair["public_key"])
        ciphertext2 = encrypt_with_public_key_hybrid(plaintext, bob_keypair["public_key"])

        # Ciphertexts should be different (random AES key and nonce)
        assert ciphertext1 != ciphertext2

        # Both should decrypt to same plaintext
        decrypted1 = decrypt_with_private_key_hybrid(ciphertext1, bob_keypair["private_key"])
        decrypted2 = decrypt_with_private_key_hybrid(ciphertext2, bob_keypair["private_key"])
        assert decrypted1 == decrypted2 == plaintext


class TestGossipManagerEncryption:
    """Test GossipManager encryption integration."""

    @pytest.mark.asyncio
    async def test_encrypt_payload_returns_base64(self, bob_keypair):
        """Test that _encrypt_payload returns base64-encoded string."""
        # Create mock GossipManager
        p2p_manager = Mock()
        manager = GossipManager(p2p_manager, "dpc-node-alice123")

        # Mock certificate retrieval
        manager._get_peer_certificate = AsyncMock(return_value=bob_keypair["certificate"])

        # Encrypt a payload
        payload = {"command": "HELLO", "data": "test"}
        encrypted = await manager._encrypt_payload(payload, bob_keypair["node_id"])

        # Should return base64 string
        assert isinstance(encrypted, str)

        # Should be valid base64
        try:
            base64.b64decode(encrypted)
        except Exception:
            pytest.fail("Encrypted payload is not valid base64")

    @pytest.mark.asyncio
    async def test_decrypt_payload_recovers_original(self, alice_keypair, bob_keypair, tmp_path):
        """Test that _decrypt_payload recovers original message (hybrid encryption)."""
        # Create mock GossipManager for Bob
        p2p_manager = Mock()
        bob_manager = GossipManager(p2p_manager, bob_keypair["node_id"])

        # Save Bob's private key to temp location
        key_file = tmp_path / "node.key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(
            bob_keypair["private_key"].private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
        )

        # Mock DPC_DIR to use temp path
        import os
        original_env = os.environ.get("DPC_DIR")
        os.environ["DPC_DIR"] = str(tmp_path)

        try:
            # Original payload
            original_payload = {"command": "SEND_TEXT", "text": "Hello Bob!"}

            # Encrypt with Bob's public key (hybrid encryption)
            payload_json = json.dumps(original_payload)
            encrypted_bytes = encrypt_with_public_key_hybrid(
                payload_json.encode('utf-8'),
                bob_keypair["public_key"]
            )
            encrypted_b64 = base64.b64encode(encrypted_bytes).decode('utf-8')

            # Decrypt with Bob's private key
            decrypted = await bob_manager._decrypt_payload(encrypted_b64)

            # Should match original
            assert decrypted == original_payload

        finally:
            # Restore environment
            if original_env is not None:
                os.environ["DPC_DIR"] = original_env
            elif "DPC_DIR" in os.environ:
                del os.environ["DPC_DIR"]

    @pytest.mark.asyncio
    async def test_intermediate_hop_cannot_decrypt(self, alice_keypair, bob_keypair, charlie_keypair, tmp_path):
        """Test that intermediate hop (Charlie) cannot decrypt message sent from Alice to Bob."""
        # Create Alice's manager
        p2p_manager = Mock()
        alice_manager = GossipManager(p2p_manager, alice_keypair["node_id"])
        alice_manager._get_peer_certificate = AsyncMock(return_value=bob_keypair["certificate"])

        # Create Charlie's manager (intermediate hop)
        charlie_manager = GossipManager(Mock(), charlie_keypair["node_id"])

        # Save Charlie's private key
        key_file = tmp_path / "node.key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(
            charlie_keypair["private_key"].private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
        )

        import os
        original_env = os.environ.get("DPC_DIR")
        os.environ["DPC_DIR"] = str(tmp_path)

        try:
            # Alice encrypts message for Bob
            payload = {"command": "SEND_TEXT", "text": "Secret message for Bob"}
            encrypted_b64 = await alice_manager._encrypt_payload(payload, bob_keypair["node_id"])

            # Charlie (intermediate hop) tries to decrypt
            # Should fail because message encrypted with Bob's public key, not Charlie's
            with pytest.raises(Exception):
                await charlie_manager._decrypt_payload(encrypted_b64)

        finally:
            if original_env is not None:
                os.environ["DPC_DIR"] = original_env
            elif "DPC_DIR" in os.environ:
                del os.environ["DPC_DIR"]

    @pytest.mark.asyncio
    async def test_send_gossip_encrypts_payload(self, bob_keypair):
        """Test that send_gossip encrypts the payload before storing."""
        # Create mock manager
        p2p_manager = Mock()
        p2p_manager.get_connected_peers = Mock(return_value=[])

        manager = GossipManager(p2p_manager, "dpc-node-alice123")
        manager._get_peer_certificate = AsyncMock(return_value=bob_keypair["certificate"])

        # Mock _forward_message to prevent actual forwarding
        manager._forward_message = AsyncMock()

        # Send gossip message
        payload = {"command": "HELLO", "data": "test"}
        msg_id = await manager.send_gossip(bob_keypair["node_id"], payload)

        # Check stored message
        assert msg_id in manager.messages
        stored_msg = manager.messages[msg_id]

        # Payload should be encrypted (contains "encrypted" key with base64 string)
        assert "encrypted" in stored_msg.payload
        assert isinstance(stored_msg.payload["encrypted"], str)

        # Should not contain original plaintext
        assert "command" not in stored_msg.payload
        assert "data" not in stored_msg.payload


class TestEndToEndSecurity:
    """Test end-to-end security properties of gossip encryption."""

    @pytest.mark.asyncio
    async def test_full_encryption_flow(self, alice_keypair, bob_keypair, tmp_path):
        """Test complete flow: Alice encrypts → Bob decrypts."""
        # Setup Alice's manager
        p2p_manager = Mock()
        alice_manager = GossipManager(p2p_manager, alice_keypair["node_id"])
        alice_manager._get_peer_certificate = AsyncMock(return_value=bob_keypair["certificate"])
        alice_manager._forward_message = AsyncMock()

        # Setup Bob's manager with private key
        bob_manager = GossipManager(Mock(), bob_keypair["node_id"])
        key_file = tmp_path / "node.key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(
            bob_keypair["private_key"].private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
        )

        import os
        original_env = os.environ.get("DPC_DIR")
        os.environ["DPC_DIR"] = str(tmp_path)

        try:
            # Alice sends encrypted message to Bob
            original_payload = {"command": "SEND_TEXT", "text": "Hello Bob!"}
            msg_id = await alice_manager.send_gossip(bob_keypair["node_id"], original_payload)

            # Get encrypted message
            encrypted_msg = alice_manager.messages[msg_id]
            encrypted_blob = encrypted_msg.payload["encrypted"]

            # Bob decrypts
            decrypted = await bob_manager._decrypt_payload(encrypted_blob)

            # Should match original
            assert decrypted == original_payload

        finally:
            if original_env is not None:
                os.environ["DPC_DIR"] = original_env
            elif "DPC_DIR" in os.environ:
                del os.environ["DPC_DIR"]

    @pytest.mark.asyncio
    async def test_metadata_visible_content_encrypted(self, alice_keypair, bob_keypair):
        """Test that metadata is visible but content is encrypted."""
        # Setup manager
        p2p_manager = Mock()
        manager = GossipManager(p2p_manager, alice_keypair["node_id"])
        manager._get_peer_certificate = AsyncMock(return_value=bob_keypair["certificate"])
        manager._forward_message = AsyncMock()

        # Send message
        payload = {"command": "SEND_TEXT", "text": "Secret content"}
        msg_id = await manager.send_gossip(bob_keypair["node_id"], payload, priority="high")

        # Get message
        msg = manager.messages[msg_id]

        # Metadata should be visible
        assert msg.source == alice_keypair["node_id"]
        assert msg.destination == bob_keypair["node_id"]
        assert msg.priority == "high"
        assert msg.max_hops == 5
        assert msg.ttl == 86400

        # Content should be encrypted
        assert "encrypted" in msg.payload
        encrypted_blob = msg.payload["encrypted"]

        # Encrypted blob should not contain plaintext
        assert "SEND_TEXT" not in encrypted_blob
        assert "Secret content" not in encrypted_blob
