"""
DHT Manager - Kademlia DHT Orchestration

This module implements the high-level Kademlia DHT operations:
- Bootstrap - Initial routing table population from seed nodes
- Iterative Lookup - O(log n) peer discovery using parallel FIND_NODE
- Announce - Advertise node presence via STORE operations
- Periodic Maintenance - Bucket refresh and value republishing

The DHT Manager coordinates the routing table and RPC handler to provide
a complete Kademlia implementation for decentralized peer discovery.

Architecture:
    DHTManager
    ├── RoutingTable (k-buckets for known peers)
    ├── DHTRPCHandler (UDP communication)
    └── Background Tasks (maintenance, refresh)
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass

from .routing import DHTNode, RoutingTable
from .rpc import DHTRPCHandler, RPCConfig

logger = logging.getLogger(__name__)


@dataclass
class DHTConfig:
    """DHT configuration parameters."""
    k: int = 20  # Kademlia k parameter (bucket size)
    alpha: int = 3  # Parallelism factor for iterative lookup
    subnet_diversity_limit: int = 2  # Max nodes per /24 subnet

    # Timeouts
    bootstrap_timeout: float = 30.0  # Bootstrap timeout in seconds
    lookup_timeout: float = 10.0  # Iterative lookup timeout

    # Maintenance intervals
    bucket_refresh_interval: float = 3600.0  # 1 hour
    announce_interval: float = 3600.0  # 1 hour
    republish_interval: float = 3600.0  # 1 hour

    # Network settings
    rpc_timeout: float = 5.0  # RPC timeout (increased for internet-wide DHT)
    rpc_retries: int = 3  # RPC retry attempts


class DHTManager:
    """
    High-level Kademlia DHT manager.

    Provides:
    - Bootstrap from seed nodes
    - Iterative lookup for peer discovery
    - Node announcement via DHT STORE
    - Periodic maintenance tasks

    Example:
        # Initialize DHT
        dht = DHTManager(node_id="dpc-node-abc123", ip="192.168.1.100", port=8889)
        await dht.start()

        # Bootstrap from known seeds
        seeds = [("seed1.example.com", 8889), ("seed2.example.com", 8889)]
        await dht.bootstrap(seeds)

        # Find peers close to target
        peers = await dht.find_node("dpc-node-target123")

        # Announce presence
        await dht.announce()

        # Shutdown
        await dht.stop()
    """

    def __init__(
        self,
        node_id: str,
        ip: str,
        port: int,
        config: Optional[DHTConfig] = None
    ):
        """
        Initialize DHT manager.

        Args:
            node_id: Local node identifier (dpc-node-*)
            ip: Local IP address for DHT announcements
            port: UDP port for DHT RPCs
            config: DHT configuration (optional)
        """
        self.node_id = node_id
        self.ip = ip
        self.port = port
        self.config = config or DHTConfig()

        # Initialize routing table and RPC handler
        self.routing_table = RoutingTable(
            node_id=node_id,
            k=self.config.k,
            subnet_diversity_limit=self.config.subnet_diversity_limit
        )

        rpc_config = RPCConfig(
            timeout=self.config.rpc_timeout,
            max_retries=self.config.rpc_retries
        )
        self.rpc_handler = DHTRPCHandler(self.routing_table, rpc_config)

        # Background tasks
        self._maintenance_task: Optional[asyncio.Task] = None
        self._running = False
        self._seed_nodes: List[Tuple[str, int]] = []  # Stored for bootstrap retries

        # Statistics
        self.stats = {
            "bootstraps": 0,
            "lookups": 0,
            "announcements": 0,
            "bucket_refreshes": 0,
        }

        logger.info(
            "DHT manager initialized: %s at %s:%d (k=%d, alpha=%d)",
            node_id[:20], ip, port, self.config.k, self.config.alpha
        )

    async def start(self, host: str = "0.0.0.0", port: Optional[int] = None):
        """
        Start DHT manager and UDP server.

        Args:
            host: Bind address (0.0.0.0 for all interfaces)
            port: UDP port (uses self.port if not specified)
        """
        if self._running:
            logger.warning("DHT manager already running")
            return

        # Start RPC handler
        bind_port = port if port is not None else self.port
        await self.rpc_handler.start(host, bind_port)

        # Start background maintenance
        self._running = True
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

        logger.info("DHT manager started on %s:%d", host, bind_port)

    async def stop(self):
        """Stop DHT manager and background tasks."""
        if not self._running:
            return

        self._running = False

        # Cancel maintenance task
        if self._maintenance_task:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass

        # Stop RPC handler
        await self.rpc_handler.stop()

        logger.info("DHT manager stopped")

    # ===== Bootstrap =====

    async def bootstrap(self, seed_nodes: List[Tuple[str, int]]) -> bool:
        """
        Bootstrap DHT from seed nodes.

        Algorithm:
        1. PING all seed nodes to populate initial routing table
        2. Perform iterative FIND_NODE lookup for self (to discover nearby peers)
        3. Refresh all k-buckets by performing lookups for random IDs in each bucket

        Args:
            seed_nodes: List of (ip, port) tuples for seed nodes

        Returns:
            True if bootstrap succeeded (at least one responsive seed)
        """
        logger.info("Starting DHT bootstrap with %d seed nodes", len(seed_nodes))
        self.stats["bootstraps"] += 1

        # Store seed nodes for retry attempts
        self._seed_nodes = seed_nodes

        if not seed_nodes:
            logger.warning("No seed nodes provided for bootstrap")
            return False

        start_time = time.time()
        responsive_seeds = 0

        # Step 1: Contact all seed nodes
        ping_tasks = [
            self._ping_node(ip, port)
            for ip, port in seed_nodes
        ]

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*ping_tasks, return_exceptions=True),
                timeout=self.config.bootstrap_timeout
            )

            for result in results:
                if result and not isinstance(result, Exception):
                    responsive_seeds += 1

        except asyncio.TimeoutError:
            logger.warning("Bootstrap PING phase timed out after %.1fs", self.config.bootstrap_timeout)

        if responsive_seeds == 0:
            logger.error("Bootstrap failed: no responsive seed nodes")
            return False

        logger.info("Bootstrap: %d/%d seed nodes responsive", responsive_seeds, len(seed_nodes))

        # Step 2: Lookup self to discover nearby peers
        logger.info("Bootstrap: performing self-lookup to discover nearby peers")
        await self.find_node(self.node_id)

        # Step 3: Refresh buckets (optional, can be async)
        asyncio.create_task(self._refresh_all_buckets())

        elapsed = time.time() - start_time
        node_count = self.routing_table.get_node_count()

        logger.info(
            "Bootstrap completed in %.2fs: %d nodes in routing table",
            elapsed, node_count
        )

        return True

    async def _retry_bootstrap(self):
        """
        Retry bootstrap when routing table is empty.

        Called by maintenance loop when no nodes are known.
        """
        if not self._seed_nodes:
            logger.debug("No seed nodes available for bootstrap retry")
            return

        logger.info("Retrying bootstrap with %d seed nodes", len(self._seed_nodes))
        await self.bootstrap(self._seed_nodes)

    async def _ping_node(self, ip: str, port: int) -> Optional[Dict]:
        """
        Ping a single node (helper for bootstrap).

        Args:
            ip: Node IP
            port: Node UDP port

        Returns:
            PONG response dict or None
        """
        try:
            response = await self.rpc_handler.ping(ip, port)
            if response:
                logger.debug("PING successful: %s:%d", ip, port)
            return response
        except Exception as e:
            logger.debug("PING failed for %s:%d: %s", ip, port, e)
            return None

    # ===== Iterative Lookup =====

    async def find_node(self, target_id: str) -> List[DHTNode]:
        """
        Iterative FIND_NODE lookup (Kademlia's core algorithm).

        Algorithm:
        1. Start with k closest nodes from local routing table
        2. Send parallel FIND_NODE RPCs to alpha closest unqueried nodes
        3. Add returned nodes to candidate set
        4. Repeat until:
           - No closer nodes found (convergence)
           - Timeout reached
           - k nodes have responded
        5. Return k closest nodes found

        Complexity: O(log n) RPCs with O(alpha) parallelism

        Args:
            target_id: Node ID to find

        Returns:
            List of k closest nodes to target (sorted by distance)
        """
        logger.debug("Starting iterative lookup for %s", target_id[:20])
        self.stats["lookups"] += 1

        start_time = time.time()

        # Initialize with k closest from local table
        shortlist = self.routing_table.find_closest_nodes(target_id, self.config.k)

        if not shortlist:
            logger.warning("Lookup failed: routing table empty")
            return []

        # Tracking sets
        queried: Set[str] = set()  # Nodes we've sent RPCs to
        responded: Set[str] = set()  # Nodes that responded
        closest_distance = float('inf')  # Best distance found so far
        stall_count = 0  # Iterations without improvement

        # Iterative lookup loop
        while True:
            # Find alpha unqueried nodes closest to target
            candidates = [
                node for node in shortlist
                if node.node_id not in queried
            ][:self.config.alpha]

            if not candidates:
                logger.debug("Lookup converged: no more candidates to query")
                break

            # Send parallel FIND_NODE RPCs
            lookup_tasks = [
                self._lookup_node(node.ip, node.port, target_id, node.node_id)
                for node in candidates
            ]

            # Mark as queried
            for node in candidates:
                queried.add(node.node_id)

            # Wait for responses
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*lookup_tasks, return_exceptions=True),
                    timeout=self.config.lookup_timeout
                )
            except asyncio.TimeoutError:
                logger.debug("Lookup timed out after %.1fs", self.config.lookup_timeout)
                break

            # Process results
            new_nodes_found = False
            for result in results:
                if isinstance(result, list):  # Successful response
                    responded.add(candidates[results.index(result)].node_id)

                    for node in result:
                        # Add to shortlist if not already present
                        if node.node_id not in {n.node_id for n in shortlist}:
                            shortlist.append(node)
                            new_nodes_found = True

            # Re-sort shortlist by distance to target
            from .distance import xor_distance
            shortlist = sorted(
                shortlist,
                key=lambda n: xor_distance(target_id, n.node_id)
            )[:self.config.k]

            # Check convergence: no closer nodes found
            if shortlist:
                current_distance = xor_distance(target_id, shortlist[0].node_id)
                if current_distance < closest_distance:
                    closest_distance = current_distance
                    stall_count = 0
                else:
                    stall_count += 1

            if stall_count >= 2:  # No improvement for 2 rounds
                logger.debug("Lookup converged: no closer nodes found")
                break

            if not new_nodes_found:
                logger.debug("Lookup converged: no new nodes discovered")
                break

            # Check if we've queried enough nodes
            if len(responded) >= self.config.k:
                logger.debug("Lookup complete: %d nodes responded", len(responded))
                break

        elapsed = time.time() - start_time
        logger.info(
            "Lookup completed in %.2fs: queried %d nodes, %d responded, found %d results",
            elapsed, len(queried), len(responded), len(shortlist)
        )

        return shortlist

    async def _lookup_node(
        self,
        ip: str,
        port: int,
        target_id: str,
        node_id: str
    ) -> Optional[List[DHTNode]]:
        """
        Perform FIND_NODE RPC (helper for iterative lookup).

        Args:
            ip: Node IP
            port: Node UDP port
            target_id: Target node ID for lookup
            node_id: Node ID of the peer being queried

        Returns:
            List of DHTNode objects or None if failed
        """
        try:
            nodes = await self.rpc_handler.find_node(ip, port, target_id)
            if nodes:
                logger.debug(
                    "FIND_NODE from %s returned %d nodes",
                    node_id[:20], len(nodes)
                )
            return nodes
        except Exception as e:
            logger.debug("FIND_NODE to %s failed: %s", node_id[:20], e)
            return None

    # ===== Node Announcement =====

    async def announce(self) -> int:
        """
        Announce node presence to DHT.

        Algorithm:
        1. Perform iterative lookup for self (find k closest nodes)
        2. Send STORE RPC to all k nodes with key=node_id, value="ip:port"

        This allows other nodes to discover us via FIND_VALUE(node_id).

        Returns:
            Number of successful STORE operations
        """
        logger.info("Announcing node presence to DHT")
        self.stats["announcements"] += 1

        # Find k closest nodes to self
        closest = await self.find_node(self.node_id)

        if not closest:
            logger.warning("Announce failed: no nodes found")
            return 0

        # Store our contact info on all k nodes
        value = f"{self.ip}:{self.port}"
        store_tasks = [
            self.rpc_handler.store(node.ip, node.port, self.node_id, value)
            for node in closest
        ]

        results = await asyncio.gather(*store_tasks, return_exceptions=True)

        success_count = sum(1 for r in results if r is True)

        logger.info(
            "Announced to %d/%d nodes (key=%s, value=%s)",
            success_count, len(closest), self.node_id[:20], value
        )

        return success_count

    # ===== Peer Discovery =====

    async def find_peer(self, target_node_id: str) -> Optional[Tuple[str, int]]:
        """
        Find contact information for a specific peer.

        Algorithm:
        1. Perform iterative lookup for target_node_id
        2. Try FIND_VALUE on closest nodes (check if they have stored contact info)
        3. Return (ip, port) if found

        Args:
            target_node_id: Node ID to find

        Returns:
            (ip, port) tuple if found, None otherwise
        """
        logger.info("Searching for peer %s", target_node_id[:20])

        # Perform iterative lookup
        closest = await self.find_node(target_node_id)

        if not closest:
            logger.warning("Peer search failed: no nodes found")
            return None

        # Try FIND_VALUE on closest nodes
        for node in closest:
            try:
                result = await self.rpc_handler.find_value(node.ip, node.port, target_node_id)

                if result and "value" in result:
                    # Found stored contact info
                    value = result["value"]

                    # Parse "ip:port" format
                    if ":" in value:
                        ip, port_str = value.rsplit(":", 1)
                        port = int(port_str)

                        logger.info(
                            "Found peer %s at %s:%d via DHT",
                            target_node_id[:20], ip, port
                        )

                        return (ip, port)

            except Exception as e:
                logger.debug("FIND_VALUE to %s failed: %s", node.node_id[:20], e)
                continue

        logger.warning("Peer %s not found in DHT", target_node_id[:20])
        return None

    # ===== Maintenance =====

    async def _maintenance_loop(self):
        """
        Background maintenance tasks.

        Runs periodically to:
        - Refresh stale k-buckets
        - Republish stored values
        - Re-announce node presence
        """
        logger.info("DHT maintenance loop started")

        last_bucket_refresh = time.time()
        last_announce = time.time()
        last_bootstrap_retry = 0  # Track bootstrap retry timing

        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute

                now = time.time()

                # Bootstrap retry if routing table is empty (retry every 5 minutes)
                node_count = self.routing_table.get_node_count()
                if node_count == 0 and now - last_bootstrap_retry >= 300:
                    logger.info("Routing table empty - retrying bootstrap")
                    # Try bootstrap again (seed nodes stored in config)
                    asyncio.create_task(self._retry_bootstrap())
                    last_bootstrap_retry = now

                # Bucket refresh (every hour)
                if now - last_bucket_refresh >= self.config.bucket_refresh_interval:
                    asyncio.create_task(self._refresh_stale_buckets())
                    last_bucket_refresh = now

                # Re-announce (every hour)
                if now - last_announce >= self.config.announce_interval:
                    asyncio.create_task(self.announce())
                    last_announce = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Maintenance loop error: %s", e)

        logger.info("DHT maintenance loop stopped")

    async def _refresh_stale_buckets(self):
        """Refresh k-buckets that haven't been updated recently."""
        stale_buckets = self.routing_table.get_buckets_needing_refresh(
            interval=self.config.bucket_refresh_interval
        )

        if not stale_buckets:
            return

        logger.info("Refreshing %d stale k-buckets", len(stale_buckets))
        self.stats["bucket_refreshes"] += 1

        # Generate random node ID in each bucket's range and perform lookup
        from .distance import parse_node_id
        import random

        for bucket_idx in stale_buckets:
            # Generate random ID at distance 2^bucket_idx from self
            self_int = parse_node_id(self.node_id)
            random_distance = (1 << bucket_idx) | random.randint(0, (1 << bucket_idx) - 1)
            target_int = self_int ^ random_distance

            # Convert back to node ID format (16 hex characters)
            target_id = f"dpc-node-{target_int:016x}"

            # Perform lookup (asynchronously)
            asyncio.create_task(self.find_node(target_id))

    async def _refresh_all_buckets(self):
        """Refresh all non-empty k-buckets (used after bootstrap)."""
        non_empty_buckets = [
            idx for idx, bucket in enumerate(self.routing_table.buckets)
            if len(bucket) > 0
        ]

        if not non_empty_buckets:
            return

        logger.info("Refreshing %d non-empty k-buckets", len(non_empty_buckets))

        # Generate random node ID in each bucket's range and perform lookup
        from .distance import parse_node_id
        import random

        # Run lookups in parallel (with semaphore to limit concurrency)
        sem = asyncio.Semaphore(self.config.alpha)

        async def limited_lookup(target_id):
            async with sem:
                return await self.find_node(target_id)

        await asyncio.gather(
            *[limited_lookup(tid) for tid in [
                f"dpc-node-{(parse_node_id(self.node_id) ^ ((1 << idx) | random.randint(0, (1 << idx) - 1))):016x}"
                for idx in non_empty_buckets
            ]],
            return_exceptions=True
        )

    # ===== Status and Diagnostics =====

    def get_stats(self) -> Dict:
        """
        Get DHT statistics.

        Returns:
            Dict with DHT stats and routing table stats
        """
        return {
            **self.stats,
            "routing_table": self.routing_table.get_bucket_stats(),
            "rpc": self.rpc_handler.stats,
        }

    def get_known_peers(self) -> List[DHTNode]:
        """
        Get all known peers from routing table.

        Returns:
            List of DHTNode objects
        """
        return self.routing_table.get_all_nodes()
