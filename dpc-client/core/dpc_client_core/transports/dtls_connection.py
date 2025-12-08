"""
DTLS Peer Connection - Secure UDP Transport

Provides DTLS (Datagram Transport Layer Security) encryption for UDP connections,
specifically for hole-punched UDP sockets. This enables encrypted peer-to-peer
communication without requiring TURN servers.

Based on the aiortc DTLS implementation pattern, but simplified for our use case
(no SRTP, no RTP routing - just encrypted message passing).

Architecture:
1. Load existing node.crt/node.key from ~/.dpc/
2. Create SSL.Context with DTLS_METHOD (DTLS 1.2+)
3. Wrap UDP socket with SSL.Connection
4. Perform DTLS handshake (asyncio-wrapped)
5. Send/receive encrypted messages

Security Features:
- DTLS 1.2+ encryption
- Certificate-based authentication (node_id validation)
- Strong cipher suites (AES-GCM, ChaCha20-Poly1305)
- Perfect forward secrecy
"""

import asyncio
import logging
import socket
from pathlib import Path
from typing import Optional, Tuple

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from OpenSSL import SSL

from dpc_protocol.crypto import load_identity

logger = logging.getLogger(__name__)

# Strong cipher suites (from aiortc)
# Priority: AES-GCM (hardware accelerated) > ChaCha20-Poly1305 > AES-SHA
DTLS_CIPHER_SUITES = b"ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-ECDSA-AES128-SHA:ECDHE-ECDSA-AES256-SHA"


class DTLSHandshakeError(Exception):
    """Raised when DTLS handshake fails"""
    pass


class DTLSCertificateError(Exception):
    """Raised when peer certificate validation fails"""
    pass


