"""Handlers for context-related commands (personal and device context)."""

from typing import Dict, Any, Optional
from . import MessageHandler
from dpc_protocol.pcm_core import PersonalContext


class RequestContextHandler(MessageHandler):
    """Handles REQUEST_CONTEXT messages (peer requesting personal context)."""

    @property
    def command_name(self) -> str:
        return "REQUEST_CONTEXT"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle REQUEST_CONTEXT message.

        Peer is requesting this node's personal context. Firewall rules
        are applied before responding.

        Args:
            sender_node_id: Node ID of requester
            payload: Contains "request_id" and optional "query" field
        """
        request_id = payload.get("request_id")
        query = payload.get("query")
        await self.service._handle_context_request(sender_node_id, query, request_id)
        return None


class ContextResponseHandler(MessageHandler):
    """Handles CONTEXT_RESPONSE messages (peer responding with their context)."""

    @property
    def command_name(self) -> str:
        return "CONTEXT_RESPONSE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle CONTEXT_RESPONSE message.

        Resolves a pending context request future with the received context.

        Args:
            sender_node_id: Node ID of responder
            payload: Contains "request_id" and "context" dict
        """
        request_id = payload.get("request_id")
        context_dict = payload.get("context")

        if request_id in self.service._pending_context_requests:
            future = self.service._pending_context_requests[request_id]
            if not future.done():
                # Deserialize dict into PersonalContext object
                try:
                    context_obj = PersonalContext.from_dict(context_dict)
                    future.set_result(context_obj)
                except Exception as e:
                    self.logger.error("Error deserializing context from peer: %s", e, exc_info=True)
                    future.set_result(None)

        return None


class RequestDeviceContextHandler(MessageHandler):
    """Handles REQUEST_DEVICE_CONTEXT messages (peer requesting device context)."""

    @property
    def command_name(self) -> str:
        return "REQUEST_DEVICE_CONTEXT"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle REQUEST_DEVICE_CONTEXT message.

        Peer is requesting this node's device context (hardware/software info).
        Firewall rules are applied before responding.

        Args:
            sender_node_id: Node ID of requester
            payload: Contains "request_id" field
        """
        request_id = payload.get("request_id")
        await self.service._handle_device_context_request(sender_node_id, request_id)
        return None


class DeviceContextResponseHandler(MessageHandler):
    """Handles DEVICE_CONTEXT_RESPONSE messages (peer responding with device context)."""

    @property
    def command_name(self) -> str:
        return "DEVICE_CONTEXT_RESPONSE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle DEVICE_CONTEXT_RESPONSE message.

        Resolves a pending device context request future with the received data.

        Args:
            sender_node_id: Node ID of responder
            payload: Contains "request_id" and "device_context" dict
        """
        request_id = payload.get("request_id")
        device_context = payload.get("device_context")

        if request_id in self.service._pending_device_context_requests:
            future = self.service._pending_device_context_requests[request_id]
            if not future.done():
                future.set_result(device_context)

        return None
