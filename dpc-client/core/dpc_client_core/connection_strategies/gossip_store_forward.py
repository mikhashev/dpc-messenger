"""
Gossip Store-and-Forward Strategy - Priority 6

Last-resort fallback for disaster-resilient messaging when all direct
methods fail. Uses epidemic gossip protocol for eventual delivery.

Algorithm:
1. Create gossip message with destination
2. Begin epidemic spreading (forward to N random peers)
3. Multi-hop routing until message reaches destination
4. Eventual delivery (not real-time)

Requirements:
- At least one connected peer
- Peer network must have path to destination (eventually)

Success Rate:
- Eventual delivery (if destination comes online)
- High latency (multiple hops, store-and-forward)
- Not suitable for real-time communication

Use Cases:
- Offline messaging
- Disaster scenarios (infrastructure outages)
- Knowledge commit sync
- Censorship resistance
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from .base import ConnectionStrategy, StrategyNotApplicableError

if TYPE_CHECKING:
    from ..models.peer_endpoint import PeerEndpoint
    from ..coordinators.connection_orchestrator import ConnectionOrchestrator

logger = logging.getLogger(__name__)


class GossipStoreForwardStrategy(ConnectionStrategy):
    """
    Store-and-forward messaging via gossip protocol.

    Priority 6 (lowest) - Last-resort fallback for disaster scenarios.
    Provides eventual delivery through multi-hop epidemic routing.

    Example:
        >>> strategy = GossipStoreForwardStrategy()
        >>> connection = await strategy.connect(node_id, endpoints, orchestrator)
        >>> # Returns virtual connection (messages sent via gossip)
    """

    name = "gossip_store_forward"
    priority = 6
    timeout = 5.0  # 5 seconds to initiate gossip (doesn't wait for delivery)

    def is_applicable(self, endpoints: "PeerEndpoint") -> bool:
        """
        Check if gossip is applicable.

        Gossip is always applicable as ultimate fallback.
        Only requirement: at least one connected peer.

        Args:
            endpoints: Peer endpoint information

        Returns:
            Always True (gossip works for all scenarios)

        Note:
            Actual connectivity checked in connect() via peer count.
        """
        # Gossip always applicable (last-resort fallback)
        return True

    async def connect(
        self,
        node_id: str,
        endpoints: "PeerEndpoint",
        orchestrator: "ConnectionOrchestrator"
    ):
        """
        Establish virtual gossip connection.

        Creates a virtual connection that sends messages via gossip protocol.
        Messages are not delivered in real-time - eventual delivery only.

        Args:
            node_id: Target node identifier
            endpoints: Peer endpoint information
            orchestrator: Connection orchestrator

        Returns:
            GossipConnection (virtual connection wrapper)

        Raises:
            StrategyNotApplicableError: If gossip_manager not initialized
            ConnectionError: If no connected peers available

        Algorithm:
            1. Check if gossip_manager available
            2. Check if we have any connected peers (for forwarding)
            3. Return virtual GossipConnection wrapper
            4. Messages sent via gossip (not real-time delivery)

        Example:
            >>> connection = await strategy.connect(
            ...     "dpc-node-bob",
            ...     endpoints,
            ...     orchestrator
            ... )
            >>> await connection.send_message({"command": "HELLO"})
            >>> # Message sent via gossip (eventual delivery)
        """
        # Check if gossip_manager is available
        if not hasattr(orchestrator, 'gossip_manager') or not orchestrator.gossip_manager:
            raise StrategyNotApplicableError("GossipManager not initialized")

        logger.info(
            "Attempting gossip store-and-forward to %s (eventual delivery)",
            node_id[:20]
        )

        # Check if we have any connected peers (for forwarding)
        if orchestrator.p2p_manager:
            peers = orchestrator.p2p_manager.get_connected_peers()
            if not peers:
                logger.warning("No connected peers for gossip forwarding")
                raise ConnectionError("No peers available for gossip routing")

        # Import here to avoid circular dependency
        from ..transports.gossip_connection import GossipConnection

        # Create virtual gossip connection
        connection = GossipConnection(
            peer_id=node_id,
            gossip_manager=orchestrator.gossip_manager,
            orchestrator=orchestrator
        )

        # Start receiving (registers callback with gossip manager)
        await connection.start()

        logger.info(
            "Gossip connection established to %s (virtual, eventual delivery)",
            node_id[:20]
        )

        return connection
