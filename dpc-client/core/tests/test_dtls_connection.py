# tests/test_dtls_connection.py

"""
Unit tests for DTLS (Datagram Transport Layer Security) connection implementation.

Tests cover:
- DTLS handshake success/failure scenarios
- Certificate validation and node_id verification
- Encrypted send/receive operations
- Timeout handling
- Error recovery and graceful degradation
"""

import asyncio
import pytest
import socket
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from pathlib import Path
from OpenSSL import SSL

from dpc_client_core.transports import (
    DTLSPeerConnection,
    DTLSHandshakeError,
    DTLSCertificateError,
    UDPPeerConnection,
)


class TestDTLSPeerConnection:
    """Test DTLSPeerConnection class for secure UDP transport."""

    @pytest.fixture
    def mock_udp_socket(self):
        """Create mock UDP socket for testing."""
        mock_sock = Mock(spec=socket.socket)
        mock_sock.family = socket.AF_INET
        mock_sock.type = socket.SOCK_DGRAM
        return mock_sock

    @pytest.fixture
    def remote_addr(self):
        """Remote peer address for testing."""
        return ("192.0.2.1", 8890)

    @pytest.fixture
    def expected_node_id(self):
        """Expected peer node identifier."""
        return "dpc-node-abc123456789"

    @pytest.fixture
    def mock_identity(self, tmp_path):
        """Mock node identity with temp certificate files."""
        node_id = "dpc-node-test-local"
        key_path = tmp_path / "node.key"
        cert_path = tmp_path / "node.crt"

        # Create dummy files (not real certs, just for file existence checks)
        key_path.write_text("MOCK_PRIVATE_KEY")
        cert_path.write_text("MOCK_CERTIFICATE")

        return (node_id, str(key_path), str(cert_path))

    @pytest.mark.asyncio
    async def test_dtls_init_client_mode(
        self, mock_udp_socket, remote_addr, expected_node_id, mock_identity
    ):
        """Test DTLS connection initialization in client mode."""
        with patch('dpc_client_core.transports.dtls_connection.load_identity', return_value=mock_identity):
            dtls_conn = DTLSPeerConnection(
                udp_socket=mock_udp_socket,
                remote_addr=remote_addr,
                expected_node_id=expected_node_id,
                is_server=False,
                handshake_timeout=3.0
            )

            assert dtls_conn.udp_socket == mock_udp_socket
            assert dtls_conn.remote_addr == remote_addr
            assert dtls_conn.expected_node_id == expected_node_id
            assert dtls_conn.is_server is False
            assert dtls_conn.handshake_timeout == 3.0
            assert dtls_conn.is_connected is False

    @pytest.mark.asyncio
    async def test_dtls_init_server_mode(
        self, mock_udp_socket, remote_addr, expected_node_id, mock_identity
    ):
        """Test DTLS connection initialization in server mode."""
        with patch('dpc_client_core.transports.dtls_connection.load_identity', return_value=mock_identity):
            dtls_conn = DTLSPeerConnection(
                udp_socket=mock_udp_socket,
                remote_addr=remote_addr,
                expected_node_id=expected_node_id,
                is_server=True,
                handshake_timeout=5.0
            )

            assert dtls_conn.is_server is True
            assert dtls_conn.handshake_timeout == 5.0

    @pytest.mark.asyncio
    async def test_dtls_handshake_success(
        self, mock_udp_socket, remote_addr, expected_node_id, mock_identity
    ):
        """Test successful DTLS handshake."""
        with patch('dpc_client_core.transports.dtls_connection.load_identity', return_value=mock_identity):
            # Mock SSL.Connection and handshake
            mock_ssl_conn = Mock(spec=SSL.Connection)
            mock_ssl_conn.do_handshake = Mock()  # Successful handshake (no exception)

            # Mock SSL.Context creation
            mock_ctx = Mock(spec=SSL.Context)

            dtls_conn = DTLSPeerConnection(
                udp_socket=mock_udp_socket,
                remote_addr=remote_addr,
                expected_node_id=expected_node_id,
                is_server=False
            )

            # Patch SSL.Context and SSL.Connection
            with patch('dpc_client_core.transports.dtls_connection.SSL.Context', return_value=mock_ctx), \
                 patch('dpc_client_core.transports.dtls_connection.SSL.Connection', return_value=mock_ssl_conn):

                # Perform handshake
                await dtls_conn.connect(timeout=3.0)

                # Verify handshake was called
                mock_ssl_conn.do_handshake.assert_called_once()
                assert dtls_conn.is_connected is True

    @pytest.mark.asyncio
    async def test_dtls_handshake_timeout(
        self, mock_udp_socket, remote_addr, expected_node_id, mock_identity
    ):
        """Test DTLS handshake timeout handling."""
        import time

        with patch('dpc_client_core.transports.dtls_connection.load_identity', return_value=mock_identity):
            # Mock SSL.Connection with slow handshake
            mock_ssl_conn = Mock(spec=SSL.Connection)

            # Use synchronous sleep (run_in_executor expects sync function)
            def slow_handshake():
                time.sleep(10)  # Longer than timeout

            mock_ssl_conn.do_handshake = slow_handshake

            mock_ctx = Mock(spec=SSL.Context)

            dtls_conn = DTLSPeerConnection(
                udp_socket=mock_udp_socket,
                remote_addr=remote_addr,
                expected_node_id=expected_node_id,
                is_server=False
            )

            with patch('dpc_client_core.transports.dtls_connection.SSL.Context', return_value=mock_ctx), \
                 patch('dpc_client_core.transports.dtls_connection.SSL.Connection', return_value=mock_ssl_conn):

                # Should raise DTLSHandshakeError due to timeout
                with pytest.raises(DTLSHandshakeError, match="timeout"):
                    await dtls_conn.connect(timeout=0.5)

                assert dtls_conn.is_connected is False

    @pytest.mark.asyncio
    async def test_dtls_certificate_validation_failure(
        self, mock_udp_socket, remote_addr, mock_identity
    ):
        """Test certificate validation failure (node_id mismatch)."""
        # Expected node_id doesn't match what peer will present
        expected_node_id = "dpc-node-expected-abc"
        actual_node_id = "dpc-node-different-xyz"

        with patch('dpc_client_core.transports.dtls_connection.load_identity', return_value=mock_identity):
            mock_ssl_conn = Mock(spec=SSL.Connection)

            # Simulate certificate verification failure
            def handshake_with_cert_error():
                raise SSL.Error([('SSL', 'ssl_handshake', 'certificate verify failed')])

            mock_ssl_conn.do_handshake = handshake_with_cert_error
            mock_ctx = Mock(spec=SSL.Context)

            dtls_conn = DTLSPeerConnection(
                udp_socket=mock_udp_socket,
                remote_addr=remote_addr,
                expected_node_id=expected_node_id,
                is_server=False
            )

            with patch('dpc_client_core.transports.dtls_connection.SSL.Context', return_value=mock_ctx), \
                 patch('dpc_client_core.transports.dtls_connection.SSL.Connection', return_value=mock_ssl_conn):

                # Should raise DTLSCertificateError
                with pytest.raises(DTLSCertificateError, match="certificate validation failed"):
                    await dtls_conn.connect(timeout=3.0)

    @pytest.mark.asyncio
    async def test_dtls_send_message(
        self, mock_udp_socket, remote_addr, expected_node_id, mock_identity
    ):
        """Test sending encrypted message over DTLS."""
        with patch('dpc_client_core.transports.dtls_connection.load_identity', return_value=mock_identity):
            mock_ssl_conn = Mock(spec=SSL.Connection)
            mock_ssl_conn.send = Mock(return_value=None)
            mock_ctx = Mock(spec=SSL.Context)

            dtls_conn = DTLSPeerConnection(
                udp_socket=mock_udp_socket,
                remote_addr=remote_addr,
                expected_node_id=expected_node_id,
                is_server=False
            )

            with patch('dpc_client_core.transports.dtls_connection.SSL.Context', return_value=mock_ctx), \
                 patch('dpc_client_core.transports.dtls_connection.SSL.Connection', return_value=mock_ssl_conn):

                # Simulate connected state
                await dtls_conn.connect(timeout=3.0)

                # Send message
                test_data = b"Hello, encrypted world!"
                await dtls_conn.send_message(test_data)

                # Verify send was called
                mock_ssl_conn.send.assert_called_once_with(test_data)

    @pytest.mark.asyncio
    async def test_dtls_receive_message(
        self, mock_udp_socket, remote_addr, expected_node_id, mock_identity
    ):
        """Test receiving encrypted message over DTLS."""
        with patch('dpc_client_core.transports.dtls_connection.load_identity', return_value=mock_identity):
            mock_ssl_conn = Mock(spec=SSL.Connection)

            # Mock receive data
            test_data = b"Encrypted response"
            mock_ssl_conn.recv = Mock(return_value=test_data)
            mock_ctx = Mock(spec=SSL.Context)

            dtls_conn = DTLSPeerConnection(
                udp_socket=mock_udp_socket,
                remote_addr=remote_addr,
                expected_node_id=expected_node_id,
                is_server=False
            )

            with patch('dpc_client_core.transports.dtls_connection.SSL.Context', return_value=mock_ctx), \
                 patch('dpc_client_core.transports.dtls_connection.SSL.Connection', return_value=mock_ssl_conn):

                # Simulate connected state
                await dtls_conn.connect(timeout=3.0)

                # Receive message
                received_data = await dtls_conn.receive_message(max_size=1024)

                # Verify
                assert received_data == test_data
                mock_ssl_conn.recv.assert_called_once_with(1024)

    @pytest.mark.asyncio
    async def test_dtls_send_without_connection(
        self, mock_udp_socket, remote_addr, expected_node_id, mock_identity
    ):
        """Test sending message before DTLS handshake raises error."""
        with patch('dpc_client_core.transports.dtls_connection.load_identity', return_value=mock_identity):
            dtls_conn = DTLSPeerConnection(
                udp_socket=mock_udp_socket,
                remote_addr=remote_addr,
                expected_node_id=expected_node_id,
                is_server=False
            )

            # Try to send without connecting
            with pytest.raises(RuntimeError, match="not connected"):
                await dtls_conn.send_message(b"test")

    @pytest.mark.asyncio
    async def test_dtls_close(
        self, mock_udp_socket, remote_addr, expected_node_id, mock_identity
    ):
        """Test graceful DTLS connection closure."""
        with patch('dpc_client_core.transports.dtls_connection.load_identity', return_value=mock_identity):
            mock_ssl_conn = Mock(spec=SSL.Connection)
            mock_ssl_conn.shutdown = Mock()
            mock_ctx = Mock(spec=SSL.Context)

            dtls_conn = DTLSPeerConnection(
                udp_socket=mock_udp_socket,
                remote_addr=remote_addr,
                expected_node_id=expected_node_id,
                is_server=False
            )

            with patch('dpc_client_core.transports.dtls_connection.SSL.Context', return_value=mock_ctx), \
                 patch('dpc_client_core.transports.dtls_connection.SSL.Connection', return_value=mock_ssl_conn):

                # Connect and close
                await dtls_conn.connect(timeout=3.0)
                await dtls_conn.close()

                # Verify shutdown was called
                mock_ssl_conn.shutdown.assert_called_once()
                assert dtls_conn.is_connected is False


