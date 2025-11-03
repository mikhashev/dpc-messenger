# dpc-client/core/dpc_client_core/service.py

import asyncio
from pathlib import Path
from typing import Dict, Any

from .firewall import ContextFirewall
from .hub_client import HubClient
from .p2p_manager import P2PManager
from .llm_manager import LLMManager
from .local_api import LocalApiServer
from dpc_protocol.pcm_core import PCMCore, PersonalContext
from .context_cache import ContextCache

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
        self.hub_client = HubClient(api_base_url="http://127.0.0.1:8000/api/v1")
        self.p2p_manager = P2PManager(firewall=self.firewall)
        self.cache = ContextCache()
        
        self.local_api = LocalApiServer(core_service=self)
        
        self._is_running = False
        self._background_tasks = set()

    async def start(self):
        """Starts all background services and connects to the Hub."""
        if self._is_running:
            print("Core Service is already running.")
            return

        print("Starting D-PC Core Service...")
        
        # 1. Start the local API server for the UI
        local_api_task = asyncio.create_task(self.local_api.start())
        self._background_tasks.add(local_api_task)

        # 2. Authenticate with the Federation Hub
        try:
            await self.hub_client.login()
            # After login, connect to the signaling WebSocket
            await self.hub_client.connect_signaling_socket()
            # Start listening for signals in the background
            hub_listen_task = asyncio.create_task(self._listen_for_hub_signals())
            self._background_tasks.add(hub_listen_task)
            print("Successfully connected to Federation Hub.")
        except Exception as e:
            print(f"Warning: Could not connect to Hub. Running in offline mode. Error: {e}")

        self._is_running = True
        print("D-PC Core Service started successfully. Awaiting UI connection...")
        
        # Keep the service alive by awaiting the tasks
        await asyncio.gather(*self._background_tasks)

    async def stop(self):
        """Gracefully stops all services."""
        if not self._is_running:
            return
            
        print("Stopping D-PC Core Service...")
        
        for task in self._background_tasks:
            task.cancel()
        
        try:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        
        await self.p2p_manager.shutdown()
        await self.hub_client.close()
        await self.local_api.stop()

        self._is_running = False
        print("D-PC Core Service stopped.")

    async def _listen_for_hub_signals(self):
        """A background task to listen for signaling messages from the Hub."""
        while self._is_running and self.hub_client.websocket and not self.hub_client.websocket.closed:
            try:
                signal = await self.hub_client.receive_signal()
                # When a signal arrives, pass it to the P2PManager to handle
                await self.p2p_manager.handle_incoming_signal(signal, self.hub_client)
            except Exception as e:
                print(f"Error in Hub signal listener: {e}. Disconnecting from Hub.")
                break # Exit the loop if the connection is lost

    # --- High-level methods (API for the UI) ---

    async def get_status(self) -> Dict[str, Any]:
        """Aggregates status from all components."""
        return {
            "node_id": self.p2p_manager.node_id,
            "hub_status": "Connected" if self.hub_client.websocket and not self.hub_client.websocket.closed else "Disconnected",
            "p2p_peers": list(self.p2p_manager.peers.keys()),
        }

    async def connect_to_peer_by_id(self, node_id: str):
        """Orchestrates a P2P connection to a peer using its node_id."""
        print(f"Orchestrating connection to {node_id}...")
        # The P2PManager will use the HubClient to send signals.
        await self.p2p_manager.connect_to_peer(
            target_node_id=node_id,
            hub_client=self.hub_client
        )

    async def execute_ai_query(self, prompt: str, context_ids: list, compute_host_id: str | None = None):
        """Orchestrates a complex AI query."""
        print("Orchestrating AI query...")
        
        # 1. Aggregate contexts
        aggregated_contexts: Dict[str, PersonalContext] = {'local': self.p2p_manager.local_context}
        for alias in context_ids:
            context = self.cache.get(alias)
            if context:
                aggregated_contexts[alias] = context
                continue
            
            # TODO: Request context from peer if not in cache
            print(f"Requesting context for {alias} (not yet implemented)...")

        # 2. Assemble the prompt
        # TODO: Create a real `assemble_final_prompt` function
        final_prompt = f"Contexts: {list(aggregated_contexts.keys())}\n\nQuery: {prompt}"

        # 3. Route the query to the correct compute host
        if compute_host_id and compute_host_id != self.p2p_manager.node_id:
            # This is a remote inference request
            # TODO: Implement remote inference call via P2PManager
            print(f"Routing query to remote host {compute_host_id} (not yet implemented)...")
            return "Remote inference result placeholder."
        else:
            # This is a local inference request
            response = await self.llm_manager.query(prompt=final_prompt)
            return response