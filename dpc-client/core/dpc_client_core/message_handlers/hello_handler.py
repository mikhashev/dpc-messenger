"""Handler for HELLO command - peer name exchange."""

from typing import Dict, Any, Optional
from . import MessageHandler


class HelloHandler(MessageHandler):
    """Handles HELLO messages for peer name exchange."""

    @property
    def command_name(self) -> str:
        return "HELLO"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle HELLO message (name exchange).

        Mainly for WebRTC connections that don't have initial handshake.

        Args:
            sender_node_id: Node ID of sender
            payload: Contains "name" field with peer's display name
        """
        peer_name = payload.get("name")
        if peer_name:
            self.logger.debug("Received name from %s: %s", sender_node_id, peer_name)
            self.service.set_peer_metadata(sender_node_id, name=peer_name)
            # Notify UI of peer list change so names update
            await self.service.on_peer_list_change()

        return None
