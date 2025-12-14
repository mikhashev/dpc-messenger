"""
Hole Punch Manager - DHT-Coordinated UDP NAT Traversal (Phase 6 Week 2)

This module implements UDP hole punching without requiring STUN/TURN servers.
It uses DHT peers to discover external endpoints and coordinates simultaneous
UDP sends to create bidirectional NAT mappings (birthday paradox).

Algorithm:
1. **Endpoint Discovery**: Query random DHT peers for our external IP:port
   - Send UDP to 3 random peers
   - Peers respond with source IP:port they see
   - Use majority vote for reflexive address

2. **NAT Type Detection**: Determine if hole punching will work
   - Send from same port to 2 different peers
   - Compare reflexive ports
   - Same → Cone NAT (60-70% success)
   - Different → Symmetric NAT (won't work, need relay)

3. **Coordinated Hole Punching**: Simultaneous UDP send
   - Store sync timestamp in DHT
   - Both peers send at exact same time
   - NAT creates bidirectional mapping
   - Upgrade to DTLS for encryption

Success Rate:
- Cone NAT: 60-70% (most consumer routers)
- Symmetric NAT: 0% (need relay fallback)
- CGNAT: 0% (need relay fallback)
"""

import asyncio
import logging
import socket
import time
from typing import Optional, Tuple, List, Dict
from dataclasses import dataclass
from collections import Counter

logger = logging.getLogger(__name__)


@dataclass
class ExternalEndpoint:
    """Discovered external endpoint information."""
    ip: str
    port: int
    confidence: float  # 0.0-1.0 (based on peer agreement)
    nat_type: str  # "none" | "cone" | "symmetric" | "unknown"
    discovered_at: float  # UNIX timestamp


