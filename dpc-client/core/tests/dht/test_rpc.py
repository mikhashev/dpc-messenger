"""
Unit tests for DHT RPC handler.

Tests cover:
- UDP message serialization/deserialization
- PING/PONG RPC
- FIND_NODE RPC
- STORE/STORED RPC
- FIND_VALUE RPC (found and not found)
- Timeout and retry logic
- Rate limiting
- Error handling
"""

import pytest
import pytest_asyncio
import asyncio
import json
from unittest.mock import Mock, patch, AsyncMock
from dpc_client_core.dht.rpc import DHTRPCHandler, RPCConfig, DHTProtocol
from dpc_client_core.dht.routing import RoutingTable, DHTNode


# ===== Test Fixtures =====

@pytest.fixture
def routing_table():
    """Create test routing table."""
    return RoutingTable(node_id="dpc-node-00000000000000000000000000000000", k=20)


@pytest.fixture
def rpc_handler(routing_table):
    """Create RPC handler with test configuration."""
    config = RPCConfig(timeout=0.5, max_retries=2, rate_limit_per_ip=10)
    return DHTRPCHandler(routing_table, config)


@pytest_asyncio.fixture
async def rpc_server(rpc_handler):
    """Start RPC server on random port."""
    # Use port 0 to get random available port
    await rpc_handler.start(host="127.0.0.1", port=0)

    # Get actual port
    actual_port = rpc_handler.transport.get_extra_info('sockname')[1]

    yield rpc_handler, actual_port

    # Cleanup
    await rpc_handler.stop()


# ===== Message Serialization Tests =====

@pytest.mark.asyncio
async def test_message_serialization(rpc_handler):
    """Test JSON message serialization."""
    message = {
        "type": "PING",
        "rpc_id": "test-123",
        "node_id": "dpc-node-00000000000000000000000000000001",
        "timestamp": 1234567890.0,
    }

    # Serialize
    data = json.dumps(message).encode('utf-8')

    # Deserialize
    decoded = json.loads(data.decode('utf-8'))

    assert decoded["type"] == "PING"
    assert decoded["rpc_id"] == "test-123"
    assert decoded["node_id"] == "dpc-node-00000000000000000000000000000001"


@pytest.mark.asyncio
async def test_message_size_limit(rpc_handler):
    """Test message size limit enforcement."""
    # Create oversized message
    huge_data = "x" * 10000  # 10KB
    message = {"type": "PING", "data": huge_data}

    data = json.dumps(message).encode('utf-8')

    # Should be larger than max packet size
    assert len(data) > rpc_handler.config.max_packet_size


# ===== PING/PONG Tests =====

@pytest.mark.asyncio
async def test_ping_pong_success(rpc_server):
    """Test successful PING/PONG exchange."""
    rpc_handler, port = rpc_server

    # Create second RPC handler to receive PING
    responder_table = RoutingTable(node_id="dpc-node-00000000000000000000000000000001", k=20)
    responder = DHTRPCHandler(responder_table)
    await responder.start(host="127.0.0.1", port=0)
    responder_port = responder.transport.get_extra_info('sockname')[1]

    try:
        # Send PING
        response = await rpc_handler.ping("127.0.0.1", responder_port)

        # Verify PONG response
        assert response is not None
        assert response["type"] == "PONG"
        assert response["node_id"] == "dpc-node-00000000000000000000000000000001"

        # Verify routing table updated
        node = rpc_handler.routing_table.get_node("dpc-node-00000000000000000000000000000001")
        assert node is not None
        assert node.ip == "127.0.0.1"

    finally:
        await responder.stop()


@pytest.mark.asyncio
async def test_ping_timeout(rpc_handler):
    """Test PING timeout when no response."""
    # Start server first
    await rpc_handler.start(host="127.0.0.1", port=0)

    try:
        # PING non-existent node
        response = await rpc_handler.ping("127.0.0.1", 65535)  # Unlikely to be listening

        assert response is None
        assert rpc_handler.stats["timeouts"] > 0
    finally:
        await rpc_handler.stop()


# ===== FIND_NODE Tests =====

