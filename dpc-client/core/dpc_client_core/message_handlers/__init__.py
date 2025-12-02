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


__all__ = ["MessageHandler"]
