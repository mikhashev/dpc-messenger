"""Handler for VOICE_TRANSCRIPTION command - distributed voice message transcription."""

from typing import Dict, Any, Optional
from . import MessageHandler


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
        conversation_monitor = self.service.conversation_monitors.get(sender_node_id)
        if conversation_monitor:
            # Iterate through message_history to find the voice attachment
            for message in conversation_monitor.message_history:
                attachments = message.get("attachments", [])
                for attachment in attachments:
                    if attachment.get("type") == "voice" and attachment.get("transfer_id") == transfer_id:
                        # Add transcription to attachment
                        attachment["transcription"] = transcription_data
                        self.logger.debug(f"Attached transcription to voice message in conversation history")
                        break

        # 4. Broadcast to UI
        await self.service.local_api.broadcast_event("voice_transcription_received", {
            "transfer_id": transfer_id,
            "node_id": sender_node_id,
            **transcription_data
        })

        self.logger.debug(f"Processed VOICE_TRANSCRIPTION for {transfer_id}")
        return None
