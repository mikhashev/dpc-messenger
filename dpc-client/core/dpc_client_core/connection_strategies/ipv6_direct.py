"""
IPv6 Direct Connection Strategy - Priority 1

Attempts direct TLS connection over IPv6 (no NAT traversal needed).
This is the preferred method when both peers have global IPv6 addresses.

Advantages:
- No NAT (most IPv6 deployments have global addresses)
- Lowest latency (direct peer-to-peer)
- No infrastructure dependencies

Requirements:
- Peer must have advertised IPv6 address in DHT
- Local node must have IPv6 connectivity
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from .base import ConnectionStrategy, StrategyNotApplicableError

if TYPE_CHECKING:
    from ..models.peer_endpoint import PeerEndpoint
    from ..coordinators.connection_orchestrator import ConnectionOrchestrator

logger = logging.getLogger(__name__)


class IPv6DirectStrategy(ConnectionStrategy):
    """
    Direct TLS connection over IPv6.

    Priority 1 (highest) - Tries IPv6 direct connection first if available.
    Provides best performance with no NAT traversal complexity.

    Example:
        >>> strategy = IPv6DirectStrategy()
        >>> if strategy.is_applicable(endpoints):
        ...     connection = await strategy.connect(node_id, endpoints, orchestrator)
    """

    name = "ipv6_direct"
    priority = 1
    timeout = 10.0  # 10 seconds for direct connection

    def is_applicable(self, endpoints: "PeerEndpoint") -> bool:
        """
        Check if peer has advertised IPv6 address.

        Args:
            endpoints: Peer endpoint information

        Returns:
            True if peer has global IPv6 address
        """
        return endpoints.has_ipv6()

    async def connect(
        self,
        node_id: str,
        endpoints: "PeerEndpoint",
        orchestrator: "ConnectionOrchestrator"
    ):
        """
        Attempt IPv6 direct TLS connection.

        Args:
            node_id: Target node identifier
            endpoints: Peer endpoint information
            orchestrator: Connection orchestrator

        Returns:
            PeerConnection instance

        Raises:
            StrategyNotApplicableError: If peer has no IPv6
            ConnectionError: If connection fails
        """
        if not self.is_applicable(endpoints):
            raise StrategyNotApplicableError(f"Peer {node_id[:20]} has no IPv6 address")

        ipv6_info = endpoints.ipv6
        logger.info(
            "Attempting IPv6 direct connection to %s at %s",
            node_id[:20], ipv6_info.address
        )

        try:
            # Parse IPv6 address with port: "[2001:db8::1]:8888"
            if ipv6_info.address.startswith('['):
                # Extract host and port from "[host]:port" format
                bracket_end = ipv6_info.address.index(']')
                host = ipv6_info.address[1:bracket_end]
                port_str = ipv6_info.address[bracket_end+2:]  # Skip "]:"
                port = int(port_str)
            else:
                raise ValueError(f"Invalid IPv6 address format: {ipv6_info.address}")

            # Use existing P2P manager's connect_to_peer method
            # Note: This assumes P2P manager supports IPv6 (it does via dual-stack)
            connection = await asyncio.wait_for(
                orchestrator.p2p_manager.connect_to_peer(host, port, node_id),
                timeout=self.timeout
            )

            logger.info("IPv6 direct connection established to %s", node_id[:20])
            return connection

        except asyncio.TimeoutError:
            logger.warning("IPv6 direct connection timeout to %s", node_id[:20])
            raise ConnectionError(f"IPv6 connection timeout to {node_id[:20]}")
        except Exception as e:
            logger.warning("IPv6 direct connection failed to %s: %s", node_id[:20], e)
            raise ConnectionError(f"IPv6 connection failed: {e}")
