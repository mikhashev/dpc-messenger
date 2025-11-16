# dpc-client/core/dpc_client_core/p2p_manager_webrtc.py
# Обновленная версия P2PManager с поддержкой WebRTC

import asyncio
import ssl
import json
from typing import Dict, Any, Callable, Tuple, Union

from cryptography import x509
import websockets

from dpc_protocol.crypto import generate_node_id, load_identity, generate_identity
from dpc_protocol.protocol import read_message, write_message, create_hello_message
from dpc_protocol.pcm_core import PCMCore, PersonalContext
from dpc_protocol.utils import parse_dpc_uri

from .firewall import ContextFirewall
from .hub_client import HubClient
from .webrtc_peer import WebRTCPeerConnection


class PeerConnection:
    """A unified wrapper for a direct TLS P2P connection."""
    def __init__(self, node_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.node_id = node_id
        self.reader = reader
        self.writer = writer
        self.connection_type = "direct_tls"

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
    """Manages all P2P connections (both direct TLS and WebRTC)."""
    
    def __init__(self, firewall: ContextFirewall):
        self.firewall = firewall
        self.peers: Dict[str, Union[PeerConnection, WebRTCPeerConnection]] = {}
        self.display_name: str | None = None

        # WebRTC connection state tracking
        self._pending_webrtc: Dict[str, WebRTCPeerConnection] = {}
        self._ice_candidates_buffer: Dict[str, list] = {}

        # Auto-reconnect tracking
        self._intentional_disconnects: set = set()  # Track user-initiated disconnects
        self._hub_client_refs: Dict[str, HubClient] = {}  # Store hub_client for reconnection
        
        try:
            self.node_id, self.key_file, self.cert_file = load_identity()
            print("Existing node identity loaded.")
        except FileNotFoundError:
            print("Node identity not found. Generating a new one...")
            generate_identity()
            self.node_id, self.key_file, self.cert_file = load_identity()
            print("New node identity generated and loaded.")

        self.local_context = PCMCore().load_context()
        self.on_peer_list_change: Callable | None = None
        self.on_message_received: Callable | None = None
        self._server_task = None
        print("P2PManager initialized with WebRTC support.")

    def set_on_peer_list_change(self, callback: Callable):
        self.on_peer_list_change = callback

    def set_on_message_received(self, callback: Callable):
        self.on_message_received = callback
    
    def set_core_service_ref(self, core_service):
        """Set reference to CoreService for storing peer metadata."""
        self._core_service_ref = core_service

    def set_display_name(self, name: str):
        """Set the display name that will be shared with peers during handshake."""
        self.display_name = name
        print(f"Display name set to: {name}")
    
    def get_display_name(self) -> str | None:
        """Get the current display name."""
        return self.display_name

    async def _notify_peer_change(self):
        if self.on_peer_list_change:
            await self.on_peer_list_change()

    # --- Direct TLS Connection Methods (existing code) ---

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
            await asyncio.sleep(0.01)

            hello_msg = await read_message(reader)
            if not hello_msg or hello_msg.get("command") != "HELLO":
                raise ConnectionError("Invalid HELLO message received.")

            payload = hello_msg.get("payload", {})
            peer_node_id = payload.get("node_id")
            peer_name = payload.get("name")

            if not peer_node_id:
                raise ValueError("Peer did not provide a node_id.")

            print(f"Connection from node: {peer_node_id}")
            if peer_name:
                print(f"  - Peer name: {peer_name}")
                if hasattr(self, '_core_service_ref') and self._core_service_ref:
                    self._core_service_ref.set_peer_metadata(peer_node_id, name=peer_name)

            ack = {
                "command": "HELLO_ACK",
                "status": "OK",
                "name": self.display_name
            }
            await write_message(writer, ack)

            peer = PeerConnection(node_id=peer_node_id, reader=reader, writer=writer)
            self.peers[peer_node_id] = peer
            await self._notify_peer_change()
            print(f"✅ Direct TLS connection established with {peer_node_id}")

            asyncio.create_task(self._listen_to_peer(peer))

        except Exception as e:
            print(f"Error handling direct connection: {e}")
            if peer_node_id and peer_node_id in self.peers:
                await self.shutdown_peer_connection(peer_node_id)
            else:
                writer.close()
                await writer.wait_closed()

    async def connect_directly(self, host: str, port: int, target_node_id: str):
        """Initiates a direct TLS connection to a peer (Client-side)."""
        print(f"Initiating direct connection to {target_node_id} at {host}:{port}...")

        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        try:
            reader, writer = await asyncio.open_connection(host, port, ssl=ssl_context)

            hello = {
                "command": "HELLO",
                "payload": {
                    "node_id": self.node_id,
                    "name": self.display_name
                }
            }
            await write_message(writer, hello)
            
            response = await read_message(reader)
            if not response or response.get("status") != "OK":
                raise ConnectionError(f"Peer did not acknowledge HELLO.")
            
            peer_name = response.get("name")
            if peer_name:
                print(f"  - Peer name: {peer_name}")
                if hasattr(self, '_core_service_ref') and self._core_service_ref:
                    self._core_service_ref.set_peer_metadata(target_node_id, name=peer_name)

            peer = PeerConnection(node_id=target_node_id, reader=reader, writer=writer)
            self.peers[target_node_id] = peer
            await self._notify_peer_change()
            print(f"✅ Direct connection established with {target_node_id}")

            asyncio.create_task(self._listen_to_peer(peer))

            # Auto-discover peer's available AI providers
            from dpc_protocol.protocol import create_get_providers_message
            try:
                await self.send_message_to_peer(target_node_id, create_get_providers_message())
                print(f"  - Requested AI providers from {target_node_id}")
            except Exception as e:
                print(f"  - Failed to request providers from {target_node_id}: {e}")

        except Exception as e:
            print(f"Failed to establish direct connection with {target_node_id}: {e}")
            raise

    async def _listen_to_peer(self, peer: PeerConnection):
        """Background task to listen for messages from a TLS peer."""
        try:
            while True:
                message = await peer.read()
                if message is None:
                    break
                
                if self.on_message_received:
                    asyncio.create_task(self.on_message_received(peer.node_id, message))

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            print(f"Connection with peer {peer.node_id} was lost.")
            await self.shutdown_peer_connection(peer.node_id)

    # --- WebRTC Connection Methods (NEW) ---

    async def connect_via_hub(self, target_node_id: str, hub_client: HubClient):
        """
        Initiates a WebRTC connection to a peer via Hub signaling.
        This is the INITIATOR side (Alice).
        """
        print(f"Initiating WebRTC connection to {target_node_id} via Hub...")

        if target_node_id in self.peers:
            print(f"Already connected to {target_node_id}")
            return

        if target_node_id in self._pending_webrtc:
            print(f"Connection to {target_node_id} already in progress")
            return

        try:
            # Store hub_client reference for reconnection
            self._hub_client_refs[target_node_id] = hub_client

            # Create WebRTC peer connection
            webrtc_peer = WebRTCPeerConnection(node_id=target_node_id, is_initiator=True)
            self._pending_webrtc[target_node_id] = webrtc_peer
            
            # Set up ICE candidate handler
            async def handle_ice(candidate_dict):
                await hub_client.send_signal(target_node_id, {
                    "type": "ice-candidate",
                    "candidate": candidate_dict
                })
            
            webrtc_peer.on_ice_candidate = handle_ice
            
            # Set up message handler
            async def handle_message(message):
                if self.on_message_received:
                    asyncio.create_task(self.on_message_received(target_node_id, message))
            
            webrtc_peer.on_message = handle_message

            # Set up close handler with auto-reconnect
            async def handle_close(node_id):
                print(f"WebRTC connection closed for {node_id}, cleaning up...")
                if node_id in self.peers:
                    self.peers.pop(node_id)
                    await self._notify_peer_change()
                if node_id in self._pending_webrtc:
                    self._pending_webrtc.pop(node_id)

                # Auto-reconnect if disconnection was not intentional
                if node_id not in self._intentional_disconnects:
                    print(f"[Auto-Reconnect] Connection to {node_id} lost unexpectedly")
                    print(f"[Auto-Reconnect] Will attempt to reconnect in 3 seconds...")
                    await asyncio.sleep(3)

                    # Check if hub_client is still connected
                    stored_hub_client = self._hub_client_refs.get(node_id)
                    if stored_hub_client and stored_hub_client.websocket and stored_hub_client.websocket.state == websockets.State.OPEN:
                        try:
                            print(f"[Auto-Reconnect] Reconnecting to {node_id}...")
                            await self.connect_via_hub(node_id, stored_hub_client)
                        except Exception as e:
                            print(f"[Auto-Reconnect] Failed to reconnect to {node_id}: {e}")
                    else:
                        print(f"[Auto-Reconnect] Cannot reconnect to {node_id} - Hub client not connected")
                        self._hub_client_refs.pop(node_id, None)
                else:
                    print(f"[Auto-Reconnect] Disconnection was intentional, not reconnecting")
                    self._intentional_disconnects.discard(node_id)
                    self._hub_client_refs.pop(node_id, None)

            webrtc_peer.on_close = handle_close
            
            # Create and send offer
            offer = await webrtc_peer.create_offer()
            await hub_client.send_signal(target_node_id, {
                "type": "offer",
                "offer": offer
            })
            
            print(f"WebRTC offer sent to {target_node_id}, waiting for answer...")
            
            # Start timeout task
            asyncio.create_task(self._handle_connection_timeout(target_node_id))
            
        except Exception as e:
            print(f"Failed to initiate WebRTC connection to {target_node_id}: {e}")
            self._pending_webrtc.pop(target_node_id, None)
            raise

    async def handle_incoming_signal(self, signal: Dict[str, Any], hub_client: HubClient):
        """
        Handles incoming WebRTC signaling messages from the Hub.
        Routes to appropriate handler based on signal type.
        
        Hub sends signals in this format:
        {
            "type": "signal",
            "sender_node_id": "dpc-node-xxx",
            "target_node_id": "dpc-node-yyy",
            "payload": {
                "type": "offer|answer|ice-candidate",
                "offer": {...} or "answer": {...} or "candidate": {...}
            }
        }
        """
        signal_type = signal.get("type")
        
        # Filter out non-signal messages (pong, auth_ok, error)
        if signal_type in ["pong", "auth_ok", "error"]:
            return
        
        # Handle Hub-wrapped signals
        if signal_type == "signal":
            # Extract sender and payload from Hub wrapper
            from_node = signal.get("sender_node_id")
            wrapped_payload = signal.get("payload", {})
            
            # The actual WebRTC signal type is inside the payload
            webrtc_signal_type = wrapped_payload.get("type")
            
            # Validate we have required fields
            if not from_node or not webrtc_signal_type:
                print(f"Received invalid Hub signal (missing sender_node_id or payload.type): {signal}")
                return
            
            print(f"Received WebRTC signal '{webrtc_signal_type}' from {from_node}")
            
            try:
                if webrtc_signal_type == "offer":
                    await self._handle_webrtc_offer(from_node, wrapped_payload, hub_client)
                
                elif webrtc_signal_type == "answer":
                    await self._handle_webrtc_answer(from_node, wrapped_payload)
                
                elif webrtc_signal_type == "ice-candidate":
                    await self._handle_ice_candidate(from_node, wrapped_payload)
                
                else:
                    print(f"Unknown WebRTC signal type: {webrtc_signal_type}")
            
            except Exception as e:
                print(f"Error handling signal from {from_node}: {e}")
                import traceback
                traceback.print_exc()
        
        else:
            # Unknown signal format
            print(f"Received unknown signal format (type={signal_type}): {signal}")

    async def _handle_webrtc_offer(self, from_node: str, payload: Dict[str, Any], hub_client: HubClient):
        """Handle incoming WebRTC offer (ANSWERER side - Bob)."""
        print(f"Handling WebRTC offer from {from_node}")

        if from_node in self.peers:
            print(f"Already connected to {from_node}, ignoring offer")
            return

        # Store hub_client reference for reconnection
        self._hub_client_refs[from_node] = hub_client

        # Create WebRTC peer connection as answerer
        webrtc_peer = WebRTCPeerConnection(node_id=from_node, is_initiator=False)
        self._pending_webrtc[from_node] = webrtc_peer
        
        # Set up ICE candidate handler
        async def handle_ice(candidate_dict):
            await hub_client.send_signal(from_node, {
                "type": "ice-candidate",
                "candidate": candidate_dict
            })
        
        webrtc_peer.on_ice_candidate = handle_ice
        
        # Set up message handler
        async def handle_message(message):
            if self.on_message_received:
                asyncio.create_task(self.on_message_received(from_node, message))
        
        webrtc_peer.on_message = handle_message

        # Set up close handler with auto-reconnect
        async def handle_close(node_id):
            print(f"WebRTC connection closed for {node_id}, cleaning up...")
            if node_id in self.peers:
                self.peers.pop(node_id)
                await self._notify_peer_change()
            if node_id in self._pending_webrtc:
                self._pending_webrtc.pop(node_id)

            # Auto-reconnect if disconnection was not intentional
            if node_id not in self._intentional_disconnects:
                print(f"[Auto-Reconnect] Connection to {node_id} lost unexpectedly")
                print(f"[Auto-Reconnect] Will attempt to reconnect in 3 seconds...")
                await asyncio.sleep(3)

                # Check if hub_client is still connected
                stored_hub_client = self._hub_client_refs.get(node_id)
                if stored_hub_client and stored_hub_client.websocket and stored_hub_client.websocket.state == websockets.State.OPEN:
                    try:
                        print(f"[Auto-Reconnect] Reconnecting to {node_id}...")
                        await self.connect_via_hub(node_id, stored_hub_client)
                    except Exception as e:
                        print(f"[Auto-Reconnect] Failed to reconnect to {node_id}: {e}")
                else:
                    print(f"[Auto-Reconnect] Cannot reconnect to {node_id} - Hub client not connected")
                    self._hub_client_refs.pop(node_id, None)
            else:
                print(f"[Auto-Reconnect] Disconnection was intentional, not reconnecting")
                self._intentional_disconnects.discard(node_id)
                self._hub_client_refs.pop(node_id, None)

        webrtc_peer.on_close = handle_close

        # Handle offer and create answer
        offer_sdp = payload.get("offer")
        answer = await webrtc_peer.handle_offer(offer_sdp)
        
        # Send answer back
        await hub_client.send_signal(from_node, {
            "type": "answer",
            "answer": answer
        })
        
        print(f"WebRTC answer sent to {from_node}")
        
        # Process any buffered ICE candidates
        if from_node in self._ice_candidates_buffer:
            for candidate in self._ice_candidates_buffer[from_node]:
                await webrtc_peer.add_ice_candidate(candidate)
            del self._ice_candidates_buffer[from_node]
        
        # Wait for connection to be ready, then move to active peers
        # This ensures the UI is notified on the answerer side too
        asyncio.create_task(self._finalize_webrtc_connection(from_node))

    async def _handle_webrtc_answer(self, from_node: str, payload: Dict[str, Any]):
        """Handle incoming WebRTC answer (INITIATOR side - Alice)."""
        print(f"Handling WebRTC answer from {from_node}")
        
        webrtc_peer = self._pending_webrtc.get(from_node)
        if not webrtc_peer:
            print(f"No pending WebRTC connection for {from_node}")
            return
        
        # Set remote description with answer
        answer_sdp = payload.get("answer")
        await webrtc_peer.handle_answer(answer_sdp)
        
        # Process any buffered ICE candidates
        if from_node in self._ice_candidates_buffer:
            for candidate in self._ice_candidates_buffer[from_node]:
                await webrtc_peer.add_ice_candidate(candidate)
            del self._ice_candidates_buffer[from_node]
        
        # Wait for connection to be ready, then move to active peers
        asyncio.create_task(self._finalize_webrtc_connection(from_node))

    async def _handle_ice_candidate(self, from_node: str, payload: Dict[str, Any]):
        """Handle incoming ICE candidate."""
        candidate = payload.get("candidate")
        
        webrtc_peer = self._pending_webrtc.get(from_node) or self.peers.get(from_node)
        
        if webrtc_peer and isinstance(webrtc_peer, WebRTCPeerConnection):
            await webrtc_peer.add_ice_candidate(candidate)
        else:
            # Buffer ICE candidates if connection not yet established
            if from_node not in self._ice_candidates_buffer:
                self._ice_candidates_buffer[from_node] = []
            self._ice_candidates_buffer[from_node].append(candidate)
            print(f"Buffered ICE candidate from {from_node} (total: {len(self._ice_candidates_buffer[from_node])})")

    async def _finalize_webrtc_connection(self, node_id: str):
        """Wait for WebRTC connection to be ready and move to active peers."""
        webrtc_peer = self._pending_webrtc.get(node_id)
        if not webrtc_peer:
            return

        try:
            # Wait for connection to be ready
            await webrtc_peer.wait_ready(timeout=30.0)

            # Move from pending to active peers
            self._pending_webrtc.pop(node_id, None)
            self.peers[node_id] = webrtc_peer

            await self._notify_peer_change()
            print(f"✅ WebRTC connection established with {node_id}")

            # Exchange names (WebRTC doesn't have initial HELLO handshake like TLS)
            from dpc_protocol.protocol import create_hello_message
            try:
                hello_msg = create_hello_message(self.node_id, self.display_name)
                await self.send_message_to_peer(node_id, hello_msg)
                print(f"  - Sent name to {node_id}: {self.display_name}")
            except Exception as e:
                print(f"  - Failed to send name to {node_id}: {e}")

            # Auto-discover peer's available AI providers
            from dpc_protocol.protocol import create_get_providers_message
            try:
                await self.send_message_to_peer(node_id, create_get_providers_message())
                print(f"  - Requested AI providers from {node_id}")
            except Exception as e:
                print(f"  - Failed to request providers from {node_id}: {e}")

        except Exception as e:
            print(f"Failed to finalize WebRTC connection with {node_id}: {e}")
            self._pending_webrtc.pop(node_id, None)
            await webrtc_peer.close()

    async def _handle_connection_timeout(self, node_id: str):
        """Handle timeout for WebRTC connection establishment."""
        try:
            # Wait for connection to be established
            await asyncio.sleep(30)  # 30 second timeout
            
            # Check if connection is still pending
            if node_id in self._pending_webrtc and node_id not in self.peers:
                print(f"WebRTC connection to {node_id} timed out after 30 seconds")
                webrtc_peer = self._pending_webrtc.pop(node_id, None)
                if webrtc_peer:
                    await webrtc_peer.close()
                
                # Clean up ICE candidates buffer
                self._ice_candidates_buffer.pop(node_id, None)
                
        except Exception as e:
            print(f"Error in connection timeout handler: {e}")

    # --- Unified Connection Management ---

    async def send_message_to_peer(self, node_id: str, message: Dict[str, Any]):
        """Sends a message to a specific connected peer (works for both TLS and WebRTC)."""
        if node_id not in self.peers:
            raise ConnectionError(f"Not connected to peer {node_id}.")
        
        peer = self.peers[node_id]
        await peer.send(message)

    async def shutdown_peer_connection(self, peer_id: str):
        """Close connection with a peer (user-initiated)."""
        # Mark this as an intentional disconnect to prevent auto-reconnect
        self._intentional_disconnects.add(peer_id)

        if peer_id in self.peers:
            peer = self.peers.pop(peer_id)
            await peer.close()
            print(f"Connection with {peer_id} closed (intentional).")
            # Ensure status update is broadcast
            await self._notify_peer_change()

        # Also clean up pending WebRTC connections
        if peer_id in self._pending_webrtc:
            webrtc_peer = self._pending_webrtc.pop(peer_id)
            await webrtc_peer.close()
            print(f"Pending WebRTC connection with {peer_id} cleaned up.")
            # Notify peer list change even for pending connections
            await self._notify_peer_change()

        # Clean up buffered ICE candidates
        self._ice_candidates_buffer.pop(peer_id, None)

        # Note: hub_client_refs will be cleaned up in the close handler

    async def shutdown_all(self):
        """Shutdown all connections (intentional shutdown)."""
        print("Shutting down P2P Manager...")

        # Mark all disconnections as intentional to prevent auto-reconnect
        for peer_id in list(self.peers.keys()):
            self._intentional_disconnects.add(peer_id)
        for peer_id in list(self._pending_webrtc.keys()):
            self._intentional_disconnects.add(peer_id)

        if self._server_task:
            self._server_task.cancel()

        for peer_id in list(self.peers.keys()):
            peer = self.peers.pop(peer_id)
            await peer.close()

        for peer_id in list(self._pending_webrtc.keys()):
            webrtc_peer = self._pending_webrtc.pop(peer_id)
            await webrtc_peer.close()

        # Clean up all tracking structures
        self._hub_client_refs.clear()
        self._intentional_disconnects.clear()
        self._ice_candidates_buffer.clear()

        print("P2P Manager shut down.")