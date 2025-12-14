"""Handlers for gossip protocol commands (GOSSIP_SYNC, GOSSIP_MESSAGE)."""

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


class GossipMessageHandler(MessageHandler):
    """Handles GOSSIP_MESSAGE commands for epidemic routing."""

    @property
    def command_name(self) -> str:
        return "GOSSIP_MESSAGE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle incoming GOSSIP_MESSAGE.

        Delegates to gossip manager for epidemic processing:
        - Check if destination is us → deliver (decrypt)
        - Check if seen before → ignore (deduplication)
        - Check TTL/hops → drop if exceeded
        - Otherwise → forward to N random peers (fanout=3)

        Args:
            sender_node_id: Node ID of sender
            payload: Contains "gossip_message" (serialized GossipMessage dict)

        Returns:
            None (gossip messages don't expect responses)
        """
        self.logger.debug(f"Received GOSSIP_MESSAGE from {sender_node_id[:20]}")

        if not hasattr(self.service, 'gossip_manager') or self.service.gossip_manager is None:
            self.logger.warning("GOSSIP_MESSAGE received but GossipManager not initialized")
            return None

        try:
            # Extract gossip message from payload
            gossip_data = payload.get("gossip_message")
            if not gossip_data:
                self.logger.warning("GOSSIP_MESSAGE missing 'gossip_message' field")
                return None

            # Deserialize and process via gossip manager
            from ..models.gossip_message import GossipMessage
            message = GossipMessage.from_dict(gossip_data)

            await self.service.gossip_manager.handle_gossip_message(message)

        except Exception as e:
            self.logger.error(f"Error handling GOSSIP_MESSAGE: {e}", exc_info=True)

        return None
