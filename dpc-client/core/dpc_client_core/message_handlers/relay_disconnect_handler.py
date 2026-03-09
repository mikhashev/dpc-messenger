"""
Relay Disconnect Handler - Session cleanup (server and client mode)

Server mode: Relay node receives RELAY_DISCONNECT from a client peer.
    Cleans up the session and notifies the other peer in the session.

Client mode: Client receives RELAY_DISCONNECT from the relay (other peer left).
    Deregisters the active RelayedPeerConnection so no further dispatch occurs.

Protocol Flow (server mode):
1. Peer A sends RELAY_DISCONNECT(peer=Peer B, session_id=...)
2. Relay cleans up session state
3. Relay notifies Peer B that session ended
4. Both peers can reconnect or fall back to gossip

Protocol Flow (client mode):
1. Relay sends RELAY_DISCONNECT(peer=Peer A, session_id=...) to Peer B
2. Peer B deregisters its RelayedPeerConnection for Peer A
"""

import logging
from typing import TYPE_CHECKING

from . import MessageHandler

if TYPE_CHECKING:
    from ..service import CoreService

logger = logging.getLogger(__name__)


class RelayDisconnectHandler(MessageHandler):
    """
    Handle RELAY_DISCONNECT (server and client mode).
    """

    @property
    def command_name(self) -> str:
        return "RELAY_DISCONNECT"

    async def handle(self, sender_node_id: str, payload: dict) -> None:
        peer_id = payload.get("peer")
        session_id = payload.get("session_id")
        reason = payload.get("reason", "unknown")

        if not session_id:
            logger.warning("RELAY_DISCONNECT missing session_id from %s", sender_node_id[:20])
            return

        relay_manager = getattr(self.service, 'relay_manager', None)
        if not relay_manager:
            logger.debug("RelayManager not initialized — ignoring RELAY_DISCONNECT")
            return

        # ---- Client mode: relay is telling us the other peer disconnected ----
        # In this case sender_node_id is the relay node, and peer_id is the other peer
        if peer_id and peer_id in relay_manager._active_relay_connections:
            logger.info(
                "Client-mode relay disconnect: peer=%s disconnected (relay=%s, reason=%s)",
                peer_id[:20], sender_node_id[:20], reason
            )
            conn = relay_manager._active_relay_connections.pop(peer_id, None)
            if conn:
                conn.running = False  # Mark as stopped without sending another RELAY_DISCONNECT
            return

        # ---- Server mode: a client peer is disconnecting from a session we host ----
        connection = self.service.p2p_manager.peers.get(sender_node_id)
        if not connection:
            logger.warning("No connection found for sender %s", sender_node_id[:20])
            return

        logger.info(
            "Relay server RELAY_DISCONNECT: session=%s requester=%s reason=%s",
            session_id, sender_node_id[:20], reason
        )

        session = relay_manager.sessions.get(session_id)
        if not session:
            logger.debug("Session %s not found (already cleaned up?)", session_id)
            await connection.send({
                "command": "RELAY_DISCONNECT_ACK",
                "payload": {"session_id": session_id, "status": "not_found"}
            })
            return

        # Verify requester is part of this session
        if sender_node_id not in [session.peer_a_id, session.peer_b_id]:
            logger.warning(
                "RELAY_DISCONNECT from non-participant %s in session %s",
                sender_node_id[:20], session_id
            )
            await connection.send({
                "command": "ERROR",
                "payload": {
                    "error": "not_authorized",
                    "message": "You are not a participant in this session"
                }
            })
            return

        other_peer_id = (
            session.peer_b_id if sender_node_id == session.peer_a_id else session.peer_a_id
        )

        # Clean up server-side session state
        relay_manager.sessions.pop(session_id, None)
        relay_manager.peer_to_session.pop(sender_node_id, None)
        relay_manager.peer_to_session.pop(other_peer_id, None)

        # Acknowledge to requester
        await connection.send({
            "command": "RELAY_DISCONNECT_ACK",
            "payload": {"session_id": session_id, "status": "cleaned_up"}
        })

        # Notify other peer that the session has ended
        other_conn = relay_manager.peer_connections.get(other_peer_id)
        if other_conn:
            try:
                await other_conn.send({
                    "command": "RELAY_DISCONNECT",
                    "payload": {
                        "peer": sender_node_id,
                        "session_id": session_id,
                        "reason": f"peer_disconnected:{reason}"
                    }
                })
                logger.debug(
                    "Notified peer %s that %s disconnected from session %s",
                    other_peer_id[:20], sender_node_id[:20], session_id
                )
            except Exception as e:
                logger.warning(
                    "Failed to notify peer %s of relay disconnect: %s",
                    other_peer_id[:20], e
                )
        else:
            logger.debug(
                "Other peer %s not connected to relay — no disconnect notification sent",
                other_peer_id[:20]
            )
