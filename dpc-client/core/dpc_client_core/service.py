# dpc-client/core/dpc_client_core/service.py

import asyncio
from dataclasses import asdict
import json
import uuid
import websockets
from pathlib import Path
from typing import Dict, Any, List

from .firewall import ContextFirewall
from .hub_client import HubClient
from .p2p_manager import P2PManager
from .llm_manager import LLMManager
from .local_api import LocalApiServer
from .context_cache import ContextCache
from dpc_protocol.pcm_core import PCMCore, PersonalContext
from dpc_protocol.utils import parse_dpc_uri

# Define the path to the user's D-PC configuration directory
DPC_HOME_DIR = Path.home() / ".dpc"

class CoreService:
    """
    The main orchestrating class for the D-PC client's backend.
    Manages all sub-components and the application's lifecycle.
    """
    def __init__(self):
        print("Initializing D-PC Core Service...")
        
        DPC_HOME_DIR.mkdir(exist_ok=True)

        # Initialize all major components
        self.firewall = ContextFirewall(DPC_HOME_DIR / ".dpc_access")
        self.llm_manager = LLMManager(DPC_HOME_DIR / "providers.toml")
        self.hub_client = HubClient(api_base_url="https://unfascinated-semifiguratively-velda.ngrok-free.dev")
        self.p2p_manager = P2PManager(firewall=self.firewall)
        self.cache = ContextCache()
        
        self.local_api = LocalApiServer(core_service=self)
        
        self._is_running = False
        self._background_tasks = set()
        
        # Store peer metadata (names, profiles, etc.)
        self.peer_metadata: Dict[str, Dict[str, Any]] = {}
        
        # Track pending context requests (for request-response matching)
        self._pending_context_requests: Dict[str, asyncio.Future] = {}
        
        # Set up callbacks AFTER all components are initialized
        self.p2p_manager.set_core_service_ref(self)
        self.p2p_manager.set_on_peer_list_change(self.on_peer_list_change)
        self.p2p_manager.set_on_message_received(self.on_p2p_message_received)
        self._processed_message_ids = set()  # Track processed messages
        self._max_processed_ids = 1000  # Limit set size

    async def start(self):
        """Starts all background services and runs indefinitely."""
        if self._is_running:
            print("Core Service is already running.")
            return

        print("Starting D-PC Core Service...")
        
        self._shutdown_event = asyncio.Event()

        # Start all background tasks
        self._background_tasks.add(asyncio.create_task(self.p2p_manager.start_server()))
        self._background_tasks.add(asyncio.create_task(self.local_api.start()))
        
        # Try to connect to Hub for WebRTC signaling
        try:
            await self.hub_client.login()
            await self.hub_client.connect_signaling_socket()
            
            # Start listening for incoming WebRTC signals
            self._background_tasks.add(asyncio.create_task(self._listen_for_hub_signals()))
            
            print("✅ Successfully connected to Federation Hub for WebRTC signaling.")
        except Exception as e:
            print(f"⚠️ Warning: Could not connect to Hub. WebRTC connections unavailable.")
            print(f"   Error: {e}")
            print(f"   You can still use direct connections via dpc:// URIs.")

        self._is_running = True
        print(f"D-PC Core Service started. Node ID: {self.p2p_manager.node_id}")
        
        # Start hub connection monitor
        task = asyncio.create_task(self._monitor_hub_connection())
        task.set_name("hub_monitor")
        self._background_tasks.add(task)

        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def stop(self):
        """Signals the service to stop and waits for a clean shutdown."""
        if not self._is_running:
            return
        print("Stopping D-PC Core Service...")
        self._shutdown_event.set()

    async def shutdown(self):
        """Performs a clean shutdown of all components."""
        self._is_running = False
        print("Shutting down components...")
        await self.p2p_manager.shutdown_all()
        await self.local_api.stop()
        await self.hub_client.close()
        print("D-PC Core Service shut down.")
    
    async def _monitor_hub_connection(self):
        """Background task to monitor hub connection and auto-reconnect if needed."""
        while self._is_running:
            try:
                await asyncio.sleep(10)
                
                if not self.hub_client.websocket or \
                self.hub_client.websocket.state != websockets.State.OPEN:
                    
                    print("Hub connection lost, attempting to reconnect...")
                    
                    # Cancel old listener task
                    old_listener = None
                    for task in list(self._background_tasks):
                        if task.get_name() == "hub_signals":
                            old_listener = task
                            break
                    
                    if old_listener:
                        print("Cancelling old Hub signal listener...")
                        old_listener.cancel()
                        try:
                            await old_listener
                        except asyncio.CancelledError:
                            pass
                        self._background_tasks.discard(old_listener)
                    
                    # Close old websocket BEFORE reconnecting (THIS IS THE KEY FIX)
                    if self.hub_client.websocket:
                        print("Closing old Hub websocket...")
                        try:
                            await self.hub_client.websocket.close()
                        except:
                            pass
                        self.hub_client.websocket = None
                    
                    try:
                        await self.hub_client.connect_signaling_socket()
                        
                        task = asyncio.create_task(self._listen_for_hub_signals())
                        task.set_name("hub_signals")
                        self._background_tasks.add(task)
                        
                        print("Hub reconnection successful")
                        await self.local_api.broadcast_event("status_update", await self.get_status())
                        
                    except Exception as e:
                        print(f"Hub reconnection failed: {e}")
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in hub connection monitor: {e}")

    async def _listen_for_hub_signals(self):
        """Background task that listens for incoming WebRTC signaling messages from Hub."""
        print("Started listening for Hub WebRTC signals...")
        
        try:
            while self._is_running:
                try:
                    if not self.hub_client.websocket or \
                    self.hub_client.websocket.state != websockets.State.OPEN:
                        print("Hub signaling socket disconnected, stopping listener...")
                        break
                    
                    signal = await self.hub_client.receive_signal()
                    await self.p2p_manager.handle_incoming_signal(signal, self.hub_client)
                    
                except asyncio.CancelledError:
                    break
                except ConnectionError as e:
                    print(f"Hub connection lost: {e}")
                    break
                except Exception as e:
                    print(f"Error receiving Hub signal: {e}")
                    await asyncio.sleep(1)
        finally:
            print("Stopped listening for Hub signals.")
            await self.local_api.broadcast_event("status_update", await self.get_status())


    # --- Callback Methods (called by P2PManager) ---

    async def on_peer_list_change(self):
        """Callback function that is triggered by P2PManager when peer list changes."""
        print("Peer list changed, broadcasting status update to UI.")
        await self.local_api.broadcast_event("status_update", await self.get_status())
    
    async def on_p2p_message_received(self, sender_node_id: str, message: Dict[str, Any]):
        """
        Callback function that is triggered when a P2P message is received.
        Handles different message types and routes them appropriately.
        """
        command = message.get("command")
        payload = message.get("payload", {})
        
        print(f"CoreService received message from {sender_node_id}: {command}")
        
        if command == "SEND_TEXT":
            # Text message from peer - forward to UI with deduplication
            text = payload.get("text")
            
            # Create unique message ID for deduplication
            import hashlib
            import time
            message_id = hashlib.sha256(
                f"{sender_node_id}:{text}:{int(time.time() * 1000)}".encode()
            ).hexdigest()[:16]
            
            # Check if already processed
            if message_id in self._processed_message_ids:
                print(f"Duplicate message detected from {sender_node_id}, skipping")
                return
            
            # Add to processed set
            self._processed_message_ids.add(message_id)
            
            # Clean up old IDs
            if len(self._processed_message_ids) > self._max_processed_ids:
                to_remove = list(self._processed_message_ids)[:self._max_processed_ids // 2]
                for mid in to_remove:
                    self._processed_message_ids.discard(mid)
            
            # Broadcast to UI
            await self.local_api.broadcast_event("new_p2p_message", {
                "sender_node_id": sender_node_id,
                "text": text,
                "message_id": message_id
            })
        
        elif command == "REQUEST_CONTEXT":
            # Context request from peer - handle and respond
            request_id = payload.get("request_id")
            query = payload.get("query")
            await self._handle_context_request(sender_node_id, query, request_id)
        
        elif command == "CONTEXT_RESPONSE":
            # Context response from peer - resolve the pending future
            request_id = payload.get("request_id")
            context_dict = payload.get("context")
            
            if request_id in self._pending_context_requests:
                future = self._pending_context_requests[request_id]
                if not future.done():
                    future.set_result(context_dict)
        
        else:
            print(f"Unknown P2P message command: {command}")

    # --- High-level methods (API for the UI) ---

    async def get_status(self) -> Dict[str, Any]:
        """Aggregates status from all components."""
        
        hub_connected = (
            self.hub_client.websocket is not None and
            self.hub_client.websocket.state == websockets.State.OPEN
        )
        
        # Get peer info with names
        peer_info = []
        for peer_id in self.p2p_manager.peers.keys():
            peer_data = {
                "node_id": peer_id,
                "name": self.peer_metadata.get(peer_id, {}).get("name", None)
            }
            peer_info.append(peer_data)
        
        # Get active model name safely
        try:
            active_model = self.llm_manager.get_active_model_name()
        except (AttributeError, Exception):
            active_model = None
        
        return {
            "node_id": self.p2p_manager.node_id,
            "hub_status": "Connected" if hub_connected else "Disconnected",
            "p2p_peers": list(self.p2p_manager.peers.keys()),
            "peer_info": peer_info,
            "active_ai_model": active_model,
        }
    
    def set_peer_metadata(self, node_id: str, **kwargs):
        """Store metadata for a peer (name, etc)."""
        if node_id not in self.peer_metadata:
            self.peer_metadata[node_id] = {}
        self.peer_metadata[node_id].update(kwargs)

    # --- P2P Connection Methods ---

    async def connect_to_peer(self, uri: str):
        """Connect to a peer using a dpc:// URI (Direct TLS)."""
        print(f"Orchestrating direct connection to {uri}...")
        
        # Parse the URI to extract host, port, and node_id
        host, port, target_node_id = parse_dpc_uri(uri)
        
        # Use connect_directly from P2PManager
        await self.p2p_manager.connect_directly(host, port, target_node_id)

    async def connect_to_peer_by_id(self, node_id: str):
        """
        Orchestrates a P2P connection to a peer using its node_id via Hub.
        Uses WebRTC with NAT traversal.
        """
        print(f"Orchestrating WebRTC connection to {node_id} via Hub...")
        
        # Check if Hub is connected
        if not self.hub_client.websocket or self.hub_client.websocket.state != websockets.State.OPEN:
            raise ConnectionError("Not connected to Hub. Cannot establish WebRTC connection.")
        
        # Use WebRTC connection via Hub
        await self.p2p_manager.connect_via_hub(
            target_node_id=node_id,
            hub_client=self.hub_client
        )

    async def disconnect_from_peer(self, node_id: str):
        """Disconnect from a peer."""
        await self.p2p_manager.shutdown_peer_connection(node_id)

    async def send_p2p_message(self, target_node_id: str, text: str):
        """Send a text message to a connected peer."""
        print(f"Sending text message to {target_node_id}: {text}")
        
        message = {
            "command": "SEND_TEXT",
            "payload": {
                "text": text
            }
        }
        
        try:
            await self.p2p_manager.send_message_to_peer(target_node_id, message)
        except Exception as e:
            print(f"Error sending message to {target_node_id}: {e}")
            raise

    # --- Context Request Methods ---

    async def _handle_context_request(self, peer_id: str, query: str, request_id: str):
        """
        Handle incoming context request from a peer.
        Apply firewall rules and return filtered context.
        """
        print(f"  - Handling context request from {peer_id} (request_id: {request_id})")
        
        # Apply firewall to filter context based on peer
        filtered_context = self.firewall.filter_context_for_peer(
            context=self.p2p_manager.local_context,
            peer_id=peer_id,
            query=query
        )
        
        print(f"  - Context filtered by firewall for {peer_id}")
        
        # Convert to dict for JSON serialization
        context_dict = asdict(filtered_context)
        
        # Send response back to peer with request_id
        response = {
            "command": "CONTEXT_RESPONSE",
            "payload": {
                "request_id": request_id,
                "context": context_dict,
                "query": query
            }
        }
        
        try:
            await self.p2p_manager.send_message_to_peer(peer_id, response)
            print(f"  - Sent filtered context response to {peer_id}")
        except Exception as e:
            print(f"  - Error sending context response to {peer_id}: {e}")

    async def _request_context_from_peer(self, peer_id: str, query: str) -> PersonalContext:
        """
        Request context from a specific peer for the given query.
        Uses async request-response pattern with Future.
        """
        print(f"  - Requesting context from peer: {peer_id}")
        
        if peer_id not in self.p2p_manager.peers:
            print(f"  - Peer {peer_id} not connected, skipping context request.")
            return None
        
        try:
            # Generate unique request ID
            request_id = str(uuid.uuid4())
            
            # Create Future to wait for response
            response_future = asyncio.Future()
            self._pending_context_requests[request_id] = response_future
            
            # Create context request message
            request_message = {
                "command": "REQUEST_CONTEXT",
                "payload": {
                    "request_id": request_id,
                    "query": query,
                    "requestor_id": self.p2p_manager.node_id
                }
            }
            
            # Send request using P2PManager's method
            await self.p2p_manager.send_message_to_peer(peer_id, request_message)
            
            # Wait for response with timeout
            try:
                context_dict = await asyncio.wait_for(response_future, timeout=5.0)
                
                if context_dict:
                    # Convert dict back to PersonalContext object
                    return PersonalContext(**context_dict)
                
            except asyncio.TimeoutError:
                print(f"  - Timeout waiting for context from {peer_id}")
                return None
            finally:
                # Clean up pending request
                self._pending_context_requests.pop(request_id, None)
            
            return None
            
        except Exception as e:
            print(f"  - Error requesting context from {peer_id}: {e}")
            return None

    async def _aggregate_contexts(self, query: str, peer_ids: List[str] = None) -> Dict[str, PersonalContext]:
        """
        Aggregate contexts from local user and connected peers.
        
        Args:
            query: The user's query (used for context relevance)
            peer_ids: Optional list of specific peer IDs to request from.
                     If None, requests from all connected peers.
        
        Returns:
            Dictionary mapping node_id to PersonalContext
        """
        contexts = {}
        
        # Always include local context
        contexts[self.p2p_manager.node_id] = self.p2p_manager.local_context
        
        # Determine which peers to query
        if peer_ids is None:
            peer_ids = list(self.p2p_manager.peers.keys())
        
        # Request context from each peer in parallel
        if peer_ids:
            tasks = [self._request_context_from_peer(peer_id, query) for peer_id in peer_ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for peer_id, result in zip(peer_ids, results):
                if isinstance(result, PersonalContext):
                    contexts[peer_id] = result
                elif result is not None:
                    print(f"  - Error getting context from {peer_id}: {result}")
        
        return contexts

    # --- AI Query Methods ---

    async def execute_ai_query(self, command_id: str, prompt: str, context_ids: list = None, **kwargs):
        """
        Orchestrates an AI query and sends the response back to the UI.
        
        Args:
            command_id: Unique ID for this command
            prompt: The user's prompt/query
            context_ids: Optional list of peer node_ids to fetch context from
            **kwargs: Additional arguments
        """
        print(f"Orchestrating AI query for command_id {command_id}: '{prompt[:50]}...'")
        
        # Start with local context
        aggregated_contexts = {'local': self.p2p_manager.local_context}
        
        # TODO: Fetch remote contexts if context_ids provided
        if context_ids:
            for node_id in context_ids:
                if node_id in self.p2p_manager.peers:
                    # Fetch context from peer
                    # context = await self.request_context_from_peer(node_id, prompt)
                    # aggregated_contexts[node_id] = context
                    pass
        
        # Assemble final prompt with context
        final_prompt = self._assemble_final_prompt(aggregated_contexts, prompt)

        response_payload = {}
        status = "OK"
        
        try:
            # Query the LLM
            response_content = await self.llm_manager.query(prompt=final_prompt)
            response_payload = {"content": response_content}
            
        except Exception as e:
            print(f"  - Error during local inference: {e}")
            status = "ERROR"
            response_payload = {"message": str(e)}

        # Send response back to UI
        await self.local_api.send_response_to_all(
            command_id=command_id,
            command="execute_ai_query",
            status=status,
            payload=response_payload
        )

    def _assemble_final_prompt(self, contexts: dict, clean_prompt: str) -> str:
        """Helper method to assemble the final prompt for the LLM."""
        system_instruction = (
            "You are a helpful AI assistant. Your task is to answer the user's query based on the provided JSON data blobs inside <CONTEXT> tags. "
            "The 'source' attribute of each tag indicates who the context belongs to. The source 'local' refers to the user asking the query. "
            "Other sources are peer nodes who have shared their context to help answer the query. "
            "Analyze all provided contexts to formulate your answer. When relevant, cite which source provided specific information."
        )
        context_blocks = []
        for source_id, context_obj in contexts.items():
            context_dict = asdict(context_obj)
            json_string = json.dumps(context_dict, indent=2, ensure_ascii=False)
            
            # Add peer name if available
            source_label = source_id
            if source_id != 'local' and source_id in self.peer_metadata:
                peer_name = self.peer_metadata[source_id].get('name')
                if peer_name:
                    source_label = f"{peer_name} ({source_id})"
            
            block = f'<CONTEXT source="{source_label}">\n{json_string}\n</CONTEXT>'
            context_blocks.append(block)
        
        final_prompt = (
            f"{system_instruction}\n\n"
            f"--- CONTEXTUAL DATA ---\n"
            f'{"\n\n".join(context_blocks)}\n'
            f"--- END OF CONTEXTUAL DATA ---\n\n"
            f"USER QUERY: {clean_prompt}"
        )
        return final_prompt

    def _build_combined_prompt(self, query: str, contexts: Dict[str, PersonalContext]) -> str:
        """Build a prompt that combines the query with multiple contexts."""
        prompt_parts = ["Context information:"]
        
        for node_id, context in contexts.items():
            source = "You" if node_id == self.p2p_manager.node_id else f"Peer {node_id}"
            prompt_parts.append(f"\n[From {source}]")
            prompt_parts.append(f"Name: {context.profile.get('name', 'Unknown')}")
            if context.profile.get('description'):
                prompt_parts.append(f"Description: {context.profile['description']}")
        
        prompt_parts.append(f"\nUser Query: {query}")
        
        return "\n".join(prompt_parts)

    # --- Hub Methods ---

    async def login_to_hub(self, provider: str = "google"):
        """Login to Hub using OAuth."""
        await self.hub_client.login(provider=provider)
        await self.hub_client.connect_signaling_socket()
        
        # Start listening for signals if not already
        if not any(task.get_name() == "hub_signals" for task in self._background_tasks):
            task = asyncio.create_task(self._listen_for_hub_signals())
            task.set_name("hub_signals")
            self._background_tasks.add(task)
        
        print("Successfully logged in to Hub.")

    async def disconnect_from_hub(self):
        """Disconnect from Hub."""
        # Cancel the signal listening task
        for task in self._background_tasks:
            if task.get_name() == "hub_signals":
                task.cancel()
                self._background_tasks.remove(task)
                break
        
        await self.hub_client.close()
        print("Disconnected from Hub.")
        await self.local_api.broadcast_event("status_update", await self.get_status())