class HolePunchManager:
    """
    Manages UDP hole punching for NAT traversal without STUN/TURN.

    Uses DHT peers for endpoint discovery and coordinates simultaneous
    sends for creating bidirectional NAT mappings.

    Attributes:
        dht_manager: DHT manager for peer queries
        local_port: UDP port for hole punching
        punch_socket: UDP socket for hole punching
        discovered_endpoint: Cached external endpoint info

    Example:
        >>> manager = HolePunchManager(dht_manager, punch_port=8890)
        >>> await manager.start()
        >>>
        >>> # Discover our external endpoint
        >>> endpoint = await manager.discover_external_endpoint()
        >>> print(f"External: {endpoint.ip}:{endpoint.port}, NAT: {endpoint.nat_type}")
        >>>
        >>> # Punch hole to peer
        >>> sock = await manager.punch_hole(peer_node_id, peer_endpoint)
        >>> # Now use sock for communication
    """

    def __init__(
        self,
        dht_manager,
        punch_port: int = 8890,
        discovery_peers: int = 3,
        punch_timeout: float = 10.0
    ):
        """
        Initialize hole punch manager.

        Args:
            dht_manager: DHT manager for peer queries
            punch_port: UDP port for hole punching
            discovery_peers: Number of peers to query for endpoint discovery
            punch_timeout: Timeout for hole punching attempts
        """
        self.dht_manager = dht_manager
        self.local_port = punch_port
        self.discovery_peers = discovery_peers
        self.punch_timeout = punch_timeout

        self.punch_socket: Optional[socket.socket] = None
        self.discovered_endpoint: Optional[ExternalEndpoint] = None
        self._running = False

        # Success rate tracking
        self.punch_attempts = 0
        self.punch_successes = 0

        logger.info(
            "HolePunchManager initialized (port=%d, discovery_peers=%d)",
            punch_port, discovery_peers
        )

    async def start(self):
        """
        Start hole punch manager and bind UDP socket.

        Creates UDP socket for hole punching on specified port.
        """
        if self._running:
            logger.warning("HolePunchManager already running")
            return

        try:
            # Create UDP socket for hole punching
            self.punch_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.punch_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.punch_socket.bind(("0.0.0.0", self.local_port))
            self.punch_socket.setblocking(False)

            self._running = True
            logger.info("HolePunchManager started on UDP port %d", self.local_port)

        except Exception as e:
            logger.error("Failed to start HolePunchManager: %s", e)
            raise

    async def stop(self):
        """Stop hole punch manager and close socket."""
        if not self._running:
            return

        if self.punch_socket:
            self.punch_socket.close()
            self.punch_socket = None

        self._running = False
        logger.info("HolePunchManager stopped")

    async def discover_external_endpoint(self, force_refresh: bool = False) -> Optional[ExternalEndpoint]:
        """
        Discover external IP:port by querying DHT peers.

        Queries random DHT peers to discover our reflexive address
        (source IP:port they see when we send UDP packets).

        Args:
            force_refresh: Force new discovery even if cached

        Returns:
            ExternalEndpoint with discovered info, or None if discovery failed

        Algorithm:
            1. Select 3 random DHT peers
            2. Send DISCOVER_ENDPOINT UDP request to each
            3. Peers respond with our source IP:port
            4. Use majority vote for IP and port
            5. Cache result

        Example:
            >>> endpoint = await manager.discover_external_endpoint()
            >>> if endpoint:
            ...     print(f"External: {endpoint.ip}:{endpoint.port}")
            ...     print(f"NAT type: {endpoint.nat_type}")
            ...     print(f"Confidence: {endpoint.confidence:.0%}")
        """
        # Return cached if available and not forcing refresh
        if not force_refresh and self.discovered_endpoint:
            # Cache expires after 5 minutes
            if time.time() - self.discovered_endpoint.discovered_at < 300:
                logger.debug("Using cached external endpoint")
                return self.discovered_endpoint

        logger.info("Discovering external endpoint via DHT peers")

        # Get random DHT peers for discovery
        all_peers = self.dht_manager.get_known_peers()
        if len(all_peers) < self.discovery_peers:
            logger.warning(
                "Not enough DHT peers for discovery (%d < %d)",
                len(all_peers), self.discovery_peers
            )
            return None

        # Select random peers
        import random
        discovery_peers = random.sample(all_peers, self.discovery_peers)

        # Query each peer for our reflexive address
        responses: List[Tuple[str, int]] = []
        for peer in discovery_peers:
            try:
                # Send DISCOVER_ENDPOINT request via DHT RPC
                # (RPC handler will be implemented next)
                response = await asyncio.wait_for(
                    self.dht_manager.rpc_handler.discover_endpoint(peer.ip, peer.port),
                    timeout=5.0
                )

                if response and "reflexive_ip" in response and "reflexive_port" in response:
                    reflexive_ip = response["reflexive_ip"]
                    reflexive_port = response["reflexive_port"]
                    responses.append((reflexive_ip, reflexive_port))
                    logger.debug(
                        "Peer %s reports reflexive address: %s:%d",
                        peer.node_id[:20], reflexive_ip, reflexive_port
                    )

            except asyncio.TimeoutError:
                logger.debug("Endpoint discovery timeout from peer %s", peer.node_id[:20])
            except Exception as e:
                logger.debug("Endpoint discovery failed from peer %s: %s", peer.node_id[:20], e)

        if not responses:
            logger.warning("No responses from endpoint discovery")
            return None

        # Use majority vote for IP and port
        ips = [r[0] for r in responses]
        ports = [r[1] for r in responses]

        most_common_ip = Counter(ips).most_common(1)[0]
        most_common_port = Counter(ports).most_common(1)[0]

        external_ip = most_common_ip[0]
        external_port = most_common_port[0]
        ip_confidence = most_common_ip[1] / len(responses)
        port_confidence = most_common_port[1] / len(responses)
        confidence = min(ip_confidence, port_confidence)

        logger.info(
            "Discovered external endpoint: %s:%d (confidence=%.0f%%)",
            external_ip, external_port, confidence * 100
        )

        # Detect NAT type (will be implemented next)
        nat_type = await self._detect_nat_type(discovery_peers)

        # Cache result
        self.discovered_endpoint = ExternalEndpoint(
            ip=external_ip,
            port=external_port,
            confidence=confidence,
            nat_type=nat_type,
            discovered_at=time.time()
        )

        return self.discovered_endpoint

    async def _detect_nat_type(self, peers: List) -> str:
        """
        Detect NAT type (cone vs symmetric).

        Sends from same local port to 2 different peers and compares
        the reflexive ports they report.

        Args:
            peers: List of DHT peers (at least 2)

        Returns:
            "cone" | "symmetric" | "none" | "unknown"

        Algorithm:
            1. Send from same port to Peer A
            2. Send from same port to Peer B
            3. Compare reflexive ports
            4. If same → Cone NAT (hole punching works)
            5. If different → Symmetric NAT (hole punching fails)

        Notes:
            - Cone NAT: Same external port for all destinations (~60-70%)
            - Symmetric NAT: Different external port per destination (~30-40%)
            - No NAT: Reflexive address matches local address
        """
        if len(peers) < 2:
            logger.warning("Not enough peers for NAT type detection")
            return "unknown"

        logger.info("Detecting NAT type")

        # Query first two peers
        peer_a = peers[0]
        peer_b = peers[1]

        port_a: Optional[int] = None
        port_b: Optional[int] = None

        try:
            # Query peer A
            response_a = await asyncio.wait_for(
                self.dht_manager.rpc_handler.discover_endpoint(peer_a.ip, peer_a.port),
                timeout=5.0
            )
            if response_a and "reflexive_port" in response_a:
                port_a = response_a["reflexive_port"]

            # Query peer B
            response_b = await asyncio.wait_for(
                self.dht_manager.rpc_handler.discover_endpoint(peer_b.ip, peer_b.port),
                timeout=5.0
            )
            if response_b and "reflexive_port" in response_b:
                port_b = response_b["reflexive_port"]

        except Exception as e:
            logger.warning("NAT type detection failed: %s", e)
            return "unknown"

        if port_a is None or port_b is None:
            logger.warning("Failed to get reflexive ports from peers")
            return "unknown"

        # Compare ports
        if port_a == port_b:
            if port_a == self.local_port:
                nat_type = "none"
                logger.info("No NAT detected (reflexive port == local port)")
            else:
                nat_type = "cone"
                logger.info("Cone NAT detected (same reflexive port to different peers)")
        else:
            nat_type = "symmetric"
            logger.info("Symmetric NAT detected (different reflexive ports per peer)")

        return nat_type

    async def punch_hole(
        self,
        peer_node_id: str,
        peer_endpoint: Tuple[str, int],
        local_endpoint: Tuple[str, int]
    ) -> Optional[socket.socket]:
        """
        Perform coordinated UDP hole punching with peer.

        Uses DHT to coordinate timing and performs simultaneous UDP
        sends to create bidirectional NAT mapping (birthday paradox).

        Args:
            peer_node_id: Target peer's node identifier
            peer_endpoint: Peer's external (IP, port)
            local_endpoint: Our external (IP, port)

        Returns:
            UDP socket with punched hole, or None if failed

        Algorithm:
            1. Store coordination info in DHT
            2. Agree on sync timestamp (5 seconds from now)
            3. Both peers send at exact same time
            4. NAT creates bidirectional mapping
            5. Verify hole with ping/pong
            6. Return socket for use

        Example:
            >>> sock = await manager.punch_hole(
            ...     "dpc-node-abc123",
            ...     ("203.0.113.50", 12345),
            ...     ("198.51.100.25", 54321)
            ... )
            >>> if sock:
            ...     # Use socket for communication
            ...     sock.sendto(b"Hello", peer_endpoint)
        """
        logger.info(
            "Attempting hole punch to %s at %s:%d",
            peer_node_id[:20], peer_endpoint[0], peer_endpoint[1]
        )

        # Track attempt
        self.punch_attempts += 1

        # Step 1: Coordinate timing via DHT
        sync_time = time.time() + 5.0  # 5 seconds from now

        coordination_key = f"punch:{peer_node_id}:{self.dht_manager.node_id}"
        coordination_data = {
            "sync_time": sync_time,
            "local_endpoint": f"{local_endpoint[0]}:{local_endpoint[1]}",
            "peer_endpoint": f"{peer_endpoint[0]}:{peer_endpoint[1]}",
            "timestamp": time.time()
        }

        # Store coordination info in DHT
        try:
            # Find closest nodes for this key
            import json
            closest = await self.dht_manager.find_node(coordination_key)
            if closest:
                # Store on first node
                await self.dht_manager.rpc_handler.store(
                    closest[0].ip,
                    closest[0].port,
                    coordination_key,
                    json.dumps(coordination_data)
                )
                logger.debug("Stored hole punch coordination in DHT")
        except Exception as e:
            logger.warning("Failed to store coordination info: %s", e)

        # Step 2: Wait until sync time
        wait_time = sync_time - time.time()
        if wait_time > 0:
            logger.debug("Waiting %.2fs until sync time", wait_time)
            await asyncio.sleep(wait_time)

        # Step 3: Simultaneous send (birthday paradox)
        try:
            # Send punch packet
            punch_message = b"PUNCH"
            self.punch_socket.sendto(punch_message, peer_endpoint)
            logger.info("Sent punch packet to %s:%d", peer_endpoint[0], peer_endpoint[1])

            # Wait for response (with timeout)
            loop = asyncio.get_event_loop()
            try:
                data, addr = await asyncio.wait_for(
                    loop.sock_recvfrom(self.punch_socket, 1024),
                    timeout=self.punch_timeout
                )

                if data == b"PUNCH" and addr[0] == peer_endpoint[0]:
                    logger.info("Hole punch successful! Received response from %s:%d", addr[0], addr[1])
                    # Track success
                    self.punch_successes += 1
                    return self.punch_socket
                else:
                    logger.warning("Received unexpected data from %s", addr)
                    return None

            except asyncio.TimeoutError:
                logger.warning("Hole punch timeout - no response from peer")
                return None

        except Exception as e:
            logger.error("Hole punch failed: %s", e)
            return None

    def get_success_rate(self) -> float:
        """
        Calculate hole punch success rate.

        Returns:
            Success rate (0.0-1.0), or 0.0 if no attempts yet
        """
        if self.punch_attempts == 0:
            return 0.0
        return self.punch_successes / self.punch_attempts

    def get_stats(self) -> Dict:
        """
        Get hole punch manager statistics.

        Returns:
            Dict with endpoint info, NAT type, and success rate
        """
        return {
            "running": self._running,
            "local_port": self.local_port,
            "discovered_endpoint": {
                "ip": self.discovered_endpoint.ip,
                "port": self.discovered_endpoint.port,
                "nat_type": self.discovered_endpoint.nat_type,
                "confidence": self.discovered_endpoint.confidence,
            } if self.discovered_endpoint else None,
            "punch_attempts": self.punch_attempts,
            "punch_successes": self.punch_successes,
            "success_rate": self.get_success_rate(),
        }
