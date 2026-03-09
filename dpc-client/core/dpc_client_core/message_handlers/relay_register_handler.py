"""
Relay Register Handler - Server-side relay session registration

Handles RELAY_REGISTER requests from clients wanting to establish
relayed connections. Creates relay sessions when both peers register.

Protocol Flow:
1. Peer A sends RELAY_REGISTER(target=Peer B)
2. Relay waits for Peer B to register
3. Peer B sends RELAY_REGISTER(target=Peer A)
4. Relay creates session and sends RELAY_READY to both peers
"""

import logging
from typing import TYPE_CHECKING

from . import MessageHandler

if TYPE_CHECKING:
    from ..service import CoreService

logger = logging.getLogger(__name__)


class RelayRegisterHandler(MessageHandler):
    """
    Handle RELAY_REGISTER requests (server mode).

    Called when a peer wants to establish a relayed connection through
    this node. Creates relay sessions when both peers have registered.

    Example:
        >>> # Peer A registers
        >>> handler.handle("dpc-node-alice", {"peer_id": "dpc-node-bob", "timeout": 30.0})
        >>> # Returns None (waiting for peer B)
        >>>
        >>> # Peer B registers
        >>> handler.handle("dpc-node-bob", {"peer_id": "dpc-node-alice", "timeout": 30.0})
        >>> # Returns session_id (both registered, session created)
    """

    @property
    def command_name(self) -> str:
        return "RELAY_REGISTER"

    async def handle(self, sender_node_id: str, payload: dict) -> None:
        """
        Handle RELAY_REGISTER request.

        Args:
            sender_node_id: Node ID of requester
            payload: Contains "peer_id" (target peer) and optional "timeout"

        Protocol:
            Request payload:
                {"peer_id": "...", "timeout": 30.0}

            Response (if both peers registered):
                {"command": "RELAY_READY", "payload": {"session_id": "..."}}

            Response (if waiting for other peer):
                {"command": "RELAY_WAITING", "payload": {"message": "Waiting for peer..."}}

            Error response:
                {"command": "ERROR", "payload": {"error": "...", "message": "..."}}
        """
        # Get connection from p2p_manager
        connection = self.service.p2p_manager.peers.get(sender_node_id)
        if not connection:
            logger.warning("No connection found for sender %s", sender_node_id[:20])
            return

        target_peer_id = payload.get("peer_id")

        if not target_peer_id:
            logger.warning("RELAY_REGISTER missing peer_id from %s", sender_node_id[:20])
            await connection.send({
                "command": "ERROR",
                "payload": {
                    "error": "invalid_request",
                    "message": "Missing peer_id in RELAY_REGISTER"
                }
            })
            return

        # Check if relay_manager is initialized and volunteering
        if not hasattr(self.service, 'relay_manager') or not self.service.relay_manager:
            logger.warning("RelayManager not initialized - rejecting registration from %s", sender_node_id[:20])
            await connection.send({
                "command": "ERROR",
                "payload": {
                    "error": "not_volunteering",
                    "message": "This node is not volunteering as a relay"
                }
            })
            return

        if not self.service.relay_manager.volunteer:
            logger.warning("Not volunteering as relay - rejecting registration from %s", sender_node_id[:20])
            await connection.send({
                "command": "ERROR",
                "payload": {
                    "error": "not_volunteering",
                    "message": "This node is not volunteering as a relay"
                }
            })
            return

        logger.info(
            "Processing RELAY_REGISTER: requester=%s, target=%s",
            sender_node_id[:20], target_peer_id[:20]
        )

        # Handle registration via RelayManager
        session_id = await self.service.relay_manager.handle_relay_register(
            requester_id=sender_node_id,
            target_id=target_peer_id,
            requester_connection=connection
        )

        if session_id:
            # Both peers registered - session created
            logger.info(
                "Relay session created: %s (peers: %s, %s)",
                session_id, sender_node_id[:20], target_peer_id[:20]
            )

            # Send RELAY_READY to requester (second peer to register)
            await connection.send({
                "command": "RELAY_READY",
                "payload": {
                    "session_id": session_id,
                    "peer_id": target_peer_id  # The other peer in this session
                }
            })

            # Send RELAY_READY to first peer (who was waiting with RELAY_WAITING)
            first_peer_conn = self.service.relay_manager.peer_connections.get(target_peer_id)
            if first_peer_conn:
                try:
                    await first_peer_conn.send({
                        "command": "RELAY_READY",
                        "payload": {
                            "session_id": session_id,
                            "peer_id": sender_node_id  # The peer who just completed the session
                        }
                    })
                    logger.debug(
                        "Sent RELAY_READY to waiting peer %s", target_peer_id[:20]
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to send RELAY_READY to first peer %s: %s",
                        target_peer_id[:20], e
                    )
            else:
                logger.warning(
                    "First peer %s connection not found for RELAY_READY", target_peer_id[:20]
                )
        else:
            # Waiting for target peer to register
            logger.debug("Waiting for target peer %s to register", target_peer_id[:20])

            await connection.send({
                "command": "RELAY_WAITING",
                "payload": {
                    "message": f"Waiting for peer {target_peer_id[:20]} to register",
                    "timeout": payload.get("timeout", 30.0)
                }
            })
