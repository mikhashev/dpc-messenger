"""One way to reach this node's signing key.

The key was being loaded independently in a couple of places, each with its own
error handling and its own idea of where the node id comes from. That is the
shape of defect this codebase keeps producing: a correct implementation in one
module and an older one beside it, because a fix landed where somebody was
looking and not on its neighbours. So there is one loader here and callers ask
it.

Returns None rather than raising when there is no key on disk: a node without an
identity file can still read, and refusing to start would be a worse answer than
sending unsigned.

Deliberately not cached here. The first version held the signer in a module
global and two history-sync tests started failing depending on the order they
ran in: they point `Path.home()` at their own directory, and a cache filled by
an earlier test handed them the wrong key. Caching belongs to the caller that
knows its own lifetime — `ConversationMonitor` keeps one per instance — and a
vote is signed rarely enough that reading the file costs nothing.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def node_signer():
    """The CommitSigner for this node's key, or None if there is no key."""
    try:
        from cryptography.hazmat.primitives import serialization
        from dpc_protocol.commit_integrity import CommitSigner

        key_path = Path.home() / ".dpc" / "node.key"
        if not key_path.exists():
            return None
        with open(key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
        node_id_path = Path.home() / ".dpc" / "node.id"
        node_id = node_id_path.read_text().strip() if node_id_path.exists() else ""
        return CommitSigner(node_id, private_key)
    except Exception as e:  # noqa: BLE001 — an unusable key is not a crash
        logger.debug("CommitSigner init failed: %s", e)
        return None


def sign_vote(
    *,
    proposal_id: str,
    conversation_id: Optional[str],
    voter_node_id: str,
    vote,
    timestamp: Optional[str] = None,
) -> dict:
    """Signature fields for a vote, or {} when this node cannot sign.

    Empty rather than partial: a receiver treats a vote with no signature fields
    as legacy and falls back to the transport, which is safe. Half a set would
    look like a signature and fail verification, which is not.
    """
    from dpc_protocol.message_signing import VOTE_PREIMAGE_VERSION, vote_content_hash

    signer = node_signer()
    if signer is None:
        return {}
    vote_hash = vote_content_hash(
        proposal_id=proposal_id,
        conversation_id=conversation_id,
        voter_node_id=voter_node_id,
        vote=vote,
        timestamp=timestamp,
    )
    try:
        signature = signer.sign_commit(vote_hash)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not sign vote for %s: %s", str(proposal_id)[:8], e)
        return {}
    return {
        "vote_hash": vote_hash,
        "signature": signature,
        "signer_node_id": signer.node_id,
        "vote_preimage_version": VOTE_PREIMAGE_VERSION,
        "timestamp": timestamp,
    }
