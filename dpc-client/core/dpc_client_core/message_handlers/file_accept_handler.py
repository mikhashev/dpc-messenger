"""Handler for FILE_ACCEPT command - receiver accepted file transfer."""

from typing import Dict, Any, Optional
from . import MessageHandler


class FileAcceptHandler(MessageHandler):
    """Handles FILE_ACCEPT messages (receiver accepted transfer)."""

    @property
    def command_name(self) -> str:
        return "FILE_ACCEPT"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle FILE_ACCEPT message.

        Flow:
        1. Verify transfer exists and is pending
        2. Start sending file chunks

        Args:
            sender_node_id: Node ID of receiver
            payload: Contains transfer_id
        """
        transfer_id = payload.get("transfer_id")

        self.logger.info(f"FILE_ACCEPT from {sender_node_id} for transfer {transfer_id}")

        # Delegate to FileTransferManager
        file_transfer_manager = self.service.file_transfer_manager
        await file_transfer_manager.handle_file_accept(sender_node_id, transfer_id)

        return None
