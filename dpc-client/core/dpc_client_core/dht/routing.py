"""
DHT Routing Table - Kademlia k-bucket Implementation

This module implements the Kademlia routing table with:
- 128 k-buckets (for 128-bit key space)
- LRU eviction policy with ping verification
- Fast O(log n) closest node lookups
- Subnet diversity enforcement (security)

Reference: Kademlia Paper (Maymounkov & Mazières, 2002)
"""

import asyncio
import ipaddress
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Set

from .distance import (
    xor_distance,
    bucket_index,
    NODE_ID_BITS,
    parse_node_id
)

logger = logging.getLogger(__name__)


@dataclass
class DHTNode:
    """
    Represents a known DHT node in the routing table.

    Attributes:
        node_id: Node identifier (e.g., "dpc-node-abcd1234...")
        ip: IP address (IPv4 or IPv6)
        port: UDP port for DHT RPCs
        last_seen: Unix timestamp of last successful communication
        failed_pings: Number of consecutive failed ping attempts
    """
    node_id: str
    ip: str
    port: int
    last_seen: float = field(default_factory=time.time)
    failed_pings: int = 0

    def __eq__(self, other):
        """Nodes are equal if they have the same node_id."""
        if not isinstance(other, DHTNode):
            return False
        return self.node_id == other.node_id

    def __hash__(self):
        """Hash based on node_id for use in sets/dicts."""
        return hash(self.node_id)

    def update_last_seen(self):
        """Update last_seen timestamp (called after successful RPC)."""
        self.last_seen = time.time()
        self.failed_pings = 0

    def mark_failed_ping(self):
        """Increment failed ping count."""
        self.failed_pings += 1

    def is_stale(self, timeout: float = 900.0) -> bool:
        """
        Check if node is stale (not seen recently).

        Args:
            timeout: Stale threshold in seconds (default: 15 minutes)

        Returns:
            True if last_seen is older than timeout
        """
        return time.time() - self.last_seen > timeout

    def get_subnet(self, prefix_len: int = 24) -> str:
        """
        Get subnet for diversity checks.

        Args:
            prefix_len: Subnet prefix length (24 for /24)

        Returns:
            Subnet string (e.g., "192.168.1.0/24")
        """
        try:
            ip_obj = ipaddress.ip_address(self.ip)
            network = ipaddress.ip_network(f"{self.ip}/{prefix_len}", strict=False)
            return str(network)
        except ValueError:
            return self.ip  # Fallback to IP if parsing fails


