"""
Gossip Manager - Store-and-Forward Messaging (Phase 6 Week 4)

Implements epidemic gossip protocol for disaster-resilient messaging.
Used as last-resort fallback when all direct methods fail (Priorities 1-5).

Key Features:
- Multi-hop message routing (epidemic spreading)
- Eventual delivery (not real-time)
- Vector clocks for causality tracking
- Anti-entropy sync for reconciliation
- Message deduplication
- TTL and hop limits

Use Cases:
- Offline messaging (peer receives when online)
- Disaster scenarios (infrastructure outages)
- Knowledge commit sync (anti-entropy)
- Censorship resistance

Success Rate:
- Eventual delivery (if peers eventually online)
- Not real-time (high latency, multiple hops)
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Set, TYPE_CHECKING
from collections import defaultdict

from ..models.gossip_message import GossipMessage
from ..models.vector_clock import VectorClock

if TYPE_CHECKING:
    from ..p2p_manager import P2PManager

logger = logging.getLogger(__name__)


class GossipManager:
    """
    Manages gossip protocol for store-and-forward messaging.

    Epidemic Algorithm:
    1. Node receives message M
    2. If M.destination == self → deliver to user
    3. If seen before → ignore (deduplication)
    4. If TTL expired or hops >= max → drop
    5. Else → forward to N random peers (fanout=3)

    Anti-Entropy:
    - Periodically sync vector clocks with peers
    - Request missing messages
    - Send messages peer is missing

    Attributes:
        p2p_manager: P2P manager for peer connections
        node_id: This node's identifier
        fanout: Number of peers to forward to (default 3)
        max_hops: Maximum hops allowed (default 5)
        ttl_seconds: Message TTL (default 24 hours)
        sync_interval: Anti-entropy sync interval (default 5 minutes)

    Example:
        >>> manager = GossipManager(p2p_manager, node_id)
        >>> await manager.start()
        >>>
        >>> # Send gossip message
        >>> msg_id = await manager.send_gossip("dpc-node-bob", {"text": "Hello"})
        >>>
        >>> # Receive gossip messages
        >>> # (handled automatically in background)
    """

    def __init__(
        self,
        p2p_manager: "P2PManager",
        node_id: str,
        message_router: Optional["MessageRouter"] = None,
        fanout: int = 3,
        max_hops: int = 5,
        ttl_seconds: int = 86400,  # 24 hours
        sync_interval: float = 300.0  # 5 minutes
    ):
        """
        Initialize gossip manager.

        Args:
            p2p_manager: P2P manager for peer connections
            node_id: This node's identifier
            message_router: Message router for delivering decrypted messages (optional)
            fanout: Number of peers to forward to
            max_hops: Maximum hops allowed
            ttl_seconds: Message TTL
            sync_interval: Anti-entropy sync interval (seconds)
        """
        self.p2p_manager = p2p_manager
        self.node_id = node_id
        self.message_router = message_router
        self.fanout = fanout
        self.max_hops = max_hops
        self.ttl_seconds = ttl_seconds
        self.sync_interval = sync_interval

        # Message storage
        self.messages: Dict[str, GossipMessage] = {}  # msg_id -> GossipMessage
        self.seen_messages: Set[str] = set()  # msg_id (for deduplication)

        # Vector clock for causality tracking
        self.vector_clock = VectorClock(node_id)

        # Delivery callbacks for GossipConnection instances
        self.delivery_callbacks: Dict[str, callable] = {}

        # Background tasks
        self._running = False
        self._sync_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

        # Statistics
        self.stats = {
            "messages_sent": 0,
            "messages_received": 0,
            "messages_forwarded": 0,
            "messages_delivered": 0,
            "messages_dropped": 0,
            "sync_cycles": 0,
        }

        logger.info(
            "GossipManager initialized (fanout=%d, max_hops=%d, ttl=%ds)",
            fanout, max_hops, ttl_seconds
        )

    async def start(self):
        """Start gossip manager and background tasks."""
        if self._running:
            logger.warning("GossipManager already running")
            return

        self._running = True

        # Publish local certificate to DHT for peer discovery
        await self._publish_certificate_to_dht()

        # Start anti-entropy sync loop
        self._sync_task = asyncio.create_task(self._anti_entropy_loop())

        # Start cleanup task (remove expired messages)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        logger.info("GossipManager started")

    async def stop(self):
        """Stop gossip manager and background tasks."""
        if not self._running:
            return

        self._running = False

        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        logger.info("GossipManager stopped")

    def register_delivery_callback(self, peer_id: str, callback: callable):
        """
        Register callback for when messages arrive from specific peer.

        Args:
            peer_id: Source peer node ID
            callback: Async function to call when message arrives

        Note:
            Used by GossipConnection to receive messages.
        """
        self.delivery_callbacks[peer_id] = callback
        logger.debug(f"Registered delivery callback for peer {peer_id[:20]}")

    def unregister_delivery_callback(self, peer_id: str):
        """
        Unregister delivery callback.

        Args:
            peer_id: Source peer node ID
        """
        if peer_id in self.delivery_callbacks:
            del self.delivery_callbacks[peer_id]
            logger.debug(f"Unregistered delivery callback for peer {peer_id[:20]}")

    async def _encrypt_payload(self, payload: Dict, destination_node_id: str) -> str:
        """
        Encrypt payload with recipient's public key (hybrid encryption).

        Returns base64-encoded encrypted blob.
        Only sender and recipient can decrypt.

        Args:
            payload: Message payload to encrypt
            destination_node_id: Recipient's node ID

        Returns:
            Base64-encoded encrypted string

        Raises:
            ValueError: If recipient's certificate cannot be found
            Exception: If encryption fails

        Note:
            Uses hybrid encryption (AES-GCM + RSA-OAEP) for unlimited payload sizes.
            Provides both encryption and authentication (GCM mode).
        """
        import json
        import base64
        from dpc_protocol.crypto import encrypt_with_public_key_hybrid

        try:
            # Get recipient's public key from DHT or peer cache
            recipient_cert = await self._get_peer_certificate(destination_node_id)
            if not recipient_cert:
                raise ValueError(f"Cannot find certificate for {destination_node_id}")

            # Serialize payload to JSON
            payload_json = json.dumps(payload)
            logger.debug(f"Encrypting payload ({len(payload_json)} bytes) for {destination_node_id[:20]}...")

            # Encrypt with hybrid encryption (AES-GCM + RSA-OAEP)
            encrypted_bytes = encrypt_with_public_key_hybrid(
                payload_json.encode('utf-8'),
                recipient_cert.public_key()
            )

            # Return as base64 for JSON serialization
            encrypted_b64 = base64.b64encode(encrypted_bytes).decode('utf-8')
            logger.debug(f"Encrypted gossip payload for {destination_node_id[:20]}... ({len(encrypted_b64)} chars base64)")
            return encrypted_b64

        except Exception as e:
            logger.error(f"Failed to encrypt gossip payload: {e}", exc_info=True)
            raise

    async def _decrypt_payload(self, encrypted_payload: str) -> Dict:
        """
        Decrypt payload with own private key (hybrid decryption).

        Returns original payload dict.

        Args:
            encrypted_payload: Base64-encoded encrypted string

        Returns:
            Decrypted payload dictionary

        Raises:
            Exception: If decryption fails (wrong key, corrupted data, authentication failure)

        Note:
            Uses hybrid decryption (AES-GCM + RSA-OAEP) for unlimited payload sizes.
            GCM mode provides authentication - decryption fails if data was tampered with.
        """
        import json
        import base64
        from pathlib import Path
        import os
        from cryptography.hazmat.primitives import serialization
        from dpc_protocol.crypto import decrypt_with_private_key_hybrid

        try:
            # Decode from base64
            encrypted_bytes = base64.b64decode(encrypted_payload.encode('utf-8'))
            logger.debug(f"Decrypting payload ({len(encrypted_bytes)} bytes)")

            # Load own private key
            dpc_dir = Path(os.getenv("DPC_DIR", Path.home() / ".dpc"))
            key_path = dpc_dir / "node.key"

            with open(key_path, "rb") as f:
                private_key = serialization.load_pem_private_key(
                    f.read(),
                    password=None
                )

            # Decrypt with hybrid decryption (AES-GCM + RSA-OAEP)
            decrypted_bytes = decrypt_with_private_key_hybrid(encrypted_bytes, private_key)

            # Parse JSON
            payload_json = decrypted_bytes.decode('utf-8')
            payload = json.loads(payload_json)
            logger.debug(f"Successfully decrypted gossip payload ({len(payload_json)} bytes, authenticated)")
            return payload

        except Exception as e:
            logger.error(f"Failed to decrypt gossip payload: {e}", exc_info=True)
            raise

    async def _publish_certificate_to_dht(self):
        """
        Publish local certificate to DHT for peer discovery.

        Stores certificate in PEM format under key: "cert:<node_id>"
        This allows peers to find our public key for E2E encryption.

        Note:
            Called on startup to ensure certificate is available in DHT.
        """
        dht_manager = getattr(self.p2p_manager, 'dht_manager', None)
        if not dht_manager:
            logger.warning("DHT manager not available, cannot publish certificate")
            return

        try:
            # Load local certificate
            from pathlib import Path
            import os
            from cryptography.hazmat.primitives import serialization

            dpc_dir = Path(os.getenv("DPC_DIR", Path.home() / ".dpc"))
            cert_path = dpc_dir / "node.crt"

            if not cert_path.exists():
                logger.warning(f"Certificate not found at {cert_path}")
                return

            # Read certificate PEM
            with open(cert_path, "rb") as f:
                cert_pem = f.read().decode('utf-8')

            # Find k closest nodes to self
            closest_nodes = await dht_manager.find_node(self.node_id)

            if not closest_nodes:
                logger.warning("No DHT nodes found to store certificate")
                return

            # Store certificate on k nodes with key "cert:<node_id>"
            cert_key = f"cert:{self.node_id}"
            store_tasks = [
                dht_manager.rpc_handler.store(node.ip, node.port, cert_key, cert_pem)
                for node in closest_nodes
                if node.node_id != self.node_id  # Don't store on self
            ]

            results = await asyncio.gather(*store_tasks, return_exceptions=True)
            success_count = sum(1 for r in results if r is True)

            logger.info(
                f"Published certificate to DHT: {success_count}/{len(store_tasks)} nodes"
            )

        except Exception as e:
            logger.error(f"Failed to publish certificate to DHT: {e}", exc_info=True)

    async def _get_peer_certificate(self, node_id: str):
        """
        Retrieve peer's certificate from cache or DHT.

        Returns X.509 certificate with public key.

        Args:
            node_id: Peer's node ID

        Returns:
            X.509 certificate or None if not found

        Note:
            First checks peer cache, then active connection, finally queries DHT.
        """
        # Check peer cache first
        if hasattr(self.p2p_manager, 'peer_cache') and self.p2p_manager.peer_cache:
            peer = self.p2p_manager.peer_cache.get_peer(node_id)
            if peer and hasattr(peer, 'certificate'):
                logger.debug(f"Found certificate for {node_id[:20]} in peer cache")
                return peer.certificate

        # Try to get from active connection
        if hasattr(self.p2p_manager, 'peers') and node_id in self.p2p_manager.peers:
            conn = self.p2p_manager.peers[node_id]
            if hasattr(conn, 'peer_cert'):
                logger.debug(f"Found certificate for {node_id[:20]} from active connection")
                return conn.peer_cert

        # Query DHT for certificate
        logger.debug(f"Certificate not in cache, querying DHT for {node_id[:20]}")
        return await self._query_dht_for_certificate(node_id)

    async def _query_dht_for_certificate(self, node_id: str):
        """
        Query DHT for peer's certificate.

        Args:
            node_id: Peer's node ID

        Returns:
            X.509 certificate or None if not found

        Note:
            Queries k closest nodes to target for key "cert:<node_id>".
        """
        dht_manager = getattr(self.p2p_manager, 'dht_manager', None)
        if not dht_manager:
            logger.debug("DHT manager not available")
            return None

        try:
            # Find k closest nodes to target
            closest_nodes = await dht_manager.find_node(node_id)

            if not closest_nodes:
                logger.debug(f"No DHT nodes found to query for {node_id[:20]}")
                return None

            # Query each node for certificate
            cert_key = f"cert:{node_id}"

            for dht_node in closest_nodes:
                try:
                    result = await dht_manager.rpc_handler.find_value(
                        dht_node.ip, dht_node.port, cert_key
                    )

                    if result and "value" in result:
                        # Found certificate PEM
                        cert_pem = result["value"]

                        # Load certificate from PEM
                        from cryptography import x509
                        from cryptography.hazmat.primitives import serialization

                        cert = x509.load_pem_x509_certificate(cert_pem.encode('utf-8'))

                        logger.info(
                            f"Retrieved certificate for {node_id[:20]} from DHT "
                            f"(via {dht_node.node_id[:20]})"
                        )

                        return cert

                except Exception as e:
                    logger.debug(
                        f"Failed to query DHT node {dht_node.node_id[:20]} "
                        f"for certificate: {e}"
                    )
                    continue

            logger.warning(f"Certificate for {node_id[:20]} not found in DHT")
            return None

        except Exception as e:
            logger.error(f"Error querying DHT for certificate: {e}", exc_info=True)
            return None

    async def send_gossip(
        self,
        destination: str,
        payload: Dict,
        priority: str = "normal"
    ) -> str:
        """
        Send message via gossip protocol (with end-to-end encryption).

        Creates new gossip message and begins epidemic spreading.
        Payload is encrypted with recipient's public key for privacy.

        Args:
            destination: Destination node ID
            payload: Message payload (will be encrypted)
            priority: Message priority ("normal", "high", "low")

        Returns:
            Message ID

        Raises:
            ValueError: If recipient's certificate cannot be found
            Exception: If encryption fails

        Example:
            >>> msg_id = await manager.send_gossip(
            ...     "dpc-node-bob",
            ...     {"command": "HELLO"},
            ...     priority="high"
            ... )

        Note:
            Intermediate hops cannot decrypt the payload (end-to-end encryption).
        """
        # Increment vector clock
        self.vector_clock.increment()

        # ENCRYPT PAYLOAD before creating gossip message
        try:
            encrypted_payload = await self._encrypt_payload(payload, destination)
            logger.debug(f"Encrypted gossip payload for {destination}")
        except Exception as e:
            logger.error(f"Cannot encrypt gossip payload: {e}")
            raise

        # Create gossip message with encrypted payload
        msg = GossipMessage.create(
            source=self.node_id,
            destination=destination,
            payload={"encrypted": encrypted_payload},  # Encrypted blob (E2E)
            max_hops=self.max_hops,
            ttl=self.ttl_seconds,
            priority=priority,
            vector_clock=self.vector_clock.to_dict()
        )

        # Store message
        self.messages[msg.id] = msg
        self.seen_messages.add(msg.id)

        self.stats["messages_sent"] += 1

        logger.info(
            "Created gossip message: %s (dst=%s, priority=%s)",
            msg.id, destination[:20], priority
        )

        # Begin forwarding
        await self._forward_message(msg)

        return msg.id

    async def handle_gossip_message(self, msg: GossipMessage):
        """
        Handle incoming gossip message.

        Algorithm:
        1. Check if destination is us → deliver
        2. Check if seen before → ignore (deduplication)
        3. Check TTL/hops → drop if exceeded
        4. Store and forward to N random peers

        Args:
            msg: Gossip message received

        Example:
            >>> await manager.handle_gossip_message(gossip_msg)
        """
        logger.debug(
            "Received gossip message: %s (src=%s, dst=%s, hops=%d/%d)",
            msg.id, msg.source[:20], msg.destination[:20], msg.hops, msg.max_hops
        )

        self.stats["messages_received"] += 1

        # Step 1: Deliver if destination is us
        if msg.destination == self.node_id:
            await self._deliver_message(msg)
            return

        # Step 2: Deduplication (seen before?)
        if msg.id in self.seen_messages:
            logger.debug("Message %s already seen - ignoring", msg.id)
            return

        # Step 3: Check TTL and hops
        if msg.is_expired():
            logger.debug("Message %s expired - dropping", msg.id)
            self.stats["messages_dropped"] += 1
            return

        if not msg.can_forward():
            logger.debug("Message %s reached max hops - dropping", msg.id)
            self.stats["messages_dropped"] += 1
            return

        # Step 4: Store and forward
        self.messages[msg.id] = msg
        self.seen_messages.add(msg.id)

        # Merge vector clock
        other_clock = VectorClock.from_dict(self.node_id, msg.vector_clock)
        self.vector_clock.merge(other_clock)

        # Forward to peers
        await self._forward_message(msg)

    async def _forward_message(self, msg: GossipMessage):
        """
        Forward message to N random peers (epidemic spreading).

        Algorithm:
        1. Get list of connected peers
        2. Exclude peers in already_forwarded
        3. Select N random peers (fanout)
        4. Send GOSSIP_MESSAGE to each

        Args:
            msg: Message to forward
        """
        if not self.p2p_manager:
            logger.warning("No P2P manager - cannot forward gossip")
            return

        # Get connected peers
        peers = self.p2p_manager.get_connected_peers()

        if not peers:
            logger.debug("No connected peers - cannot forward message %s", msg.id)
            return

        # Exclude peers that already saw this message
        eligible_peers = [
            p for p in peers
            if not msg.already_seen_by(p.node_id)
        ]

        if not eligible_peers:
            logger.debug("No eligible peers for message %s", msg.id)
            return

        # Select N random peers (fanout)
        import random
        forward_to = random.sample(eligible_peers, min(self.fanout, len(eligible_peers)))

        # Increment hops and add us to forwarding history
        msg.increment_hops(self.node_id)

        # Send to selected peers
        for peer in forward_to:
            try:
                await peer.send({
                    "command": "GOSSIP_MESSAGE",
                    "payload": msg.to_dict()
                })

                self.stats["messages_forwarded"] += 1

                logger.debug(
                    "Forwarded message %s to %s (hops=%d)",
                    msg.id, peer.node_id[:20], msg.hops
                )

            except Exception as e:
                logger.debug("Failed to forward message %s to %s: %s", msg.id, peer.node_id[:20], e)

    async def _deliver_message(self, msg: GossipMessage):
        """
        Deliver message to local application (with decryption).

        Args:
            msg: Message for this node (with encrypted payload)

        Note:
            Decrypts payload before delivering to callback.
            Only the intended recipient can decrypt (end-to-end encryption).
        """
        logger.info(
            "Delivering gossip message %s from %s (hops: %d)",
            msg.id, msg.source[:20], msg.hops
        )

        self.stats["messages_delivered"] += 1
        self.seen_messages.add(msg.id)

        # DECRYPT PAYLOAD before delivering
        try:
            encrypted_blob = msg.payload.get("encrypted")
            if not encrypted_blob:
                logger.error(f"Gossip message {msg.id} missing encrypted payload")
                return

            decrypted_payload = await self._decrypt_payload(encrypted_blob)
            logger.debug(f"Decrypted gossip payload from {msg.source[:20]}")

        except Exception as e:
            logger.error(f"Failed to decrypt gossip message: {e}", exc_info=True)
            return

        # Notify GossipConnection if callback registered
        source_peer = msg.source
        if hasattr(self, 'delivery_callbacks') and source_peer in self.delivery_callbacks:
            try:
                callback = self.delivery_callbacks[source_peer]
                await callback(decrypted_payload)  # Send decrypted payload
                logger.debug(f"Notified callback for peer {source_peer[:20]}")
            except Exception as e:
                logger.error(f"Error in delivery callback: {e}", exc_info=True)
        else:
            # Route message through message router (if available)
            if self.message_router:
                try:
                    await self.message_router.route_message(source_peer, decrypted_payload)
                    logger.debug(f"Routed gossip message from {source_peer[:20]} to message router")
                except Exception as e:
                    logger.error(f"Error routing gossip message: {e}", exc_info=True)
            else:
                # No callback and no message router - just log
                logger.info(f"No callback or router for {source_peer[:20]}, payload: {decrypted_payload}")

    async def _anti_entropy_loop(self):
        """
        Periodic anti-entropy sync with random peer.

        Algorithm:
        1. Select random connected peer
        2. Exchange vector clocks
        3. Request missing messages
        4. Send messages peer is missing
        """
        logger.info("Anti-entropy sync loop started (interval=%.0fs)", self.sync_interval)

        while self._running:
            try:
                await asyncio.sleep(self.sync_interval)

                if not self.p2p_manager:
                    continue

                peers = self.p2p_manager.get_connected_peers()
                if not peers:
                    continue

                # Select random peer
                import random
                peer = random.choice(peers)

                logger.debug("Anti-entropy sync with %s", peer.node_id[:20])

                # Send our vector clock
                await peer.send({
                    "command": "GOSSIP_SYNC",
                    "payload": {
                        "vector_clock": self.vector_clock.to_dict(),
                        "message_ids": list(self.messages.keys())
                    }
                })

                self.stats["sync_cycles"] += 1

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Anti-entropy sync error: %s", e)

    async def handle_gossip_sync(self, peer_id: str, peer_clock_dict: Dict[str, int], peer_message_ids: List[str]):
        """
        Handle anti-entropy sync request from peer.

        Args:
            peer_id: Peer node ID
            peer_clock_dict: Peer's vector clock
            peer_message_ids: Message IDs peer has

        Algorithm:
        1. Compare vector clocks
        2. Find messages we have that peer doesn't
        3. Send missing messages
        """
        logger.debug("Handling GOSSIP_SYNC from %s", peer_id[:20])

        # Find messages we have that peer doesn't
        our_message_ids = set(self.messages.keys())
        peer_message_ids_set = set(peer_message_ids)
        missing_ids = our_message_ids - peer_message_ids_set

        if missing_ids:
            logger.info(f"Sending {len(missing_ids)} missing messages to {peer_id[:20]}")

            # Look up connection to peer
            if not hasattr(self.p2p_manager, 'peers') or peer_id not in self.p2p_manager.peers:
                logger.warning(f"No connection to {peer_id[:20]}, cannot send missing messages")
                return

            connection = self.p2p_manager.peers[peer_id]

            # Send each missing message
            for msg_id in missing_ids:
                if msg_id in self.messages:
                    message = self.messages[msg_id]
                    try:
                        # Re-gossip the message (will use existing routing)
                        await connection.send({
                            "command": "GOSSIP_MESSAGE",
                            "payload": {
                                "gossip_message": message.to_dict()
                            }
                        })
                        logger.debug(f"Sent missing message {msg_id[:8]}... to {peer_id[:20]}")
                    except Exception as e:
                        logger.error(f"Failed to send missing message {msg_id}: {e}")

    async def _cleanup_loop(self):
        """Remove expired messages periodically."""
        logger.info("Cleanup loop started (interval=300s)")

        while self._running:
            try:
                await asyncio.sleep(300)  # 5 minutes

                expired_ids = [
                    msg_id for msg_id, msg in self.messages.items()
                    if msg.is_expired()
                ]

                for msg_id in expired_ids:
                    del self.messages[msg_id]
                    logger.debug("Cleaned up expired message %s", msg_id)

                if expired_ids:
                    logger.info("Cleaned up %d expired messages", len(expired_ids))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Cleanup loop error: %s", e)

    def get_stats(self) -> Dict:
        """Get gossip manager statistics."""
        return {
            **self.stats,
            "messages_stored": len(self.messages),
            "messages_seen": len(self.seen_messages),
            "vector_clock": self.vector_clock.to_dict(),
        }
