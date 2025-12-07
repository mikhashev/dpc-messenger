"""
Base Connection Strategy - Abstract Interface for Phase 6

This module defines the abstract base class for connection strategies in the
6-tier fallback hierarchy. Each strategy implements a specific connection method
(IPv6 direct, IPv4 direct, Hub WebRTC, UDP hole punching, relay, gossip).

Strategy Pattern:
- Enables pluggable connection methods
- Allows strategies to be tried in priority order
- Each strategy can check if it's applicable before attempting connection
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models.peer_endpoint import PeerEndpoint


class StrategyNotApplicableError(Exception):
    """Raised when a connection strategy is not applicable for the given peer."""
    pass


class ConnectionStrategy(ABC):
    """
    Abstract base class for connection strategies.

    Each strategy implements a specific method for connecting to a peer:
    - Priority 1: IPv6 direct connection (no NAT)
    - Priority 2: IPv4 direct connection (local network/port forward)
    - Priority 3: Hub WebRTC (existing STUN/TURN)
    - Priority 4: DHT-coordinated UDP hole punching
    - Priority 5: Volunteer relay nodes
    - Priority 6: Gossip store-and-forward

    Attributes:
        name: Strategy identifier (e.g., "ipv6_direct", "hub_webrtc")
        priority: Numeric priority (1 = highest, 6 = lowest)
        timeout: Connection attempt timeout in seconds

    Example:
        >>> class MyStrategy(ConnectionStrategy):
        ...     name = "my_strategy"
        ...     priority = 4
        ...     timeout = 15.0
        ...
        ...     def is_applicable(self, endpoints):
        ...         return endpoints.supports_hole_punching()
        ...
        ...     async def connect(self, node_id, endpoints, orchestrator):
        ...         # Implement connection logic
        ...         return peer_connection
    """

    name: str  # Strategy identifier
    priority: int  # Numeric priority (1-6)
    timeout: float  # Connection timeout in seconds

    @abstractmethod
    def is_applicable(self, endpoints: "PeerEndpoint") -> bool:
        """
        Check if this strategy is applicable for the given peer.

        Args:
            endpoints: Peer endpoint information from DHT

        Returns:
            True if strategy can be attempted, False otherwise

        Example:
            >>> ipv6_strategy.is_applicable(endpoints)
            True  # Peer has IPv6 address
        """
        pass

    @abstractmethod
    async def connect(
        self,
        node_id: str,
        endpoints: "PeerEndpoint",
        orchestrator: "ConnectionOrchestrator"
    ):
        """
        Attempt connection using this strategy.

        Args:
            node_id: Target node identifier
            endpoints: Peer endpoint information from DHT
            orchestrator: Connection orchestrator (for accessing managers)

        Returns:
            PeerConnection instance on success

        Raises:
            StrategyNotApplicableError: If strategy cannot be used
            ConnectionError: If connection attempt fails
            asyncio.TimeoutError: If connection times out

        Example:
            >>> connection = await strategy.connect(
            ...     "dpc-node-abc123",
            ...     endpoints,
            ...     orchestrator
            ... )
            >>> await connection.send_message({"command": "HELLO"})
        """
        pass

    def __repr__(self) -> str:
        """String representation for debugging."""
        return f"<{self.__class__.__name__} priority={self.priority} timeout={self.timeout}s>"