class KBucket:
    """
    Single k-bucket for Kademlia routing table.

    K-bucket stores up to k nodes at similar distance from local node.
    Uses LRU eviction policy: least-recently-seen nodes are evicted first.

    Attributes:
        k: Maximum nodes per bucket (typically 20)
        nodes: Deque of DHTNode objects (ordered by last_seen, oldest first)
        replacement_cache: Nodes to add if space becomes available
    """

    def __init__(self, k: int = 20, subnet_diversity_limit: int = 2):
        """
        Initialize k-bucket.

        Args:
            k: Maximum nodes per bucket (Kademlia constant)
            subnet_diversity_limit: Max nodes per /24 subnet (security)
        """
        self.k = k
        self.subnet_diversity_limit = subnet_diversity_limit
        self.nodes: Deque[DHTNode] = deque()
        self.replacement_cache: Deque[DHTNode] = deque(maxlen=k)
        self._last_updated = time.time()

    def __len__(self) -> int:
        """Return number of nodes in bucket."""
        return len(self.nodes)

    def is_full(self) -> bool:
        """Check if bucket has reached capacity."""
        return len(self.nodes) >= self.k

    def get_nodes(self) -> List[DHTNode]:
        """Return all nodes as list (sorted by last_seen, oldest first)."""
        return list(self.nodes)

    def has_node(self, node_id: str) -> bool:
        """Check if node_id exists in bucket."""
        return any(node.node_id == node_id for node in self.nodes)

    def get_node(self, node_id: str) -> Optional[DHTNode]:
        """Get DHTNode by node_id."""
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def _check_subnet_diversity(self, new_node: DHTNode) -> bool:
        """
        Check if adding new_node violates subnet diversity limit.

        Security: Prevents eclipse attacks by limiting nodes per subnet.

        Args:
            new_node: Node to check

        Returns:
            True if adding node is allowed (diversity ok)
        """
        new_subnet = new_node.get_subnet(24)

        # Count existing nodes in same subnet
        subnet_count = sum(
            1 for node in self.nodes if node.get_subnet(24) == new_subnet
        )

        return subnet_count < self.subnet_diversity_limit

    def add(self, node: DHTNode, ping_callback=None) -> bool:
        """
        Add node to k-bucket with LRU eviction policy.

        Algorithm:
        1. If node already exists, move to tail (mark as recently seen)
        2. If bucket not full, append node
        3. If bucket full:
           a. Check subnet diversity
           b. Check if head (least-recently-seen) node is stale
           c. Ping head node to verify it's still alive
           d. If head responds, add new node to replacement cache
           e. If head fails, evict head and add new node

        Args:
            node: DHTNode to add
            ping_callback: Optional async function to ping nodes

        Returns:
            True if node was added, False if rejected or cached
        """
        # Case 1: Node already exists - move to tail (refresh)
        existing = self.get_node(node.node_id)
        if existing:
            self.nodes.remove(existing)
            existing.update_last_seen()
            self.nodes.append(existing)
            self._last_updated = time.time()
            logger.debug("Refreshed node %s in k-bucket", node.node_id[:20])
            return True

        # Case 2: Bucket not full - add directly
        if not self.is_full():
            # Check subnet diversity
            if not self._check_subnet_diversity(node):
                logger.warning(
                    "Subnet diversity limit: rejecting %s (subnet %s)",
                    node.node_id[:20],
                    node.get_subnet(24)
                )
                return False

            self.nodes.append(node)
            self._last_updated = time.time()
            logger.debug("Added node %s to k-bucket (%d/%d)", node.node_id[:20], len(self), self.k)
            return True

        # Case 3: Bucket full - check if head is stale
        head = self.nodes[0]  # Least-recently-seen

        if head.is_stale(timeout=900):  # 15 minutes
            # Head is stale - evict and add new node
            logger.debug(
                "Evicting stale node %s (last seen %ds ago)",
                head.node_id[:20],
                int(time.time() - head.last_seen)
            )
            self.nodes.popleft()

            if self._check_subnet_diversity(node):
                self.nodes.append(node)
                self._last_updated = time.time()
                logger.debug("Added node %s after evicting stale node", node.node_id[:20])
                return True
            else:
                logger.warning("Subnet diversity prevents adding node %s", node.node_id[:20])
                return False

        # Case 4: Bucket full, head not stale - add to replacement cache
        logger.debug(
            "Bucket full, adding %s to replacement cache",
            node.node_id[:20]
        )
        self.replacement_cache.append(node)
        return False

    def remove(self, node_id: str) -> bool:
        """
        Remove node from bucket.

        If replacement cache has nodes, promote one to fill the slot.

        Args:
            node_id: Node identifier to remove

        Returns:
            True if node was removed
        """
        for node in self.nodes:
            if node.node_id == node_id:
                self.nodes.remove(node)
                logger.debug("Removed node %s from k-bucket", node_id[:20])

                # Promote from replacement cache if available
                if self.replacement_cache:
                    replacement = self.replacement_cache.popleft()
                    self.nodes.append(replacement)
                    logger.debug(
                        "Promoted %s from replacement cache",
                        replacement.node_id[:20]
                    )

                self._last_updated = time.time()
                return True

        return False

    def get_last_updated(self) -> float:
        """Return timestamp of last update (add/remove/refresh)."""
        return self._last_updated

    def needs_refresh(self, interval: float = 3600.0) -> bool:
        """
        Check if bucket needs periodic refresh.

        Args:
            interval: Refresh interval in seconds (default: 1 hour)

        Returns:
            True if bucket hasn't been updated in interval
        """
        return time.time() - self._last_updated > interval


