"""Handlers for remote inference commands (compute sharing)."""

from typing import Dict, Any, Optional
from . import MessageHandler


class RemoteInferenceRequestHandler(MessageHandler):
    """Handles REMOTE_INFERENCE_REQUEST messages (peer requesting AI inference)."""

    @property
    def command_name(self) -> str:
        return "REMOTE_INFERENCE_REQUEST"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle REMOTE_INFERENCE_REQUEST message.

        Peer is requesting this node to run AI inference on their behalf.
        Compute sharing firewall rules are checked before processing.

        Args:
            sender_node_id: Node ID of requester
            payload: Contains "request_id", "prompt", "model", "provider", "images" (optional)
        """
        request_id = payload.get("request_id")
        prompt = payload.get("prompt")
        model = payload.get("model")
        provider = payload.get("provider")
        images = payload.get("images")  # Phase 2: Remote Vision support

        await self.service._handle_inference_request(
            sender_node_id, request_id, prompt, model, provider, images
        )
        return None


class RemoteInferenceResponseHandler(MessageHandler):
    """Handles REMOTE_INFERENCE_RESPONSE messages (peer responding with inference result)."""

    @property
    def command_name(self) -> str:
        return "REMOTE_INFERENCE_RESPONSE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle REMOTE_INFERENCE_RESPONSE message.

        Resolves a pending inference request future with the received result.

        Args:
            sender_node_id: Node ID of responder
            payload: Contains "request_id", "status", "response", "error", and token metadata
        """
        request_id = payload.get("request_id")
        status = payload.get("status")
        response = payload.get("response")
        error = payload.get("error")

        # Extract token metadata
        tokens_used = payload.get("tokens_used")
        model_max_tokens = payload.get("model_max_tokens")
        prompt_tokens = payload.get("prompt_tokens")
        response_tokens = payload.get("response_tokens")

        if request_id in self.service._pending_inference_requests:
            future = self.service._pending_inference_requests[request_id]
            if not future.done():
                if status == "success":
                    # Return dict with response and token metadata
                    result_data = {
                        "response": response,
                        "tokens_used": tokens_used,
                        "model_max_tokens": model_max_tokens,
                        "prompt_tokens": prompt_tokens,
                        "response_tokens": response_tokens
                    }
                    future.set_result(result_data)
                else:
                    future.set_exception(RuntimeError(error or "Remote inference failed"))

        return None
