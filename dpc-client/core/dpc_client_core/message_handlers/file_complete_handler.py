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

        # Get transfer info BEFORE calling handle_file_complete (which deletes it)
        file_transfer_manager = self.service.file_transfer_manager
        transfer = file_transfer_manager.active_transfers.get(transfer_id)
        if not transfer:
            self.logger.warning(f"FILE_COMPLETE for unknown transfer: {transfer_id}")
            return None

        # Delegate to FileTransferManager (will cleanup/delete the transfer)
        await file_transfer_manager.handle_file_complete(sender_node_id, payload)

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

        # Add to conversation history and broadcast chat message
        # For sender (upload): Show "You sent file X" now that transfer succeeded
        # For receiver (download): Already handled in file_transfer_manager._finalize_download()

        if transfer.direction == "upload":
            # Sender side: Broadcast "You sent file X" message now that receiver accepted and completed
            import hashlib
            import time
            message_id = hashlib.sha256(
                f"{self.service.p2p_manager.node_id}:file-send:{transfer_id}:{int(time.time() * 1000)}".encode()
            ).hexdigest()[:16]

            size_mb = round(transfer.size_bytes / (1024 * 1024), 2)

            # Detect if this is an image or voice transfer
            is_image = (transfer.mime_type and transfer.mime_type.startswith("image/")
                       and transfer.image_metadata is not None)
            is_voice = transfer.voice_metadata is not None

            # Build attachment
            attachment = {
                "type": "image" if is_image else ("voice" if is_voice else "file"),
                "filename": transfer.filename,
                "size_bytes": transfer.size_bytes,
                "size_mb": size_mb,
                "hash": transfer.hash,
                "mime_type": transfer.mime_type,
                "transfer_id": transfer_id,
                "status": "completed"
            }

            # Add image-specific fields
            if is_image:
                # Only include file_path if file still exists (not deleted by privacy settings)
                if transfer.file_path and transfer.file_path.exists():
                    attachment["file_path"] = str(transfer.file_path)
                if transfer.image_metadata:
                    attachment["dimensions"] = transfer.image_metadata.get("dimensions", {})
                    attachment["thumbnail"] = transfer.image_metadata.get("thumbnail_base64", "")

            # Add voice-specific fields (v0.13.0+)
            if is_voice:
                # Voice messages always need file_path for playback
                if transfer.file_path and transfer.file_path.exists():
                    attachment["file_path"] = str(transfer.file_path)
                if transfer.voice_metadata:
                    attachment["voice_metadata"] = transfer.voice_metadata

            # Extract text caption from image_metadata if available
            caption_text = ""
            if is_image and transfer.image_metadata:
                caption_text = transfer.image_metadata.get("text", "")

            await self.service.local_api.broadcast_event("new_p2p_message", {
                "sender_node_id": "user",
                "sender_name": "You",
                "text": caption_text,  # User's caption (empty if not provided)
                "message_id": message_id,
                "attachments": [attachment]
            })

            # Add to conversation history (SENDER SIDE)
            # Create monitor if it doesn't exist (in case user sends voice/file before making AI query)
            conversation_monitor = self.service._get_or_create_conversation_monitor(sender_node_id)
            if conversation_monitor:
                file_type = 'screenshot' if is_image else ('voice message' if is_voice else 'file')
                message_content = f"Sent {file_type}: {transfer.filename} ({size_mb} MB)"
                conversation_monitor.add_message("user", message_content, [attachment])
                self.logger.debug(f"Added sent {file_type} to conversation history: {transfer.filename}")

            # Auto-transcribe if sender_transcribes enabled (v0.13.2+)
            if is_voice:
                import asyncio
                asyncio.create_task(
                    self.service._maybe_transcribe_voice_message(
                        transfer_id=transfer_id,
                        node_id=sender_node_id,
                        file_path=transfer.file_path,
                        voice_metadata=transfer.voice_metadata,
                        is_sender=True
                    )
                )

        # Auto-transcribe voice messages on download (v0.13.2+)
        if transfer.direction == "download" and is_voice:
            import asyncio
            asyncio.create_task(
                self.service._maybe_transcribe_voice_message(
                    transfer_id=transfer_id,
                    node_id=sender_node_id,
                    file_path=transfer.file_path,
                    voice_metadata=transfer.voice_metadata,
                    is_sender=False
                )
            )

        self.logger.debug(f"FILE_COMPLETE processed, transfer marked as completed: {transfer.filename}")

        return None
