# dpc-client/core/dpc_client_core/p2p_manager_webrtc.py
# Обновленная версия P2PManager с поддержкой WebRTC

import asyncio
import ssl
import json
import logging
import platform
from typing import Dict, Any, Callable, Tuple, Union

from cryptography import x509
import websockets

from dpc_protocol.crypto import generate_node_id, load_identity, generate_identity
from dpc_protocol.protocol import read_message, write_message, create_hello_message
from dpc_protocol.pcm_core import PCMCore, PersonalContext
from dpc_protocol.utils import parse_dpc_uri

from .firewall import ContextFirewall
from .hub_client import HubClient
from .webrtc_peer import WebRTCPeerConnection
from .dht import DHTManager, DHTConfig
from .peer_cache import PeerCache
from pathlib import Path

logger = logging.getLogger(__name__)


class PeerConnection:
    """A unified wrapper for a direct TLS P2P connection."""
    def __init__(self, node_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.node_id = node_id
        self.reader = reader
        self.writer = writer
        self.connection_type = "direct_tls"

    async def send(self, message: Dict[str, Any]):
        """Sends a message over the TLS stream."""
        await write_message(self.writer, message)

    async def read(self) -> Dict[str, Any] | None:
        """Reads a message from the TLS stream."""
        return await read_message(self.reader)

    async def close(self):
        """Closes the TLS connection with timeout protection."""
        if not self.writer.is_closing():
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("SSL shutdown timed out for peer %s", self.node_id)


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

        # DHT (Distributed Hash Table) for decentralized peer discovery
        self.dht_manager: DHTManager | None = None  # Initialized in start_server()

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
        self._server_task = None
        logger.info("P2PManager initialized with WebRTC support")

    def set_on_peer_list_change(self, callback: Callable):
        self.on_peer_list_change = callback

    def set_on_message_received(self, callback: Callable):
        self.on_message_received = callback
    
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

        if host == "dual":
            # Windows requires separate IPv4 and IPv6 listeners
            # On Windows, binding to [::] only listens on IPv6 (doesn't accept IPv4-mapped addresses)
            # On Linux/macOS, binding to [::] with IPV6_V6ONLY=0 works for both
            is_windows = platform.system() == "Windows"

            if is_windows:
                # Windows: Create separate IPv4 and IPv6 listeners
                logger.info("Windows detected - creating separate IPv4 and IPv6 listeners")

                try:
                    server_v4 = await asyncio.start_server(
                        self._handle_direct_connection, "0.0.0.0", port, ssl=ssl_context
                    )
                    server_v6 = await asyncio.start_server(
                        self._handle_direct_connection, "::", port, ssl=ssl_context
                    )

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
                    server_v4 = await asyncio.start_server(
                        self._handle_direct_connection, "0.0.0.0", port, ssl=ssl_context
                    )
                    server_v6 = await asyncio.start_server(
                        self._handle_direct_connection, "::", port, ssl=ssl_context
                    )

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
            server = await asyncio.start_server(
                self._handle_direct_connection, host, port, ssl=ssl_context
            )
            self._server_task = asyncio.create_task(server.serve_forever())

            # Format IPv6 addresses with brackets for clarity
            formatted_host = f"[{host}]" if ":" in host else host
            logger.info("P2PManager Direct TLS server listening on %s:%d for node %s",
                      formatted_host, port, self.node_id)

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
                if seed_nodes:
                    logger.info("Bootstrapping DHT from %d seed nodes", len(seed_nodes))
                    success = await self.dht_manager.bootstrap(seed_nodes)
                    if success:
                        logger.info("DHT bootstrap successful")
                        # Announce our presence to the DHT
                        await self.announce_to_dht()
                    else:
                        logger.warning("DHT bootstrap failed (no responsive seeds)")
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
        if cached_peer and cached_peer.last_direct_ip and cached_peer.is_recently_seen(hours=168):  # 7 days
            logger.info(
                "Attempting cached connection to %s at %s:%d (last seen %s)",
                target_node_id[:20], cached_peer.last_direct_ip,
                cached_peer.last_direct_port, cached_peer.last_seen
            )
            try:
                await self.connect_directly(
                    cached_peer.last_direct_ip,
                    cached_peer.last_direct_port,
                    target_node_id,
                    timeout=5.0  # Quick timeout for cache attempt
                )
                logger.info("Connected to %s via peer cache (no DHT lookup needed)", target_node_id[:20])
                # Update cache with successful connection
                self.peer_cache.add_or_update_peer(
                    node_id=target_node_id,
                    direct_ip=cached_peer.last_direct_ip,
                    direct_port=cached_peer.last_direct_port,
                    supports_direct=True
                )
                return True
            except Exception as e:
                logger.debug("Cached connection failed, falling back to DHT: %s", e)
                # Don't return False yet, try DHT next

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
                    supports_direct=True
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

    async def _handle_direct_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handles an incoming raw TLS connection (Server-side)."""
        peer_node_id = None
        try:
            logger.info("Received a direct TLS connection attempt")
            await asyncio.sleep(0.01)

            hello_msg = await read_message(reader)
            if not hello_msg or hello_msg.get("command") != "HELLO":
                raise ConnectionError("Invalid HELLO message received.")

            payload = hello_msg.get("payload", {})
            peer_node_id = payload.get("node_id")
            peer_name = payload.get("name")

            if not peer_node_id:
                raise ValueError("Peer did not provide a node_id.")

            logger.info("Connection from node: %s", peer_node_id)
            if peer_name:
                logger.info("Peer name: %s", peer_name)
                if hasattr(self, '_core_service_ref') and self._core_service_ref:
                    self._core_service_ref.set_peer_metadata(peer_node_id, name=peer_name)

            ack = {
                "command": "HELLO_ACK",
                "status": "OK",
                "name": self.display_name
            }
            await write_message(writer, ack)

            peer = PeerConnection(node_id=peer_node_id, reader=reader, writer=writer)

            # Set strategy_used for incoming connections (for UI display)
            sock = writer.get_extra_info('socket')
            if sock:
                import socket as socket_module
                if sock.family == socket_module.AF_INET6:
                    peer.strategy_used = "ipv6_direct"
                else:
                    peer.strategy_used = "ipv4_direct"
            else:
                peer.strategy_used = "ipv4_direct"  # Default fallback

            self.peers[peer_node_id] = peer
            await self._notify_peer_change()
            logger.info("Direct TLS connection established with %s", peer_node_id)

            # Update peer cache with incoming connection info
            peer_addr = writer.get_extra_info('peername')
            if peer_addr:
                peer_ip, peer_port = peer_addr[0], peer_addr[1]
                self.peer_cache.add_or_update_peer(
                    node_id=peer_node_id,
                    display_name=peer_name,
                    direct_ip=peer_ip,
                    direct_port=8888,  # Default port (peer connects FROM random port but listens ON 8888)
                    supports_direct=True
                )
                logger.debug("Cached peer %s at %s:8888", peer_node_id[:20], peer_ip)

            # Announce to DHT after successful connection
            asyncio.create_task(self.announce_to_dht())

            # Start listener task and track it for graceful shutdown
            task = asyncio.create_task(self._listen_to_peer(peer))
            self._peer_listener_tasks[peer_node_id] = task

        except Exception as e:
            logger.error("Error handling direct connection: %s", e, exc_info=True)
            if peer_node_id and peer_node_id in self.peers:
                await self.shutdown_peer_connection(peer_node_id)
            else:
                writer.close()
                await writer.wait_closed()

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
        # Use configured timeout if not explicitly provided
        if timeout is None:
            timeout = self.settings.get_p2p_connection_timeout() if self.settings else 30.0

        logger.info("Initiating direct connection to %s at %s:%d", target_node_id, host, port)

        # Pre-flight check: Test basic port connectivity before SSL handshake
        # This provides clearer error messages than cryptic SSL errors (e.g., WinError 121)
        logger.debug("Running pre-flight port connectivity check for %s:%d", host, port)
        preflight_timeout = 30.0  # Increased for high-latency networks (mobile carriers, CGNAT)
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

        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        try:
            # Add timeout to prevent long hangs
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_context),
                timeout=timeout
            )

            hello = {
                "command": "HELLO",
                "payload": {
                    "node_id": self.node_id,
                    "name": self.display_name
                }
            }
            await write_message(writer, hello)

            response = await read_message(reader)
            if not response or response.get("status") != "OK":
                raise ConnectionError(f"Peer did not acknowledge HELLO.")

            peer_name = response.get("name")
            if peer_name:
                logger.info("Peer name: %s", peer_name)
                if hasattr(self, '_core_service_ref') and self._core_service_ref:
                    self._core_service_ref.set_peer_metadata(target_node_id, name=peer_name)

            peer = PeerConnection(node_id=target_node_id, reader=reader, writer=writer)

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

            self.peers[target_node_id] = peer
            await self._notify_peer_change()
            logger.info("Direct connection established with %s", target_node_id)

            # Announce to DHT after successful connection
            asyncio.create_task(self.announce_to_dht())

            # Start listener task and track it for graceful shutdown
            task = asyncio.create_task(self._listen_to_peer(peer))
            self._peer_listener_tasks[target_node_id] = task

            # Auto-discover peer's available AI providers
            from dpc_protocol.protocol import create_get_providers_message
            try:
                await self.send_message_to_peer(target_node_id, create_get_providers_message())
                logger.debug("Requested AI providers from %s", target_node_id)
            except Exception as e:
                logger.warning("Failed to request providers from %s: %s", target_node_id, e)

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
        except Exception as e:
            logger.error("Failed to establish direct connection with %s: %s", target_node_id, e, exc_info=True)
            raise

    async def _listen_to_peer(self, peer: PeerConnection):
        """Background task to listen for messages from a TLS peer."""
        try:
            while True:
                message = await peer.read()
                if message is None:
                    break

                if self.on_message_received:
                    asyncio.create_task(self.on_message_received(peer.node_id, message))

        except asyncio.CancelledError:
            # Graceful cancellation during shutdown
            logger.debug("Listener task for peer %s cancelled during shutdown", peer.node_id)
            raise  # Re-raise to properly exit the task
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            # Clean up task tracking
            self._peer_listener_tasks.pop(peer.node_id, None)

            # Only trigger shutdown if not already shutting down
            if peer.node_id in self.peers:
                logger.info("Connection with peer %s was lost", peer.node_id)
                await self.shutdown_peer_connection(peer.node_id)

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
            timeout = self.settings.get_p2p_connection_timeout() if self.settings else 30.0
            await webrtc_peer.wait_ready(timeout=timeout)

            # Move from pending to active peers
            self._pending_webrtc.pop(node_id, None)

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
            timeout = self.settings.get_p2p_connection_timeout() if self.settings else 30.0
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
        Get the primary local IP address for DHT announcements.

        Returns the first non-Docker, non-loopback IPv4 address,
        or falls back to 0.0.0.0 if none found.

        Note: For local network testing, this returns LAN IP (192.168.x.x).
        For internet-wide DHT, external IP should be used instead (call update_dht_ip()).
        """
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