class TestUDPPeerConnection:
    """Test UDPPeerConnection wrapper for DTLS connections."""

    @pytest.fixture
    def mock_dtls_connected(self):
        """Create mock connected DTLS connection."""
        mock_dtls = Mock(spec=DTLSPeerConnection)
        mock_dtls.is_connected = True
        mock_dtls.send_message = AsyncMock()
        mock_dtls.receive_message = AsyncMock()
        mock_dtls.close = AsyncMock()
        return mock_dtls

    @pytest.fixture
    def node_id(self):
        """Test node identifier."""
        return "dpc-node-test-peer-123"

    def test_udp_peer_connection_init_success(self, node_id, mock_dtls_connected):
        """Test UDPPeerConnection initialization with connected DTLS."""
        peer_conn = UDPPeerConnection(node_id=node_id, dtls_conn=mock_dtls_connected)

        assert peer_conn.node_id == node_id
        assert peer_conn.dtls_conn == mock_dtls_connected
        assert peer_conn.connection_type == "udp_dtls"

    def test_udp_peer_connection_init_not_connected(self, node_id):
        """Test UDPPeerConnection initialization fails if DTLS not connected."""
        mock_dtls = Mock(spec=DTLSPeerConnection)
        mock_dtls.is_connected = False

        with pytest.raises(RuntimeError, match="must be connected"):
            UDPPeerConnection(node_id=node_id, dtls_conn=mock_dtls)

    @pytest.mark.asyncio
    async def test_udp_peer_send_message(self, node_id, mock_dtls_connected):
        """Test sending message via UDPPeerConnection (DPTP protocol)."""
        peer_conn = UDPPeerConnection(node_id=node_id, dtls_conn=mock_dtls_connected)

        # Send test message
        test_message = {"command": "HELLO", "payload": {"greeting": "Hi!"}}
        await peer_conn.send(test_message)

        # Verify DTLS send was called with proper framing
        mock_dtls_connected.send_message.assert_called_once()
        call_args = mock_dtls_connected.send_message.call_args[0][0]

        # Check 10-byte header
        assert len(call_args) > 10
        header = call_args[:10]
        assert header.decode('ascii').isdigit()

        # Check payload
        import json
        payload_length = int(header.decode('ascii'))
        payload = call_args[10:]
        assert len(payload) == payload_length

        decoded_message = json.loads(payload.decode('utf-8'))
        assert decoded_message == test_message

    @pytest.mark.asyncio
    async def test_udp_peer_read_message(self, node_id, mock_dtls_connected):
        """Test reading message via UDPPeerConnection (DPTP protocol)."""
        import json

        # Mock DTLS receive to return framed message
        test_message = {"command": "SEND_TEXT", "payload": {"text": "Hello!"}}
        payload = json.dumps(test_message).encode('utf-8')
        header = f"{len(payload):010d}".encode('ascii')

        # First call returns header, second returns payload
        mock_dtls_connected.receive_message.side_effect = [header, payload]

        peer_conn = UDPPeerConnection(node_id=node_id, dtls_conn=mock_dtls_connected)

        # Read message
        received_message = await peer_conn.read()

        # Verify
        assert received_message == test_message
        assert mock_dtls_connected.receive_message.call_count == 2

    @pytest.mark.asyncio
    async def test_udp_peer_read_connection_closed(self, node_id, mock_dtls_connected):
        """Test reading when connection is closed gracefully."""
        # Mock empty response (connection closed)
        mock_dtls_connected.receive_message.return_value = b""

        peer_conn = UDPPeerConnection(node_id=node_id, dtls_conn=mock_dtls_connected)

        # Should return None for closed connection
        result = await peer_conn.read()
        assert result is None

    @pytest.mark.asyncio
    async def test_udp_peer_read_invalid_header(self, node_id, mock_dtls_connected):
        """Test reading with invalid header (protocol error)."""
        # Mock invalid header (not 10 bytes)
        mock_dtls_connected.receive_message.return_value = b"12345"

        peer_conn = UDPPeerConnection(node_id=node_id, dtls_conn=mock_dtls_connected)

        # Should return None for protocol error
        result = await peer_conn.read()
        assert result is None

    @pytest.mark.asyncio
    async def test_udp_peer_read_invalid_json(self, node_id, mock_dtls_connected):
        """Test reading with invalid JSON payload."""
        # Mock valid header but invalid JSON
        invalid_payload = b"not valid json {"
        header = f"{len(invalid_payload):010d}".encode('ascii')

        mock_dtls_connected.receive_message.side_effect = [header, invalid_payload]

        peer_conn = UDPPeerConnection(node_id=node_id, dtls_conn=mock_dtls_connected)

        # Should return None for invalid JSON
        result = await peer_conn.read()
        assert result is None

    @pytest.mark.asyncio
    async def test_udp_peer_close(self, node_id, mock_dtls_connected):
        """Test closing UDPPeerConnection."""
        peer_conn = UDPPeerConnection(node_id=node_id, dtls_conn=mock_dtls_connected)

        # Close connection
        await peer_conn.close()

        # Verify DTLS close was called
        mock_dtls_connected.close.assert_called_once()


class TestDTLSIntegration:
    """Integration tests for DTLS with hole punch strategy."""

    @pytest.mark.asyncio
    async def test_dtls_handshake_with_real_sockets(self, tmp_path):
        """
        Integration test: DTLS handshake with real UDP sockets (loopback).

        Note: This test uses localhost loopback, not real network NAT traversal.
        For real NAT testing, see MANUAL_TESTING_GUIDE.md
        """
        # Skip this test in CI (requires real certificates and OpenSSL)
        pytest.skip("Integration test requires real certificates - see MANUAL_TESTING_GUIDE.md")

        # TODO: Implement integration test with real socket pair on loopback
        # This would require:
        # 1. Generate real X.509 certificates for testing
        # 2. Create two UDP sockets on loopback
        # 3. Perform DTLS handshake between them
        # 4. Verify encrypted communication
        # 5. Clean up sockets

    @pytest.mark.asyncio
    async def test_fallback_to_relay_on_dtls_failure(self):
        """
        Test that connection strategy falls back to relay when DTLS fails.

        This is tested in connection_strategies/test_udp_hole_punch.py
        """
        pytest.skip("Fallback logic tested in connection strategy tests")


# Run tests with: poetry run pytest tests/test_dtls_connection.py -v
