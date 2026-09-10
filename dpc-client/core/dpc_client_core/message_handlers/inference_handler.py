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
        # What the peer asked to spend on thinking. The host clamps it; absent
        # means it did not choose.
        reasoning_effort = payload.get("reasoning_effort")

        await self.service._handle_inference_request(
            sender_node_id, request_id, prompt, model, provider, images,
            reasoning_effort,
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

        # Extract model and provider metadata (v0.12.0+)
        model = payload.get("model")
        provider = payload.get("provider")

        # Extract thinking fields (v1.4+)
        thinking = payload.get("thinking")
        thinking_tokens = payload.get("thinking_tokens")

        # What the call cost the host and under which billing model, when the
        # host counted it (v1.7). Absent stays absent: a zero would read as free.
        cost_usd = payload.get("cost_usd")
        billing = payload.get("billing")

        if request_id in self.service._pending_inference_requests:
            future = self.service._pending_inference_requests[request_id]
            if not future.done():
                if status == "success":
                    # Return dict with response, token, model, and thinking metadata
                    result_data = {
                        # The wire id: the requester's usage row joins the host's on it.
                        "request_id": request_id,
                        "response": response,
                        "tokens_used": tokens_used,
                        "model_max_tokens": model_max_tokens,
                        "prompt_tokens": prompt_tokens,
                        "response_tokens": response_tokens,
                        "model": model,
                        "provider": provider,
                        "thinking": thinking,
                        "thinking_tokens": thinking_tokens,
                    }
                    if cost_usd is not None:
                        result_data["cost_usd"] = cost_usd
                    if billing is not None:
                        result_data["billing"] = billing
                    future.set_result(result_data)
                else:
                    future.set_exception(RuntimeError(error or "Remote inference failed"))
            else:
                self.logger.warning(
                    "Remote inference answer for %s arrived after its future was settled "
                    "(peer %s, status %s) — discarded",
                    request_id, sender_node_id, status,
                )
        else:
            # The requester has already given up and the host has already paid.
            # This used to return in silence, so the cost of every abandoned
            # remote call was invisible on both sides of the wire.
            self.logger.warning(
                "Remote inference answer discarded: request %s is no longer pending "
                "(peer %s, status %s, %d chars, %s tokens) — the requester timed out "
                "before the host finished",
                request_id, sender_node_id, status,
                len(response or ""), tokens_used,
            )

        return None
