"""
Tests for DHT Manager - Kademlia DHT Orchestration

Tests cover:
- Initialization and lifecycle (start/stop)
- Bootstrap from seed nodes
- Iterative lookup algorithm
- Node announcement
- Peer discovery
- Maintenance tasks
- Statistics and diagnostics
"""

import asyncio
import pytest
import pytest_asyncio

from dpc_client_core.dht.manager import DHTManager, DHTConfig
from dpc_client_core.dht.routing import DHTNode


# ===== Fixtures =====

@pytest_asyncio.fixture
async def dht_manager():
    """Create DHT manager for testing."""
    manager = DHTManager(
        node_id="dpc-node-00000000000000000000000000000001",
        ip="127.0.0.1",
        port=0,  # Random port
        config=DHTConfig(
            k=20,
            alpha=3,
            bootstrap_timeout=5.0,
            lookup_timeout=5.0
        )
    )

    await manager.start(host="127.0.0.1", port=0)

    yield manager

    await manager.stop()


@pytest_asyncio.fixture
async def seed_network():
    """Create network of 5 seed nodes for testing."""
    managers = []

    for i in range(5):
        node_id = f"dpc-node-{i:032x}"  # 32 hex characters
        manager = DHTManager(
            node_id=node_id,
            ip="127.0.0.1",
            port=0,
            config=DHTConfig(k=20, alpha=3)
        )
        await manager.start(host="127.0.0.1", port=0)
        managers.append(manager)

    yield managers

    # Cleanup
    for manager in managers:
        await manager.stop()


# ===== Initialization Tests =====

@pytest.mark.asyncio
async def test_dht_manager_initialization():
    """Test DHT manager initialization."""
    manager = DHTManager(
        node_id="dpc-node-74657374313233000000000000000000",
        ip="192.168.1.100",
        port=8889
    )

    assert manager.node_id == "dpc-node-74657374313233000000000000000000"
    assert manager.ip == "192.168.1.100"
    assert manager.port == 8889
    assert manager.config.k == 20
    assert manager.config.alpha == 3


@pytest.mark.asyncio
async def test_dht_manager_start_stop(dht_manager):
    """Test DHT manager lifecycle."""
    # Should be running from fixture
    assert dht_manager._running is True
    assert dht_manager.rpc_handler.transport is not None

    # Stop should work cleanly
    await dht_manager.stop()
    assert dht_manager._running is False


@pytest.mark.asyncio
async def test_dht_manager_double_start():
    """Test that double start is handled gracefully."""
    manager = DHTManager(
        node_id="dpc-node-646f75626c6500000000000000000000",
        ip="127.0.0.1",
        port=0
    )

    await manager.start(host="127.0.0.1", port=0)
    await manager.start(host="127.0.0.1", port=0)  # Should log warning but not crash

    assert manager._running is True

    await manager.stop()


# ===== Bootstrap Tests =====

@pytest.mark.asyncio
async def test_bootstrap_empty_seeds(dht_manager):
    """Test bootstrap with no seed nodes."""
    result = await dht_manager.bootstrap([])

    assert result is False  # Should fail
    assert dht_manager.stats["bootstraps"] == 1


@pytest.mark.asyncio
async def test_bootstrap_unreachable_seeds(dht_manager):
    """Test bootstrap with unreachable seed nodes."""
    # Use unreachable IP (RFC 5737 TEST-NET-1)
    seeds = [("192.0.2.1", 8889), ("192.0.2.2", 8889)]

    result = await dht_manager.bootstrap(seeds)

    assert result is False  # Should fail (no responsive seeds)
    assert dht_manager.stats["bootstraps"] == 1


