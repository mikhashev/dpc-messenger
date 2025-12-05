"""
Unit tests for DHT routing table and distance utilities.

Tests cover:
- XOR distance calculation and properties
- K-bucket add/remove/eviction with LRU policy
- Routing table find_closest_nodes algorithm
- Subnet diversity enforcement
- Stale node handling
"""

import pytest
import time
from dpc_client_core.dht.distance import (
    parse_node_id,
    xor_distance,
    bucket_index,
    node_id_distance_to_bucket,
    sort_by_distance,
    find_closest_nodes,
    is_closer,
    generate_random_node_id_in_bucket,
)
from dpc_client_core.dht.routing import DHTNode, KBucket, RoutingTable


# ===== Helper Functions =====

def create_test_node(hex_suffix: str, ip: str = "192.168.1.1", port: int = 8889) -> DHTNode:
    """Create test DHTNode with given hex suffix (16 chars)."""
    # Pad hex_suffix to 16 characters
    padded = hex_suffix.zfill(16)
    node_id = f"dpc-node-{padded}"
    return DHTNode(node_id=node_id, ip=ip, port=port)


# ===== XOR Distance Tests =====

def test_parse_node_id():
    """Test node ID parsing to integer."""
    node_id = "dpc-node-0000000000000001"
    result = parse_node_id(node_id)
    assert result == 1

    node_id = "dpc-node-00000000000000ff"
    result = parse_node_id(node_id)
    assert result == 255


def test_parse_node_id_invalid():
    """Test node ID parsing with invalid input."""
    # Missing prefix
    with pytest.raises(ValueError, match="must start with"):
        parse_node_id("invalid-0000000000000001")

    # Wrong hex length
    with pytest.raises(ValueError, match="must be 16 characters"):
        parse_node_id("dpc-node-123")

    # Invalid hex characters
    with pytest.raises(ValueError, match="Invalid hex characters"):
        parse_node_id("dpc-node-gggggggggggggggg")


def test_xor_distance_symmetric():
    """Test XOR distance is symmetric: d(A, B) = d(B, A)."""
    node_a = "dpc-node-0000000000000001"
    node_b = "dpc-node-0000000000000002"

    dist_ab = xor_distance(node_a, node_b)
    dist_ba = xor_distance(node_b, node_a)

    assert dist_ab == dist_ba
    assert dist_ab == 3  # 0x01 XOR 0x02 = 0x03


def test_xor_distance_zero():
    """Test XOR distance to self is zero."""
    node_a = "dpc-node-0000000000000001"
    dist = xor_distance(node_a, node_a)
    assert dist == 0


def test_bucket_index():
    """Test bucket index calculation."""
    # Distance 1 → bucket 0 (2^0 = 1)
    assert bucket_index(1) == 0

    # Distance 2 → bucket 1 (2^1 = 2)
    assert bucket_index(2) == 1

    # Distance 3 → bucket 1 (2^1 < 3 < 2^2)
    assert bucket_index(3) == 1

    # Distance 256 → bucket 8 (2^8 = 256)
    assert bucket_index(256) == 8

    # Distance 0 → error
    with pytest.raises(ValueError, match="cannot be 0"):
        bucket_index(0)


def test_node_id_distance_to_bucket():
    """Test bucket index calculation from two node IDs."""
    node_a = "dpc-node-0000000000000000"
    node_b = "dpc-node-0000000000000001"

    # Distance = 1 → bucket 0
    assert node_id_distance_to_bucket(node_a, node_b) == 0

    node_c = "dpc-node-0000000000000100"
    # Distance = 0x100 = 256 → bucket 8
    assert node_id_distance_to_bucket(node_a, node_c) == 8


def test_sort_by_distance():
    """Test sorting nodes by XOR distance."""
    target = "dpc-node-0000000000000000"
    nodes = [
        "dpc-node-0000000000000003",
        "dpc-node-0000000000000001",
        "dpc-node-0000000000000002",
    ]

    sorted_nodes = sort_by_distance(target, nodes)

    assert sorted_nodes == [
        "dpc-node-0000000000000001",  # Distance 1
        "dpc-node-0000000000000002",  # Distance 2
        "dpc-node-0000000000000003",  # Distance 3
    ]


