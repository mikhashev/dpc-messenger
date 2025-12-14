"""
Relay Disconnect Handler - Server-side session cleanup

Handles RELAY_DISCONNECT requests from clients ending relay sessions.
Cleans up relay state and notifies other peer if still connected.

Protocol Flow:
1. Peer A sends RELAY_DISCONNECT(peer=Peer B, session_id=...)
2. Relay cleans up session state
3. Relay notifies Peer B that session ended
4. Both peers can reconnect or fall back to gossip
"""

import logging
from typing import TYPE_CHECKING

from . import MessageHandler

if TYPE_CHECKING:
    from ..service import CoreService

logger = logging.getLogger(__name__)


class RelayDisconnectHandler(MessageHandler):
    """
    Handle RELAY_DISCONNECT requests (server mode).

    Cleans up relay sessions when a peer disconnects or explicitly
    ends the relayed connection.

    Example:
        >>> # Peer A disconnects from relay session
        >>> handler.handle(
        ...     {
        ...         "command": "RELAY_DISCONNECT",
        ...         "payload": {
        ...             "peer": "dpc-node-bob",
        ...             "session_id": "...",
        ...             "reason": "connection_closed"
        ...         }
        ...     },
        ...     connection
        ... )
        >>> # Relay cleans up session and notifies Peer B
    """

    command = "RELAY_DISCONNECT"

    def __init__(self, service: "CoreService"):
        """
        Initialize handler.

        Args:
            service: CoreService instance
        """
        self.service = service

    async def handle(self, message: dict, connection) -> None:
        """
        Handle RELAY_DISCONNECT request.

        Args:
            message: Protocol message with RELAY_DISCONNECT command
            connection: Connection from disconnecting peer

        Protocol:
            Request:
                {
                    "command": "RELAY_DISCONNECT",
                    "payload": {
                        "peer": "dpc-node-other-peer",
                        "session_id": "...",
                        "reason": "connection_closed|user_request|timeout"
                    }
                }

            Response:
                {"command": "RELAY_DISCONNECT_ACK", "payload": {"session_id": "..."}}
        """
        payload = message.get("payload", {})
        peer_id = payload.get("peer")
        session_id = payload.get("session_id")
        reason = payload.get("reason", "unknown")
        requester_id = connection.node_id

        if not session_id:
            logger.warning("RELAY_DISCONNECT missing session_id from %s", requester_id[:20])
            return

        # Check if relay_manager is initialized
        if not hasattr(self.service, 'relay_manager') or not self.service.relay_manager:
            logger.debug("RelayManager not initialized - ignoring RELAY_DISCONNECT")
            return

        logger.info(
            "Processing RELAY_DISCONNECT: session=%s, requester=%s, reason=%s",
            session_id, requester_id[:20], reason
        )

        # Find session
        session = self.service.relay_manager.sessions.get(session_id)
        if not session:
            logger.debug("Session %s not found (already cleaned up?)", session_id)
            await connection.send_message({
                "command": "RELAY_DISCONNECT_ACK",
                "payload": {"session_id": session_id, "status": "not_found"}
            })
            return

        # Verify requester is part of this session
        if requester_id not in [session.peer_a_id, session.peer_b_id]:
            logger.warning(
                "RELAY_DISCONNECT from non-participant: requester=%s, session=%s",
                requester_id[:20], session_id
            )
            await connection.send_message({
                "command": "ERROR",
                "payload": {
                    "error": "not_authorized",
                    "message": "You are not a participant in this session"
                }
            })
            return

        # Determine other peer
        other_peer_id = session.peer_b_id if requester_id == session.peer_a_id else session.peer_a_id

        logger.info(
            "Cleaning up relay session %s: %s disconnected (reason: %s)",
            session_id, requester_id[:20], reason
        )

        # Clean up session state
        del self.service.relay_manager.sessions[session_id]
        if requester_id in self.service.relay_manager.peer_to_session:
            del self.service.relay_manager.peer_to_session[requester_id]
        if other_peer_id in self.service.relay_manager.peer_to_session:
            del self.service.relay_manager.peer_to_session[other_peer_id]

        # Send acknowledgment to requester
        await connection.send_message({
            "command": "RELAY_DISCONNECT_ACK",
            "payload": {
                "session_id": session_id,
                "status": "cleaned_up"
            }
        })

        # TODO: Notify other peer that session ended
        # (Requires storing connection references in RelayManager)
        logger.debug(
            "Session %s cleaned up, other peer %s should be notified",
            session_id, other_peer_id[:20]
        )
