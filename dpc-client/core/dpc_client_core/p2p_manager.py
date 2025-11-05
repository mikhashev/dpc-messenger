# dpc-client/core/dpc_client_core/p2p_manager.py

import asyncio
import ssl
import json
from typing import Dict, Any, Callable
from dataclasses import dataclass, field
from enum import Enum, auto

from aiortc import RTCPeerConnection, RTCSessionDescription
from cryptography import x509

from dpc_protocol.crypto import generate_node_id, load_identity
from dpc_protocol.protocol import read_message, write_message, create_hello_message
from dpc_protocol.utils import parse_dpc_uri

from .firewall import ContextFirewall
from .hub_client import HubClient

class ConnectionState(Enum):
    CONNECTING = auto()
    CONNECTED = auto()
    DISCONNECTED = auto()
    FAILED = auto()

@dataclass
class PeerConnection:
    """A unified wrapper for any type of P2P connection."""
    node_id: str
    state: ConnectionState = ConnectionState.CONNECTING
    transport: Any = None  # Will hold (reader, writer) for TLS or RTCPeerConnection for WebRTC

    async def send(self, message: Dict[str, Any]):
        """Sends a message over the active transport."""
        # TODO: Implement logic to send over TLS or WebRTC data channel
        print(f"Sending message to {self.node_id}: {message}")

    async def close(self):
        """Closes the connection regardless of type."""
        # TODO: Implement logic to close TLS or WebRTC connection
        print(f"Closing connection to {self.node_id}")
        self.state = ConnectionState.DISCONNECTED

class P2PManager:
    """

    Manages all direct P2P connections, supporting both direct TLS (PoC method)
    and Hub-assisted WebRTC for NAT traversal.
    """
    def __init__(self, firewall: ContextFirewall):
        self.firewall = firewall
        self.peers: Dict[str, PeerConnection] = {}
        self.node_id, self.key_file, self.cert_file = load_identity()
        self.on_peer_list_change: Callable | None = None
        self._server_task = None
        print("P2PManager initialized in dual-mode.")

    def set_on_peer_list_change(self, callback: Callable):
        self.on_peer_list_change = callback

    async def _notify_peer_change(self):
        if self.on_peer_list_change:
            await self.on_peer_list_change()

    # --- Direct TLS Connection (PoC Method) ---

    async def start_server(self):
        """Starts the raw TLS server to listen for direct connections."""
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file)
        
        server = await asyncio.start_server(
            self._handle_direct_connection, "0.0.0.0", 8888, ssl=ssl_context # Using a fixed port for now
        )
        self._server_task = asyncio.create_task(server.serve_forever())
        print(f"P2PManager Direct TLS server listening on port 8888 for node {self.node_id}")

    async def _handle_direct_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handles an incoming raw TLS connection."""
        print("Received a direct TLS connection attempt...")
        # This logic is adapted directly from our PoC's p2p_node.py
        # TODO: Implement the full handshake and message loop from the PoC
        # For now, we'll just close it.
        writer.close()
        await writer.wait_closed()

    async def connect_directly(self, host: str, port: int, target_node_id: str):
        """Attempts a direct TLS connection to a peer."""
        print(f"Attempting direct TLS connection to {target_node_id} at {host}:{port}...")
        # This logic is adapted directly from our PoC's p2p_node.py
        # TODO: Implement the full connection, verification, and HELLO handshake
        # For now, we'll simulate a failure to test the fallback.
        raise ConnectionError("Direct TLS connection is not fully implemented yet.")

    # --- Hub-Assisted WebRTC Connection ---

    async def connect_via_hub(self, target_node_id: str, hub_client: HubClient):
        """Initiates a connection using the Hub for signaling."""
        # This is the WebRTC logic we developed in the previous steps.
        # It remains largely the same.
        print(f"Attempting Hub-assisted WebRTC connection to {target_node_id}...")
        # TODO: Implement the full WebRTC offer/answer flow
        raise NotImplementedError("WebRTC connection not fully implemented yet.")

    async def handle_incoming_signal(self, signal: Dict[str, Any], hub_client: HubClient):
        """Processes a signaling message from the Hub."""
        # TODO: Implement the WebRTC signal handling
        pass

    # --- Unified Connection Management ---

    async def shutdown_peer_connection(self, peer_id: str):
        if peer_id in self.peers:
            await self.peers[peer_id].close()
            del self.peers[peer_id]
            await self._notify_peer_change()

    async def shutdown_all(self):
        print("Shutting down P2P Manager...")
        if self._server_task:
            self._server_task.cancel()
        for peer_id in list(self.peers.keys()):
            await self.shutdown_peer_connection(peer_id)
        print("P2P Manager shut down.")