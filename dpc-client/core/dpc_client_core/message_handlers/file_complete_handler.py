"""Handler for FILE_COMPLETE command - file transfer completed successfully."""

from typing import Dict, Any, Optional
from . import MessageHandler


class FileCompleteHandler(MessageHandler):
    """Handles FILE_COMPLETE messages (transfer finished successfully)."""

    @property
    def command_name(self) -> str:
        return "FILE_COMPLETE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle FILE_COMPLETE message.

        Flow:
        1. Verify hash matches (if provided)
        2. Mark transfer as completed
        3. Notify UI
        4. Update conversation history with attachment metadata

        Args:
            sender_node_id: Node ID of peer
            payload: Contains transfer_id, hash (optional)
        """
        transfer_id = payload.get("transfer_id")
        hash_value = payload.get("hash")

        self.logger.info(f"FILE_COMPLETE from {sender_node_id} for transfer {transfer_id}")

        # Delegate to FileTransferManager
        file_transfer_manager = self.service.file_transfer_manager
        await file_transfer_manager.handle_file_complete(sender_node_id, payload)

        # Get transfer info
        transfer = file_transfer_manager.active_transfers.get(transfer_id)
        if not transfer:
            self.logger.warning(f"FILE_COMPLETE for unknown transfer: {transfer_id}")
            return None

        # Broadcast completion event to UI
        await self.service.local_api.broadcast_event("file_transfer_complete", {
            "transfer_id": transfer_id,
            "node_id": sender_node_id,
            "filename": transfer.filename,
            "size_bytes": transfer.size_bytes,
            "size_mb": round(transfer.size_bytes / (1024 * 1024), 2),
            "direction": transfer.direction,
            "hash": transfer.hash,
            "mime_type": transfer.mime_type,
            "status": "completed"
        })

        # Add to conversation history with attachment metadata
        conversation_monitor = self.service.conversation_monitors.get(sender_node_id)
        if conversation_monitor:
            size_mb = round(transfer.size_bytes / (1024 * 1024), 2)
            message_content = f"Received file: {transfer.filename} ({size_mb} MB)"

            attachments = [{
                "type": "file",
                "filename": transfer.filename,
                "size_bytes": transfer.size_bytes,
                "hash": transfer.hash,
                "mime_type": transfer.mime_type,
                "transfer_id": transfer_id,
                "status": "completed"
            }]

            conversation_monitor.add_message("assistant", message_content, attachments)
            self.logger.debug(f"Added file attachment to conversation history: {transfer.filename}")

        # Note: No UI broadcast here on sender side
        # The sender already got a "You sent file X" message from service.py send_file()
        # The receiver gets "Peer sent file X" message from file_transfer_manager._finalize_download()
        self.logger.debug(f"FILE_COMPLETE processed, transfer marked as completed: {transfer.filename}")

        return None
