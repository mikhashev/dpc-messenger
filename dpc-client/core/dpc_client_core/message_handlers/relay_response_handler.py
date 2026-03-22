"""
Relay Response Handlers - Client-side RELAY_READY / RELAY_WAITING

Handles responses from the relay node during session establishment.

RELAY_WAITING: Relay received our RELAY_REGISTER but the other peer has not
    registered yet. We stay waiting — connect_via_relay holds an asyncio.Future
    that will be resolved when RELAY_READY arrives.

RELAY_READY: Both peers have registered; the relay has created the session.
    Resolves the pending Future in relay_manager._pending_relay_sessions so
    connect_via_relay can proceed to create the RelayedPeerConnection.
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
