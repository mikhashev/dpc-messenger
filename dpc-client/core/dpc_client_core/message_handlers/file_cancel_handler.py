"""Handler for FILE_CANCEL command - file transfer cancelled by peer."""

from typing import Dict, Any, Optional
from . import MessageHandler


class FileCancelHandler(MessageHandler):
    """Handles FILE_CANCEL messages (transfer cancelled or rejected)."""

    @property
    def command_name(self) -> str:
        return "FILE_CANCEL"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle FILE_CANCEL message.

        Reasons for cancellation:
        - user_cancelled: User manually cancelled transfer
        - firewall_denied: Receiver's firewall rejected transfer
        - hash_mismatch: File hash verification failed
        - timeout: Transfer timed out
        - send_error: Error occurred during sending

        Flow:
        1. Mark transfer as cancelled
        2. Cleanup resources
        3. Notify UI

        Args:
            sender_node_id: Node ID of peer
            payload: Contains transfer_id, reason
        """
        transfer_id = payload.get("transfer_id")
        reason = payload.get("reason", "unknown")

        self.logger.info(f"FILE_CANCEL from {sender_node_id} for transfer {transfer_id} (reason: {reason})")

        # Delegate to FileTransferManager
        file_transfer_manager = self.service.file_transfer_manager
        await file_transfer_manager.handle_file_cancel(sender_node_id, payload)

        # Notify UI
        await self.service.local_api.broadcast_event("file_transfer_cancelled", {
            "transfer_id": transfer_id,
            "node_id": sender_node_id,
            "reason": reason,
            "status": "cancelled"
        })

        return None