def test_find_closest_nodes():
    """Test finding k closest nodes from candidates."""
    target = "dpc-node-0000000000000000"
    candidates = [
        ("dpc-node-0000000000000003", "data3"),
        ("dpc-node-0000000000000001", "data1"),
        ("dpc-node-0000000000000005", "data5"),
    ]

    # Find 2 closest
    closest = find_closest_nodes(target, candidates, count=2)

    assert len(closest) == 2
    assert closest[0] == ("dpc-node-0000000000000001", "data1")
    assert closest[1] == ("dpc-node-0000000000000002", "data2") or closest[1] == ("dpc-node-0000000000000003", "data3")


def test_is_closer():
    """Test proximity comparison."""
    target = "dpc-node-0000000000000000"
    candidate = "dpc-node-0000000000000001"  # Distance 1
    reference = "dpc-node-0000000000000003"  # Distance 3

    assert is_closer(target, candidate, reference) is True
    assert is_closer(target, reference, candidate) is False


def test_generate_random_node_id_in_bucket():
    """Test generating random node ID in specific bucket."""
    reference = "dpc-node-0000000000000000"

    # Generate 10 random IDs in bucket 5
    for _ in range(10):
        random_id = generate_random_node_id_in_bucket(reference, bucket_idx=5)

        # Verify it falls in bucket 5 (distance in range [2^5, 2^6) = [32, 64))
        distance = xor_distance(reference, random_id)
        idx = bucket_index(distance)
        assert idx == 5


# ===== DHTNode Tests =====

def test_dht_node_creation():
    """Test DHTNode creation and attributes."""
    node = create_test_node("0001", ip="10.0.0.1", port=8889)

    assert node.node_id == "dpc-node-0000000000000001"
    assert node.ip == "10.0.0.1"
    assert node.port == 8889
    assert node.failed_pings == 0
    assert not node.is_stale(timeout=900)


def test_dht_node_update_last_seen():
    """Test updating last_seen timestamp."""
    node = create_test_node("0001")
    node.failed_pings = 3

    original_time = node.last_seen
    time.sleep(0.01)  # Small delay
    node.update_last_seen()

    assert node.last_seen > original_time
    assert node.failed_pings == 0  # Should reset


def test_dht_node_stale_detection():
    """Test stale node detection."""
    node = create_test_node("0001")

    # Fresh node - not stale
    assert not node.is_stale(timeout=10)

    # Manually set old timestamp
    node.last_seen = time.time() - 20

    # Should be stale with 10s timeout
    assert node.is_stale(timeout=10)


def test_dht_node_subnet():
    """Test subnet extraction."""
    node = create_test_node("0001", ip="192.168.1.100")

    subnet = node.get_subnet(24)
    assert subnet == "192.168.1.0/24"


def test_dht_node_equality():
    """Test node equality based on node_id."""
    node1 = create_test_node("0001", ip="10.0.0.1")
    node2 = create_test_node("0001", ip="10.0.0.2")  # Different IP
    node3 = create_test_node("0002", ip="10.0.0.1")

    assert node1 == node2  # Same node_id
    assert node1 != node3  # Different node_id


# ===== KBucket Tests =====

def test_kbucket_add_node():
    """Test adding node to empty k-bucket."""
    bucket = KBucket(k=5)
    node = create_test_node("0001")

    success = bucket.add(node)

    assert success is True
    assert len(bucket) == 1
    assert bucket.has_node("dpc-node-0000000000000001")


def test_kbucket_add_duplicate():
    """Test adding duplicate node (should refresh)."""
    bucket = KBucket(k=5)
    node = create_test_node("0001")

    bucket.add(node)
    original_last_updated = bucket.get_last_updated()

    time.sleep(0.01)
    bucket.add(node)  # Add again

    # Should still have 1 node
    assert len(bucket) == 1

    # Last updated should change
    assert bucket.get_last_updated() > original_last_updated


def test_kbucket_lru_eviction():
    """Test LRU eviction when bucket is full."""
    bucket = KBucket(k=3)  # Small bucket

    # Fill bucket with nodes from different subnets (avoid diversity limit)
    node1 = create_test_node("0001", ip="10.0.1.1")
    node2 = create_test_node("0002", ip="10.0.2.2")
    node3 = create_test_node("0003", ip="10.0.3.3")

    bucket.add(node1)
    bucket.add(node2)
    bucket.add(node3)

    assert len(bucket) == 3
    assert bucket.is_full()

    # Make node1 stale by manually setting timestamp
    node1.last_seen = time.time() - 1000

    # Add new node - should evict stale node1
    node4 = create_test_node("0004", ip="10.0.4.4")
    success = bucket.add(node4)

    assert success is True
    assert len(bucket) == 3
    assert not bucket.has_node("dpc-node-0000000000000001")  # node1 evicted
    assert bucket.has_node("dpc-node-0000000000000004")  # node4 added