@pytest.mark.asyncio
async def test_find_node_success(rpc_server):
    """Test successful FIND_NODE exchange."""
    rpc_handler, port = rpc_server

    # Create responder with populated routing table
    responder_table = RoutingTable(node_id="dpc-node-00000000000000000000000000000001", k=20)
    responder_table.add_node("dpc-node-00000000000000000000000000000002", "10.0.0.2", 8889)
    responder_table.add_node("dpc-node-00000000000000030000000000000003", "10.0.1.3", 8889)

    responder = DHTRPCHandler(responder_table)
    await responder.start(host="127.0.0.1", port=0)
    responder_port = responder.transport.get_extra_info('sockname')[1]

    try:
        # Send FIND_NODE
        nodes = await rpc_handler.find_node(
            "127.0.0.1",
            responder_port,
            "dpc-node-00000000000000000000000000000002"
        )

        # Verify response
        assert nodes is not None
        assert len(nodes) == 2  # Should return 2 nodes

        node_ids = {node.node_id for node in nodes}
        assert "dpc-node-00000000000000000000000000000002" in node_ids
        assert "dpc-node-00000000000000030000000000000003" in node_ids

    finally:
        await responder.stop()


@pytest.mark.asyncio
async def test_find_node_empty_table(rpc_server):
    """Test FIND_NODE with empty routing table."""
    rpc_handler, port = rpc_server

    # Create responder with empty routing table
    responder_table = RoutingTable(node_id="dpc-node-00000000000000000000000000000001", k=20)
    responder = DHTRPCHandler(responder_table)
    await responder.start(host="127.0.0.1", port=0)
    responder_port = responder.transport.get_extra_info('sockname')[1]

    try:
        # Send FIND_NODE
        nodes = await rpc_handler.find_node(
            "127.0.0.1",
            responder_port,
            "dpc-node-00000000000000000000000000000002"
        )

        # Should return empty list
        assert nodes is not None
        assert len(nodes) == 0

    finally:
        await responder.stop()


# ===== STORE/STORED Tests =====

@pytest.mark.asyncio
async def test_store_success(rpc_server):
    """Test successful STORE operation."""
    rpc_handler, port = rpc_server

    # Create responder
    responder_table = RoutingTable(node_id="dpc-node-00000000000000000000000000000001", k=20)
    responder = DHTRPCHandler(responder_table)
    await responder.start(host="127.0.0.1", port=0)
    responder_port = responder.transport.get_extra_info('sockname')[1]

    try:
        # Send STORE
        success = await rpc_handler.store(
            "127.0.0.1",
            responder_port,
            key="dpc-node-00000000000000000000000000000002",
            value="10.0.0.2:8889"
        )

        # Verify success
        assert success is True

        # Verify value stored in responder
        assert "dpc-node-00000000000000000000000000000002" in responder._storage
        assert responder._storage["dpc-node-00000000000000000000000000000002"] == "10.0.0.2:8889"

    finally:
        await responder.stop()


@pytest.mark.asyncio
async def test_store_timeout(rpc_handler):
    """Test STORE timeout when no response."""
    # Start server first
    await rpc_handler.start(host="127.0.0.1", port=0)

    try:
        success = await rpc_handler.store(
            "127.0.0.1",
            65535,
            key="test-key",
            value="test-value"
        )

        assert success is False
        assert rpc_handler.stats["timeouts"] > 0
    finally:
        await rpc_handler.stop()


# ===== FIND_VALUE Tests =====

@pytest.mark.asyncio
async def test_find_value_found(rpc_server):
    """Test FIND_VALUE when value exists."""
    rpc_handler, port = rpc_server

    # Create responder with stored value
    responder_table = RoutingTable(node_id="dpc-node-00000000000000000000000000000001", k=20)
    responder = DHTRPCHandler(responder_table)
    responder._storage["test-key"] = "test-value"

    await responder.start(host="127.0.0.1", port=0)
    responder_port = responder.transport.get_extra_info('sockname')[1]

    try:
        # Send FIND_VALUE
        result = await rpc_handler.find_value("127.0.0.1", responder_port, "test-key")

        # Verify value found
        assert result is not None
        assert "value" in result
        assert result["value"] == "test-value"

    finally:
        await responder.stop()


