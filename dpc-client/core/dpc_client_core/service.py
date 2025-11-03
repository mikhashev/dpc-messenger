# dpc-client/core/dpc_client_core/service.py

import asyncio
from pathlib import Path
from typing import Dict, Any

# These are the modules we will build or adapt.
# Note: We are importing the classes we are about to define.
from .firewall import ContextFirewall
from .hub_client import HubClient
from .p2p_manager import P2PManager
from .llm_manager import LLMManager
from .local_api import LocalApiServer

# Define the path to the user's D-PC configuration directory
DPC_HOME_DIR = Path.home() / ".dpc"

class CoreService:
    """
    The main orchestrating class for the D-PC client's backend.
    Manages all sub-components and the application's lifecycle.
    """
    def __init__(self):
        print("Initializing D-PC Core Service...")
        
        # Ensure the main D-PC directory exists
        DPC_HOME_DIR.mkdir(exist_ok=True)

        # Initialize all major components
        self.firewall = ContextFirewall(DPC_HOME_DIR / ".dpc_access")
        self.llm_manager = LLMManager(DPC_HOME_DIR / "providers.toml")
        self.hub_client = HubClient(api_base_url="http://127.0.0.1:8000/api/v1") # This will be configurable later
        
        # The P2PManager needs the firewall to check permissions on incoming requests
        self.p2p_manager = P2PManager(firewall=self.firewall)
        
        # The local API server needs a reference back to the CoreService
        # to execute commands from the UI.
        self.local_api = LocalApiServer(core_service=self)
        
        self._is_running = False
        self._background_tasks = set()

    async def start(self):
        """Starts all background services and connects to the Hub."""
        if self._is_running:
            print("Core Service is already running.")
            return

        print("Starting D-PC Core Service...")
        
        # 1. Start the server that listens for other D-PC peers
        p2p_task = asyncio.create_task(self.p2p_manager.start_server())
        self._background_tasks.add(p2p_task)

        # 2. Start the local API server for the UI
        local_api_task = asyncio.create_task(self.local_api.start())
        self._background_tasks.add(local_api_task)

        # 3. Authenticate with the Federation Hub
        try:
            await self.hub_client.login()
            # After login, connect to the signaling WebSocket
            await self.hub_client.connect_signaling_socket()
            # Start listening for signals in the background
            hub_listen_task = asyncio.create_task(self._listen_for_hub_signals())
            self._background_tasks.add(hub_listen_task)
        except Exception as e:
            print(f"Could not connect to Hub. Running in offline mode. Error: {e}")

        self._is_running = True
        print("D-PC Core Service started successfully.")
        
        # This will keep the service running until stop() is called
        await asyncio.gather(*self._background_tasks)

    async def stop(self):
        """Gracefully stops all services."""
        if not self._is_running:
            return
            
        print("Stopping D-PC Core Service...")
        
        for task in self._background_tasks:
            task.cancel()
        
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        
        await self.p2p_manager.shutdown()
        await self.hub_client.close()
        await self.local_api.stop()

        self._is_running = False
        print("D-PC Core Service stopped.")

    async def _listen_for_hub_signals(self):
        """A background task to listen for signaling messages from the Hub."""
        while self._is_running:
            try:
                signal = await self.hub_client.receive_signal()
                # When a signal arrives, pass it to the P2PManager to handle
                await self.p2p_manager.handle_incoming_signal(signal)
            except Exception as e:
                print(f"Error in Hub signal listener: {e}")
                # Wait a bit before retrying
                await asyncio.sleep(5)

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
        # 1. Find the peer on the Hub
        search_result = await self.hub_client.search_users(topic=f"node_id:{node_id}") # Fictional search syntax
        if not search_result["results"]:
            raise ValueError(f"Node {node_id} not found on the Hub.")
        
        # 2. Initiate P2P connection (this will involve signaling)
        # The P2PManager will use the HubClient to send signals.
        await self.p2p_manager.connect_to_peer(
            target_node_id=node_id,
            hub_client=self.hub_client
        )