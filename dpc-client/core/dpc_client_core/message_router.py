"""
Message router for P2P commands.

Routes incoming P2P messages to appropriate handlers based on command type.
"""

import logging
from typing import Dict, Optional, Any
from .message_handlers import MessageHandler

logger = logging.getLogger(__name__)

#: Commands that arrive in bulk, where one line per message is not a trace
#: but a flood. Measured 2026-08-24 on Mike's transfer: 131 211 chunks, two
#: DEBUG lines each from this layer and the service above it — a quarter of
#: a million lines for one file, burying everything else in the log.
#:
#: They are not silenced: the count is carried on a heartbeat instead, so a
#: reader still sees the traffic flowing and by how much, at 1/500 the
#: volume. The receiving handler keeps its own progress line.
BULK_COMMANDS = frozenset({"FILE_CHUNK"})

#: How many bulk messages pass between heartbeats. The first is always
#: logged — «did anything arrive at all» is the question a trace is for.
BULK_TRACE_EVERY = 500


class MessageRouter:
    """Routes P2P messages to appropriate handlers."""

    def __init__(self):
        """Initialize empty handler registry."""
        self._handlers: Dict[str, MessageHandler] = {}
        #: Per-command counters for BULK_COMMANDS, process-lifetime.
        self._bulk_seen: Dict[str, int] = {}

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
            if command in BULK_COMMANDS:
                seen = self._bulk_seen.get(command, 0) + 1
                self._bulk_seen[command] = seen
                if seen == 1 or seen % BULK_TRACE_EVERY == 0:
                    logger.debug(
                        "Routing %s from %s to %s (%d so far, tracing 1 in %d)",
                        command, sender_node_id, handler.__class__.__name__,
                        seen, BULK_TRACE_EVERY,
                    )
            else:
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
