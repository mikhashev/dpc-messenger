# dpc-client/core/dpc_client_core/service.py

import asyncio
from dataclasses import asdict
import json
import websockets
from pathlib import Path
from typing import Dict, Any

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
        self.p2p_manager.set_on_peer_list_change(self.on_peer_list_change)
        self.cache = ContextCache()
        
        self.local_api = LocalApiServer(core_service=self)
        
        self._is_running = False
        self._background_tasks = set()
        self.hub_connected = asyncio.Event()

    async def start(self):
        """Starts all background services and runs indefinitely."""
        if self._is_running:
            return

        print("Starting D-PC Core Service...")
        self._shutdown_event = asyncio.Event()

        # --- THE CORE FIX ---
        # Start the P2PManager's own server for direct connections
        self._background_tasks.add(asyncio.create_task(self.p2p_manager.start_server()))
        # --------------------
        
        self._background_tasks.add(asyncio.create_task(self.local_api.start()))

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
        return {
            "node_id": self.p2p_manager.node_id,
            "hub_status": "Connected" if hub_connected else "Disconnected",
            "p2p_peers": list(self.p2p_manager.peers.keys()),
        }
    
    async def on_peer_list_change(self):
        """Callback function that is triggered by P2PManager."""
        print("Peer list changed, broadcasting status update to UI.")
        await self.local_api.broadcast_event("status_update", await self.get_status())
    
    async def connect_to_peer(self, uri: str):
        """
        Orchestrates a P2P connection using the "Smart Connect" strategy.
        """
        try:
            host, port, target_node_id = parse_dpc_uri(uri)
        except ValueError as e:
            raise ValueError(f"Invalid URI provided: {e}")

        # --- "Ultimate Smart Connect" Logic ---

        # 1. Attempt Direct Connection (Plan A)
        try:
            print(f"Smart Connect: Attempting Direct P2P connection to {target_node_id}...")
            await self.p2p_manager.connect_directly(host, port, target_node_id)
            print(f"✅ Smart Connect: Direct P2P connection successful!")
            return # Success!
        except Exception as e:
            print(f"⚠️ Smart Connect: Direct connection failed ({e}). Falling back to Hub-assisted connection.")

        # 2. Fallback to Hub-Assisted Connection (Plan B)
        if not self.hub_connected.is_set():
            raise ConnectionError(
                "Direct connection failed and Hub is not connected. "
                "Please log in to a Hub to connect to peers behind firewalls."
            )
        
        print(f"Smart Connect: Attempting Hub-assisted P2P connection to {target_node_id}...")
        try:
            await self.p2p_manager.connect_via_hub(target_node_id, self.hub_client)
            print(f"✅ Smart Connect: Hub-assisted P2P connection successful!")
        except Exception as e:
            print(f"🔴 Smart Connect: Hub-assisted connection also failed: {e}")
            raise

    async def disconnect_from_peer(self, node_id: str):
        # This method now only needs to start the process.
        await self.p2p_manager.shutdown_peer_connection(node_id)
        # The on_close handler will trigger the callback.

    async def connect_to_peer_by_id(self, node_id: str):
        """Orchestrates a P2P connection to a peer using its node_id."""
        print(f"Orchestrating connection to {node_id}...")
        # The P2PManager will use the HubClient to send signals.
        await self.p2p_manager.connect_to_peer(
            target_node_id=node_id,
            hub_client=self.hub_client
        )

    async def execute_ai_query(self, command_id: str, prompt: str, **kwargs):
        """
        Orchestrates an AI query and sends the response directly back to the UI
        via the LocalApiServer.
        """
        print(f"Orchestrating AI query for command_id {command_id}: '{prompt[:50]}...'")
        
        # ... (context aggregation and prompt assembly are the same)
        aggregated_contexts = {'local': self.p2p_manager.local_context}
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

        # --- THE CORE FIX ---
        # Use the LocalApiServer to send the final response back to the UI.
        await self.local_api.send_response_to_all(
            command_id=command_id,
            command="execute_ai_query",
            status=status,
            payload=response_payload
        )

    def _assemble_final_prompt(self, contexts: dict, clean_prompt: str) -> str:
        """Helper method to assemble the final prompt for the LLM."""
        # This logic is moved from the old cli.py
        system_instruction = (
            "You are a helpful AI assistant. Your task is to answer the user's query based on the provided JSON data blobs inside <CONTEXT> tags. "
            "The 'source' attribute of each tag indicates who the context belongs to. The source 'local' refers to the user asking the query. "
            "Analyze all provided contexts to formulate your answer."
        )
        context_blocks = []
        for source_id, context_obj in contexts.items():
            context_dict = asdict(context_obj)
            json_string = json.dumps(context_dict, indent=2, ensure_ascii=False)
            block = f'<CONTEXT source="{source_id}">\n{json_string}\n</CONTEXT>'
            context_blocks.append(block)
        
        final_prompt = (
            f"{system_instruction}\n\n"
            f"--- CONTEXTUAL DATA ---\n"
            f'{"\n\n".join(context_blocks)}\n'
            f"--- END OF CONTEXTUAL DATA ---\n\n"
            f"USER QUERY: {clean_prompt}"
        )
        return final_prompt
    
    async def login_to_hub(self, provider: str = "google"):
        """
        Initiates the Hub login flow. Called by the UI.
        """
        if self.hub_client.jwt_token:
            print("Already logged into Hub.")
            return await self.get_status()

        try:
            await self.hub_client.login(provider)
            await self.hub_client.connect_signaling_socket()
            
            hub_listen_task = asyncio.create_task(self._listen_for_hub_signals())
            self._background_tasks.add(hub_listen_task)
            
            self.hub_connected.set() # <-- NEW: Set the flag on successful login
            
            print("Successfully connected to Federation Hub.")
            await self.local_api.broadcast_event("status_update", await self.get_status())
            return await self.get_status()
        except Exception as e:
            self.hub_connected.clear() # <-- NEW: Ensure flag is clear on failure
            print(f"Hub login failed: {e}")
            raise