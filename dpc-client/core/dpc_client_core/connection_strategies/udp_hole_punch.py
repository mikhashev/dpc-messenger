"""
UDP Hole Punch Connection Strategy - Priority 4

Attempts DHT-coordinated UDP hole punching for NAT traversal without STUN/TURN servers.
Works for Cone NAT (60-70% of consumer routers) when Hub is unavailable.

Algorithm:
1. Discover external endpoints via DHT peers (no STUN server)
2. Detect NAT type (cone vs symmetric)
3. Coordinate simultaneous UDP send via DHT (birthday paradox)
4. Upgrade to DTLS for encryption (v0.10.1)

Requirements:
- Peer must support hole punching (advertised in DHT)
- Both peers must have Cone NAT (symmetric NAT fails)
- DHT must have active peers for endpoint discovery

Success Rate:
- Cone NAT: 60-70% success
- Symmetric NAT: 0% (falls back to relay)
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from .base import ConnectionStrategy, StrategyNotApplicableError
from ..transports import DTLSPeerConnection, UDPPeerConnection, DTLSHandshakeError, DTLSCertificateError

if TYPE_CHECKING:
    from ..models.peer_endpoint import PeerEndpoint
    from ..coordinators.connection_orchestrator import ConnectionOrchestrator

logger = logging.getLogger(__name__)


class UDPHolePunchStrategy(ConnectionStrategy):
    """
    UDP hole punching via DHT coordination (no STUN/TURN servers).

    Priority 4 - Tries UDP hole punching when Hub unavailable.
    Requires Cone NAT on both sides (60-70% of consumer routers).

    Example:
        >>> strategy = UDPHolePunchStrategy()
        >>> if strategy.is_applicable(endpoints):
        ...     connection = await strategy.connect(node_id, endpoints, orchestrator)
    """

    name = "udp_hole_punch"
    priority = 4
    timeout = 15.0  # 15 seconds for hole punching (discovery + coordination + punch)

    def is_applicable(self, endpoints: "PeerEndpoint") -> bool:
        """
        Check if UDP hole punching is applicable.

        This strategy is applicable when:
        1. Peer advertised hole punch support in DHT
        2. Peer has Cone NAT or No NAT (symmetric NAT not supported)

        Args:
            endpoints: Peer endpoint information

        Returns:
            True if hole punching is supported and NAT type is compatible

        Note:
            Symmetric NAT requires relay fallback (Priority 5).
        """
        if not endpoints.supports_hole_punching():
            return False

        # Only works for Cone NAT or No NAT
        # Symmetric NAT requires relay (will be tried next in fallback)
        if endpoints.punch:
            nat_type = endpoints.punch.nat_type
            return nat_type in ["cone", "none", "unknown"]

        return True

    async def connect(
        self,
        node_id: str,
        endpoints: "PeerEndpoint",
        orchestrator: "ConnectionOrchestrator"
    ):
        """
        Attempt UDP hole punching connection.

        Uses HolePunchManager to:
        1. Discover our external endpoint via DHT peers
        2. Detect our NAT type (cone vs symmetric)
        3. Coordinate simultaneous UDP send with peer
        4. Create bidirectional NAT mapping (birthday paradox)
        5. Upgrade to DTLS for encryption (v0.10.1)

        Args:
            node_id: Target node identifier
            endpoints: Peer endpoint information
            orchestrator: Connection orchestrator

        Returns:
            PeerConnection instance (UDP socket wrapper)

        Raises:
            StrategyNotApplicableError: If hole punching not supported
            ConnectionError: If hole punching fails

        Algorithm:
            1. Discover our external endpoint (query 3 DHT peers)
            2. Check if we have Cone NAT (symmetric NAT fails)
            3. Get peer's external endpoint from DHT announcement
            4. Coordinate punch timing via DHT (5 seconds from now)
            5. Both peers send at exact same time
            6. NAT creates bidirectional mapping
            7. Verify with ping/pong
            8. Upgrade to DTLS and return encrypted connection
        """
        if not self.is_applicable(endpoints):
            raise StrategyNotApplicableError(
                f"Peer {node_id[:20]} does not support hole punching or has symmetric NAT"
            )

        # Check if hole_punch_manager is available
        if not hasattr(orchestrator, 'hole_punch_manager') or not orchestrator.hole_punch_manager:
            raise StrategyNotApplicableError("HolePunchManager not initialized")

        logger.info(
            "Attempting UDP hole punch to %s (peer NAT type: %s)",
            node_id[:20], endpoints.punch.nat_type if endpoints.punch else "unknown"
        )

        try:
            # Step 1: Discover our external endpoint
            local_endpoint = await orchestrator.hole_punch_manager.discover_external_endpoint()

            if not local_endpoint:
                logger.warning("Failed to discover external endpoint for hole punching")
                raise ConnectionError("External endpoint discovery failed")

            # Step 2: Check our NAT type
            if local_endpoint.nat_type == "symmetric":
                logger.warning("Symmetric NAT detected - hole punching will fail")
                raise StrategyNotApplicableError("Symmetric NAT not supported by hole punching")

            logger.info(
                "Local endpoint discovered: %s:%d (NAT type: %s, confidence: %.0f%%)",
                local_endpoint.ip, local_endpoint.port,
                local_endpoint.nat_type, local_endpoint.confidence * 100
            )

            # Step 3: Get peer's external endpoint from DHT announcement
            if not endpoints.punch or not endpoints.punch.external_endpoint:
                logger.warning("Peer has not announced external endpoint")
                raise ConnectionError("Peer external endpoint not available")

            # Parse peer endpoint "ip:port"
            peer_endpoint_str = endpoints.punch.external_endpoint
            if ":" not in peer_endpoint_str:
                raise ValueError(f"Invalid peer endpoint format: {peer_endpoint_str}")

            peer_ip, peer_port_str = peer_endpoint_str.rsplit(":", 1)
            peer_port = int(peer_port_str)
            peer_endpoint = (peer_ip, peer_port)

            # Step 4-7: Coordinate and perform hole punching
            logger.info(
                "Starting hole punch: local=%s:%d, peer=%s:%d",
                local_endpoint.ip, local_endpoint.port,
                peer_ip, peer_port
            )

            sock = await asyncio.wait_for(
                orchestrator.hole_punch_manager.punch_hole(
                    peer_node_id=node_id,
                    peer_endpoint=peer_endpoint,
                    local_endpoint=(local_endpoint.ip, local_endpoint.port)
                ),
                timeout=self.timeout
            )

            if not sock:
                raise ConnectionError("Hole punching failed - no response from peer")

            logger.info("UDP hole punch successful to %s, upgrading to DTLS...", node_id[:20])

            # Step 8: Upgrade to DTLS for encryption
            try:
                # Create DTLS connection wrapper
                dtls_conn = DTLSPeerConnection(
                    udp_socket=sock,
                    remote_addr=peer_endpoint,
                    expected_node_id=node_id,
                    is_server=False,  # We initiated the connection (client role)
                    handshake_timeout=3.0
                )

                # Perform DTLS handshake
                await dtls_conn.connect(timeout=3.0)

                # Wrap in UDPPeerConnection for PeerConnection compatibility
                peer_connection = UDPPeerConnection(node_id=node_id, dtls_conn=dtls_conn)

                logger.info(
                    "UDP hole punch + DTLS successful to %s (encrypted)",
                    node_id[:20]
                )

                return peer_connection

            except (DTLSHandshakeError, DTLSCertificateError) as e:
                logger.error(
                    "DTLS handshake failed after successful hole punch to %s: %s",
                    node_id[:20], e
                )
                # Close the UDP socket
                sock.close()
                raise ConnectionError(f"DTLS handshake failed: {e}")

        except asyncio.TimeoutError:
            logger.warning(
                "UDP hole punch timeout to %s (after %.1fs)",
                node_id[:20], self.timeout
            )
            raise ConnectionError(f"Hole punch timeout to {node_id[:20]}")
        except Exception as e:
            logger.warning(
                "UDP hole punch failed to %s: %s",
                node_id[:20], e
            )
            raise ConnectionError(f"Hole punch failed: {e}")
