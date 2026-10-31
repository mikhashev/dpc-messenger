# dpc-client/core/dpc_client_core/p2p_manager.py
# P2PManager with WebRTC support

import asyncio
import ssl
import json
import logging
import platform
import socket as _socket_module
import time
from typing import Dict, Any, Callable, Tuple, Union, Optional

from cryptography import x509
from cryptography.hazmat.primitives import serialization
import websockets

from dpc_protocol.crypto import DPC_HOME_DIR, generate_node_id, load_identity, generate_identity
from dpc_protocol.protocol import (
    MAX_HELLO_FRAME_BYTES, read_message, write_message, create_hello_message,
)
from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.message_signing import HELLO_PREIMAGE_VERSION, hello_content_hash
from dpc_protocol.pcm_core import PCMCore, PersonalContext
from dpc_protocol.utils import parse_dpc_uri

from .firewall import ContextFirewall, is_blocked_by
from .hub_client import HubClient
from .webrtc_peer import WebRTCPeerConnection
from .dht import DHTManager, DHTConfig
from .peer_cache import (
    PeerCache, addresses_to_try, dial_in_order,
    checked_local_addresses, local_addresses_to_advertise,
)
from .own_addresses import OwnAddresses, lan_addresses
from pathlib import Path

logger = logging.getLogger(__name__)


# The connection types on which the peer's key has been proved (ADR-041 D2):
# `_verify_hello_identity` inbound, `_validate_peer_certificate` outbound.
PROVED_CONNECTION_TYPES = ("direct_tls",)

# The HELLO_CHALLENGE nonce, exactly: the dialler signs no other length.
HELLO_NONCE_BYTES = 32


class SelfDialError(ConnectionError):
    """A dial reached this node itself: the certificate it met is our own.

    A ConnectionError, so every caller that already fails a dial on one still
    does; its text is the whole reason, short enough to stand in a log line.
    """


def peer_proof(peers: Any, peer_id: str) -> tuple[Optional[bool], Optional[str]]:
    """The two columns a usage row carries about the node at the other end of
    the call: whether its key was proved, and the tier that says so.

    `(None, None)` when there is no connection to read — the peer is gone by
    the time the row is written, or the row has no peer in it at all (an agent
    or a gateway client on this machine is not reached over a tier). A wrapper
    that names no tier is `"unknown"`, and unknown is not proved.
    """
    connection = peers.get(peer_id) if hasattr(peers, "get") else None
    if connection is None:
        return None, None
    connection_type = getattr(connection, "connection_type", None)
    if not isinstance(connection_type, str):
        connection_type = "unknown"
    return connection_type in PROVED_CONNECTION_TYPES, connection_type


# The most one read of a frame asks the stream for. `last_received` is stamped
# after each read, so a frame of many megabytes on a slow link keeps the link
# alive while it arrives instead of only once it is whole.
_FRAME_SLICE_BYTES = 64 * 1024


class PeerConnection:
    """A unified wrapper for a direct TLS P2P connection."""
    def __init__(self, node_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 direction: Optional[str] = None):
        self.node_id = node_id
        self.reader = reader
        self.writer = writer
        self.connection_type = "direct_tls"
        # "in" if the peer dialled this node, "out" if this node dialled it —
        # the direction the peer cache records for the same connection.
        self.direction = direction
        # time.monotonic() of the last bytes read from the peer: the clock
        # `_watch_peer_silence` holds against link_silence_timeout.
        self.last_received = time.monotonic()

    async def send(self, message: Dict[str, Any]):
        """Sends a message over the TLS stream."""
        await write_message(self.writer, message)

    async def read(self) -> Dict[str, Any] | None:
        """Reads a message from the TLS stream, stamping `last_received` per slice of it."""
        # read_message calls only readexactly() on what it is given, so this
        # object stands in for the StreamReader and the framing stays in one place.
        return await read_message(self)

    async def readexactly(self, n: int) -> bytes:
        """StreamReader.readexactly, read in slices that each stamp `last_received`."""
        data = bytearray()
        while True:
            try:
                data += await self.reader.readexactly(min(_FRAME_SLICE_BYTES, n - len(data)))
            except asyncio.IncompleteReadError as e:
                # The whole frame's partial, as one readexactly(n) would report it.
                raise asyncio.IncompleteReadError(bytes(data) + e.partial, n) from None
            self.last_received = time.monotonic()
            if len(data) >= n:
                return bytes(data)

    async def close(self):
        """Closes the TLS connection with timeout protection."""
        if not self.writer.is_closing():
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("SSL shutdown timed out for peer %s", self.node_id)
            except Exception as e:
                # Gracefully handle SSL errors during shutdown (race conditions)
                logger.debug("Error during SSL shutdown for peer %s: %s", self.node_id, e)


def _enable_tcp_keepalive(writer: asyncio.StreamWriter) -> None:
    """
    Enable TCP keepalive on the underlying socket of an asyncio StreamWriter.

    Without keepalive, a half-open (zombie) TCP connection appears alive to both
    sides indefinitely. The OS will send periodic probes and close the connection
    if the peer stops ACKing, triggering the normal disconnect handler.

    Timing (applied where supported):
      - idle:  60s  — start probing after 60s of inactivity
      - intvl: 10s  — probe every 10s
      - count:  5   — close after 5 unanswered probes (~110s total detection)
    """
    sock = writer.get_extra_info('socket')
    if sock is None:
        return
    try:
        sock.setsockopt(_socket_module.SOL_SOCKET, _socket_module.SO_KEEPALIVE, 1)
        # Fine-grained control (Linux, macOS, Windows 10 1703+)
        if hasattr(_socket_module, 'TCP_KEEPIDLE'):
            sock.setsockopt(_socket_module.IPPROTO_TCP, _socket_module.TCP_KEEPIDLE, 60)
        if hasattr(_socket_module, 'TCP_KEEPINTVL'):
            sock.setsockopt(_socket_module.IPPROTO_TCP, _socket_module.TCP_KEEPINTVL, 10)
        if hasattr(_socket_module, 'TCP_KEEPCNT'):
            sock.setsockopt(_socket_module.IPPROTO_TCP, _socket_module.TCP_KEEPCNT, 5)
    except Exception as e:
        logger.debug("Could not set TCP keepalive: %s", e)


def _advertises_link_ping(fields: Any) -> bool:
    """A HELLO payload or HELLO_ACK says its sender answers LINK_PING (DPTP 1.13)."""
    value = fields.get("link_ping") if isinstance(fields, dict) else None
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


