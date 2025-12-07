"""
Connection Orchestrator - 6-Tier Fallback Logic (Phase 6)

This module implements the connection orchestrator that coordinates the 6-tier
fallback hierarchy for establishing peer connections:

Priority 1: IPv6 direct connection (no NAT) → 40%+ networks
Priority 2: IPv4 direct connection (local/port forward) → Local network
Priority 3: Hub WebRTC (existing STUN/TURN) → When Hub available
Priority 4: DHT-coordinated UDP hole punching → 60-70% NAT (Hub-independent)
Priority 5: Volunteer relay nodes → 100% NAT (Hub-independent)
Priority 6: Gossip store-and-forward → Disaster fallback

Key Innovation: Hub becomes optional. When Hub unavailable (offline mode,
censorship, disaster), system falls back to fully decentralized alternatives.
"""

import asyncio
import logging
from typing import Optional, List, TYPE_CHECKING

from ..connection_strategies.base import ConnectionStrategy, StrategyNotApplicableError
from ..connection_strategies.ipv6_direct import IPv6DirectStrategy
from ..connection_strategies.ipv4_direct import IPv4DirectStrategy
from ..connection_strategies.hub_webrtc import HubWebRTCStrategy
from ..connection_strategies.udp_hole_punch import UDPHolePunchStrategy

if TYPE_CHECKING:
    from ..p2p_manager import P2PManager
    from ..dht.manager import DHTManager
    from ..hub_client import HubClient
    from ..models.peer_endpoint import PeerEndpoint
    from ..managers.hole_punch_manager import HolePunchManager

logger = logging.getLogger(__name__)


class ConnectionFailedError(Exception):
    """Raised when all connection strategies have been exhausted."""
    pass


