"""
File Transfer Manager - Chunked P2P File Transfers

This module implements chunked file transfer functionality for D-PC Messenger.
Files are split into chunks, transferred with hash verification, and support
progress tracking and cancellation.

Architecture:
- Chunked transfer (64KB default) for resumability
- SHA256 hash verification for integrity
- Progress callbacks for UI updates
- Background transfer for large files (>50MB)
- Firewall-gated (explicit allow rules required)

Transfer Flow:
1. Sender: Compute hash, create FILE_OFFER
2. Receiver: Check firewall, prompt user, send FILE_ACCEPT
3. Sender: Send FILE_CHUNK messages (with progress)
4. Receiver: Reassemble, verify hash, send FILE_COMPLETE
5. Both: Update conversation history with attachment metadata

Features:
- No hard size limit (large files use background process)
- Direct TLS preferred for >100MB (fallback to WebRTC/relay for smaller)
- Per-peer storage: ~/.dpc/conversations/{peer_id}/files/
- Metadata references in conversation history (not full content)

Privacy:
- Files require explicit firewall rules (not auto-allowed)
- Optional per-peer size limits
- Optional MIME type restrictions
"""

import asyncio
import logging
import hashlib
import base64
import uuid
import zlib  # For CRC32 checksums
from pathlib import Path
from typing import Optional, Dict, Callable, List, Any, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import Enum

if TYPE_CHECKING:
    from ..p2p_manager import P2PManager
    from ..firewall import ContextFirewall
    from ..settings import Settings

logger = logging.getLogger(__name__)


