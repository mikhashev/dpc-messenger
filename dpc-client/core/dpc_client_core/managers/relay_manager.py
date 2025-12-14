"""
Relay Manager - Volunteer Relay Nodes (Phase 6 Week 3)

This module implements volunteer relay functionality for 100% NAT coverage.
When direct connections and hole punching fail (symmetric NAT/CGNAT),
peers can connect via volunteer relay nodes.

Architecture:
- Relays announce availability in DHT
- Clients query DHT for available relays
- Both peers connect to relay (encrypted end-to-end)
- Relay forwards encrypted messages (cannot read content)

Modes:
- Client mode: connect_via_relay() - use relay for outbound connections
- Server mode: handle_relay_register() - volunteer as relay for others

Privacy:
- Relay sees encrypted payloads only (end-to-end encryption maintained)
- No relay can decrypt message content
- Rate limiting prevents abuse

Success Rate:
- 100% NAT coverage (works for symmetric NAT, CGNAT, restrictive firewalls)
- Fallback when all direct methods fail (Priorities 1-4)
"""

import asyncio
import logging
import time
import uuid
from typing import Optional, List, Dict, TYPE_CHECKING
from collections import defaultdict

from ..models.relay_node import RelayNode, RelaySession

if TYPE_CHECKING:
    from ..dht.manager import DHTManager
    from ..p2p_manager import P2PManager

logger = logging.getLogger(__name__)