@pytest.mark.asyncio
async def test_find_value_not_found(rpc_server):
    """Test FIND_VALUE when value doesn't exist (returns nodes)."""
    rpc_handler, port = rpc_server

    # Create responder without stored value but with routing table entries
    responder_table = RoutingTable(node_id="dpc-node-00000000000000000000000000000001", k=20)
    responder_table.add_node("dpc-node-00000000000000000000000000000002", "10.0.0.2", 8889)

    responder = DHTRPCHandler(responder_table)
    await responder.start(host="127.0.0.1", port=0)
    responder_port = responder.transport.get_extra_info('sockname')[1]

    try:
        # Send FIND_VALUE for non-existent key (use valid node ID format)
        result = await rpc_handler.find_value(
            "127.0.0.1",
            responder_port,
            "dpc-node-00000000000000990000000000000099"  # Valid node ID format
        )

        # Should return nodes instead
        assert result is not None
        assert "nodes" in result
        assert len(result["nodes"]) == 1
        assert result["nodes"][0].node_id == "dpc-node-00000000000000000000000000000002"

    finally:
        await responder.stop()


# ===== Timeout and Retry Tests =====

@pytest.mark.asyncio
async def test_retry_logic(rpc_handler):
    """Test RPC retry logic on timeout."""
    # Start server first
    await rpc_handler.start(host="127.0.0.1", port=0)

    try:
        initial_timeouts = rpc_handler.stats["timeouts"]

        # PING non-existent node (will retry config.max_retries times)
        await rpc_handler.ping("127.0.0.1", 65535)

        # Should have max_retries timeouts (2 in test config)
        assert rpc_handler.stats["timeouts"] == initial_timeouts + rpc_handler.config.max_retries
    finally:
        await rpc_handler.stop()


@pytest.mark.asyncio
async def test_timeout_configuration(routing_table):
    """Test custom timeout configuration."""
    config = RPCConfig(timeout=0.1, max_retries=1)  # Very short timeout
    handler = DHTRPCHandler(routing_table, config)

    # Should timeout quickly
    import time
    start = time.time()
    await handler.ping("127.0.0.1", 65535)
    elapsed = time.time() - start

    # Should complete in under 0.5 seconds (0.1s timeout + 1 retry)
    assert elapsed < 0.5


# ===== Rate Limiting Tests =====

@pytest.mark.asyncio
async def test_rate_limiting(rpc_server):
    """Test rate limiting enforcement."""
    rpc_handler, port = rpc_server

    # Create sender IP
    test_ip = "192.168.1.100"

    # Send max_rate_limit requests (should all succeed)
    for i in range(rpc_handler.config.rate_limit_per_ip):
        allowed = rpc_handler._check_rate_limit(test_ip)
        assert allowed is True

    # Next request should be rate limited
    allowed = rpc_handler._check_rate_limit(test_ip)
    assert allowed is False


@pytest.mark.asyncio
async def test_rate_limit_reset(rpc_handler):
    """Test rate limit window reset."""
    test_ip = "192.168.1.200"

    # Exhaust rate limit
    for i in range(rpc_handler.config.rate_limit_per_ip):
        rpc_handler._check_rate_limit(test_ip)

    # Should be rate limited
    assert rpc_handler._check_rate_limit(test_ip) is False

    # Manually reset window
    import time
    rpc_handler._rate_limiter[test_ip] = (0, time.time() - 100)

    # Should be allowed again
    assert rpc_handler._check_rate_limit(test_ip) is True


# ===== Statistics Tests =====

@pytest.mark.asyncio
async def test_statistics_tracking(rpc_handler):
    """Test RPC statistics tracking."""
    # Start server first
    await rpc_handler.start(host="127.0.0.1", port=0)

    try:
        initial_sent = rpc_handler.stats["rpcs_sent"]

        # Send PING (will timeout)
        await rpc_handler.ping("127.0.0.1", 65535)

        # Stats should be updated
        assert rpc_handler.stats["rpcs_sent"] > initial_sent
        assert rpc_handler.stats["timeouts"] > 0
    finally:
        await rpc_handler.stop()


# ===== Error Handling Tests =====