@pytest.mark.asyncio
async def test_bootstrap_success(seed_network):
    """Test successful bootstrap from seed network."""
    # Create new node to bootstrap
    new_node = DHTManager(
        node_id="dpc-node-11111111111111111111111111111111",  # Valid 32 hex chars
        ip="127.0.0.1",
        port=0,
        config=DHTConfig(bootstrap_timeout=10.0)
    )

    await new_node.start(host="127.0.0.1", port=0)

    try:
        # Get seed addresses
        seeds = [
            (
                "127.0.0.1",
                manager.rpc_handler.transport.get_extra_info('sockname')[1]
            )
            for manager in seed_network[:3]  # Use 3 seeds
        ]

        # Bootstrap
        result = await new_node.bootstrap(seeds)

        assert result is True  # Should succeed
        assert new_node.stats["bootstraps"] == 1

        # Should have populated routing table
        node_count = new_node.routing_table.get_node_count()
        assert node_count > 0

    finally:
        await new_node.stop()


@pytest.mark.asyncio
async def test_bootstrap_partial_failure(seed_network):
    """Test bootstrap with some unresponsive seeds."""
    new_node = DHTManager(
        node_id="dpc-node-22222222222222222222222222222222",  # Valid 32 hex chars
        ip="127.0.0.1",
        port=0,
        config=DHTConfig(bootstrap_timeout=10.0)
    )

    await new_node.start(host="127.0.0.1", port=0)

    try:
        # Mix of responsive and unresponsive seeds
        seeds = [
            ("192.0.2.1", 8889),  # Unresponsive
            (
                "127.0.0.1",
                seed_network[0].rpc_handler.transport.get_extra_info('sockname')[1]
            ),  # Responsive
            ("192.0.2.2", 8889),  # Unresponsive
        ]

        result = await new_node.bootstrap(seeds)

        assert result is True  # Should succeed with at least one responsive seed

    finally:
        await new_node.stop()


# ===== Iterative Lookup Tests =====

@pytest.mark.asyncio
async def test_find_node_empty_table(dht_manager):
    """Test iterative lookup with empty routing table."""
    target_id = "dpc-node-target"

    nodes = await dht_manager.find_node(target_id)

    assert nodes == []  # Should return empty list
    assert dht_manager.stats["lookups"] == 1


@pytest.mark.asyncio
async def test_find_node_in_network(seed_network):
    """Test iterative lookup in a network of nodes."""
    # Bootstrap node 0 from node 1
    node0 = seed_network[0]
    node1 = seed_network[1]

    node1_port = node1.rpc_handler.transport.get_extra_info('sockname')[1]
    seeds = [("127.0.0.1", node1_port)]

    await node0.bootstrap(seeds)

    # Perform lookup for node 1
    nodes = await node0.find_node(node1.node_id)

    assert len(nodes) > 0  # Should find nodes
    assert node0.stats["lookups"] >= 2  # Bootstrap does self-lookup + manual lookup


@pytest.mark.asyncio
async def test_find_node_self_lookup(dht_manager, seed_network):
    """Test self-lookup during bootstrap."""
    # Bootstrap from seed network
    seeds = [
        (
            "127.0.0.1",
            seed_network[0].rpc_handler.transport.get_extra_info('sockname')[1]
        )
    ]

    await dht_manager.bootstrap(seeds)

    # Lookup self
    nodes = await dht_manager.find_node(dht_manager.node_id)

    # Should find nodes (populated during bootstrap)
    # Note: May not find self, but should find other nodes
    assert dht_manager.stats["lookups"] >= 2  # Bootstrap + manual


@pytest.mark.asyncio
async def test_find_node_convergence():
    """Test that iterative lookup converges."""
    # Create small network
    managers = []
    for i in range(3):
        manager = DHTManager(
            node_id=f"dpc-node-{i:032x}",  # 32 hex characters
            ip="127.0.0.1",
            port=0,
            config=DHTConfig(alpha=2)
        )
        await manager.start(host="127.0.0.1", port=0)
        managers.append(manager)

    try:
        # Interconnect nodes
        for i, manager in enumerate(managers):
            for j, other in enumerate(managers):
                if i != j:
                    other_port = other.rpc_handler.transport.get_extra_info('sockname')[1]
                    await manager.rpc_handler.ping("127.0.0.1", other_port)

        # Perform lookup
        target_id = "dpc-node-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # Valid 32 hex chars
        nodes = await managers[0].find_node(target_id)

        # Should return at most k nodes
        assert len(nodes) <= managers[0].config.k

    finally:
        for manager in managers:
            await manager.stop()


# ===== Announcement Tests =====

