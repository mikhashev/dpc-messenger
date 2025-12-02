"""Handlers for AI provider discovery commands."""

from typing import Dict, Any, Optional
from . import MessageHandler


class GetProvidersHandler(MessageHandler):
    """Handles GET_PROVIDERS messages (peer requesting available AI providers)."""

    @property
    def command_name(self) -> str:
        return "GET_PROVIDERS"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GET_PROVIDERS message.

        Peer is requesting the list of AI providers (models) available on this node.
        Used for remote inference capability discovery.

        Args:
            sender_node_id: Node ID of requester
            payload: Empty payload (no parameters)
        """
        await self.service._handle_get_providers_request(sender_node_id)
        return None


class ProvidersResponseHandler(MessageHandler):
    """Handles PROVIDERS_RESPONSE messages (peer responding with their providers)."""

    @property
    def command_name(self) -> str:
        return "PROVIDERS_RESPONSE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle PROVIDERS_RESPONSE message.

        Peer is sending their list of available AI providers.
        Stores this information for UI display and remote inference selection.

        Args:
            sender_node_id: Node ID of responder
            payload: Contains "providers" list with provider/model info
        """
        providers = payload.get("providers", [])
        await self.service._handle_providers_response(sender_node_id, providers)
        return None