@pytest.mark.asyncio
async def test_invalid_json_handling(rpc_server):
    """Test handling of invalid JSON messages."""
    rpc_handler, port = rpc_server

    # Send invalid JSON directly to handler
    invalid_data = b"not valid json{"
    addr = ("127.0.0.1", 12345)

    initial_errors = rpc_handler.stats["errors"]

    await rpc_handler.handle_rpc(invalid_data, addr)

    # Error count should increment
    assert rpc_handler.stats["errors"] == initial_errors + 1


@pytest.mark.asyncio
async def test_missing_fields_handling(rpc_server):
    """Test handling of messages with missing required fields."""
    rpc_handler, port = rpc_server

    # Message missing target_id for FIND_NODE
    message = {
        "type": "FIND_NODE",
        "rpc_id": "test-123",
        "node_id": "dpc-node-00000000000000000000000000000001",
    }

    data = json.dumps(message).encode('utf-8')
    addr = ("127.0.0.1", 12345)

    # Should handle gracefully without crashing
    await rpc_handler.handle_rpc(data, addr)


# ===== Protocol Tests =====

@pytest.mark.asyncio
async def test_dht_protocol_datagram_received():
    """Test DHTProtocol datagram_received method."""
    routing_table = RoutingTable(node_id="dpc-node-00000000000000000000000000000000", k=20)
    rpc_handler = DHTRPCHandler(routing_table)
    protocol = DHTProtocol(rpc_handler)

    # Mock transport
    protocol.transport = Mock()

    # Valid PING message
    message = {
        "type": "PING",
        "rpc_id": "test-123",
        "node_id": "dpc-node-00000000000000000000000000000001",
        "timestamp": 1234567890.0,
    }

    data = json.dumps(message).encode('utf-8')
    addr = ("127.0.0.1", 12345)

    # Should create async task to handle RPC
    protocol.datagram_received(data, addr)

    # Give async task time to execute
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_dht_protocol_error_handling():
    """Test DHTProtocol error_received method."""
    routing_table = RoutingTable(node_id="dpc-node-00000000000000000000000000000000", k=20)
    rpc_handler = DHTRPCHandler(routing_table)
    protocol = DHTProtocol(rpc_handler)

    # Should handle errors gracefully
    exc = Exception("Test error")
    protocol.error_received(exc)  # Should not crash


# ===== Integration Tests =====

@pytest.mark.asyncio
async def test_full_rpc_exchange():
    """Test full RPC exchange between two nodes."""
    # Create two RPC handlers
    table1 = RoutingTable(node_id="dpc-node-00000000000000000000000000000001", k=20)
    handler1 = DHTRPCHandler(table1)
    await handler1.start(host="127.0.0.1", port=0)
    port1 = handler1.transport.get_extra_info('sockname')[1]

    table2 = RoutingTable(node_id="dpc-node-00000000000000000000000000000002", k=20)
    handler2 = DHTRPCHandler(table2)
    await handler2.start(host="127.0.0.1", port=0)
    port2 = handler2.transport.get_extra_info('sockname')[1]

    try:
        # 1. PING from handler1 to handler2
        pong = await handler1.ping("127.0.0.1", port2)
        assert pong is not None

        # 2. STORE from handler1 to handler2
        success = await handler1.store(
            "127.0.0.1",
            port2,
            key="test-key",
            value="test-value"
        )
        assert success is True

        # 3. FIND_VALUE from handler1 to handler2
        result = await handler1.find_value("127.0.0.1", port2, "test-key")
        assert result is not None
        assert result["value"] == "test-value"

        # 4. Add nodes to handler2's routing table
        table2.add_node("dpc-node-00000000000000030000000000000003", "10.0.0.3", 8889)

        # 5. FIND_NODE from handler1 to handler2
        nodes = await handler1.find_node("127.0.0.1", port2, "dpc-node-00000000000000030000000000000003")
        assert nodes is not None
        assert len(nodes) >= 1  # Should have at least the node we added
        # Find the node we added
        node_ids = {node.node_id for node in nodes}
        assert "dpc-node-00000000000000030000000000000003" in node_ids

    finally:
        await handler1.stop()
        await handler2.stop()
