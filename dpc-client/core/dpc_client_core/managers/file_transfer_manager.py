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
- Firewall-gated: node or group entry, else a direct (proved) connection, else the user is asked

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
- A sender no rule names and not on a direct connection is put to the user
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

# The receiver's answer to an offer. Explicit entries win in this order: the
# node entry, then the sender's first group that has an entry (allow or deny
# alike), then `file_transfer.proved_connection` when the sender is on a
# proved (direct TLS) connection; a sender none of these names is ASKed — the
# offer waits for the user instead of being refused. Mike's call, 2026-10-25.
FILE_TRANSFER_ALLOW = "allow"
FILE_TRANSFER_DENY = "deny"
FILE_TRANSFER_ASK = "ask"
FIREWALL_DENIED_TEXT = "File transfer not permitted by firewall rules"
# `denial_reason` of the FILE_CANCEL the sender gets when the user declines an
# offer that was put to them; the `reason` word stays the one the UI sends.
ASKED_OFFER_DECLINED_TEXT = (
    "The receiver declined files from your node: it is not on a direct connection "
    "with them and no File Transfer rule of theirs names it"
)


class FileTransferDecision(NamedTuple):
    """`outcome` is FILE_TRANSFER_ALLOW / _DENY / _ASK — a word, never a truth value.

    `basis` names what decided: `node`, `group`, `proved_connection`, or `none`
    for an ASK. `reason` is the sentence a FILE_CANCEL carries as `denial_reason`.
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
    # A download whose sender no rule names (FILE_TRANSFER_ASK), until the user
    # answers "allow files from X?"; a decline meanwhile tells the sender why.
    awaiting_permission: bool = False

    # Receive side only. Decided at the first chunk: where the file goes and
    # whether it goes to disk at all (a screenshot with save_screenshots_to_disk
    # off is verified chunk by chunk and never written).
    destination: Optional[Path] = None
    save_to_disk: Optional[bool] = None
    receive_path: Optional[Path] = None  # the temporary file, renamed to `destination` at the end
    receive_file: Any = field(default=None, repr=False, compare=False)  # open handle on receive_path
    quiet_since: float = 0.0             # monotonic time of the last chunk or re-ask
    stall_rounds: int = 0                # re-ask rounds in a row without a new chunk
    reasked: set = field(default_factory=set)  # indices of the current re-ask batch still owed
    stall_watch: Any = field(default=None, repr=False, compare=False)  # asyncio.Task
    finalizing: bool = False


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
        self.active_transfers: Dict[str, FileTransfer] = _TransferTable(self._release_receive)

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
        chunk_hashes: Optional[List[str]] = None
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

        Returns:
            transfer_id: Unique transfer identifier

        Raises:
            FileNotFoundError: If file doesn't exist
            PermissionError: If firewall denies file transfer
            RuntimeError: If max concurrent transfers exceeded
        """
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Check concurrent transfer limit
        active_uploads = sum(1 for t in self.active_transfers.values() if t.direction == "upload")
        if active_uploads >= self.max_concurrent_transfers:
            raise RuntimeError(f"Max concurrent transfers ({self.max_concurrent_transfers}) exceeded")

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
            group_id=group_id              # Group ID for group file transfers (v0.19.0+)
        )
        self.active_transfers[transfer_id] = transfer

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

        await self.p2p_manager.send_message_to_peer(node_id, {
            "command": "FILE_OFFER",
            "payload": payload
        })

        logger.info(f"File transfer initiated: {file_path.name} ({file_size} bytes) to {node_id}")
        return transfer_id

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

        # Send chunks
        try:
            with open(transfer.file_path, 'rb') as f:
                for chunk_index in range(transfer.total_chunks):
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

            logger.info(f"All chunks sent for {transfer.filename}")

        except Exception as e:
            logger.error(f"Error sending chunks: {e}", exc_info=True)
            transfer.status = TransferStatus.FAILED
            await self._send_file_cancel(node_id, transfer_id, "send_error")

    # --- receiving ---------------------------------------------------------

    def _incoming_destination(self, node_id: str, transfer: FileTransfer) -> Tuple[Path, bool]:
        """Where a download lands, and whether it is written to disk at all."""
        is_image = (transfer.mime_type and transfer.mime_type.startswith("image/")
                    and transfer.image_metadata is not None)
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

        save_to_disk = True
        if is_image and self.firewall:
            img_rules = self.firewall.rules.get("image_transfer", {})
            save_to_disk = img_rules.get("save_screenshots_to_disk", True)
        return file_path, save_to_disk

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

    async def _fail_download(self, node_id: str, transfer: FileTransfer, reason: str) -> None:
        """End a download as FAILED: tell the sender, tell the UI, free the state."""
        transfer.status = TransferStatus.FAILED
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
            logger.warning(f"FILE_CHUNK for unknown download: {transfer_id}")
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
            message_id = hashlib.sha256(
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
                existing_record = finder(node_id, transfer.filename, transfer.size_bytes, transfer.hash)
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
            import asyncio
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

        # Relay group file to connected members that couldn't reach the original sender (star topology)
        if (transfer.group_id and save_to_disk and file_path and file_path.exists()
                and self.service and hasattr(self.service, 'group_manager')):
            relay_key = f"relay:{transfer.transfer_id}"
            if relay_key not in self.service._processed_message_ids:
                self.service._processed_message_ids.add(relay_key)
                group = self.service.group_manager.get_group(transfer.group_id)
                if group:
                    for member_id in group.members:
                        if member_id == self.p2p_manager.node_id:
                            continue
                        if member_id == node_id:  # skip original sender
                            continue
                        if member_id in self.p2p_manager.peers:
                            try:
                                import asyncio
                                asyncio.create_task(self.send_file(
                                    node_id=member_id,
                                    file_path=file_path,
                                    group_id=transfer.group_id,
                                    image_metadata=transfer.image_metadata,
                                    voice_metadata=transfer.voice_metadata,
                                ))
                                logger.info(
                                    "Relayed group %s %s to %s",
                                    "voice" if is_voice else "image" if is_image else "file",
                                    transfer.filename, member_id[:20]
                                )
                            except Exception as e:
                                logger.error(
                                    "Failed to relay group file to %s: %s", member_id[:20], e
                                )

        # Cleanup: leaving the table releases the receive (the file is already renamed)
        self.active_transfers.pop(transfer.transfer_id, None)
        logger.debug(f"Cleaned up completed download transfer: {transfer.transfer_id}")

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

        # Detect if this is an image transfer
        is_image = (transfer.mime_type and transfer.mime_type.startswith("image/")
                   and transfer.image_metadata is not None)

        # Check privacy settings - delete screenshot if save_screenshots_to_disk is false
        if is_image and transfer.file_path and self.firewall:
            img_rules = self.firewall.rules.get("image_transfer", {})
            save_to_disk = img_rules.get("save_screenshots_to_disk", True)
            logger.debug(f"Screenshot cleanup check: save_to_disk={save_to_disk}, file_exists={transfer.file_path.exists()}")

            if not save_to_disk and transfer.file_path.exists():
                transfer.file_path.unlink()  # Delete the screenshot file
                logger.info(f"Deleted screenshot after upload (save_screenshots_to_disk=false): {transfer.file_path}")
        elif is_image:
            logger.debug(f"Screenshot cleanup skipped: file_path={transfer.file_path}, firewall={self.firewall is not None}")

        # Note: FileCompleteHandler broadcasts new_p2p_message and file_transfer_complete
        # for upload direction, so we don't duplicate broadcasts here

        # Cleanup
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
            with open(transfer.file_path, 'rb') as f:
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

    async def handle_file_cancel(self, node_id: str, payload: dict):
        """Handle FILE_CANCEL from peer."""
        transfer_id = payload["transfer_id"]
        reason = payload.get("reason", "unknown")

        transfer = self.active_transfers.get(transfer_id)
        if transfer:
            transfer.status = TransferStatus.CANCELLED
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
            reason = f"No File Transfer rule names {node_id} and it is not on a direct connection"
            logger.info(f"File transfer from {node_id} is put to the user: {reason}")
            return FileTransferDecision(FILE_TRANSFER_ASK, reason, basis)

        if not isinstance(entry, dict):
            entry = {}  # a hand-edited non-entry decides nothing but deny
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
