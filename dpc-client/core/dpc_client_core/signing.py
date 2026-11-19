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


def quorum_is_proven(
    *,
    proposal_id: str,
    conversation_id: Optional[str],
    participants,
    votes: dict,
) -> bool:
    """Do these signed votes show every participant approving that proposal?

    This is what makes a `session_started_at` marker checkable by a node that
    kept nothing older than it: the votes travel with the marker, so the check
    needs no history at all.

    Unanimity, matching the rule a reset already runs under: the marker is a
    statement about the whole group, so it needs the whole group's agreement
    (GROUP_CHAT.md, Session Reset). A signature that cannot be checked for want of a
    certificate counts as not proven: the marker will be adopted later, when the
    certificate arrives, rather than on trust now.
    """
    from dpc_protocol.commit_integrity import CommitSigner
    from dpc_protocol.message_signing import VOTE_PREIMAGE_VERSION, vote_content_hash

    wanted = set(participants or ())
    if not wanted:
        return False

    for node_id in wanted:
        vote = (votes or {}).get(node_id)
        if not isinstance(vote, dict):
            return False
        if vote.get("vote") not in (True, "approve", "yes"):
            return False
        if vote.get("vote_preimage_version") != VOTE_PREIMAGE_VERSION:
            return False
        if vote.get("signer_node_id") != node_id:
            return False
        expected = vote_content_hash(
            proposal_id=proposal_id,
            conversation_id=conversation_id,
            voter_node_id=node_id,
            vote=vote.get("vote"),
            timestamp=vote.get("timestamp"),
        )
        if expected != vote.get("vote_hash"):
            return False
        try:
            if CommitSigner.verify_signature(
                node_id, vote["vote_hash"], vote.get("signature")
            ) is not True:
                return False
        except Exception:  # noqa: BLE001
            return False
    return True


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


# --- ADR-047 phase 1: the author-signed file description ----------------------

# The verdicts a file description gets on receipt (specs/dptp_v1.md §4.4), the
# §4.2 words: checked against the author's cached certificate; signed but no
# certificate here yet; no description this node can read (absent, or another
# `file_preimage_version`); present and wrong.
FILE_VERIFIED = "verified"
FILE_UNVERIFIED = "unverified"
FILE_LEGACY = "legacy"
FILE_REJECTED = "rejected"

# Every key of a description, in the order the author writes them. A relay or
# holder re-serves exactly these, so an honest description crosses every hop
# byte for byte, and nothing a hop added rides along unsigned.
FILE_DESCRIPTION_FIELDS = (
    "file_preimage_version", "message_id", "sender_node_id", "size_bytes", "chunk_size",
    "hash", "chunks_hash", "filename", "mime_type", "file_signature",
)

_HEX = frozenset("0123456789abcdef")
_MAX_ID_CHARS = 128


def _is_digest(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _is_count(value, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def file_conversation_id(*, group_id: Optional[str], my_node_id: Optional[str],
                         peer_node_id: Optional[str], author: Optional[str]) -> Optional[str]:
    """The `conversation_id` a file description must have been signed for, from this node's state.

    A group file: the group. A one-to-one file was signed for its recipient,
    so a node that receives it from its author checks it against its own id,
    and an author that gets its own file back (FILE_REQUEST) against the
    other party's. A one-to-one description whose author is neither party is
    some other chat's: None, which no description verifies against.
    """
    if group_id:
        return group_id
    if author and author == my_node_id:
        return peer_node_id or None
    if author and author == peer_node_id:
        return my_node_id or None
    return None


def file_description_verdict(desc, conversation_id: Optional[str]) -> str:
    """FILE_VERIFIED / _UNVERIFIED / _LEGACY / _REJECTED for one description.

    The signature is checked against `sender_node_id`'s certificate, so a
    description signed by anyone else is rejected; the preimage is rebuilt
    with the caller's `conversation_id`, so one replayed into another chat is
    rejected. Whether the offer carrying it agrees with it is the caller's
    check (`file_transfer_manager.offer_file_description`).
    """
    from dpc_protocol.commit_integrity import CommitSigner
    from dpc_protocol.message_signing import FILE_PREIMAGE_VERSION, file_content_hash

    if not isinstance(desc, dict) or desc.get("file_preimage_version") != FILE_PREIMAGE_VERSION:
        return FILE_LEGACY
    message_id = desc.get("message_id")
    author = desc.get("sender_node_id")
    filename = desc.get("filename")
    mime_type = desc.get("mime_type")
    signature = desc.get("file_signature")
    if not (isinstance(message_id, str) and 0 < len(message_id) <= _MAX_ID_CHARS
            and isinstance(author, str) and author.startswith("dpc-node-")
            and len(author) <= _MAX_ID_CHARS
            and _is_count(desc.get("size_bytes"), 0) and _is_count(desc.get("chunk_size"), 1)
            and _is_digest(desc.get("hash")) and _is_digest(desc.get("chunks_hash"))
            and isinstance(filename, str) and isinstance(mime_type, str)
            and isinstance(signature, str) and signature):
        return FILE_REJECTED
    if not conversation_id:
        return FILE_REJECTED
    content_hash = file_content_hash(
        conversation_id=conversation_id,
        message_id=message_id,
        sender_node_id=author,
        size_bytes=desc["size_bytes"],
        chunk_size=desc["chunk_size"],
        hash=desc["hash"],
        chunks_hash=desc["chunks_hash"],
        filename=filename,
        mime_type=mime_type,
    )
    try:
        result = CommitSigner.verify_signature(author, content_hash, signature)
    except Exception as e:  # noqa: BLE001 - bad base64, unreadable certificate
        logger.warning("File description check failed for %s: %s", author[:20], e)
        return FILE_REJECTED
    if result is True:
        return FILE_VERIFIED
    if result is None:
        return FILE_UNVERIFIED
    return FILE_REJECTED


def sign_file_description(
    *,
    conversation_id: str,
    message_id: str,
    sender_node_id: str,
    size_bytes: int,
    chunk_size: int,
    hash: str,
    chunks_hash: str,
    filename: str,
    mime_type: str,
) -> dict:
    """The author's `file_description` for one file, or {} when this node cannot sign.

    Empty rather than partial, as `sign_vote`: an offer without a description
    is read as legacy, which is safe; half of one would be rejected. Also
    empty when the key on disk is not `sender_node_id`'s, so a node never
    signs a description in another node's name.
    """
    from dpc_protocol.message_signing import FILE_PREIMAGE_VERSION, file_content_hash

    signer = node_signer()
    if signer is None:
        return {}
    if signer.node_id != sender_node_id:
        logger.warning("The key on disk is %s's, not %s's: file %s sent without a description",
                       str(signer.node_id)[:20], str(sender_node_id)[:20], filename)
        return {}
    fields = {
        "message_id": message_id,
        "sender_node_id": sender_node_id,
        "size_bytes": size_bytes,
        "chunk_size": chunk_size,
        "hash": hash,
        "chunks_hash": chunks_hash,
        "filename": filename,
        "mime_type": mime_type,
    }
    try:
        signature = signer.sign_commit(file_content_hash(conversation_id=conversation_id, **fields))
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not sign the description of %s: %s", filename, e)
        return {}
    return {"file_preimage_version": FILE_PREIMAGE_VERSION, **fields, "file_signature": signature}
