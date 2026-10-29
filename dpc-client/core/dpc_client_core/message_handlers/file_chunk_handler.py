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
        2. Write the chunk at its offset in the partial file (no bytes kept in memory)
        3. Track progress (the manager sends file_transfer_progress on a time cadence)
        4. When all chunks received → finalize download

        Args:
            sender_node_id: Node ID of sender
            payload: Contains transfer_id, chunk_index, total_chunks, data (base64)
        """
        transfer_id = payload.get("transfer_id")
        chunk_index = payload.get("chunk_index")
        total_chunks = payload.get("total_chunks")

        # Delegate to FileTransferManager
        file_transfer_manager = self.service.file_transfer_manager
        await file_transfer_manager.handle_file_chunk(sender_node_id, payload)

        # The log keeps a 5 % cadence: every 10 chunks was 12 700 lines for an 8 GB file
        transfer = file_transfer_manager.active_transfers.get(transfer_id)
        if transfer and isinstance(chunk_index, int):
            if chunk_index % max(1, transfer.total_chunks // 20) == 0 or chunk_index == transfer.total_chunks - 1:
                progress = (len(transfer.chunks_received) / transfer.total_chunks) * 100
                self.logger.debug(f"FILE_CHUNK {chunk_index + 1}/{total_chunks} from {sender_node_id} ({progress:.1f}%)")

        return None
