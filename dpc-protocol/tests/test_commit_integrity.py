"""
Unit tests for Cryptographic Commit Integrity System
"""

import pytest
import hashlib
from pathlib import Path
from datetime import datetime
from cryptography.hazmat.primitives.asymmetric import rsa

from dpc_protocol.commit_integrity import (
    compute_commit_hash,
    verify_commit_hash,
    CommitSigner,
    verify_commit_chain,
    ChainIntegrityError,
    compute_content_hash,
    extract_commit_id_from_filename,
    extract_hash_from_commit_id
)
from dpc_protocol.knowledge_commit import (
    KnowledgeCommit,
    KnowledgeEntry,
    KnowledgeSource
)


class TestCommitHashing:
    """Test hash computation and verification"""

    def test_hash_computation_deterministic(self):
        """Same content should produce same hash."""

        commit1 = KnowledgeCommit(
            topic="test_topic",
            summary="Test commit",
            participants=["alice", "bob"],
            approved_by=["alice", "bob"]
        )

        commit2 = KnowledgeCommit(
            topic="test_topic",
            summary="Test commit",
            participants=["alice", "bob"],
            approved_by=["alice", "bob"]
        )

        # Set same timestamp
        timestamp = "2025-01-01T00:00:00.000000"
        commit1.timestamp = timestamp
        commit2.timestamp = timestamp

        hash1 = compute_commit_hash(commit1)
        hash2 = compute_commit_hash(commit2)

        assert hash1 == hash2, "Same content should produce same hash"
        assert len(hash1) == 64, "SHA256 hash should be 64 characters"

    def test_hash_changes_on_content_change(self):
        """Different content should produce different hash."""

        commit1 = KnowledgeCommit(
            topic="test_topic",
            summary="Test commit"
        )

        commit2 = KnowledgeCommit(
            topic="test_topic",
            summary="Different summary"
        )

        hash1 = compute_commit_hash(commit1)
        hash2 = compute_commit_hash(commit2)

        assert hash1 != hash2, "Different content should produce different hash"

    def test_hash_includes_entries(self):
        """Hash should change when entries change."""

        entry1 = KnowledgeEntry(
            content="Test entry 1",
            tags=["test"]
        )

        entry2 = KnowledgeEntry(
            content="Test entry 2",
            tags=["test"]
        )

        commit1 = KnowledgeCommit(
            topic="test",
            summary="Test",
            entries=[entry1]
        )

        commit2 = KnowledgeCommit(
            topic="test",
            summary="Test",
            entries=[entry2]
        )

        hash1 = compute_commit_hash(commit1)
        hash2 = compute_commit_hash(commit2)

        assert hash1 != hash2, "Hash should change when entries change"

    def test_hash_excludes_conversation_id(self):
        """Hash should be same even with different conversation_id."""

        commit1 = KnowledgeCommit(
            topic="test",
            summary="Test",
            conversation_id="conv-123"
        )

        commit2 = KnowledgeCommit(
            topic="test",
            summary="Test",
            conversation_id="conv-456"
        )

        # Set same timestamp
        timestamp = "2025-01-01T00:00:00.000000"
        commit1.timestamp = timestamp
        commit2.timestamp = timestamp

        hash1 = compute_commit_hash(commit1)
        hash2 = compute_commit_hash(commit2)

        assert hash1 == hash2, "Hash should not include conversation_id"

    def test_compute_hash_method(self):
        """Test KnowledgeCommit.compute_hash() method."""

        commit = KnowledgeCommit(
            topic="test",
            summary="Test commit"
        )

        # Initially no hash
        assert commit.commit_hash is None

        # Compute hash
        full_hash = commit.compute_hash()

        # Check hash is set
        assert commit.commit_hash == full_hash
        assert len(commit.commit_hash) == 64

        # Check commit_id is set to first 16 chars
        assert commit.commit_id == f"commit-{full_hash[:16]}"
        assert len(commit.commit_id) == 23  # "commit-" + 16 chars

    def test_verify_commit_hash(self):
        """Test hash verification."""

        commit = KnowledgeCommit(
            topic="test",
            summary="Test commit"
        )

        # Compute hash
        commit.compute_hash()

        # Should verify correctly
        assert verify_commit_hash(commit) is True

        # Tamper with summary
        commit.summary = "Tampered summary"

        # Should fail verification
        assert verify_commit_hash(commit) is False


