"""
Message router for P2P commands.

Routes incoming P2P messages to appropriate handlers based on command type.
"""

import logging
from typing import Dict, Optional, Any
from .message_handlers import MessageHandler

logger = logging.getLogger(__name__)


class MessageRouter:
    """Routes P2P messages to appropriate handlers."""

    def __init__(self):
        """Initialize empty handler registry."""
        self._handlers: Dict[str, MessageHandler] = {}

    def register_handler(self, handler: MessageHandler):
        """
        Register a message handler.

        Args:
            handler: MessageHandler instance to register
        """
        command = handler.command_name
        if command in self._handlers:
            logger.warning("Overwriting handler for %s", command)
        self._handlers[command] = handler
        logger.debug("Registered handler for %s", command)

    def unregister_handler(self, command: str):
        """
        Unregister a message handler.

        Args:
            command: Command name to unregister
        """
        if command in self._handlers:
            del self._handlers[command]
            logger.debug("Unregistered handler for %s", command)

    async def route_message(self, sender_node_id: str, message: Dict[str, Any]) -> Optional[Any]:
        """
        Route message to appropriate handler.

        Args:
            sender_node_id: Node ID of message sender
            message: Message dict with "command" and "payload" fields

        Returns:
            Optional response data from handler (for request-response patterns)
        """
        command = message.get("command")

        if command not in self._handlers:
            logger.warning("Unknown P2P message command: %s", command)
            return None

        handler = self._handlers[command]
        payload = message.get("payload", {})

        try:
            logger.debug("Routing %s message from %s to %s", command, sender_node_id, handler.__class__.__name__)
            return await handler.handle(sender_node_id, payload)
        except Exception as e:
            logger.error("Error handling %s from %s: %s", command, sender_node_id, e, exc_info=True)
            return None

    def get_registered_commands(self) -> list:
        """
        Get list of registered command names.

        Returns:
            List of command names with registered handlers
        """
        return list(self._handlers.keys())
