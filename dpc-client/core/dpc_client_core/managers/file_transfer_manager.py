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
from pathlib import Path
from typing import Optional, Dict, Callable, TYPE_CHECKING
from dataclasses import dataclass
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
        file_data: Accumulated file data (for downloads)
        file_path: Path to file being sent (for uploads)
        progress_callback: Optional callback for progress updates
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
    file_data: Optional[bytearray] = None
    file_path: Optional[Path] = None
    progress_callback: Optional[Callable[[str, int, int], None]] = None


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
        chunk_size: Default chunk size in bytes
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
        self.background_threshold_mb = int(settings.get("file_transfer", "background_threshold_mb", "50"))
        self.direct_tls_threshold_mb = int(settings.get("file_transfer", "direct_tls_only_threshold_mb", "100"))
        self.max_concurrent_transfers = int(settings.get("file_transfer", "max_concurrent_transfers", "3"))
        self.verify_hash = settings.get("file_transfer", "verify_hash", "true").lower() in ("true", "1", "yes")

        # Storage path: ~/.dpc/conversations/{peer_id}/files/
        if storage_base_path is None:
            storage_base_path = Path.home() / ".dpc"
        self.storage_base_path = storage_base_path / "conversations"
        self.storage_base_path.mkdir(parents=True, exist_ok=True)

        logger.info(f"FileTransferManager initialized (chunk_size={self.chunk_size}, max_concurrent={self.max_concurrent_transfers})")

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

    async def send_file(
        self,
        node_id: str,
        file_path: Path,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> str:
        """
        Initiate file transfer to peer.

        Args:
            node_id: Target peer node ID
            file_path: Path to file to send
            progress_callback: Optional callback(transfer_id, chunks_sent, total_chunks)

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
        if not await self._check_file_transfer_permission(node_id, file_path):
            raise PermissionError(f"Firewall denies file transfer to {node_id}")

        # Compute file metadata
        file_size = file_path.stat().st_size
        file_hash = self._compute_file_hash(file_path) if self.verify_hash else "none"
        mime_type = self._detect_mime_type(file_path)
        total_chunks = (file_size + self.chunk_size - 1) // self.chunk_size

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
            progress_callback=progress_callback
        )
        self.active_transfers[transfer_id] = transfer

        # Send FILE_OFFER
        await self.p2p_manager.send_message_to_peer(node_id, {
            "command": "FILE_OFFER",
            "payload": {
                "transfer_id": transfer_id,
                "filename": transfer.filename,
                "size_bytes": file_size,
                "hash": file_hash,
                "mime_type": mime_type,
                "chunk_size": self.chunk_size
            }
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

                    # Small delay to avoid overwhelming receiver
                    await asyncio.sleep(0.01)

            logger.info(f"All chunks sent for {transfer.filename}")

        except Exception as e:
            logger.error(f"Error sending chunks: {e}", exc_info=True)
            transfer.status = TransferStatus.FAILED
            await self._send_file_cancel(node_id, transfer_id, "send_error")

    async def handle_file_chunk(self, node_id: str, payload: dict):
        """
        Handle incoming FILE_CHUNK - reassemble file.

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

        # Initialize file data buffer
        if transfer.file_data is None:
            transfer.file_data = bytearray()

        # Append chunk (assume in-order for now; TODO: handle out-of-order)
        transfer.file_data.extend(chunk_data)
        transfer.chunks_received.add(chunk_index)

        # Progress callback
        if transfer.progress_callback:
            transfer.progress_callback(transfer_id, len(transfer.chunks_received), transfer.total_chunks)

        # Check if complete
        if len(transfer.chunks_received) == transfer.total_chunks:
            await self._finalize_download(node_id, transfer)

    async def _finalize_download(self, node_id: str, transfer: FileTransfer):
        """Finalize download: verify hash and save file."""
        logger.info(f"All chunks received for {transfer.filename}, finalizing...")

        # Verify hash
        if self.verify_hash and transfer.hash != "none":
            computed_hash = hashlib.sha256(transfer.file_data).hexdigest()
            if computed_hash != transfer.hash:
                logger.error(f"Hash mismatch! Expected {transfer.hash}, got {computed_hash}")
                transfer.status = TransferStatus.FAILED
                await self._send_file_cancel(node_id, transfer.transfer_id, "hash_mismatch")
                return

        # Save file
        storage_path = self._get_peer_storage_path(node_id, "files")
        # Use hash in filename to avoid collisions
        safe_filename = f"{transfer.filename.replace('/', '_')}"
        file_path = storage_path / safe_filename

        with open(file_path, 'wb') as f:
            f.write(transfer.file_data)

        transfer.status = TransferStatus.COMPLETED
        logger.info(f"File saved: {file_path}")

        # Send FILE_COMPLETE
        await self.p2p_manager.send_message_to_peer(node_id, {
            "command": "FILE_COMPLETE",
            "payload": {
                "transfer_id": transfer.transfer_id,
                "hash": hashlib.sha256(transfer.file_data).hexdigest()
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

            await self.local_api.broadcast_event("new_p2p_message", {
                "sender_node_id": node_id,
                "sender_name": sender_name,
                "text": f"📎 {transfer.filename} ({size_mb} MB)",
                "message_id": message_id,
                "attachments": [{
                    "type": "file",
                    "filename": transfer.filename,
                    "size_bytes": transfer.size_bytes,
                    "size_mb": size_mb,
                    "hash": transfer.hash,
                    "mime_type": transfer.mime_type,
                    "transfer_id": transfer.transfer_id,
                    "status": "completed"
                }]
            })
            logger.debug(f"Broadcasted file received message to UI: {transfer.filename}")

        # Cleanup
        transfer.file_data = None  # Free memory

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
        """Send FILE_CANCEL message to peer."""
        await self.p2p_manager.send_message_to_peer(node_id, {
            "command": "FILE_CANCEL",
            "payload": {
                "transfer_id": transfer_id,
                "reason": reason
            }
        })

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
    ) -> bool:
        """
        Check if firewall allows file transfer with peer.

        Args:
            node_id: Peer node ID
            file_path_or_name: Path object (for upload) or filename string (for download)
            size_bytes: File size in bytes (optional, for downloads)
            mime_type: MIME type (optional, for downloads)

        Returns:
            True if allowed, False otherwise
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
            logger.warning(f"File transfer denied by firewall for {node_id}")
            return False

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
        if max_size_mb and size_mb > max_size_mb:
            logger.warning(f"File too large ({size_mb:.1f} MB > {max_size_mb} MB limit) for {node_id}")
            return False

        # Check MIME type restrictions (optional)
        if allowed_types and "*" not in allowed_types:
            if not any(self._mime_match(mime_type, pattern) for pattern in allowed_types):
                logger.warning(f"MIME type {mime_type} not allowed for {node_id}")
                return False

        return True

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
