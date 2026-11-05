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

from .distance import parse_node_id
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
        config: Optional[RPCConfig] = None,
        is_blocked: Optional[Callable[[str], bool]] = None
    ):
        """
        Initialize RPC handler.

        Args:
            routing_table: Routing table for node lookups
            config: RPC configuration (optional)
            is_blocked: asked on every packet, so a block or an Unblock
                needs no restart; None means nobody is blocked
        """
        self.routing_table = routing_table
        self.config = config or RPCConfig()
        self._is_blocked = is_blocked

        # UDP transport
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.protocol: Optional['DHTProtocol'] = None

        # Pending RPC tracking (rpc_id → Future)
        self._pending_rpcs: Dict[str, asyncio.Future] = {}

        # Key-value storage (key → value, for STORE/FIND_VALUE)
        self._storage: Dict[str, str] = {}

        # Answers to our own requests that a block dropped (see _drop_blocked)
        self.blocked_answers = 0

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

    # ===== Block (blocked_nodes) =====

    def _blocked(self, node_id: Any) -> bool:
        """A node id in `blocked_nodes`. Never this node's own id, and False
        when the question fails. An id here is a claim: nothing in a packet
        proves it, so this stops a client that keeps its id, not one that lies."""
        if self._is_blocked is None or not isinstance(node_id, str):
            return False
        if node_id == self.routing_table.node_id:
            return False
        try:
            return self._is_blocked(node_id) is True
        except Exception:
            return False

    # Storage keys that carry a node id after a prefix.
    _KEY_PREFIXES = ("cert:", "relay:")

    @classmethod
    def _key_id(cls, key: Any) -> Any:
        """The node id a storage key names: `cert:<id>` and `relay:<id>` name <id>."""
        if isinstance(key, str):
            for prefix in cls._KEY_PREFIXES:
                if key.startswith(prefix):
                    return key[len(prefix):]
        return key

    def _blocked_key(self, key: Any) -> bool:
        """A key that is a blocked id, or `cert:` plus one."""
        return self._blocked(self._key_id(key))

    def without_blocked(self, nodes: List[DHTNode]) -> List[DHTNode]:
        """`nodes` minus every blocked id."""
        return [n for n in nodes if not self._blocked(n.node_id)]

    def _unblocked_closest(self, target: str) -> List[DHTNode]:
        """The 20 closest known nodes to hand out, a blocked id never among them.
        `target` may be a key (`cert:<id>`); any key that is not a node id (nor a
        prefix plus one) has no neighbours to give and is answered with an
        empty list, not silence."""
        target_id = self._key_id(target)
        try:
            parse_node_id(target_id)
        except (ValueError, AttributeError):
            return []
        pool = self.routing_table.find_closest_nodes(
            target_id, count=max(20, self.routing_table.get_node_count())
        )
        return self.without_blocked(pool)[:20]

    def forget(self, node_id: str) -> None:
        """Drop `node_id` from the table (and its replacement cache) and any
        value stored under it, its `cert:` too. Called when it is blocked."""
        self.routing_table.remove_node(node_id)
        for key in (node_id, *(prefix + node_id for prefix in self._KEY_PREFIXES)):
            self._storage.pop(key, None)

    def _drop_blocked(self, message: Dict, addr: Tuple[str, int]) -> None:
        """What the choke point does with a packet from a blocked id."""
        node_id = message.get("node_id")
        logger.debug("DHT: dropped %s from blocked node %s (%s:%d)",
                     message.get("type"), node_id, addr[0], addr[1])
        self.routing_table.remove_node(node_id)
        # Our own ping, answered by a blocked node: end the wait at once, as
        # an answer that does not count, and remember that it was a block.
        future = self._pending_rpcs.pop(message.get("rpc_id"), None)
        if future is not None and not future.done():
            self.blocked_answers += 1
            future.set_result({"type": "BLOCKED", "node_id": node_id})

    def purge_blocked(self) -> int:
        """Take every blocked id out of the table: a block edited in by hand or
        reloaded leaves entries `block_peer` never saw. Returns how many."""
        gone = [n.node_id for n in self.routing_table.get_all_nodes() if self._blocked(n.node_id)]
        for node_id in gone:
            self.routing_table.remove_node(node_id)
        return len(gone)

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
            # A node that answered is responsive whatever happens next. Letting
            # add_node's ValueError escape here made a seed that replied — and
            # replied with our own node id, because it was us — indistinguishable
            # from one that never answered at all.
            peer_id = response.get("node_id")
            if peer_id:
                try:
                    self.routing_table.add_node(peer_id, ip, port)
                except ValueError as e:
                    logger.info("PONG from %s:%d not recorded: %s", ip, port, e)
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
            return self.without_blocked(nodes)

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
        if self._blocked_key(key):
            return False

        rpc = {
            "type": "STORE",
            "rpc_id": str(uuid.uuid4()),
            "node_id": self.routing_table.node_id,
            "key": key,
            "value": value,
            "timestamp": time.time(),
        }

        logger.debug("DHT: Sending STORE to %s:%d (rpc_id=%s, key=%s, value=%s)",
                    ip, port, rpc["rpc_id"], key[:20] if len(key) > 20 else key, value)

        response = await self._send_rpc(ip, port, rpc)

        if response and response.get("type") == "STORED":
            logger.debug("DHT: STORE successful to %s:%d (rpc_id=%s)", ip, port, rpc["rpc_id"])
            return response.get("success", False)

        logger.warning("DHT: STORE failed to %s:%d (rpc_id=%s, response=%s)",
                      ip, port, rpc["rpc_id"], response)
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
        if self._blocked_key(key):
            return None

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
                return {"nodes": self.without_blocked(nodes)}

        return None

    async def discover_endpoint(
        self,
        ip: str,
        port: int
    ) -> Optional[Dict]:
        """
        Send DISCOVER_ENDPOINT RPC to node.

        Peer responds with our reflexive address (source IP:port they see).
        Used for NAT traversal endpoint discovery without STUN servers.

        Args:
            ip: Target IP address
            port: Target UDP port

        Returns:
            {"reflexive_ip": str, "reflexive_port": int} if successful, None otherwise
        """
        rpc = {
            "type": "DISCOVER_ENDPOINT",
            "rpc_id": str(uuid.uuid4()),
            "node_id": self.routing_table.node_id,
            "timestamp": time.time(),
        }

        response = await self._send_rpc(ip, port, rpc)

        if response and response.get("type") == "ENDPOINT_DISCOVERED":
            return {
                "reflexive_ip": response.get("reflexive_ip"),
                "reflexive_port": response.get("reflexive_port")
            }

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

        # One choke point, before the rate limit so a blocked sender spends no
        # budget of whoever shares its address: a request or a PONG naming a
        # blocked id gets no reply, changes nothing, and loses its table entry.
        # A bad packet is left to the rate limit and the error path below.
        try:
            message = json.loads(data.decode('utf-8'))
        except ValueError:
            message = None
        if isinstance(message, dict) and self._blocked(message.get("node_id")):
            self._drop_blocked(message, addr)
            return

        # Rate limiting
        if not self._check_rate_limit(sender_ip):
            logger.warning("Rate limit exceeded for %s", sender_ip)
            return

        self.stats["rpcs_received"] += 1

        try:
            # Deserialize message
            if message is None:
                message = json.loads(data.decode('utf-8'))
            rpc_type = message.get("type")
            rpc_id = message.get("rpc_id")

            logger.debug("DHT: Routing message type=%s from %s:%d (rpc_id=%s)",
                       rpc_type, sender_ip, sender_port, rpc_id)

            # Route to handler
            if rpc_type == "PING":
                await self._handle_ping(message, addr)
            elif rpc_type == "FIND_NODE":
                await self._handle_find_node(message, addr)
            elif rpc_type == "STORE":
                logger.debug("DHT: Dispatching STORE to handler (rpc_id=%s)", rpc_id)
                await self._handle_store(message, addr)
            elif rpc_type == "FIND_VALUE":
                await self._handle_find_value(message, addr)
            elif rpc_type == "DISCOVER_ENDPOINT":
                await self._handle_discover_endpoint(message, addr)
            elif rpc_type in ["PONG", "NODES_FOUND", "STORED", "VALUE_FOUND", "ENDPOINT_DISCOVERED"]:
                # Response to our RPC - resolve pending future
                logger.debug("DHT: Dispatching response %s to handler (rpc_id=%s)", rpc_type, rpc_id)
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

        logger.debug("DHT: Handling PING from %s (node_id=%s, rpc_id=%s)", addr, sender_id, rpc_id)

        # Update routing table
        if sender_id:
            try:
                self.routing_table.add_node(sender_id, sender_ip, sender_port)
                logger.debug("DHT: Added %s to routing table", sender_id[:20])
            except ValueError as e:
                # Our own packet came back to us — a seed pointing at this host,
                # or a NAT hairpin. A fact about the topology, not a failure.
                logger.info("DHT: PING from %s is this node itself: %s", addr, e)
            except Exception as e:
                logger.error("DHT: Failed to add node to routing table: %s", e)

        # Send PONG response
        response = {
            "type": "PONG",
            "rpc_id": rpc_id,
            "node_id": self.routing_table.node_id,
            "timestamp": time.time(),
        }

        logger.debug("DHT: Sending PONG to %s (rpc_id=%s)", addr, rpc_id)
        await self._send_response(addr, response)
        logger.debug("DHT: PONG sent to %s", addr)

    async def _handle_find_node(self, message: Dict, addr: Tuple[str, int]):
        """Handle FIND_NODE RPC (return k closest nodes)."""
        target_id = message.get("target_id")
        rpc_id = message.get("rpc_id")

        if not target_id:
            logger.warning("FIND_NODE missing target_id from %s", addr)
            return

        # Find k closest nodes
        closest = self._unblocked_closest(target_id)

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
        sender_id = message.get("node_id")

        logger.debug("DHT: Handling STORE from %s (node_id=%s, rpc_id=%s, key=%s, value=%s)",
                   addr, sender_id[:20] if sender_id else None, rpc_id,
                   key[:20] if key and len(key) > 20 else key, value)

        if not key or not value:
            logger.warning("STORE missing key/value from %s", addr)
            return

        # Not stored, and said so: the sender is not the blocked one.
        if self._blocked_key(key):
            logger.debug("DHT: refused STORE under blocked key %s", key)
            await self._send_response(addr, {
                "type": "STORED",
                "rpc_id": rpc_id,
                "success": False,
                "timestamp": time.time(),
            })
            return

        # Store key-value pair
        self._storage[key] = value
        logger.debug("DHT: Stored %s → %s (storage now has %d entries)",
                   key[:20] if len(key) > 20 else key, value, len(self._storage))

        # Send response
        response = {
            "type": "STORED",
            "rpc_id": rpc_id,
            "success": True,
            "timestamp": time.time(),
        }

        logger.debug("DHT: Sending STORED response to %s (rpc_id=%s)", addr, rpc_id)
        await self._send_response(addr, response)
        logger.debug("DHT: STORED response sent to %s", addr)

    async def _handle_find_value(self, message: Dict, addr: Tuple[str, int]):
        """Handle FIND_VALUE RPC (return value or k closest nodes)."""
        key = message.get("key")
        rpc_id = message.get("rpc_id")

        if not key:
            logger.warning("FIND_VALUE missing key from %s", addr)
            return

        # Check if we have the value
        if key in self._storage and not self._blocked_key(key):
            # Found - return value
            response = {
                "type": "VALUE_FOUND",
                "rpc_id": rpc_id,
                "value": self._storage[key],
                "timestamp": time.time(),
            }
        else:
            # Not found - return k closest nodes
            closest = self._unblocked_closest(key)
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

    async def _handle_discover_endpoint(self, message: Dict, addr: Tuple[str, int]):
        """
        Handle DISCOVER_ENDPOINT RPC (respond with sender's reflexive address).

        This enables DHT-based endpoint discovery for NAT traversal without STUN servers.
        The reflexive address is the source IP:port we see when receiving the request.

        Args:
            message: RPC message dict
            addr: (ip, port) tuple of sender
        """
        rpc_id = message.get("rpc_id")
        sender_ip, sender_port = addr  # Reflexive address (what we see)

        logger.debug("DHT: Handling DISCOVER_ENDPOINT from %s:%d (rpc_id=%s)",
                   sender_ip, sender_port, rpc_id)

        # Respond with reflexive address
        response = {
            "type": "ENDPOINT_DISCOVERED",
            "rpc_id": rpc_id,
            "reflexive_ip": sender_ip,
            "reflexive_port": sender_port,
            "timestamp": time.time(),
        }

        logger.debug("DHT: Sending ENDPOINT_DISCOVERED to %s:%d (reflexive=%s:%d)",
                    sender_ip, sender_port, sender_ip, sender_port)
        await self._send_response(addr, response)

    async def _handle_response(self, rpc_id: str, message: Dict):
        """Handle RPC response (resolve pending future)."""
        msg_type = message.get("type")
        logger.debug("DHT: Handling response %s (rpc_id=%s, pending=%s)",
                   msg_type, rpc_id, rpc_id in self._pending_rpcs)

        if rpc_id in self._pending_rpcs:
            future = self._pending_rpcs.pop(rpc_id)
            if not future.done():
                future.set_result(message)
                logger.debug("DHT: Response %s resolved for rpc_id=%s", msg_type, rpc_id)
            else:
                logger.warning("DHT: Future already done for rpc_id=%s", rpc_id)
        else:
            logger.warning("DHT: No pending RPC for rpc_id=%s (msg_type=%s)", rpc_id, msg_type)

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

            msg_type = message.get("type")
            rpc_id = message.get("rpc_id")
            logger.debug("DHT: Sending UDP packet to %s:%d (type=%s, rpc_id=%s, %d bytes)",
                       ip, port, msg_type, rpc_id, len(data))

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
        logger.debug("DHT: Received UDP packet from %s:%d (%d bytes)", addr[0], addr[1], len(data))
        # Handle RPC asynchronously
        asyncio.create_task(self.rpc_handler.handle_rpc(data, addr))

    def error_received(self, exc):
        """Called when send/receive operation raises exception."""
        logger.error("UDP protocol error: %s", exc)

    def connection_lost(self, exc):
        """Called when transport is closed."""
        if exc:
            logger.error("UDP connection lost: %s", exc)