def test_kbucket_replacement_cache():
    """Test replacement cache when bucket full but head not stale."""
    bucket = KBucket(k=3)

    # Fill bucket with fresh nodes from different subnets
    node1 = create_test_node("0001", ip="10.0.1.1")
    node2 = create_test_node("0002", ip="10.0.2.2")
    node3 = create_test_node("0003", ip="10.0.3.3")

    bucket.add(node1)
    bucket.add(node2)
    bucket.add(node3)

    # Try to add 4th node (all nodes are fresh)
    node4 = create_test_node("0004", ip="10.0.4.4")
    success = bucket.add(node4)

    assert success is False  # Not added to bucket
    assert len(bucket) == 3
    assert len(bucket.replacement_cache) == 1  # Added to cache


def test_kbucket_remove_node():
    """Test removing node from k-bucket."""
    bucket = KBucket(k=5)
    node = create_test_node("0001")

    bucket.add(node)
    assert len(bucket) == 1

    success = bucket.remove("dpc-node-0000000000000001")

    assert success is True
    assert len(bucket) == 0
    assert not bucket.has_node("dpc-node-0000000000000001")


def test_kbucket_remove_promotes_replacement():
    """Test removing node promotes from replacement cache."""
    bucket = KBucket(k=2)

    # Fill bucket
    node1 = create_test_node("0001", ip="10.0.0.1")
    node2 = create_test_node("0002", ip="10.0.0.2")
    bucket.add(node1)
    bucket.add(node2)

    # Add to replacement cache
    node3 = create_test_node("0003", ip="10.0.0.3")
    bucket.add(node3)
    assert len(bucket.replacement_cache) == 1

    # Remove node1
    bucket.remove("dpc-node-0000000000000001")

    # node3 should be promoted from cache
    assert len(bucket) == 2
    assert bucket.has_node("dpc-node-0000000000000003")
    assert len(bucket.replacement_cache) == 0


def test_kbucket_subnet_diversity():
    """Test subnet diversity enforcement."""
    bucket = KBucket(k=10, subnet_diversity_limit=2)

    # Add 2 nodes from same subnet (should succeed)
    node1 = create_test_node("0001", ip="192.168.1.1")
    node2 = create_test_node("0002", ip="192.168.1.2")

    assert bucket.add(node1) is True
    assert bucket.add(node2) is True

    # Try to add 3rd node from same subnet (should fail)
    node3 = create_test_node("0003", ip="192.168.1.3")
    assert bucket.add(node3) is False

    # Node from different subnet should succeed
    node4 = create_test_node("0004", ip="10.0.0.1")
    assert bucket.add(node4) is True


def test_kbucket_needs_refresh():
    """Test refresh detection."""
    bucket = KBucket(k=5)
    node = create_test_node("0001")
    bucket.add(node)

    # Fresh bucket - doesn't need refresh
    assert not bucket.needs_refresh(interval=10)

    # Manually set old timestamp
    bucket._last_updated = time.time() - 20

    # Should need refresh with 10s interval
    assert bucket.needs_refresh(interval=10)


# ===== RoutingTable Tests =====

def test_routing_table_creation():
    """Test routing table initialization."""
    table = RoutingTable(node_id="dpc-node-0000000000000000", k=20)

    assert table.node_id == "dpc-node-0000000000000000"
    assert table.k == 20
    assert len(table.buckets) == 128
    assert table.get_node_count() == 0


def test_routing_table_add_node():
    """Test adding node to routing table."""
    table = RoutingTable(node_id="dpc-node-0000000000000000", k=20)

    success = table.add_node(
        node_id="dpc-node-0000000000000001",
        ip="10.0.0.1",
        port=8889
    )

    assert success is True
    assert table.get_node_count() == 1

    # Verify node is in correct bucket (distance 1 → bucket 0)
    node = table.get_node("dpc-node-0000000000000001")
    assert node is not None
    assert node.ip == "10.0.0.1"