class ConnectionOrchestrator:
    """
    Intelligent connection strategy orchestrator with 6-tier fallback.

    Coordinates multiple connection strategies and tries them in priority order
    until one succeeds. Strategies can be added/removed dynamically.

    Attributes:
        p2p_manager: P2P connection manager (for direct TLS connections)
        dht_manager: DHT manager (for peer discovery and endpoint lookup)
        hub_client: Hub client (for WebRTC signaling, optional)
        hole_punch_manager: Hole punch manager (for UDP NAT traversal, optional)
        strategies: List of connection strategies (sorted by priority)

    Example:
        >>> orchestrator = ConnectionOrchestrator(
        ...     p2p_manager=p2p_manager,
        ...     dht_manager=dht_manager,
        ...     hub_client=hub_client
        ... )
        >>> connection = await orchestrator.connect("dpc-node-abc123")
        >>> print(f"Connected via {connection.strategy_used}")
    """

    def __init__(
        self,
        p2p_manager: "P2PManager",
        dht_manager: "DHTManager",
        hub_client: Optional["HubClient"] = None,
        hole_punch_manager: Optional["HolePunchManager"] = None
    ):
        """
        Initialize connection orchestrator.

        Args:
            p2p_manager: P2P connection manager
            dht_manager: DHT manager for peer discovery
            hub_client: Hub client (optional, for WebRTC)
            hole_punch_manager: Hole punch manager (optional, for UDP hole punching)
        """
        self.p2p_manager = p2p_manager
        self.dht_manager = dht_manager
        self.hub_client = hub_client
        self.hole_punch_manager = hole_punch_manager

        # Initialize strategies (sorted by priority)
        self.strategies: List[ConnectionStrategy] = [
            IPv6DirectStrategy(),           # Priority 1 (always try first)
            IPv4DirectStrategy(),           # Priority 2 (local network)
            HubWebRTCStrategy(),            # Priority 3 (existing STUN/TURN via Hub)
            UDPHolePunchStrategy(),         # Priority 4 (DHT hole punching - Hub-independent)
            # Priority 5: VolunteerRelayStrategy() - TODO: Week 3
            # Priority 6: GossipStoreForwardStrategy() - TODO: Week 4
        ]

        # Sort by priority (lowest number = highest priority)
        self.strategies.sort(key=lambda s: s.priority)

        # Statistics
        self.stats = {
            "total_attempts": 0,
            "successful_connections": 0,
            "failed_connections": 0,
            "strategy_usage": {},  # Track which strategies succeed
        }

        logger.info(
            "ConnectionOrchestrator initialized with %d strategies",
            len(self.strategies)
        )

    async def connect(self, node_id: str, timeout: float = 30.0):
        """
        Attempt connection using fallback hierarchy.

        Tries strategies in priority order until one succeeds or all fail.

        Args:
            node_id: Target node identifier
            timeout: Overall timeout for all strategies (seconds)

        Returns:
            PeerConnection instance on success

        Raises:
            ConnectionFailedError: If all strategies exhausted

        Example:
            >>> try:
            ...     connection = await orchestrator.connect("dpc-node-abc123")
            ...     print(f"Connected via {connection.strategy_used}")
            ... except ConnectionFailedError as e:
            ...     print(f"All strategies failed: {e}")
        """
        self.stats["total_attempts"] += 1
        logger.info("Connecting to %s (timeout=%ds)", node_id[:20], timeout)

        # Step 1: Query DHT for peer endpoints
        logger.debug("Querying DHT for peer endpoints")
        endpoints = await self.dht_manager.find_peer_full(node_id)

        if not endpoints:
            logger.warning("Peer %s not found in DHT", node_id[:20])
            self.stats["failed_connections"] += 1
            raise ConnectionFailedError(f"Peer {node_id[:20]} not announced in DHT")

        logger.info(
            "Found peer %s endpoints (IPv6=%s, relay=%s, punch=%s)",
            node_id[:20],
            "yes" if endpoints.has_ipv6() else "no",
            "yes" if endpoints.supports_relay() else "no",
            "yes" if endpoints.supports_hole_punching() else "no"
        )

        # Step 2: Try strategies in priority order
        last_error = None
        start_time = asyncio.get_event_loop().time()

        for strategy in self.strategies:
            # Check timeout
            elapsed = asyncio.get_event_loop().time() - start_time
            remaining = timeout - elapsed

            if remaining <= 0:
                logger.warning("Overall connection timeout exceeded")
                break

            # Check if strategy is applicable
            if not strategy.is_applicable(endpoints):
                logger.debug(
                    "Strategy %s not applicable for peer %s",
                    strategy.name, node_id[:20]
                )
                continue

            # Attempt connection with strategy
            try:
                logger.info(
                    "Trying strategy %s (priority=%d, timeout=%.1fs)",
                    strategy.name, strategy.priority, min(strategy.timeout, remaining)
                )

                connection = await asyncio.wait_for(
                    strategy.connect(node_id, endpoints, self),
                    timeout=min(strategy.timeout, remaining)
                )

                # Success!
                logger.info("Connected to %s via %s", node_id[:20], strategy.name)

                # Update statistics
                self.stats["successful_connections"] += 1
                if strategy.name not in self.stats["strategy_usage"]:
                    self.stats["strategy_usage"][strategy.name] = 0
                self.stats["strategy_usage"][strategy.name] += 1

                # Store strategy used (for diagnostics)
                connection.strategy_used = strategy.name

                return connection

            except StrategyNotApplicableError as e:
                logger.debug("Strategy %s not applicable: %s", strategy.name, e)
                continue
            except asyncio.TimeoutError:
                logger.warning(
                    "Strategy %s timeout for peer %s",
                    strategy.name, node_id[:20]
                )
                last_error = f"{strategy.name} timeout"
            except Exception as e:
                logger.warning(
                    "Strategy %s failed for peer %s: %s",
                    strategy.name, node_id[:20], e
                )
                last_error = f"{strategy.name}: {e}"

        # All strategies exhausted
        logger.error("All strategies exhausted for peer %s", node_id[:20])
        self.stats["failed_connections"] += 1

        raise ConnectionFailedError(
            f"All connection strategies failed for {node_id[:20]}: {last_error}"
        )

    def get_stats(self) -> dict:
        """
        Get orchestrator statistics.

        Returns:
            Dictionary with connection stats and strategy usage

        Example:
            >>> stats = orchestrator.get_stats()
            >>> print(f"Success rate: {stats['successful_connections'] / stats['total_attempts']:.1%}")
            >>> print(f"Most used strategy: {max(stats['strategy_usage'], key=stats['strategy_usage'].get)}")
        """
        return {
            **self.stats,
            "active_strategies": [s.name for s in self.strategies],
        }

    def add_strategy(self, strategy: ConnectionStrategy):
        """
        Add a new connection strategy.

        Strategies are automatically sorted by priority.

        Args:
            strategy: ConnectionStrategy instance to add

        Example:
            >>> orchestrator.add_strategy(HubWebRTCStrategy())
        """
        self.strategies.append(strategy)
        self.strategies.sort(key=lambda s: s.priority)
        logger.info("Added strategy %s (priority=%d)", strategy.name, strategy.priority)

    def remove_strategy(self, strategy_name: str):
        """
        Remove a connection strategy by name.

        Args:
            strategy_name: Name of strategy to remove

        Example:
            >>> orchestrator.remove_strategy("hub_webrtc")
        """
        self.strategies = [s for s in self.strategies if s.name != strategy_name]
        logger.info("Removed strategy %s", strategy_name)
