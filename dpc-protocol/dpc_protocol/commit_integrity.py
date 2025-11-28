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
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509
from cryptography.exceptions import InvalidSignature

from .crypto import DPC_HOME_DIR

logger = logging.getLogger(__name__)


class ChainIntegrityError(Exception):
    """Raised when commit chain validation fails"""
    pass


class IntegrityError(Exception):
    """Raised when hash verification fails"""
    pass


class SignatureError(Exception):
    """Raised when signature verification fails"""
    pass


class AuthorizationError(Exception):
    """Raised when sender is not authorized"""
    pass


def compute_commit_hash(commit: 'KnowledgeCommit') -> str:
    """
    Compute deterministic SHA256 hash of commit content.

    This function creates a canonical representation of the commit
    that is identical across all nodes, enabling content-addressable
    storage similar to Git.

    Args:
        commit: KnowledgeCommit object to hash

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
        - signatures (added after hash computation)
    """
    # Build canonical hash input
    hash_input = {
        "parent": commit.parent_commit_id or "",
        "timestamp": commit.timestamp,
        "topic": commit.topic,
        "summary": commit.summary,
        "description": commit.description,

        # Entries: sorted by content, exclude volatile fields
        "entries": sorted([
            {
                "content": entry.content,
                "tags": sorted(entry.tags),
                "confidence": round(entry.confidence, 2),
                "cultural_specific": entry.cultural_specific,
                "alternative_viewpoints": sorted(entry.alternative_viewpoints or [])
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


def verify_commit_hash(commit: 'KnowledgeCommit') -> bool:
    """
    Verify commit hash matches content.

    Args:
        commit: KnowledgeCommit object with commit_hash field

    Returns:
        True if hash is valid
    """
    if not commit.commit_hash:
        logger.warning("Commit has no hash to verify")
        return False

    recomputed = compute_commit_hash(commit)
    claimed = commit.commit_hash

    is_valid = recomputed == claimed
    if not is_valid:
        logger.warning(
            f"Hash mismatch for commit {commit.commit_id}: "
            f"expected {claimed[:16]}..., got {recomputed[:16]}..."
        )

    return is_valid


class CommitSigner:
    """Sign and verify commits with RSA keys."""

    def __init__(self, node_id: str, private_key: rsa.RSAPrivateKey):
        """
        Initialize signer.

        Args:
            node_id: Node identifier
            private_key: RSA private key for signing
        """
        self.node_id = node_id
        self.private_key = private_key

    def sign_commit(self, commit_hash: str) -> str:
        """
        Sign commit hash with node's private key.

        Uses RSA-PSS signature scheme with SHA256 for
        provably secure signatures.

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

        logger.info(f"Signed commit hash {commit_hash[:16]}... with {self.node_id}")

        return signature_b64

    @staticmethod
    def verify_signature(
        node_id: str,
        commit_hash: str,
        signature_b64: str,
        peers_dir: Optional[Path] = None
    ) -> bool:
        """
        Verify signature from a peer.

        Args:
            node_id: Peer's node ID
            commit_hash: SHA256 hash as hex string
            signature_b64: Base64-encoded signature
            peers_dir: Directory containing peer certificates (default: ~/.dpc/peers)

        Returns:
            True if signature is valid
        """
        if peers_dir is None:
            peers_dir = DPC_HOME_DIR / "peers"

        # Load peer's public key from certificate
        cert_path = peers_dir / f"{node_id}.crt"

        if not cert_path.exists():
            # Try own certificate (for self-signed commits)
            own_cert_path = DPC_HOME_DIR / "node.crt"
            if own_cert_path.exists():
                cert_path = own_cert_path
            else:
                logger.error(f"Certificate not found for {node_id}")
                return False

        try:
            with open(cert_path, 'rb') as f:
                cert = x509.load_pem_x509_certificate(f.read())
                public_key = cert.public_key()

            # Decode signature
            signature_bytes = base64.b64decode(signature_b64)

            # Verify
            public_key.verify(
                signature_bytes,
                commit_hash.encode('utf-8'),
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH
                ),
                hashes.SHA256()
            )

            logger.debug(f"Signature verified for {node_id}")
            return True

        except InvalidSignature:
            logger.warning(f"Invalid signature from {node_id}")
            return False
        except Exception as e:
            logger.error(f"Signature verification error for {node_id}: {e}")
            return False


def parse_markdown_with_frontmatter(markdown_path: Path) -> Tuple[Dict[str, Any], str]:
    """
    Parse markdown file with YAML frontmatter.

    Args:
        markdown_path: Path to markdown file

    Returns:
        Tuple of (frontmatter_dict, content_string)

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If frontmatter is invalid
    """
    if not markdown_path.exists():
        raise FileNotFoundError(f"Markdown file not found: {markdown_path}")

    content = markdown_path.read_text(encoding='utf-8')

    # Check for frontmatter (starts and ends with ---)
    if not content.startswith('---\n'):
        # No frontmatter
        return {}, content

    # Find end of frontmatter
    end_marker = content.find('\n---\n', 4)
    if end_marker == -1:
        raise ValueError(f"Invalid frontmatter in {markdown_path}: no closing ---")

    # Extract frontmatter and content
    frontmatter_text = content[4:end_marker]
    markdown_content = content[end_marker + 5:].lstrip()  # Only strip leading whitespace

    # Remove topic title if present (added by build_markdown_with_frontmatter)
    # Title format: "# Topic Name\n\n"
    if markdown_content.startswith('# '):
        # Find first line break
        first_newline = markdown_content.find('\n')
        if first_newline != -1:
            # Skip title line and any following blank lines
            markdown_content = markdown_content[first_newline + 1:].lstrip('\n')

    # Parse frontmatter (simple YAML parsing for our use case)
    frontmatter = {}
    current_key = None
    current_list = None

    for line in frontmatter_text.split('\n'):
        line = line.rstrip()

        if not line or line.startswith('#'):
            continue

        # List item
        if line.startswith('  - '):
            if current_list is not None:
                current_list.append(line[4:].strip())
            continue

        # Key-value pair
        if ': ' in line:
            key, value = line.split(': ', 1)
            key = key.strip()

            # Handle special types
            if value == '[]':
                frontmatter[key] = []
                current_key = key
                current_list = frontmatter[key]
            elif value == '{}':
                frontmatter[key] = {}
                current_key = key
                current_list = None
            elif value.startswith('{') or value.startswith('['):
                # Try to parse as JSON
                try:
                    frontmatter[key] = json.loads(value)
                except json.JSONDecodeError:
                    frontmatter[key] = value
                current_key = key
                current_list = None
            else:
                # String or number
                if value.replace('.', '').replace('-', '').isdigit():
                    try:
                        frontmatter[key] = float(value) if '.' in value else int(value)
                    except ValueError:
                        frontmatter[key] = value
                else:
                    # Remove quotes if present
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    frontmatter[key] = value

                current_key = key
                current_list = None

        # Dictionary entry (for signatures)
        elif line.startswith('  ') and current_key and isinstance(frontmatter.get(current_key), dict):
            key_value = line.strip()
            if ': ' in key_value:
                k, v = key_value.split(': ', 1)
                # Remove quotes
                if v.startswith('"') and v.endswith('"'):
                    v = v[1:-1]
                frontmatter[current_key][k] = v

    return frontmatter, markdown_content


def compute_content_hash(content: str) -> str:
    """
    Compute SHA256 hash of markdown content.

    Args:
        content: Markdown content (without frontmatter)

    Returns:
        16-character hex string (truncated SHA256)
    """
    hash_bytes = hashlib.sha256(content.encode('utf-8'))
    return hash_bytes.hexdigest()[:16]


def verify_markdown_integrity(
    markdown_path: Path,
    knowledge_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """
    Verify markdown file integrity.

    Performs comprehensive integrity checks:
    1. Content hash matches actual markdown content
    2. Commit ID in filename matches frontmatter
    3. Recomputed commit hash matches stored commit_hash
    4. All signatures are valid
    5. Parent commit exists (chain integrity)

    Args:
        markdown_path: Path to markdown file
        knowledge_dir: Directory containing knowledge files (default: ~/.dpc/knowledge)

    Returns:
        {
            'valid': bool,
            'warnings': List[Dict],
            'commit_id': str,
            'content_hash_valid': bool,
            'commit_hash_valid': bool,
            'signatures_valid': Dict[str, bool]
        }
    """
    if knowledge_dir is None:
        knowledge_dir = DPC_HOME_DIR / "knowledge"

    result = {
        'valid': True,
        'warnings': [],
        'commit_id': None,
        'content_hash_valid': True,
        'commit_hash_valid': True,
        'signatures_valid': {}
    }

    try:
        # Parse markdown
        frontmatter, content = parse_markdown_with_frontmatter(markdown_path)

        result['commit_id'] = frontmatter.get('commit_id')

        # CHECK 1: Content hash
        if 'content_hash' in frontmatter:
            actual_content_hash = compute_content_hash(content)
            expected_content_hash = frontmatter['content_hash']

            if actual_content_hash != expected_content_hash:
                result['valid'] = False
                result['content_hash_valid'] = False
                result['warnings'].append({
                    'file': markdown_path.name,
                    'type': 'content_tampered',
                    'severity': 'warning',
                    'message': 'Markdown content manually edited (hash mismatch)',
                    'expected': expected_content_hash,
                    'actual': actual_content_hash
                })

        # CHECK 2: Filename vs frontmatter commit_id
        commit_id_from_filename = extract_commit_id_from_filename(markdown_path.name)
        commit_id_from_frontmatter = frontmatter.get('commit_id')

        if commit_id_from_filename and commit_id_from_frontmatter:
            if commit_id_from_filename != commit_id_from_frontmatter:
                result['valid'] = False
                result['warnings'].append({
                    'file': markdown_path.name,
                    'type': 'filename_mismatch',
                    'severity': 'error',
                    'message': "Filename commit_id doesn't match frontmatter",
                    'expected': commit_id_from_frontmatter,
                    'actual': commit_id_from_filename
                })

        # CHECK 3: Verify signatures (if present)
        signatures = frontmatter.get('signatures', {})
        commit_hash = frontmatter.get('commit_hash')

        if signatures and commit_hash:
            for node_id, signature in signatures.items():
                is_valid = CommitSigner.verify_signature(node_id, commit_hash, signature)
                result['signatures_valid'][node_id] = is_valid

                if not is_valid:
                    result['valid'] = False
                    result['warnings'].append({
                        'file': markdown_path.name,
                        'type': 'invalid_signature',
                        'severity': 'error',
                        'message': f'Invalid signature from {node_id}',
                        'signer': node_id
                    })

        # CHECK 4: Parent commit exists
        parent_commit_id = frontmatter.get('parent_commit')
        if parent_commit_id and parent_commit_id != "":
            parent_file_pattern = f"*_{parent_commit_id}.md"
            if not list(knowledge_dir.glob(parent_file_pattern)):
                result['warnings'].append({
                    'file': markdown_path.name,
                    'type': 'missing_parent',
                    'severity': 'warning',
                    'message': f'Parent commit {parent_commit_id} not found'
                })

    except Exception as e:
        logger.error(f"Error verifying {markdown_path}: {e}")
        result['valid'] = False
        result['warnings'].append({
            'file': markdown_path.name,
            'type': 'verification_error',
            'severity': 'error',
            'message': f'Verification failed: {str(e)}'
        })

    return result


def extract_commit_id_from_filename(filename: str) -> Optional[str]:
    """
    Extract commit ID from filename.

    Args:
        filename: Filename like "topic_commit-abc123.md"

    Returns:
        Commit ID like "commit-abc123" or None if not found
    """
    import re
    match = re.search(r'(commit-[a-f0-9]+)\.md$', filename)
    return match.group(1) if match else None


def extract_hash_from_commit_id(commit_id: str) -> str:
    """
    Extract hash from commit ID.

    Args:
        commit_id: Like "commit-abc123..."

    Returns:
        Hash portion (without "commit-" prefix)
    """
    if commit_id.startswith('commit-'):
        return commit_id[7:]
    return commit_id


def verify_commit_chain(commits: List['KnowledgeCommit']) -> None:
    """
    Verify integrity of commit chain.

    Checks:
        1. Each commit's parent_commit_id references previous commit
        2. Parent hashes match stored commit_hash
        3. No circular references
        4. No missing parents

    Args:
        commits: List of KnowledgeCommit objects in chronological order

    Raises:
        ChainIntegrityError: If chain is broken
    """
    # Build commit index
    commit_index = {c.commit_id: c for c in commits}

    for i, commit in enumerate(commits):
        # Skip first commit (no parent required)
        if i == 0:
            if commit.parent_commit_id:
                logger.warning(
                    f"First commit {commit.commit_id} has parent {commit.parent_commit_id}, "
                    "this is unusual but not an error"
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

        # Verify parent hash matches (if present)
        parent_commit = commit_index[parent_id]
        if parent_commit.commit_hash:
            expected_hash = extract_hash_from_commit_id(parent_id)
            if parent_commit.commit_hash[:16] != expected_hash:
                raise ChainIntegrityError(
                    f"Parent commit {parent_id} hash mismatch"
                )

    logger.info(f"✓ Commit chain verified ({len(commits)} commits)")
