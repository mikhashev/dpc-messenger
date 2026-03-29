"""
Message handlers for P2P commands.

This module provides a pluggable architecture for handling different P2P message types.
Each message handler is responsible for processing a specific command type.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


class MessageHandler(ABC):
    """Base class for P2P message handlers."""

    def __init__(self, service):
        """
        Initialize handler with reference to CoreService.

        Args:
            service: CoreService instance (provides access to managers, settings, etc.)

        Note: Handlers access service components dynamically (e.g., self.service.local_api)
        rather than storing references. This allows tests to mock components after initialization.
        """
        self.service = service
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle message from sender with given payload.

        Args:
            sender_node_id: Node ID of the message sender
            payload: Message payload (data after command field)

        Returns:
            Optional response data (for request-response patterns)
        """
        pass

    @property
    @abstractmethod
    def command_name(self) -> str:
        """Command name this handler responds to."""
        pass

    async def _relay_to_group(
        self, command: str, payload: Dict[str, Any],
        sender_node_id: str, group_id: str
    ) -> None:
        """Relay message to group members the sender can't reach directly (star topology)."""
        group = self.service.group_manager.get_group(group_id)
        if not group:
            return
        relay_msg = {"command": command, "payload": payload}
        for member_id in group.members:
            if member_id == self.service.p2p_manager.node_id:
                continue
            if member_id == sender_node_id:
                continue
            if member_id in self.service.p2p_manager.peers:
                try:
                    await self.service.p2p_manager.send_message_to_peer(member_id, relay_msg)
                    self.logger.debug("Relayed %s to %s", command, member_id[:20])
                except Exception as e:
                    self.logger.error("Failed to relay %s to %s: %s", command, member_id[:20], e)


__all__ = ["MessageHandler"]