class P2PManager:
    """Manages all P2P connections (both direct TLS and WebRTC)."""

    def __init__(self, firewall: ContextFirewall, settings=None):
        self.firewall = firewall
        self.settings = settings  # Optional settings for configurable timeouts/ports
        self.peers: Dict[str, Union[PeerConnection, WebRTCPeerConnection]] = {}
        self.display_name: str | None = None

        # WebRTC connection state tracking
        self._pending_webrtc: Dict[str, WebRTCPeerConnection] = {}
        self._ice_candidates_buffer: Dict[str, list] = {}

        # Auto-reconnect tracking
        self._intentional_disconnects: set = set()  # Track user-initiated disconnects
        self._hub_client_refs: Dict[str, HubClient] = {}  # Store hub_client for reconnection

        # Background task tracking for graceful shutdown
        self._peer_listener_tasks: Dict[str, asyncio.Task] = {}  # Track _listen_to_peer tasks
        self._peer_watchdog_tasks: Dict[str, asyncio.Task] = {}  # Track _watch_peer_silence tasks

        # Addresses that are this machine — see own_addresses.py
        self.own_addresses = OwnAddresses()
        # The latest STUN-seen external address (service.py keeps the same word);
        # own_addresses never forgets an earlier one.
        self._external_ip: Optional[str] = None

        # DHT (Distributed Hash Table) for decentralized peer discovery
        self.dht_manager: DHTManager | None = None  # Initialized in start_server()

        # Rate limiting for failed connection attempts (ARCH-26 quick win)
        self._failed_hello_counts: Dict[str, list] = {}  # ip -> [timestamps]
        self._rate_limit_max_failures = 10
        self._rate_limit_window_seconds = 300  # 5 minutes

        # What the failed-HELLO count cannot see (ADR-041 D8): a peer that
        # completes TLS and never sends HELLO fails nothing, so its read is
        # bounded and its connections are counted while they wait.
        self._pending_hello_counts: Dict[str, int] = {}  # ip -> connections before HELLO_ACK
        self._hello_timeout = settings.get_hello_timeout() if settings else 10.0
        self._max_pending_hellos_per_ip = settings.get_max_pending_hellos_per_ip() if settings else 8
        # Past HELLO_ACK the pending count is released, and a node id costs one
        # key pair: inbound proved connections are bounded per address as well.
        self._max_proved_connections_per_ip = settings.get_max_proved_connections_per_ip() if settings else 8
        # A link to a peer that advertised link_ping ends after this much silence.
        self._link_silence_timeout = settings.get_link_silence_timeout() if settings else 90.0

        # Peer cache for faster reconnection (stores last known IP/port)
        cache_file = Path.home() / ".dpc" / "peer_cache.json"
        self.peer_cache = PeerCache(cache_file)
        logger.info("Peer cache initialized with %d cached peers", len(self.peer_cache.get_all_peers()))

        try:
            self.node_id, self.key_file, self.cert_file = load_identity()
            logger.info("Existing node identity loaded")
        except FileNotFoundError:
            logger.info("Node identity not found - generating a new one")
            generate_identity()
            self.node_id, self.key_file, self.cert_file = load_identity()
            logger.info("New node identity generated and loaded")

        self.local_context = PCMCore().load_context()
        self.on_peer_list_change: Callable | None = None
        self.on_message_received: Callable | None = None
        self.on_peer_disconnected: Callable | None = None  # Callback for peer disconnection
        # GroupManager.get_groups_for_peer, set by the service: who gets local_addresses.
        self.groups_for_peer: Callable | None = None
        self._server_task = None
        logger.info("P2PManager initialized with WebRTC support")

    def refuse_if_blocked(self, node_id: Any, path: str) -> bool:
        """True, with one INFO line naming the node and the path, when the
        node is in `blocked_nodes`. Every door that admits a peer or delivers
        a message as coming from it asks this."""
        if not is_blocked_by(getattr(self, "firewall", None), node_id):
            return False
        logger.info("Refused blocked node %s on %s", node_id, path)
        return True

    def set_on_peer_list_change(self, callback: Callable):
        self.on_peer_list_change = callback

    def set_on_message_received(self, callback: Callable):
        self.on_message_received = callback

    def set_on_peer_disconnected(self, callback: Callable):
        """Set callback for peer disconnection events (for cleanup like file transfers)."""
        self.on_peer_disconnected = callback

    def set_groups_for_peer(self, callback: Callable):
        """Set the groups-shared-with-a-peer lookup that local_addresses is gated on."""
        self.groups_for_peer = callback

    def set_core_service_ref(self, core_service):
        """Set reference to CoreService for storing peer metadata."""
        self._core_service_ref = core_service

    def set_display_name(self, name: str):
        """Set the display name that will be shared with peers during handshake."""
        self.display_name = name
        logger.info("Display name set to: %s", name)
    
    def get_display_name(self) -> str | None:
        """Get the current display name."""
        return self.display_name

    def get_state(self) -> dict:
        """Snapshot of P2PManager state for diagnostics and CoreService.get_state()."""
        peer_types: dict[str, int] = {}
        for conn in self.peers.values():
            t = getattr(conn, "connection_type", "unknown")
            peer_types[t] = peer_types.get(t, 0) + 1
        return {
            "node_id": self.node_id,
            "connected_peers": len(self.peers),
            "peer_types": peer_types,
            "pending_webrtc": len(self._pending_webrtc),
            "server_running": self._server_task is not None and not self._server_task.done(),
            "dht_running": self.dht_manager is not None,
        }

    async def _notify_peer_change(self):
        if self.on_peer_list_change:
            await self.on_peer_list_change()

    def get_external_ips(self) -> list[str]:
        """
        Get external IP addresses discovered via STUN servers from WebRTC connections.

        Returns:
            List of unique external IP addresses
        """
        external_ips = set()

        # Check all WebRTC peer connections
        for peer in self.peers.values():
            if isinstance(peer, WebRTCPeerConnection):
                external_ip = peer.get_external_ip()
                if external_ip:
                    external_ips.add(external_ip)

        # Also check pending WebRTC connections
        for peer in self._pending_webrtc.values():
            external_ip = peer.get_external_ip()
            if external_ip:
                external_ips.add(external_ip)

        return sorted(list(external_ips))

    def get_connected_peers(self) -> list:
        """
        Get list of currently connected peer connection objects.

        Returns:
            List of PeerConnection/WebRTCPeerConnection objects
        """
        return list(self.peers.values())

    # --- Direct TLS Connection Methods (existing code) ---

    async def start_server(self, host: str = "0.0.0.0", port: int = 8888):
        """
        Starts the raw TLS server to listen for direct connections.

        Supports IPv4, IPv6, and dual-stack modes:
        - host="0.0.0.0": IPv4 only
        - host="::": IPv6 only (may also accept IPv4 on some systems)
        - host="dual": Dual-stack (both IPv4 and IPv6)

        Args:
            host: Bind address ("0.0.0.0", "::", or "dual")
            port: Port to listen on
        """
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file)
        self._listen_port = port  # the port our local_addresses name

        async def _listen(bind_host: str):
            # One clock for "connected and not yet proved": the TLS handshake
            # gets the hello_timeout the HELLO read gets, not asyncio's 60 s.
            # A socket stuck here never reaches the handler, so it is bounded
            # and still counted by neither D8 guard.
            return await asyncio.start_server(
                self._handle_direct_connection, bind_host, port,
                ssl=ssl_context, ssl_handshake_timeout=self._hello_timeout,
            )

        if host == "dual":
            # Windows requires separate IPv4 and IPv6 listeners
            # On Windows, binding to [::] only listens on IPv6 (doesn't accept IPv4-mapped addresses)
            # On Linux/macOS, binding to [::] with IPV6_V6ONLY=0 works for both
            is_windows = platform.system() == "Windows"

            if is_windows:
                # Windows: Create separate IPv4 and IPv6 listeners
                logger.info("Windows detected - creating separate IPv4 and IPv6 listeners")

                try:
                    server_v4 = await _listen("0.0.0.0")
                    server_v6 = await _listen("::")

                    self._server_task = asyncio.gather(
                        server_v4.serve_forever(),
                        server_v6.serve_forever()
                    )
                    logger.info("P2PManager Direct TLS server listening on 0.0.0.0:%d and [::]:%d (dual-stack) for node %s",
                              port, port, self.node_id)
                except Exception as e:
                    logger.error("Failed to bind dual-stack on Windows: %s", e)
                    raise
            else:
                # Linux/macOS: Create separate IPv4 and IPv6 listeners
                # Note: asyncio.start_server doesn't set IPV6_V6ONLY=0, so binding to ::
                # only accepts IPv6 on most modern systems. We need separate listeners.
                logger.info("Linux/macOS detected - creating separate IPv4 and IPv6 listeners")

                try:
                    server_v4 = await _listen("0.0.0.0")
                    server_v6 = await _listen("::")

                    self._server_task = asyncio.gather(
                        server_v4.serve_forever(),
                        server_v6.serve_forever()
                    )
                    logger.info("P2PManager Direct TLS server listening on 0.0.0.0:%d and [::]:%d (dual-stack) for node %s",
                              port, port, self.node_id)
                except Exception as e:
                    logger.error("Failed to bind dual-stack on Linux/macOS: %s", e)
                    raise
        else:
            # Single-stack mode (IPv4 or IPv6 only)
            server = await _listen(host)
            self._server_task = asyncio.create_task(server.serve_forever())

            # Format IPv6 addresses with brackets for clarity
            formatted_host = f"[{host}]" if ":" in host else host
            logger.info("P2PManager Direct TLS server listening on %s:%d for node %s",
                      formatted_host, port, self.node_id)

        # Learned before the DHT block, not inside it: the peer-cache check
        # consults this set on every dial, and a node with the DHT switched
        # off knew one address instead of eighteen.
        self.own_addresses.learn_local_interfaces()

        # Initialize DHT (Distributed Hash Table) for peer discovery
        if self.settings.get_dht_enabled():
            try:
                from .dht import DHTConfig

                # Get DHT configuration from settings
                dht_config = DHTConfig(
                    k=self.settings.get_dht_k(),
                    alpha=self.settings.get_dht_alpha(),
                    bootstrap_timeout=self.settings.get_dht_bootstrap_timeout(),
                    lookup_timeout=self.settings.get_dht_lookup_timeout(),
                    bucket_refresh_interval=self.settings.get_dht_bucket_refresh_interval(),
                    announce_interval=self.settings.get_dht_announce_interval()
                )

                # Initialize DHT manager
                dht_port = self.settings.get_dht_port()

                # Get local IP for DHT announcements (not 0.0.0.0)
                dht_announce_ip = await self._get_primary_local_ip()

                self.own_addresses.learn(dht_announce_ip, "dht announce")

                # DHT announces the configured P2P TLS port for connections, not the DHT UDP port
                p2p_port = self.settings.get_p2p_listen_port()

                self.dht_manager = DHTManager(
                    node_id=self.node_id,
                    ip=dht_announce_ip,
                    port=p2p_port,  # P2P TLS port for connections
                    config=dht_config
                )

                # Start DHT UDP server
                await self.dht_manager.start(host="0.0.0.0", port=dht_port)
                logger.info("DHT started on UDP port %d for node %s", dht_port, self.node_id)

                # Bootstrap DHT from seed nodes if available
                seed_nodes = self.settings.get_dht_seed_nodes()
                mine = self.own_addresses.self_seeds(seed_nodes)
                if mine:
                    # A warning, not a filter: two machines behind one NAT
                    # share an address, so this can be wrong about someone
                    # else's node. The node id in the reply decides.
                    logger.warning(
                        "DHT seed(s) %s look like this node's own address(es) — "
                        "a seed has to be a node somewhere else",
                        ", ".join(f"{ip}:{port}" for ip, port in mine),
                    )
                if seed_nodes:
                    logger.info("Bootstrapping DHT from %d seed nodes", len(seed_nodes))
                    success = await self.dht_manager.bootstrap(seed_nodes)
                    if success:
                        logger.info("DHT bootstrap successful")
                        # Announce our presence to the DHT
                        await self.announce_to_dht()
                    else:
                        # The reason lives in bootstrap(), which has just said
                        # whether the seeds were silent or were us. Repeating a
                        # guess here put the old lie back on the screen one line
                        # under the correct answer.
                        logger.info("DHT did not bootstrap — see the reason above")
            except Exception as e:
                logger.error("Failed to initialize DHT: %s", e, exc_info=True)
                self.dht_manager = None
        else:
            logger.info("DHT disabled in configuration")

    # --- DHT Peer Discovery Methods ---

    async def find_peer_via_dht(self, target_node_id: str) -> tuple[str, int] | None:
        """
        Look up a peer's IP and port via DHT.

        Args:
            target_node_id: Node ID to look up

        Returns:
            Tuple of (ip, port) if found, None otherwise
        """
        if not self.dht_manager:
            logger.debug("DHT not available for peer lookup")
            return None

        try:
            logger.info("Looking up %s via DHT", target_node_id)
            result = await self.dht_manager.find_peer(target_node_id)

            if result:
                ip, port = result  # Unpack (ip, port) tuple
                logger.info("DHT found %s at %s:%d", target_node_id, ip, port)
                return (ip, port)
            else:
                logger.info("DHT lookup for %s returned no results", target_node_id)
                return None

        except Exception as e:
            logger.error("DHT lookup failed for %s: %s", target_node_id, e)
            return None

    async def announce_to_dht(self):
        """Announce our presence to the DHT for peer discovery."""
        if not self.dht_manager:
            return

        try:
            count = await self.dht_manager.announce()
            if count > 0:
                logger.info("Announced to DHT (%d nodes)", count)
            else:
                logger.debug("DHT announce sent (no nearby nodes)")
        except Exception as e:
            logger.error("DHT announce failed: %s", e)

    async def connect_via_node_id(self, target_node_id: str, timeout: float = None) -> bool:
        """
        Connect to a peer using just their node ID.

        Tries discovery methods in order:
        1. Peer cache (last known IP/port) - fastest, no network overhead
        2. DHT lookup (finds peer's current IP/port)
        3. Hub WebRTC (Phase 5 - not yet implemented)

        Args:
            target_node_id: Node ID to connect to
            timeout: Connection timeout in seconds

        Returns:
            True if connection established, False otherwise
        """
        # Check if already connected
        if target_node_id in self.peers:
            logger.info("Already connected to %s", target_node_id)
            return True

        # Try 0: Peer cache (fastest - no DHT lookup needed)
        cached_peer = self.peer_cache.get_peer(target_node_id)
        candidates = (
            addresses_to_try(cached_peer, getattr(self, "own_addresses", None))
            if cached_peer and cached_peer.is_recently_seen(hours=168)  # 7 days
            else []
        )
        if candidates:
            logger.info(
                "Attempting cached connection to %s over %d address(es) (last seen %s)",
                target_node_id[:20], len(candidates), cached_peer.last_seen
            )

            async def _dial(ip, port, budget):
                return await self.connect_directly(ip, port, target_node_id, timeout=budget)

            # Quick, before the DHT is asked: the advertised local_addresses at
            # their own short budget, then 5 s an answered address, at most three.
            advertised = sum(e.get("dial_timeout_s") or 0 for e in candidates)
            answered = sum(1 for e in candidates if not e.get("dial_timeout_s"))
            connection, entry, error = await dial_in_order(
                _dial, candidates,
                total_timeout=advertised + 5.0 * min(answered, 3), per_address_timeout=5.0,
            )
            if connection is not None:
                logger.info("Connected to %s via peer cache (no DHT lookup needed)", target_node_id[:20])
                return True
            logger.debug("Cached connection failed, falling back to DHT: %s", error)

        # Try 1: DHT lookup
        dht_result = await self.find_peer_via_dht(target_node_id)
        if dht_result:
            ip, port = dht_result
            try:
                await self.connect_directly(ip, port, target_node_id, timeout)
                logger.info("Connected to %s via DHT", target_node_id)

                # Update peer cache with successful DHT connection
                self.peer_cache.add_or_update_peer(
                    node_id=target_node_id,
                    direct_ip=ip,
                    direct_port=port,
                    supports_direct=True,
                    direction="out",
                )

                # Announce ourselves after successful connection
                await self.announce_to_dht()
                return True
            except Exception as e:
                logger.warning("DHT connection failed: %s", e)

        # Try 2: Hub WebRTC (if available)
        # This will be implemented in Phase 5

        logger.error("Failed to connect to %s - All connection methods failed", target_node_id)
        return False

    def _is_rate_limited(self, ip: str) -> bool:
        """Check if an IP has exceeded the failed HELLO rate limit."""
        now = time.monotonic()
        if ip not in self._failed_hello_counts:
            return False
        self._failed_hello_counts[ip] = [
            t for t in self._failed_hello_counts[ip]
            if now - t < self._rate_limit_window_seconds
        ]
        return len(self._failed_hello_counts[ip]) >= self._rate_limit_max_failures

    def _record_failed_hello(self, ip: str):
        """Record a failed HELLO attempt from an IP."""
        if ip not in self._failed_hello_counts:
            self._failed_hello_counts[ip] = []
        self._failed_hello_counts[ip].append(time.monotonic())

    def _release_pending_hello(self, ip: str):
        """One connection from `ip` is past HELLO_ACK, or gone."""
        left = self._pending_hello_counts.get(ip, 0) - 1
        if left > 0:
            self._pending_hello_counts[ip] = left
        else:
            self._pending_hello_counts.pop(ip, None)

    def _proved_connections_from(self, ip: str) -> int:
        """Inbound direct connections in `self.peers` that arrived from `ip`.

        Derived from the entries rather than kept as a count, so a closed
        connection frees its place with nothing to release.
        """
        count = 0
        for connection in list(self.peers.values()):
            if getattr(connection, "direction", None) != "in":
                continue
            writer = getattr(connection, "writer", None)
            peername = writer.get_extra_info("peername") if writer is not None else None
            if peername and peername[0] == ip:
                count += 1
        return count

    async def _handle_direct_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handles an incoming raw TLS connection (Server-side)."""
        peer_node_id = None
        peer = None  # set once this connection holds an entry in self.peers
        peer_addr = writer.get_extra_info('peername')
        peer_addr_str = f"{peer_addr[0]}:{peer_addr[1]}" if peer_addr else "unknown"
        peer_ip = peer_addr[0] if peer_addr else "unknown"
        if self._is_rate_limited(peer_ip):
            logger.debug("Rate-limited connection from %s, closing silently", peer_ip)
            writer.close()
            return
        # Silent like the rate limit: whoever holds eight connections open
        # before HELLO_ACK learns nothing from a ninth.
        if self._pending_hello_counts.get(peer_ip, 0) >= self._max_pending_hellos_per_ip:
            logger.debug("%s already holds %d connections before HELLO_ACK, closing silently",
                         peer_ip, self._max_pending_hellos_per_ip)
            writer.close()
            return

        self._pending_hello_counts[peer_ip] = self._pending_hello_counts.get(peer_ip, 0) + 1
        try:
            logger.info("Received a direct TLS connection attempt from %s", peer_addr_str)
            await asyncio.sleep(0.01)

            # The dialler proves its key by signing the HELLO preimage over this
            # nonce and both node ids (`hello_preimage`), never the nonce itself.
            import os as _os, base64 as _b64
            nonce = _os.urandom(HELLO_NONCE_BYTES)
            challenge = {
                "command": "HELLO_CHALLENGE",
                "payload": {
                    "nonce": _b64.b64encode(nonce).decode(),
                    "hello_preimage_version": HELLO_PREIMAGE_VERSION,
                }
            }
            await write_message(writer, challenge)

            # Bounded, and the expiry lands in the except below as one more
            # failed HELLO; the dial side's _handshake_read has the same shape.
            # The frame cap is the stranger's one until the identity check
            # below passes: a header over it is refused before its body is read,
            # and the None it returns is one more failed HELLO too.
            try:
                hello_msg = await asyncio.wait_for(
                    read_message(reader, max_frame_bytes=MAX_HELLO_FRAME_BYTES),
                    timeout=self._hello_timeout,
                )
            except asyncio.TimeoutError:
                raise ConnectionError(
                    f"No HELLO within {self._hello_timeout}s of the challenge"
                ) from None
            if not hello_msg or hello_msg.get("command") != "HELLO":
                raise ConnectionError("Invalid HELLO message received.")

            payload = hello_msg.get("payload", {})
            peer_node_id = payload.get("node_id")
            peer_name = payload.get("name")
            cert_pem = payload.get("cert_pem")
            hello_signature_b64 = payload.get("hello_signature")

            if not peer_node_id:
                raise ValueError("Peer did not provide a node_id.")

            # Verify the peer owns the private key for their claimed node_id.
            # No fallback to a raw-nonce signature: that is the relay this replaced.
            if (
                not cert_pem
                or not hello_signature_b64
                or payload.get("hello_preimage_version") != HELLO_PREIMAGE_VERSION
            ):
                raise ConnectionError(
                    f"Peer {peer_node_id} sent HELLO without cert_pem, hello_signature or "
                    f"hello_preimage_version {HELLO_PREIMAGE_VERSION!r} "
                    f"— rejecting (peer may be running an outdated version)"
                )
            if not self._verify_hello_identity(peer_node_id, cert_pem, nonce, hello_signature_b64):
                raise ConnectionError(
                    f"HELLO identity verification failed for claimed node_id={peer_node_id} "
                    f"— connection rejected"
                )

            # A blocked node proved its key too, so not a failed HELLO either,
            # and it is told nothing, as at the ceiling below. Before the
            # ceiling's own exception: a blocked node holding an entry is refused.
            if self.refuse_if_blocked(peer_node_id, "direct TLS accept, before HELLO_ACK"):
                writer.close()
                return

            # The peer proved its key, so this is not a failed HELLO; silent to
            # it like the pre-HELLO ceiling. A node that holds an entry already
            # replaces its own connection and is never refused.
            ceiling = getattr(self, "_max_proved_connections_per_ip", 8)
            if peer_node_id not in self.peers and self._proved_connections_from(peer_ip) >= ceiling:
                logger.info("%s already holds %d proved connections, closing %s before HELLO_ACK",
                            peer_ip, ceiling, peer_node_id)
                writer.close()
                return

            # Keep what the three checks above just proved — otherwise every
            # signature this peer ever makes verifies as None ("cannot verify").
            self._persist_peer_certificate(peer_node_id, cert_pem)

            logger.info("Connection from node: %s (identity verified)", peer_node_id)
            if peer_name:
                logger.info("Peer name: %s", peer_name)
                if hasattr(self, '_core_service_ref') and self._core_service_ref:
                    self._core_service_ref.set_peer_metadata(peer_node_id, name=peer_name)

            ack = {
                "command": "HELLO_ACK",
                "status": "OK",
                "name": self.display_name,
                "node_id": self.node_id,  # Include actual node_id for validation
                "link_ping": 1,  # this node answers LINK_PING (DPTP 1.13)
            }
            local_addresses = self._own_local_addresses_for(peer_node_id)
            if local_addresses is not None:
                ack["local_addresses"] = local_addresses
            await write_message(writer, ack)

            peer = PeerConnection(node_id=peer_node_id, reader=reader, writer=writer, direction="in")

            # Enable TCP keepalive on accepted connections to detect zombie connections.
            _enable_tcp_keepalive(writer)

            # Set strategy_used for incoming connections (for UI display)
            sock = writer.get_extra_info('socket')
            if sock:
                if sock.family == _socket_module.AF_INET6:
                    peer.strategy_used = "ipv6_direct"
                else:
                    peer.strategy_used = "ipv4_direct"
            else:
                peer.strategy_used = "ipv4_direct"  # Default fallback

            # A reconnect of a node we still hold a session with: the new,
            # identity-verified connection replaces the old one, which is closed
            # here rather than left open with no entry pointing at it.
            replaced = self.peers.get(peer_node_id)
            self.peers[peer_node_id] = peer
            if replaced is not None and replaced is not peer:
                logger.info("Connection with %s replaced by a new one", peer_node_id)
                await replaced.close()
            await self._notify_peer_change()
            logger.info("Direct TLS connection established with %s", peer_node_id)

            # Update peer cache with incoming connection info
            peer_addr = writer.get_extra_info('peername')
            if peer_addr:
                peer_ip = peer_addr[0]
                # No port: the source port of an inbound connection is not the
                # peer's listener. The cache keeps the port this address's entry
                # already has, or starts a new entry at DEFAULT_DIRECT_PORT.
                self.peer_cache.add_or_update_peer(
                    node_id=peer_node_id,
                    display_name=peer_name,
                    direct_ip=peer_ip,
                    direct_port=None,
                    supports_direct=True,
                    direction="in",
                )
                logger.debug("Cached inbound address of peer %s", peer_node_id[:20])
            self.peer_cache.set_local_addresses(
                peer_node_id,
                checked_local_addresses(payload.get("local_addresses"), getattr(self, "own_addresses", None)),
            )

            # Announce to DHT after successful connection
            asyncio.create_task(self.announce_to_dht())

            # Start listener task and track it for graceful shutdown
            task = asyncio.create_task(self._listen_to_peer(
                peer, link_ping=_advertises_link_ping(payload), direction="in",
            ))
            self._peer_listener_tasks[peer_node_id] = task

            # Ask the peer for its AI providers, as the outgoing paths do: the
            # listener is running, so the PROVIDERS_RESPONSE can be read.
            from dpc_protocol.protocol import create_get_providers_message
            try:
                await self.send_message_to_peer(peer_node_id, create_get_providers_message())
                logger.debug("Requested AI providers from %s", peer_node_id)
            except Exception as e:
                logger.warning("Failed to request providers from %s: %s", peer_node_id, e)

        except Exception as e:
            self._record_failed_hello(peer_ip)
            if peer_node_id:
                logger.error("Error handling direct connection from %s: %s", peer_addr_str, e, exc_info=True)
            else:
                logger.warning("Rejected non-DPTP connection from %s: %s", peer_addr_str, e)
            # `peer_node_id` is the HELLO's unsigned claim until the identity
            # check passes: only an entry this very connection registered is
            # ours to close, never the live session of whoever the frame named.
            if peer is not None and self.peers.get(peer_node_id) is peer:
                await self.shutdown_peer_connection(peer_node_id)
            else:
                try:
                    writer.close()
                    await writer.wait_closed()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    pass
        finally:
            self._release_pending_hello(peer_ip)

    async def test_port_connectivity(self, host: str, port: int, timeout: float = 10.0) -> tuple[bool, str]:
        """
        Test if a port is accessible before attempting full connection.

        Args:
            host: Target IP address
            port: Target port
            timeout: Test timeout in seconds (default 10.0)

        Returns:
            Tuple of (success: bool, message: str)
            - (True, "Port is accessible") if connection succeeds
            - (False, "error details") if connection fails
        """
        try:
            # Attempt basic TCP connection without SSL/TLS
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return (True, f"Port {port} is accessible on {host}")

        except asyncio.TimeoutError:
            msg = (
                f"Port {port} on {host} is not accessible (timeout).\n"
                f"  - If this is an external IP, ensure port forwarding is configured on the peer's router.\n"
                f"  - If this is a local IP, check the peer's firewall settings."
            )
            return (False, msg)

        except ConnectionRefusedError:
            msg = (
                f"Port {port} on {host} actively refused connection.\n"
                f"  - Peer's D-PC client may not be running.\n"
                f"  - Port may be blocked by firewall."
            )
            return (False, msg)

        except Exception as e:
            return (False, f"Port test failed: {e}")

    async def connect_directly(self, host: str, port: int, target_node_id: str, timeout: float = None):
        """
        Initiates a direct TLS connection to a peer (Client-side).

        Args:
            host: Peer's IP address (local or external)
            port: Peer's port (default 8888)
            target_node_id: Expected node ID for identity verification
            timeout: Connection timeout in seconds (default 10.0)

        Raises:
            asyncio.TimeoutError: If connection times out (likely port forwarding issue)
            ConnectionRefusedError: If peer actively refuses connection (firewall/not running)
            ConnectionError: If peer handshake fails
        """
        if self.refuse_if_blocked(target_node_id, "direct TLS dial"):
            raise ConnectionError(f"{target_node_id} is blocked. Unblock it first to connect.")

        # Use configured timeout if not explicitly provided
        if timeout is None:
            timeout = self.settings.get_p2p_connection_timeout() if self.settings else 60.0

        logger.info("Initiating direct connection to %s at %s:%d", target_node_id, host, port)

        # One budget for the whole dial, taken before the first packet. The
        # pre-flight below is itself a TCP connect, so leaving it outside meant a
        # caller asking for five seconds could wait ten.
        deadline = asyncio.get_running_loop().time() + timeout

        def _budget_left() -> float:
            return deadline - asyncio.get_running_loop().time()

        # Pre-flight check: Test basic port connectivity before SSL handshake
        # This provides clearer error messages than cryptic SSL errors (e.g., WinError 121)
        logger.debug("Running pre-flight port connectivity check for %s:%d", host, port)
        # Respect caller's timeout (e.g., 5s for quick cache probes) up to 60s max for slow networks
        preflight_timeout = max(0.0, min(_budget_left(), 60.0))
        port_accessible, port_message = await self.test_port_connectivity(host, port, preflight_timeout)

        if not port_accessible:
            error_msg = (
                f"Pre-flight check failed for {host}:{port} - Port is not accessible.\n"
                f"{port_message}\n"
                f"  Direct TLS connection requires:\n"
                f"    - Peer's D-PC client running and listening on port {port}\n"
                f"    - No firewall blocking port {port}\n"
                f"    - For external IPs: Router port forwarding configured\n"
                f"  Alternative: Use WebRTC connections (no port forwarding needed)"
            )
            logger.error(error_msg)
            raise ConnectionError(error_msg)

        logger.debug("Pre-flight check passed - port %d is accessible", port)

        # Create SSL context for self-signed certificates
        # Note: We use CERT_NONE because we're in a P2P network with self-signed certs
        # (no shared CA). Security is provided by manual validation below.
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.check_hostname = False  # We validate manually via CN
        ssl_context.verify_mode = ssl.CERT_NONE  # Required for self-signed certs

        try:
            # The connect gets what the pre-flight left, and the handshake reads
            # below get what the connect left: a peer that completes TLS and then
            # says nothing used to hold this task for ever.
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_context),
                timeout=max(0.0, _budget_left()),
            )

            async def _handshake_read(expected: str) -> Optional[dict]:
                """A read that cannot outlive the budget the caller paid for,
                nor take a frame larger than a handshake needs."""
                left = _budget_left()
                try:
                    if left <= 0:
                        raise asyncio.TimeoutError
                    return await asyncio.wait_for(
                        read_message(reader, max_frame_bytes=MAX_HELLO_FRAME_BYTES),
                        timeout=left,
                    )
                except asyncio.TimeoutError:
                    writer.close()
                    await writer.wait_closed()
                    raise ConnectionError(
                        f"Handshake with {target_node_id} at {host}:{port} timed out "
                        f"waiting for {expected}: the TLS connect succeeded inside the "
                        f"{timeout}s budget and the peer then said nothing."
                    ) from None

            # Enable TCP keepalive to detect zombie connections (half-open TCP)
            # Without this, a dead connection appears alive indefinitely and causes
            # inference/context requests to hang until the 240s timeout fires.
            _enable_tcp_keepalive(writer)

            # Extract and validate peer certificate (TLS layer security)
            ssl_object = writer.get_extra_info('ssl_object')
            if not ssl_object:
                error_msg = "TLS connection established but no SSL object available"
                logger.error(error_msg)
                writer.close()
                await writer.wait_closed()
                raise ConnectionError(error_msg)

            peer_cert = self._extract_peer_certificate(ssl_object)
            if not peer_cert:
                error_msg = "Failed to extract peer certificate - rejecting connection"
                logger.error(error_msg)
                writer.close()
                await writer.wait_closed()
                raise ConnectionError(error_msg)

            if self._certificate_is_this_nodes_own(peer_cert) and target_node_id != self.node_id:
                # Not an attack and not a wrong identity: the address now leads
                # back here (a shared exit, a port forward onto this machine, a
                # peer that moved away). Nothing to dial at it ever again.
                writer.close()
                await writer.wait_closed()
                dropped = self.peer_cache.remove_address(target_node_id, host, port)
                logger.info(
                    "Address %s:%d of %s leads back to this node%s",
                    host, port, target_node_id[:20],
                    " and is dropped from that peer's cache entry" if dropped
                    else " (it was not in that peer's cache entry)",
                )
                raise SelfDialError(f"{host}:{port} leads back to this node")

            if not self._validate_peer_certificate(peer_cert, target_node_id):
                error_msg = (
                    f"Certificate validation failed for {target_node_id}. "
                    f"This means either:\n"
                    f"  1. Peer regenerated identity - Get fresh URI from peer\n"
                    f"  2. Connecting to wrong peer\n"
                    f"  3. MITM attack detected"
                )
                logger.error(error_msg)
                writer.close()
                await writer.wait_closed()
                raise ConnectionError(error_msg)

            logger.info("TLS certificate validated successfully for %s", target_node_id)

            # Dialling out, the peer's cert arrives over TLS and never in HELLO
            # (HELLO_ACK carries only name and node_id), so this is the only
            # place an outbound connection can learn it.
            self._persist_peer_certificate(
                target_node_id,
                peer_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
            )

            # Read the server's challenge nonce (new in authenticated HELLO protocol).
            import base64 as _b64
            challenge_msg = await _handshake_read("HELLO_CHALLENGE")
            if not challenge_msg or challenge_msg.get("command") != "HELLO_CHALLENGE":
                error_msg = (
                    f"Expected HELLO_CHALLENGE from {target_node_id} but got "
                    f"{challenge_msg.get('command') if challenge_msg else 'nothing'} "
                    f"— peer may be running an outdated version"
                )
                logger.error(error_msg)
                writer.close()
                await writer.wait_closed()
                raise ConnectionError(error_msg)

            # The nonce is the listener's choice, so it is checked before it goes
            # anywhere near the key — and even then only into hello_preimage.
            challenge_payload = challenge_msg.get("payload")
            if not isinstance(challenge_payload, dict):
                challenge_payload = {}
            refusal = None
            nonce = None
            if challenge_payload.get("hello_preimage_version") != HELLO_PREIMAGE_VERSION:
                refusal = (
                    f"HELLO_CHALLENGE from {target_node_id} without hello_preimage_version "
                    f"{HELLO_PREIMAGE_VERSION!r} — rejecting (peer may be running an outdated version)"
                )
            else:
                try:
                    nonce = _b64.b64decode(challenge_payload.get("nonce") or "", validate=True)
                except (ValueError, TypeError):
                    nonce = None
                if nonce is None or len(nonce) != HELLO_NONCE_BYTES:
                    refusal = (
                        f"HELLO_CHALLENGE from {target_node_id} carries a nonce that is not "
                        f"{HELLO_NONCE_BYTES} bytes of base64 — rejecting"
                    )
            if refusal:
                logger.error(refusal)
                writer.close()
                await writer.wait_closed()
                raise ConnectionError(refusal)

            # target_node_id is the listener id the TLS certificate check above proved.
            hello_signature = self._sign_hello(nonce=nonce, listener_node_id=target_node_id)

            # Read own certificate PEM to include in HELLO
            with open(self.cert_file, 'r') as _cf:
                own_cert_pem = _cf.read()

            hello = {
                "command": "HELLO",
                "payload": {
                    "node_id": self.node_id,
                    "name": self.display_name,
                    "cert_pem": own_cert_pem,
                    "hello_preimage_version": HELLO_PREIMAGE_VERSION,
                    "hello_signature": hello_signature,
                    "link_ping": 1,  # this node answers LINK_PING (DPTP 1.13)
                }
            }
            local_addresses = self._own_local_addresses_for(target_node_id)
            if local_addresses is not None:
                hello["payload"]["local_addresses"] = local_addresses
            # Loud here rather than a silent hang-up at a listener that reads
            # HELLO under the same cap.
            await write_message(writer, hello, max_frame_bytes=MAX_HELLO_FRAME_BYTES)

            response = await _handshake_read("HELLO_ACK")
            if not response or response.get("status") != "OK":
                raise ConnectionError(f"Peer did not acknowledge HELLO.")

            # Extract actual node_id from HELLO_ACK (critical for security validation)
            actual_node_id = response.get("node_id")
            if not actual_node_id:
                error_msg = "HELLO_ACK missing node_id field - rejecting connection for security"
                logger.error(error_msg)
                writer.close()
                await writer.wait_closed()
                raise ConnectionError(error_msg)

            if actual_node_id != target_node_id:
                error_msg = (
                    f"Node ID mismatch! Expected {target_node_id} but peer reports {actual_node_id}. "
                    f"This means either:\n"
                    f"  1. Peer regenerated identity (fresh install) - Get fresh connection URI from peer\n"
                    f"  2. Connecting to wrong peer - Verify you have correct URI\n"
                    f"  3. Potential MITM attack - Connection rejected for security"
                )
                logger.error(error_msg)
                writer.close()
                await writer.wait_closed()
                raise ConnectionError(error_msg)

            peer_name = response.get("name")
            if peer_name:
                logger.info("Peer name: %s", peer_name)
                if hasattr(self, '_core_service_ref') and self._core_service_ref:
                    self._core_service_ref.set_peer_metadata(target_node_id, name=peer_name)

            # actual_node_id is guaranteed to equal target_node_id after validation above
            peer = PeerConnection(node_id=target_node_id, reader=reader, writer=writer, direction="out")

            # Set strategy_used for outgoing direct connections (for UI display)
            sock = writer.get_extra_info('socket')
            if sock:
                import socket as socket_module
                if sock.family == socket_module.AF_INET6:
                    peer.strategy_used = "ipv6_direct"
                else:
                    peer.strategy_used = "ipv4_direct"
            else:
                peer.strategy_used = "ipv4_direct"  # Default fallback

            # A dial to a node we still hold a session with: the new, identity-verified
            # connection replaces the old one, which is closed below rather than left
            # open with no entry pointing at it.
            replaced = self.peers.get(target_node_id)
            self.peers[target_node_id] = peer
            logger.info("Direct connection established with %s", target_node_id)

            # Start listener task FIRST so it survives any timeout during post-connection
            # callbacks (e.g. _notify_peer_change runs get_status/ip-addr which can be slow).
            # If the strategy timeout fires during _notify_peer_change, the peer is already
            # in self.peers and the listener is running, so request-response patterns work.
            task = asyncio.create_task(self._listen_to_peer(
                peer, link_ping=_advertises_link_ping(response), direction="out",
            ))
            self._peer_listener_tasks[target_node_id] = task

            # Closed only now, with the new session registered and listening: the old
            # listener ends on its own and, finding another entry, takes nothing down.
            if replaced is not None and replaced is not peer:
                logger.info("Connection with %s replaced by a new one", target_node_id)
                await replaced.close()

            # Persist successful endpoint to peer cache. An advertised
            # local_addresses candidate becomes a known address here and only here.
            self.peer_cache.add_or_update_peer(
                node_id=target_node_id,
                direct_ip=host,
                direct_port=port,
                supports_direct=True,
                direction="out",
            )
            self.peer_cache.set_local_addresses(
                target_node_id,
                checked_local_addresses(response.get("local_addresses"), getattr(self, "own_addresses", None)),
            )

            # Announce to DHT after successful connection
            asyncio.create_task(self.announce_to_dht())

            # Fire peer-change notification as a background task so it doesn't
            # block the strategy timeout window (it runs get_status + ip addr).
            asyncio.create_task(self._notify_peer_change())

            # Auto-discover peer's available AI providers
            from dpc_protocol.protocol import create_get_providers_message
            try:
                await self.send_message_to_peer(target_node_id, create_get_providers_message())
                logger.debug("Requested AI providers from %s", target_node_id)
            except Exception as e:
                logger.warning("Failed to request providers from %s: %s", target_node_id, e)

            return peer

        except asyncio.TimeoutError:
            error_msg = (
                f"Connection to {host}:{port} timed out after {timeout} seconds.\n"
                f"  Possible causes:\n"
                f"  - Peer is behind NAT/firewall without port forwarding configured\n"
                f"  - For external IP connections: Router must forward port {port} to peer device\n"
                f"  - Peer's D-PC client may not be running\n"
                f"  - Network connectivity issues\n"
                f"  Recommended: Use WebRTC connections for internet-wide P2P (no port forwarding needed)"
            )
            logger.error(error_msg)
            raise ConnectionError(error_msg)
        except ConnectionRefusedError as e:
            error_msg = (
                f"Connection to {host}:{port} was refused.\n"
                f"  Possible causes:\n"
                f"  - Peer's D-PC client is not running\n"
                f"  - Firewall is blocking port {port}\n"
                f"  - Port {port} is used by another application\n"
                f"  Verify peer is running: Check for 'Direct TLS server started on port {port}' in their logs"
            )
            logger.error(error_msg)
            raise ConnectionError(error_msg)
        except SelfDialError:
            raise  # already logged at INFO, with the cache entry dealt with
        except Exception as e:
            # Use target_node_id in error since actual_node_id may not be set yet
            logger.error("Failed to establish direct connection with %s: %s", target_node_id, e, exc_info=True)
            raise

    def _extract_peer_certificate(self, ssl_object) -> Optional[Any]:
        """
        Extract peer's X.509 certificate from TLS connection.

        Args:
            ssl_object: SSL object from writer.get_extra_info('ssl_object')

        Returns:
            Certificate object if available, None otherwise
        """
        try:
            # Get binary DER-encoded certificate
            cert_bin = ssl_object.getpeercert(binary_form=True)
            if not cert_bin:
                logger.error("No peer certificate available")
                return None

            # Parse with cryptography library
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend

            cert = x509.load_der_x509_certificate(cert_bin, default_backend())
            return cert

        except Exception as e:
            logger.error("Failed to extract peer certificate: %s", e)
            return None

    def _certificate_is_this_nodes_own(self, cert: Any) -> bool:
        """True when the certificate carries this node's id as its CN and its key
        hashes to that id: the far end answered with our own identity. A CN alone
        is a claim anyone can write, so a stranger copying it falls through to
        the ordinary check and its wording."""
        own_id = getattr(self, "node_id", None)
        if not own_id:
            return False
        try:
            cn = next((a.value for a in cert.subject if a.oid == x509.oid.NameOID.COMMON_NAME), None)
            return cn == own_id and generate_node_id(cert.public_key()) == own_id
        except Exception:  # noqa: BLE001 — an unreadable certificate is the ordinary check's to refuse
            return False

    def _validate_peer_certificate(
        self,
        cert: Any,
        expected_node_id: str
    ) -> bool:
        """
        Validate peer certificate matches expected node_id: CN, then the
        public key's fingerprint. The CN catches a wrong peer, the key
        catches a peer that says the right name.

        Args:
            cert: Peer's X.509 certificate
            expected_node_id: Expected node ID from URI

        Returns:
            True if valid, False otherwise
        """
        try:
            from cryptography import x509

            # Extract Common Name from certificate subject
            cn = None
            for attribute in cert.subject:
                if attribute.oid == x509.oid.NameOID.COMMON_NAME:
                    cn = attribute.value
                    break

            if not cn:
                logger.error("Certificate has no Common Name")
                return False

            # Verify CN matches expected node_id
            if cn != expected_node_id:
                if cn.strip() == expected_node_id.strip():
                    # Naming the cause, because the generic message below ends
                    # with "MITM attack detected" and a stray space is not one.
                    logger.error(
                        "Certificate validation failed on whitespace alone: "
                        "CN=%r vs expected %r — the identity matches, the URI "
                        "picked up whitespace in transit",
                        cn, expected_node_id
                    )
                else:
                    logger.error(
                        "Certificate validation failed: CN=%r but expected node_id=%r",
                        cn, expected_node_id
                    )
                return False

            # A CN is a claim; the key fingerprint is the proof. node_id is the
            # hash of the public key, and TLS has already made the far end prove
            # it holds that key's private half — so this comparison turns
            # possession into identity. Without it a stranger who terminates TLS
            # with a self-signed certificate carrying the right CN is that peer
            # for the rest of the session, and every instrument keyed on
            # sender_node_id downstream inherits the lie.
            derived_id = generate_node_id(cert.public_key())
            if derived_id != expected_node_id:
                logger.error(
                    "Certificate validation failed: CN says %r but its public key "
                    "hashes to %r — the name matches and the key does not",
                    expected_node_id, derived_id
                )
                return False

            logger.info("Certificate validated: node_id=%s", cn)
            return True

        except Exception as e:
            logger.error("Certificate validation error: %s", e)
            return False

    def _sign_hello(self, *, nonce: bytes, listener_node_id: str) -> str:
        """This node's base64 HELLO signature: CommitSigner over hello_content_hash.

        The one signing path every other signature here takes, separated from
        them by the preimage's own tag. Never the handed-in nonce itself.
        """
        with open(self.key_file, 'rb') as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
        hello_hash = hello_content_hash(
            nonce=nonce, listener_node_id=listener_node_id, dialer_node_id=self.node_id
        )
        return CommitSigner(self.node_id, private_key).sign_commit(hello_hash)

    def _verify_hello_identity(
        self,
        peer_node_id: str,
        cert_pem: str,
        nonce: bytes,
        hello_signature_b64: str
    ) -> bool:
        """
        Verify that the connecting peer owns the private key for their claimed node_id.

        Three checks must all pass:
        1. cert CN == claimed node_id (cert is for this identity)
        2. generate_node_id(cert.public_key()) == node_id (public key binds to node_id)
        3. hello_signature verifies under cert.public_key() over
           hello_content_hash(nonce, this node's id, the claimed node_id)

        Check 2 prevents an attacker from forging a cert with CN == victim node_id.
        Check 3 prevents impersonation with the victim's public cert alone, and,
        the listener id being this node's own, a relay of a signature the victim
        made for another listener.

        Returns:
            True if all checks pass, False otherwise
        """
        from cryptography import x509 as _x509

        try:
            # Parse the certificate
            cert = _x509.load_pem_x509_certificate(cert_pem.encode('utf-8'))

            # Check 1: cert CN == claimed node_id
            cn = None
            for attr in cert.subject:
                if attr.oid == _x509.oid.NameOID.COMMON_NAME:
                    cn = attr.value
                    break
            if cn != peer_node_id:
                logger.warning(
                    "HELLO identity check 1 failed: cert CN='%s' != claimed node_id='%s'",
                    cn, peer_node_id
                )
                return False

            # Check 2: public key fingerprint == claimed node_id (self-certifying identity)
            derived_id = generate_node_id(cert.public_key())
            if derived_id != peer_node_id:
                logger.warning(
                    "HELLO identity check 2 failed: cert public key hashes to '%s' "
                    "but claimed node_id is '%s' — cert is forged",
                    derived_id, peer_node_id
                )
                return False

            # Check 3: the HELLO preimage, rebuilt with this node as the listener
            expected = hello_content_hash(
                nonce=nonce, listener_node_id=self.node_id, dialer_node_id=peer_node_id
            )
            if not CommitSigner.verify_with_public_key(
                cert.public_key(), expected, hello_signature_b64
            ):
                logger.warning(
                    "HELLO identity check 3 failed for %s: hello_signature does not verify "
                    "over the HELLO preimage naming this listener", peer_node_id
                )
                return False
            logger.debug("HELLO identity verified for %s (all 3 checks passed)", peer_node_id)
            return True

        except Exception as e:
            logger.warning("HELLO identity verification failed for %s: %s", peer_node_id, e)
            return False

    def _persist_peer_certificate(
        self,
        peer_node_id: str,
        cert_pem: str,
        peers_dir: Optional[Path] = None
    ) -> bool:
        """Remember a peer's certificate so its signatures stay verifiable.

        Without this, ~/.dpc/peers/ never fills and CommitSigner.verify_signature
        answers None ("cert not cached") for every peer, every time — so message
        signatures can be produced but never checked.

        The identity is re-derived here rather than trusted from the caller,
        because callers are many and this store is one. node_id is the
        fingerprint of the public key, so a cert whose key hashes to the claimed
        node_id is that peer's by construction — which also makes overwriting a
        re-issued cert for the same key safe.

        Returns:
            True if the certificate is now stored, False if it was refused.
        """
        if peers_dir is None:
            peers_dir = DPC_HOME_DIR / "peers"

        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
            derived_id = generate_node_id(cert.public_key())
            if derived_id != peer_node_id:
                logger.warning(
                    "Refusing to cache certificate for %s: its public key hashes to %s",
                    peer_node_id, derived_id
                )
                return False

            peers_dir.mkdir(parents=True, exist_ok=True)
            cert_path = peers_dir / f"{peer_node_id}.crt"
            tmp_path = cert_path.with_suffix(".crt.tmp")
            tmp_path.write_text(cert_pem, encoding="utf-8")
            tmp_path.replace(cert_path)
            logger.debug("Cached peer certificate for %s", peer_node_id)
            return True

        except Exception as e:
            logger.warning("Could not cache certificate for %s: %s", peer_node_id, e)
            return False

    async def _listen_to_peer(self, peer: PeerConnection, link_ping: bool = False, direction: str = ""):
        """Background task to listen for messages from a TLS peer.

        `link_ping`: the peer advertised it in HELLO or HELLO_ACK, so its silence
        is watched (`_watch_peer_silence`); a peer that did not is never pinged
        and never cut. `direction` is the peer cache's "in" or "out", for the log.
        """
        watchdog = None
        if link_ping:
            watchdog = asyncio.create_task(self._watch_peer_silence(peer, direction))
            # getattr: tests build managers with __new__, without these attributes.
            if getattr(self, "_peer_watchdog_tasks", None) is None:
                self._peer_watchdog_tasks = {}
            self._peer_watchdog_tasks[peer.node_id] = watchdog
        try:
            while True:
                message = await peer.read()
                if message is None:
                    break

                # Answered here, before the router: a LINK_PING never reaches it,
                # and a busy service loop cannot make a live link look dead.
                command = message.get("command") if isinstance(message, dict) else None
                if command == "LINK_PING":
                    payload = message.get("payload")
                    asyncio.create_task(self._send_link_frame(
                        peer, {"command": "LINK_PONG", "payload": payload if isinstance(payload, dict) else {}},
                    ))
                    continue
                if command == "LINK_PONG":
                    payload = message.get("payload")
                    sent_at = payload.get("sent_at") if isinstance(payload, dict) else None
                    if isinstance(sent_at, (int, float)):
                        logger.debug("LINK_PONG from %s after %.3fs", peer.node_id, time.monotonic() - sent_at)
                    continue

                if self.on_message_received:
                    asyncio.create_task(self.on_message_received(peer.node_id, message))

        except asyncio.CancelledError:
            # Graceful cancellation during shutdown
            logger.debug("Listener task for peer %s cancelled during shutdown", peer.node_id)
            raise  # Re-raise to properly exit the task
        except (asyncio.IncompleteReadError, ConnectionResetError, TimeoutError):
            pass
        except OSError as e:
            # Any other socket error ends the link the same way (WinError 121
            # on a dead route); left to escape, it died as "Task exception was
            # never retrieved" with no errno beside the peer.
            logger.warning(
                "Read from peer %s failed: errno %s, winerror %s: %s",
                peer.node_id, e.errno, getattr(e, "winerror", None), e.strerror,
            )
        finally:
            # Clean up task tracking — only our own entry: after a reconnect the
            # slot holds the newer connection's listener
            if self._peer_listener_tasks.get(peer.node_id) is asyncio.current_task():
                self._peer_listener_tasks.pop(peer.node_id, None)
            if watchdog is not None:
                watchdog.cancel()
                if self._peer_watchdog_tasks.get(peer.node_id) is watchdog:
                    self._peer_watchdog_tasks.pop(peer.node_id, None)

            # Only trigger shutdown if this connection is still the one registered
            if self.peers.get(peer.node_id) is peer:
                logger.info("Connection with peer %s was lost", peer.node_id)
                await self.shutdown_peer_connection(peer.node_id)

    async def _watch_peer_silence(self, peer: PeerConnection, direction: str = ""):
        """End a direct TLS link to a link_ping peer that has gone silent.

        Any frame counts, a LINK_PONG no more than the rest: `last_received` is
        stamped per slice of every frame. After a third of link_silence_timeout with
        nothing, a LINK_PING asks for a frame, and again after each further
        third. At the threshold the transport is aborted — not closed: a TLS
        close on a dead path waits for a shutdown that never comes — so the
        pending read in `_listen_to_peer` ends and its cleanup, the only one,
        runs. That listener cancels this task when it ends.
        """
        threshold = getattr(self, "_link_silence_timeout", 90.0)
        interval = threshold / 3
        last_ping = 0.0
        while True:
            now = time.monotonic()
            silence = now - peer.last_received
            if silence >= threshold:
                logger.warning(
                    "No frame from peer %s for %.0fs (threshold %gs) — ending the connection",
                    peer.node_id, silence, threshold,
                )
                try:
                    self._log_dead_link_path(peer, silence, direction)
                except Exception as e:  # noqa: BLE001 - the verdict stands without its details
                    logger.debug("Could not describe the path to %s: %s", peer.node_id, e)
                peer.writer.transport.abort()
                return
            if now - max(peer.last_received, last_ping) >= interval:
                last_ping = now
                await self._send_link_frame(peer, {"command": "LINK_PING", "payload": {"sent_at": now}})
                continue
            wake = min(peer.last_received + threshold, max(peer.last_received, last_ping) + interval)
            await asyncio.sleep(max(wake - time.monotonic(), 0.01))

    async def _send_link_frame(self, peer: PeerConnection, message: Dict[str, Any]):
        """Write a LINK_PING or LINK_PONG within a third of the threshold.

        Bounded because write_message awaits drain(), which on a dead link with
        a full buffer never returns. A failure is only logged: the silence
        clock, not a write error, decides whether the link is dead.
        """
        timeout = getattr(self, "_link_silence_timeout", 90.0) / 3
        try:
            await asyncio.wait_for(peer.send(message), timeout=timeout)
        except asyncio.TimeoutError:
            logger.debug("%s to %s not drained within %gs", message["command"], peer.node_id, timeout)
        except Exception as e:  # noqa: BLE001 - a dead link is the watchdog's verdict
            logger.debug("Could not send %s to %s: %s", message["command"], peer.node_id, e)

    def _log_dead_link_path(self, peer: PeerConnection, silence: float, direction: str):
        """One INFO line beside the dead-link WARNING: the path the link ran on.

        To tell a peer whose process died from a path that died under a live
        peer — a VPN that dropped, or changed its exit — the line names both
        ends as the socket saw them, who dialled, when the last frame came,
        whether the peer's address is one of this node's own (interfaces,
        default route, the STUN-seen external address: `own_addresses`) and,
        when it is, which sources say so, a STUN one marked current or earlier
        (the set keeps an exit seen an hour ago: "we dialled our own interface"
        and "the peer sits behind our own VPN exit" read differently), and
        whether the local address is still on an interface of this host.
        """
        def _plain(ip):
            # A dual-stack socket names an IPv4 peer as ::ffff:a.b.c.d.
            return ip[7:] if isinstance(ip, str) and ip.startswith("::ffff:") else ip

        def _endpoint(info):
            if not info:
                return "unknown"
            host = _plain(info[0])
            return f"[{host}]:{info[1]}" if ":" in host else f"{host}:{info[1]}"

        peername = peer.writer.get_extra_info("peername")
        sockname = peer.writer.get_extra_info("sockname")
        peer_ip = _plain(peername[0]) if peername else None
        local_ip = _plain(sockname[0]) if sockname else None
        own = getattr(self, "own_addresses", None)
        if own is None or not peer_ip:
            peer_is_own = "unknown"
        else:
            peer_is_own = "yes" if own.contains(peer_ip) else "no"
            if peer_is_own == "yes":
                named = []
                for source in own.sources_of(peer_ip):
                    if source == "stun":
                        source += ": the current external address" if peer_ip == getattr(
                            self, "_external_ip", None) else ": an earlier external address"
                    named.append(source)
                if named:
                    peer_is_own += f" ({', '.join(named)})"
        if own is None or not local_ip:
            local_still_up = "unknown"
        else:
            local_still_up = "yes" if local_ip in own.learn_local_interfaces() else "no"
        dialler = {"out": "this node", "in": "the peer"}.get(direction, "unknown")
        last_frame_at = time.strftime("%H:%M:%S", time.localtime(time.time() - silence))
        logger.info(
            "Silent link to %s: peer %s, local %s, dialled by %s, last frame at %s; "
            "peer address is this node's own: %s; local address still on an interface: %s",
            peer.node_id, _endpoint(peername), _endpoint(sockname), dialler, last_frame_at,
            peer_is_own, local_still_up,
        )

    # --- WebRTC Connection Methods (NEW) ---

    def _create_webrtc_close_handler(self, node_id: str):
        """
        Create WebRTC connection close handler with auto-reconnect logic.

        Returns a closure that handles connection cleanup and auto-reconnect
        for the specified node_id.

        Args:
            node_id: Node ID of the peer

        Returns:
            Async function that handles connection close
        """
        async def handle_close(_node_id):
            logger.info("WebRTC connection closed for %s, cleaning up", node_id)
            if node_id in self.peers:
                self.peers.pop(node_id)
                await self._notify_peer_change()
            if node_id in self._pending_webrtc:
                self._pending_webrtc.pop(node_id)

            # Auto-reconnect if disconnection was not intentional
            if node_id not in self._intentional_disconnects:
                logger.info("Auto-Reconnect: Connection to %s lost unexpectedly", node_id)
                logger.info("Auto-Reconnect: Will attempt to reconnect in 3 seconds")
                await asyncio.sleep(3)

                # Check if hub_client is still connected
                stored_hub_client = self._hub_client_refs.get(node_id)
                if stored_hub_client and stored_hub_client.websocket and stored_hub_client.websocket.state == websockets.State.OPEN:
                    try:
                        logger.info("Auto-Reconnect: Reconnecting to %s", node_id)
                        await self.connect_via_hub(node_id, stored_hub_client)
                    except Exception as e:
                        logger.error("Auto-Reconnect: Failed to reconnect to %s: %s", node_id, e)
                else:
                    logger.warning("Auto-Reconnect: Cannot reconnect to %s - Hub client not connected", node_id)
                    self._hub_client_refs.pop(node_id, None)
            else:
                logger.info("Auto-Reconnect: Disconnection was intentional, not reconnecting")
                self._intentional_disconnects.discard(node_id)
                self._hub_client_refs.pop(node_id, None)

        return handle_close

    async def connect_via_hub(self, target_node_id: str, hub_client: HubClient):
        """
        Initiates a WebRTC connection to a peer via Hub signaling.
        This is the INITIATOR side (Alice).
        """
        if self.refuse_if_blocked(target_node_id, "Hub WebRTC dial"):
            raise ConnectionError(f"{target_node_id} is blocked. Unblock it first to connect.")

        logger.info("Initiating WebRTC connection to %s via Hub", target_node_id)

        if target_node_id in self.peers:
            logger.info("Already connected to %s", target_node_id)
            return

        if target_node_id in self._pending_webrtc:
            logger.info("Connection to %s already in progress", target_node_id)
            return

        try:
            # Store hub_client reference for reconnection
            self._hub_client_refs[target_node_id] = hub_client

            # Create WebRTC peer connection
            webrtc_peer = WebRTCPeerConnection(node_id=target_node_id, is_initiator=True)
            self._pending_webrtc[target_node_id] = webrtc_peer
            
            # Set up ICE candidate handler
            async def handle_ice(candidate_dict):
                await hub_client.send_signal(target_node_id, {
                    "type": "ice-candidate",
                    "candidate": candidate_dict
                })
            
            webrtc_peer.on_ice_candidate = handle_ice
            
            # Set up message handler
            async def handle_message(message):
                if self.on_message_received:
                    asyncio.create_task(self.on_message_received(target_node_id, message))
            
            webrtc_peer.on_message = handle_message

            # Set up close handler with auto-reconnect
            webrtc_peer.on_close = self._create_webrtc_close_handler(target_node_id)
            
            # Create and send offer
            offer = await webrtc_peer.create_offer()
            await hub_client.send_signal(target_node_id, {
                "type": "offer",
                "offer": offer
            })

            logger.info("WebRTC offer sent to %s, waiting for answer", target_node_id)

            # Start timeout task
            asyncio.create_task(self._handle_connection_timeout(target_node_id))

        except Exception as e:
            logger.error("Failed to initiate WebRTC connection to %s: %s", target_node_id, e, exc_info=True)
            self._pending_webrtc.pop(target_node_id, None)
            raise

    async def handle_incoming_signal(self, signal: Dict[str, Any], hub_client: HubClient):
        """
        Handles incoming WebRTC signaling messages from the Hub.
        Routes to appropriate handler based on signal type.
        
        Hub sends signals in this format:
        {
            "type": "signal",
            "sender_node_id": "dpc-node-xxx",
            "target_node_id": "dpc-node-yyy",
            "payload": {
                "type": "offer|answer|ice-candidate",
                "offer": {...} or "answer": {...} or "candidate": {...}
            }
        }
        """
        signal_type = signal.get("type")
        
        # Filter out non-signal messages (pong, auth_ok, error)
        if signal_type in ["pong", "auth_ok", "error"]:
            return
        
        # Handle Hub-wrapped signals
        if signal_type == "signal":
            # Extract sender and payload from Hub wrapper
            from_node = signal.get("sender_node_id")
            wrapped_payload = signal.get("payload", {})
            
            # The actual WebRTC signal type is inside the payload
            webrtc_signal_type = wrapped_payload.get("type")
            
            # Validate we have required fields
            if not from_node or not webrtc_signal_type:
                logger.warning("Received invalid Hub signal (missing sender_node_id or payload.type): %s", signal)
                return

            logger.debug("Received WebRTC signal '%s' from %s", webrtc_signal_type, from_node)

            try:
                if webrtc_signal_type == "offer":
                    await self._handle_webrtc_offer(from_node, wrapped_payload, hub_client)

                elif webrtc_signal_type == "answer":
                    await self._handle_webrtc_answer(from_node, wrapped_payload)

                elif webrtc_signal_type == "ice-candidate":
                    await self._handle_ice_candidate(from_node, wrapped_payload)

                else:
                    logger.warning("Unknown WebRTC signal type: %s", webrtc_signal_type)

            except Exception as e:
                logger.error("Error handling signal from %s: %s", from_node, e, exc_info=True)

        else:
            # Unknown signal format
            logger.warning("Received unknown signal format (type=%s): %s", signal_type, signal)

    async def _handle_webrtc_offer(self, from_node: str, payload: Dict[str, Any], hub_client: HubClient):
        """Handle incoming WebRTC offer (ANSWERER side - Bob)."""
        # No answer: the offerer learns nothing, as on the direct path.
        if self.refuse_if_blocked(from_node, "Hub WebRTC offer"):
            return

        logger.info("Handling WebRTC offer from %s", from_node)

        if from_node in self.peers:
            logger.info("Already connected to %s, ignoring offer", from_node)
            return

        # Store hub_client reference for reconnection
        self._hub_client_refs[from_node] = hub_client

        # Create WebRTC peer connection as answerer
        webrtc_peer = WebRTCPeerConnection(node_id=from_node, is_initiator=False)
        self._pending_webrtc[from_node] = webrtc_peer
        
        # Set up ICE candidate handler
        async def handle_ice(candidate_dict):
            await hub_client.send_signal(from_node, {
                "type": "ice-candidate",
                "candidate": candidate_dict
            })
        
        webrtc_peer.on_ice_candidate = handle_ice
        
        # Set up message handler
        async def handle_message(message):
            if self.on_message_received:
                asyncio.create_task(self.on_message_received(from_node, message))
        
        webrtc_peer.on_message = handle_message

        # Set up close handler with auto-reconnect
        webrtc_peer.on_close = self._create_webrtc_close_handler(from_node)

        # Handle offer and create answer
        offer_sdp = payload.get("offer")
        answer = await webrtc_peer.handle_offer(offer_sdp)
        
        # Send answer back
        await hub_client.send_signal(from_node, {
            "type": "answer",
            "answer": answer
        })

        logger.info("WebRTC answer sent to %s", from_node)

        # Process any buffered ICE candidates
        if from_node in self._ice_candidates_buffer:
            for candidate in self._ice_candidates_buffer[from_node]:
                await webrtc_peer.add_ice_candidate(candidate)
            del self._ice_candidates_buffer[from_node]

        # Wait for connection to be ready, then move to active peers
        # This ensures the UI is notified on the answerer side too
        asyncio.create_task(self._finalize_webrtc_connection(from_node))

    async def _handle_webrtc_answer(self, from_node: str, payload: Dict[str, Any]):
        """Handle incoming WebRTC answer (INITIATOR side - Alice)."""
        logger.info("Handling WebRTC answer from %s", from_node)

        webrtc_peer = self._pending_webrtc.get(from_node)
        if not webrtc_peer:
            logger.warning("No pending WebRTC connection for %s", from_node)
            return
        
        # Set remote description with answer
        answer_sdp = payload.get("answer")
        await webrtc_peer.handle_answer(answer_sdp)
        
        # Process any buffered ICE candidates
        if from_node in self._ice_candidates_buffer:
            for candidate in self._ice_candidates_buffer[from_node]:
                await webrtc_peer.add_ice_candidate(candidate)
            del self._ice_candidates_buffer[from_node]
        
        # Wait for connection to be ready, then move to active peers
        asyncio.create_task(self._finalize_webrtc_connection(from_node))

    async def _handle_ice_candidate(self, from_node: str, payload: Dict[str, Any]):
        """Handle incoming ICE candidate."""
        candidate = payload.get("candidate")
        # Not buffered for a node whose offer is refused: it would never drain.
        if is_blocked_by(getattr(self, "firewall", None), from_node):
            return

        webrtc_peer = self._pending_webrtc.get(from_node) or self.peers.get(from_node)
        
        if webrtc_peer and isinstance(webrtc_peer, WebRTCPeerConnection):
            await webrtc_peer.add_ice_candidate(candidate)
        else:
            # Buffer ICE candidates if connection not yet established
            if from_node not in self._ice_candidates_buffer:
                self._ice_candidates_buffer[from_node] = []
            self._ice_candidates_buffer[from_node].append(candidate)
            logger.debug("Buffered ICE candidate from %s (total: %d)", from_node, len(self._ice_candidates_buffer[from_node]))

    async def _finalize_webrtc_connection(self, node_id: str):
        """Wait for WebRTC connection to be ready and move to active peers."""
        webrtc_peer = self._pending_webrtc.get(node_id)
        if not webrtc_peer:
            return

        try:
            # Wait for connection to be ready (use configured timeout)
            timeout = self.settings.get_p2p_connection_timeout() if self.settings else 60.0
            await webrtc_peer.wait_ready(timeout=timeout)

            # Move from pending to active peers
            self._pending_webrtc.pop(node_id, None)

            # Blocked while ICE ran: closed instead of entered.
            if self.refuse_if_blocked(node_id, "Hub WebRTC connection"):
                await webrtc_peer.close()
                return

            # Set strategy_used for WebRTC connections (for UI display)
            webrtc_peer.strategy_used = "hub_webrtc"

            self.peers[node_id] = webrtc_peer

            await self._notify_peer_change()
            logger.info("WebRTC connection established with %s", node_id)

            # Exchange names (WebRTC doesn't have initial HELLO handshake like TLS)
            from dpc_protocol.protocol import create_hello_message
            try:
                hello_msg = create_hello_message(self.node_id, self.display_name)
                await self.send_message_to_peer(node_id, hello_msg)
                logger.debug("Sent name to %s: %s", node_id, self.display_name)
            except Exception as e:
                logger.warning("Failed to send name to %s: %s", node_id, e)

            # Auto-discover peer's available AI providers
            from dpc_protocol.protocol import create_get_providers_message
            try:
                await self.send_message_to_peer(node_id, create_get_providers_message())
                logger.debug("Requested AI providers from %s", node_id)
            except Exception as e:
                logger.warning("Failed to request providers from %s: %s", node_id, e)

        except Exception as e:
            logger.error("Failed to finalize WebRTC connection with %s: %s", node_id, e, exc_info=True)
            self._pending_webrtc.pop(node_id, None)
            await webrtc_peer.close()

    async def _handle_connection_timeout(self, node_id: str):
        """Handle timeout for WebRTC connection establishment."""
        try:
            # Use configured timeout
            timeout = self.settings.get_p2p_connection_timeout() if self.settings else 60.0
            await asyncio.sleep(timeout)

            # Check if connection is still pending
            if node_id in self._pending_webrtc and node_id not in self.peers:
                logger.warning("WebRTC connection to %s timed out after %.1f seconds", node_id, timeout)
                webrtc_peer = self._pending_webrtc.pop(node_id, None)
                if webrtc_peer:
                    await webrtc_peer.close()

                # Clean up ICE candidates buffer
                self._ice_candidates_buffer.pop(node_id, None)

        except Exception as e:
            logger.error("Error in connection timeout handler: %s", e, exc_info=True)

    # --- Unified Connection Management ---

    async def send_message_to_peer(self, node_id: str, message: Dict[str, Any]):
        """Sends a message to a specific connected peer (works for both TLS and WebRTC)."""
        if node_id not in self.peers:
            raise ConnectionError(f"Not connected to peer {node_id}.")
        
        peer = self.peers[node_id]
        await peer.send(message)

    async def _get_primary_local_ip(self) -> str:
        """
        Get the local IP address for DHT announcements.

        Prefers this node's first LAN address (`lan_addresses`: a physical
        interface, 192.168/16 first), because behind a full-tunnel VPN the OS's
        route toward the internet is the tunnel address, which a peer on the
        same LAN cannot reach. With no LAN address it falls back to the source
        address of the default route, or 0.0.0.0 when that fails too.

        Every address seen here, the tunnel's included, is learned as our own,
        so self-dial detection stays as wide as before.

        For internet-wide DHT, external IP should be used instead (call update_dht_ip()).
        """
        route_ip = self._default_route_source_ip()
        # The route guess breaks a tie inside a range, and names a full tunnel.
        lan = lan_addresses(route_ip)
        self.own_addresses.learn_all(lan, "lan interface")
        self.own_addresses.learn(route_ip, "default route")
        if lan:
            logger.debug("Detected LAN IP for DHT: %s (default route: %s)", lan[0], route_ip)
            return lan[0]
        return route_ip

    def _own_local_addresses(self) -> list:
        """This node's `local_addresses` for HELLO and HELLO_ACK; [] when not listening or unknown."""
        port = getattr(self, "_listen_port", None)
        if not port:
            return []
        try:
            return local_addresses_to_advertise(lan_addresses(self._default_route_source_ip()), port)
        except Exception as e:  # noqa: BLE001 - a hint must never stop a handshake
            logger.debug("Could not list local_addresses: %s", e)
            return []

    def _own_local_addresses_for(self, node_id: str) -> Optional[list]:
        """`local_addresses` for this peer's HELLO or HELLO_ACK; None leaves the key out.

        Only a peer in a group with this node, or one this node has dialled
        successfully (a peer-cache address with direction "out"), gets them, so a
        stranger that reaches the port does not learn the LAN (Mike's call,
        2026-10-30). Every inbound handshake writes the cache, so a cache entry
        alone does not count: a stranger that dials in twice would have one. Both
        handshake sides ask after the peer's node id is proven and before this
        handshake writes the cache.

        Coverage: no live peer of this node holds an "out" address today, so in
        practice the addresses go only through a shared group.
        """
        try:
            cached = self.peer_cache.get_peer(node_id)
            known = cached is not None and any(
                entry.get("direction") == "out" for entry in (cached.addresses or [])
            )
            if not known and getattr(self, "groups_for_peer", None):
                known = bool(self.groups_for_peer(node_id))
        except Exception as e:  # noqa: BLE001 - a hint must never stop a handshake
            logger.debug("Could not tell whether %s is known: %s", node_id[:20], e)
            known = False
        return self._own_local_addresses() if known else None

    def _default_route_source_ip(self) -> str:
        """The source address the OS would use toward 8.8.8.8, or 0.0.0.0."""
        import socket
        try:
            # Use UDP socket trick to get local IP (doesn't actually send packet)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            logger.debug("Detected primary local IP for DHT: %s", local_ip)
            return local_ip
        except Exception as e:
            logger.warning("Failed to detect local IP for DHT, using 0.0.0.0: %s", e)
            return "0.0.0.0"

    async def update_dht_ip(self, external_ip: str):
        """
        Update DHT announcement IP (e.g., after external IP is discovered via STUN).

        This allows DHT to announce with external IP for internet-wide discovery
        instead of local IP.

        Args:
            external_ip: External/public IP address to announce
        """
        # Learned before the guard: the peer-cache check needs this address
        # even on a node whose DHT is switched off.
        self.own_addresses.learn(external_ip, "stun")
        self._external_ip = external_ip

        if not self.dht_manager:
            return

        logger.info("Updating DHT announcement IP to %s", external_ip)
        self.dht_manager.ip = external_ip

        # Re-announce with new IP
        try:
            await self.announce_to_dht()
        except Exception as e:
            logger.error("Failed to re-announce with new IP: %s", e)

    async def restart_webrtc_connections(self):
        """
        Restart all active WebRTC connections (e.g., after network change).

        This method disconnects all WebRTC connections and relies on
        auto-reconnect to establish new connections via the 6-tier fallback
        hierarchy. The reconnection may use a different strategy (direct, relay,
        etc.) depending on network conditions.

        Algorithm:
            1. Identify all WebRTC connections (both active and pending)
            2. Close them without marking as intentional disconnect
            3. Auto-reconnect mechanism will trigger reconnection via ConnectionOrchestrator
            4. ConnectionOrchestrator tries 6-tier fallback (may not use WebRTC again)

        Use cases:
            - External IP change detected (WiFi reconnect, VPN change, CGNAT rotation)
            - Network switch (mobile → WiFi, WiFi → ethernet)
            - ICE connection failure requiring fresh connection attempt
        """
        webrtc_peers_to_restart = []

        # Collect WebRTC peers from active connections
        for peer_id, peer in list(self.peers.items()):
            if isinstance(peer, WebRTCPeerConnection):
                webrtc_peers_to_restart.append(peer_id)

        # Also collect from pending WebRTC connections
        for peer_id in list(self._pending_webrtc.keys()):
            if peer_id not in webrtc_peers_to_restart:
                webrtc_peers_to_restart.append(peer_id)

        if not webrtc_peers_to_restart:
            logger.debug("No WebRTC connections to restart")
            return

        logger.info(
            "Restarting %d WebRTC connection(s) due to network change",
            len(webrtc_peers_to_restart)
        )

        # Close WebRTC connections (without marking as intentional disconnect)
        # Auto-reconnect will trigger reconnection via 6-tier fallback
        for peer_id in webrtc_peers_to_restart:
            try:
                # Remove from active peers
                if peer_id in self.peers:
                    peer = self.peers.pop(peer_id)
                    await peer.close()
                    logger.debug("Closed active WebRTC connection with %s", peer_id[:20])

                # Remove from pending WebRTC
                if peer_id in self._pending_webrtc:
                    pending_peer = self._pending_webrtc.pop(peer_id)
                    await pending_peer.close()
                    logger.debug("Closed pending WebRTC connection with %s", peer_id[:20])

                # Clean up ICE candidate buffer
                self._ice_candidates_buffer.pop(peer_id, None)

            except Exception as e:
                logger.error("Error closing WebRTC connection with %s: %s", peer_id[:20], e)

        # Notify UI of peer list change
        await self._notify_peer_change()

        logger.info(
            "WebRTC restart complete - auto-reconnect will establish new connections"
        )

    async def shutdown_peer_connection(self, peer_id: str):
        """Close connection with a peer (user-initiated)."""
        # Mark this as an intentional disconnect to prevent auto-reconnect
        self._intentional_disconnects.add(peer_id)

        if peer_id in self.peers:
            peer = self.peers.pop(peer_id)
            await peer.close()
            logger.info("Connection with %s closed (intentional)", peer_id)
            # Ensure status update is broadcast
            await self._notify_peer_change()

        # Also clean up pending WebRTC connections
        if peer_id in self._pending_webrtc:
            webrtc_peer = self._pending_webrtc.pop(peer_id)
            await webrtc_peer.close()
            logger.info("Pending WebRTC connection with %s cleaned up", peer_id)
            # Notify peer list change even for pending connections
            await self._notify_peer_change()

        # Clean up buffered ICE candidates
        self._ice_candidates_buffer.pop(peer_id, None)

        # Notify about peer disconnection (for cleanup like file transfers)
        if self.on_peer_disconnected:
            try:
                await self.on_peer_disconnected(peer_id)
            except Exception as e:
                logger.error(f"Error in on_peer_disconnected callback for {peer_id}: {e}", exc_info=True)

        # Note: hub_client_refs will be cleaned up in the close handler

    async def shutdown_all(self):
        """Shutdown all connections (intentional shutdown)."""
        logger.info("Shutting down P2P Manager")

        # Mark all disconnections as intentional to prevent auto-reconnect
        for peer_id in list(self.peers.keys()):
            self._intentional_disconnects.add(peer_id)
        for peer_id in list(self._pending_webrtc.keys()):
            self._intentional_disconnects.add(peer_id)

        # Step 1: Cancel server task
        if self._server_task:
            self._server_task.cancel()

        # Step 2: Cancel all listener tasks BEFORE closing connections
        logger.debug("Cancelling %d listener tasks", len(self._peer_listener_tasks))
        for task in self._peer_listener_tasks.values():
            task.cancel()

        # Wait for all listener tasks to finish (with exceptions suppressed)
        if self._peer_listener_tasks:
            await asyncio.gather(*self._peer_listener_tasks.values(), return_exceptions=True)
        self._peer_listener_tasks.clear()
        # Each listener cancels its own watchdog on the way out; none outlives it.
        getattr(self, "_peer_watchdog_tasks", {}).clear()

        # Step 3: Now safely close peer connections
        for peer_id in list(self.peers.keys()):
            peer = self.peers.pop(peer_id)
            await peer.close()

        for peer_id in list(self._pending_webrtc.keys()):
            webrtc_peer = self._pending_webrtc.pop(peer_id)
            await webrtc_peer.close()

        # Clean up all tracking structures
        self._hub_client_refs.clear()
        self._intentional_disconnects.clear()
        self._ice_candidates_buffer.clear()

        # Shutdown DHT if active
        if self.dht_manager:
            try:
                await self.dht_manager.stop()
                logger.info("DHT shut down")
            except Exception as e:
                logger.error("Error shutting down DHT: %s", e)
            self.dht_manager = None

        logger.info("P2P Manager shut down")