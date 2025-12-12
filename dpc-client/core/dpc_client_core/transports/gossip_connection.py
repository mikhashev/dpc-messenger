"""
Gossip Connection - Gossip Protocol Transport Wrapper

Wraps the gossip manager to provide the same interface as direct PeerConnection.
Messages are sent via epidemic routing with eventual delivery guarantee.

Architecture:
- Virtual connection (no actual socket)
- Messages forwarded via epidemic gossip protocol
- End-to-end encryption (only sender and recipient can decrypt)
- Transparent to higher layers (same API as direct connection)

Use Cases:
- Last-resort fallback when all direct methods fail (Priority 6)
- Offline messaging (peer receives when online)
- Disaster scenarios (infrastructure outages)

Privacy:
- Messages encrypted end-to-end (intermediate hops see only encrypted blobs)
- Intermediate hops know: peer IDs, message sizes, TTL, hop count
- Intermediate hops do NOT know: message content

Performance:
- Eventual delivery (not real-time)
- High latency (multi-hop routing)
- Use only when direct connections unavailable
"""

import asyncio
import logging
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..managers.gossip_manager import GossipManager
    from ..coordinators.connection_orchestrator import ConnectionOrchestrator

logger = logging.getLogger(__name__)


class GossipConnection:
    """
    Virtual connection that sends/receives via gossip protocol.

    Provides same interface as PeerConnection but routes messages
    through epidemic gossip instead of direct connection.

    Attributes:
        peer_id: Target peer node ID
        gossip_manager: Gossip manager for message routing
        orchestrator: Connection orchestrator reference
        running: Whether connection is active
        _receive_queue: Queue for incoming messages

    Example:
        >>> # Create gossip connection
        >>> conn = GossipConnection(
        ...     peer_id="dpc-node-bob",
        ...     gossip_manager=manager,
        ...     orchestrator=orch
        ... )
        >>> await conn.start()
        >>>
        >>> # Use like direct connection (eventual delivery)
        >>> await conn.send({"command": "HELLO"})
        >>> message = await conn.read()  # May take seconds/minutes
    """

    def __init__(
        self,
        peer_id: str,
        gossip_manager: "GossipManager",
        orchestrator: "ConnectionOrchestrator"
    ):
        """
        Initialize gossip connection.

        Args:
            peer_id: Target peer node ID
            gossip_manager: Gossip manager for epidemic routing
            orchestrator: Connection orchestrator reference
        """
        self.peer_id = peer_id
        self.gossip_manager = gossip_manager
        self.orchestrator = orchestrator

        self.running = False
        self._receive_queue = asyncio.Queue()

        logger.info(
            "GossipConnection created: peer=%s (virtual, eventual delivery)",
            peer_id[:20]
        )

    async def start(self):
        """
        Start gossip connection (register for message delivery).

        Registers callback with gossip manager to receive messages
        from the specified peer.

        Note:
            No actual connection established (virtual connection).
            Messages delivered via callback when they arrive.
        """
        if self.running:
            logger.warning("GossipConnection already running")
            return

        self.running = True

        # Register delivery callback with gossip manager
        self.gossip_manager.register_delivery_callback(
            self.peer_id,
            self._on_message_delivered
        )

        logger.info(
            "GossipConnection started: peer=%s (virtual, registered callback)",
            self.peer_id[:20]
        )

    async def _on_message_delivered(self, message: Dict):
        """
        Callback when gossip message arrives from peer.

        Args:
            message: Decrypted message payload

        Note:
            Called by gossip manager when message delivered.
        """
        logger.debug(f"Gossip message arrived from {self.peer_id[:20]}")
        await self._receive_queue.put(message)

    async def send(self, message: Dict):
        """
        Send message via gossip epidemic protocol.

        Args:
            message: Message to send (will be encrypted)

        Note:
            Eventual delivery (not real-time).
            Message forwarded via epidemic routing (fanout=3).

        Raises:
            Exception: If encryption or sending fails
        """
        if not self.running:
            raise RuntimeError("GossipConnection not running")

        logger.debug(f"Sending message to {self.peer_id[:20]} via gossip")

        # Send via gossip manager (with encryption)
        await self.gossip_manager.send_gossip(
            destination=self.peer_id,
            payload=message,
            priority="normal"
        )

        logger.debug(f"Message sent to {self.peer_id[:20]} via gossip")

    async def read(self) -> Optional[Dict]:
        """
        Receive message from gossip protocol (eventual delivery).

        Returns:
            Message dict or None if timeout

        Note:
            Blocks until message arrives or timeout (30 seconds).
            Gossip is not real-time - expect high latency.
        """
        if not self.running:
            return None

        try:
            # Wait for message with timeout (gossip isn't real-time)
            message = await asyncio.wait_for(
                self._receive_queue.get(),
                timeout=30.0
            )
            logger.debug(f"Received message from {self.peer_id[:20]} via gossip")
            return message

        except asyncio.TimeoutError:
            logger.debug(f"Gossip read timeout for {self.peer_id[:20]}")
            return None

    async def close(self):
        """
        Stop receiving gossip messages.

        Unregisters delivery callback and clears pending messages.
        """
        if not self.running:
            return

        self.running = False

        # Unregister delivery callback
        self.gossip_manager.unregister_delivery_callback(self.peer_id)

        # Clear pending messages
        while not self._receive_queue.empty():
            try:
                self._receive_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        logger.info(f"GossipConnection closed: peer={self.peer_id[:20]}")
