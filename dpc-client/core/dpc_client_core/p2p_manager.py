# dpc-client/core/dpc_client_core/p2p_manager.py

import asyncio
import ssl
import json
from typing import Dict, Any, Callable, Tuple

from cryptography import x509

from dpc_protocol.crypto import generate_node_id, load_identity
from dpc_protocol.protocol import read_message, write_message, create_hello_message
from dpc_protocol.pcm_core import PCMCore, PersonalContext
from dpc_protocol.utils import parse_dpc_uri

from .firewall import ContextFirewall
from .hub_client import HubClient

class PeerConnection:
    """A unified wrapper for a direct TLS P2P connection."""
    def __init__(self, node_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.node_id = node_id
        self.reader = reader
        self.writer = writer

    async def send(self, message: Dict[str, Any]):
        """Sends a message over the TLS stream."""
        await write_message(self.writer, message)

    async def read(self) -> Dict[str, Any] | None:
        """Reads a message from the TLS stream."""
        return await read_message(self.reader)

    async def close(self):
        """Closes the TLS connection."""
        if not self.writer.is_closing():
            self.writer.close()
            await self.writer.wait_closed()

class P2PManager:
    """Manages all direct P2P connections."""
    def __init__(self, firewall: ContextFirewall):
        self.firewall = firewall
        self.peers: Dict[str, PeerConnection] = {}
        self.node_id, self.key_file, self.cert_file = load_identity()
        self.local_context = PCMCore().load_context()
        self.on_peer_list_change: Callable | None = None
        self.on_message_received: Callable | None = None
        self._server_task = None

        print("P2PManager initialized for Direct TLS connections.")

    def set_on_peer_list_change(self, callback: Callable):
        self.on_peer_list_change = callback

    def set_on_message_received(self, callback: Callable):
        self.on_message_received = callback

    async def _notify_peer_change(self):
        if self.on_peer_list_change:
            await self.on_peer_list_change()

    # --- Direct TLS Connection (PoC Method) ---

    async def start_server(self, host: str = "0.0.0.0", port: int = 8888):
        """Starts the raw TLS server to listen for direct connections."""
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file)
        
        server = await asyncio.start_server(
            self._handle_direct_connection, host, port, ssl=ssl_context
        )
        self._server_task = asyncio.create_task(server.serve_forever())
        print(f"P2PManager Direct TLS server listening on {host}:{port} for node {self.node_id}")

    async def _handle_direct_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handles an incoming raw TLS connection (Server-side)."""
        peer_node_id = None
        try:
            print("Received a direct TLS connection attempt...")
            await asyncio.sleep(0.01) # Grace period for handshake

            # Perform HELLO handshake (server side)
            hello_msg = await read_message(reader)
            if not hello_msg or hello_msg.get("command") != "HELLO":
                raise ConnectionError("Invalid HELLO message received.")
            
            peer_node_id = hello_msg["payload"]["node_id"]
            print(f"Received HELLO from {peer_node_id}. Sending response.")
            await write_message(writer, {"status": "OK", "message": "HELLO received"})

            # Create and register the peer connection
            peer = PeerConnection(node_id=peer_node_id, reader=reader, writer=writer)
            self.peers[peer_node_id] = peer
            await self._notify_peer_change()
            print(f"✅ Direct connection established with {peer_node_id}")

            # Start listening for messages from this peer
            await self._listen_to_peer(peer)

        except Exception as e:
            print(f"Direct connection failed with {peer_node_id or 'unknown'}: {e}")
            writer.close()
            await writer.wait_closed()

    async def connect_directly(self, host: str, port: int, target_node_id: str):
        """Attempts a direct TLS connection to a peer (Client-side)."""
        if target_node_id in self.peers:
            print(f"Already connected to {target_node_id}")
            return

        print(f"Attempting direct TLS connection to {target_node_id} at {host}:{port}...")
        
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE # Disable standard verification

        reader, writer = await asyncio.open_connection(host, port, ssl=ssl_context)
        
        try:
            # Perform our custom identity verification
            ssl_object = writer.get_extra_info('ssl_object')
            peer_cert_bytes = ssl_object.getpeercert(binary_form=True)
            peer_cert = x509.load_der_x509_certificate(peer_cert_bytes)
            verified_node_id = generate_node_id(peer_cert.public_key())
            
            if verified_node_id != target_node_id:
                raise ConnectionRefusedError(f"Security alert! Node ID mismatch.")

            # Perform HELLO handshake (client side)
            await write_message(writer, create_hello_message(self.node_id))
            response = await read_message(reader)
            if not response or response.get("status") != "OK":
                raise ConnectionError(f"Peer did not acknowledge HELLO.")

            # Create and register the peer connection
            peer = PeerConnection(node_id=target_node_id, reader=reader, writer=writer)
            self.peers[target_node_id] = peer
            await self._notify_peer_change()
            print(f"✅ Direct connection established with {target_node_id}")

            # Start listening for messages from this peer
            asyncio.create_task(self._listen_to_peer(peer))

        except Exception as e:
            print(f"Failed to establish direct connection with {target_node_id}: {e}")
            writer.close()
            await writer.wait_closed()
            raise # Re-raise the exception for the CoreService to handle

    async def _listen_to_peer(self, peer: PeerConnection):
        """Background task to listen for and process messages from a single peer."""
        try:
            while True:
                message = await peer.read()
                if message is None:
                    break # Connection closed
                
                # --- THE CORE FIX ---
                # Route the message to the CoreService via the callback
                if self.on_message_received:
                    # We run this as a task so it doesn't block the listener loop
                    asyncio.create_task(self.on_message_received(peer.node_id, message))
                # --------------------

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass # Normal disconnection
        finally:
            print(f"Connection with peer {peer.node_id} was lost.")
            await self.shutdown_peer_connection(peer.node_id)

    async def send_message_to_peer(self, node_id: str, message: Dict[str, Any]):
        """Sends a message to a specific connected peer."""
        if node_id not in self.peers:
            raise ConnectionError(f"Not connected to peer {node_id}.")
        
        peer = self.peers[node_id]
        await peer.send(message)

    async def send_message_to_peer(self, node_id: str, message: Dict[str, Any]):
        """Sends a message to a specific connected peer."""
        if node_id not in self.peers:
            raise ConnectionError(f"Not connected to peer {node_id}.")
        
        peer = self.peers[node_id]
        await peer.send(message)

    # --- Hub-Assisted WebRTC (Placeholder for now) ---

    async def connect_via_hub(self, target_node_id: str, hub_client: HubClient):
        print("Hub-assisted WebRTC connection not yet implemented.")
        raise NotImplementedError("WebRTC connection not fully implemented yet.")

    async def handle_incoming_signal(self, signal: Dict[str, Any], hub_client: HubClient):
        print("WebRTC signal handling not yet implemented.")
        pass

    # --- Unified Connection Management ---

    async def shutdown_peer_connection(self, peer_id: str):
        if peer_id in self.peers:
            peer = self.peers.pop(peer_id)
            await peer.close()
            print(f"Connection with {peer_id} closed.")
            await self._notify_peer_change()

    async def shutdown_all(self):
        print("Shutting down P2P Manager...")
        if self._server_task:
            self._server_task.cancel()
        for peer_id in list(self.peers.keys()):
            await self.shutdown_peer_connection(peer_id)
        print("P2P Manager shut down.")