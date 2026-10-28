"""
File Transfer Manager - Chunked P2P File Transfers

This module implements chunked file transfer functionality for D-PC Messenger.
Files are split into chunks, transferred with hash verification, and support
progress tracking and cancellation.

Architecture:
- Chunked transfer (64KB default). The receiver writes each verified chunk at
  its offset (index * the offer's chunk_size) into a temporary file beside the
  final one, so its memory grows with the number of chunks, not with the file
- Per-chunk CRC32 (re-asked through FILE_CHUNK_RETRY), SHA256 of the whole file
  read back from disk at the end
- A chunk that has not arrived is re-asked after a silence; after a bounded
  number of fruitless rounds the receiver gives up with `missing_chunks`
- No resume: a transfer that ends (cancel, disconnect, restart, failure)
  deletes its temporary file and starts over from the beginning next time
- Progress callbacks for UI updates
- Firewall-gated: node or group entry, else the direct-connection entry (ask by
  default: a proved key is not an acquaintance), else the user is asked
- An offer that waits for a person has a deadline on both sides (see
  OFFER_ANSWER_DEADLINE_S, UNANSWERED_UPLOAD_RELEASE_S)

Transfer Flow:
1. Sender: Compute hash, create FILE_OFFER
2. Receiver: Check firewall, prompt user, send FILE_ACCEPT
3. Sender: Send FILE_CHUNK messages (with progress)
4. Receiver: Write each chunk to the temporary file, verify hash, rename, send FILE_COMPLETE
5. Both: Update conversation history with attachment metadata

Features:
- No size limit in this module; the receiver's firewall may set one. The
  offer still carries one CRC per chunk, which grows with the file
- Direct TLS preferred for >100MB (fallback to WebRTC/relay for smaller)
- Per-peer storage: ~/.dpc/conversations/{peer_id}/files/
- Metadata references in conversation history (not full content)

Privacy:
- A sender no rule names is put to the user, a directly connected one included
  unless the owner set `file_transfer.proved_connection` to allow
- Optional per-peer size limits
- Optional MIME type restrictions
"""

import asyncio
import logging
import hashlib
import os
import base64
import time
import uuid
import zlib  # For CRC32 checksums
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Callable, List, Any, NamedTuple, Tuple, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import Enum

from ..firewall import FILE_TRANSFER_PROVED_CONNECTION_DEFAULT, FILE_TRANSFER_PROVED_CONNECTION_KEY
from ..p2p_manager import peer_proof

if TYPE_CHECKING:
    from ..p2p_manager import P2PManager
    from ..firewall import ContextFirewall
    from ..settings import Settings

logger = logging.getLogger(__name__)

# Preparation progress reaches the UI over its local websocket. At disk speed a
# fixed byte interval emitted ~13 events a second on a 47 GB file and the socket
# died on its keepalive ping, so progress is limited by time, not by volume.
PREPARATION_PROGRESS_MIN_INTERVAL_S = 0.5

# One read of the file serves both digests; the block is a multiple of the chunk size.
HASH_READ_BLOCK_BYTES = 8 * 1024 * 1024

_monotonic = time.monotonic  # Indirection so a test can drive the clock
_answer_sleep = asyncio.sleep  # Indirection so a test can hold the answer clocks

# Receiving. The sender streams chunks back to back (chunk_delay apart), so a
# silence means the stream has ended or lost something, not that it is slow:
# 30 s of nothing is 64 KB at ~2 KB/s, slower than any relay we route through.
RECEIVE_STALL_TIMEOUT_S = 30.0
# Indices re-asked per round: 64 chunks (4 MB at the default size) keeps one
# re-ask from flooding the sender, which answers each with its own file read.
RECEIVE_REASK_BATCH = 64
# Rounds in a row with no new chunk before the receiver gives up. With the
# timeout above a chunk that is never sent ends the transfer within
# (rounds + 1) * timeout = 120 s of the last chunk that did arrive.
RECEIVE_STALL_ROUNDS = 3
# FILE_CANCEL reason for a receive that cannot be written to the local disk
# (disk full, permission). No word in DPTP §3.11 covered it: this one is new.
RECEIVE_ERROR_REASON = "receive_error"
# FILE_CANCEL reason for a group file offer this node already has under the
# same identity: received, still receiving, or sent it itself. The sender takes
# it as a quiet success. Mike's word, 2026-10-25.
ALREADY_RECEIVED_REASON = "already_received"
# How long, and for how many ids, a receiver remembers a FILE_CANCEL it sent. A
# sender that kept streaming (its loop had not seen the cancel yet) is answered
# once more with the same reason per window, not once per chunk.
CANCEL_MEMORY_S = 600.0
CANCEL_MEMORY_MAX = 256
# An identity field read off a peer's offer becomes a record id here: bounded.
_MAX_IDENTITY_CHARS = 128

# FILE_CANCEL reason for an offer nobody answered in time. The receiver sends it
# when its person did not answer within OFFER_ANSWER_DEADLINE_S; the sender sends
# it when it released an upload that got neither FILE_ACCEPT nor FILE_CANCEL
# within UNANSWERED_UPLOAD_RELEASE_S. Either way it means "the offer expired
# unanswered": not a refusal and not a failure. Mike's word, 2026-10-25.
NO_ANSWER_REASON = "no_answer"
# How long a receiver lets an offer wait for its person (the question "allow
# files from X?" and the accept button of an ordinary file alike) before it
# refuses on its own with NO_ANSWER_REASON. Not a setting: the sender's release
# below is derived from it. Mike's call, 2026-10-25.
OFFER_ANSWER_DEADLINE_S = 120.0
# How long a sender keeps an upload that has had neither FILE_ACCEPT nor
# FILE_CANCEL before it frees the slot by itself. Longer than the receiver's
# deadline by the transit time of its NO_ANSWER_REASON plus a margin, so a
# current receiver answers first; it exists for an older receiver that never
# sends the word, so that no peer can hold an upload slot for good.
UNANSWERED_UPLOAD_RELEASE_S = 180.0

# The receiver's answer to an offer. Explicit entries win in this order: the
# node entry, then the sender's first group that has an entry (allow or deny
# alike), then `file_transfer.proved_connection` when the sender is on a
# proved (direct TLS) connection; a sender none of these names, or whose
# proved_connection entry says "ask" (the default), is ASKed — the offer waits
# for the user instead of being refused. Mike's call, 2026-10-25.
FILE_TRANSFER_ALLOW = "allow"
FILE_TRANSFER_DENY = "deny"
FILE_TRANSFER_ASK = "ask"
FIREWALL_DENIED_TEXT = "File transfer not permitted by firewall rules"
# `denial_reason` of the FILE_CANCEL the sender gets when the user declines an
# offer that was put to them; the `reason` word stays the one the UI sends.
ASKED_OFFER_DECLINED_TEXT = "The receiver declined this file"


class FileTransferDecision(NamedTuple):
    """`outcome` is FILE_TRANSFER_ALLOW / _DENY / _ASK — a word, never a truth value.

    `basis` names what decided: `node`, `group`, `proved_connection` (also for an
    ASK when that entry says "ask"), or `none` for an ASK no entry caused.
    `reason` is the sentence a FILE_CANCEL carries as `denial_reason`.
    """
    outcome: str
    reason: Optional[str]
    basis: str