def test_routing_table_cannot_add_self():
    """Test adding self to routing table raises error."""
    table = RoutingTable(node_id="dpc-node-0000000000000000", k=20)

    with pytest.raises(ValueError, match="Cannot add self"):
        table.add_node(
            node_id="dpc-node-0000000000000000",
            ip="10.0.0.1",
            port=8889
        )


def test_routing_table_remove_node():
    """Test removing node from routing table."""
    table = RoutingTable(node_id="dpc-node-0000000000000000", k=20)

    table.add_node("dpc-node-0000000000000001", "10.0.0.1", 8889)
    assert table.get_node_count() == 1

    success = table.remove_node("dpc-node-0000000000000001")

    assert success is True
    assert table.get_node_count() == 0


def test_routing_table_find_closest_nodes():
    """Test finding closest nodes to target."""
    table = RoutingTable(node_id="dpc-node-0000000000000000", k=20)

    # Add several nodes
    table.add_node("dpc-node-0000000000000001", "10.0.0.1", 8889)
    table.add_node("dpc-node-0000000000000002", "10.0.0.2", 8889)
    table.add_node("dpc-node-0000000000000005", "10.0.0.5", 8889)
    table.add_node("dpc-node-000000000000000a", "10.0.0.10", 8889)

    # Find 2 closest to target "0000000000000003"
    target = "dpc-node-0000000000000003"
    closest = table.find_closest_nodes(target, count=2)

    assert len(closest) == 2
    # Closest should be 0x02 (distance 1) and 0x01 (distance 2)
    assert closest[0].node_id == "dpc-node-0000000000000002"
    assert closest[1].node_id == "dpc-node-0000000000000001"


def test_routing_table_get_bucket_for_node():
    """Test retrieving specific bucket for node."""
    table = RoutingTable(node_id="dpc-node-0000000000000000", k=20)

    # Node at distance 1 → bucket 0
    bucket = table.get_bucket_for_node("dpc-node-0000000000000001")
    assert bucket is not None
    assert bucket == table.buckets[0]

    # Node at distance 256 (0x100) → bucket 8
    bucket = table.get_bucket_for_node("dpc-node-0000000000000100")
    assert bucket is not None
    assert bucket == table.buckets[8]


def test_routing_table_stats():
    """Test routing table statistics."""
    table = RoutingTable(node_id="dpc-node-0000000000000000", k=2)

    # Empty table
    stats = table.get_bucket_stats()
    assert stats["total_nodes"] == 0
    assert stats["empty_buckets"] == 128

    # Add nodes that fall in SAME bucket (use different subnets)
    # Node 0x02 and 0x03 both fall in bucket 1 (distances 2 and 3, both in range [2^1, 2^2) = [2, 4))
    table.add_node("dpc-node-0000000000000002", "10.0.1.1", 8889)
    table.add_node("dpc-node-0000000000000003", "10.0.2.3", 8889)

    stats = table.get_bucket_stats()
    assert stats["total_nodes"] == 2
    assert stats["full_buckets"] == 1
    assert stats["non_empty_buckets"] == 1


def test_routing_table_get_buckets_needing_refresh():
    """Test finding buckets that need periodic refresh."""
    table = RoutingTable(node_id="dpc-node-0000000000000000", k=20)

    # Add node to bucket 0
    table.add_node("dpc-node-0000000000000001", "10.0.0.1", 8889)

    # Fresh bucket - doesn't need refresh
    buckets = table.get_buckets_needing_refresh(interval=10)
    assert len(buckets) == 0

    # Manually age the bucket
    table.buckets[0]._last_updated = time.time() - 20

    # Should need refresh now
    buckets = table.get_buckets_needing_refresh(interval=10)
    assert 0 in buckets


def test_routing_table_get_all_nodes():
    """Test retrieving all nodes from routing table."""
    table = RoutingTable(node_id="dpc-node-0000000000000000", k=20)

    table.add_node("dpc-node-0000000000000001", "10.0.0.1", 8889)
    table.add_node("dpc-node-0000000000000100", "10.0.0.2", 8889)

    all_nodes = table.get_all_nodes()

    assert len(all_nodes) == 2
    node_ids = {node.node_id for node in all_nodes}
    assert "dpc-node-0000000000000001" in node_ids
    assert "dpc-node-0000000000000100" in node_ids
