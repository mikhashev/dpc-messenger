"""Handler for FILE_OFFER command with image/voice/file metadata - P2P attachment transfers."""

from typing import Dict, Any, Optional
from pathlib import Path
from . import MessageHandler


class FileOfferHandler(MessageHandler):
    """
    Handles FILE_OFFER messages for images, voice, and regular file transfers.

    This handler processes FILE_OFFER commands and provides specialized handling for:
    - Images: Detected by mime_type (image/*) with image_metadata
    - Voice messages: Detected by mime_type (audio/*) with voice_metadata
    - Regular files: Standard file transfer handling (no metadata)

    Events broadcast:
    - image_offer_received: For images (with thumbnail preview)
    - voice_offer_received: For voice messages (with duration)
    - file_transfer_offered: For regular files
    """

    @property
    def command_name(self) -> str:
        return "FILE_OFFER"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle FILE_OFFER message (images, voice, and regular files).

        For images (mime_type=image/* with image_metadata):
        1. Check firewall permission
        2. Extract thumbnail from image_metadata
        3. Broadcast image_offer_received event to UI
        4. Create pending download transfer

        For voice messages (mime_type=audio/* with voice_metadata):
        1. Check firewall permission
        2. Extract duration from voice_metadata
        3. Broadcast voice_offer_received event to UI
        4. Create pending download transfer

        For regular files (no image_metadata or voice_metadata):
        1. Check firewall permission
        2. Broadcast file_transfer_offered event to UI
        3. Create pending download transfer

        Args:
            sender_node_id: Node ID of sender
            payload: Contains:
                - transfer_id: str
                - filename: str
                - size_bytes: int
                - hash: str
                - mime_type: str
                - chunk_size: int
                - chunk_hashes: dict (optional, v0.11.1)
                - image_metadata: dict (optional, for images) v0.12.0+
                    - dimensions: {width: int, height: int}
                    - thumbnail_base64: str (data URL)
                    - source: str (e.g., "clipboard")
                    - captured_at: str (ISO timestamp)
                - voice_metadata: dict (optional, for voice) v0.13.0+
                    - duration_seconds: float
                    - sample_rate: int
                    - channels: int
                    - codec: str (e.g., "opus")
                    - recorded_at: str (ISO timestamp)
        """
        transfer_id = payload.get("transfer_id")
        filename = payload.get("filename")
        size_bytes = payload.get("size_bytes")
        hash_value = payload.get("hash")
        mime_type = payload.get("mime_type")
        chunk_size = payload.get("chunk_size")
        chunk_hashes = payload.get("chunk_hashes")
        image_metadata = payload.get("image_metadata")
        voice_metadata = payload.get("voice_metadata")
        group_id = payload.get("group_id")  # v0.19.0+: Group file transfers
        # A group file's identity, kept by every relay. Unsigned: it decides
        # whether this node takes the copy, never who the file is credited to.
        from ..managers.file_transfer_manager import offer_identity_field
        message_id = offer_identity_field(payload.get("message_id")) if group_id else None
        author_node_id = offer_identity_field(payload.get("sender_node_id")) if message_id else None

        # Detect transfer type
        is_image = mime_type and mime_type.startswith("image/") and image_metadata
        is_voice = mime_type and mime_type.startswith("audio/") and voice_metadata

        if is_image:
            self.logger.info(f"IMAGE_OFFER from {sender_node_id}: {filename} ({size_bytes} bytes)")
        elif is_voice:
            self.logger.info(f"VOICE_OFFER from {sender_node_id}: {filename} ({size_bytes} bytes)")
        else:
            self.logger.info(f"FILE_OFFER from {sender_node_id}: {filename} ({size_bytes} bytes)")

        file_transfer_manager = self.service.file_transfer_manager

        # Check firewall permission. The outcome is a word compared by value —
        # testing a tuple for truth is how every allow-list, size cap and MIME
        # filter once came to be unreachable on the receiving side.
        from ..managers.file_transfer_manager import FILE_TRANSFER_ALLOW, FILE_TRANSFER_ASK
        decision = await file_transfer_manager._file_transfer_decision(
            sender_node_id, filename, size_bytes, mime_type
        )
        denial_reason = decision.reason
        # A sender no rule names: every kind of offer waits for the user's answer
        awaiting_permission = decision.outcome == FILE_TRANSFER_ASK

        if decision.outcome not in (FILE_TRANSFER_ALLOW, FILE_TRANSFER_ASK):
            transfer_type = 'Image' if is_image else 'Voice' if is_voice else 'File'
            self.logger.warning(
                f"{transfer_type} transfer denied by firewall from {sender_node_id}: {denial_reason}"
            )
            # Send FILE_CANCEL (rejected). `denial_reason` is the prose behind the
            # `firewall_denied` word (rule, size limit or MIME type), so the sender
            # can tell the user why; an older peer ignores the field.
            cancel_payload = {
                "transfer_id": transfer_id,
                "reason": "firewall_denied"
            }
            if denial_reason:
                cancel_payload["denial_reason"] = str(denial_reason)
            await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
                "command": "FILE_CANCEL",
                "payload": cancel_payload
            })
            return None

        # A group file this node already has comes back on every relay of a
        # full mesh. Refused out loud, so the relaying node frees its upload:
        # an offer nobody answers holds one of its upload slots for good.
        # After the firewall, never before: a peer it refuses must not learn
        # from the answer whether this node holds a file. Before the dialog,
        # so a relay of a held file asks nobody "allow files from X?".
        if group_id and file_transfer_manager.holds_group_file(
                group_id, message_id, author_node_id, hash_value):
            from ..managers.file_transfer_manager import ALREADY_RECEIVED_REASON
            self.logger.info(
                f"Group file {filename} from {sender_node_id} is already here "
                f"(message {message_id or 'by hash'}); offer {transfer_id} refused"
            )
            await file_transfer_manager._send_file_cancel(
                sender_node_id, transfer_id, ALREADY_RECEIVED_REASON
            )
            return None

        # Deduplicate: Check for existing pending transfer
        for existing_transfer_id, existing_transfer in file_transfer_manager.active_transfers.items():
            if (existing_transfer.node_id == sender_node_id and
                existing_transfer.filename == filename and
                existing_transfer.size_bytes == size_bytes and
                existing_transfer.direction == "download" and
                existing_transfer.status.value == "pending"):
                transfer_type = 'IMAGE' if is_image else 'VOICE' if is_voice else 'FILE'
                self.logger.warning(
                    f"Ignoring duplicate {transfer_type}_OFFER from {sender_node_id} "
                    f"for {filename} (already have pending transfer {existing_transfer_id}, "
                    f"new offer is {transfer_id})"
                )
                return None

        # Get sender name for UI display
        sender_name = self.service.peer_metadata.get(sender_node_id, {}).get("name") or sender_node_id

        # Broadcast appropriate event based on type. An asked offer goes out as
        # the dialog's event whatever it carries, naming its kind.
        if awaiting_permission:
            event_data = {
                "transfer_id": transfer_id,
                "node_id": sender_node_id,
                "sender_name": sender_name,
                "filename": filename,
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / (1024 * 1024), 2),
                "mime_type": mime_type,
                "hash": hash_value,
                "awaiting_permission": True,
                "kind": "image" if is_image else "voice" if is_voice else "file",
            }
            if group_id:
                event_data["group_id"] = group_id
                if author_node_id:
                    # The author as the offer claims it (unsigned). `node_id` is
                    # the hop, the node the dialog's answer is about.
                    event_data["sender_node_id"] = author_node_id
            await self.service.local_api.broadcast_event("file_transfer_offered", event_data)
        elif is_image:
            # Image offer: include thumbnail and dimensions
            event_data = {
                "transfer_id": transfer_id,
                "node_id": sender_node_id,
                "sender_name": sender_name,
                "filename": filename,
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / (1024 * 1024), 2),
                "mime_type": mime_type,
                "hash": hash_value,
                "dimensions": image_metadata.get("dimensions", {}),
                "thumbnail": image_metadata.get("thumbnail_base64", ""),
                "source": image_metadata.get("source", "unknown"),
                "captured_at": image_metadata.get("captured_at", "")
            }
            if group_id:
                event_data["group_id"] = group_id
            await self.service.local_api.broadcast_event("image_offer_received", event_data)
        elif is_voice:
            # Voice offer: include duration and codec info
            event_data = {
                "transfer_id": transfer_id,
                "node_id": sender_node_id,
                "sender_name": sender_name,
                "filename": filename,
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / (1024 * 1024), 2),
                "mime_type": mime_type,
                "hash": hash_value,
                "duration_seconds": voice_metadata.get("duration_seconds", 0),
                "sample_rate": voice_metadata.get("sample_rate", 48000),
                "channels": voice_metadata.get("channels", 1),
                "codec": voice_metadata.get("codec", "unknown"),
                "recorded_at": voice_metadata.get("recorded_at", "")
            }
            if group_id:
                event_data["group_id"] = group_id
            await self.service.local_api.broadcast_event("voice_offer_received", event_data)
        else:
            # Regular file offer: standard notification
            event_data = {
                "transfer_id": transfer_id,
                "node_id": sender_node_id,
                "sender_name": sender_name,
                "filename": filename,
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / (1024 * 1024), 2),
                "mime_type": mime_type,
                "hash": hash_value
            }
            if group_id:
                event_data["group_id"] = group_id
            await self.service.local_api.broadcast_event("file_transfer_offered", event_data)

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
            retry_count={},  # v0.11.1: Track retry attempts per chunk
            image_metadata=image_metadata,  # Store image metadata for chat display (v0.12.0+)
            voice_metadata=voice_metadata,  # Store voice metadata for chat display (v0.13.0+)
            group_id=group_id,             # Group ID for group file transfers (v0.19.0+)
            sender_node_id=author_node_id,
            message_id=message_id,
            awaiting_permission=awaiting_permission,
        )
        file_transfer_manager.active_transfers[transfer_id] = transfer

        # Every offer that is left waiting for a person below (the question, or the
        # accept button of an ordinary file) has a deadline: see begin_waiting_for_answer.
        # Auto-accept voice messages, images, and group files (v0.19.1+) - these are
        # automatically accepted without showing a dialog, similar to how text messages work.
        # Voice messages: Typically small (< 10MB), personal communication
        # Images (screenshots): Pasted from clipboard, shared in conversation context
        # Group files: Shared in group chat context, should flow like messages
        if (is_voice or is_image or group_id) and not awaiting_permission:
            # Send FILE_ACCEPT to peer immediately to start the download
            await self.service.p2p_manager.send_message_to_peer(sender_node_id, {
                "command": "FILE_ACCEPT",
                "payload": {"transfer_id": transfer_id}
            })
            # Update status to TRANSFERRING so UI refresh doesn't show acceptance dialog
            transfer.status = TransferStatus.TRANSFERRING
            # From here a silence with chunks missing is noticed and re-asked
            file_transfer_manager.begin_receiving(transfer_id)
            if is_voice:
                transfer_type = 'Voice'
            elif is_image:
                transfer_type = 'Image'
            else:
                transfer_type = 'Group file'
            self.logger.info(f"{transfer_type} transfer auto-accepted: {transfer_id}")
        elif awaiting_permission:
            self.logger.info(f"Transfer from {sender_node_id} waits for the user's permission: {transfer_id}")
            file_transfer_manager.begin_waiting_for_answer(transfer_id)
        else:
            transfer_type = 'File'
            self.logger.info(f"{transfer_type} transfer pending user acceptance: {transfer_id}")
            file_transfer_manager.begin_waiting_for_answer(transfer_id)

        return None