class TestCommitSigning:
    """Test RSA signature creation and verification"""

    def test_signature_creation(self):
        """Test signing a commit."""

        # Generate test key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )

        commit = KnowledgeCommit(topic="test", summary="Test")
        commit.compute_hash()

        # Sign
        commit.sign("test-node-abc123", private_key)

        # Check signature exists
        assert "test-node-abc123" in commit.signatures
        assert len(commit.signatures["test-node-abc123"]) > 0

        # Signature should be base64
        import base64
        try:
            base64.b64decode(commit.signatures["test-node-abc123"])
        except Exception:
            pytest.fail("Signature should be valid base64")

    def test_signature_requires_hash(self):
        """Signing should fail if hash not computed."""

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )

        commit = KnowledgeCommit(topic="test", summary="Test")

        # Should raise error
        with pytest.raises(ValueError, match="Must compute hash before signing"):
            commit.sign("test-node", private_key)

    def test_multiple_signatures(self):
        """Test multiple participants signing."""

        # Generate keys for two nodes
        key1 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        commit = KnowledgeCommit(
            topic="test",
            summary="Test",
            participants=["node1", "node2"],
            approved_by=["node1", "node2"]
        )

        commit.compute_hash()

        # Both sign
        commit.sign("node1", key1)
        commit.sign("node2", key2)

        # Check both signatures exist
        assert "node1" in commit.signatures
        assert "node2" in commit.signatures
        assert len(commit.signatures) == 2


class TestCommitChain:
    """Test commit chain validation"""

    def test_valid_chain(self):
        """Verify valid commit chain."""

        # Create chain
        commit1 = KnowledgeCommit(topic="test", summary="Initial")
        commit1.parent_commit_id = None
        commit1.compute_hash()

        commit2 = KnowledgeCommit(topic="test", summary="Second")
        commit2.parent_commit_id = commit1.commit_id
        commit2.compute_hash()

        commit3 = KnowledgeCommit(topic="test", summary="Third")
        commit3.parent_commit_id = commit2.commit_id
        commit3.compute_hash()

        # Should not raise
        verify_commit_chain([commit1, commit2, commit3])

    def test_broken_chain_detected(self):
        """Detect broken commit chain."""

        commit1 = KnowledgeCommit(topic="test", summary="Initial")
        commit1.parent_commit_id = None
        commit1.compute_hash()

        commit2 = KnowledgeCommit(topic="test", summary="Second")
        commit2.parent_commit_id = "commit-invalid"  # Wrong parent
        commit2.compute_hash()

        with pytest.raises(ChainIntegrityError, match="non-existent parent"):
            verify_commit_chain([commit1, commit2])

    def test_missing_parent_reference(self):
        """Detect missing parent reference."""

        commit1 = KnowledgeCommit(topic="test", summary="Initial")
        commit1.parent_commit_id = None
        commit1.compute_hash()

        commit2 = KnowledgeCommit(topic="test", summary="Second")
        commit2.parent_commit_id = None  # Missing parent
        commit2.compute_hash()

        with pytest.raises(ChainIntegrityError, match="missing parent reference"):
            verify_commit_chain([commit1, commit2])

    def test_parent_mismatch(self):
        """Detect parent mismatch in chain."""

        commit1 = KnowledgeCommit(topic="test", summary="Initial")
        commit1.parent_commit_id = None
        commit1.compute_hash()

        commit2 = KnowledgeCommit(topic="test", summary="Second")
        commit2.parent_commit_id = commit1.commit_id
        commit2.compute_hash()

        commit3 = KnowledgeCommit(topic="test", summary="Third")
        commit3.parent_commit_id = commit1.commit_id  # Should reference commit2
        commit3.compute_hash()

        with pytest.raises(ChainIntegrityError, match="parent mismatch"):
            verify_commit_chain([commit1, commit2, commit3])