class TransferStatus(Enum):
    """File transfer status states."""
    PENDING = "pending"
    TRANSFERRING = "transferring"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
        chunk_data: Dict[int, bytes] - Chunks indexed by chunk_index for out-of-order assembly (v0.11.1)
        file_path: Path to file being sent (for uploads)
        progress_callback: Optional callback for progress updates
        chunk_hashes: List of CRC32 hashes for each chunk (v0.11.1)
        chunks_failed: Set of chunk indices that failed verification (v0.11.1)
        retry_count: Dict mapping chunk index to retry attempt count (v0.11.1)
        max_retries: Maximum retry attempts per chunk (default: 3)
        image_metadata: Optional dict with image metadata (dimensions, thumbnail, etc.) for v0.12.0+
        voice_metadata: Optional dict with voice metadata (duration, sample_rate, codec, etc.) for v0.13.0+
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
    chunk_data: Optional[dict] = None  # v0.11.1: Dict[int, bytes] - chunks indexed by chunk_index
    file_path: Optional[Path] = None
    progress_callback: Optional[Callable[[str, int, int], None]] = None
    chunk_hashes: Optional[list] = None  # CRC32 hashes for integrity
    chunks_failed: Optional[set] = None  # Failed chunk indices
    retry_count: Optional[dict] = None   # Chunk index -> retry count
    max_retries: int = 3                 # Max retries per chunk
    image_metadata: Optional[dict] = None  # Image metadata (dimensions, thumbnail, etc.) v0.12.0+
    voice_metadata: Optional[dict] = None  # Voice metadata (duration, sample_rate, codec) v0.13.0+


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
        self.active_transfers: Dict[str, FileTransfer] = {}

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

    async def _compute_file_hash_async(self, file_path: Path, progress_interval_bytes: int = 100 * 1024 * 1024) -> str:
        """
        Compute SHA256 hash of file asynchronously with progress reporting.

        Uses thread pool executor to prevent blocking the event loop during
        hash computation for large files.

        Args:
            file_path: Path to file
            progress_interval_bytes: Emit progress event every N bytes (default: 100MB)

        Returns:
            SHA256 hash as hex string

        Emits:
            file_preparation_progress event with phase="hashing_file", percent, bytes_processed
        """
        loop = asyncio.get_event_loop()

        def _hash_worker():
            sha256 = hashlib.sha256()
            total_size = file_path.stat().st_size
            bytes_processed = 0
            last_progress_emit = 0

            with open(file_path, 'rb') as f:
                # Use larger chunks (10MB) for better performance during hashing
                while chunk := f.read(10 * 1024 * 1024):
                    sha256.update(chunk)
                    bytes_processed += len(chunk)

                    # Emit progress every 100MB
                    if bytes_processed - last_progress_emit >= progress_interval_bytes:
                        percent = int((bytes_processed / total_size) * 100)
                        # Schedule event emission on event loop (don't block worker thread)
                        asyncio.run_coroutine_threadsafe(
                            self._emit_preparation_progress(
                                file_path.name,
                                "hashing_file",
                                min(percent, 100),  # Cap at 100%
                                bytes_processed=bytes_processed,
                                total_bytes=total_size
                            ),
                            loop
                        )
                        last_progress_emit = bytes_processed

            return sha256.hexdigest()

        # Run blocking hash computation in thread pool
        return await loop.run_in_executor(None, _hash_worker)

    async def _compute_chunk_hashes_async(self, file_path: Path, progress_interval_chunks: int = 10000) -> List[str]:
        """
        Compute CRC32 hash for each chunk asynchronously with progress reporting.

        Uses thread pool executor to prevent blocking the event loop during
        per-chunk hash computation for large files.

        Args:
            file_path: Path to file
            progress_interval_chunks: Emit progress every N chunks (default: 10,000 = ~640MB)

        Returns:
            List of CRC32 hashes as hex strings

        Emits:
            file_preparation_progress event with phase="hashing_chunks", percent, chunks_processed
        """
        loop = asyncio.get_event_loop()

        def _chunk_hash_worker():
            chunk_hashes = []
            total_size = file_path.stat().st_size
            total_chunks = (total_size + self.chunk_size - 1) // self.chunk_size
            chunks_processed = 0
            last_progress_emit = 0

            with open(file_path, 'rb') as f:
                while chunk := f.read(self.chunk_size):  # 64KB chunks
                    crc = zlib.crc32(chunk) & 0xffffffff
                    chunk_hashes.append(f"{crc:08x}")
                    chunks_processed += 1

                    # Emit progress events periodically
                    if chunks_processed - last_progress_emit >= progress_interval_chunks:
                        percent = int((chunks_processed / total_chunks) * 100)
                        asyncio.run_coroutine_threadsafe(
                            self._emit_preparation_progress(
                                file_path.name,
                                "hashing_chunks",
                                min(percent, 100),
                                chunks_processed=chunks_processed,
                                total_chunks=total_chunks
                            ),
                            loop
                        )
                        last_progress_emit = chunks_processed

            return chunk_hashes

        return await loop.run_in_executor(None, _chunk_hash_worker)

    async def send_file(
        self,
        node_id: str,
        file_path: Path,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        image_metadata: Optional[Dict[str, Any]] = None,
        voice_metadata: Optional[Dict[str, Any]] = None
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

        # Check firewall permission
        allowed, error_msg = await self._check_file_transfer_permission(node_id, file_path)
        if not allowed:
            if error_msg:
                raise PermissionError(error_msg)
            else:
                raise PermissionError(f"Firewall denies file transfer to {node_id}")

        # Compute file metadata
        file_size = file_path.stat().st_size

        # Emit preparation started event
        if self.local_api:
            await self.local_api.broadcast_event("file_preparation_started", {
                "filename": file_path.name,
                "size_bytes": file_size,
                "size_mb": round(file_size / (1024 * 1024), 2)
            })

        # Use async hash computation with progress reporting
        file_hash = await self._compute_file_hash_async(file_path) if self.verify_hash else "none"
        chunk_hashes = await self._compute_chunk_hashes_async(file_path)  # v0.11.1: Per-chunk CRC32

        # Emit preparation completed event
        if self.local_api:
            await self.local_api.broadcast_event("file_preparation_completed", {
                "filename": file_path.name,
                "hash": file_hash,
                "total_chunks": len(chunk_hashes)
            })

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
            voice_metadata=voice_metadata   # Store voice metadata for voice messages (v0.13.0+)
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

    async def handle_file_chunk(self, node_id: str, payload: dict):
        """
        Handle incoming FILE_CHUNK - reassemble file.

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

        # Initialize chunk data dict (v0.11.1: indexed storage for out-of-order assembly)
        if transfer.chunk_data is None:
            transfer.chunk_data = {}

        # v0.11.1: Verify chunk CRC32 if hashes provided
        chunk_verified = True
        if transfer.chunk_hashes and chunk_index < len(transfer.chunk_hashes):
            expected_hash = transfer.chunk_hashes[chunk_index]
            computed_crc = zlib.crc32(chunk_data) & 0xffffffff
            computed_hash = f"{computed_crc:08x}"

            if computed_hash != expected_hash:
                chunk_verified = False
                transfer.chunks_failed.add(chunk_index)

                # Track retry count
                if chunk_index not in transfer.retry_count:
                    transfer.retry_count[chunk_index] = 0
                transfer.retry_count[chunk_index] += 1

                logger.warning(
                    f"Chunk {chunk_index} CRC32 mismatch! "
                    f"Expected {expected_hash}, got {computed_hash}. "
                    f"Retry attempt {transfer.retry_count[chunk_index]}/{transfer.max_retries}"
                )

                # If max retries exceeded, fail transfer
                if transfer.retry_count[chunk_index] >= transfer.max_retries:
                    logger.error(f"Chunk {chunk_index} failed {transfer.max_retries} times, aborting transfer")
                    transfer.status = TransferStatus.FAILED
                    await self._send_file_cancel(node_id, transfer_id, "chunk_verification_failed")
                    return
                else:
                    # Send retry request for this chunk
                    await self._send_chunk_retry_request(node_id, transfer_id, chunk_index)
                    return  # Don't process this chunk further, wait for retry
            else:
                # Chunk verified successfully - remove from failed set if it was retried
                if chunk_index in transfer.chunks_failed:
                    transfer.chunks_failed.discard(chunk_index)
                    logger.info(f"Chunk {chunk_index} verified successfully on retry")

        # Only store chunk if verified (v0.11.1: indexed storage for out-of-order)
        if chunk_verified:
            # Store chunk by index (supports out-of-order arrival)
            transfer.chunk_data[chunk_index] = chunk_data
            transfer.chunks_received.add(chunk_index)

            # Progress callback
            if transfer.progress_callback:
                transfer.progress_callback(transfer_id, len(transfer.chunks_received), transfer.total_chunks)

        # Check if complete (all chunks received and verified)
        if len(transfer.chunks_received) == transfer.total_chunks:
            await self._finalize_download(node_id, transfer)

    async def _finalize_download(self, node_id: str, transfer: FileTransfer):
        """Finalize download: assemble chunks in order, verify hash, and save file."""
        logger.info(f"All chunks received for {transfer.filename}, finalizing...")

        # v0.11.1: Verify all chunks are present before assembly
        for chunk_index in range(transfer.total_chunks):
            if chunk_index not in transfer.chunk_data:
                logger.error(f"Missing chunk {chunk_index} during finalization!")
                transfer.status = TransferStatus.FAILED
                await self._send_file_cancel(node_id, transfer.transfer_id, "missing_chunks")
                return

        # Assemble chunks in correct order for hash verification
        assembled_data = bytearray()
        for chunk_index in range(transfer.total_chunks):
            assembled_data.extend(transfer.chunk_data[chunk_index])

        logger.info(f"Assembled {len(assembled_data)} bytes from {transfer.total_chunks} chunks in correct order")

        # Verify hash
        computed_hash = None
        if self.verify_hash and transfer.hash != "none":
            computed_hash = hashlib.sha256(assembled_data).hexdigest()
            if computed_hash != transfer.hash:
                logger.error(f"Hash mismatch! Expected {transfer.hash}, got {computed_hash}")
                transfer.status = TransferStatus.FAILED
                await self._send_file_cancel(node_id, transfer.transfer_id, "hash_mismatch")
                return
        else:
            # Compute hash for FILE_COMPLETE even if verification disabled
            computed_hash = hashlib.sha256(assembled_data).hexdigest()

        # Detect if this is an image transfer
        is_image = (transfer.mime_type and transfer.mime_type.startswith("image/")
                   and transfer.image_metadata is not None)

        # Save file - use screenshots subdirectory for images
        subdir = "files/screenshots" if is_image else "files"
        storage_path = self._get_peer_storage_path(node_id, subdir)
        # Use hash in filename to avoid collisions
        safe_filename = f"{transfer.filename.replace('/', '_')}"
        file_path = storage_path / safe_filename

        # Check privacy settings - allow disabling screenshot storage
        save_to_disk = True
        if is_image and self.firewall:
            img_rules = self.firewall.rules.get("image_transfer", {})
            save_to_disk = img_rules.get("save_screenshots_to_disk", True)

        if save_to_disk:
            with open(file_path, 'wb') as f:
                f.write(assembled_data)
            transfer.status = TransferStatus.COMPLETED
            logger.info(f"{'Screenshot' if is_image else 'File'} saved: {file_path}")
        else:
            # Don't save to disk - use thumbnail for display only
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

            # Get sender name from peer metadata
            sender_name = node_id
            if self.service and hasattr(self.service, 'peer_metadata'):
                sender_name = self.service.peer_metadata.get(node_id, {}).get("name") or node_id

            size_mb = round(transfer.size_bytes / (1024 * 1024), 2)

            # Detect if this is an image or voice transfer
            is_image = (transfer.mime_type and transfer.mime_type.startswith("image/")
                       and transfer.image_metadata is not None)
            is_voice = transfer.voice_metadata is not None

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

            await self.local_api.broadcast_event("new_p2p_message", {
                "sender_node_id": node_id,
                "sender_name": sender_name,
                "text": caption_text,  # Sender's caption (empty if not provided)
                "message_id": message_id,
                "attachments": [attachment]
            })
            logger.debug(f"Broadcasted {'image' if is_image else 'voice' if is_voice else 'file'} received message to UI: {transfer.filename}")

            # Broadcast completion event to hide active transfer panel
            await self.local_api.broadcast_event("file_transfer_complete", {
                "transfer_id": transfer.transfer_id,
                "node_id": node_id,
                "filename": transfer.filename,
                "size_bytes": transfer.size_bytes,
                "size_mb": size_mb,
                "direction": "download",
                "hash": transfer.hash,
                "mime_type": transfer.mime_type,
                "status": "completed"
            })

        # Cleanup (chunk_data will be freed when transfer is deleted)
        del self.active_transfers[transfer.transfer_id]  # Remove from active transfers
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

        transfer.status = TransferStatus.CANCELLED
        await self._send_file_cancel(transfer.node_id, transfer_id, reason)

        # Cleanup
        del self.active_transfers[transfer_id]
        logger.info(f"Transfer cancelled: {transfer_id} (reason: {reason})")

    async def _send_file_cancel(self, node_id: str, transfer_id: str, reason: str):
        """Send FILE_CANCEL message to peer (gracefully handles disconnected peer)."""
        try:
            await self.p2p_manager.send_message_to_peer(node_id, {
                "command": "FILE_CANCEL",
                "payload": {
                    "transfer_id": transfer_id,
                    "reason": reason
                }
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
        """
        Check if firewall allows file transfer with peer.

        Args:
            node_id: Peer node ID
            file_path_or_name: Path object (for upload) or filename string (for download)
            size_bytes: File size in bytes (optional, for downloads)
            mime_type: MIME type (optional, for downloads)

        Returns:
            Tuple of (allowed: bool, error_message: str|None)
            - (True, None) if allowed
            - (False, error_msg) if denied with specific reason
        """
        # Get file transfer rules from firewall
        file_transfer_rules = self.firewall.rules.get('file_transfer', {})

        # Check node-specific rules first
        node_rules = file_transfer_rules.get('nodes', {}).get(node_id, {})
        if node_rules:
            allowed = node_rules.get('file_transfer.allow', 'deny') == 'allow'
            max_size_mb = node_rules.get('file_transfer.max_size_mb')
            allowed_types = node_rules.get('file_transfer.allowed_mime_types', ['*'])
        else:
            # Check group rules
            allowed = False
            max_size_mb = None
            allowed_types = ['*']

            groups = self.firewall._get_groups_for_node(node_id)
            group_rules = file_transfer_rules.get('groups', {})

            for group_name in groups:
                if group_name in group_rules:
                    group_rule = group_rules[group_name]
                    if group_rule.get('file_transfer.allow') == 'allow':
                        allowed = True
                        max_size_mb = group_rule.get('file_transfer.max_size_mb', max_size_mb)
                        allowed_types = group_rule.get('file_transfer.allowed_mime_types', allowed_types)
                        break  # Use first matching group

        if not allowed:
            error_msg = "File transfer not permitted by firewall rules"
            logger.warning(f"{error_msg} for {node_id}")
            return (False, error_msg)

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
            return (False, error_msg)

        # Check MIME type restrictions (optional)
        if allowed_types and "*" not in allowed_types:
            if not any(self._mime_match(mime_type, pattern) for pattern in allowed_types):
                error_msg = f"File type '{mime_type}' not allowed (allowed: {', '.join(allowed_types)})"
                logger.warning(f"{error_msg} for {node_id}")
                return (False, error_msg)

        return (True, None)

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
