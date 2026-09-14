"""Handlers for remote inference commands (compute sharing)."""

from typing import Dict, Any, Optional
from . import MessageHandler
from ..node_ledger import stated_output_includes_thinking, stated_thinking_source


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
            payload: "request_id" and "prompt", plus the optional "model",
                "provider", "images", "reasoning_effort", "messages", "system",
                "tools" and "stream". A field this node has never heard of is
                neither read nor refused: a newer guest must still be answered.
        """
        request_id = payload.get("request_id")
        prompt = payload.get("prompt")
        model = payload.get("model")
        provider = payload.get("provider")
        images = payload.get("images")  # Phase 2: Remote Vision support
        # What the peer asked to spend on thinking. The host clamps it; absent
        # means it did not choose.
        reasoning_effort = payload.get("reasoning_effort")
        messages = payload.get("messages")
        system = payload.get("system")
        tools = payload.get("tools")
        stream = bool(payload.get("stream"))

        await self.service._handle_inference_request(
            sender_node_id, request_id, prompt, model, provider, images,
            reasoning_effort, messages=messages, system=system, tools=tools,
            stream=stream,
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

        # The owner's price for this call (v1.7): the applied rates, their unit,
        # the dated entry they came from and what they came to. Read as one
        # group, as it is sent — a half group is no tariff at all — and absent
        # stays absent, because a zero would read as «declared free».
        # The host's own cost is not on the wire and is not this node's to copy.
        tariff = {name: payload.get(name) for name in
                  ("tariff_in", "tariff_out", "tariff_currency", "tariff_at")}
        if any(value is None for value in tariff.values()):
            tariff = {}
        elif payload.get("tariff_amount") is not None:
            tariff["tariff_amount"] = payload.get("tariff_amount")
        billing = payload.get("billing")
        # Any string can arrive here; only the three words go further (v1.7).
        output_includes_thinking = payload.get("output_includes_thinking")
        if output_includes_thinking is not None:
            output_includes_thinking = stated_output_includes_thinking(
                output_includes_thinking, peer=sender_node_id, log=self.logger,
            )
        # Where the host's thinking count came from (v1.7); a word that is
        # neither of the two is dropped, not carried.
        thinking_source = stated_thinking_source(
            payload.get("thinking_source"), peer=sender_node_id, log=self.logger,
        )
        # The effort the host actually ran at, after its clamp (v1.7). Absent
        # means the host applied no effort control, which is not `off`.
        served_effort = payload.get("served_effort")

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
                    result_data.update(tariff)
                    if billing is not None:
                        result_data["billing"] = billing
                    if output_includes_thinking is not None:
                        result_data["output_includes_thinking"] = output_includes_thinking
                    if thinking_source is not None:
                        result_data["thinking_source"] = thinking_source
                    if served_effort is not None:
                        result_data["served_effort"] = served_effort
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
