"""Handler for VOICE_TRANSCRIPTION command - distributed voice message transcription."""

from typing import Dict, Any, Optional
from . import MessageHandler
from .group_access import off_the_roster, refuse_group_access


class VoiceTranscriptionHandler(MessageHandler):
    """Handles VOICE_TRANSCRIPTION messages (peer sharing transcription result)."""

    @property
    def command_name(self) -> str:
        return "VOICE_TRANSCRIPTION"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle VOICE_TRANSCRIPTION message.

        Flow:
        1. Extract transcription data
        2. Check if transfer_id already has transcription (deduplication)
        3. Store transcription locally
        4. Update conversation history with transcription
        5. Notify UI

        Args:
            sender_node_id: Node ID of transcriber
            payload: Contains transfer_id, transcription_text, provider, etc.
        """
        transfer_id = payload.get("transfer_id")
        transcription_text = payload.get("transcription_text", "")
        transcriber_node_id = payload.get("transcriber_node_id")
        provider = payload.get("provider", "unknown")
        confidence = payload.get("confidence", 0.0)
        language = payload.get("language", "unknown")
        timestamp = payload.get("timestamp")
        remote_provider_node_id = payload.get("remote_provider_node_id")  # Optional
        group_id = payload.get("group_id")  # Present for group voice messages (v0.21.0+)

        if group_id and not await self._may_annotate_group_voice(sender_node_id, payload):
            return None

        # 1.5. Validate transcription is not empty (v0.13.3+ - prevent empty/malicious transcriptions)
        transcription_text = transcription_text.strip()
        if not transcription_text:
            self.logger.warning(
                f"Received empty transcription from {sender_node_id} for {transfer_id}, ignoring"
            )
            return None  # Don't store empty transcriptions

        self.logger.info(
            f"VOICE_TRANSCRIPTION from {sender_node_id} for transfer {transfer_id} "
            f"(provider: {provider}, language: {language}, confidence: {confidence})"
        )

        # 1. Check for duplicate (already have successful transcription for this transfer)
        existing = self.service._voice_transcriptions.get(transfer_id)
        if existing:
            # Only ignore if existing transcription was successful
            if existing.get("success", False):
                self.logger.debug(f"Already have successful transcription for {transfer_id}, ignoring duplicate")
                return None
            else:
                # Replace failed/partial transcription with peer's successful one
                self.logger.info(f"Replacing failed transcription for {transfer_id} with peer's transcription")

        # 2. Store transcription locally
        transcription_data = {
            "text": transcription_text,
            # One message, one transfer per recipient: the file name is what
            # names the message on a node that holds no transfer for it.
            "filename": payload.get("filename"),
            "transcriber_node_id": transcriber_node_id,
            "provider": provider,
            "confidence": confidence,
            "language": language,
            "timestamp": timestamp,
            "success": True  # Peer's transcription is considered successful
        }

        # Add remote provider node if present
        if remote_provider_node_id:
            transcription_data["remote_provider_node_id"] = remote_provider_node_id

        self.service._voice_transcriptions[transfer_id] = transcription_data

        # 3. Update conversation history with transcription
        # Find the voice message in conversation history and attach transcription
        # For group voice messages, key on group_id not sender_node_id
        monitor_key = group_id if group_id else sender_node_id
        conversation_monitor = self.service.conversation_monitors.get(monitor_key)
        if conversation_monitor:
            # A. Update message_history attachments (existing code)
            for message in conversation_monitor.message_history:
                attachments = message.get("attachments", [])
                for attachment in attachments:
                    if self._names_voice(attachment, transfer_id, transcription_data.get("filename")):
                        # Add transcription to attachment
                        attachment["transcription"] = transcription_data
                        self.logger.debug(f"Attached transcription to voice message in conversation history")
                        break

            # B. Also update Message objects in extraction buffers (v0.14.0 fix)
            transcription_text = transcription_data.get("text", "")
            if transcription_text:
                # Find and update Message objects in both buffers using transfer_id
                for message_list in [conversation_monitor.message_buffer, conversation_monitor.full_conversation]:
                    for msg_obj in message_list:
                        if hasattr(msg_obj, 'attachment_transfer_id') and msg_obj.attachment_transfer_id == transfer_id:
                            # Replace placeholder with actual transcription
                            # Format: "Voice message: [transcription text]"
                            msg_obj.text = f"Voice message: {transcription_text}"
                            self.logger.debug(f"Updated Message object text with transcription: {transfer_id}")
                            break

        # 4. Broadcast to UI
        await self.service.local_api.broadcast_event("voice_transcription_received", {
            "transfer_id": transfer_id,
            "node_id": sender_node_id,
            **transcription_data
        })

        # 5. Relay to group members that can't reach the transcriber directly (star topology)
        if group_id:
            dedup_key = f"vt:{transfer_id}"
            if dedup_key not in self.service._processed_message_ids:
                self.service._processed_message_ids.add(dedup_key)
                await self._relay_to_group("VOICE_TRANSCRIPTION", payload, sender_node_id, group_id)

        self.logger.debug(f"Processed VOICE_TRANSCRIPTION for {transfer_id}")
        return None

    @staticmethod
    def _names_voice(attachment: Dict[str, Any], transfer_id, filename) -> bool:
        """Whether this attachment is the voice message a transcription names.

        The sender's own group record carries no transfer id: one message
        becomes one transfer per recipient. The file name is minted once and
        travels with the offer. An absent key matches nothing.
        """
        if not isinstance(attachment, dict) or attachment.get("type") != "voice":
            return False
        return bool((transfer_id and attachment.get("transfer_id") == transfer_id)
                    or (filename and attachment.get("filename") == filename))

    async def _may_annotate_group_voice(self, sender_node_id: str, payload: Dict[str, Any]) -> bool:
        """Whether a group transcription may be stored, shown and relayed.

        A transcription is text put into a group, so it is a member's
        (`group_access.off_the_roster`): the node handing it over and the
        transcriber it names must both be on our roster. Any member that holds
        the voice message may transcribe it (specs/dptp_v1.md 3.12), and only a
        voice message this group's history holds can be annotated.
        """
        group_id = payload.get("group_id")
        transfer_id = payload.get("transfer_id")
        group = self.service.group_manager.get_group(group_id)
        if group is None:
            self.logger.warning(
                "Dropped VOICE_TRANSCRIPTION %s from %s: this node does not hold group %s",
                str(transfer_id)[:12], sender_node_id[:20], str(group_id)[:20],
            )
            return False
        transcriber = payload.get("transcriber_node_id") or sender_node_id
        outsider = off_the_roster(group, sender_node_id, transcriber)
        if outsider == sender_node_id:
            await refuse_group_access(
                self.service.p2p_manager, sender_node_id, group_id, "VOICE_TRANSCRIPTION", self.logger,
            )
            return False
        if outsider:
            self.logger.warning(
                "Dropped VOICE_TRANSCRIPTION %s in group %s relayed by %s: its transcriber %s is "
                "not a member", str(transfer_id)[:12], group_id, sender_node_id[:20], outsider[:20],
            )
            return False
        monitor = self.service.conversation_monitors.get(group_id)
        if monitor is None and hasattr(self.service, "_get_or_create_conversation_monitor"):
            monitor = self.service._get_or_create_conversation_monitor(group_id)
        filename = payload.get("filename")
        held = any(
            self._names_voice(attachment, transfer_id, filename)
            for message in (getattr(monitor, "message_history", None) or ())
            for attachment in (message.get("attachments") or ())
        )
        if not held:
            self.logger.warning(
                "Dropped VOICE_TRANSCRIPTION %s in group %s from %s: this node holds no voice "
                "message with that transfer id or file name (%s)",
                str(transfer_id)[:12], group_id, sender_node_id[:20], filename,
            )
            return False
        return True