class DTLSPeerConnection:
    """
    DTLS-encrypted UDP connection wrapper.

    Wraps a punched UDP socket with DTLS encryption for secure peer-to-peer
    communication. Uses existing node certificates from ~/.dpc/ for authentication.

    Example:
        >>> # After successful UDP hole punch
        >>> dtls_conn = DTLSPeerConnection(
        ...     udp_socket=punched_socket,
        ...     remote_addr=("192.0.2.1", 12345),
        ...     expected_node_id="dpc-node-abc123...",
        ...     is_server=False
        ... )
        >>> await dtls_conn.connect(timeout=3.0)
        >>> await dtls_conn.send_message(b"Hello, encrypted world!")
        >>> response = await dtls_conn.receive_message()
        >>> await dtls_conn.close()
    """

    def __init__(
        self,
        udp_socket: socket.socket,
        remote_addr: Tuple[str, int],
        expected_node_id: str,
        is_server: bool = False,
        handshake_timeout: float = 3.0
    ):
        """
        Initialize DTLS connection wrapper.

        Args:
            udp_socket: Punched UDP socket (already connected to remote)
            remote_addr: Remote peer address (ip, port)
            expected_node_id: Expected peer node_id (for certificate validation)
            is_server: True if we should act as DTLS server (accept), False for client (connect)
            handshake_timeout: Timeout for DTLS handshake in seconds
        """
        self.udp_socket = udp_socket
        self.remote_addr = remote_addr
        self.expected_node_id = expected_node_id
        self.is_server = is_server
        self.handshake_timeout = handshake_timeout

        # DTLS connection state
        self._ssl: Optional[SSL.Connection] = None
        self._is_connected = False
        self._loop = asyncio.get_event_loop()

        # Load our node identity
        self.node_id, self.key_path, self.cert_path = load_identity()
        logger.info(
            f"DTLS init: local={self.node_id}, remote={expected_node_id}, "
            f"role={'server' if is_server else 'client'}"
        )

    def _create_ssl_context(self) -> SSL.Context:
        """
        Create SSL context for DTLS.

        Uses DTLS 1.2+ with strong cipher suites and our node certificate.
        Pattern copied from aiortc's RTCCertificate._create_context().

        Returns:
            SSL.Context configured for DTLS
        """
        # Use DTLS method (DTLS 1.2+)
        ctx = SSL.Context(SSL.DTLS_METHOD)

        # Load our certificate and private key
        ctx.use_certificate_file(str(self.cert_path))
        ctx.use_privatekey_file(str(self.key_path))

        # Set strong cipher suites
        ctx.set_cipher_list(DTLS_CIPHER_SUITES)

        # Require peer certificate (mutual TLS)
        ctx.set_verify(
            SSL.VERIFY_PEER | SSL.VERIFY_FAIL_IF_NO_PEER_CERT,
            self._verify_callback
        )

        logger.debug("DTLS SSL context created")
        return ctx

    def _verify_callback(
        self,
        connection: SSL.Connection,
        x509_cert: SSL.X509,
        errno: int,
        depth: int,
        preverify_ok: int
    ) -> bool:
        """
        Callback for certificate verification.

        Called by OpenSSL during handshake to verify peer certificate.
        We check that the peer's certificate Common Name (CN) matches
        the expected node_id.

        Args:
            connection: SSL connection
            x509_cert: Peer certificate (OpenSSL format)
            errno: Error number (0 if no error)
            depth: Certificate chain depth
            preverify_ok: 1 if pre-verification passed

        Returns:
            True if certificate is valid, False otherwise
        """
        # Only verify at depth 0 (peer certificate, not CA)
        if depth != 0:
            return bool(preverify_ok)

        # Extract Common Name from certificate
        cert_subject = x509_cert.get_subject()
        cn = cert_subject.commonName

        # Verify it matches expected node_id
        if cn != self.expected_node_id:
            logger.error(
                f"DTLS certificate validation failed: "
                f"expected node_id={self.expected_node_id}, got CN={cn}"
            )
            return False

        logger.info(f"DTLS certificate validated: node_id={cn}")
        return True

    async def connect(self, timeout: Optional[float] = None) -> None:
        """
        Perform DTLS handshake.

        Wraps the UDP socket with SSL.Connection and performs the DTLS handshake.
        Uses asyncio.run_in_executor() to avoid blocking the event loop.

        Args:
            timeout: Handshake timeout in seconds (default: self.handshake_timeout)

        Raises:
            DTLSHandshakeError: If handshake fails or times out
            DTLSCertificateError: If peer certificate validation fails
        """
        if timeout is None:
            timeout = self.handshake_timeout

        logger.info(f"Starting DTLS handshake (timeout={timeout}s)...")

        try:
            # Create SSL context
            ctx = self._create_ssl_context()

            # Wrap UDP socket with SSL.Connection
            self._ssl = SSL.Connection(ctx, self.udp_socket)

            # Set DTLS role (server=accept, client=connect)
            if self.is_server:
                self._ssl.set_accept_state()
                logger.debug("DTLS role: server (accept)")
            else:
                self._ssl.set_connect_state()
                logger.debug("DTLS role: client (connect)")

            # Perform handshake (blocking operation, wrap in executor)
            await asyncio.wait_for(
                self._loop.run_in_executor(None, self._ssl.do_handshake),
                timeout=timeout
            )

            self._is_connected = True
            logger.info("DTLS handshake successful")

        except asyncio.TimeoutError:
            logger.error(f"DTLS handshake timeout after {timeout}s")
            raise DTLSHandshakeError(f"Handshake timeout after {timeout}s")

        except SSL.Error as e:
            logger.error(f"DTLS handshake failed: {e}")
            # Check if it's a certificate validation error
            if "certificate verify failed" in str(e):
                raise DTLSCertificateError(f"Peer certificate validation failed: {e}")
            raise DTLSHandshakeError(f"Handshake failed: {e}")

        except Exception as e:
            logger.error(f"DTLS handshake unexpected error: {e}")
            raise DTLSHandshakeError(f"Unexpected handshake error: {e}")

    async def send_message(self, data: bytes) -> None:
        """
        Send encrypted message over DTLS.

        Args:
            data: Message bytes to send (will be encrypted)

        Raises:
            RuntimeError: If not connected
            DTLSHandshakeError: If send fails
        """
        if not self._is_connected or self._ssl is None:
            raise RuntimeError("DTLS not connected. Call connect() first.")

        try:
            # Send encrypted data (blocking operation, wrap in executor)
            await self._loop.run_in_executor(None, self._ssl.send, data)
            logger.debug(f"DTLS sent {len(data)} bytes (encrypted)")

        except SSL.Error as e:
            logger.error(f"DTLS send failed: {e}")
            raise DTLSHandshakeError(f"Send failed: {e}")

    async def receive_message(self, max_size: int = 65536) -> bytes:
        """
        Receive encrypted message over DTLS.

        Args:
            max_size: Maximum message size to receive (default: 64KB)

        Returns:
            Decrypted message bytes

        Raises:
            RuntimeError: If not connected
            DTLSHandshakeError: If receive fails
        """
        if not self._is_connected or self._ssl is None:
            raise RuntimeError("DTLS not connected. Call connect() first.")

        try:
            # Receive encrypted data (blocking operation, wrap in executor)
            data = await self._loop.run_in_executor(None, self._ssl.recv, max_size)
            logger.debug(f"DTLS received {len(data)} bytes (decrypted)")
            return data

        except SSL.Error as e:
            logger.error(f"DTLS receive failed: {e}")
            raise DTLSHandshakeError(f"Receive failed: {e}")

    async def close(self) -> None:
        """
        Close DTLS connection gracefully.

        Sends SSL shutdown alert and closes the connection.
        Does NOT close the underlying UDP socket (caller's responsibility).
        """
        if self._ssl is not None:
            try:
                # Send SSL shutdown alert (blocking operation, wrap in executor)
                await self._loop.run_in_executor(None, self._ssl.shutdown)
                logger.info("DTLS connection closed gracefully")
            except SSL.Error as e:
                logger.warning(f"DTLS shutdown warning: {e}")
            finally:
                self._ssl = None
                self._is_connected = False

    @property
    def is_connected(self) -> bool:
        """Check if DTLS handshake is complete and connection is active."""
        return self._is_connected

    def get_peer_certificate(self) -> Optional[x509.Certificate]:
        """
        Get peer's X.509 certificate (cryptography format).

        Returns:
            Peer certificate or None if not connected

        Example:
            >>> cert = dtls_conn.get_peer_certificate()
            >>> if cert:
            ...     print(f"Peer CN: {cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value}")
        """
        if self._ssl is None:
            return None

        try:
            # Get peer certificate (OpenSSL format)
            peer_cert_openssl = self._ssl.get_peer_certificate()
            if peer_cert_openssl is None:
                return None

            # Convert to cryptography format
            peer_cert_pem = SSL.dump_certificate(SSL.FILETYPE_PEM, peer_cert_openssl)
            peer_cert = x509.load_pem_x509_certificate(peer_cert_pem, default_backend())
            return peer_cert

        except Exception as e:
            logger.error(f"Failed to get peer certificate: {e}")
            return None

    def __repr__(self) -> str:
        return (
            f"DTLSPeerConnection(remote={self.remote_addr}, "
            f"peer={self.expected_node_id}, "
            f"role={'server' if self.is_server else 'client'}, "
            f"connected={self._is_connected})"
        )