class TestContentHash:
    """Test markdown content hashing"""

    def test_content_hash_computation(self):
        """Test computing content hash."""

        content = "# Test Topic\n\nSome content here."

        hash1 = compute_content_hash(content)
        hash2 = compute_content_hash(content)

        assert hash1 == hash2, "Same content should produce same hash"
        assert len(hash1) == 16, "Content hash should be 16 characters"

    def test_content_hash_changes(self):
        """Test that content hash changes with content."""

        content1 = "# Test Topic\n\nOriginal content."
        content2 = "# Test Topic\n\nModified content."

        hash1 = compute_content_hash(content1)
        hash2 = compute_content_hash(content2)

        assert hash1 != hash2, "Different content should produce different hash"


class TestFilenameExtraction:
    """Test filename and commit ID extraction"""

    def test_extract_commit_id_from_filename(self):
        """Test extracting commit ID from filename."""

        filename = "astronomy_commit-a3f7b2c91d4e5f6a.md"
        commit_id = extract_commit_id_from_filename(filename)

        assert commit_id == "commit-a3f7b2c91d4e5f6a"

    def test_extract_commit_id_no_match(self):
        """Test extracting from invalid filename."""

        filename = "invalid_filename.md"
        commit_id = extract_commit_id_from_filename(filename)

        assert commit_id is None

    def test_extract_hash_from_commit_id(self):
        """Test extracting hash from commit ID."""

        commit_id = "commit-a3f7b2c91d4e5f6a"
        hash_part = extract_hash_from_commit_id(commit_id)

        assert hash_part == "a3f7b2c91d4e5f6a"

    def test_extract_hash_no_prefix(self):
        """Test extracting hash without prefix."""

        hash_value = "a3f7b2c91d4e5f6a"
        result = extract_hash_from_commit_id(hash_value)

        assert result == hash_value


class TestKnowledgeCommitIntegration:
    """Integration tests for KnowledgeCommit with integrity features"""

    def test_full_commit_workflow(self):
        """Test complete commit workflow: create, hash, sign, verify."""

        # Generate key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )

        # Create commit with entries
        entry = KnowledgeEntry(
            content="Test knowledge entry",
            tags=["test", "knowledge"],
            confidence=0.95,
            source=KnowledgeSource(
                type="ai_summary",
                participants=["alice"],
                cultural_perspectives_considered=["Western"],
                confidence_score=0.95
            )
        )

        commit = KnowledgeCommit(
            topic="test_topic",
            summary="Test commit for integration",
            description="Full workflow test",
            entries=[entry],
            participants=["alice"],
            approved_by=["alice"],
            consensus_type="unanimous",
            confidence_score=0.95
        )

        # Compute hash
        full_hash = commit.compute_hash()

        assert commit.commit_hash == full_hash
        assert commit.commit_id.startswith("commit-")
        assert len(commit.commit_id) == 23

        # Sign
        commit.sign("alice", private_key)

        assert "alice" in commit.signatures
        assert len(commit.signatures["alice"]) > 0

        # Verify hash
        assert commit.verify_hash() is True

        # Note: Signature verification requires certificate files,
        # which are tested separately in the actual deployment environment

    def test_tampering_detection(self):
        """Test that tampering is detected."""

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )

        commit = KnowledgeCommit(
            topic="test",
            summary="Original summary"
        )

        commit.compute_hash()
        commit.sign("test-node", private_key)

        # Tamper with content
        commit.summary = "Tampered summary"

        # Hash verification should fail
        assert commit.verify_hash() is False

    def test_commit_serialization(self):
        """Test that commits can be serialized with integrity fields."""

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )

        commit = KnowledgeCommit(
            topic="test",
            summary="Test commit"
        )

        commit.compute_hash()
        commit.sign("test-node", private_key)

        # Serialize
        data = commit.to_dict()

        # Check fields are present
        assert 'commit_hash' in data
        assert 'signatures' in data
        assert data['commit_hash'] == commit.commit_hash
        assert 'test-node' in data['signatures']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