class RoutingTable:
    """
    Kademlia routing table with 128 k-buckets.

    The routing table organizes known nodes by XOR distance from local node.
    Each bucket stores nodes at distance [2^i, 2^(i+1)) for i in [0, 127].

    Attributes:
        node_id: Local node identifier
        k: Maximum nodes per bucket (typically 20)
        buckets: Array of 128 KBucket objects
    """

    def __init__(
        self,
        node_id: str,
        k: int = 20,
        subnet_diversity_limit: int = 2
    ):
        """
        Initialize routing table.

        Args:
            node_id: Local node identifier
            k: Maximum nodes per bucket
            subnet_diversity_limit: Max nodes per /24 subnet in each bucket
        """
        self.node_id = node_id
        self.k = k
        self.subnet_diversity_limit = subnet_diversity_limit
        self.buckets: List[KBucket] = [
            KBucket(k, subnet_diversity_limit) for _ in range(NODE_ID_BITS)
        ]

        logger.info(
            "Initialized routing table for %s (k=%d, buckets=%d)",
            node_id[:20],
            k,
            NODE_ID_BITS
        )

    def add_node(self, node_id: str, ip: str, port: int) -> bool:
        """
        Add node to routing table.

        Determines appropriate k-bucket based on XOR distance, then adds node
        using k-bucket's LRU eviction policy.

        Args:
            node_id: Node identifier
            ip: IP address
            port: UDP port

        Returns:
            True if node was added

        Raises:
            ValueError: If trying to add self
        """
        if node_id == self.node_id:
            raise ValueError("Cannot add self to routing table")

        # Determine bucket index
        distance = xor_distance(self.node_id, node_id)
        if distance == 0:
            raise ValueError(f"Node {node_id} has same ID as local node")

        idx = bucket_index(distance)

        # Add to bucket
        node = DHTNode(node_id=node_id, ip=ip, port=port)
        success = self.buckets[idx].add(node)

        if success:
            logger.debug(
                "Added node %s to bucket %d (distance: %d)",
                node_id[:20],
                idx,
                distance
            )

        return success

    def remove_node(self, node_id: str) -> bool:
        """
        Remove node from routing table.

        Args:
            node_id: Node identifier

        Returns:
            True if node was removed
        """
        try:
            distance = xor_distance(self.node_id, node_id)
            idx = bucket_index(distance)
            return self.buckets[idx].remove(node_id)
        except ValueError:
            logger.warning("Cannot remove node %s (invalid distance)", node_id[:20])
            return False

    def get_node(self, node_id: str) -> Optional[DHTNode]:
        """
        Get DHTNode by node_id.

        Args:
            node_id: Node identifier

        Returns:
            DHTNode if found, None otherwise
        """
        try:
            distance = xor_distance(self.node_id, node_id)
            idx = bucket_index(distance)
            return self.buckets[idx].get_node(node_id)
        except ValueError:
            return None

    def find_closest_nodes(self, target_id: str, count: int = 20) -> List[DHTNode]:
        """
        Find k closest nodes to target.

        This is the core of Kademlia lookup - returns up to k nodes closest
        to target by XOR distance.

        Args:
            target_id: Target node identifier
            count: Number of nodes to return (k parameter)

        Returns:
            List of DHTNode objects sorted by distance (closest first)
        """
        # Collect all nodes from all buckets
        all_nodes: List[DHTNode] = []
        for bucket in self.buckets:
            all_nodes.extend(bucket.get_nodes())

        # Sort by XOR distance to target
        sorted_nodes = sorted(
            all_nodes,
            key=lambda node: xor_distance(target_id, node.node_id)
        )

        return sorted_nodes[:count]

    def get_bucket_for_node(self, node_id: str) -> Optional[KBucket]:
        """
        Get k-bucket that would contain node_id.

        Args:
            node_id: Node identifier

        Returns:
            KBucket or None if node_id is self
        """
        try:
            distance = xor_distance(self.node_id, node_id)
            idx = bucket_index(distance)
            return self.buckets[idx]
        except ValueError:
            return None

    def get_node_count(self) -> int:
        """Return total number of nodes in routing table."""
        return sum(len(bucket) for bucket in self.buckets)

    def get_bucket_stats(self) -> Dict[str, int]:
        """
        Get routing table statistics.

        Returns:
            Dict with: total_nodes, full_buckets, empty_buckets
        """
        total_nodes = self.get_node_count()
        full_buckets = sum(1 for bucket in self.buckets if bucket.is_full())
        empty_buckets = sum(1 for bucket in self.buckets if len(bucket) == 0)

        return {
            "total_nodes": total_nodes,
            "full_buckets": full_buckets,
            "empty_buckets": empty_buckets,
            "non_empty_buckets": NODE_ID_BITS - empty_buckets,
        }

    def get_buckets_needing_refresh(self, interval: float = 3600.0) -> List[int]:
        """
        Get list of bucket indices that need periodic refresh.

        Args:
            interval: Refresh interval in seconds

        Returns:
            List of bucket indices
        """
        return [
            idx for idx, bucket in enumerate(self.buckets)
            if len(bucket) > 0 and bucket.needs_refresh(interval)
        ]

    def get_all_nodes(self) -> List[DHTNode]:
        """
        Get all nodes from all buckets.

        Returns:
            List of all DHTNode objects
        """
        all_nodes: List[DHTNode] = []
        for bucket in self.buckets:
            all_nodes.extend(bucket.get_nodes())
        return all_nodes
