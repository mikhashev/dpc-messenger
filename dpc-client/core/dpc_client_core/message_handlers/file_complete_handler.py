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

        # Notify UI
        await self.service.local_api.broadcast_event("file_transfer_complete", {
            "transfer_id": transfer_id,
            "node_id": sender_node_id,
            "filename": transfer.filename,
            "size_bytes": transfer.size_bytes,
            "direction": transfer.direction,
            "status": "completed"
        })

        # TODO: Update conversation history with attachment metadata
        # This will be done when we integrate with conversation_monitor.py
        # to support the new attachments[] field in message history

        return None
