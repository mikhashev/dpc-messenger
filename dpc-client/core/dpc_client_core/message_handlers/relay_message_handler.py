"""
Relay Message Handler - Server-side message forwarding

Handles RELAY_MESSAGE commands from clients, forwarding encrypted
messages between relay sessions. Relay cannot decrypt message content
(end-to-end encryption maintained).

Protocol Flow:
1. Peer A sends RELAY_MESSAGE(to=Peer B, message={encrypted})
2. Relay verifies session exists
3. Relay forwards to Peer B via active connection
4. Peer B receives RELAY_MESSAGE(from=Peer A, message={encrypted})

Privacy:
- Relay sees: peer IDs, message sizes, timing
- Relay does NOT see: message content (E2E encrypted)
"""

import logging
from typing import TYPE_CHECKING

from . import MessageHandler

if TYPE_CHECKING:
    from ..service import CoreService

logger = logging.getLogger(__name__)


class RelayMessageHandler(MessageHandler):
    """
    Handle RELAY_MESSAGE forwarding (server mode).

    Forwards encrypted messages between peers in a relay session.
    Maintains end-to-end encryption (relay cannot decrypt content).

    Example:
        >>> # Peer A sends message to Peer B via relay
        >>> handler.handle(
        ...     "dpc-node-alice",
        ...     {
        ...         "from": "dpc-node-alice",
        ...         "to": "dpc-node-bob",
        ...         "session_id": "...",
        ...         "message": {"command": "SEND_TEXT", "payload": {...}}  # Encrypted
        ...     }
        ... )
        >>> # Relay forwards to Peer B's connection
    """

    @property
    def command_name(self) -> str:
        return "RELAY_MESSAGE"

    async def handle(self, sender_node_id: str, payload: dict) -> None:
        """
        Handle RELAY_MESSAGE forwarding.

        Args:
            sender_node_id: Node ID of message sender
            payload: Contains "from", "to", "session_id", and "message" (encrypted)

        Protocol:
            Request payload:
                {
                    "from": "dpc-node-sender",
                    "to": "dpc-node-receiver",
                    "session_id": "...",
                    "message": {...}  # Encrypted message
                }

            Response (on error):
                {"command": "ERROR", "payload": {"error": "...", "message": "..."}}
        """
        # Get connection from p2p_manager
        connection = self.service.p2p_manager.peers.get(sender_node_id)
        if not connection:
            logger.warning("No connection found for sender %s", sender_node_id[:20])
            return
        from_peer = payload.get("from")
        to_peer = payload.get("to")
        session_id = payload.get("session_id")
        encrypted_message = payload.get("message")

        # Validate payload
        if not all([from_peer, to_peer, session_id, encrypted_message]):
            logger.warning("Invalid RELAY_MESSAGE payload from %s", sender_node_id[:20])
            await connection.send_message({
                "command": "ERROR",
                "payload": {
                    "error": "invalid_request",
                    "message": "Missing required fields in RELAY_MESSAGE"
                }
            })
            return

        # Verify sender identity matches connection
        if from_peer != sender_node_id:
            logger.warning(
                "RELAY_MESSAGE from field mismatch: connection=%s, from=%s",
                sender_node_id[:20], from_peer[:20]
            )
            await connection.send_message({
                "command": "ERROR",
                "payload": {
                    "error": "invalid_sender",
                    "message": "Sender ID does not match connection"
                }
            })
            return

        # Check if relay_manager is initialized and volunteering
        if not hasattr(self.service, 'relay_manager') or not self.service.relay_manager:
            logger.warning("RelayManager not initialized - cannot forward message")
            await connection.send_message({
                "command": "ERROR",
                "payload": {
                    "error": "not_volunteering",
                    "message": "This node is not volunteering as a relay"
                }
            })
            return

        if not self.service.relay_manager.volunteer:
            logger.warning("Not volunteering as relay - cannot forward message")
            await connection.send_message({
                "command": "ERROR",
                "payload": {
                    "error": "not_volunteering",
                    "message": "This node is not volunteering as a relay"
                }
            })
            return

        logger.debug(
            "Forwarding RELAY_MESSAGE: %s → %s (session=%s, size=%d bytes)",
            from_peer[:20], to_peer[:20], session_id,
            len(str(encrypted_message).encode())
        )

        # Convert message dict to bytes for relaying
        import json
        message_bytes = json.dumps(encrypted_message).encode('utf-8')

        # Forward via RelayManager
        success = await self.service.relay_manager.handle_relay_message(
            from_peer=from_peer,
            to_peer=to_peer,
            message=message_bytes
        )

        if not success:
            logger.warning(
                "Failed to forward RELAY_MESSAGE: %s → %s",
                from_peer[:20], to_peer[:20]
            )
            await connection.send_message({
                "command": "ERROR",
                "payload": {
                    "error": "forward_failed",
                    "message": "Failed to forward message (session not found or rate limited)"
                }
            })
