# Cryptographic Commit Integrity System - Implementation Plan

**Status:** Ready for Implementation
**Version:** 1.0
**Date:** 2025-11-26
**Architecture:** Git/Blockchain-Inspired Content-Addressable Storage

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Hash-Based Commit IDs](#hash-based-commit-ids)
4. [Versioned Markdown Storage](#versioned-markdown-storage)
5. [Verification System](#verification-system)
6. [Multi-Signature Support](#multi-signature-support)
7. [Chain of Trust](#chain-of-trust)
8. [Implementation Details](#implementation-details)
9. [File Structure](#file-structure)
10. [Testing Plan](#testing-plan)
11. [Security Considerations](#security-considerations)

---

## Overview

### Problem Statement

Current commit system uses random UUIDs (`commit-{uuid4}`), which:
- **Cannot detect tampering** - Manual markdown edits go unnoticed
- **No proof of agreement** - Collaborative commits lack cryptographic verification
- **No chain of trust** - Commits are independent, no version history integrity

### Solution

Implement **content-addressable storage** with cryptographic verification:
- **Hash-based commit IDs** - Commit ID = SHA256 hash of content
- **Multi-signature support** - Collaborative commits signed by all participants
- **Chain of trust** - Each commit references parent hash
- **Tamper detection** - Content hash verifies markdown integrity

### Use Cases

1. **Personal Knowledge Commits:**
   - User converses with AI
   - Manually edits extracted knowledge in UI
   - Commits changes with hash verification

2. **Peer-to-Peer Agreements:**
   - Alice and Bob discuss Task A
   - Both commit to deadline Date B
   - Cryptographic proof stored on both devices

3. **Multi-Peer Group Collaboration** (Future):
   - Topic-based chat with multiple peers
   - Consensus on project tasks
   - All participants sign and store commits

---

## Architecture

### High-Level Design

```
┌─────────────────────────────────────────────────────────┐
│                   Knowledge Commit                      │
│                                                         │
│  1. Create commit with metadata + entries               │
│  2. Compute SHA256 hash of canonical JSON               │
│  3. Use hash as commit_id (content-addressable)        │
│  4. Each participant signs hash with RSA key            │
│  5. Store as versioned markdown file                    │
│  6. Verify hash + signatures on load                    │
└─────────────────────────────────────────────────────────┘
```

### Data Flow

```
Commit Creation:
  Consensus Vote → Approved → Compute Hash → Sign → Store Markdown

Commit Verification:
  Load Markdown → Parse Frontmatter → Verify Hash → Verify Signatures → Use
```

---

## Hash-Based Commit IDs

### Current Implementation (Random UUID)

```python
# OLD: dpc-protocol/dpc_protocol/knowledge_commit.py:129
commit_id = f"commit-{uuid.uuid4().hex[:8]}"

# Example: commit-a3f7b2c9
```

**Problem:** Different commits can accidentally have same UUID (collision), no content verification.

### New Implementation (Content Hash)

```python
# NEW: Hash-based
commit_id = f"commit-{compute_commit_hash(commit)[:16]}"

# Example: commit-a3f7b2c91d4e5f6a
#                   └─ First 16 chars of SHA256 hash
```

**Benefit:** Same content = same hash, different content = different hash.

### Hash Computation Algorithm

```python
def compute_commit_hash(commit: KnowledgeCommit) -> str:
    """
    Compute deterministic SHA256 hash of commit content.

    Returns:
        64-character hex string (SHA256 hash)

    Hash Input (canonical JSON):
        - parent_commit_id (chain of trust)
        - timestamp (ISO format)
        - topic
        - summary
        - entries (sorted by content)
        - participants (sorted)
        - approved_by (sorted)
        - rejected_by (sorted)
        - cultural_perspectives_considered (sorted)
        - confidence_score (rounded to 2 decimals)

    Excluded from hash:
        - conversation_id (varies by session)
        - entry source timestamps (varies by node)
        - commit_id itself (circular dependency)
    """

    # Build canonical hash input
    hash_input = {
        "parent": commit.parent_commit_id or "",
        "timestamp": commit.timestamp,
        "topic": commit.topic,
        "summary": commit.summary,

        # Entries: sorted by content, exclude volatile fields
        "entries": sorted([
            {
                "content": entry.content,
                "tags": sorted(entry.tags),
                "confidence": round(entry.confidence, 2),
                "cultural_specific": entry.cultural_specific,
                "alternative_viewpoints": sorted(entry.alternative_viewpoints)
            }
            for entry in commit.entries
        ], key=lambda x: x["content"]),

        # Consensus metadata: sorted for determinism
        "participants": sorted(commit.participants),
        "approved_by": sorted(commit.approved_by),
        "rejected_by": sorted(commit.rejected_by),
        "cultural_perspectives": sorted(commit.cultural_perspectives_considered),
        "confidence": round(commit.confidence_score, 2)
    }

    # Canonical JSON: sorted keys, no whitespace
    canonical_json = json.dumps(
        hash_input,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True
    )

    # SHA256 hash
    hash_bytes = hashlib.sha256(canonical_json.encode('utf-8'))
    return hash_bytes.hexdigest()


# Example usage:
commit = KnowledgeCommit(
    topic="astronomy",
    summary="Andromeda Galaxy distance",
    entries=[...],
    participants=["alice", "bob"],
    approved_by=["alice", "bob"]
)

commit_hash = compute_commit_hash(commit)
# Result: "a3f7b2c91d4e5f6a8b9c0d1e2f3g4h5i6j7k8l9m0n1o2p3q4r5s6t7u8v9w0x1y2z"

commit.commit_id = f"commit-{commit_hash[:16]}"
# Result: "commit-a3f7b2c91d4e5f6a"
```

### Properties

- **Deterministic:** Same input → same hash (all peers agree)
- **Tamper-evident:** Change ANY field → different hash
- **Collision-resistant:** SHA256 has 2^256 possible values
- **Verifiable:** Anyone can recompute and verify

---

## Versioned Markdown Storage

### File Naming Convention

```
~/.dpc/knowledge/
├── astronomy_commit-a3f7b2c91d4e5f6a.md
├── astronomy_commit-b8c9d0e1f2a3b4c5.md  (newer version)
└── game_design_commit-c1d2e3f4a5b6c7d8.md
```

**Format:** `{topic_name}_{commit_id}.md`

### Markdown Structure with Frontmatter

```markdown
---
# Commit Identification
topic: astronomy
commit_id: commit-a3f7b2c91d4e5f6a
commit_hash: a3f7b2c91d4e5f6a8b9c0d1e2f3g4h5i6j7k8l9m0n1o2p3q4r5s6t7u8v9w0x1y2z
parent_commit: commit-b8c9d0e1f2a3b4c5

# Integrity Verification
content_hash: f9e8d7c6b5a49382

# Metadata
timestamp: 2025-11-18T10:30:00.000000Z
version: 2
author: dpc-node-e07fb59e46f34940

# Consensus Tracking
participants:
  - dpc-node-alice-123
  - dpc-node-bob-456
approved_by:
  - dpc-node-alice-123
  - dpc-node-bob-456
rejected_by: []
consensus: unanimous
confidence_score: 0.92

# Cryptographic Signatures
signatures:
  dpc-node-alice-123: "MEUCIQDXvK...=="
  dpc-node-bob-456: "MEYCIQCqwL...=="

# Cultural Context
cultural_perspectives:
  - Western individualistic
  - Eastern collective
---

# Astronomy

**Summary:** The Andromeda Galaxy is approximately 2.5 million light-years away from Earth.

## Key Concepts

### Andromeda Galaxy Distance Measurement

**Tags:** Astronomy, Distance
**Confidence:** 0.9
**Source:** NASA (ai_summary from conversation local_ai)

The Andromeda Galaxy's distance can be measured using various methods, including redshift measurements and the expansion rate of the universe.

**Alternative Viewpoints:**
- Alternative method 1: Redshift measurements
- Alternative method 2: Expansion rate of the universe

---

### Cultural Perspectives on Andromeda

**Tags:** Astronomy, Philosophy
**Confidence:** 0.8
**Cultural Context:** Indigenous/holistic

Some ancient cultures considered the Andromeda Galaxy a companion to our own Milky Way galaxy.
```

### Two-Level Verification

1. **Commit Hash** (`commit_hash` field):
   - Verifies entire commit metadata + entries
   - Detects changes to consensus, participants, timestamps

2. **Content Hash** (`content_hash` field):
   - Verifies ONLY markdown content below frontmatter
   - Detects manual edits to knowledge text

**Why Both?**
- Commit hash ensures collaborative agreement integrity
- Content hash detects local tampering of markdown files

---

## Verification System

### On Startup (Integrity Check)

```python
async def verify_all_knowledge_commits():
    """
    Verify integrity of all knowledge commits on startup.

    Checks:
        1. Content hash matches actual markdown content
        2. Commit ID in filename matches frontmatter
        3. Recomputed commit hash matches stored commit_hash
        4. All signatures are valid
        5. Parent commit exists (chain integrity)

    Returns:
        List of integrity warnings (empty if all valid)
    """

    knowledge_dir = Path.home() / ".dpc" / "knowledge"
    warnings = []

    for markdown_file in knowledge_dir.glob("*_commit-*.md"):
        # Parse markdown
        frontmatter, content = parse_markdown_with_frontmatter(markdown_file)

        # CHECK 1: Content hash
        actual_content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
        expected_content_hash = frontmatter['content_hash']

        if actual_content_hash != expected_content_hash:
            warnings.append({
                'file': markdown_file.name,
                'type': 'content_tampered',
                'severity': 'warning',
                'message': f'Markdown content manually edited (hash mismatch)',
                'expected': expected_content_hash,
                'actual': actual_content_hash
            })

        # CHECK 2: Filename vs frontmatter commit_id
        commit_id_from_filename = extract_commit_id_from_filename(markdown_file.name)
        commit_id_from_frontmatter = frontmatter['commit_id']

        if commit_id_from_filename != commit_id_from_frontmatter:
            warnings.append({
                'file': markdown_file.name,
                'type': 'filename_mismatch',
                'severity': 'error',
                'message': 'Filename commit_id doesn\'t match frontmatter'
            })

        # CHECK 3: Recompute commit hash
        commit_data = reconstruct_commit_from_markdown(frontmatter, content)
        recomputed_hash = compute_commit_hash(commit_data)
        expected_hash = frontmatter['commit_hash']

        if recomputed_hash != expected_hash:
            warnings.append({
                'file': markdown_file.name,
                'type': 'commit_hash_invalid',
                'severity': 'error',
                'message': 'Commit metadata altered (hash mismatch)',
                'expected': expected_hash,
                'actual': recomputed_hash
            })

        # CHECK 4: Verify signatures
        signatures = frontmatter.get('signatures', {})
        for node_id, signature in signatures.items():
            if not verify_rsa_signature(node_id, expected_hash, signature):
                warnings.append({
                    'file': markdown_file.name,
                    'type': 'invalid_signature',
                    'severity': 'error',
                    'message': f'Invalid signature from {node_id}',
                    'signer': node_id
                })

        # CHECK 5: Parent commit exists
        parent_commit_id = frontmatter.get('parent_commit')
        if parent_commit_id and parent_commit_id != "":
            parent_file_pattern = f"*_{parent_commit_id}.md"
            if not list(knowledge_dir.glob(parent_file_pattern)):
                warnings.append({
                    'file': markdown_file.name,
                    'type': 'missing_parent',
                    'severity': 'warning',
                    'message': f'Parent commit {parent_commit_id} not found'
                })

    # Log results
    if warnings:
        logger.warning(f"⚠️ Integrity check: {len(warnings)} issues found")
        for w in warnings:
            logger.warning(f"  [{w['severity'].upper()}] {w['file']}: {w['message']}")
    else:
        logger.info("✓ All knowledge commits verified")

    return warnings
```

### On Peer Commit Receipt

```python
async def receive_collaborative_commit(commit_data: dict, sender_node_id: str):
    """
    Receive and verify collaborative commit from peer.

    Verification Steps:
        1. Recompute commit hash from content
        2. Verify hash matches commit_id
        3. Verify sender is in approved_by list
        4. Verify all signatures (including sender's)
        5. Check parent commit exists locally

    Raises:
        IntegrityError: If hash verification fails
        SignatureError: If any signature is invalid
        AuthorizationError: If sender not authorized

    Returns:
        Verified KnowledgeCommit object
    """

    # 1. Reconstruct commit
    commit = KnowledgeCommit.from_dict(commit_data)

    # 2. Verify commit hash
    recomputed_hash = compute_commit_hash(commit)
    claimed_hash = extract_hash_from_commit_id(commit.commit_id)

    if recomputed_hash[:16] != claimed_hash:
        raise IntegrityError(
            f"Commit hash mismatch from {sender_node_id}. "
            f"Expected: {claimed_hash}, Got: {recomputed_hash[:16]}. "
            f"Possible tampering detected."
        )

    # 3. Verify sender authorization
    if sender_node_id not in commit.approved_by:
        raise AuthorizationError(
            f"Sender {sender_node_id} not in approved_by list. "
            f"Approved participants: {', '.join(commit.approved_by)}"
        )

    # 4. Verify all signatures
    for node_id in commit.approved_by:
        if node_id not in commit.signatures:
            raise SignatureError(
                f"Missing signature from {node_id}"
            )

        signature = commit.signatures[node_id]
        public_key = load_peer_public_key(node_id)

        if not verify_rsa_signature_with_key(public_key, commit.commit_hash, signature):
            raise SignatureError(
                f"Invalid signature from {node_id}. "
                f"Signature verification failed."
            )

    # 5. Check parent exists
    if commit.parent_commit_id:
        if not commit_exists_locally(commit.parent_commit_id):
            logger.warning(
                f"Parent commit {commit.parent_commit_id} not found locally. "
                f"This commit may be out of order."
            )

    logger.info(
        f"✓ Verified commit {commit.commit_id} from {sender_node_id} "
        f"({len(commit.signatures)} signatures)"
    )

    return commit
```

---

## Multi-Signature Support

### RSA Signature Algorithm

Uses existing node RSA keys (2048-bit) from `~/.dpc/node.key`.

```python
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes
import base64

class CommitSigner:
    """Sign and verify commits with RSA keys."""

    def __init__(self, node_id: str, private_key: rsa.RSAPrivateKey):
        self.node_id = node_id
        self.private_key = private_key

    def sign_commit(self, commit_hash: str) -> str:
        """
        Sign commit hash with node's private key.

        Args:
            commit_hash: SHA256 hash as hex string

        Returns:
            Base64-encoded RSA signature
        """

        # Sign the hash (as bytes)
        signature_bytes = self.private_key.sign(
            commit_hash.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )

        # Encode as base64 for storage
        signature_b64 = base64.b64encode(signature_bytes).decode('ascii')

        return signature_b64

    @staticmethod
    def verify_signature(
        node_id: str,
        commit_hash: str,
        signature_b64: str
    ) -> bool:
        """
        Verify signature from a peer.

        Args:
            node_id: Peer's node ID
            commit_hash: SHA256 hash as hex string
            signature_b64: Base64-encoded signature

        Returns:
            True if signature is valid
        """

        # Load peer's public key from certificate
        cert_path = Path.home() / ".dpc" / "peers" / f"{node_id}.crt"

        if not cert_path.exists():
            logger.error(f"Certificate not found for {node_id}")
            return False

        with open(cert_path, 'rb') as f:
            from cryptography import x509
            cert = x509.load_pem_x509_certificate(f.read())
            public_key = cert.public_key()

        # Decode signature
        signature_bytes = base64.b64decode(signature_b64)

        # Verify
        try:
            public_key.verify(
                signature_bytes,
                commit_hash.encode('utf-8'),
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH
                ),
                hashes.SHA256()
            )
            return True
        except Exception as e:
            logger.warning(f"Signature verification failed for {node_id}: {e}")
            return False
```

### Collaborative Commit Workflow

```
┌──────────────────────────────────────────────────────────┐
│         Collaborative Knowledge Commit Workflow          │
└──────────────────────────────────────────────────────────┘

1. PROPOSAL PHASE
   ┌─ Alice & Bob chat about Task A ─┐
   │ AI: "I've identified 3 key points" │
   │ Alice: "Agreed, let's commit this" │
   └────────────────────────────────────┘

   → AI creates KnowledgeCommitProposal
   → Broadcasts to participants: [Alice, Bob]

2. VOTING PHASE
   ┌─ Alice's Device ─┐     ┌─ Bob's Device ─┐
   │ Vote: APPROVE    │     │ Vote: APPROVE  │
   │ Comment: "Good"  │     │ Comment: "Yes" │
   └──────────────────┘     └────────────────┘

   → Consensus Manager tallies: 2/2 approved

3. COMMIT CREATION
   ┌─ Consensus Manager ─┐
   │ approved_by = [Alice, Bob]            │
   │ consensus_type = "unanimous"          │
   │ entries = [extracted knowledge]       │
   └───────────────────────────────────────┘

   → Compute commit hash (includes approved_by)
   → commit_id = "commit-a3f7b2c91d4e5f6a"
   → commit_hash = "a3f7b2c9...w0x1y2z"

4. SIGNING PHASE
   ┌─ Alice signs ─┐              ┌─ Bob signs ─┐
   │ Load ~/.dpc/node.key        │ Load ~/.dpc/node.key        │
   │ Sign commit_hash            │ Sign commit_hash            │
   │ signature_alice = "MEU..." │ │ signature_bob = "MEY..."   │
   └────────────────────────────┘ └─────────────────────────────┘

   → Both signatures added to commit.signatures{}

5. DISTRIBUTION
   ┌─ Alice broadcasts SignedCommit to Bob ─┐
   │ commit_id: commit-a3f7b2c91d4e5f6a     │
   │ signatures: {alice: "MEU...", bob: "MEY..."} │
   └──────────────────────────────────────────────┘

   ┌─ Bob receives and verifies ─┐
   │ ✓ Hash matches content       │
   │ ✓ Alice's signature valid    │
   │ ✓ Bob's signature valid      │
   │ ✓ Both in approved_by list   │
   └──────────────────────────────┘

6. STORAGE (Both Devices)
   ~/.dpc/knowledge/
   └── task_a_commit-a3f7b2c91d4e5f6a.md
       (with both signatures in frontmatter)

RESULT:
✓ Cryptographic proof: "Alice and Bob both agreed to Task A on 2025-11-18"
✓ Immutable: Changing content breaks hash
✓ Verifiable: Anyone can verify signatures
✓ Distributed: Both peers have identical copy
```

---

## Chain of Trust

### Git-Style Commit Chain

```
commit-a3f7b2c9 (Initial commit, parent: null)
    ↓
commit-b8c9d0e1 (parent: commit-a3f7b2c9)
    ↓
commit-c1d2e3f4 (parent: commit-b8c9d0e1)
    ↓
commit-d4e5f6a7 (parent: commit-c1d2e3f4)
```

### Properties

- **Linear history:** Each commit references exactly one parent
- **Tamper-evident:** Changing any commit breaks all descendants
- **Verifiable:** Can reconstruct entire history from commits

### Verification

```python
def verify_commit_chain(commits: List[KnowledgeCommit]):
    """
    Verify integrity of commit chain.

    Checks:
        1. Each commit's parent_commit_id references previous commit
        2. Parent hashes match stored commit_hash
        3. No circular references
        4. No missing parents

    Raises:
        ChainIntegrityError: If chain is broken
    """

    # Build commit index
    commit_index = {c.commit_id: c for c in commits}

    for i, commit in enumerate(commits):
        # Skip first commit (no parent)
        if i == 0:
            if commit.parent_commit_id:
                raise ChainIntegrityError(
                    f"First commit {commit.commit_id} should have no parent"
                )
            continue

        # Verify parent exists
        parent_id = commit.parent_commit_id

        if not parent_id:
            raise ChainIntegrityError(
                f"Commit {commit.commit_id} missing parent reference"
            )

        if parent_id not in commit_index:
            raise ChainIntegrityError(
                f"Commit {commit.commit_id} references non-existent parent {parent_id}"
            )

        # Verify parent is previous commit in chain
        expected_parent = commits[i - 1].commit_id
        if parent_id != expected_parent:
            raise ChainIntegrityError(
                f"Commit {commit.commit_id} parent mismatch. "
                f"Expected: {expected_parent}, Got: {parent_id}"
            )

        # Verify parent hash matches
        parent_commit = commit_index[parent_id]
        if parent_commit.commit_hash:
            expected_hash = extract_hash_from_commit_id(parent_id)
            if parent_commit.commit_hash[:16] != expected_hash:
                raise ChainIntegrityError(
                    f"Parent commit {parent_id} hash mismatch"
                )

    logger.info(f"✓ Commit chain verified ({len(commits)} commits)")
```

---

## Implementation Details

### File Structure

```
dpc-messenger/
├── dpc-protocol/
│   └── dpc_protocol/
│       ├── commit_integrity.py        # NEW: Hash & signature functions
│       ├── knowledge_commit.py        # UPDATE: Add commit_hash, signatures
│       ├── crypto.py                  # EXISTING: RSA key functions
│       └── markdown_manager.py        # UPDATE: Frontmatter with hashes
│
├── dpc-client/core/
│   └── dpc_client_core/
│       ├── consensus_manager.py       # UPDATE: Hash-based commit creation
│       ├── service.py                 # UPDATE: Startup integrity check
│       └── local_api.py              # UPDATE: Expose verification API
│
├── dpc-client/ui/
│   └── src/lib/components/
│       └── ContextViewer.svelte       # UPDATE: Integrity status UI
│
└── docs/
    └── COMMIT_INTEGRITY_IMPLEMENTATION.md  # THIS FILE
```

### New File: `dpc-protocol/dpc_protocol/commit_integrity.py`

```python
"""
Cryptographic commit integrity verification system.

Provides:
    - Hash-based commit ID generation
    - Multi-signature support (RSA)
    - Markdown integrity verification
    - Commit chain validation
"""

import hashlib
import json
import base64
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509

from .knowledge_commit import KnowledgeCommit
from .crypto import load_identity


def compute_commit_hash(commit: KnowledgeCommit) -> str:
    """
    Compute deterministic SHA256 hash of commit content.

    Returns:
        64-character hex string
    """
    # Implementation from earlier section
    pass


def verify_commit_hash(commit: KnowledgeCommit) -> bool:
    """
    Verify commit hash matches content.

    Returns:
        True if hash is valid
    """
    recomputed = compute_commit_hash(commit)
    claimed = commit.commit_hash

    return recomputed == claimed


class CommitSigner:
    """Sign and verify commits with RSA keys."""

    def __init__(self, node_id: str, private_key: rsa.RSAPrivateKey):
        self.node_id = node_id
        self.private_key = private_key

    def sign_commit(self, commit_hash: str) -> str:
        """Sign commit hash with private key."""
        # Implementation from earlier section
        pass

    @staticmethod
    def verify_signature(node_id: str, commit_hash: str, signature: str) -> bool:
        """Verify signature from peer's public key."""
        # Implementation from earlier section
        pass


def verify_markdown_integrity(markdown_path: Path) -> Dict:
    """
    Verify markdown file integrity.

    Returns:
        {
            'valid': bool,
            'warnings': List[str],
            'commit_id': str,
            'content_hash_valid': bool,
            'commit_hash_valid': bool,
            'signatures_valid': Dict[str, bool]
        }
    """
    # Implementation from verification section
    pass


def verify_commit_chain(commits: List[KnowledgeCommit]):
    """Verify commit chain integrity."""
    # Implementation from chain of trust section
    pass
```

### Update: `dpc-protocol/dpc_protocol/knowledge_commit.py`

```python
# Add imports
import hashlib
import json
from cryptography.hazmat.primitives.asymmetric import rsa

# Update KnowledgeCommit dataclass
@dataclass
class KnowledgeCommit:
    """Finalized knowledge commit (after consensus approval)"""

    # Existing fields...
    commit_id: str = field(default_factory=lambda: f"commit-{uuid.uuid4().hex[:8]}")
    parent_commit_id: Optional[str] = None
    summary: str = ""
    description: str = ""
    topic: str = ""
    entries: List[KnowledgeEntry] = field(default_factory=list)
    conversation_id: Optional[str] = None
    participants: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    consensus_type: Literal["unanimous", "majority", "disputed"] = "unanimous"
    approved_by: List[str] = field(default_factory=list)
    rejected_by: List[str] = field(default_factory=list)
    cultural_perspectives_considered: List[str] = field(default_factory=list)
    confidence_score: float = 1.0
    sources_cited: List[str] = field(default_factory=list)
    dissenting_opinion: Optional[str] = None

    # NEW: Cryptographic integrity fields
    commit_hash: Optional[str] = None  # Full SHA256 hash (64 chars)
    signatures: Dict[str, str] = field(default_factory=dict)  # node_id -> signature

    def compute_hash(self) -> str:
        """
        Compute hash-based commit ID.

        Sets:
            self.commit_hash = full SHA256 hash
            self.commit_id = "commit-{hash[:16]}"

        Returns:
            Full hash (64 chars)
        """
        from .commit_integrity import compute_commit_hash

        self.commit_hash = compute_commit_hash(self)
        self.commit_id = f"commit-{self.commit_hash[:16]}"

        return self.commit_hash

    def sign(self, node_id: str, private_key: rsa.RSAPrivateKey):
        """
        Sign this commit with node's private key.

        Args:
            node_id: Node identifier
            private_key: RSA private key
        """
        from .commit_integrity import CommitSigner

        if not self.commit_hash:
            raise ValueError("Must compute hash before signing")

        signer = CommitSigner(node_id, private_key)
        self.signatures[node_id] = signer.sign_commit(self.commit_hash)

    def verify_signatures(self) -> bool:
        """
        Verify all signatures in this commit.

        Returns:
            True if all signatures are valid
        """
        from .commit_integrity import CommitSigner

        for node_id, signature in self.signatures.items():
            if not CommitSigner.verify_signature(node_id, self.commit_hash, signature):
                return False

        return True
```

### Update: `dpc-client/core/dpc_client_core/consensus_manager.py`

```python
# In _apply_commit() method (around line 265)

async def _apply_commit(self, commit: KnowledgeCommit) -> None:
    """Apply approved commit to local PCM with hash verification."""

    try:
        # 1. Set parent commit (chain of trust)
        context = self.pcm_core.load_context()
        commit.parent_commit_id = context.last_commit_id

        # 2. Compute hash-based commit ID
        commit.compute_hash()  # Sets commit_hash and commit_id

        logger.info(f"Created commit {commit.commit_id} (hash: {commit.commit_hash[:16]}...)")

        # 3. Sign commit with our private key
        from dpc_protocol.crypto import load_identity
        from cryptography.hazmat.primitives import serialization

        node_id, key_path, cert_path = load_identity()

        with open(key_path, 'rb') as f:
            private_key = serialization.load_pem_private_key(
                f.read(),
                password=None
            )

        commit.sign(node_id, private_key)

        logger.info(f"Signed commit with {node_id}")

        # 4. Add or update topic (existing logic)
        topic_name = commit.topic

        if topic_name in context.knowledge:
            topic = context.knowledge[topic_name]
            topic.entries.extend(commit.entries)
            topic.version += 1
            topic.last_modified = datetime.utcnow().isoformat()
        else:
            from dpc_protocol.pcm_core import Topic
            context.knowledge[topic_name] = Topic(
                summary=commit.summary,
                entries=commit.entries,
                version=1
            )

        topic = context.knowledge[topic_name]

        # 5. Update context metadata
        context.version += 1
        context.last_commit_id = commit.commit_id
        context.last_commit_message = commit.summary
        context.last_commit_timestamp = commit.timestamp

        # 6. Add to commit history
        context.commit_history.append({
            'commit_id': commit.commit_id,
            'commit_hash': commit.commit_hash,
            'timestamp': commit.timestamp,
            'message': commit.summary,
            'participants': commit.participants,
            'consensus': commit.consensus_type,
            'approved_by': commit.approved_by,
            'signatures': commit.signatures
        })

        # 7. Create versioned markdown file
        from dpc_protocol.markdown_manager import MarkdownKnowledgeManager

        markdown_manager = MarkdownKnowledgeManager()

        # Compute content hash for markdown
        markdown_content = markdown_manager.topic_to_markdown_content(topic)
        content_hash = hashlib.sha256(markdown_content.encode('utf-8')).hexdigest()[:16]

        # Create markdown with frontmatter
        safe_topic_name = topic_name.lower().replace(' ', '_').replace("'", '')
        markdown_filename = f"{safe_topic_name}_{commit.commit_id}.md"
        markdown_path = markdown_manager.knowledge_dir / markdown_filename

        frontmatter = {
            'topic': topic_name,
            'commit_id': commit.commit_id,
            'commit_hash': commit.commit_hash,
            'parent_commit': commit.parent_commit_id or "",
            'content_hash': content_hash,
            'timestamp': commit.timestamp,
            'version': topic.version,
            'author': node_id,
            'participants': commit.participants,
            'approved_by': commit.approved_by,
            'rejected_by': commit.rejected_by,
            'consensus': commit.consensus_type,
            'confidence_score': commit.confidence_score,
            'signatures': commit.signatures,
            'cultural_perspectives': commit.cultural_perspectives_considered
        }

        markdown_manager.write_markdown_with_frontmatter(
            markdown_path,
            frontmatter,
            markdown_content
        )

        # Update topic reference
        topic.markdown_file = f"knowledge/{markdown_filename}"
        topic.commit_id = commit.commit_id
        topic.entries = []  # Clear entries (markdown is source of truth)

        # 8. Save context
        self.pcm_core.save_context(context)

        logger.info(f"✅ Applied commit: {commit.commit_id}")
        logger.info(f"   Topic: {topic_name}")
        logger.info(f"   Markdown: {markdown_filename}")
        logger.info(f"   Signatures: {len(commit.signatures)}")

    except Exception as e:
        logger.error(f"Error applying commit: {e}")
        raise
```

### Update: `dpc-client/core/dpc_client_core/service.py`

```python
# Add startup integrity check

async def start(self):
    """Start the core service."""

    # ... existing startup logic ...

    # NEW: Run integrity check
    logger.info("Running knowledge integrity check...")
    await self._startup_integrity_check()

    # ... rest of startup ...


async def _startup_integrity_check(self):
    """Verify all knowledge commits on startup."""
    from dpc_protocol.commit_integrity import verify_markdown_integrity

    knowledge_dir = self.settings.config_dir / "knowledge"

    if not knowledge_dir.exists():
        logger.info("No knowledge directory found, skipping integrity check")
        return

    warnings = []
    verified_count = 0

    for markdown_file in knowledge_dir.glob("*_commit-*.md"):
        result = verify_markdown_integrity(markdown_file)

        if result['valid']:
            verified_count += 1
        else:
            warnings.extend(result['warnings'])

    if warnings:
        # Broadcast warning to UI
        if self.local_api:
            await self.local_api.broadcast({
                'event': 'integrity_warnings',
                'data': {
                    'count': len(warnings),
                    'warnings': warnings
                }
            })

        logger.warning(f"⚠️ Knowledge integrity: {len(warnings)} issues found")
        for w in warnings:
            logger.warning(f"  [{w['severity'].upper()}] {w['file']}: {w['message']}")
    else:
        logger.info(f"✓ Knowledge integrity verified ({verified_count} commits)")
```

---

## Testing Plan

### Unit Tests

**File:** `dpc-protocol/tests/test_commit_integrity.py`

```python
import pytest
from dpc_protocol.commit_integrity import compute_commit_hash
from dpc_protocol.knowledge_commit import KnowledgeCommit, KnowledgeEntry

def test_hash_computation_deterministic():
    """Same content should produce same hash."""

    commit1 = KnowledgeCommit(
        topic="test",
        summary="Test commit",
        participants=["alice", "bob"],
        approved_by=["alice", "bob"]
    )

    commit2 = KnowledgeCommit(
        topic="test",
        summary="Test commit",
        participants=["alice", "bob"],
        approved_by=["alice", "bob"]
    )

    hash1 = compute_commit_hash(commit1)
    hash2 = compute_commit_hash(commit2)

    assert hash1 == hash2, "Same content should produce same hash"


def test_hash_changes_on_content_change():
    """Different content should produce different hash."""

    commit1 = KnowledgeCommit(
        topic="test",
        summary="Test commit"
    )

    commit2 = KnowledgeCommit(
        topic="test",
        summary="Different summary"
    )

    hash1 = compute_commit_hash(commit1)
    hash2 = compute_commit_hash(commit2)

    assert hash1 != hash2, "Different content should produce different hash"


def test_signature_roundtrip():
    """Sign and verify signature."""

    from dpc_protocol.commit_integrity import CommitSigner
    from cryptography.hazmat.primitives.asymmetric import rsa

    # Generate test key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )

    commit = KnowledgeCommit(topic="test", summary="Test")
    commit.compute_hash()

    # Sign
    signer = CommitSigner("test-node", private_key)
    commit.sign("test-node", private_key)

    # Verify
    assert "test-node" in commit.signatures
    assert len(commit.signatures["test-node"]) > 0


def test_commit_chain_validation():
    """Verify commit chain integrity."""

    from dpc_protocol.commit_integrity import verify_commit_chain

    # Create valid chain
    commit1 = KnowledgeCommit(topic="test", summary="Initial")
    commit1.compute_hash()
    commit1.parent_commit_id = None

    commit2 = KnowledgeCommit(topic="test", summary="Second")
    commit2.parent_commit_id = commit1.commit_id
    commit2.compute_hash()

    commit3 = KnowledgeCommit(topic="test", summary="Third")
    commit3.parent_commit_id = commit2.commit_id
    commit3.compute_hash()

    # Should not raise
    verify_commit_chain([commit1, commit2, commit3])


def test_commit_chain_detects_broken_link():
    """Detect broken commit chain."""

    from dpc_protocol.commit_integrity import verify_commit_chain, ChainIntegrityError

    commit1 = KnowledgeCommit(topic="test", summary="Initial")
    commit1.compute_hash()
    commit1.parent_commit_id = None

    commit2 = KnowledgeCommit(topic="test", summary="Second")
    commit2.parent_commit_id = "commit-invalid"  # Wrong parent
    commit2.compute_hash()

    with pytest.raises(ChainIntegrityError):
        verify_commit_chain([commit1, commit2])
```

### Integration Tests

**File:** `dpc-client/core/tests/test_collaborative_commits.py`

```python
import pytest
from dpc_client_core.consensus_manager import ConsensusManager
from dpc_protocol.knowledge_commit import KnowledgeCommit

@pytest.mark.asyncio
async def test_collaborative_commit_creation():
    """Test creating a collaborative commit with multiple signatures."""

    # Setup
    manager = ConsensusManager(...)

    # Create proposal
    proposal = create_test_proposal(participants=["alice", "bob"])

    # Vote
    await manager.cast_vote(proposal.proposal_id, "approve")

    # Simulate Bob's vote
    await manager.receive_vote(CommitVote(
        proposal_id=proposal.proposal_id,
        voter_node_id="bob",
        vote="approve"
    ))

    # Wait for commit
    await asyncio.sleep(0.1)

    # Verify commit was created
    context = manager.pcm_core.load_context()
    assert context.last_commit_id is not None

    # Verify hash-based ID
    assert context.last_commit_id.startswith("commit-")
    assert len(context.last_commit_id) == 23  # "commit-" + 16 hex chars

    # Verify signatures
    last_commit = context.commit_history[-1]
    assert "alice" in last_commit['signatures']
    assert "bob" in last_commit['signatures']


@pytest.mark.asyncio
async def test_receive_and_verify_peer_commit():
    """Test receiving collaborative commit from peer."""

    # Create commit
    commit = create_signed_commit(participants=["alice", "bob"])

    # Receive from peer
    verified_commit = await receive_collaborative_commit(
        commit.to_dict(),
        sender_node_id="alice"
    )

    # Verify
    assert verified_commit.commit_id == commit.commit_id
    assert verified_commit.verify_signatures()


@pytest.mark.asyncio
async def test_reject_tampered_commit():
    """Test rejection of tampered commit."""

    commit = create_signed_commit(participants=["alice", "bob"])
    commit_data = commit.to_dict()

    # Tamper with summary
    commit_data['summary'] = "Tampered summary"

    # Should raise IntegrityError
    with pytest.raises(IntegrityError):
        await receive_collaborative_commit(commit_data, sender_node_id="alice")
```

### Manual Tests

1. **Create Personal Commit:**
   - Chat with AI, extract knowledge
   - Manually edit in UI
   - Commit changes
   - Verify hash-based commit_id created
   - Verify markdown file has frontmatter with hash

2. **Collaborative Commit:**
   - Two peers chat and agree on knowledge
   - Both vote approve
   - Verify both peers have identical commit_id
   - Verify both signatures in frontmatter

3. **Tamper Detection:**
   - Manually edit markdown content
   - Restart service
   - Verify integrity warning appears in UI

4. **Chain Verification:**
   - Create 3 commits in sequence
   - Verify each parent_commit_id references previous
   - Delete middle commit file
   - Verify chain integrity error

---

## Security Considerations

### Threat Model

**What We Protect Against:**
1. ✅ **Accidental tampering** - User manually edits markdown
2. ✅ **Intentional tampering** - Malicious modification of commits
3. ✅ **Repudiation** - "I never agreed to that" (signatures prove agreement)
4. ✅ **Commit forgery** - Creating fake commits (hash + signature required)

**What We Don't Protect Against:**
1. ❌ **Private key theft** - If attacker gets `~/.dpc/node.key`, they can sign as you
2. ❌ **Denial of Service** - Attacker can delete all markdown files (local only)
3. ❌ **Man-in-the-Middle** (mitigated by TLS, but assumes TLS works)

### Cryptographic Strength

- **SHA256:** 256-bit security, collision-resistant
- **RSA 2048:** Equivalent to ~112-bit security, industry standard
- **PSS Padding:** Provably secure padding scheme

### Best Practices

1. **Protect private keys:**
   - Store in `~/.dpc/node.key` with restricted permissions (0600)
   - Never transmit private keys over network
   - Consider encrypting with passphrase (future enhancement)

2. **Verify all commits:**
   - Run integrity check on startup
   - Verify peer commits before applying
   - Alert user to any integrity issues

3. **Backup commits:**
   - Include signatures in backups
   - Backup entire `.dpc/knowledge/` directory
   - Encrypted backups recommended

---

## FAQ

**Q: Why use hash as commit_id instead of separate field?**
A: Makes commits content-addressable (like Git objects). Same content = same ID across all devices.

**Q: Why 16 hex chars instead of full 64-char hash?**
A: Balance between collision resistance (2^64 possibilities) and usability (shorter IDs).

**Q: Can two different commits have same hash?**
A: Astronomically unlikely (2^-256 probability). More likely to win lottery 10 times in a row.

**Q: What if I edit markdown manually?**
A: Content hash breaks, integrity check shows warning. Can restore from backup or create new commit.

**Q: Do all participants need to sign?**
A: Yes, for collaborative commits. Personal commits only need your signature.

**Q: What if a peer's signature is invalid?**
A: Commit is rejected. All signatures must be valid for commit to be accepted.

**Q: Can I delete old commit versions?**
A: Yes, but keep at least the current version. Archive old versions to `knowledge/archive/`.

---

## Related Implementation

**After completing this implementation, proceed to:**

[PERSONAL_CONTEXT_V2_IMPLEMENTATION.md](PERSONAL_CONTEXT_V2_IMPLEMENTATION.md) - Schema cleanup and markdown integration

**That document covers:**
- Removing legacy `instruction` field from `personal.json`
- Exporting knowledge to versioned markdown files (using hash-based commit_ids from this implementation)
- Minimal `personal.json` structure (~3-5 KB)
- Loading knowledge from markdown on startup

**Implementation Order:**
1. **This document** (COMMIT_INTEGRITY) - Foundational hash-based commits
2. **Next document** (PERSONAL_CONTEXT_V2) - Schema cleanup and markdown storage

---

## Next Steps

1. **Implement core hash functions** (`commit_integrity.py`)
2. **Update KnowledgeCommit dataclass** (add commit_hash, signatures)
3. **Modify consensus manager** (use hash-based IDs)
4. **Add startup integrity check** (verify all commits)
5. **Update UI** (show integrity status)
6. **Write tests** (unit, integration, manual)
7. **Document** (update CLAUDE.md with new architecture)

---

**End of Implementation Plan**
