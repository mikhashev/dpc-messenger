"""Handler for FILE_OFFER command - incoming file transfer offers."""

from typing import Dict, Any, Optional
from . import MessageHandler


class FileOfferHandler(MessageHandler):
    """Handles FILE_OFFER messages (incoming file transfer requests)."""

    @property
    def command_name(self) -> str:
        return "FILE_OFFER"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle FILE_OFFER message.

        Flow:
        1. Check firewall permission
        2. Notify UI (prompt user to accept/reject)
        3. Create pending download transfer
        4. User accepts → send FILE_ACCEPT
        5. User rejects → send FILE_CANCEL

        Args:
            sender_node_id: Node ID of sender
            payload: Contains transfer_id, filename, size_bytes, hash, mime_type, chunk_size
        """
        transfer_id = payload.get("transfer_id")
        filename = payload.get("filename")
        size_bytes = payload.get("size_bytes")
        hash_value = payload.get("hash")
        mime_type = payload.get("mime_type")
        chunk_size = payload.get("chunk_size")
        chunk_hashes = payload.get("chunk_hashes")  # v0.11.1: CRC32 hashes per chunk

        self.logger.info(f"FILE_OFFER from {sender_node_id}: {filename} ({size_bytes} bytes)")

        # Check firewall permission
        file_transfer_manager = self.service.file_transfer_manager
        allowed = await file_transfer_manager._check_file_transfer_permission(sender_node_id, filename, size_bytes, mime_type)

        if not allowed:
            self.logger.warning(f"File transfer denied by firewall from {sender_node_id}")
            # Send FILE_CANCEL (rejected)
            await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
                "command": "FILE_CANCEL",
                "payload": {
                    "transfer_id": transfer_id,
                    "reason": "firewall_denied"
                }
            })
            return None

        # Notify UI (prompt user)
        sender_name = self.service.peer_metadata.get(sender_node_id, {}).get("name") or sender_node_id
        await self.service.local_api.broadcast_event("file_transfer_offered", {
            "transfer_id": transfer_id,
            "node_id": sender_node_id,
            "sender_name": sender_name,
            "filename": filename,
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / (1024 * 1024), 2),
            "mime_type": mime_type,
            "hash": hash_value
        })

        # Create pending download transfer
        from ..managers.file_transfer_manager import FileTransfer, TransferStatus
        total_chunks = (size_bytes + chunk_size - 1) // chunk_size

        transfer = FileTransfer(
            transfer_id=transfer_id,
            filename=filename,
            size_bytes=size_bytes,
            hash=hash_value,
            mime_type=mime_type,
            chunk_size=chunk_size,
            node_id=sender_node_id,
            direction="download",
            status=TransferStatus.PENDING,
            chunks_received=set(),
            total_chunks=total_chunks,
            chunk_hashes=chunk_hashes,  # v0.11.1: Store for per-chunk verification
            chunks_failed=set(),  # v0.11.1: Track failed chunks for retry
            retry_count={}  # v0.11.1: Track retry attempts per chunk
        )
        file_transfer_manager.active_transfers[transfer_id] = transfer

        self.logger.info(f"File transfer pending user acceptance: {transfer_id}")
        return None
