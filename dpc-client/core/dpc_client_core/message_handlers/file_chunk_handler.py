"""Handler for FILE_CHUNK command - receiving file data chunks."""

from typing import Dict, Any, Optional
from . import MessageHandler


class FileChunkHandler(MessageHandler):
    """Handles FILE_CHUNK messages (incoming file data chunks)."""

    @property
    def command_name(self) -> str:
        return "FILE_CHUNK"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle FILE_CHUNK message.

        Flow:
        1. Verify transfer exists and is active
        2. Append chunk to file data
        3. Track progress
        4. When all chunks received → finalize download

        Args:
            sender_node_id: Node ID of sender
            payload: Contains transfer_id, chunk_index, total_chunks, data (base64)
        """
        transfer_id = payload.get("transfer_id")
        chunk_index = payload.get("chunk_index")
        total_chunks = payload.get("total_chunks")

        # Log progress (every 10 chunks to avoid spam)
        if chunk_index % 10 == 0:
            self.logger.debug(f"FILE_CHUNK {chunk_index + 1}/{total_chunks} from {sender_node_id}")

        # Delegate to FileTransferManager
        file_transfer_manager = self.service.file_transfer_manager
        await file_transfer_manager.handle_file_chunk(sender_node_id, payload)

        return None
