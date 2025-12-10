"""Handler for GOSSIP_SYNC command - anti-entropy synchronization."""

from typing import Dict, Any, Optional
from . import MessageHandler


class GossipSyncHandler(MessageHandler):
    """Handles GOSSIP_SYNC messages for anti-entropy synchronization."""

    @property
    def command_name(self) -> str:
        return "GOSSIP_SYNC"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle GOSSIP_SYNC message (anti-entropy synchronization).

        Args:
            sender_node_id: Node ID of sender
            payload: Contains "vector_clock" (dict) and "message_ids" (list)
        """
        vector_clock = payload.get("vector_clock", {})
        message_ids = payload.get("message_ids", [])

        if not hasattr(self.service, 'gossip_manager') or self.service.gossip_manager is None:
            self.logger.warning("GOSSIP_SYNC received but GossipManager not initialized")
            return None

        # Delegate to gossip manager
        await self.service.gossip_manager.handle_gossip_sync(
            peer_id=sender_node_id,
            peer_clock_dict=vector_clock,
            peer_message_ids=message_ids
        )

        return None
