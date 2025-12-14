"""Handler for FILE_CHUNK_RETRY command - retry failed chunks (v0.11.1)."""

from typing import Dict, Any, Optional
from . import MessageHandler


class FileChunkRetryHandler(MessageHandler):
    """Handles FILE_CHUNK_RETRY messages (request to resend specific chunk)."""

    @property
    def command_name(self) -> str:
        return "FILE_CHUNK_RETRY"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle FILE_CHUNK_RETRY message.

        Flow:
        1. Validate transfer exists and is an upload
        2. Read specific chunk from file
        3. Resend chunk via FILE_CHUNK message

        Args:
            sender_node_id: Node ID of receiver requesting retry
            payload: Contains transfer_id, chunk_index
        """
        transfer_id = payload.get("transfer_id")
        chunk_index = payload.get("chunk_index")

        self.logger.info(f"FILE_CHUNK_RETRY from {sender_node_id}: transfer {transfer_id}, chunk {chunk_index}")

        # Delegate to file_transfer_manager
        file_transfer_manager = self.service.file_transfer_manager
        await file_transfer_manager.handle_file_chunk_retry(sender_node_id, payload)

        return None
