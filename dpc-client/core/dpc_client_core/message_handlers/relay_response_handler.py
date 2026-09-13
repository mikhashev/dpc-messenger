"""
Relay Response Handlers - Client-side RELAY_READY / RELAY_WAITING / RELAY_DISCONNECT_ACK

Handles responses from the relay node during session establishment and teardown.
RELAY_WAITING logs and keeps waiting; RELAY_READY resolves the pending Future
in relay_manager._pending_relay_sessions; RELAY_DISCONNECT_ACK has no pending
Future to settle (stop() sends RELAY_DISCONNECT fire-and-forget), so it is
recorded on the matching RelayedPeerConnection instead.
"""

import logging
from typing import TYPE_CHECKING

from . import MessageHandler

if TYPE_CHECKING:
    from ..service import CoreService

logger = logging.getLogger(__name__)


class RelayWaitingHandler(MessageHandler):
    """
    Handle RELAY_WAITING from relay node (client mode).

    Relay sends this when we registered first and are waiting for the
    other peer. connect_via_relay is already blocked on a Future — just log.
    """

    @property
    def command_name(self) -> str:
        return "RELAY_WAITING"

    async def handle(self, sender_node_id: str, payload: dict) -> None:
        message = payload.get("message", "Waiting for peer to register")
        logger.info(
            "Relay waiting for peer to connect (relay=%s): %s",
            sender_node_id[:20], message
        )


class RelayReadyHandler(MessageHandler):
    """
    Handle RELAY_READY from relay node (client mode).

    Relay sends this when both peers have registered and the session is ready.
    Resolves the asyncio.Future in relay_manager._pending_relay_sessions so
    connect_via_relay can proceed.

    Payload:
        session_id: Relay session identifier
        peer_id: The other peer in this session
    """

    @property
    def command_name(self) -> str:
        return "RELAY_READY"

    async def handle(self, sender_node_id: str, payload: dict) -> None:
        session_id = payload.get("session_id")
        peer_id = payload.get("peer_id")

        if not session_id or not peer_id:
            logger.warning(
                "RELAY_READY missing session_id or peer_id from %s",
                sender_node_id[:20]
            )
            return

        relay_manager = getattr(self.service, 'relay_manager', None)
        if not relay_manager:
            logger.warning("RelayManager not available — cannot resolve RELAY_READY")
            return

        future = relay_manager._pending_relay_sessions.get(peer_id)
        if future is None:
            logger.warning(
                "No pending relay session for peer %s (RELAY_READY from %s)",
                peer_id[:20], sender_node_id[:20]
            )
            return

        if future.done():
            logger.debug(
                "RELAY_READY for peer %s but Future already resolved", peer_id[:20]
            )
            return

        future.set_result(session_id)
        logger.info(
            "Relay session ready: session=%s peer=%s relay=%s",
            session_id, peer_id[:20], sender_node_id[:20]
        )


class RelayDisconnectAckHandler(MessageHandler):
    """
    Handle RELAY_DISCONNECT_ACK from relay node (client mode, spec §3.13).

    Payload:
        session_id: The session the relay just cleaned up (or reports not_found)
        status: "cleaned_up" or "not_found"

    Carries no peer_id, so the matching RelayedPeerConnection is found by
    session_id among relay_manager._active_relay_connections.
    """

    @property
    def command_name(self) -> str:
        return "RELAY_DISCONNECT_ACK"

    async def handle(self, sender_node_id: str, payload: dict) -> None:
        session_id = payload.get("session_id")
        status = payload.get("status", "unknown")

        if not session_id:
            logger.warning(
                "RELAY_DISCONNECT_ACK missing session_id from %s", sender_node_id[:20]
            )
            return

        relay_manager = getattr(self.service, 'relay_manager', None)
        if not relay_manager:
            logger.debug("RelayManager not initialized — ignoring RELAY_DISCONNECT_ACK")
            return

        conn = next(
            (
                c for c in relay_manager._active_relay_connections.values()
                if c.session_id == session_id
            ),
            None,
        )
        if conn is None:
            logger.info(
                "RELAY_DISCONNECT_ACK for session %s (status=%s) from %s — "
                "no matching active connection (already torn down)",
                session_id, status, sender_node_id[:20]
            )
            return

        conn.disconnect_acked = True
        conn.disconnect_ack_status = status
        logger.info(
            "Relay disconnect acknowledged: session=%s status=%s peer=%s",
            session_id, status, conn.peer_id[:20]
        )
