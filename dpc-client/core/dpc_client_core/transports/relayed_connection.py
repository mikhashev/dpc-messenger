"""
Relayed Peer Connection - Relay Transport Wrapper

Wraps a relayed connection (via volunteer relay node) to provide the
same interface as direct PeerConnection. Messages are encrypted end-to-end
with the recipient's public key before being handed to the relay.

Architecture:
- Client connects to relay via TLS
- Before sending, client encrypts payload with recipient's RSA public key
- Relay forwards opaque encrypted blob — cannot read content
- Recipient decrypts with own private key
- Transparent to higher layers (same API as direct connection)

Privacy:
- Messages encrypted end-to-end (AES-256-GCM + RSA-OAEP, same as gossip tier)
- Relay sees: peer IDs, encrypted blob sizes, timing
- Relay does NOT know: message content, conversation context
"""

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.x509 import load_pem_x509_certificate

from dpc_protocol.crypto import encrypt_with_public_key_hybrid, decrypt_with_private_key_hybrid

if TYPE_CHECKING:
    from ..models.relay_node import RelayNode
    from ..dht.manager import DHTManager

logger = logging.getLogger(__name__)


class RelayedPeerConnection:
    """
    Peer connection via volunteer relay node.

    Provides same interface as PeerConnection but routes messages
    through relay instead of direct connection, with E2E encryption
    so the relay cannot read message content.

    Attributes:
        peer_id: Target peer node ID
        relay_node: Relay node handling forwarding
        relay_connection: TLS connection to relay (PeerConnection)
        session_id: Relay session identifier
        own_node_id: Our own node ID (used in "from" field)
        dht_manager: DHT manager for certificate discovery
        running: Whether connection is active

    Example:
        >>> # Establish relayed connection
        >>> conn = RelayedPeerConnection(
        ...     peer_id="dpc-node-bob",
        ...     relay_node=relay,
        ...     relay_connection=relay_conn,
        ...     session_id="...",
        ...     own_node_id="dpc-node-alice",
        ...     dht_manager=dht_manager,
        ... )
        >>> await conn.start()
        >>>
        >>> # Use like direct connection
        >>> await conn.send_message({"command": "HELLO"})
        >>> message = await conn.receive_message()
    """

    def __init__(
        self,
        peer_id: str,
        relay_node: "RelayNode",
        relay_connection,  # PeerConnection to relay (.send() / .read())
        session_id: str,
        own_node_id: str = "",
        dht_manager: Optional["DHTManager"] = None,
    ):
        """
        Initialize relayed connection.

        Args:
            peer_id: Target peer node ID
            relay_node: Relay node metadata
            relay_connection: TLS connection to relay
            session_id: Relay session identifier
            own_node_id: Our own node ID (for "from" field in relay envelope)
            dht_manager: DHT manager for peer certificate discovery
        """
        self.peer_id = peer_id
        self.relay_node = relay_node
        self.relay_connection = relay_connection
        self.session_id = session_id
        self.own_node_id = own_node_id
        self.dht_manager = dht_manager

        self.running = False
        self._receive_queue: asyncio.Queue = asyncio.Queue()
        self._receive_task: Optional[asyncio.Task] = None
        self._own_private_key = None  # Lazy-loaded, cached after first use

        logger.info(
            "RelayedPeerConnection created: peer=%s, relay=%s, session=%s",
            peer_id[:20], relay_node.node_id[:20], session_id
        )

    # ===== Certificate / Key Helpers =====

    async def _get_peer_certificate(self, node_id: str):
        """
        Get peer's X.509 certificate for E2E encryption.

        Queries DHT using key "cert:<node_id>" (same mechanism as gossip tier).

        Args:
            node_id: Target peer node ID

        Returns:
            x509.Certificate object

        Raises:
            ConnectionError: If certificate not found
        """
        if not self.dht_manager:
            raise ConnectionError(
                f"No DHT manager available — cannot look up certificate for {node_id[:20]}"
            )

        cert_key = f"cert:{node_id}"
        known_peers = self.dht_manager.get_known_peers()

        for peer in known_peers:
            try:
                result = await self.dht_manager.rpc_handler.find_value(
                    peer.ip, peer.port, cert_key
                )
                if result and "value" in result:
                    cert_pem = result["value"]
                    if isinstance(cert_pem, str):
                        cert_pem = cert_pem.encode()
                    return load_pem_x509_certificate(cert_pem)
            except Exception as e:
                logger.debug("DHT cert lookup from %s failed: %s", peer.node_id[:20], e)
                continue

        raise ConnectionError(
            f"Certificate for peer {node_id[:20]} not found in DHT. "
            "Peer must be online and have published their certificate."
        )

    async def _load_own_private_key(self):
        """
        Load own RSA private key for decryption (cached after first load).

        Returns:
            RSA private key object
        """
        if self._own_private_key is not None:
            return self._own_private_key

        dpc_dir = Path(os.getenv("DPC_DIR", Path.home() / ".dpc"))
        key_path = dpc_dir / "node.key"

        with open(key_path, "rb") as f:
            self._own_private_key = serialization.load_pem_private_key(
                f.read(), password=None
            )

        return self._own_private_key

    # ===== Connection Lifecycle =====

    async def start(self):
        """
        Start relayed connection (begin receiving messages).

        Starts background task to receive RELAY_MESSAGE protocol messages
        from relay and enqueue them for application consumption.
        """
        if self.running:
            logger.warning("RelayedPeerConnection already running")
            return

        self.running = True
        self._receive_task = asyncio.create_task(self._receive_loop())

        logger.info("RelayedPeerConnection started for peer %s", self.peer_id[:20])

    async def stop(self):
        """Stop relayed connection and cleanup."""
        if not self.running:
            return

        self.running = False

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        # Send RELAY_DISCONNECT to relay
        try:
            await self.relay_connection.send({
                "command": "RELAY_DISCONNECT",
                "payload": {
                    "peer": self.peer_id,
                    "session_id": self.session_id,
                    "reason": "connection_closed"
                }
            })
        except Exception as e:
            logger.debug("Failed to send RELAY_DISCONNECT: %s", e)

        logger.info("RelayedPeerConnection stopped for peer %s", self.peer_id[:20])

    # ===== Send / Receive =====

    async def send_message(self, message: dict):
        """
        Send message to peer via relay with E2E encryption.

        Encrypts the message with the recipient's public key (AES-256-GCM +
        RSA-OAEP) before wrapping it in the relay envelope. The relay node
        forwards the opaque encrypted blob without being able to read it.

        Args:
            message: Message dictionary to send

        Raises:
            ConnectionError: If relay connection failed or cert not available

        Protocol:
            1. Fetch recipient's certificate from DHT
            2. Encrypt: AES-256-GCM(message) + RSA-OAEP(AES key)
            3. Base64-encode the encrypted blob
            4. Wrap in RELAY_MESSAGE with "data" field (opaque to relay)
        """
        if not self.running:
            raise ConnectionError("RelayedPeerConnection not running")

        # 1. Get recipient certificate from DHT for E2E encryption
        try:
            peer_cert = await self._get_peer_certificate(self.peer_id)
        except ConnectionError as e:
            logger.error("Cannot encrypt for relay — cert lookup failed: %s", e)
            raise

        # 2. Encrypt payload (same hybrid scheme as gossip tier)
        payload_bytes = json.dumps(message).encode("utf-8")
        encrypted = encrypt_with_public_key_hybrid(payload_bytes, peer_cert.public_key())
        data_b64 = base64.b64encode(encrypted).decode("utf-8")

        # 3. Wrap in relay envelope — relay sees only "from/to/session_id/data"
        relay_message = {
            "command": "RELAY_MESSAGE",
            "payload": {
                "from": self.own_node_id,
                "to": self.peer_id,
                "session_id": self.session_id,
                "data": data_b64  # Opaque encrypted blob — relay cannot read
            }
        }

        try:
            await self.relay_connection.send(relay_message)
            logger.debug(
                "Sent E2E-encrypted message to peer %s via relay (%d bytes)",
                self.peer_id[:20], len(encrypted)
            )
        except Exception as e:
            logger.error("Failed to send relayed message: %s", e)
            raise ConnectionError(f"Relay send failed: {e}")

    async def receive_message(self, timeout: Optional[float] = None) -> Optional[dict]:
        """
        Receive message from peer via relay.

        Args:
            timeout: Receive timeout in seconds

        Returns:
            Decrypted message dictionary, or None if timeout

        Raises:
            ConnectionError: If connection closed
        """
        if not self.running:
            raise ConnectionError("RelayedPeerConnection not running")

        try:
            if timeout:
                message = await asyncio.wait_for(
                    self._receive_queue.get(),
                    timeout=timeout
                )
            else:
                message = await self._receive_queue.get()

            return message

        except asyncio.TimeoutError:
            return None

    async def _receive_loop(self):
        """
        Background task: receive RELAY_MESSAGE from relay, decrypt, and enqueue.

        Runs until connection closed. Filters for RELAY_MESSAGE commands
        from our session, decrypts the E2E-encrypted payload, and queues
        the plaintext message for application consumption.
        """
        logger.debug("Relay receive loop started for peer %s", self.peer_id[:20])

        try:
            while self.running:
                # Receive message from relay
                relay_msg = await self.relay_connection.read()

                if not relay_msg:
                    await asyncio.sleep(0.05)
                    continue

                # Filter for RELAY_MESSAGE from our session
                if relay_msg.get("command") != "RELAY_MESSAGE":
                    continue

                payload = relay_msg.get("payload", {})

                if (payload.get("session_id") != self.session_id
                        or payload.get("from") != self.peer_id):
                    continue

                # Decrypt E2E-encrypted blob
                data_b64 = payload.get("data")
                if not data_b64:
                    logger.warning(
                        "Received RELAY_MESSAGE without 'data' field from %s",
                        self.peer_id[:20]
                    )
                    continue

                try:
                    encrypted_bytes = base64.b64decode(data_b64)
                    private_key = await self._load_own_private_key()
                    decrypted_bytes = decrypt_with_private_key_hybrid(
                        encrypted_bytes, private_key
                    )
                    peer_message = json.loads(decrypted_bytes.decode("utf-8"))
                except Exception as e:
                    logger.error(
                        "Failed to decrypt relay message from %s: %s "
                        "(wrong key, tampered data, or legacy unencrypted message)",
                        self.peer_id[:20], e
                    )
                    continue

                await self._receive_queue.put(peer_message)
                logger.debug(
                    "Decrypted and enqueued relay message from %s",
                    self.peer_id[:20]
                )

        except asyncio.CancelledError:
            logger.debug("Relay receive loop cancelled")
            raise
        except Exception as e:
            logger.error("Relay receive loop error: %s", e)
            self.running = False

    def is_connected(self) -> bool:
        """Check if relayed connection is active."""
        return self.running

    def __repr__(self) -> str:
        """String representation for debugging."""
        return (
            f"<RelayedPeerConnection peer={self.peer_id[:20]} "
            f"relay={self.relay_node.node_id[:20]} session={self.session_id}>"
        )
