"""Handlers for remote transcription commands."""

from typing import Dict, Any, Optional
from . import MessageHandler


class RemoteTranscriptionRequestHandler(MessageHandler):
    """Handles REMOTE_TRANSCRIPTION_REQUEST messages (peer requesting audio transcription)."""

    @property
    def command_name(self) -> str:
        return "REMOTE_TRANSCRIPTION_REQUEST"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle REMOTE_TRANSCRIPTION_REQUEST message.

        Peer is requesting this node to transcribe audio on their behalf.
        Transcription sharing firewall rules are checked before processing.

        Args:
            sender_node_id: Node ID of requester
            payload: Contains "request_id", "audio_base64", "mime_type", "model", "provider", "language", "task"
        """
        request_id = payload.get("request_id")
        audio_base64 = payload.get("audio_base64")
        mime_type = payload.get("mime_type")
        model = payload.get("model")
        provider = payload.get("provider")
        language = payload.get("language", "auto")
        task = payload.get("task", "transcribe")

        await self.service._handle_transcription_request(
            sender_node_id, request_id, audio_base64, mime_type, model, provider, language, task
        )
        return None


class RemoteTranscriptionResponseHandler(MessageHandler):
    """Handles REMOTE_TRANSCRIPTION_RESPONSE messages (peer responding with transcription result)."""

    @property
    def command_name(self) -> str:
        return "REMOTE_TRANSCRIPTION_RESPONSE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle REMOTE_TRANSCRIPTION_RESPONSE message.

        Resolves a pending transcription request future with the received result.

        Args:
            sender_node_id: Node ID of responder
            payload: Contains "request_id", "status", "text", "language", "duration_seconds", "provider", "error"
        """
        request_id = payload.get("request_id")
        status = payload.get("status")
        text = payload.get("text")
        error = payload.get("error")

        # Extract metadata
        language = payload.get("language")
        duration_seconds = payload.get("duration_seconds")
        provider = payload.get("provider")

        if request_id in self.service._pending_transcription_requests:
            future = self.service._pending_transcription_requests[request_id]
            if not future.done():
                if status == "success":
                    # Return dict with transcription result and metadata
                    result_data = {
                        "text": text,
                        "language": language,
                        "duration_seconds": duration_seconds,
                        "provider": provider
                    }
                    future.set_result(result_data)
                else:
                    future.set_exception(RuntimeError(error or "Remote transcription failed"))

        return None
