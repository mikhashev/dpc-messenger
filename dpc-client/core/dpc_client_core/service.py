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
        self.hub_client = HubClient(api_base_url="http://127.0.0.1:8000")
        self.p2p_manager = P2PManager(firewall=self.firewall)
        self.p2p_manager.set_core_service_ref(self)
        self.p2p_manager.set_on_peer_list_change(self.on_peer_list_change)
        self.p2p_manager.set_on_message_received(self.on_p2p_message_received)  # Add message handler
        self.cache = ContextCache()
        
        self.local_api = LocalApiServer(core_service=self)
        
        self._is_running = False
        self._background_tasks = set()
        
        # Store peer metadata (names, profiles, etc.)
        self.peer_metadata: Dict[str, Dict[str, Any]] = {}
        
        # Track pending context requests (for request-response matching)
        self._pending_context_requests: Dict[str, asyncio.Future] = {}

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
        
        # Optional: Try to connect to Hub (comment out if you don't want auto-connect)
        # try:
        #     await self.hub_client.login()
        #     await self.hub_client.connect_signaling_socket()
        #     self._background_tasks.add(asyncio.create_task(self._listen_for_hub_signals()))
        #     print("Successfully connected to Federation Hub.")
        # except Exception as e:
        #     print(f"Warning: Could not connect to Hub. Running in offline mode. Error: {e}")

        self._is_running = True
        print("D-PC Core Service is running in offline mode.")
        print("To connect to Hub, click 'Login to Hub' button in the UI.")
        print("Awaiting UI connection...")
        
        await self._shutdown_event.wait()

    async def stop(self):
        """Gracefully stops all services."""
        if not self._is_running:
            return
            
        print("Stopping D-PC Core Service...")
        
        self._shutdown_event.set()
        
        for task in self._background_tasks:
            task.cancel()
        
        try:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        
        await self.p2p_manager.shutdown_all()
        await self.hub_client.close()
        await self.local_api.stop()

        self._is_running = False
        print("D-PC Core Service stopped.")

    async def _listen_for_hub_signals(self):
        """A background task to listen for signaling messages from the Hub."""
        while self._is_running and self.hub_client.websocket and not self.hub_client.websocket.closed:
            try:
                signal = await self.hub_client.receive_signal()
                await self.p2p_manager.handle_incoming_signal(signal, self.hub_client)
                # After handling a signal that might change peer status, broadcast update
                await self.local_api.broadcast_event("status_update", await self.get_status())
            except Exception as e:
                print(f"Error in Hub signal listener: {e}. Disconnecting from Hub.")
                await self.local_api.broadcast_event("status_update", await self.get_status())
                break

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
            # Fallback if method doesn't exist or fails
            active_model = None
        
        return {
            "node_id": self.p2p_manager.node_id,
            "hub_status": "Connected" if hub_connected else "Disconnected",
            "p2p_peers": list(self.p2p_manager.peers.keys()),  # Keep for backward compatibility
            "peer_info": peer_info,  # Detailed peer info with names
            "active_ai_model": active_model,  # Add active model
        }
    
    async def on_peer_list_change(self):
        """Callback function that is triggered by P2PManager."""
        print("Peer list changed, broadcasting status update to UI.")
        await self.local_api.broadcast_event("status_update", await self.get_status())
    
    async def on_p2p_message_received(self, sender_node_id: str, message: Dict[str, Any]):
        """
        Callback function that is triggered when a P2P message is received.
        Handles different message types and routes them appropriately.
        """
        command = message.get("command")
        payload = message.get("payload", {})
        
        print(f"CoreService received message from {sender_node_id}: {message}")
        
        if command == "SEND_TEXT":
            # Text message from peer - forward to UI
            text = payload.get("text")
            await self.local_api.broadcast_event("new_p2p_message", {
                "sender_node_id": sender_node_id,
                "text": text
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
        from dataclasses import asdict
        context_dict = asdict(filtered_context)
        
        # Send response back to peer with request_id
        response = {
            "command": "CONTEXT_RESPONSE",
            "payload": {
                "request_id": request_id,  # Include request_id for matching
                "context": context_dict,
                "query": query
            }
        }
        
        try:
            await self.p2p_manager.send_message_to_peer(peer_id, response)
            print(f"  - Sent filtered context response to {peer_id}")
        except Exception as e:
            print(f"  - Error sending context response to {peer_id}: {e}")
    
    async def send_p2p_message(self, target_node_id: str, text: str):
        """
        Send a text message to a connected peer.
        """
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
    
    async def connect_to_peer(self, uri: str):
        """Connect to a peer using a dpc:// URI."""
        print(f"Orchestrating connection to {uri}...")
        
        # Parse the URI to extract host, port, and node_id
        host, port, target_node_id = parse_dpc_uri(uri)
        
        # Use connect_directly from P2PManager
        await self.p2p_manager.connect_directly(host, port, target_node_id)
        
        # The callback will handle the UI update

    async def disconnect_from_peer(self, node_id: str):
        # This method now only needs to start the process.
        await self.p2p_manager.shutdown_peer_connection(node_id)
        # The on_close handler will trigger the callback.

    async def connect_to_peer_by_id(self, node_id: str):
        """
        Orchestrates a P2P connection to a peer using its node_id via Hub.
        Note: WebRTC via hub is not yet implemented.
        """
        print(f"Orchestrating connection to {node_id} via Hub...")
        # This would use WebRTC signaling via hub
        await self.p2p_manager.connect_via_hub(
            target_node_id=node_id,
            hub_client=self.hub_client
        )

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
                    "request_id": request_id,  # Include request ID
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
            Dictionary mapping source_id to PersonalContext
        """
        print(f"Aggregating contexts for query: '{query[:50]}...'")
        
        aggregated_contexts = {
            'local': self.p2p_manager.local_context
        }
        
        # Determine which peers to request from
        if peer_ids is None:
            peer_ids = list(self.p2p_manager.peers.keys())
        
        if not peer_ids:
            print("  - No connected peers to request context from.")
            return aggregated_contexts
        
        print(f"  - Requesting contexts from {len(peer_ids)} peer(s)...")
        
        # Request contexts from all peers concurrently
        context_tasks = [
            self._request_context_from_peer(peer_id, query)
            for peer_id in peer_ids
        ]
        
        peer_contexts = await asyncio.gather(*context_tasks, return_exceptions=True)
        
        # Add successful peer contexts to aggregated results
        for peer_id, context in zip(peer_ids, peer_contexts):
            if isinstance(context, PersonalContext):
                aggregated_contexts[peer_id] = context
                print(f"  - Successfully received context from {peer_id}")
            elif isinstance(context, Exception):
                print(f"  - Error getting context from {peer_id}: {context}")
        
        print(f"  - Total contexts aggregated: {len(aggregated_contexts)}")
        return aggregated_contexts

    async def execute_ai_query(self, command_id: str, prompt: str, peer_ids: List[str] = None, **kwargs):
        """
        Orchestrates an AI query with context aggregation from peers and sends 
        the response directly back to the UI via the LocalApiServer.
        
        Args:
            command_id: Unique identifier for this command
            prompt: The user's query
            peer_ids: Optional list of peer IDs to request context from.
                     If None, requests from all connected peers.
        """
        print(f"Orchestrating AI query for command_id {command_id}: '{prompt[:50]}...'")
        
        # Aggregate contexts from local and peers
        aggregated_contexts = await self._aggregate_contexts(prompt, peer_ids)
        
        # Assemble final prompt with all contexts
        final_prompt = self._assemble_final_prompt(aggregated_contexts, prompt)

        response_payload = {}
        status = "OK"

        try:
            response_content = await self.llm_manager.query(prompt=final_prompt)
            response_payload = {"type": "full_response", "content": response_content}
            print(f"  - Successfully received response from LLM.")
        except Exception as e:
            print(f"  - Error during local inference: {e}")
            status = "ERROR"
            response_payload = {"message": str(e)}

        # Send the final response back to the UI
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
    
    def set_peer_metadata(self, peer_id: str, name: str = None, **metadata):
        """
        Store metadata about a peer (name, profile, etc.)
        
        Args:
            peer_id: The peer's node ID
            name: Optional display name for the peer
            **metadata: Additional metadata fields
        """
        if peer_id not in self.peer_metadata:
            self.peer_metadata[peer_id] = {}
        
        if name:
            self.peer_metadata[peer_id]['name'] = name
        
        self.peer_metadata[peer_id].update(metadata)
        
        print(f"Updated metadata for peer {peer_id}: {self.peer_metadata[peer_id]}")
    
    async def set_my_name(self, name: str):
        """
        Set your display name that will be shared with peers.
        Can be called from UI.
        """
        self.p2p_manager.display_name = name
        print(f"Your display name set to: {name}")
        
        # Broadcast status update to UI
        await self.local_api.broadcast_event("status_update", await self.get_status())
        
        return {"name": name, "status": "success"}