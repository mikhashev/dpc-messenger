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
                - text: str (optional) - what the sender wrote with a plain file
                - file_description: dict (optional, ADR-047 phase 1) - the author's
                    signed description; checked below after the firewall
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
        # What the sender wrote with a plain file: the text of the one message
        # recorded for it. Anything but a non-empty string is read as absent.
        text = payload.get("text")
        text = text if isinstance(text, str) and text.strip() else None
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

        # A group file lands in the folder its group id names, and the sender
        # chooses the id: `dpc-node-<another peer>`, `agent_001` or an upper-case
        # copy of a real group's id would put it in another conversation's store.
        # Not answered: no node that runs this code sends such an id.
        from ..managers.group_manager import is_valid_group_id
        if group_id and not is_valid_group_id(group_id):
            self.logger.warning(
                f"Ignoring offer {transfer_id} from {sender_node_id}: {group_id!r} is not a group id"
            )
            return None

        # A well-formed id is not yet a group: an unknown one, or one the
        # sender is not in, created a monitor and `conversations/<group>/files`
        # for whatever arrived (Zcode's review of card A-SYNCED-ATTACHMENT-PATH-
        # CAN-MAKE-THIS-NODE-SERVE-ANY-FILE-ON-ITS-DISK). The rule every group
        # handler keeps (`may_share_group`); dropped, not answered, as
        # GROUP_HISTORY_RESPONSE drops an answer from outside the roster.
        from .group_access import may_share_group
        if group_id and not may_share_group(
                getattr(self.service, "group_manager", None), group_id, sender_node_id):
            self.logger.warning(
                f"Ignoring offer {transfer_id} from {sender_node_id} for group {group_id}: "
                f"unknown group or sender is not a member"
            )
            return None

        file_transfer_manager = self.service.file_transfer_manager

        # An offer that answers this node's own FILE_REQUEST (managers/file_request.py):
        # the file of a record already here, which the user asked for.
        file_requests = getattr(file_transfer_manager, "file_requests", None)
        open_request = (file_requests.open_request_for_offer(sender_node_id, payload)
                        if file_requests is not None else None)
        if open_request is not None and open_request.transfer_id:
            # A second holder answered the same request: the first one's file is coming.
            from ..managers.file_transfer_manager import ALREADY_RECEIVED_REASON
            self.logger.info(f"Offer {transfer_id} answers request {open_request.request_id[:8]}, "
                             f"which another holder already serves; refused")
            await file_transfer_manager._send_file_cancel(
                sender_node_id, transfer_id, ALREADY_RECEIVED_REASON
            )
            return None
        if open_request is not None:
            # Only the file requested: its name, the record's size and the
            # record's content hash where the record has them. Otherwise a peer
            # that was asked could push any bytes and have the auto-accept glue
            # them onto the record (found by Zcode in review, 2026-11-19).
            # Refused here, never passed on as an ordinary offer.
            why = file_requests.offer_mismatch(open_request, payload)
            if why is not None:
                from ..managers.file_request import NOT_THE_REQUESTED_FILE_REASON
                self.logger.warning(f"Offer {transfer_id} from {sender_node_id} answers request "
                                    f"{open_request.request_id[:8]} with another file ({why}); refused")
                await file_transfer_manager._send_file_cancel(
                    sender_node_id, transfer_id, NOT_THE_REQUESTED_FILE_REASON
                )
                await file_requests.offer_mismatched(open_request, sender_node_id, why)
                return None

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
            if open_request is not None:
                await file_requests.offer_refused(open_request, str(denial_reason or "firewall_denied"))
            return None

        # A group file this node already has comes back on every relay of a
        # full mesh. Refused out loud, so the relaying node frees its upload:
        # an offer nobody answers holds one of its upload slots for good.
        # After the firewall, never before: a peer it refuses must not learn
        # from the answer whether this node holds a file. Before the dialog,
        # so a relay of a held file asks nobody "allow files from X?".
        # A requested file is let through: this node asked for it because the
        # copy it once had, or wrote, is gone from its disk.
        if open_request is None and group_id and file_transfer_manager.holds_group_file(
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

        # The author's signed description (ADR-047 phase 1), after the firewall
        # and before any answer: a description that does not hold, or an offer
        # that is not the file it describes, is refused; a requester asks its
        # next peer. No description is legacy and goes on as today.
        from ..signing import FILE_REJECTED, FILE_UNVERIFIED
        description, file_verdict, why = file_transfer_manager.offer_file_description(
            sender_node_id, payload)
        if file_verdict == FILE_REJECTED:
            from ..managers.file_transfer_manager import FILE_SIGNATURE_REJECTED_REASON
            self.logger.warning(f"Offer {transfer_id} of {filename} from {sender_node_id}: "
                                f"its file description is rejected ({why}); refused")
            await file_transfer_manager._send_file_cancel(
                sender_node_id, transfer_id, FILE_SIGNATURE_REJECTED_REASON
            )
            if open_request is not None:
                await file_requests.offer_mismatched(
                    open_request, sender_node_id, f"file description rejected: {why}")
            return None

        if description is None and open_request is not None:
            # A binding request answered by a holder that served no description
            # (it lacks the author's certificate): the offer is the described
            # file (`offer_mismatch`), and the transfer carries this node's own
            # verified description, so the bytes are checked against its hash
            # and chunks_hash at the end (found in the second review by a fresh
            # subagent, 2026-11-20).
            held = file_requests.held_description_for_offer(open_request, payload)
            if held is not None:
                from ..signing import FILE_VERIFIED
                description, file_verdict = dict(held), FILE_VERIFIED

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

        def add_group_fields(event_data):
            if open_request is not None:
                # The UI opens no accept dialog for a file its user asked for.
                event_data["request_id"] = open_request.request_id
            if group_id:
                event_data["group_id"] = group_id
                if author_node_id:
                    # The author as the offer claims it (unsigned). `node_id` is
                    # the hop: the node the dialog's answer is about, and the
                    # Active Transfers row's "via".
                    event_data["sender_node_id"] = author_node_id

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
            add_group_fields(event_data)
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
            add_group_fields(event_data)
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
            add_group_fields(event_data)
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
            if text:
                event_data["text"] = text  # shown in the accept dialog
            add_group_fields(event_data)
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
            text=text,
            group_id=group_id,            # Group ID for group file transfers (v0.19.0+)
            sender_node_id=author_node_id,
            message_id=message_id,
            awaiting_permission=awaiting_permission,
            file_description=description,
            file_verdict=file_verdict if description else None,
        )
        if open_request is not None:
            # The record the file completes is the requested one, by its id.
            transfer.request_id = open_request.request_id
            transfer.message_id = open_request.message_id
            # What the bytes must hash to once here: the record's hash, not the
            # one the offer declares (None when the record has none).
            transfer.requested_hash = open_request.file_hash
        file_transfer_manager.active_transfers[transfer_id] = transfer
        if open_request is not None:
            await file_requests.offer_taken(open_request, sender_node_id, transfer_id)

        if file_verdict == FILE_UNVERIFIED:
            # No certificate for the author here yet: ask the hop for it (W2).
            await file_transfer_manager.ask_for_certificate(
                description["sender_node_id"], sender_node_id)
            if (open_request is not None and not awaiting_permission
                    and file_requests.has_next_candidate(open_request)):
                # A requester with other peers to ask accepts only once the
                # certificate is here: a holder that withholds it must not keep
                # the file unverified when another holder can serve it checked.
                file_requests.accept_once_verified(open_request, sender_node_id, transfer)
                self.logger.info(f"Requested file {transfer_id} waits for its author's "
                                 f"certificate before it is accepted")
                return None

        # Every offer that is left waiting for a person below (the question, or the
        # accept button of an ordinary file) has a deadline: see begin_waiting_for_answer.
        # Auto-accept voice messages, images, and group files (v0.19.1+) - these are
        # automatically accepted without showing a dialog, similar to how text messages work.
        # Voice messages: Typically small (< 10MB), personal communication
        # Images (screenshots): Pasted from clipboard, shared in conversation context
        # Group files: Shared in group chat context, should flow like messages
        if (is_voice or is_image or group_id or open_request is not None) and not awaiting_permission:
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
            elif group_id:
                transfer_type = 'Group file'
            else:
                transfer_type = 'Requested file'
            self.logger.info(f"{transfer_type} transfer auto-accepted: {transfer_id}")
        elif awaiting_permission:
            self.logger.info(f"Transfer from {sender_node_id} waits for the user's permission: {transfer_id}")
            file_transfer_manager.begin_waiting_for_answer(transfer_id)
        else:
            transfer_type = 'File'
            self.logger.info(f"{transfer_type} transfer pending user acceptance: {transfer_id}")
            file_transfer_manager.begin_waiting_for_answer(transfer_id)

        return None
