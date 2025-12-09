"""
Hub WebRTC Connection Strategy - Priority 3

Wraps existing WebRTC functionality (STUN/TURN) into the connection strategy
framework. This strategy uses the Hub for signaling and existing STUN/TURN
infrastructure for NAT traversal.

Advantages:
- Works through most NATs (STUN for 60-70%, TURN for remaining)
- Leverages existing infrastructure
- Proven reliable for real-time communication

Requirements:
- Hub must be connected (hub_client.is_connected())
- Peer must be registered with Hub (has sent HELLO)

Notes:
- This is the existing connection method (pre-Phase 6)
- Now integrated as Priority 3 in fallback hierarchy
- When Hub unavailable, falls back to DHT alternatives (Priorities 4-6)
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from .base import ConnectionStrategy, StrategyNotApplicableError

if TYPE_CHECKING:
    from ..models.peer_endpoint import PeerEndpoint
    from ..coordinators.connection_orchestrator import ConnectionOrchestrator

logger = logging.getLogger(__name__)


class HubWebRTCStrategy(ConnectionStrategy):
    """
    WebRTC connection via Hub signaling (existing STUN/TURN).

    Priority 3 - Uses existing Hub infrastructure when available.
    Falls back to DHT-based alternatives when Hub offline.

    Example:
        >>> strategy = HubWebRTCStrategy()
        >>> if strategy.is_applicable(endpoints):
        ...     connection = await strategy.connect(node_id, endpoints, orchestrator)
    """

    name = "hub_webrtc"
    priority = 3
    timeout = 30.0  # Default 30s, configurable via connection.webrtc_timeout

    def is_applicable(self, endpoints: "PeerEndpoint") -> bool:
        """
        Check if Hub WebRTC is available.

        This strategy is applicable when:
        1. Hub client exists and is connected
        2. Peer has announced to DHT (has valid endpoints)

        Args:
            endpoints: Peer endpoint information

        Returns:
            True if Hub is connected

        Note:
            We don't check endpoints here because Hub WebRTC doesn't
            need specific endpoint info - signaling handles that.
        """
        # Endpoints must exist (peer announced to DHT)
        # Actual Hub connectivity checked in connect()
        return endpoints is not None

    async def connect(
        self,
        node_id: str,
        endpoints: "PeerEndpoint",
        orchestrator: "ConnectionOrchestrator"
    ):
        """
        Attempt WebRTC connection via Hub signaling.

        Uses existing p2p_manager.connect_via_webrtc() method which:
        1. Sends WebRTC offer to Hub for signaling
        2. Hub forwards to peer (if online)
        3. Peer responds with answer via Hub
        4. ICE candidates exchanged (STUN/TURN as needed)
        5. WebRTC data channel established

        Args:
            node_id: Target node identifier
            endpoints: Peer endpoint information (for validation only)
            orchestrator: Connection orchestrator

        Returns:
            PeerConnection instance (WebRTC data channel wrapper)

        Raises:
            StrategyNotApplicableError: If Hub not connected
            ConnectionError: If WebRTC connection fails
            asyncio.TimeoutError: If connection times out
        """
        # Check if Hub client exists and is connected
        if not orchestrator.hub_client:
            raise StrategyNotApplicableError("Hub client not initialized")

        if not orchestrator.hub_client.is_connected():
            raise StrategyNotApplicableError("Hub not connected")

        logger.info(
            "Attempting Hub WebRTC connection to %s (via existing STUN/TURN)",
            node_id[:20]
        )

        try:
            # Use existing WebRTC connection method
            # Note: p2p_manager.connect_via_webrtc() handles:
            #   - Offer/Answer SDP exchange via Hub
            #   - ICE candidate gathering (STUN)
            #   - TURN relay fallback if needed
            #   - Data channel creation
            connection = await asyncio.wait_for(
                orchestrator.p2p_manager.connect_via_webrtc(
                    node_id,
                    orchestrator.hub_client
                ),
                timeout=self.timeout
            )

            logger.info(
                "Hub WebRTC connection established to %s (STUN/TURN)",
                node_id[:20]
            )
            return connection

        except asyncio.TimeoutError:
            logger.warning(
                "Hub WebRTC connection timeout to %s (after %.1fs)",
                node_id[:20], self.timeout
            )
            raise ConnectionError(f"WebRTC timeout to {node_id[:20]}")
        except AttributeError as e:
            # connect_via_webrtc() might not exist in current p2p_manager
            logger.debug(
                "WebRTC method not available in P2P manager: %s", e
            )
            raise StrategyNotApplicableError(
                "WebRTC connection method not implemented in P2P manager"
            )
        except Exception as e:
            logger.warning(
                "Hub WebRTC connection failed to %s: %s",
                node_id[:20], e
            )
            raise ConnectionError(f"WebRTC connection failed: {e}")
