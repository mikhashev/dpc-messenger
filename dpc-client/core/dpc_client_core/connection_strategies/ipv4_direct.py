"""
IPv4 Direct Connection Strategy - Priority 2

Attempts direct TLS connection over IPv4. Works for:
- Local network peers (same subnet)
- Peers with port forwarding configured
- Peers with public static IPv4 (no NAT)

Requirements:
- Peer must be reachable via IPv4 (local or external address)
- No symmetric NAT or CGNAT blocking the connection
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from .base import ConnectionStrategy, StrategyNotApplicableError

if TYPE_CHECKING:
    from ..models.peer_endpoint import PeerEndpoint
    from ..coordinators.connection_orchestrator import ConnectionOrchestrator

logger = logging.getLogger(__name__)


class IPv4DirectStrategy(ConnectionStrategy):
    """
    Direct TLS connection over IPv4.

    Priority 2 - Tries IPv4 direct connection (local or external address).
    Works for local network peers and port-forwarded nodes.

    Example:
        >>> strategy = IPv4DirectStrategy()
        >>> connection = await strategy.connect(node_id, endpoints, orchestrator)
    """

    name = "ipv4_direct"
    priority = 2
    timeout = 10.0  # 10 seconds for direct connection

    def is_applicable(self, endpoints: "PeerEndpoint") -> bool:
        """
        Check if peer has IPv4 address (always true).

        IPv4 is required in schema v2.0, so this strategy is always applicable.

        Args:
            endpoints: Peer endpoint information

        Returns:
            Always True (IPv4 required field)
        """
        # IPv4 local address is required in schema v2.0
        return endpoints.ipv4 is not None and endpoints.ipv4.local is not None

    async def connect(
        self,
        node_id: str,
        endpoints: "PeerEndpoint",
        orchestrator: "ConnectionOrchestrator"
    ):
        """
        Attempt IPv4 direct TLS connection.

        Tries addresses in this order:
        1. External address (if behind NAT with port forwarding)
        2. Local address (for same subnet peers)

        Args:
            node_id: Target node identifier
            endpoints: Peer endpoint information
            orchestrator: Connection orchestrator

        Returns:
            PeerConnection instance

        Raises:
            StrategyNotApplicableError: If peer has no IPv4
            ConnectionError: If all connection attempts fail
        """
        if not self.is_applicable(endpoints):
            raise StrategyNotApplicableError(f"Peer {node_id[:20]} has no IPv4 address")

        ipv4_info = endpoints.ipv4

        # Determine connection order based on external address availability
        addresses_to_try = []

        # Try external address first (if available)
        if ipv4_info.external:
            addresses_to_try.append(("external", ipv4_info.external))

        # Then try local address
        addresses_to_try.append(("local", ipv4_info.local))

        last_error = None

        for addr_type, address in addresses_to_try:
            try:
                # Parse "ip:port" format
                if ":" not in address:
                    logger.warning("Invalid IPv4 address format: %s", address)
                    continue

                ip, port_str = address.rsplit(":", 1)
                port = int(port_str)

                logger.info(
                    "Attempting IPv4 %s connection to %s at %s:%d",
                    addr_type, node_id[:20], ip, port
                )

                # Use existing P2P manager's connect_to_peer method
                connection = await asyncio.wait_for(
                    orchestrator.p2p_manager.connect_to_peer(ip, port, node_id),
                    timeout=self.timeout
                )

                logger.info(
                    "IPv4 %s connection established to %s",
                    addr_type, node_id[:20]
                )
                return connection

            except asyncio.TimeoutError:
                logger.debug(
                    "IPv4 %s connection timeout to %s:%d",
                    addr_type, ip, port
                )
                last_error = ConnectionError(f"IPv4 {addr_type} timeout")
            except Exception as e:
                logger.debug(
                    "IPv4 %s connection failed to %s:%d: %s",
                    addr_type, ip, port, e
                )
                last_error = e

        # All attempts failed
        logger.warning("All IPv4 direct connection attempts failed to %s", node_id[:20])
        raise ConnectionError(f"IPv4 connection failed: {last_error}")