def hash_file_in_one_pass(
    file_path: Path,
    chunk_size: int,
    with_sha256: bool = True,
    with_chunk_crcs: bool = True,
    on_progress: Optional[Callable[[int, int], None]] = None,
    min_interval_s: float = PREPARATION_PROGRESS_MIN_INTERVAL_S,
) -> Tuple[Optional[str], List[str]]:
    """
    Read the file once and produce the SHA-256 and the per-chunk CRC32 list.

    Returns ``(sha256_hex or None, [crc32 hex of each chunk_size slice])``. The
    values are the same as hashing the whole file and each ``chunk_size`` read
    separately: the block is a multiple of ``chunk_size`` so slice boundaries
    never move, and only the last slice may be short.

    ``on_progress(bytes_done, total_bytes)`` is called at most once per
    ``min_interval_s`` of wall time, plus once at the end (100%).
    """
    block_size = chunk_size * max(1, HASH_READ_BLOCK_BYTES // chunk_size)
    sha256 = hashlib.sha256() if with_sha256 else None
    chunk_hashes: List[str] = []
    total_size = file_path.stat().st_size
    bytes_done = 0
    last_emit_at = _monotonic()
    last_emit_bytes = -1

    with open(file_path, 'rb') as f:
        while block := f.read(block_size):
            if sha256 is not None:
                sha256.update(block)
            if with_chunk_crcs:
                view = memoryview(block)
                for start in range(0, len(block), chunk_size):
                    crc = zlib.crc32(view[start:start + chunk_size]) & 0xffffffff
                    chunk_hashes.append(f"{crc:08x}")
            bytes_done += len(block)

            if on_progress is not None:
                now = _monotonic()
                if now - last_emit_at >= min_interval_s:
                    on_progress(bytes_done, total_size)
                    last_emit_at = now
                    last_emit_bytes = bytes_done

    if on_progress is not None and last_emit_bytes != bytes_done:
        on_progress(bytes_done, total_size)  # final 100% event

    return (sha256.hexdigest() if sha256 is not None else None), chunk_hashes


class TransferStatus(Enum):
    """File transfer status states."""
    PENDING = "pending"
    TRANSFERRING = "transferring"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def group_file_ui_key(group_id: str, filename: str) -> str:
    """Dedup key shared by the group send paths and FileCompleteHandler.

    The sender seeds it on send; the handler checks it on every FILE_COMPLETE
    echo. `filename` is the on-disk name (what `transfer.filename` carries).
    """
    return f"group_file_ui:{group_id}:{filename}"


def group_file_keys(group_id: str, message_id: Optional[str],
                    file_hash: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """The identity of one group file, as `(by message_id, by hash)`.

    A group file keeps the author's `message_id` across every relay, as a
    GROUP_TEXT does; it is how a member knows a copy it is offered is one it
    already has. An offer from a peer that predates the field carries none, and
    then the group and the file's hash stand in. That fallback costs one thing:
    such a peer sending the same bytes to the group twice in one session gets
    its second copy refused here as `already_received`.
    """
    by_id = f"group_file:{group_id}:{message_id}" if message_id else None
    by_hash = (f"group_file_hash:{group_id}:{file_hash}"
               if file_hash and file_hash != "none" else None)
    return by_id, by_hash


def offer_identity_field(value: Any) -> Optional[str]:
    """A peer's `message_id` / `sender_node_id`, or None when it is not a usable one."""
    if isinstance(value, str) and value and len(value) <= _MAX_IDENTITY_CHARS:
        return value
    return None


def saves_screenshots_to_disk(firewall) -> bool:
    """The owner's `image_transfer.save_screenshots_to_disk`; true when unset.

    Off, a screenshot is kept on neither side: the receiver never writes it and
    the sender deletes its copy once no upload needs it. The chat then shows the
    thumbnail the record carries, so no record may point at the deleted file.
    """
    rules = getattr(firewall, "rules", None) or {}
    return bool(rules.get("image_transfer", {}).get("save_screenshots_to_disk", True))


@dataclass
class FileTransfer:
    """
    Represents an active file transfer session.

    Attributes:
        transfer_id: Unique transfer identifier
        filename: Original filename
        size_bytes: Total file size in bytes
        hash: SHA256 hash of complete file
        mime_type: MIME type of file
        chunk_size: Size of each chunk in bytes
        node_id: Peer node ID (sender or receiver)
        direction: 'upload' or 'download'
        status: Current transfer status
        chunks_received: Set of received chunk indices
        total_chunks: Total number of chunks
        file_path: Path to file being sent (for uploads)
        progress_callback: Optional callback for progress updates
        chunk_hashes: List of CRC32 hashes for each chunk (v0.11.1)
        chunks_failed: Set of chunk indices that failed verification (v0.11.1)
        retry_count: Dict mapping chunk index to retry attempt count (v0.11.1)
        max_retries: Maximum retry attempts per chunk (default: 3)
        image_metadata: Optional dict with image metadata (dimensions, thumbnail, etc.) for v0.12.0+
        voice_metadata: Optional dict with voice metadata (duration, sample_rate, codec, etc.) for v0.13.0+

    A download holds no file bytes: each chunk goes to `receive_path` at
    index * chunk_size (the offer's chunk_size) and only its index is kept.
    """
    transfer_id: str
    filename: str
    size_bytes: int
    hash: str
    mime_type: str
    chunk_size: int
    node_id: str
    direction: str  # 'upload' or 'download'
    status: TransferStatus
    chunks_received: set
    total_chunks: int
    file_path: Optional[Path] = None
    progress_callback: Optional[Callable[[str, int, int], None]] = None
    chunk_hashes: Optional[list] = None  # CRC32 hashes for integrity
    chunks_failed: Optional[set] = None  # Failed chunk indices
    retry_count: Optional[dict] = None   # Chunk index -> retry count
    max_retries: int = 3                 # Max retries per chunk
    image_metadata: Optional[dict] = None  # Image metadata (dimensions, thumbnail, etc.) v0.12.0+
    voice_metadata: Optional[dict] = None  # Voice metadata (duration, sample_rate, codec) v0.13.0+
    group_id: Optional[str] = None  # Group ID for group file transfers (v0.19.0+)
    # A group file's identity, the GROUP_TEXT words: the member who wrote it and
    # its id there. Every relay carries both unchanged. On a download they are
    # the offer's claim, unsigned, and never decide who the file is credited to.
    sender_node_id: Optional[str] = None
    message_id: Optional[str] = None
    # An upload that passes on a group file this node received, never on the wire:
    # its completion is nobody's message here.
    group_relay: bool = False
    # A download whose sender no rule names (FILE_TRANSFER_ASK), until the user
    # answers "allow files from X?"; a decline meanwhile tells the sender why.
    awaiting_permission: bool = False

    # Whether the file stays on disk. A download decides it at the first chunk
    # (a screenshot with save_screenshots_to_disk off is verified chunk by chunk
    # and never written); an upload at the offer (False: the sender's copy is
    # deleted once no upload of it needs it).
    save_to_disk: Optional[bool] = None
    # Receive side only. Decided at the first chunk: where the file goes.
    destination: Optional[Path] = None
    receive_path: Optional[Path] = None  # the temporary file, renamed to `destination` at the end
    receive_file: Any = field(default=None, repr=False, compare=False)  # open handle on receive_path
    quiet_since: float = 0.0             # monotonic time of the last chunk or re-ask
    stall_rounds: int = 0                # re-ask rounds in a row without a new chunk
    reasked: set = field(default_factory=set)  # indices of the current re-ask batch still owed
    stall_watch: Any = field(default=None, repr=False, compare=False)  # asyncio.Task
    finalizing: bool = False
    # The clock of an offer nobody has answered yet: on a download the receiver's
    # deadline (OFFER_ANSWER_DEADLINE_S), on an upload the sender's release
    # (UNANSWERED_UPLOAD_RELEASE_S). Stopped by the answer or by leaving the table.
    answer_timer: Any = field(default=None, repr=False, compare=False)  # asyncio.Task


class _TransferTable(dict):
    """`active_transfers`: leaving it, by any path, releases what a receive holds.

    A transfer is dropped from the table in several places, some outside this
    module (the peer-disconnect cleanup in service.py deletes the entry
    directly). Each of them must close the temporary file, delete it and stop
    the stall watch, so the table does it on removal rather than each caller.
    """

    def __init__(self, on_remove: Callable[[Any], None]):
        super().__init__()
        self._on_remove = on_remove

    def __setitem__(self, key, value):
        old = self.get(key)
        super().__setitem__(key, value)
        if old is not None and old is not value:
            self._on_remove(old)

    def __delitem__(self, key):
        old = self[key]
        super().__delitem__(key)
        self._on_remove(old)

    def pop(self, key, *default):
        if key in self:
            old = super().pop(key)
            self._on_remove(old)
            return old
        return super().pop(key, *default)

    def popitem(self):
        key, old = super().popitem()
        self._on_remove(old)
        return key, old

    def clear(self):
        olds = list(self.values())
        super().clear()
        for old in olds:
            self._on_remove(old)


# `reason` of a fan-out's `not_offered` row. The cap's word is the setting's own
# name; every other refusal to make the offer is `send_error`, FILE_CANCEL's word
# for a send that failed, with the exception's text in `detail`.
NOT_OFFERED_UPLOAD_LIMIT = "max_concurrent_transfers"
NOT_OFFERED_SEND_ERROR = "send_error"
UPLOAD_LIMIT_SETTING = "[file_transfer] max_concurrent_transfers"
# How many open uploads the sentence names before it says "and N more".
_UPLOAD_LIMIT_SENTENCE_LISTS = 6
_UPLOAD_STATUS_WORDS = {
    TransferStatus.PENDING.value: "waiting for an answer",
    TransferStatus.TRANSFERRING.value: "sending",
}


def upload_limit_sentence(limit: int, open_uploads: List[Dict[str, Any]]) -> str:
    """The one sentence a sender is told when `send_file` finds every upload slot taken.

    The 1:1 senders get it as the error's text; a group fan-out puts it in the
    result's `upload_limit`, so both screens say the same thing. `open_uploads`
    are the rows `FileTransferManager.open_uploads` returns.
    """
    shown = [
        f"{u['filename']} to {u.get('name') or u['node_id']} "
        f"({_UPLOAD_STATUS_WORDS.get(u['status'], u['status'])})"
        for u in open_uploads[:_UPLOAD_LIMIT_SENTENCE_LISTS]
    ]
    more = len(open_uploads) - len(shown)
    if more > 0:
        shown.append(f"and {more} more")
    return (
        f"Upload limit reached: {len(open_uploads)} uploads are open and the limit is {limit} "
        f"({UPLOAD_LIMIT_SETTING} in config.ini, read at start). "
        f"Open now: {'; '.join(shown)}. "
        "Each stays open until its receiver answers and the transfer ends."
    )


class MaxConcurrentTransfersExceeded(RuntimeError):
    """`send_file` found as many open uploads as `max_concurrent_transfers` allows.

    A `RuntimeError`, as it was before it had a type, so a caller that catches
    that keeps working. `str()` is `upload_limit_sentence`; `limit` and
    `open_uploads` are what it was built from, taken at the moment of the refusal.
    """

    def __init__(self, limit: int, open_uploads: List[Dict[str, Any]]):
        self.limit = limit
        self.open_uploads = open_uploads
        super().__init__(upload_limit_sentence(limit, open_uploads))


class NotOfferedReport:
    """The members of a group fan-out that were not offered the file, and why.

    A fan-out visits the members connected to this node; one that fails is
    recorded here with `record`, and `fields` is what goes into the result next
    to `transfer_ids`. Nothing is added when everyone was offered, so a send
    that went through looks as it always did.
    """

    def __init__(self, label: Callable[[str], Optional[str]] = lambda node_id: None):
        self._label = label
        self.rows: List[Dict[str, Any]] = []
        self._cap: Optional[MaxConcurrentTransfersExceeded] = None

    def record(self, node_id: str, error: BaseException) -> None:
        row: Dict[str, Any] = {"node_id": node_id, "name": self._label(node_id)}
        if isinstance(error, MaxConcurrentTransfersExceeded):
            row["reason"] = NOT_OFFERED_UPLOAD_LIMIT
            self._cap = error  # the last one saw the fullest table
        else:
            row["reason"] = NOT_OFFERED_SEND_ERROR
            row["detail"] = str(error) or type(error).__name__
        self.rows.append(row)

    def fields(self) -> Dict[str, Any]:
        if not self.rows:
            return {}
        out: Dict[str, Any] = {"not_offered": self.rows}
        if self._cap is not None:
            out["upload_limit"] = {
                "limit": self._cap.limit,
                "open_uploads": self._cap.open_uploads,
                "sentence": str(self._cap),
            }
        return out


class FileTransferManager:
    """
    Manages P2P file transfers with chunking, hash verification, and progress tracking.

    Client Mode:
    - send_file() - initiate file transfer to peer
    - cancel_transfer() - cancel active transfer

    Server Mode (receiving):
    - handle_file_offer() - process incoming FILE_OFFER
    - handle_file_chunk() - receive and reassemble chunks
    - handle_file_complete() - verify hash and finalize

    Attributes:
        p2p_manager: P2P manager for sending messages
        firewall: Firewall for permission checks
        settings: Settings for configuration
        active_transfers: Dict of active transfer sessions
        chunk_size: Default chunk size in bytes (default: 65536 = 64KB)
        chunk_delay: Delay between chunks in seconds (default: 0.001 = 1ms)
        background_threshold_mb: Threshold for background transfers
        direct_tls_threshold_mb: Threshold for direct TLS requirement
        max_concurrent_transfers: Maximum concurrent transfers
        storage_path: Base path for file storage

    Example:
        >>> # Send file
        >>> transfer_id = await manager.send_file(
        ...     node_id="dpc-node-alice-123",
        ...     file_path=Path("/path/to/file.pdf"),
        ...     progress_callback=lambda id, current, total: print(f"{current}/{total}")
        ... )
        >>>
        >>> # Receive file (automatic via message handlers)
        >>> # User prompted when FILE_OFFER received
    """

    def __init__(
        self,
        p2p_manager: "P2PManager",
        firewall: "ContextFirewall",
        settings: "Settings",
        local_api=None,
        storage_base_path: Optional[Path] = None,
        service=None
    ):
        """
        Initialize file transfer manager.

        Args:
            p2p_manager: P2P manager for sending messages
            firewall: Firewall for permission checks
            settings: Settings for configuration
            local_api: LocalApiServer for broadcasting events to UI (optional)
            storage_base_path: Base path for file storage (default: ~/.dpc)
            service: CoreService instance for accessing peer_metadata (optional)
        """
        self.p2p_manager = p2p_manager
        self.firewall = firewall
        self.settings = settings
        self.local_api = local_api
        self.service = service

        # Active transfers: transfer_id -> FileTransfer
        self.active_transfers: Dict[str, FileTransfer] = _TransferTable(self._release_transfer)
        # Files an upload is reading or a fan-out is about to offer: path -> holders.
        # A screenshot that is not kept is deleted only when nothing holds it.
        self._files_in_use: Dict[Path, int] = {}
        # Group files this node sent or received, by `group_file_keys`, in the
        # order seen (a dict, so the oldest half can go). Memory only: after a
        # restart a copy may be taken again, and its record still lands once.
        self._group_files_held: Dict[str, None] = {}
        self._cancels_sent: Dict[str, tuple] = {}  # transfer_id -> (when, reason)
        self._stray_chunk_ids: Dict[str, float] = {}  # transfer_id -> when first met

        # Configuration (from settings)
        self.chunk_size = int(settings.get("file_transfer", "chunk_size", "65536"))  # 64KB
        self.chunk_delay = float(settings.get("file_transfer", "chunk_delay", "0.001"))  # 1ms (v0.11.1)
        self.background_threshold_mb = int(settings.get("file_transfer", "background_threshold_mb", "50"))
        self.direct_tls_threshold_mb = int(settings.get("file_transfer", "direct_tls_only_threshold_mb", "100"))
        self.max_concurrent_transfers = int(settings.get("file_transfer", "max_concurrent_transfers", "3"))
        self.verify_hash = settings.get("file_transfer", "verify_hash", "true").lower() in ("true", "1", "yes")

        # Storage path: ~/.dpc/conversations/{peer_id}/files/
        if storage_base_path is None:
            storage_base_path = Path.home() / ".dpc"
        self.storage_base_path = storage_base_path / "conversations"
        self.storage_base_path.mkdir(parents=True, exist_ok=True)

        logger.info(f"FileTransferManager initialized (chunk_size={self.chunk_size}, chunk_delay={self.chunk_delay}s, max_concurrent={self.max_concurrent_transfers})")

    def _get_peer_storage_path(self, node_id: str, subdir: str = "files") -> Path:
        """Get storage path for peer's files."""
        path = self.storage_base_path / node_id / subdir
        path.mkdir(parents=True, exist_ok=True)
        return path

    # Kept as insurance for older Windows, not because the hazard was observed.
    # The claim first written here — that Windows resolves these to devices
    # whatever the directory, so a file named CON vanishes into one — does not
    # hold on Windows 11 26200: `CON`, `CON.txt`, `NUL.txt` and `aux .log` were
    # each created as ordinary files and listed by `dir` (checked 2026-08-07,
    # three ways). The guard costs one underscore on a name nobody sends by
    # accident, so it stays; the reasoning is corrected rather than the code.
    _RESERVED_NAMES = frozenset(
        ["CON", "PRN", "AUX", "NUL"]
        + [f"COM{i}" for i in range(1, 10)]
        + [f"LPT{i}" for i in range(1, 10)]
    )

    @staticmethod
    def _safe_incoming_name(filename: str) -> str:
        """A peer sends a name. Everything that makes it a path is removed here.

        The old code replaced `/` and nothing else, which left three ways out of
        the peer's directory on Windows: a backslash still separates
        (`..\\..\\node.key`), an absolute path replaces the base outright
        (`Path(base) / "C:/x"` is `C:\\x`), and a drive-relative prefix (`C:x`)
        resolves against that drive's working directory. A colon also opens an
        NTFS alternate stream. Both separators are cut before taking the last
        segment, so neither platform's convention survives the trip.
        """
        name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        name = name.replace(":", "_").strip()
        if not name.strip(". "):
            # "", ".", "..", "..." — a name that would address a directory.
            return "received_file"
        if name.split(".")[0].upper() in FileTransferManager._RESERVED_NAMES:
            name = "_" + name
        return name

    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash of file."""
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            while chunk := f.read(self.chunk_size):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _compute_chunk_hashes(self, file_path: Path) -> List[str]:
        """
        Compute CRC32 hash for each chunk (v0.11.1).

        Returns list of CRC32 hashes in hex format (8 chars each).
        This enables per-chunk integrity verification without waiting
        for the entire file transfer to complete.

        Args:
            file_path: Path to file

        Returns:
            List of CRC32 hashes as hex strings (e.g., ["a1b2c3d4", "e5f6a7b8", ...])
        """
        chunk_hashes = []
        with open(file_path, 'rb') as f:
            while chunk := f.read(self.chunk_size):
                crc = zlib.crc32(chunk) & 0xffffffff  # Ensure unsigned 32-bit
                chunk_hashes.append(f"{crc:08x}")  # 8-char hex string
        return chunk_hashes

    async def _emit_preparation_progress(self, filename: str, phase: str, percent: int, **kwargs):
        """
        Emit file preparation progress event to UI.

        Args:
            filename: Name of file being prepared
            phase: "hashing_file" or "hashing_chunks"
            percent: Progress percentage (0-100)
            **kwargs: Additional metadata (bytes_processed, chunks_processed, etc.)
        """
        if self.local_api:
            await self.local_api.broadcast_event("file_preparation_progress", {
                "filename": filename,
                "phase": phase,
                "percent": percent,
                **kwargs
            })
            logger.debug(f"File prep progress: {filename} - {phase} {percent}%")

    async def _hash_in_executor(
        self, file_path: Path, phase: str, with_sha256: bool, with_chunk_crcs: bool
    ) -> Tuple[Optional[str], List[str]]:
        """Run `hash_file_in_one_pass` in the thread pool, relaying throttled progress to the UI."""
        loop = asyncio.get_running_loop()

        def _relay_progress(bytes_done: int, total_size: int):
            percent = 100 if total_size <= 0 else min(int(bytes_done * 100 / total_size), 100)
            # Schedule on the event loop; the worker thread must not wait for the broadcast
            asyncio.run_coroutine_threadsafe(
                self._emit_preparation_progress(
                    file_path.name, phase, percent,
                    bytes_processed=bytes_done, total_bytes=total_size
                ),
                loop
            )

        return await loop.run_in_executor(
            None,
            lambda: hash_file_in_one_pass(
                file_path, self.chunk_size,
                with_sha256=with_sha256, with_chunk_crcs=with_chunk_crcs,
                on_progress=_relay_progress,
            ),
        )

    async def _compute_file_hash_async(self, file_path: Path) -> str:
        """
        Compute SHA256 hash of file asynchronously with throttled progress reporting.

        Emits:
            file_preparation_progress with phase="hashing_file" (at most 2 a second, plus a final 100%)
        """
        file_hash, _ = await self._hash_in_executor(
            file_path, "hashing_file", with_sha256=True, with_chunk_crcs=False
        )
        return file_hash

    async def compute_file_fingerprint(self, file_path: Path) -> Tuple[str, List[str]]:
        """
        Compute (file hash, per-chunk CRC32 list) of a file to send, in ONE read of the file.

        The hash is "none" when hash verification is off. Emits file_preparation_started /
        file_preparation_progress / file_preparation_completed. A caller that sends the same
        file to several peers calls this once and passes the pair to `send_file`.
        """
        file_size = file_path.stat().st_size
        if self.local_api:
            await self.local_api.broadcast_event("file_preparation_started", {
                "filename": file_path.name,
                "size_bytes": file_size,
                "size_mb": round(file_size / (1024 * 1024), 2)
            })

        # With the SHA-256 on, the one pass is reported as "hashing_file"; without it only the
        # chunk CRCs are computed ("hashing_chunks", which the UI shows as "Preparing file").
        file_hash, chunk_hashes = await self._hash_in_executor(
            file_path,
            "hashing_file" if self.verify_hash else "hashing_chunks",
            with_sha256=self.verify_hash,
            with_chunk_crcs=True,
        )
        if file_hash is None:
            file_hash = "none"

        if self.local_api:
            await self.local_api.broadcast_event("file_preparation_completed", {
                "filename": file_path.name,
                "hash": file_hash,
                "total_chunks": len(chunk_hashes)
            })
        return file_hash, chunk_hashes

    async def send_file(
        self,
        node_id: str,
        file_path: Path,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        image_metadata: Optional[Dict[str, Any]] = None,
        voice_metadata: Optional[Dict[str, Any]] = None,
        group_id: Optional[str] = None,
        file_hash: Optional[str] = None,
        chunk_hashes: Optional[List[str]] = None,
        message_id: Optional[str] = None,
        sender_node_id: Optional[str] = None,
        group_relay: bool = False,
    ) -> str:
        """
        Initiate file transfer to peer.

        Args:
            node_id: Target peer node ID
            file_path: Path to file to send
            progress_callback: Optional callback(transfer_id, chunks_sent, total_chunks)
            image_metadata: Optional image metadata (for image transfers only):
                - dimensions: {width: int, height: int}
                - thumbnail_base64: str (data URL)
                - source: str (e.g., "clipboard")
                - captured_at: str (ISO timestamp)
            voice_metadata: Optional voice metadata (for voice transfers only):
                - duration_seconds: float (recording duration)
                - sample_rate: int (e.g., 48000)
                - channels: int (e.g., 1 for mono)
                - codec: str (e.g., "opus")
                - recorded_at: str (ISO timestamp)
            group_id: Optional group ID (group file transfers)
            file_hash, chunk_hashes: Optional precomputed pair from `compute_file_fingerprint`.
                A fan-out to N members hashes the file once and passes the pair to each call;
                both must be given, otherwise the file is hashed here.
            message_id, sender_node_id: A group file's identity (see `group_file_keys`).
                The author passes its own message_id and leaves sender_node_id to
                default to itself; a relay passes both as it received them.
            group_relay: This upload passes on a group file received from someone else.

        Returns:
            transfer_id: Unique transfer identifier

        Raises:
            FileNotFoundError: If file doesn't exist
            PermissionError: If firewall denies file transfer
            MaxConcurrentTransfersExceeded: (a RuntimeError) if every upload slot is taken
        """
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Check concurrent transfer limit
        active_uploads = sum(1 for t in self.active_transfers.values() if t.direction == "upload")
        if active_uploads >= self.max_concurrent_transfers:
            raise MaxConcurrentTransfersExceeded(self.max_concurrent_transfers, self.open_uploads())

        # Receiver will check firewall permission upon FILE_OFFER receipt (receiver-only control)
        # Sender-side permission check removed in v0.13.3 - receiver has full control
        # See: file_offer_handler.py:107-123 for receiver-side permission enforcement

        # Compute file metadata
        file_size = file_path.stat().st_size

        precomputed = file_hash is not None and chunk_hashes is not None
        expected_chunks = (file_size + self.chunk_size - 1) // self.chunk_size
        if precomputed and len(chunk_hashes) != expected_chunks:
            logger.warning(
                f"Precomputed chunk hashes ({len(chunk_hashes)}) do not match {expected_chunks} chunks "
                f"of {file_path.name}; hashing again"
            )
            precomputed = False
        if not precomputed:
            # One read of the file yields the SHA-256 and the per-chunk CRC32 (v0.11.1)
            file_hash, chunk_hashes = await self.compute_file_fingerprint(file_path)

        # Detect MIME type (but override for voice messages to ensure audio/webm)
        if voice_metadata:
            # Force audio MIME type for voice messages (.webm can be detected as video/webm)
            mime_type = "audio/webm"
        else:
            mime_type = self._detect_mime_type(file_path)
        total_chunks = (file_size + self.chunk_size - 1) // self.chunk_size

        logger.info(f"Computed {len(chunk_hashes)} chunk hashes ({len(chunk_hashes) * 8} bytes) for {file_path.name}")

        # Create transfer session
        transfer_id = str(uuid.uuid4())
        transfer = FileTransfer(
            transfer_id=transfer_id,
            filename=file_path.name,
            size_bytes=file_size,
            hash=file_hash,
            mime_type=mime_type,
            chunk_size=self.chunk_size,
            node_id=node_id,
            direction="upload",
            status=TransferStatus.PENDING,
            chunks_received=set(),
            total_chunks=total_chunks,
            file_path=file_path,
            progress_callback=progress_callback,
            chunk_hashes=chunk_hashes,  # v0.11.1: Store for retry requests
            image_metadata=image_metadata,  # Store image metadata for screenshots (v0.12.0+)
            voice_metadata=voice_metadata,  # Store voice metadata for voice messages (v0.13.0+)
            group_id=group_id,             # Group ID for group file transfers (v0.19.0+)
            group_relay=group_relay,
        )
        if group_id and message_id:
            transfer.message_id = message_id
            transfer.sender_node_id = sender_node_id or self.p2p_manager.node_id
        transfer.save_to_disk = self._saves_to_disk(transfer)
        self.active_transfers[transfer_id] = transfer
        if group_id:
            # Before the offer leaves: a copy relayed back here is then refused.
            self._remember_group_file(group_id, transfer.message_id, file_hash)

        # Send FILE_OFFER (with optional image_metadata for v0.12.0+ or voice_metadata for v0.13.0+)
        payload = {
            "transfer_id": transfer_id,
            "filename": transfer.filename,
            "size_bytes": file_size,
            "hash": file_hash,
            "mime_type": mime_type,
            "chunk_size": self.chunk_size,
            "chunk_hashes": chunk_hashes  # v0.11.1: CRC32 per chunk for integrity
        }

        # Add image metadata if provided (v0.12.0+: Vision + P2P Image Transfer)
        if image_metadata:
            payload["image_metadata"] = image_metadata

        # Add voice metadata if provided (v0.13.0+: Voice Messages)
        if voice_metadata:
            payload["voice_metadata"] = voice_metadata

        # Add group_id if provided (v0.19.0+: Group file transfers)
        if group_id:
            payload["group_id"] = group_id
            if transfer.message_id:
                payload["message_id"] = transfer.message_id
                payload["sender_node_id"] = transfer.sender_node_id

        await self.p2p_manager.send_message_to_peer(node_id, {
            "command": "FILE_OFFER",
            "payload": payload
        })

        logger.info(f"File transfer initiated: {file_path.name} ({file_size} bytes) to {node_id}")
        self._start_answer_timer(transfer, UNANSWERED_UPLOAD_RELEASE_S, self._release_unanswered_upload)
        return transfer_id

    def peer_label(self, node_id: str) -> Optional[str]:
        """The name this node knows `node_id` by, if it has one."""
        meta = getattr(self.service, "peer_metadata", None) or {}
        entry = meta.get(node_id) or {}
        return entry.get("name") or None

    def open_uploads(self) -> List[Dict[str, Any]]:
        """Every upload that holds a slot: to whom, what, and `pending` (waiting
        for the receiver) or `transferring` (sending)."""
        return [
            {
                "node_id": t.node_id,
                "name": self.peer_label(t.node_id),
                "filename": t.filename,
                "status": t.status.value,
            }
            for t in self.active_transfers.values() if t.direction == "upload"
        ]

    _MAX_GROUP_FILES_HELD = 10000

    def _remember_group_file(self, group_id: str, message_id: Optional[str],
                             file_hash: Optional[str]) -> None:
        """Note that this node has this group file, under both of its keys.

        Both, so a copy that lost its `message_id` on the way (an older relay)
        is still known by its hash.
        """
        for key in group_file_keys(group_id, message_id, file_hash):
            if key:
                self._group_files_held[key] = None
        if len(self._group_files_held) > self._MAX_GROUP_FILES_HELD:
            for key in list(self._group_files_held)[:self._MAX_GROUP_FILES_HELD // 2]:
                del self._group_files_held[key]

    @staticmethod
    def _offer_key(group_id: str, message_id: Optional[str], file_hash: Optional[str]) -> Optional[str]:
        """The key an offer is judged by: its message_id when it has one, else its hash.

        Not both: a member who sends the same bytes again sends a new message,
        and a hash match must not swallow it.
        """
        by_id, by_hash = group_file_keys(group_id, message_id, file_hash)
        return by_id or by_hash

    def holds_group_file(self, group_id: str, message_id: Optional[str],
                         sender_node_id: Optional[str], file_hash: Optional[str]) -> bool:
        """Whether a group file offer is one this node already has.

        True when this node wrote it, sent or received it under the same key,
        or is receiving it now. A false claim in the offer can only make this
        node refuse a copy, never credit one: the claim is unsigned.
        """
        if sender_node_id and sender_node_id == self.p2p_manager.node_id:
            return True
        key = self._offer_key(group_id, message_id, file_hash)
        if not key:
            return False
        if key in self._group_files_held:
            return True
        for t in self.active_transfers.values():
            if (t.direction == "download" and t.group_id == group_id
                    and key in group_file_keys(t.group_id, t.message_id, t.hash)):
                return True
        return False

    async def handle_file_accept(self, node_id: str, transfer_id: str):
        """
        Handle FILE_ACCEPT from peer - start sending chunks.

        Args:
            node_id: Peer node ID
            transfer_id: Transfer identifier
        """
        transfer = self.active_transfers.get(transfer_id)
        if not transfer or transfer.direction != "upload":
            logger.warning(f"FILE_ACCEPT for unknown upload: {transfer_id}")
            return

        transfer.status = TransferStatus.TRANSFERRING
        self._stop_answer_timer(transfer)
        logger.info(f"FILE_ACCEPT received, starting chunk transfer: {transfer.filename}")

        # Broadcast file transfer started event to UI
        if self.local_api:
            await self.local_api.broadcast_event("file_transfer_started", {
                "transfer_id": transfer_id,
                "node_id": node_id,
                "filename": transfer.filename,
                "size_bytes": transfer.file_path.stat().st_size,
                "direction": "upload",
                "total_chunks": transfer.total_chunks
            })

        # Send chunks. The file is held until it is closed: on Windows a
        # FILE_COMPLETE that arrives during the last chunk's pause could not
        # delete it, and the error took the sender's own message with it.
        try:
            with self.using_file(transfer.file_path), open(transfer.file_path, 'rb') as f:
                for chunk_index in range(transfer.total_chunks):
                    # A cancel (the peer's, or ours) ends the send; the peer knows already
                    if (self.active_transfers.get(transfer_id) is not transfer
                            or transfer.status in (TransferStatus.CANCELLED, TransferStatus.FAILED)):
                        logger.info(
                            f"stopped sending {transfer.filename} at chunk "
                            f"{chunk_index}/{transfer.total_chunks}: {transfer.status.value}"
                        )
                        break

                    # Read chunk
                    chunk_data = f.read(self.chunk_size)
                    chunk_base64 = base64.b64encode(chunk_data).decode('utf-8')

                    # Send FILE_CHUNK
                    await self.p2p_manager.send_message_to_peer(node_id, {
                        "command": "FILE_CHUNK",
                        "payload": {
                            "transfer_id": transfer_id,
                            "chunk_index": chunk_index,
                            "total_chunks": transfer.total_chunks,
                            "data": chunk_base64
                        }
                    })

                    # Progress callback
                    if transfer.progress_callback:
                        transfer.progress_callback(transfer_id, chunk_index + 1, transfer.total_chunks)

                    # Broadcast progress to UI (every 10 chunks to avoid spam)
                    if self.local_api and (chunk_index % 10 == 0 or chunk_index == transfer.total_chunks - 1):
                        await self.local_api.broadcast_event("file_transfer_progress", {
                            "transfer_id": transfer_id,
                            "node_id": node_id,
                            "filename": transfer.filename,
                            "direction": "upload",
                            "chunks_sent": chunk_index + 1,
                            "total_chunks": transfer.total_chunks,
                            "progress_percent": int(((chunk_index + 1) / transfer.total_chunks) * 100)
                        })

                    # Configurable delay to control transfer speed (v0.11.1)
                    await asyncio.sleep(self.chunk_delay)
                else:
                    logger.info(f"All chunks sent for {transfer.filename}")

        except Exception as e:
            logger.error(f"Error sending chunks: {e}", exc_info=True)
            transfer.status = TransferStatus.FAILED
            await self._send_file_cancel(node_id, transfer_id, "send_error")
        finally:
            # A completion that came while the file was open left the delete to us
            self._release_upload(transfer)

    # --- what stays on disk ------------------------------------------------

    @staticmethod
    def _is_screenshot(transfer: FileTransfer) -> bool:
        return bool(transfer.mime_type and transfer.mime_type.startswith("image/")
                    and transfer.image_metadata is not None)

    def _saves_to_disk(self, transfer: FileTransfer) -> bool:
        """False for a screenshot when the owner turned save_screenshots_to_disk off."""
        if not self._is_screenshot(transfer) or not self.firewall:
            return True
        return saves_screenshots_to_disk(self.firewall)

    class _FileHold:
        def __init__(self, manager: "FileTransferManager", path: Optional[Path]):
            self.manager, self.path = manager, path

        def __enter__(self):
            if self.path is not None:
                held = self.manager._files_in_use
                held[self.path] = held.get(self.path, 0) + 1
            return self

        def __exit__(self, *exc):
            if self.path is not None:
                held = self.manager._files_in_use
                left = held.get(self.path, 1) - 1
                if left > 0:
                    held[self.path] = left
                else:
                    held.pop(self.path, None)
            return False

    def using_file(self, path: Optional[Path]) -> "FileTransferManager._FileHold":
        """Hold `path` so an unsaved screenshot is not deleted under a reader."""
        return self._FileHold(self, path)

    def delete_unsaved_screenshot(self, path: Optional[Path]) -> bool:
        """Delete a screenshot that is not kept, once no upload of it needs it.

        Called by whichever user of the file finishes last: a transfer leaving
        `active_transfers`, the chunk loop closing it, a fan-out that offered
        it to nobody. Returns True when the file is gone.
        """
        if path is None:
            return False
        if self._files_in_use.get(path):
            return False
        if any(t.direction == "upload" and t.file_path == path
               for t in list(self.active_transfers.values())):
            return False
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not delete unsaved screenshot %s: %s", path, e)
            return False
        logger.info(f"Deleted screenshot after upload (save_screenshots_to_disk=false): {path}")
        return True

    def _release_upload(self, transfer: Optional[FileTransfer]) -> None:
        if transfer is None or getattr(transfer, "direction", None) != "upload":
            return
        if transfer.save_to_disk is False:
            self.delete_unsaved_screenshot(transfer.file_path)

    def _release_transfer(self, transfer: Optional[FileTransfer]) -> None:
        """Whatever a transfer leaving `active_transfers` held, by direction."""
        self._stop_answer_timer(transfer)
        self._release_receive(transfer)
        self._release_upload(transfer)

    # --- offers nobody has answered ------------------------------------------

    def _stop_answer_timer(self, transfer: Optional[FileTransfer]) -> None:
        if transfer is None:
            return
        task = transfer.answer_timer
        transfer.answer_timer = None
        if task is None or task.done():
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current:
            task.cancel()

    def _start_answer_timer(self, transfer: FileTransfer, seconds: float, on_silence) -> None:
        """Run `on_silence(transfer)` if `transfer` is still unanswered after `seconds`."""
        self._stop_answer_timer(transfer)
        try:
            transfer.answer_timer = asyncio.get_running_loop().create_task(
                self._answer_clock(transfer, seconds, on_silence)
            )
        except RuntimeError:
            logger.debug("No running loop: the answer clock of %s is not started", transfer.transfer_id)

    def _still_unanswered(self, transfer: FileTransfer) -> bool:
        """The transfer is in the table, pending, and nobody has accepted it."""
        if self.active_transfers.get(transfer.transfer_id) is not transfer:
            return False
        if transfer.status != TransferStatus.PENDING:
            return False
        return transfer.direction != "download" or transfer.stall_watch is None

    async def _answer_clock(self, transfer: FileTransfer, seconds: float, on_silence) -> None:
        try:
            await _answer_sleep(seconds)
            if self._still_unanswered(transfer):
                await on_silence(transfer)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 - a clock must not take the loop down
            logger.error("Answer clock of %s failed: %s", transfer.transfer_id, e, exc_info=True)

    def begin_waiting_for_answer(self, transfer_id: str) -> None:
        """Give a download that waits for a person its deadline (idempotent).

        Called for every offer that is neither refused nor accepted on arrival:
        the question "allow files from X?" and the accept button of an ordinary
        file. After OFFER_ANSWER_DEADLINE_S with no answer the receiver refuses
        with NO_ANSWER_REASON, so the sender's slot is not held by a person who
        walked away. An answer (`mark_answered`, or the accept that starts the
        receive) stops the clock.
        """
        transfer = self.active_transfers.get(transfer_id)
        if transfer is None or transfer.direction != "download":
            return
        if transfer.answer_timer is not None and not transfer.answer_timer.done():
            return
        self._start_answer_timer(transfer, OFFER_ANSWER_DEADLINE_S, self._expire_unanswered_offer)

    def mark_answered(self, transfer: Optional[FileTransfer]) -> None:
        """The person has answered: stop the deadline before the answer is acted on."""
        self._stop_answer_timer(transfer)

    async def _expire_unanswered_offer(self, transfer: FileTransfer) -> None:
        """The receiver's deadline ran out: refuse, tell the sender and the UI, drop the offer."""
        logger.info(
            "Offer %s (%s) from %s went unanswered for %.0f s; refusing it as %s",
            transfer.transfer_id, transfer.filename, transfer.node_id,
            OFFER_ANSWER_DEADLINE_S, NO_ANSWER_REASON,
        )
        await self._fail_download(
            transfer.node_id, transfer, NO_ANSWER_REASON, status=TransferStatus.CANCELLED
        )

    async def _release_unanswered_upload(self, transfer: FileTransfer) -> None:
        """The sender's release: no FILE_ACCEPT and no FILE_CANCEL within
        UNANSWERED_UPLOAD_RELEASE_S. Free the slot, tell the receiver (an older
        one may still be asking its person) and the UI."""
        logger.warning(
            "Upload %s (%s) to %s had no answer for %.0f s; releasing its slot",
            transfer.transfer_id, transfer.filename, transfer.node_id,
            UNANSWERED_UPLOAD_RELEASE_S,
        )
        transfer.status = TransferStatus.CANCELLED
        await self._send_file_cancel(transfer.node_id, transfer.transfer_id, NO_ANSWER_REASON)
        if self.local_api:
            event = {
                "transfer_id": transfer.transfer_id,
                "node_id": transfer.node_id,
                "filename": transfer.filename,
                "direction": transfer.direction,
                "reason": NO_ANSWER_REASON,
                "status": "cancelled",
            }
            if transfer.group_id:
                event["group_id"] = transfer.group_id
            await self.local_api.broadcast_event("file_transfer_cancelled", event)
        if self.active_transfers.get(transfer.transfer_id) is transfer:
            del self.active_transfers[transfer.transfer_id]  # frees the slot

    # --- receiving ---------------------------------------------------------

    def _incoming_destination(self, node_id: str, transfer: FileTransfer) -> Tuple[Path, bool]:
        """Where a download lands, and whether it is written to disk at all."""
        is_image = self._is_screenshot(transfer)
        subdir = "files/screenshots" if is_image else "files"

        if transfer.group_id:
            if self.service and hasattr(self.service, '_get_or_create_conversation_monitor'):
                group_monitor = self.service._get_or_create_conversation_monitor(transfer.group_id)
                storage_path = group_monitor._get_conversation_dir() / subdir
            else:
                storage_path = self.storage_base_path / transfer.group_id / subdir
            storage_path.mkdir(parents=True, exist_ok=True)
        else:
            storage_path = self._get_peer_storage_path(node_id, subdir)

        file_path = storage_path / self._safe_incoming_name(transfer.filename)

        return file_path, self._saves_to_disk(transfer)

    @staticmethod
    def _receive_path_for(file_path: Path, transfer_id: str) -> Path:
        """The temporary file beside the final one, unique per transfer.

        Two transfers of one name (a group file from its sender and from a
        relaying member) would otherwise write into one file, and on Windows
        the first rename fails while the other still holds it open. The id is
        the peer's, so it is hashed rather than put into the path.
        """
        tag = hashlib.sha256(str(transfer_id).encode()).hexdigest()[:12]
        return file_path.with_name(f"{file_path.name}.{tag}.tmp")

    def _open_receive(self, node_id: str, transfer: FileTransfer) -> None:
        """Decide the destination and open the temporary file, sized to the offer.

        Sizing it up front makes a disk that cannot hold the file fail here on
        NTFS (on ext4 the file is sparse and a full disk fails at a write).
        """
        file_path, save_to_disk = self._incoming_destination(node_id, transfer)
        transfer.destination = file_path
        transfer.save_to_disk = save_to_disk
        if not save_to_disk:
            return
        receive_path = self._receive_path_for(file_path, transfer.transfer_id)
        transfer.receive_path = receive_path
        handle = open(receive_path, "w+b")
        try:
            handle.truncate(max(0, int(transfer.size_bytes or 0)))
        except Exception:
            handle.close()
            raise
        transfer.receive_file = handle

    def _close_receive_file(self, transfer: FileTransfer) -> None:
        handle = transfer.receive_file
        transfer.receive_file = None
        if handle is not None:
            handle.close()

    def _release_receive(self, transfer: Optional[FileTransfer]) -> None:
        """Free what a download holds: the stall watch, the handle, the temporary file.

        Called whenever a transfer leaves `active_transfers`. After a completed
        download the temporary file has already been renamed and `receive_path`
        is None, so only an unfinished one is deleted.
        """
        if transfer is None or getattr(transfer, "direction", None) != "download":
            return
        self._stop_stall_watch(transfer)
        try:
            self._close_receive_file(transfer)
        except OSError as e:
            logger.warning("Closing the partial file of %s failed: %s", transfer.transfer_id, e)
        receive_path = transfer.receive_path
        transfer.receive_path = None
        if receive_path is not None:
            try:
                receive_path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Could not delete partial file %s: %s", receive_path, e)

    def _stop_stall_watch(self, transfer: FileTransfer) -> None:
        task = transfer.stall_watch
        transfer.stall_watch = None
        if task is None or task.done():
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current:
            task.cancel()

    def begin_receiving(self, transfer_id: str) -> None:
        """Start the stall watch of an accepted download (idempotent).

        Called when FILE_ACCEPT goes out, so a sender that never starts is
        noticed too; the first chunk starts it as well when nothing did.
        """
        transfer = self.active_transfers.get(transfer_id)
        if transfer is None or transfer.direction != "download":
            return
        self._stop_answer_timer(transfer)  # accepted: no deadline any more
        if transfer.stall_watch is not None and not transfer.stall_watch.done():
            return
        transfer.quiet_since = _monotonic()
        transfer.stall_watch = asyncio.create_task(
            self._watch_receive_stall(transfer.node_id, transfer)
        )

    def _missing_indices(self, transfer: FileTransfer) -> List[int]:
        received = transfer.chunks_received
        return [i for i in range(transfer.total_chunks) if i not in received]

    async def _reask_missing(self, node_id: str, transfer: FileTransfer, missing: List[int]) -> None:
        batch = missing[:RECEIVE_REASK_BATCH]
        transfer.reasked = set(batch)
        transfer.quiet_since = _monotonic()
        logger.info(
            "Re-asking %d of %d missing chunks of %s (round %d/%d)",
            len(batch), len(missing), transfer.filename, transfer.stall_rounds, RECEIVE_STALL_ROUNDS,
        )
        for index in batch:
            await self._send_chunk_retry_request(node_id, transfer.transfer_id, index)

    async def _watch_receive_stall(self, node_id: str, transfer: FileTransfer) -> None:
        """Re-ask what has not arrived after a silence; give up after bounded rounds."""
        tid = transfer.transfer_id
        try:
            while self.active_transfers.get(tid) is transfer and not transfer.finalizing:
                idle = _monotonic() - transfer.quiet_since
                if idle < RECEIVE_STALL_TIMEOUT_S:
                    await asyncio.sleep(RECEIVE_STALL_TIMEOUT_S - idle)
                    continue
                missing = self._missing_indices(transfer)
                if not missing:
                    return
                if transfer.stall_rounds >= RECEIVE_STALL_ROUNDS:
                    logger.error(
                        "Giving up on %s: %d chunks still missing after %d re-ask rounds",
                        transfer.filename, len(missing), transfer.stall_rounds,
                    )
                    await self._fail_download(node_id, transfer, "missing_chunks")
                    return
                transfer.stall_rounds += 1
                await self._reask_missing(node_id, transfer, missing)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Stall watch of %s failed: %s", tid, e, exc_info=True)

    async def _fail_download(self, node_id: str, transfer: FileTransfer, reason: str,
                             status: TransferStatus = TransferStatus.FAILED) -> None:
        """End a download (FAILED unless `status` says otherwise): tell the sender,
        tell the UI, free the state."""
        transfer.status = status
        await self._send_file_cancel(node_id, transfer.transfer_id, reason)
        if self.local_api:
            event = {
                "transfer_id": transfer.transfer_id,
                "node_id": node_id,
                "filename": transfer.filename,
                "direction": transfer.direction,
                "reason": reason,
                "status": "cancelled"
            }
            if transfer.group_id:
                event["group_id"] = transfer.group_id
            await self.local_api.broadcast_event("file_transfer_cancelled", event)
        if self.active_transfers.get(transfer.transfer_id) is transfer:
            del self.active_transfers[transfer.transfer_id]  # releases the receive
        else:
            self._release_receive(transfer)

    async def handle_file_chunk(self, node_id: str, payload: dict):
        """
        Handle incoming FILE_CHUNK: verify it and write it at its offset on disk.

        v0.11.1: Added per-chunk CRC32 verification with retry tracking.

        Args:
            node_id: Sender node ID
            payload: FILE_CHUNK payload
        """
        transfer_id = payload["transfer_id"]
        chunk_index = payload["chunk_index"]
        chunk_data = base64.b64decode(payload["data"])

        transfer = self.active_transfers.get(transfer_id)
        if not transfer or transfer.direction != "download":
            await self._stray_chunk(node_id, transfer_id)
            return
        if transfer.finalizing:
            return
        if not isinstance(chunk_index, int) or not 0 <= chunk_index < transfer.total_chunks:
            logger.warning(f"FILE_CHUNK index {chunk_index!r} out of range for {transfer_id}")
            return

        transfer.quiet_since = _monotonic()
        if transfer.stall_watch is None:
            self.begin_receiving(transfer_id)
        if chunk_index in transfer.chunks_received:
            return  # a duplicate (a re-ask crossed the original); it is already on disk
        if transfer.chunks_failed is None:
            transfer.chunks_failed = set()
        if transfer.retry_count is None:
            transfer.retry_count = {}

        # A chunk is chunk_size long except the last, from the offer's numbers
        expected_len = min(transfer.chunk_size, transfer.size_bytes - chunk_index * transfer.chunk_size)

        # v0.11.1: Verify chunk CRC32 if hashes provided
        chunk_verified = len(chunk_data) == expected_len
        if chunk_verified and transfer.chunk_hashes and chunk_index < len(transfer.chunk_hashes):
            expected_hash = transfer.chunk_hashes[chunk_index]
            computed_crc = zlib.crc32(chunk_data) & 0xffffffff
            chunk_verified = f"{computed_crc:08x}" == expected_hash

        if not chunk_verified:
            transfer.chunks_failed.add(chunk_index)
            transfer.retry_count[chunk_index] = transfer.retry_count.get(chunk_index, 0) + 1
            logger.warning(
                f"Chunk {chunk_index} failed verification ({len(chunk_data)} bytes, "
                f"{expected_len} expected). "
                f"Retry attempt {transfer.retry_count[chunk_index]}/{transfer.max_retries}"
            )
            if transfer.retry_count[chunk_index] >= transfer.max_retries:
                logger.error(f"Chunk {chunk_index} failed {transfer.max_retries} times, aborting transfer")
                await self._fail_download(node_id, transfer, "chunk_verification_failed")
            else:
                await self._send_chunk_retry_request(node_id, transfer_id, chunk_index)
            return  # Don't process this chunk further, wait for retry

        if chunk_index in transfer.chunks_failed:
            transfer.chunks_failed.discard(chunk_index)
            logger.info(f"Chunk {chunk_index} verified successfully on retry")

        # Straight to disk at its offset; no await between seek and write, so
        # chunks handled in concurrent tasks cannot interleave here.
        try:
            if transfer.save_to_disk is None:
                self._open_receive(node_id, transfer)
            if transfer.receive_file is not None:
                transfer.receive_file.seek(chunk_index * transfer.chunk_size)
                transfer.receive_file.write(chunk_data)
        except OSError as e:
            logger.error(f"Cannot write {transfer.filename} to disk: {e}")
            await self._fail_download(node_id, transfer, RECEIVE_ERROR_REASON)
            return

        transfer.chunks_received.add(chunk_index)
        transfer.stall_rounds = 0

        # Progress callback
        if transfer.progress_callback:
            transfer.progress_callback(transfer_id, len(transfer.chunks_received), transfer.total_chunks)

        # Check if complete (all chunks received and verified)
        if len(transfer.chunks_received) == transfer.total_chunks:
            await self._finalize_download(node_id, transfer)
            return

        # A re-asked batch that has all come in: ask the next one now instead
        # of waiting out another silence.
        if transfer.reasked:
            transfer.reasked.discard(chunk_index)
            if not transfer.reasked:
                missing = self._missing_indices(transfer)
                if missing:
                    await self._reask_missing(node_id, transfer, missing)

    async def _stray_chunk(self, node_id: str, transfer_id: str) -> None:
        """A FILE_CHUNK for a download we do not hold.

        The first one per id and window is logged; when we cancelled the id
        ourselves the sender is told again with the reason we sent. The rest
        are dropped at DEBUG. An id we never sent a FILE_CANCEL for gets no
        answer: no reason word in DPTP §3.11 says "unknown transfer".
        """
        now = _monotonic()
        for memory, when_of in ((self._cancels_sent, lambda v: v[0]), (self._stray_chunk_ids, lambda v: v)):
            for old in [k for k, v in memory.items() if now - when_of(v) > CANCEL_MEMORY_S]:
                del memory[old]
        if transfer_id in self._stray_chunk_ids:
            logger.debug(f"FILE_CHUNK for unknown download dropped: {transfer_id}")
            return
        if len(self._stray_chunk_ids) >= CANCEL_MEMORY_MAX:
            del self._stray_chunk_ids[next(iter(self._stray_chunk_ids))]
        self._stray_chunk_ids[transfer_id] = now
        sent = self._cancels_sent.get(transfer_id)
        if sent is None:
            logger.warning(f"FILE_CHUNK for unknown download: {transfer_id}")
            return
        reason = sent[1]
        logger.warning(f"FILE_CHUNK for a download cancelled here ({reason}): {transfer_id}, telling the sender again")
        await self._send_file_cancel(node_id, transfer_id, reason)

    async def _finalize_download(self, node_id: str, transfer: FileTransfer):
        """Finalize download: verify the hash of the file on disk, rename it into place."""
        logger.info(f"All chunks received for {transfer.filename}, finalizing...")
        transfer.finalizing = True
        self._stop_stall_watch(transfer)
        tid = transfer.transfer_id

        is_image = (transfer.mime_type and transfer.mime_type.startswith("image/")
                   and transfer.image_metadata is not None)
        # Assigned here rather than inside the `if self.local_api:` block below,
        # where it used to live: the transcription branch at the end of this
        # method reads it unconditionally, so a manager without a local API
        # raised UnboundLocalError after the file was already on disk.
        is_voice = transfer.voice_metadata is not None

        try:
            if transfer.save_to_disk is None:
                self._open_receive(node_id, transfer)  # no chunk ever came: an empty file
            self._close_receive_file(transfer)
        except OSError as e:
            logger.error(f"Cannot write {transfer.filename} to disk: {e}")
            await self._fail_download(node_id, transfer, RECEIVE_ERROR_REASON)
            return
        file_path = transfer.destination
        save_to_disk = transfer.save_to_disk

        if save_to_disk:
            temp_path = transfer.receive_path
            computed_hash = None
            if self.verify_hash and transfer.hash != "none":
                try:
                    computed_hash = await self._compute_file_hash_async(temp_path)
                except OSError as e:
                    if self.active_transfers.get(tid) is not transfer:
                        return  # cancelled while hashing; the release removed the file
                    logger.error(f"Cannot read back {temp_path}: {e}")
                    await self._fail_download(node_id, transfer, RECEIVE_ERROR_REASON)
                    return
                if self.active_transfers.get(tid) is not transfer:
                    temp_path.unlink(missing_ok=True)  # cancelled while hashing
                    return
                if computed_hash != transfer.hash:
                    logger.error(
                        f"Hash mismatch for {transfer.filename}: expected {transfer.hash}, "
                        f"got {computed_hash}; every chunk passed its CRC, the whole file is dropped"
                    )
                    await self._fail_download(node_id, transfer, "hash_mismatch")
                    return
            else:
                computed_hash = "none"

            try:
                os.replace(temp_path, file_path)
            except OSError as e:
                # On Windows a final file held open elsewhere cannot be replaced
                logger.error(f"Cannot move {temp_path.name} into place: {e}")
                await self._fail_download(node_id, transfer, RECEIVE_ERROR_REASON)
                return
            transfer.receive_path = None
            transfer.status = TransferStatus.COMPLETED
            logger.info(f"{'Screenshot' if is_image else 'File'} saved: {file_path}")
        else:
            computed_hash = "none"
            transfer.status = TransferStatus.COMPLETED
            logger.info(f"Screenshot received but not saved to disk (save_screenshots_to_disk=false): {transfer.filename}")

        if transfer.group_id:
            # From here a copy of this file offered again is `already_received`.
            self._remember_group_file(transfer.group_id, transfer.message_id, transfer.hash)

        # Send FILE_COMPLETE
        await self.p2p_manager.send_message_to_peer(node_id, {
            "command": "FILE_COMPLETE",
            "payload": {
                "transfer_id": transfer.transfer_id,
                "hash": computed_hash
            }
        })

        # Broadcast to UI as chat message (receiver side)
        if self.local_api:
            import time
            # A group file that carries its author's message_id is noted under
            # it: when the author's signed record of it arrives, the merge
            # finds this note by that id and puts the record in its place.
            message_id = transfer.message_id or hashlib.sha256(
                f"{node_id}:file-received:{transfer.transfer_id}:{int(time.time() * 1000)}".encode()
            ).hexdigest()[:16]

            # Group history sync may already have brought the sender's own
            # record of this file, pointing at the sender's disk. Then this
            # transfer completes that record rather than adding a second one
            # — the UI could render only ours, so one paste showed up twice
            # in the store and once on screen.
            existing_record = None
            monitor_key = transfer.group_id if transfer.group_id else node_id
            conversation_monitor = (
                self.service._get_or_create_conversation_monitor(monitor_key)
                if self.service and hasattr(self.service, "_get_or_create_conversation_monitor")
                else None
            )
            finder = getattr(conversation_monitor, "find_file_record", None)
            if transfer.group_id and callable(finder):
                # By the offer's message_id first: in a star the file arrives
                # from the relay, not its author, and only the id finds the
                # author's record. The record found is signed by whoever it
                # names, so the offer's unsigned claim credits nobody.
                existing_record = finder(node_id, transfer.filename, transfer.size_bytes,
                                         transfer.hash, message_id=transfer.message_id)
                if existing_record:
                    message_id = existing_record["id"]

            # Get sender name from peer metadata
            sender_name = node_id
            if self.service and hasattr(self.service, 'peer_metadata'):
                sender_name = self.service.peer_metadata.get(node_id, {}).get("name") or node_id

            size_mb = round(transfer.size_bytes / (1024 * 1024), 2)

            # Build attachment
            attachment = {
                "type": "image" if is_image else ("voice" if is_voice else "file"),
                "filename": transfer.filename,
                "size_bytes": transfer.size_bytes,
                "size_mb": size_mb,
                "hash": transfer.hash,
                "mime_type": transfer.mime_type,
                "transfer_id": transfer.transfer_id,
                "status": "completed"
            }

            # Add image-specific fields
            if is_image:
                # Only include file_path if file was actually saved to disk
                if save_to_disk:
                    attachment["file_path"] = str(file_path)
                if transfer.image_metadata:
                    attachment["dimensions"] = transfer.image_metadata.get("dimensions", {})
                    attachment["thumbnail"] = transfer.image_metadata.get("thumbnail_base64", "")

            # Add voice-specific fields (v0.13.0+)
            if is_voice:
                # Voice messages always need file_path for playback
                if file_path:
                    attachment["file_path"] = str(file_path)
                if transfer.voice_metadata:
                    attachment["voice_metadata"] = transfer.voice_metadata

            # Extract text caption from image_metadata if available
            caption_text = ""
            if is_image and transfer.image_metadata:
                caption_text = transfer.image_metadata.get("text", "")

            if existing_record is not None:
                # Patched in place: the signature covers no attachment field.
                attachment = conversation_monitor.attach_received_file(existing_record, attachment)
                logger.info("Completed the sender's record %s with the received %s",
                            message_id, transfer.filename)

            # Route to group chat or P2P chat based on group_id
            message_event = {
                "sender_node_id": node_id,
                "sender_name": sender_name,
                "text": caption_text,  # Sender's caption (empty if not provided)
                "message_id": message_id,
                "attachments": [attachment]
            }

            if transfer.group_id:
                message_event["group_id"] = transfer.group_id
                await self.local_api.broadcast_event("group_file_received", message_event)
            else:
                await self.local_api.broadcast_event("new_p2p_message", message_event)
            logger.debug(f"Broadcasted {'image' if is_image else 'voice' if is_voice else 'file'} received message to UI: {transfer.filename}")

            # Broadcast completion event to hide active transfer panel
            completion_event = {
                "transfer_id": transfer.transfer_id,
                "node_id": node_id,
                "filename": transfer.filename,
                "size_bytes": transfer.size_bytes,
                "size_mb": size_mb,
                "direction": "download",
                "hash": transfer.hash,
                "mime_type": transfer.mime_type,
                "status": "completed"
            }
            if transfer.group_id:
                completion_event["group_id"] = transfer.group_id
            await self.local_api.broadcast_event("file_transfer_complete", completion_event)

            # Add to backend conversation monitor (RECEIVER SIDE)
            # This ensures transcriptions can be attached and knowledge extraction works
            # For group transfers, key on group_id; for P2P, key on node_id
            if conversation_monitor and existing_record is None:
                # Determine message content based on attachment type
                if is_voice:
                    message_content = f"Received voice message: {transfer.filename} ({size_mb} MB)"
                elif is_image:
                    message_content = f"Received screenshot: {transfer.filename} ({size_mb} MB)"
                else:
                    message_content = f"Received file: {transfer.filename} ({size_mb} MB)"

                # Add as "assistant" role (peer's message from receiver's perspective).
                # The node that sent the file is the author of this note: without
                # it the row rendered as "You" on the receiving node, and the
                # record joined the per-author digest under an empty author whose
                # contents differ on every node, so that author never agreed.
                conversation_monitor.add_message(
                    "assistant", message_content, [attachment],
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    sender_node_id=node_id,
                    sender_name=sender_name,
                    # The same id the broadcast above carried. Without it the
                    # stored record and the bubble already on screen are two
                    # messages to anything that joins them by id, and the
                    # history backfill drew the file a second time.
                    message_id=message_id,
                )
                # So the merge can drop this note when the sender's own
                # record of the file arrives after the transfer.
                remember = getattr(conversation_monitor, "note_local_file_record", None)
                if transfer.group_id and callable(remember):
                    remember(message_id)
                logger.debug(f"Added received {attachment['type']} to conversation history: {transfer.filename}")

        # Auto-transcribe voice messages if enabled (v0.13.2+ recipient-side)
        if is_voice and transfer.voice_metadata and save_to_disk:
            # The module's asyncio: an `import asyncio` here made the name local
            # to the whole method, and the relay below raised on every file.
            asyncio.create_task(
                self.service._maybe_transcribe_voice_message(
                    transfer_id=transfer.transfer_id,
                    node_id=node_id,
                    file_path=file_path,
                    voice_metadata=transfer.voice_metadata,
                    is_sender=False,  # Recipient-side transcription
                    group_id=transfer.group_id
                )
            )

        # Relay a group file to the members its author may not reach (star
        # topology). Once per file: the key, not the transfer, decides, because
        # every relay is a new transfer and keying on that relayed one file
        # around a full mesh for as long as everyone stayed online.
        if (transfer.group_id and save_to_disk and file_path and file_path.exists()
                and self.service and hasattr(self.service, 'group_manager')):
            file_key = self._offer_key(transfer.group_id, transfer.message_id, transfer.hash)
            relay_key = f"relay:{file_key or transfer.transfer_id}"
            if relay_key not in self._group_files_held:
                self._group_files_held[relay_key] = None
                group = self.service.group_manager.get_group(transfer.group_id)
                if group:
                    kind = "voice" if is_voice else "image" if is_image else "file"
                    for member_id in group.members:
                        if member_id == self.p2p_manager.node_id:
                            continue
                        if member_id == node_id:  # the hop it came from
                            continue
                        if member_id == transfer.sender_node_id:  # its author
                            continue
                        if member_id in self.p2p_manager.peers:
                            asyncio.create_task(self._relay_group_file(member_id, file_path, transfer, kind))

        # Cleanup: leaving the table releases the receive (the file is already renamed)
        self.active_transfers.pop(transfer.transfer_id, None)
        logger.debug(f"Cleaned up completed download transfer: {transfer.transfer_id}")

    async def _relay_group_file(self, member_id: str, file_path: Path,
                                received: FileTransfer, kind: str) -> None:
        """Offer a received group file to one member, under the identity it came with.

        Runs as its own task, so a refusal here (the upload cap, a dropped
        peer) is logged here: raised, it surfaced only as "Task exception was
        never retrieved".
        """
        try:
            await self.send_file(
                node_id=member_id,
                file_path=file_path,
                group_id=received.group_id,
                image_metadata=received.image_metadata,
                voice_metadata=received.voice_metadata,
                message_id=received.message_id,
                sender_node_id=received.sender_node_id,
                group_relay=True,
            )
            logger.info("Relayed group %s %s to %s", kind, received.filename, member_id[:20])
        except Exception as e:  # noqa: BLE001 - one member's failure ends only its relay
            logger.warning("Could not relay group %s %s to %s: %s",
                           kind, received.filename, member_id[:20], e)

    async def handle_file_complete(self, node_id: str, payload: dict):
        """
        Handle FILE_COMPLETE from receiver - transfer finished.

        Args:
            node_id: Receiver node ID
            payload: FILE_COMPLETE payload
        """
        transfer_id = payload["transfer_id"]
        transfer = self.active_transfers.get(transfer_id)

        if not transfer:
            logger.warning(f"FILE_COMPLETE for unknown transfer: {transfer_id}")
            return

        transfer.status = TransferStatus.COMPLETED
        logger.info(f"File transfer completed: {transfer.filename}")

        # Note: FileCompleteHandler broadcasts new_p2p_message and file_transfer_complete
        # for upload direction, so we don't duplicate broadcasts here

        # Cleanup. Leaving the table deletes a screenshot that is not kept, when
        # this was the last upload of it: a group fan-out deleted it on the first
        # member's completion, and the members still to be sent got nothing.
        del self.active_transfers[transfer_id]  # Remove from active transfers
        logger.debug(f"Cleaned up completed upload transfer: {transfer_id}")

    async def cancel_transfer(self, transfer_id: str, reason: str = "user_cancelled"):
        """
        Cancel active transfer.

        Args:
            transfer_id: Transfer to cancel
            reason: Cancellation reason
        """
        transfer = self.active_transfers.get(transfer_id)
        if not transfer:
            logger.warning(f"Cannot cancel unknown transfer: {transfer_id}")
            return

        # Declining an offer that was put to the user: the sender learns why
        # rather than seeing a bare cancel (its UI shows `denial_reason`).
        denial_reason = None
        if transfer.awaiting_permission and transfer.direction == "download":
            denial_reason = ASKED_OFFER_DECLINED_TEXT

        transfer.status = TransferStatus.CANCELLED
        await self._send_file_cancel(transfer.node_id, transfer_id, reason, denial_reason)

        # Cleanup
        del self.active_transfers[transfer_id]
        logger.info(f"Transfer cancelled: {transfer_id} (reason: {reason})")

    async def _send_file_cancel(self, node_id: str, transfer_id: str, reason: str,
                                denial_reason: Optional[str] = None):
        """Send FILE_CANCEL message to peer (gracefully handles disconnected peer)."""
        payload = {"transfer_id": transfer_id, "reason": reason}
        self._cancels_sent[transfer_id] = (_monotonic(), reason)
        if len(self._cancels_sent) > CANCEL_MEMORY_MAX:
            del self._cancels_sent[next(iter(self._cancels_sent))]
        if denial_reason:
            payload["denial_reason"] = denial_reason
        try:
            await self.p2p_manager.send_message_to_peer(node_id, {
                "command": "FILE_CANCEL",
                "payload": payload
            })
        except ConnectionError:
            # Peer already disconnected, no need to send FILE_CANCEL
            logger.debug(f"Could not send FILE_CANCEL to {node_id} (already disconnected)")

    async def _send_chunk_retry_request(self, node_id: str, transfer_id: str, chunk_index: int):
        """
        Send FILE_CHUNK_RETRY request to sender (v0.11.1).

        Args:
            node_id: Sender node ID
            transfer_id: Transfer identifier
            chunk_index: Index of chunk to retry
        """
        logger.info(f"Requesting retry for chunk {chunk_index} of transfer {transfer_id}")
        try:
            await self.p2p_manager.send_message_to_peer(node_id, {
                "command": "FILE_CHUNK_RETRY",
                "payload": {
                    "transfer_id": transfer_id,
                    "chunk_index": chunk_index
                }
            })
        except ConnectionError:
            # Peer disconnected during transfer, retry request not needed
            logger.debug(f"Could not send FILE_CHUNK_RETRY to {node_id} (already disconnected)")

    async def handle_file_chunk_retry(self, node_id: str, payload: dict):
        """
        Handle FILE_CHUNK_RETRY request - resend specific chunk (v0.11.1).

        Args:
            node_id: Receiver node ID requesting retry
            payload: Contains transfer_id, chunk_index
        """
        transfer_id = payload["transfer_id"]
        chunk_index = payload["chunk_index"]

        transfer = self.active_transfers.get(transfer_id)
        if not transfer or transfer.direction != "upload":
            logger.warning(f"FILE_CHUNK_RETRY for unknown upload: {transfer_id}")
            return

        logger.info(f"Resending chunk {chunk_index} for transfer {transfer_id}")

        try:
            # Read and send the specific chunk
            with self.using_file(transfer.file_path), open(transfer.file_path, 'rb') as f:
                f.seek(chunk_index * transfer.chunk_size)
                chunk = f.read(transfer.chunk_size)

                if not chunk:
                    logger.error(f"Failed to read chunk {chunk_index} from {transfer.file_path}")
                    return

                chunk_base64 = base64.b64encode(chunk).decode('utf-8')

                # Send FILE_CHUNK
                await self.p2p_manager.send_message_to_peer(node_id, {
                    "command": "FILE_CHUNK",
                    "payload": {
                        "transfer_id": transfer_id,
                        "chunk_index": chunk_index,
                        "total_chunks": transfer.total_chunks,
                        "data": chunk_base64
                    }
                })

                logger.debug(f"Resent chunk {chunk_index}/{transfer.total_chunks} for {transfer.filename}")

        except Exception as e:
            logger.error(f"Error resending chunk {chunk_index}: {e}", exc_info=True)
        finally:
            self._release_upload(transfer)

    async def handle_file_cancel(self, node_id: str, payload: dict):
        """Handle FILE_CANCEL from peer."""
        transfer_id = payload["transfer_id"]
        reason = payload.get("reason", "unknown")

        transfer = self.active_transfers.get(transfer_id)
        if transfer:
            transfer.status = TransferStatus.CANCELLED
            if reason == ALREADY_RECEIVED_REASON:
                # Not a failure: the peer has this file already.
                logger.info(f"Peer already has {transfer.filename}, offer withdrawn: {transfer_id}")
            elif reason == NO_ANSWER_REASON:
                # Not a failure either: nobody answered the offer in time. On an
                # upload this frees the slot; on a download it drops the offer a
                # sender gave up on.
                logger.info(f"Offer {transfer_id} ({transfer.filename}) expired unanswered, peer sent {reason}")
            else:
                logger.info(f"Transfer cancelled by peer: {transfer_id} (reason: {reason})")
            del self.active_transfers[transfer_id]

    async def _check_file_transfer_permission(
        self,
        node_id: str,
        file_path_or_name: any = None,
        size_bytes: int = None,
        mime_type: str = None
    ) -> tuple:
        """`_file_transfer_decision` folded to `(allowed, error_message)` for a
        caller that cannot ask the user: an ASK is a no here (fail closed)."""
        decision = await self._file_transfer_decision(node_id, file_path_or_name, size_bytes, mime_type)
        if decision.outcome == FILE_TRANSFER_ALLOW:
            return (True, None)
        return (False, decision.reason or FIREWALL_DENIED_TEXT)

    def _on_proved_connection(self, node_id: str) -> bool:
        """The live connection to `node_id` has its key proved (direct TLS),
        the line the gateway draws for serving peer inference."""
        proved, _connection_type = peer_proof(getattr(self.p2p_manager, "peers", None), node_id)
        return proved is True

    async def _file_transfer_decision(
        self,
        node_id: str,
        file_path_or_name: any = None,
        size_bytes: int = None,
        mime_type: str = None
    ) -> FileTransferDecision:
        """
        Allow, deny or ask for an offer from `node_id`, in the order above
        FILE_TRANSFER_ALLOW. The deciding entry's size and MIME limits apply;
        an ASK has none, because the user sees the file.

        Args:
            node_id: Peer node ID
            file_path_or_name: Path object (for upload) or filename string (for download)
            size_bytes: File size in bytes (optional, for downloads)
            mime_type: MIME type (optional, for downloads)
        """
        file_transfer_rules = self.firewall.rules.get('file_transfer', {}) or {}

        entry = None
        basis = "none"
        node_rules = (file_transfer_rules.get('nodes') or {}).get(node_id, {})
        if node_rules:
            entry, basis = node_rules, "node"
        else:
            group_rules = file_transfer_rules.get('groups') or {}
            for group_name in self.firewall._get_groups_for_node(node_id):
                if group_name in group_rules:
                    entry, basis = group_rules[group_name], "group"
                    break  # the first group with an entry decides, deny included

        if entry is None and self._on_proved_connection(node_id):
            entry = file_transfer_rules.get(
                FILE_TRANSFER_PROVED_CONNECTION_KEY, FILE_TRANSFER_PROVED_CONNECTION_DEFAULT
            )
            basis = FILE_TRANSFER_PROVED_CONNECTION_KEY

        if entry is None:
            reason = f"No File Transfer rule names {node_id}"
            logger.info(f"File transfer from {node_id} is put to the user: {reason}")
            return FileTransferDecision(FILE_TRANSFER_ASK, reason, basis)

        if not isinstance(entry, dict):
            entry = {}  # a hand-edited non-entry decides nothing but deny
        if basis == FILE_TRANSFER_PROVED_CONNECTION_KEY and entry.get('file_transfer.allow') == FILE_TRANSFER_ASK:
            # "Proved" says the peer holds its key, not that anyone knows it: the
            # entry has no answer for this sender, so the user is asked. Only this
            # entry takes the value; on a node or group entry anything but "allow"
            # still reads as a refusal.
            reason = f"{node_id} is on a direct connection and the direct-connection rule says ask"
            logger.info(f"File transfer from {node_id} is put to the user: {reason}")
            return FileTransferDecision(FILE_TRANSFER_ASK, reason, basis)
        allowed = entry.get('file_transfer.allow', 'deny') == 'allow'
        max_size_mb = entry.get('file_transfer.max_size_mb')
        allowed_types = entry.get('file_transfer.allowed_mime_types', ['*'])

        if not allowed:
            error_msg = FIREWALL_DENIED_TEXT
            logger.warning(f"{error_msg} for {node_id} (by its {basis} entry)")
            return FileTransferDecision(FILE_TRANSFER_DENY, error_msg, basis)

        # Get size and mime type
        if isinstance(file_path_or_name, Path):
            # Upload case
            size_mb = file_path_or_name.stat().st_size / (1024 * 1024)
            mime_type = self._detect_mime_type(file_path_or_name)
        else:
            # Download case
            if size_bytes is not None:
                size_mb = size_bytes / (1024 * 1024)
            else:
                size_mb = 0
            if mime_type is None:
                mime_type = "application/octet-stream"

        # Check per-peer size limit (optional)
        # Note: max_size_mb = 0 means unlimited (no limit)
        if max_size_mb and max_size_mb > 0 and size_mb > max_size_mb:
            error_msg = f"File too large: {size_mb:.1f} MB exceeds {max_size_mb} MB limit"
            logger.warning(f"{error_msg} for {node_id}")
            return FileTransferDecision(FILE_TRANSFER_DENY, error_msg, basis)

        # Check MIME type restrictions (optional)
        if allowed_types and "*" not in allowed_types:
            if not any(self._mime_match(mime_type, pattern) for pattern in allowed_types):
                error_msg = f"File type '{mime_type}' not allowed (allowed: {', '.join(allowed_types)})"
                logger.warning(f"{error_msg} for {node_id}")
                return FileTransferDecision(FILE_TRANSFER_DENY, error_msg, basis)

        return FileTransferDecision(FILE_TRANSFER_ALLOW, None, basis)

    def _detect_mime_type(self, file_path: Path) -> str:
        """Detect MIME type from file extension."""
        import mimetypes
        mime_type, _ = mimetypes.guess_type(str(file_path))
        return mime_type or "application/octet-stream"

    def _mime_match(self, mime_type: str, pattern: str) -> bool:
        """Check if MIME type matches pattern (e.g., 'image/*')."""
        if pattern == "*":
            return True
        if pattern.endswith("/*"):
            return mime_type.startswith(pattern[:-2])
        return mime_type == pattern

    def get_transfer_progress(self, transfer_id: str) -> Optional[tuple[int, int]]:
        """
        Get transfer progress.

        Args:
            transfer_id: Transfer to check

        Returns:
            Tuple of (chunks_done, total_chunks) or None if not found
        """
        transfer = self.active_transfers.get(transfer_id)
        if not transfer:
            return None
        return (len(transfer.chunks_received), transfer.total_chunks)

    def cleanup_completed_transfers(self):
        """Remove completed/failed/cancelled transfers from active list."""
        to_remove = [
            tid for tid, t in self.active_transfers.items()
            if t.status in (TransferStatus.COMPLETED, TransferStatus.FAILED, TransferStatus.CANCELLED)
        ]
        for tid in to_remove:
            del self.active_transfers[tid]
        if to_remove:
            logger.debug(f"Cleaned up {len(to_remove)} completed transfers")