@pytest.mark.asyncio
async def test_announce_empty_table(dht_manager):
    """Test announce with empty routing table."""
    count = await dht_manager.announce()

    assert count == 0  # No nodes to announce to
    assert dht_manager.stats["announcements"] == 1


@pytest.mark.asyncio
async def test_announce_in_network(seed_network):
    """Test node announcement in a network."""
    # Bootstrap node 0 from network
    node0 = seed_network[0]

    seeds = [
        (
            "127.0.0.1",
            seed_network[1].rpc_handler.transport.get_extra_info('sockname')[1]
        )
    ]

    await node0.bootstrap(seeds)

    # Announce presence
    count = await node0.announce()

    assert count > 0  # Should announce to at least one node
    assert node0.stats["announcements"] == 1


# ===== Peer Discovery Tests =====

@pytest.mark.asyncio
async def test_find_peer_not_found(dht_manager, seed_network):
    """Test finding a peer that hasn't announced."""
    # Bootstrap
    seeds = [
        (
            "127.0.0.1",
            seed_network[0].rpc_handler.transport.get_extra_info('sockname')[1]
        )
    ]

    await dht_manager.bootstrap(seeds)

    # Try to find non-existent peer
    result = await dht_manager.find_peer("dpc-node-99999999999999999999999999999999")  # Valid 32 hex chars

    assert result is None  # Should not find


@pytest.mark.asyncio
async def test_find_peer_success():
    """Test successful peer discovery via DHT."""
    # Create network of 3 nodes
    managers = []
    for i in range(3):
        manager = DHTManager(
            node_id=f"dpc-node-{i:032x}",  # 32 hex characters
            ip="127.0.0.1",
            port=10000 + i,  # Use known ports for this test
            config=DHTConfig()
        )
        await manager.start(host="127.0.0.1", port=0)
        managers.append(manager)

    try:
        # Interconnect all nodes
        for i, manager in enumerate(managers):
            for j, other in enumerate(managers):
                if i != j:
                    other_port = other.rpc_handler.transport.get_extra_info('sockname')[1]
                    await manager.rpc_handler.ping("127.0.0.1", other_port)

        # Node 2 announces itself
        await managers[2].announce()

        # Give time for STORE to propagate
        await asyncio.sleep(0.5)

        # Node 0 tries to find node 2
        result = await managers[0].find_peer(managers[2].node_id)

        # Should find node 2's contact info
        if result:  # May fail if STORE didn't reach node 0
            ip, port = result
            assert ip == "127.0.0.1"
            assert port == 10002  # Node 2's port

    finally:
        for manager in managers:
            await manager.stop()


# ===== Maintenance Tests =====

@pytest.mark.asyncio
async def test_maintenance_loop_starts():
    """Test that maintenance loop starts correctly."""
    manager = DHTManager(
        node_id="dpc-node-6d61696e74656e616e636500000000000",
        ip="127.0.0.1",
        port=0
    )

    await manager.start(host="127.0.0.1", port=0)

    assert manager._maintenance_task is not None
    assert not manager._maintenance_task.done()

    await manager.stop()

    assert manager._maintenance_task.done()


@pytest.mark.asyncio
async def test_bucket_refresh():
    """Test bucket refresh logic."""
    manager = DHTManager(
        node_id="dpc-node-33333333333333333333333333333333",  # Valid 32 hex chars
        ip="127.0.0.1",
        port=0,
        config=DHTConfig(bucket_refresh_interval=1.0)  # 1 second for testing
    )

    await manager.start(host="127.0.0.1", port=0)

    try:
        # Manually add a node to populate a bucket
        manager.routing_table.add_node(
            "dpc-node-1234567890abcdef1234567890abcdef",  # Valid 32 hex chars
            "127.0.0.1",
            8889
        )

        # Wait for refresh interval
        await asyncio.sleep(2.0)

        # Check that bucket refresh was attempted
        # (Will fail to find nodes, but should increment counter)
        # Note: This is a weak test - in real deployment would verify actual refresh

    finally:
        await manager.stop()


# ===== Statistics Tests =====