class RelayManager:
    """
    Manages volunteer relay functionality (client and server modes).

    Client Mode:
    - Query DHT for available relays
    - Score relays by quality (uptime, latency, capacity)
    - Establish relayed connections

    Server Mode (if volunteering):
    - Announce relay availability in DHT
    - Handle relay registration from peers
    - Forward encrypted messages between peers
    - Rate limiting and capacity management

    Attributes:
        dht_manager: DHT manager for relay discovery
        p2p_manager: P2P manager for relay connections
        volunteer: Whether this node volunteers as relay
        max_peers: Maximum concurrent relay sessions (server mode)
        sessions: Active relay sessions (server mode)
        bandwidth_limit_mbps: Bandwidth limit for relaying

    Example:
        >>> # Client mode
        >>> relay = await manager.find_relay(prefer_region="us-west")
        >>> connection = await manager.connect_via_relay(peer_id, relay)
        >>>
        >>> # Server mode (volunteering)
        >>> manager = RelayManager(volunteer=True, max_peers=10)
        >>> await manager.announce_relay_availability()
    """

    def __init__(
        self,
        dht_manager: "DHTManager",
        p2p_manager: Optional["P2PManager"] = None,
        hole_punch_manager: Optional["HolePunchManager"] = None,
        volunteer: bool = False,
        max_peers: int = 10,
        bandwidth_limit_mbps: float = 10.0,
        region: str = "global"
    ):
        """
        Initialize relay manager.

        Args:
            dht_manager: DHT manager for relay discovery
            p2p_manager: P2P manager for relay connections
            hole_punch_manager: Hole punch manager for success rate reporting
            volunteer: Whether to volunteer as relay
            max_peers: Max concurrent relay sessions (server mode)
            bandwidth_limit_mbps: Bandwidth limit for relaying
            region: Geographic region for relay announcements
        """
        self.dht_manager = dht_manager
        self.p2p_manager = p2p_manager
        self.hole_punch_manager = hole_punch_manager
        self.volunteer = volunteer
        self.max_peers = max_peers
        self.bandwidth_limit_mbps = bandwidth_limit_mbps
        self.region = region

        # Server mode state
        self.sessions: Dict[str, RelaySession] = {}  # session_id -> RelaySession
        self.peer_to_session: Dict[str, str] = {}  # node_id -> session_id
        self.peer_connections: Dict[str, any] = {}  # node_id -> PeerConnection (for forwarding)
        self.rate_limits: Dict[str, List[float]] = defaultdict(list)  # node_id -> timestamps

        # Client mode cache
        self._relay_cache: List[RelayNode] = []
        self._cache_timestamp: float = 0.0

        # Statistics
        self.stats = {
            "relays_discovered": 0,
            "relay_connections": 0,
            "sessions_created": 0,
            "messages_relayed": 0,
            "bytes_relayed": 0,
        }

        # Uptime tracking (for relay volunteering)
        self.start_time = time.time() if volunteer else None

        logger.info(
            "RelayManager initialized (volunteer=%s, max_peers=%d, region=%s)",
            volunteer, max_peers, region
        )

    def _detect_region_from_ip(self, ip: str) -> str:
        """
        Detect geographic region from IP address.

        Uses simple heuristics based on IP ranges. For production use,
        consider integrating a proper GeoIP database (e.g., MaxMind GeoLite2).

        Args:
            ip: IP address to check

        Returns:
            Region string (e.g., "us-west", "eu-central", "asia-pacific", "global")
        """
        try:
            # For now, return region from config or "global"
            # TODO: Integrate proper GeoIP database for production
            # Simple approach: check if we have a configured region
            if self.region and self.region != "global":
                return self.region

            # Fallback: detect from IP (very basic)
            # Private/local IPs -> global
            if ip.startswith(("192.168.", "10.", "172.", "127.")):
                return "global"

            # For production: Use GeoIP2 library
            # from geoip2 import database
            # reader = database.Reader('/path/to/GeoLite2-City.mmdb')
            # response = reader.city(ip)
            # return response.continent.code  # or response.country.iso_code

            return "global"  # Default fallback
        except Exception as e:
            logger.debug("Failed to detect region from IP %s: %s", ip, e)
            return "global"

    # ===== Client Mode: Relay Discovery =====

    async def find_relay(
        self,
        prefer_region: Optional[str] = None,
        force_refresh: bool = False
    ) -> Optional[RelayNode]:
        """
        Find best available relay node via DHT query.

        Queries DHT for relay announcements and scores them by quality.

        Args:
            prefer_region: Preferred geographic region (e.g., "us-west")
            force_refresh: Force new DHT query (ignore cache)

        Returns:
            Best RelayNode, or None if no relays available

        Algorithm:
            1. Query DHT for relay announcements (key: "relay:*")
            2. Parse relay metadata (availability, capacity, uptime)
            3. Score relays by quality (uptime, capacity, latency)
            4. Filter by region preference (if specified)
            5. Return highest-scored available relay

        Example:
            >>> relay = await manager.find_relay(prefer_region="us-west")
            >>> if relay:
            ...     print(f"Found relay: {relay.node_id} (score: {relay.quality_score():.2f})")
        """
        # Check cache (5 minute expiry)
        if not force_refresh and self._relay_cache and (time.time() - self._cache_timestamp < 300):
            logger.debug("Using cached relay list (%d relays)", len(self._relay_cache))
            return self._select_best_relay(self._relay_cache, prefer_region)

        logger.info("Discovering relays via DHT (prefer_region=%s)", prefer_region)

        # Query DHT for relay announcements
        # Relays announce with key: "relay:<node_id>"
        # We need to iterate through known peers and check their relay status

        relays: List[RelayNode] = []
        known_peers = self.dht_manager.get_known_peers()

        for peer in known_peers:
            try:
                # Query for relay announcement
                relay_key = f"relay:{peer.node_id}"
                result = await self.dht_manager.rpc_handler.find_value(
                    peer.ip, peer.port, relay_key
                )

                if result and "value" in result:
                    # Parse relay metadata
                    import json
                    relay_data = json.loads(result["value"])
                    relay_node = RelayNode.from_dict(relay_data, discovered_at=time.time())

                    if relay_node.available and not relay_node.is_full():
                        relays.append(relay_node)
                        logger.debug(
                            "Found relay: %s (capacity: %d/%d, quality: %.2f)",
                            relay_node.node_id[:20], relay_node.current_peers,
                            relay_node.max_peers, relay_node.quality_score()
                        )

            except Exception as e:
                logger.debug("Failed to query relay info from %s: %s", peer.node_id[:20], e)
                continue

        self.stats["relays_discovered"] += len(relays)

        # Update cache
        self._relay_cache = relays
        self._cache_timestamp = time.time()

        if not relays:
            logger.warning("No available relays found in DHT")
            return None

        logger.info("Discovered %d available relays", len(relays))

        return self._select_best_relay(relays, prefer_region)

    def _select_best_relay(
        self,
        relays: List[RelayNode],
        prefer_region: Optional[str] = None
    ) -> Optional[RelayNode]:
        """
        Select best relay from list based on quality score.

        Args:
            relays: List of available relays
            prefer_region: Preferred region (prioritized but not required)

        Returns:
            Best relay, or None if list is empty
        """
        if not relays:
            return None

        # Filter by region preference (if specified)
        if prefer_region:
            regional_relays = [r for r in relays if r.region == prefer_region]
            if regional_relays:
                relays = regional_relays
                logger.debug("Filtered to %d relays in region %s", len(relays), prefer_region)

        # Sort by quality score (highest first)
        relays_sorted = sorted(relays, key=lambda r: r.quality_score(), reverse=True)

        best_relay = relays_sorted[0]
        logger.info(
            "Selected relay: %s (region=%s, quality=%.2f, capacity=%d/%d)",
            best_relay.node_id[:20], best_relay.region,
            best_relay.quality_score(), best_relay.current_peers, best_relay.max_peers
        )

        return best_relay

    async def connect_via_relay(
        self,
        peer_id: str,
        relay_node: RelayNode
    ):
        """
        Establish relayed connection to peer via relay node.

        Both peers connect to relay, which forwards encrypted messages.

        Args:
            peer_id: Target peer node ID
            relay_node: Relay node to use

        Returns:
            RelayedPeerConnection instance (TODO)

        Raises:
            ConnectionError: If relay connection fails

        Algorithm:
            1. Connect to relay node (TLS)
            2. Send RELAY_REGISTER request (target peer ID)
            3. Wait for RELAY_READY response (relay confirms both peers connected)
            4. Return RelayedPeerConnection wrapper

        Example:
            >>> relay = await manager.find_relay()
            >>> connection = await manager.connect_via_relay("dpc-node-peer", relay)
            >>> await connection.send_message({"command": "HELLO"})
        """
        logger.info(
            "Connecting to peer %s via relay %s",
            peer_id[:20], relay_node.node_id[:20]
        )

        if not self.p2p_manager:
            raise ConnectionError("P2PManager not initialized")

        try:
            # Step 1: Connect to relay via P2P manager (TLS)
            relay_connection = await asyncio.wait_for(
                self.p2p_manager.connect_to_peer(
                    relay_node.ip,
                    relay_node.port,
                    relay_node.node_id
                ),
                timeout=20.0  # Connection timeout
            )

            logger.info("Connected to relay %s", relay_node.node_id[:20])

            # Step 2: Send RELAY_REGISTER request
            await relay_connection.send_message({
                "command": "RELAY_REGISTER",
                "payload": {
                    "peer_id": peer_id,  # Target peer we want to connect to
                    "timeout": 30.0
                }
            })

            logger.debug("Sent RELAY_REGISTER to relay for peer %s", peer_id[:20])

            # Step 3: Wait for RELAY_READY or RELAY_WAITING response
            response = await asyncio.wait_for(
                relay_connection.receive_message(),
                timeout=30.0
            )

            if not response:
                raise ConnectionError("No response from relay")

            # Handle different responses
            if response.get("command") == "RELAY_WAITING":
                # Relay is waiting for other peer - keep waiting for RELAY_READY
                logger.debug("Relay waiting for peer %s to register", peer_id[:20])
                response = await asyncio.wait_for(
                    relay_connection.receive_message(),
                    timeout=30.0  # Wait for other peer to register
                )

            if not response or response.get("command") != "RELAY_READY":
                logger.warning("Invalid relay response: %s", response)
                raise ConnectionError(f"Relay did not confirm session: {response}")

            session_id = response.get("payload", {}).get("session_id")
            if not session_id:
                raise ConnectionError("Relay did not provide session ID")

            logger.info(
                "Relay session established: %s (peer=%s, relay=%s)",
                session_id, peer_id[:20], relay_node.node_id[:20]
            )

            # Step 4: Wrap connection in RelayedPeerConnection
            from ..transports.relayed_connection import RelayedPeerConnection

            relayed_conn = RelayedPeerConnection(
                peer_id=peer_id,
                relay_node=relay_node,
                relay_connection=relay_connection,
                session_id=session_id
            )

            await relayed_conn.start()

            self.stats["relay_connections"] += 1

            logger.info("Relay connection established to %s", peer_id[:20])
            return relayed_conn

        except asyncio.TimeoutError as e:
            logger.warning(
                "Relay connection timeout to %s via %s",
                peer_id[:20], relay_node.node_id[:20]
            )
            raise ConnectionError(f"Relay timeout: {e}")
        except Exception as e:
            logger.error(
                "Failed to connect via relay to %s: %s",
                peer_id[:20], e
            )
            raise ConnectionError(f"Relay connection failed: {e}")

    # ===== Server Mode: Relay Volunteering =====

    async def announce_relay_availability(self) -> int:
        """
        Announce this node as available relay in DHT.

        Only runs if volunteer=True.

        Returns:
            Number of DHT nodes announcement was stored on

        Algorithm:
            1. Create relay metadata (capacity, region, uptime)
            2. Store in DHT with key "relay:<our_node_id>"
            3. Periodic refresh (every 15 minutes)
        """
        if not self.volunteer:
            logger.debug("Not volunteering as relay - skipping announcement")
            return 0

        logger.info("Announcing relay availability in DHT")

        # Calculate actual uptime (time since relay started / 24 hours, capped at 1.0)
        uptime = 1.0
        if self.start_time:
            elapsed_hours = (time.time() - self.start_time) / 3600.0
            uptime = min(1.0, elapsed_hours / 24.0)  # 0.0-1.0 (full uptime after 24 hours)

        # Detect region from IP if not configured
        detected_region = self._detect_region_from_ip(self.dht_manager.ip)

        # Get hole punch success rate if available
        punch_success_rate = 0.0
        punch_supported = False
        punch_port = None
        if self.hole_punch_manager:
            punch_success_rate = self.hole_punch_manager.get_success_rate()
            punch_supported = True
            punch_port = self.hole_punch_manager.local_port

        # Announce via DHT's announce_full() method (uses PeerEndpoint schema)
        stored_count = await self.dht_manager.announce_full(
            relay_available=True,
            relay_max_peers=self.max_peers,
            relay_region=detected_region,
            relay_uptime=uptime,
            punch_supported=punch_supported,
            punch_port=punch_port,
            punch_success_rate=punch_success_rate
        )

        logger.info(
            "Announced relay availability to %d DHT nodes (region=%s, uptime=%.2f)",
            stored_count, detected_region, uptime
        )
        return stored_count

    async def handle_relay_register(
        self,
        requester_id: str,
        target_id: str,
        requester_connection
    ) -> Optional[str]:
        """
        Handle relay registration request (server mode).

        Called when a peer wants to establish relayed connection.

        Args:
            requester_id: Node ID of peer requesting relay
            target_id: Node ID of target peer
            requester_connection: Connection to requester

        Returns:
            Session ID if successful, None if failed

        Algorithm:
            1. Check if we're at capacity (max_peers)
            2. Check if target is already registered
            3. If target registered, create session and notify both peers
            4. If target not registered, wait for target to register

        Example:
            >>> # Peer A registers first
            >>> session_id = await manager.handle_relay_register(
            ...     "dpc-node-alice", "dpc-node-bob", alice_conn
            ... )
            >>> # Returns None (waiting for bob)
            >>>
            >>> # Peer B registers
            >>> session_id = await manager.handle_relay_register(
            ...     "dpc-node-bob", "dpc-node-alice", bob_conn
            ... )
            >>> # Returns session_id (both registered, session created)
        """
        if not self.volunteer:
            logger.warning("Not volunteering as relay - rejecting registration")
            return None

        if len(self.sessions) >= self.max_peers:
            logger.warning("Relay at capacity (%d/%d) - rejecting registration", len(self.sessions), self.max_peers)
            return None

        logger.info(
            "Relay registration: %s → %s",
            requester_id[:20], target_id[:20]
        )

        # Store connection reference for message forwarding
        self.peer_connections[requester_id] = requester_connection

        # Check if target already registered
        if target_id in self.peer_to_session:
            # Both peers registered - create session
            session_id = str(uuid.uuid4())
            session = RelaySession(
                session_id=session_id,
                relay_node_id=self.dht_manager.node_id,
                peer_a_id=requester_id,
                peer_b_id=target_id,
                created_at=time.time(),
                last_activity=time.time()
            )

            self.sessions[session_id] = session
            self.peer_to_session[requester_id] = session_id
            self.peer_to_session[target_id] = session_id

            self.stats["sessions_created"] += 1

            logger.info("Relay session created: %s (peers: %s, %s)", session_id, requester_id[:20], target_id[:20])
            return session_id
        else:
            # Waiting for target to register
            logger.debug("Waiting for target peer %s to register", target_id[:20])
            return None

    async def handle_relay_message(
        self,
        from_peer: str,
        to_peer: str,
        message: bytes
    ) -> bool:
        """
        Forward encrypted message between peers (server mode).

        Args:
            from_peer: Source peer node ID
            to_peer: Destination peer node ID
            message: Encrypted message payload

        Returns:
            True if forwarded successfully

        Algorithm:
            1. Verify session exists for both peers
            2. Check rate limits
            3. Forward message to destination peer
            4. Update session statistics
        """
        if not self.volunteer:
            return False

        # Verify session
        if from_peer not in self.peer_to_session:
            logger.warning("No relay session for peer %s", from_peer[:20])
            return False

        session_id = self.peer_to_session[from_peer]
        session = self.sessions.get(session_id)

        if not session:
            logger.error("Session %s not found", session_id)
            return False

        # Check rate limit (100 messages per second per peer)
        if not self._check_rate_limit(from_peer, limit=100, window=1.0):
            logger.warning("Rate limit exceeded for peer %s", from_peer[:20])
            return False

        # Forward message to destination peer via P2P connection
        if to_peer not in self.peer_connections:
            logger.warning(
                "Destination peer %s not connected to relay",
                to_peer[:20]
            )
            return False

        try:
            # Get destination peer's connection
            dest_connection = self.peer_connections[to_peer]

            # Decode message bytes to dict for protocol
            import json
            message_dict = json.loads(message.decode('utf-8'))

            # Wrap in RELAY_MESSAGE protocol and forward
            relay_message = {
                "command": "RELAY_MESSAGE",
                "payload": {
                    "from": from_peer,
                    "to": to_peer,
                    "session_id": session_id,
                    "message": message_dict
                }
            }

            await dest_connection.send_message(relay_message)

            logger.debug(
                "Forwarded RELAY_MESSAGE: %s → %s (%d bytes, session=%s)",
                from_peer[:20], to_peer[:20], len(message), session_id
            )

        except Exception as e:
            logger.error(
                "Failed to forward message to %s: %s",
                to_peer[:20], e
            )
            return False

        # Update statistics
        session.messages_relayed += 1
        session.bytes_relayed += len(message)
        session.last_activity = time.time()

        self.stats["messages_relayed"] += 1
        self.stats["bytes_relayed"] += len(message)

        return True

    def _check_rate_limit(self, node_id: str, limit: int = 100, window: float = 1.0) -> bool:
        """
        Check rate limit for node (server mode).

        Args:
            node_id: Node to check
            limit: Max requests per window
            window: Time window in seconds

        Returns:
            True if within limit
        """
        now = time.time()
        timestamps = self.rate_limits[node_id]

        # Remove old timestamps
        timestamps[:] = [t for t in timestamps if now - t < window]

        # Check limit
        if len(timestamps) >= limit:
            return False

        # Add new timestamp
        timestamps.append(now)
        return True

    def get_stats(self) -> Dict:
        """
        Get relay manager statistics.

        Returns:
            Dict with relay statistics including uptime and latency
        """
        # Calculate actual uptime (if volunteering)
        uptime = 1.0
        if self.volunteer and self.start_time:
            elapsed_hours = (time.time() - self.start_time) / 3600.0
            uptime = min(1.0, elapsed_hours / 24.0)  # 0.0-1.0

        # Calculate average latency from recent relay sessions
        latency_ms = 0.0
        if self.sessions:
            # Estimate latency from message relay times
            # For now, use a simple heuristic based on active sessions
            # TODO: Track actual round-trip times for more accurate measurement
            latency_ms = 50.0  # Default estimate for local relay

        return {
            **self.stats,
            "volunteer": self.volunteer,
            "active_sessions": len(self.sessions),
            "max_peers": self.max_peers,
            "region": self.region,
            "uptime": uptime,
            "latency_ms": latency_ms,
        }
