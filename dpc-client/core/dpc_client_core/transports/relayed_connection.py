"""
Relayed Peer Connection - Relay Transport Wrapper

Wraps a relayed connection (via volunteer relay node) to provide the
same interface as direct PeerConnection. Messages are forwarded through
the relay node with end-to-end encryption maintained.

Architecture:
- Client connects to relay via TLS
- Relay forwards encrypted messages between peers
- Relay cannot decrypt message content (E2E encryption)
- Transparent to higher layers (same API as direct connection)

Privacy:
- Messages encrypted end-to-end (relay sees only encrypted payloads)
- Relay knows: peer IDs, message sizes, timing
- Relay does NOT know: message content, conversation context
"""

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..models.relay_node import RelayNode

logger = logging.getLogger(__name__)


class RelayedPeerConnection:
    """
    Peer connection via volunteer relay node.

    Provides same interface as PeerConnection but routes messages
    through relay instead of direct connection.

    Attributes:
        peer_id: Target peer node ID
        relay_node: Relay node handling forwarding
        relay_connection: TLS connection to relay
        session_id: Relay session identifier
        running: Whether connection is active

    Example:
        >>> # Establish relayed connection
        >>> conn = RelayedPeerConnection(
        ...     peer_id="dpc-node-bob",
        ...     relay_node=relay,
        ...     relay_connection=relay_conn
        ... )
        >>> await conn.start()
        >>>
        >>> # Use like direct connection
        >>> await conn.send_message({"command": "HELLO"})
        >>> message = await conn.receive_message()
    """

    def __init__(
        self,
        peer_id: str,
        relay_node: "RelayNode",
        relay_connection,  # PeerConnection to relay
        session_id: str
    ):
        """
        Initialize relayed connection.

        Args:
            peer_id: Target peer node ID
            relay_node: Relay node metadata
            relay_connection: TLS connection to relay
            session_id: Relay session identifier
        """
        self.peer_id = peer_id
        self.relay_node = relay_node
        self.relay_connection = relay_connection
        self.session_id = session_id

        self.running = False
        self._receive_queue = asyncio.Queue()
        self._receive_task: Optional[asyncio.Task] = None

        logger.info(
            "RelayedPeerConnection created: peer=%s, relay=%s, session=%s",
            peer_id[:20], relay_node.node_id[:20], session_id
        )

    async def start(self):
        """
        Start relayed connection (begin receiving messages).

        Starts background task to receive RELAY_MESSAGE protocol messages
        from relay and enqueue them for application consumption.
        """
        if self.running:
            logger.warning("RelayedPeerConnection already running")
            return

        self.running = True
        self._receive_task = asyncio.create_task(self._receive_loop())

        logger.info("RelayedPeerConnection started for peer %s", self.peer_id[:20])

    async def stop(self):
        """Stop relayed connection and cleanup."""
        if not self.running:
            return

        self.running = False

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        # Send RELAY_DISCONNECT to relay
        try:
            await self.relay_connection.send_message({
                "command": "RELAY_DISCONNECT",
                "payload": {
                    "peer": self.peer_id,
                    "session_id": self.session_id,
                    "reason": "connection_closed"
                }
            })
        except Exception as e:
            logger.debug("Failed to send RELAY_DISCONNECT: %s", e)

        logger.info("RelayedPeerConnection stopped for peer %s", self.peer_id[:20])

    async def send_message(self, message: dict):
        """
        Send message to peer via relay.

        Args:
            message: Message dictionary (will be encrypted)

        Raises:
            ConnectionError: If relay connection failed

        Protocol:
            Wraps message in RELAY_MESSAGE command and sends to relay.
            Relay forwards to destination peer.
        """
        if not self.running:
            raise ConnectionError("RelayedPeerConnection not running")

        # Wrap in RELAY_MESSAGE protocol
        relay_message = {
            "command": "RELAY_MESSAGE",
            "payload": {
                "from": self.relay_connection.node_id,  # Our node ID
                "to": self.peer_id,
                "session_id": self.session_id,
                "message": message  # Encrypted by protocol layer
            }
        }

        try:
            await self.relay_connection.send_message(relay_message)
            logger.debug("Sent message to peer %s via relay", self.peer_id[:20])
        except Exception as e:
            logger.error("Failed to send relayed message: %s", e)
            raise ConnectionError(f"Relay send failed: {e}")

    async def receive_message(self, timeout: Optional[float] = None) -> Optional[dict]:
        """
        Receive message from peer via relay.

        Args:
            timeout: Receive timeout in seconds

        Returns:
            Message dictionary, or None if timeout

        Raises:
            ConnectionError: If connection closed
        """
        if not self.running:
            raise ConnectionError("RelayedPeerConnection not running")

        try:
            if timeout:
                message = await asyncio.wait_for(
                    self._receive_queue.get(),
                    timeout=timeout
                )
            else:
                message = await self._receive_queue.get()

            return message

        except asyncio.TimeoutError:
            return None

    async def _receive_loop(self):
        """
        Background task: Receive RELAY_MESSAGE from relay and enqueue.

        Runs until connection closed. Filters for RELAY_MESSAGE commands
        from our session and extracts the actual peer message.
        """
        logger.debug("Relay receive loop started for peer %s", self.peer_id[:20])

        try:
            while self.running:
                # Receive message from relay
                relay_msg = await self.relay_connection.receive_message(timeout=1.0)

                if not relay_msg:
                    continue

                # Filter for RELAY_MESSAGE from our session
                if relay_msg.get("command") == "RELAY_MESSAGE":
                    payload = relay_msg.get("payload", {})

                    if payload.get("session_id") == self.session_id:
                        if payload.get("from") == self.peer_id:
                            # Extract actual message from peer
                            peer_message = payload.get("message")
                            if peer_message:
                                await self._receive_queue.put(peer_message)
                                logger.debug(
                                    "Received message from peer %s via relay",
                                    self.peer_id[:20]
                                )

        except asyncio.CancelledError:
            logger.debug("Relay receive loop cancelled")
            raise
        except Exception as e:
            logger.error("Relay receive loop error: %s", e)
            self.running = False

    def is_connected(self) -> bool:
        """Check if relayed connection is active."""
        return self.running and self.relay_connection.is_connected()

    def __repr__(self) -> str:
        """String representation for debugging."""
        return (
            f"<RelayedPeerConnection peer={self.peer_id[:20]} "
            f"relay={self.relay_node.node_id[:20]} session={self.session_id}>"
        )
