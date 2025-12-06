"""
DHT RPC Handler - UDP-based Kademlia RPC Implementation

This module implements the Kademlia RPC protocol over UDP:
- PING/PONG - Node liveness checks
- FIND_NODE - Find k closest nodes to target (iterative lookup)
- STORE - Store key-value pairs (node_id → ip:port)
- FIND_VALUE - Find stored value or return k closest nodes

All RPCs use JSON over UDP with timeout/retry logic.

Protocol Format:
    {
        "type": "PING|PONG|FIND_NODE|NODES_FOUND|STORE|STORED|FIND_VALUE|VALUE_FOUND",
        "rpc_id": "uuid",
        "node_id": "sender_node_id",
        "timestamp": 1234567890,
        ...type-specific fields...
    }
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Dict, Any, List, Optional, Tuple, Callable
from dataclasses import dataclass

from .routing import DHTNode, RoutingTable

logger = logging.getLogger(__name__)


@dataclass
class RPCConfig:
    """RPC configuration parameters."""
    timeout: float = 5.0  # RPC timeout in seconds (increased for internet-wide DHT)
    max_retries: int = 3  # Max retry attempts
    max_packet_size: int = 8192  # Max UDP packet size (8KB)
    rate_limit_per_ip: int = 100  # Max RPCs per minute per IP


class DHTRPCHandler:
    """
    Handles DHT RPC messages over UDP.

    Implements Kademlia RPC protocol with:
    - Asynchronous UDP communication
    - Request-response matching via rpc_id
    - Timeout and retry logic
    - Rate limiting (security)
    - Message serialization/deserialization
    """

    def __init__(
        self,
        routing_table: RoutingTable,
        config: Optional[RPCConfig] = None
    ):
        """
        Initialize RPC handler.

        Args:
            routing_table: Routing table for node lookups
            config: RPC configuration (optional)
        """
        self.routing_table = routing_table
        self.config = config or RPCConfig()

        # UDP transport
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.protocol: Optional['DHTProtocol'] = None

        # Pending RPC tracking (rpc_id → Future)
        self._pending_rpcs: Dict[str, asyncio.Future] = {}

        # Key-value storage (key → value, for STORE/FIND_VALUE)
        self._storage: Dict[str, str] = {}

        # Rate limiting (ip → (count, reset_time))
        self._rate_limiter: Dict[str, Tuple[int, float]] = {}

        # Statistics
        self.stats = {
            "rpcs_sent": 0,
            "rpcs_received": 0,
            "timeouts": 0,
            "errors": 0,
        }

    async def start(self, host: str = "0.0.0.0", port: int = 8889):
        """
        Start UDP server for DHT RPCs.

        Args:
            host: Bind address (0.0.0.0 for all interfaces)
            port: UDP port (default: TLS port + 1 = 8889)
        """
        loop = asyncio.get_event_loop()

        try:
            self.transport, self.protocol = await loop.create_datagram_endpoint(
                lambda: DHTProtocol(self),
                local_addr=(host, port)
            )
            logger.info("DHT RPC server started on %s:%d (UDP)", host, port)
        except Exception as e:
            logger.error("Failed to start DHT RPC server: %s", e)
            raise

    async def stop(self):
        """Stop UDP server."""
        if self.transport:
            self.transport.close()
            logger.info("DHT RPC server stopped")

    # ===== Outgoing RPCs (Client-side) =====

    async def ping(self, ip: str, port: int) -> Optional[Dict]:
        """
        Send PING RPC to node.

        Args:
            ip: Target IP address
            port: Target UDP port

        Returns:
            PONG response dict or None if timeout
        """
        rpc = {
            "type": "PING",
            "rpc_id": str(uuid.uuid4()),
            "node_id": self.routing_table.node_id,
            "timestamp": time.time(),
        }

        response = await self._send_rpc(ip, port, rpc)

        if response and response.get("type") == "PONG":
            # Update routing table with responsive node
            peer_id = response.get("node_id")
            if peer_id:
                self.routing_table.add_node(peer_id, ip, port)
            return response

        return None

    async def find_node(
        self,
        ip: str,
        port: int,
        target_id: str
    ) -> Optional[List[DHTNode]]:
        """
        Send FIND_NODE RPC to node.

        Args:
            ip: Target IP address
            port: Target UDP port
            target_id: Node ID to find

        Returns:
            List of k closest nodes or None if timeout
        """
        rpc = {
            "type": "FIND_NODE",
            "rpc_id": str(uuid.uuid4()),
            "node_id": self.routing_table.node_id,
            "target_id": target_id,
            "timestamp": time.time(),
        }

        response = await self._send_rpc(ip, port, rpc)

        if response and response.get("type") == "NODES_FOUND":
            # Parse nodes from response
            nodes_data = response.get("nodes", [])
            nodes = [
                DHTNode(
                    node_id=n["node_id"],
                    ip=n["ip"],
                    port=n["port"]
                )
                for n in nodes_data
            ]
            return nodes

        return None

    async def store(
        self,
        ip: str,
        port: int,
        key: str,
        value: str
    ) -> bool:
        """
        Send STORE RPC to node.

        Args:
            ip: Target IP address
            port: Target UDP port
            key: Storage key (typically node_id)
            value: Storage value (typically "ip:port")

        Returns:
            True if stored successfully
        """
        rpc = {
            "type": "STORE",
            "rpc_id": str(uuid.uuid4()),
            "node_id": self.routing_table.node_id,
            "key": key,
            "value": value,
            "timestamp": time.time(),
        }

        response = await self._send_rpc(ip, port, rpc)

        if response and response.get("type") == "STORED":
            return response.get("success", False)

        return False

    async def find_value(
        self,
        ip: str,
        port: int,
        key: str
    ) -> Optional[Dict]:
        """
        Send FIND_VALUE RPC to node.

        Args:
            ip: Target IP address
            port: Target UDP port
            key: Key to find

        Returns:
            {"value": str} if found, {"nodes": List[DHTNode]} if not found
        """
        rpc = {
            "type": "FIND_VALUE",
            "rpc_id": str(uuid.uuid4()),
            "node_id": self.routing_table.node_id,
            "key": key,
            "timestamp": time.time(),
        }

        response = await self._send_rpc(ip, port, rpc)

        if response:
            if response.get("type") == "VALUE_FOUND":
                return {"value": response.get("value")}
            elif response.get("type") == "NODES_FOUND":
                nodes_data = response.get("nodes", [])
                nodes = [
                    DHTNode(
                        node_id=n["node_id"],
                        ip=n["ip"],
                        port=n["port"]
                    )
                    for n in nodes_data
                ]
                return {"nodes": nodes}

        return None

    # ===== Incoming RPCs (Server-side) =====

    async def handle_rpc(self, data: bytes, addr: Tuple[str, int]):
        """
        Handle incoming RPC message.

        Args:
            data: Raw UDP packet data
            addr: (ip, port) tuple of sender
        """
        sender_ip, sender_port = addr

        # Rate limiting
        if not self._check_rate_limit(sender_ip):
            logger.warning("Rate limit exceeded for %s", sender_ip)
            return

        self.stats["rpcs_received"] += 1

        try:
            # Deserialize message
            message = json.loads(data.decode('utf-8'))
            rpc_type = message.get("type")
            rpc_id = message.get("rpc_id")

            logger.debug("Received %s from %s:%d", rpc_type, sender_ip, sender_port)

            # Route to handler
            if rpc_type == "PING":
                await self._handle_ping(message, addr)
            elif rpc_type == "FIND_NODE":
                await self._handle_find_node(message, addr)
            elif rpc_type == "STORE":
                await self._handle_store(message, addr)
            elif rpc_type == "FIND_VALUE":
                await self._handle_find_value(message, addr)
            elif rpc_type in ["PONG", "NODES_FOUND", "STORED", "VALUE_FOUND"]:
                # Response to our RPC - resolve pending future
                await self._handle_response(rpc_id, message)
            else:
                logger.warning("Unknown RPC type: %s", rpc_type)

        except json.JSONDecodeError as e:
            logger.error("Invalid JSON from %s: %s", sender_ip, e)
            self.stats["errors"] += 1
        except Exception as e:
            logger.error("Error handling RPC from %s: %s", sender_ip, e, exc_info=True)
            self.stats["errors"] += 1

    async def _handle_ping(self, message: Dict, addr: Tuple[str, int]):
        """Handle PING RPC (respond with PONG)."""
        sender_ip, sender_port = addr
        sender_id = message.get("node_id")
        rpc_id = message.get("rpc_id")

        logger.info("DHT: Handling PING from %s (node_id=%s, rpc_id=%s)", addr, sender_id, rpc_id)

        # Update routing table
        if sender_id:
            try:
                self.routing_table.add_node(sender_id, sender_ip, sender_port)
                logger.info("DHT: Added %s to routing table", sender_id[:20])
            except Exception as e:
                logger.error("DHT: Failed to add node to routing table: %s", e)

        # Send PONG response
        response = {
            "type": "PONG",
            "rpc_id": rpc_id,
            "node_id": self.routing_table.node_id,
            "timestamp": time.time(),
        }

        logger.info("DHT: Sending PONG to %s (rpc_id=%s)", addr, rpc_id)
        await self._send_response(addr, response)
        logger.info("DHT: PONG sent to %s", addr)

    async def _handle_find_node(self, message: Dict, addr: Tuple[str, int]):
        """Handle FIND_NODE RPC (return k closest nodes)."""
        target_id = message.get("target_id")
        rpc_id = message.get("rpc_id")

        if not target_id:
            logger.warning("FIND_NODE missing target_id from %s", addr)
            return

        # Find k closest nodes
        closest = self.routing_table.find_closest_nodes(target_id, count=20)

        # Serialize nodes
        nodes_data = [
            {"node_id": node.node_id, "ip": node.ip, "port": node.port}
            for node in closest
        ]

        # Send response
        response = {
            "type": "NODES_FOUND",
            "rpc_id": rpc_id,
            "nodes": nodes_data,
            "timestamp": time.time(),
        }

        await self._send_response(addr, response)

    async def _handle_store(self, message: Dict, addr: Tuple[str, int]):
        """Handle STORE RPC (store key-value pair)."""
        key = message.get("key")
        value = message.get("value")
        rpc_id = message.get("rpc_id")

        if not key or not value:
            logger.warning("STORE missing key/value from %s", addr)
            return

        # Store key-value pair
        self._storage[key] = value
        logger.debug("Stored %s → %s", key[:20], value)

        # Send response
        response = {
            "type": "STORED",
            "rpc_id": rpc_id,
            "success": True,
            "timestamp": time.time(),
        }

        await self._send_response(addr, response)

    async def _handle_find_value(self, message: Dict, addr: Tuple[str, int]):
        """Handle FIND_VALUE RPC (return value or k closest nodes)."""
        key = message.get("key")
        rpc_id = message.get("rpc_id")

        if not key:
            logger.warning("FIND_VALUE missing key from %s", addr)
            return

        # Check if we have the value
        if key in self._storage:
            # Found - return value
            response = {
                "type": "VALUE_FOUND",
                "rpc_id": rpc_id,
                "value": self._storage[key],
                "timestamp": time.time(),
            }
        else:
            # Not found - return k closest nodes
            closest = self.routing_table.find_closest_nodes(key, count=20)
            nodes_data = [
                {"node_id": node.node_id, "ip": node.ip, "port": node.port}
                for node in closest
            ]

            response = {
                "type": "NODES_FOUND",
                "rpc_id": rpc_id,
                "nodes": nodes_data,
                "timestamp": time.time(),
            }

        await self._send_response(addr, response)

    async def _handle_response(self, rpc_id: str, message: Dict):
        """Handle RPC response (resolve pending future)."""
        if rpc_id in self._pending_rpcs:
            future = self._pending_rpcs.pop(rpc_id)
            if not future.done():
                future.set_result(message)

    # ===== Internal RPC Helpers =====

    async def _send_rpc(
        self,
        ip: str,
        port: int,
        rpc: Dict,
        retries: Optional[int] = None
    ) -> Optional[Dict]:
        """
        Send RPC with timeout and retries.

        Args:
            ip: Target IP
            port: Target port
            rpc: RPC message dict
            retries: Number of retries (default: config.max_retries)

        Returns:
            Response dict or None if timeout
        """
        if retries is None:
            retries = self.config.max_retries

        rpc_id = rpc["rpc_id"]

        for attempt in range(retries):
            try:
                # Create future for response
                future = asyncio.Future()
                self._pending_rpcs[rpc_id] = future

                # Send message
                await self._send_message(ip, port, rpc)
                self.stats["rpcs_sent"] += 1

                # Wait for response with timeout
                response = await asyncio.wait_for(
                    future,
                    timeout=self.config.timeout
                )

                logger.debug("RPC %s succeeded (attempt %d/%d)", rpc["type"], attempt + 1, retries)
                return response

            except asyncio.TimeoutError:
                logger.debug("RPC %s timeout (attempt %d/%d)", rpc["type"], attempt + 1, retries)
                self.stats["timeouts"] += 1

                # Clean up future
                if rpc_id in self._pending_rpcs:
                    self._pending_rpcs.pop(rpc_id, None)

                if attempt < retries - 1:
                    # Retry with exponential backoff
                    await asyncio.sleep(0.1 * (2 ** attempt))
            except Exception as e:
                logger.error("RPC %s error: %s", rpc["type"], e)
                self.stats["errors"] += 1

                # Clean up future
                if rpc_id in self._pending_rpcs:
                    self._pending_rpcs.pop(rpc_id, None)

                break

        logger.warning("RPC %s failed after %d attempts", rpc["type"], retries)
        return None

    async def _send_message(self, ip: str, port: int, message: Dict):
        """Send UDP message to address."""
        if not self.transport:
            raise RuntimeError("Transport not initialized")

        try:
            data = json.dumps(message).encode('utf-8')

            if len(data) > self.config.max_packet_size:
                logger.warning("Message too large (%d bytes), truncating", len(data))
                data = data[:self.config.max_packet_size]

            self.transport.sendto(data, (ip, port))

        except Exception as e:
            logger.error("Failed to send message to %s:%d: %s", ip, port, e)
            raise

    async def _send_response(self, addr: Tuple[str, int], response: Dict):
        """Send RPC response."""
        ip, port = addr
        await self._send_message(ip, port, response)

    def _check_rate_limit(self, ip: str) -> bool:
        """
        Check if IP is within rate limit.

        Args:
            ip: IP address to check

        Returns:
            True if allowed, False if rate limited
        """
        now = time.time()

        if ip in self._rate_limiter:
            count, reset_time = self._rate_limiter[ip]

            if now < reset_time:
                # Within rate limit window
                if count >= self.config.rate_limit_per_ip:
                    return False  # Rate limited

                # Increment count
                self._rate_limiter[ip] = (count + 1, reset_time)
            else:
                # Reset window
                self._rate_limiter[ip] = (1, now + 60)
        else:
            # First request from this IP
            self._rate_limiter[ip] = (1, now + 60)

        return True


class DHTProtocol(asyncio.DatagramProtocol):
    """UDP protocol handler for DHT RPC."""

    def __init__(self, rpc_handler: DHTRPCHandler):
        self.rpc_handler = rpc_handler
        super().__init__()

    def connection_made(self, transport):
        """Called when transport is ready."""
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        """Called when UDP packet is received."""
        logger.info("DHT: Received UDP packet from %s:%d (%d bytes)", addr[0], addr[1], len(data))
        # Handle RPC asynchronously
        asyncio.create_task(self.rpc_handler.handle_rpc(data, addr))

    def error_received(self, exc):
        """Called when send/receive operation raises exception."""
        logger.error("UDP protocol error: %s", exc)

    def connection_lost(self, exc):
        """Called when transport is closed."""
        if exc:
            logger.error("UDP connection lost: %s", exc)
