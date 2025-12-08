"""
UDP Peer Connection - DTLS-Encrypted Wrapper

Provides a PeerConnection-compatible interface over DTLS-encrypted UDP transport.
This wrapper makes DTLS connections look like regular PeerConnection instances,
allowing seamless integration with existing message routing infrastructure.

Architecture:
1. Wraps DTLSPeerConnection (DTLS over UDP)
2. Implements same interface as PeerConnection (send/read/close)
3. Handles DPTP protocol framing (10-byte header + JSON payload)
4. Provides connection_type = "udp_dtls" for UI display

Usage:
    >>> # After successful UDP hole punch + DTLS handshake
    >>> dtls_conn = DTLSPeerConnection(...)
    >>> await dtls_conn.connect()
    >>>
    >>> # Wrap in UDPPeerConnection
    >>> peer_conn = UDPPeerConnection(node_id="dpc-node-abc123...", dtls_conn=dtls_conn)
    >>>
    >>> # Use like regular PeerConnection
    >>> await peer_conn.send({"command": "HELLO", "payload": {...}})
    >>> response = await peer_conn.read()
    >>> await peer_conn.close()
"""

import asyncio
import json
import logging
from typing import Dict, Any, Optional

from .dtls_connection import DTLSPeerConnection, DTLSHandshakeError

logger = logging.getLogger(__name__)


class UDPPeerConnection:
    """
    PeerConnection-compatible wrapper for DTLS-encrypted UDP connections.

    Provides the same interface as PeerConnection (send/read/close) but uses
    DTLSPeerConnection underneath for encrypted UDP transport.

    This allows UDP hole punch connections to work with existing message
    routing infrastructure without changes.

    Attributes:
        node_id: Remote peer's node identifier
        dtls_conn: Underlying DTLS connection
        connection_type: "udp_dtls" (for UI display)
    """

    def __init__(self, node_id: str, dtls_conn: DTLSPeerConnection):
        """
        Initialize UDP peer connection wrapper.

        Args:
            node_id: Remote peer's node identifier
            dtls_conn: Connected DTLS connection (must be already connected)

        Raises:
            RuntimeError: If DTLS connection is not connected
        """
        if not dtls_conn.is_connected:
            raise RuntimeError("DTLS connection must be connected before wrapping")

        self.node_id = node_id
        self.dtls_conn = dtls_conn
        self.connection_type = "udp_dtls"

        logger.info(f"UDPPeerConnection created for {node_id[:20]}")

    async def send(self, message: Dict[str, Any]) -> None:
        """
        Send message over DTLS-encrypted UDP.

        Implements DPTP protocol framing:
        1. Convert message dict to JSON
        2. Calculate payload length
        3. Send 10-byte ASCII header (payload length)
        4. Send JSON payload

        Args:
            message: Message dictionary to send

        Raises:
            DTLSHandshakeError: If send fails
        """
        try:
            # Serialize message to JSON bytes
            payload = json.dumps(message).encode('utf-8')
            payload_length = len(payload)

            # Create 10-byte ASCII header (DPTP protocol)
            header = f"{payload_length:010d}".encode('ascii')

            # Send header + payload over DTLS
            await self.dtls_conn.send_message(header + payload)

            logger.debug(
                f"UDP-DTLS sent message to {self.node_id[:20]}: "
                f"command={message.get('command', 'unknown')}, "
                f"size={payload_length}"
            )

        except DTLSHandshakeError as e:
            logger.error(f"Failed to send message over UDP-DTLS to {self.node_id[:20]}: {e}")
            raise

        except Exception as e:
            logger.error(f"Unexpected error sending UDP-DTLS message to {self.node_id[:20]}: {e}")
            raise

    async def read(self) -> Optional[Dict[str, Any]]:
        """
        Read message from DTLS-encrypted UDP.

        Implements DPTP protocol framing:
        1. Read 10-byte ASCII header (payload length)
        2. Parse payload length
        3. Read that many bytes for JSON payload
        4. Deserialize JSON to dict

        Returns:
            Message dictionary, or None if connection closed

        Raises:
            DTLSHandshakeError: If receive fails
        """
        try:
            # Read 10-byte header (DPTP protocol)
            header_data = await self.dtls_conn.receive_message(max_size=10)

            if not header_data or len(header_data) == 0:
                # Connection closed gracefully
                logger.info(f"Connection closed by peer {self.node_id[:20]}")
                return None

            if len(header_data) != 10:
                logger.warning(
                    f"Protocol error: expected 10-byte header, got {len(header_data)} bytes "
                    f"from {self.node_id[:20]}"
                )
                return None

            # Parse payload length from header
            try:
                payload_length = int(header_data.decode('ascii'))
            except ValueError as e:
                logger.warning(
                    f"Protocol error: invalid header from {self.node_id[:20]}: {header_data!r}"
                )
                return None

            # Read payload
            payload_data = await self.dtls_conn.receive_message(max_size=payload_length)

            if not payload_data or len(payload_data) != payload_length:
                logger.warning(
                    f"Protocol error: expected {payload_length} bytes, got {len(payload_data) if payload_data else 0} "
                    f"from {self.node_id[:20]}"
                )
                return None

            # Deserialize JSON
            try:
                message = json.loads(payload_data.decode('utf-8'))
                logger.debug(
                    f"UDP-DTLS received message from {self.node_id[:20]}: "
                    f"command={message.get('command', 'unknown')}, "
                    f"size={payload_length}"
                )
                return message

            except json.JSONDecodeError as e:
                logger.warning(
                    f"Protocol error: invalid JSON from {self.node_id[:20]}: {e}"
                )
                return None

        except DTLSHandshakeError as e:
            logger.error(f"Failed to read message over UDP-DTLS from {self.node_id[:20]}: {e}")
            return None

        except Exception as e:
            logger.error(f"Unexpected error reading UDP-DTLS message from {self.node_id[:20]}: {e}")
            return None

    async def close(self) -> None:
        """
        Close DTLS-encrypted UDP connection gracefully.

        Sends SSL shutdown alert and closes the DTLS connection.
        The underlying UDP socket remains open (caller's responsibility).
        """
        try:
            await self.dtls_conn.close()
            logger.info(f"UDP-DTLS connection closed to {self.node_id[:20]}")

        except Exception as e:
            logger.warning(f"Error closing UDP-DTLS connection to {self.node_id[:20]}: {e}")

    def __repr__(self) -> str:
        return (
            f"UDPPeerConnection(node_id={self.node_id[:20]}, "
            f"type={self.connection_type}, "
            f"connected={self.dtls_conn.is_connected})"
        )
