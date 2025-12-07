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
            fanout: Number of peers to forward to
            max_hops: Maximum hops allowed
            ttl_seconds: Message TTL
            sync_interval: Anti-entropy sync interval (seconds)
        """
        self.p2p_manager = p2p_manager
        self.node_id = node_id
        self.fanout = fanout
        self.max_hops = max_hops
        self.ttl_seconds = ttl_seconds
        self.sync_interval = sync_interval

        # Message storage
        self.messages: Dict[str, GossipMessage] = {}  # msg_id -> GossipMessage
        self.seen_messages: Set[str] = set()  # msg_id (for deduplication)

        # Vector clock for causality tracking
        self.vector_clock = VectorClock(node_id)

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

    async def send_gossip(
        self,
        destination: str,
        payload: Dict,
        priority: str = "normal"
    ) -> str:
        """
        Send message via gossip protocol.

        Creates new gossip message and begins epidemic spreading.

        Args:
            destination: Destination node ID
            payload: Message payload
            priority: Message priority ("normal", "high", "low")

        Returns:
            Message ID

        Example:
            >>> msg_id = await manager.send_gossip(
            ...     "dpc-node-bob",
            ...     {"command": "HELLO"},
            ...     priority="high"
            ... )
        """
        # Increment vector clock
        self.vector_clock.increment()

        # Create gossip message
        msg = GossipMessage.create(
            source=self.node_id,
            destination=destination,
            payload=payload,
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
                await peer.send_message({
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
        Deliver message to local application.

        Args:
            msg: Message for this node
        """
        logger.info(
            "Delivering gossip message %s from %s",
            msg.id, msg.source[:20]
        )

        # TODO: Integrate with message delivery system
        # For now, just log and update stats

        self.stats["messages_delivered"] += 1
        self.seen_messages.add(msg.id)

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
                await peer.send_message({
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
            logger.debug("Sending %d missing messages to %s", len(missing_ids), peer_id[:20])

            # TODO: Send missing messages to peer
            # (Requires peer connection lookup)

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
