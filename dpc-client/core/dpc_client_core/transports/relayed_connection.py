"""
Relayed Peer Connection - Relay Transport Wrapper

Wraps a relayed connection (via volunteer relay node) to provide the
same interface as direct PeerConnection. Messages are encrypted end-to-end
with the recipient's public key before being handed to the relay.

Architecture:
- Client connects to relay via TLS (connect_directly — full message routing)
- All messages from relay arrive through MessageRouter (no raw .read())
- Before sending, client encrypts payload with recipient's RSA public key
- Relay forwards opaque encrypted blob — cannot read content
- Incoming RELAY_MESSAGE is dispatched by RelayMessageHandler via relay_manager
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

    Incoming messages are pushed into this object by the relay_manager
    (via _dispatch_incoming) when RelayMessageHandler routes them, rather
    than by a background read loop. This avoids conflicting with the
    _listen_to_peer task that owns the relay TLS socket.

    Attributes:
        peer_id: Target peer node ID
        relay_node: Relay node handling forwarding
        relay_connection: TLS connection to relay (PeerConnection, .send() only)
        session_id: Relay session identifier
        own_node_id: Our own node ID (used in "from" field)
        dht_manager: DHT manager for certificate discovery
        running: Whether connection is active

    Example:
        >>> conn = RelayedPeerConnection(
        ...     peer_id="dpc-node-bob",
        ...     relay_node=relay,
        ...     relay_connection=relay_conn,
        ...     session_id="...",
        ...     own_node_id="dpc-node-alice",
        ...     dht_manager=dht_manager,
        ... )
        >>> await conn.start()
        >>> await conn.send_message({"command": "HELLO"})
        >>> message = await conn.receive_message()
    """

    def __init__(
        self,
        peer_id: str,
        relay_node: "RelayNode",
        relay_connection,  # PeerConnection to relay (.send() only — no raw .read())
        session_id: str,
        own_node_id: str = "",
        dht_manager: Optional["DHTManager"] = None,
    ):
        self.peer_id = peer_id
        self.relay_node = relay_node
        self.relay_connection = relay_connection
        self.session_id = session_id
        self.own_node_id = own_node_id
        self.dht_manager = dht_manager

        self.running = False
        self._receive_queue: asyncio.Queue = asyncio.Queue()
        self._own_private_key = None  # Lazy-loaded, cached after first use

        logger.info(
            "RelayedPeerConnection created: peer=%s, relay=%s, session=%s",
            peer_id[:20], relay_node.node_id[:20], session_id
        )

    # ===== Certificate / Key Helpers =====

    async def _get_peer_certificate(self, node_id: str):
        """
        Get peer's X.509 certificate for E2E encryption via DHT lookup.

        Uses cert:<node_id> key — same mechanism as the gossip tier.
        """
        if not self.dht_manager:
            raise ConnectionError(
                f"No DHT manager — cannot look up certificate for {node_id[:20]}"
            )

        cert_key = f"cert:{node_id}"
        for peer in self.dht_manager.get_known_peers():
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
                logger.debug("DHT cert lookup from %s: %s", peer.node_id[:20], e)

        raise ConnectionError(
            f"Certificate for {node_id[:20]} not found in DHT. "
            "Peer must be online and have published their certificate."
        )

    async def _load_own_private_key(self):
        """Load own RSA private key for decryption (cached after first load)."""
        if self._own_private_key is not None:
            return self._own_private_key
        dpc_dir = Path(os.getenv("DPC_DIR", Path.home() / ".dpc"))
        with open(dpc_dir / "node.key", "rb") as f:
            self._own_private_key = serialization.load_pem_private_key(
                f.read(), password=None
            )
        return self._own_private_key

    # ===== Connection Lifecycle =====

    async def start(self):
        """Mark connection as active. No background task needed — incoming
        messages are pushed via _dispatch_incoming by the message router."""
        if self.running:
            logger.warning("RelayedPeerConnection already running")
            return
        self.running = True
        logger.info("RelayedPeerConnection started for peer %s", self.peer_id[:20])

    async def stop(self):
        """Stop relayed connection and notify relay."""
        if not self.running:
            return
        self.running = False
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

    # ===== Incoming message dispatch (called by RelayMessageHandler) =====

    async def _dispatch_incoming(self, data_b64: str):
        """
        Decrypt and enqueue an incoming E2E-encrypted relay message.

        Called by relay_manager when RelayMessageHandler routes a
        RELAY_MESSAGE destined for this connection.

        Args:
            data_b64: Base64-encoded AES-GCM + RSA-OAEP encrypted blob
        """
        try:
            encrypted_bytes = base64.b64decode(data_b64)
            private_key = await self._load_own_private_key()
            decrypted_bytes = decrypt_with_private_key_hybrid(encrypted_bytes, private_key)
            message = json.loads(decrypted_bytes.decode("utf-8"))
        except Exception as e:
            logger.error(
                "Failed to decrypt relay message from %s: %s "
                "(wrong key, tampered data, or legacy unencrypted message)",
                self.peer_id[:20], e
            )
            return
        await self._receive_queue.put(message)
        logger.debug("Decrypted and enqueued relay message from %s", self.peer_id[:20])

    # ===== Send / Receive =====

    async def send_message(self, message: dict):
        """
        Send E2E-encrypted message to peer via relay.

        Encrypts with recipient's public key (AES-256-GCM + RSA-OAEP)
        then wraps in relay envelope. Relay forwards opaque blob.
        """
        if not self.running:
            raise ConnectionError("RelayedPeerConnection not running")

        try:
            peer_cert = await self._get_peer_certificate(self.peer_id)
        except ConnectionError as e:
            logger.error("Cert lookup failed for relay send: %s", e)
            raise

        payload_bytes = json.dumps(message).encode("utf-8")
        encrypted = encrypt_with_public_key_hybrid(payload_bytes, peer_cert.public_key())
        data_b64 = base64.b64encode(encrypted).decode("utf-8")

        relay_envelope = {
            "command": "RELAY_MESSAGE",
            "payload": {
                "from": self.own_node_id,
                "to": self.peer_id,
                "session_id": self.session_id,
                "data": data_b64  # Opaque blob — relay cannot read
            }
        }

        try:
            await self.relay_connection.send(relay_envelope)
            logger.debug(
                "Sent E2E-encrypted message to %s via relay (%d bytes)",
                self.peer_id[:20], len(encrypted)
            )
        except Exception as e:
            logger.error("Relay send failed: %s", e)
            raise ConnectionError(f"Relay send failed: {e}")

    async def receive_message(self, timeout: Optional[float] = None) -> Optional[dict]:
        """Receive next decrypted message from peer (blocks until available or timeout)."""
        if not self.running:
            raise ConnectionError("RelayedPeerConnection not running")
        try:
            if timeout:
                return await asyncio.wait_for(self._receive_queue.get(), timeout=timeout)
            return await self._receive_queue.get()
        except asyncio.TimeoutError:
            return None

    def is_connected(self) -> bool:
        """Check if relayed connection is active."""
        return self.running

    def __repr__(self) -> str:
        return (
            f"<RelayedPeerConnection peer={self.peer_id[:20]} "
            f"relay={self.relay_node.node_id[:20]} session={self.session_id}>"
        )
