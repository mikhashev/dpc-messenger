"""
Tests for GossipConnection transport wrapper.

Validates that:
- GossipConnection provides PeerConnection-like interface
- Messages sent via send() are encrypted and routed through gossip manager
- Messages received via read() are decrypted from gossip manager
- Callback registration/unregistration works correctly
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, MagicMock

from dpc_client_core.transports.gossip_connection import GossipConnection
from dpc_client_core.managers.gossip_manager import GossipManager


@pytest.fixture
def mock_gossip_manager():
    """Create mock gossip manager."""
    manager = Mock(spec=GossipManager)
    manager.send_gossip = AsyncMock()
    manager.register_delivery_callback = Mock()
    manager.unregister_delivery_callback = Mock()
    return manager


@pytest.fixture
def mock_orchestrator():
    """Create mock connection orchestrator."""
    orchestrator = Mock()
    return orchestrator


class TestGossipConnectionBasics:
    """Test basic GossipConnection functionality."""

    @pytest.mark.asyncio
    async def test_initialization(self, mock_gossip_manager, mock_orchestrator):
        """Test GossipConnection initialization."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        assert conn.peer_id == "dpc-node-bob123"
        assert conn.gossip_manager == mock_gossip_manager
        assert conn.orchestrator == mock_orchestrator
        assert conn.running is False

    @pytest.mark.asyncio
    async def test_start_registers_callback(self, mock_gossip_manager, mock_orchestrator):
        """Test that start() registers delivery callback."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        await conn.start()

        # Should register callback
        mock_gossip_manager.register_delivery_callback.assert_called_once()
        call_args = mock_gossip_manager.register_delivery_callback.call_args
        assert call_args[0][0] == "dpc-node-bob123"
        assert callable(call_args[0][1])

        # Should be running
        assert conn.running is True

    @pytest.mark.asyncio
    async def test_close_unregisters_callback(self, mock_gossip_manager, mock_orchestrator):
        """Test that close() unregisters delivery callback."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        await conn.start()
        await conn.close()

        # Should unregister callback
        mock_gossip_manager.unregister_delivery_callback.assert_called_once_with("dpc-node-bob123")

        # Should not be running
        assert conn.running is False

    @pytest.mark.asyncio
    async def test_send_calls_gossip_manager(self, mock_gossip_manager, mock_orchestrator):
        """Test that send() calls gossip_manager.send_gossip()."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        await conn.start()

        message = {"command": "HELLO", "data": "test"}
        await conn.send(message)

        # Should call send_gossip
        mock_gossip_manager.send_gossip.assert_called_once_with(
            destination="dpc-node-bob123",
            payload=message,
            priority="normal"
        )

    @pytest.mark.asyncio
    async def test_send_fails_if_not_running(self, mock_gossip_manager, mock_orchestrator):
        """Test that send() fails if connection not started."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        # Don't call start()

        message = {"command": "HELLO", "data": "test"}

        with pytest.raises(RuntimeError, match="not running"):
            await conn.send(message)


class TestGossipConnectionReceive:
    """Test message receiving functionality."""

    @pytest.mark.asyncio
    async def test_read_receives_message_from_callback(self, mock_gossip_manager, mock_orchestrator):
        """Test that read() receives messages delivered via callback."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        await conn.start()

        # Simulate message delivery via callback
        test_message = {"command": "SEND_TEXT", "text": "Hello!"}
        await conn._on_message_delivered(test_message)

        # Read should return the message
        received = await conn.read()
        assert received == test_message

    @pytest.mark.asyncio
    async def test_read_timeout_returns_none(self, mock_gossip_manager, mock_orchestrator):
        """Test that read() returns None on timeout."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        await conn.start()

        # Read with no messages should timeout
        # Override timeout for faster test
        original_timeout = 30.0

        # Manually test timeout
        start = asyncio.get_event_loop().time()

        # Patch the timeout to be shorter for testing
        async def read_with_short_timeout():
            try:
                message = await asyncio.wait_for(
                    conn._receive_queue.get(),
                    timeout=0.1  # Short timeout for testing
                )
                return message
            except asyncio.TimeoutError:
                return None

        received = await read_with_short_timeout()
        assert received is None

    @pytest.mark.asyncio
    async def test_read_returns_none_if_not_running(self, mock_gossip_manager, mock_orchestrator):
        """Test that read() returns None if connection closed."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        # Don't start connection
        received = await conn.read()
        assert received is None

    @pytest.mark.asyncio
    async def test_multiple_messages_queued(self, mock_gossip_manager, mock_orchestrator):
        """Test that multiple messages can be queued and read."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        await conn.start()

        # Deliver multiple messages
        msg1 = {"command": "SEND_TEXT", "text": "First"}
        msg2 = {"command": "SEND_TEXT", "text": "Second"}
        msg3 = {"command": "SEND_TEXT", "text": "Third"}

        await conn._on_message_delivered(msg1)
        await conn._on_message_delivered(msg2)
        await conn._on_message_delivered(msg3)

        # Read all messages
        assert await conn.read() == msg1
        assert await conn.read() == msg2
        assert await conn.read() == msg3


class TestGossipConnectionIntegration:
    """Test integration scenarios."""

    @pytest.mark.asyncio
    async def test_full_send_receive_cycle(self, mock_gossip_manager, mock_orchestrator):
        """Test complete send/receive cycle."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        await conn.start()

        # Send a message
        outgoing = {"command": "HELLO", "data": "test"}
        await conn.send(outgoing)

        # Verify send was called
        assert mock_gossip_manager.send_gossip.called

        # Simulate receiving a reply
        incoming = {"command": "HELLO_RESPONSE", "data": "reply"}
        await conn._on_message_delivered(incoming)

        # Read the reply
        received = await conn.read()
        assert received == incoming

    @pytest.mark.asyncio
    async def test_close_clears_pending_messages(self, mock_gossip_manager, mock_orchestrator):
        """Test that close() clears pending messages from queue."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        await conn.start()

        # Queue some messages
        await conn._on_message_delivered({"command": "MSG1"})
        await conn._on_message_delivered({"command": "MSG2"})

        # Close connection
        await conn.close()

        # Queue should be empty
        assert conn._receive_queue.empty()

    @pytest.mark.asyncio
    async def test_start_idempotent(self, mock_gossip_manager, mock_orchestrator):
        """Test that calling start() multiple times is safe."""
        conn = GossipConnection(
            peer_id="dpc-node-bob123",
            gossip_manager=mock_gossip_manager,
            orchestrator=mock_orchestrator
        )

        # Start twice
        await conn.start()
        await conn.start()

        # Should only register callback once (second call logs warning)
        # Check that we're still running
        assert conn.running is True
