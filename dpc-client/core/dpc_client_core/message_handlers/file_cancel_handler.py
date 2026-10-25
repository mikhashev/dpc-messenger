"""Handler for FILE_CANCEL command - file transfer cancelled by peer."""

from typing import Dict, Any, Optional
from . import MessageHandler
from ..managers.file_transfer_manager import ALREADY_RECEIVED_REASON


class FileCancelHandler(MessageHandler):
    """Handles FILE_CANCEL messages (transfer cancelled or rejected)."""

    @property
    def command_name(self) -> str:
        return "FILE_CANCEL"

    # A peer-supplied string that ends up on screen: bound it.
    MAX_DENIAL_REASON_CHARS = 300

    @classmethod
    def _read_denial_reason(cls, payload: Dict[str, Any]) -> Optional[str]:
        value = payload.get("denial_reason")
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()[:cls.MAX_DENIAL_REASON_CHARS]

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle FILE_CANCEL message.

        Reasons for cancellation:
        - user_cancelled: User manually cancelled transfer
        - firewall_denied: Receiver's firewall rejected transfer
        - hash_mismatch: File hash verification failed
        - timeout: Transfer timed out
        - send_error: Error occurred during sending
        - already_received: the peer already has this group file; not shown
        - no_answer: the offer expired unanswered (the receiver's person did not
          answer in time, or the sender released it); the UI words it as such,
          not as a failure

        Flow:
        1. Mark transfer as cancelled
        2. Cleanup resources
        3. Notify UI

        Args:
            sender_node_id: Node ID of peer
            payload: Contains transfer_id, reason, and optionally denial_reason
                (the receiver's prose for a firewall_denied; absent from older peers)
        """
        transfer_id = payload.get("transfer_id")
        reason = payload.get("reason", "unknown")
        denial_reason = self._read_denial_reason(payload)

        self.logger.info(f"FILE_CANCEL from {sender_node_id} for transfer {transfer_id} (reason: {reason})")

        # Get transfer info BEFORE deletion (for UI notification)
        file_transfer_manager = self.service.file_transfer_manager
        transfer = file_transfer_manager.active_transfers.get(transfer_id)

        # Delegate to FileTransferManager (this will delete the transfer)
        await file_transfer_manager.handle_file_cancel(sender_node_id, payload)

        if reason == ALREADY_RECEIVED_REASON:
            # The peer has the file: nothing failed, so nothing is shown. The
            # offer never reached FILE_ACCEPT, so the UI holds no entry for it.
            return None

        # Broadcast cancellation event to UI
        event = {
            "transfer_id": transfer_id,
            "node_id": sender_node_id,
            "filename": transfer.filename if transfer else "unknown",
            "direction": transfer.direction if transfer else "unknown",
            "reason": reason,
            "status": "cancelled"
        }
        if denial_reason:
            event["denial_reason"] = denial_reason
        if transfer and transfer.group_id:
            event["group_id"] = transfer.group_id
        await self.service.local_api.broadcast_event("file_transfer_cancelled", event)

        return None
