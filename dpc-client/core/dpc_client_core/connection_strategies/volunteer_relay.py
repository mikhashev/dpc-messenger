"""
Volunteer Relay Connection Strategy - Priority 5

Attempts connection via volunteer relay nodes when direct methods fail.
Provides 100% NAT coverage for symmetric NAT, CGNAT, and restrictive firewalls.

Algorithm:
1. Query DHT for available relays
2. Score relays by quality (uptime, capacity, latency)
3. Connect to best relay
4. Relay forwards encrypted messages between peers

Requirements:
- Available relay nodes in DHT
- Both peers can connect to relay (outbound connections)

Success Rate:
- 100% NAT coverage (if relays available)
- Works for symmetric NAT, CGNAT, strict firewalls
- Only fails if no relays available or relay unreachable

Privacy:
- End-to-end encryption maintained
- Relay cannot decrypt message content
- Relay sees: peer IDs, message sizes, timing
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from .base import ConnectionStrategy, StrategyNotApplicableError

if TYPE_CHECKING:
    from ..models.peer_endpoint import PeerEndpoint
    from ..coordinators.connection_orchestrator import ConnectionOrchestrator

logger = logging.getLogger(__name__)


class VolunteerRelayStrategy(ConnectionStrategy):
    """
    Connection via volunteer relay nodes.

    Priority 5 - Fallback for symmetric NAT/CGNAT when hole punching fails.
    Provides 100% NAT coverage by routing through relay nodes.

    Example:
        >>> strategy = VolunteerRelayStrategy()
        >>> if strategy.is_applicable(endpoints):
        ...     connection = await strategy.connect(node_id, endpoints, orchestrator)
    """

    name = "volunteer_relay"
    priority = 5
    timeout = 20.0  # 20 seconds for relay discovery + connection

    def is_applicable(self, endpoints: "PeerEndpoint") -> bool:
        """
        Check if relay connection is applicable.

        Relay is always applicable as a fallback (works for all NAT types).
        The only failure case is if no relays are available in the DHT.

        Args:
            endpoints: Peer endpoint information

        Returns:
            Always True (relay works for all NAT types)

        Note:
            This strategy tries to find relays dynamically via DHT.
            If no relays available, connection will fail in connect().
        """
        # Relay works for all NAT types (always applicable)
        # Actual availability checked in connect() via DHT query
        return True

    async def connect(
        self,
        node_id: str,
        endpoints: "PeerEndpoint",
        orchestrator: "ConnectionOrchestrator"
    ):
        """
        Attempt relay connection.

        Uses RelayManager to:
        1. Query DHT for available relays
        2. Score relays by quality (uptime, capacity, latency)
        3. Connect to best relay
        4. Establish relay session with peer
        5. Return RelayedPeerConnection wrapper

        Args:
            node_id: Target node identifier
            endpoints: Peer endpoint information
            orchestrator: Connection orchestrator

        Returns:
            RelayedPeerConnection instance

        Raises:
            StrategyNotApplicableError: If relay_manager not initialized
            ConnectionError: If no relays available or connection fails

        Algorithm:
            1. Query DHT for available relays (via RelayManager)
            2. Select best relay (highest quality score)
            3. Connect to relay (TLS)
            4. Send RELAY_REGISTER request (target peer ID)
            5. Wait for RELAY_READY response (relay confirms session)
            6. Return RelayedPeerConnection wrapper

        Example:
            >>> connection = await strategy.connect(
            ...     "dpc-node-bob",
            ...     endpoints,
            ...     orchestrator
            ... )
            >>> await connection.send_message({"command": "HELLO"})
        """
        # Check if relay_manager is available
        if not hasattr(orchestrator, 'relay_manager') or not orchestrator.relay_manager:
            raise StrategyNotApplicableError("RelayManager not initialized")

        logger.info("Attempting relay connection to %s", node_id[:20])

        try:
            # Step 1: Find best available relay via DHT
            relay_node = await asyncio.wait_for(
                orchestrator.relay_manager.find_relay(),
                timeout=10.0
            )

            if not relay_node:
                logger.warning("No available relays found in DHT")
                raise ConnectionError("No relay nodes available")

            logger.info(
                "Selected relay: %s (region=%s, quality=%.2f)",
                relay_node.node_id[:20], relay_node.region, relay_node.quality_score()
            )

            # Step 2-5: Connect via relay manager (delegates connection logic)
            relayed_conn = await orchestrator.relay_manager.connect_via_relay(
                peer_id=node_id,
                relay_node=relay_node
            )

            logger.info("Relay connection established to %s", node_id[:20])
            return relayed_conn

        except asyncio.TimeoutError:
            logger.warning(
                "Relay connection timeout to %s (after %.1fs)",
                node_id[:20], self.timeout
            )
            raise ConnectionError(f"Relay timeout to {node_id[:20]}")
        except Exception as e:
            logger.warning(
                "Relay connection failed to %s: %s",
                node_id[:20], e
            )
            raise ConnectionError(f"Relay connection failed: {e}")