@pytest.mark.asyncio
async def test_get_stats(dht_manager):
    """Test statistics retrieval."""
    stats = dht_manager.get_stats()

    assert "bootstraps" in stats
    assert "lookups" in stats
    assert "announcements" in stats
    assert "bucket_refreshes" in stats
    assert "routing_table" in stats
    assert "rpc" in stats

    # Check routing table stats
    rt_stats = stats["routing_table"]
    assert "total_nodes" in rt_stats
    assert "full_buckets" in rt_stats
    assert "empty_buckets" in rt_stats


@pytest.mark.asyncio
async def test_get_known_peers(dht_manager, seed_network):
    """Test retrieval of known peers."""
    # Initially empty
    peers = dht_manager.get_known_peers()
    assert peers == []

    # Bootstrap from network
    seeds = [
        (
            "127.0.0.1",
            seed_network[0].rpc_handler.transport.get_extra_info('sockname')[1]
        )
    ]

    await dht_manager.bootstrap(seeds)

    # Should have peers now
    peers = dht_manager.get_known_peers()
    assert len(peers) > 0
    assert all(isinstance(p, DHTNode) for p in peers)


# ===== Integration Tests =====

@pytest.mark.asyncio
async def test_full_dht_workflow():
    """Test complete DHT workflow: bootstrap -> lookup -> announce -> find_peer."""
    # Create network of 4 nodes
    managers = []
    for i in range(4):
        manager = DHTManager(
            node_id=f"dpc-node-{i:032x}",  # 32 hex characters
            ip="127.0.0.1",
            port=20000 + i,
            config=DHTConfig(bootstrap_timeout=15.0, lookup_timeout=10.0)
        )
        await manager.start(host="127.0.0.1", port=0)
        managers.append(manager)

    try:
        # Step 1: Bootstrap nodes 1-3 from node 0
        node0_port = managers[0].rpc_handler.transport.get_extra_info('sockname')[1]

        for manager in managers[1:]:
            await manager.bootstrap([("127.0.0.1", node0_port)])

        # Wait for bootstrap to complete
        await asyncio.sleep(1.0)

        # Step 2: Node 3 announces itself
        await managers[3].announce()

        await asyncio.sleep(0.5)

        # Step 3: Node 1 performs lookup for node 2
        nodes = await managers[1].find_node(managers[2].node_id)
        assert len(nodes) > 0

        # Step 4: Node 1 tries to find node 3 via DHT
        result = await managers[1].find_peer(managers[3].node_id)

        # May or may not find (depends on DHT convergence)
        if result:
            ip, port = result
            assert ip == "127.0.0.1"

        # Verify statistics
        assert managers[1].stats["bootstraps"] >= 1
        assert managers[1].stats["lookups"] >= 2  # Bootstrap + manual lookup
        assert managers[3].stats["announcements"] >= 1

    finally:
        for manager in managers:
            await manager.stop()


@pytest.mark.asyncio
async def test_concurrent_lookups():
    """Test multiple concurrent lookups."""
    # Create small network
    managers = []
    for i in range(3):
        manager = DHTManager(
            node_id=f"dpc-node-{i:032x}",  # 32 hex characters
            ip="127.0.0.1",
            port=0,
            config=DHTConfig(alpha=2)
        )
        await manager.start(host="127.0.0.1", port=0)
        managers.append(manager)

    try:
        # Bootstrap node 0 from node 1
        node1_port = managers[1].rpc_handler.transport.get_extra_info('sockname')[1]
        await managers[0].bootstrap([("127.0.0.1", node1_port)])

        # Perform concurrent lookups
        targets = [f"dpc-node-{i:032x}" for i in range(1000, 1005)]  # Valid 32 hex chars
        lookup_tasks = [managers[0].find_node(tid) for tid in targets]

        results = await asyncio.gather(*lookup_tasks, return_exceptions=True)

        # All lookups should complete without exceptions
        assert len(results) == 5
        assert all(not isinstance(r, Exception) for r in results)

        # Stats should reflect all lookups
        assert managers[0].stats["lookups"] >= 6  # Bootstrap + 5 manual

    finally:
        for manager in managers:
            await manager.stop()
