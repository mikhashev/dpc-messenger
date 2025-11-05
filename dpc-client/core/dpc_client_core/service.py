# dpc-client/core/dpc_client_core/service.py

import asyncio
import json
from pathlib import Path
from typing import Dict, Any
from dataclasses import asdict

import websockets # Ensure this is imported for the type hint in get_status

from .firewall import ContextFirewall
from .hub_client import HubClient
from .p2p_manager import P2PManager
from .llm_manager import LLMManager
from .local_api import LocalApiServer
from dpc_protocol.pcm_core import PCMCore, PersonalContext
from .context_cache import ContextCache
from dpc_protocol.utils import parse_dpc_uri
from dpc_protocol.protocol import create_send_text_message

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
        self.cache = ContextCache()
        
        # Register callbacks
        self.p2p_manager.set_on_peer_list_change(self.on_peer_list_change)
        self.p2p_manager.set_on_message_received(self.on_p2p_message)
        
        self.local_api = LocalApiServer(core_service=self)
        
        self._is_running = False
        self._background_tasks = set()
        self._shutdown_event = asyncio.Event()
        self.hub_connected = asyncio.Event()

    async def start(self):
        """
        Starts background services in a stable offline mode and runs indefinitely.
        """
        if self._is_running:
            return
        print("Starting D-PC Core Service components...")
        
        # Start all essential background tasks that can run offline
        self._background_tasks.add(asyncio.create_task(self.p2p_manager.start_server()))
        self._background_tasks.add(asyncio.create_task(self.local_api.start()))

        self._is_running = True
        print("D-PC Core Service is running in offline mode. Awaiting UI connection.")
        
        # Wait until stop() is called
        await self._shutdown_event.wait()

    async def stop(self):
        """Gracefully stops all services."""
        if not self._is_running:
            return
            
        print("Stopping D-PC Core Service...")
        
        self._is_running = False
        self._shutdown_event.set() # Release the main loop in start()
        
        # Give a moment for tasks to receive cancellation
        await asyncio.sleep(0.1)

        for task in self._background_tasks:
            task.cancel()
        
        try:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        
        await self.p2p_manager.shutdown_all()
        await self.hub_client.close()
        await self.local_api.stop()
        print("D-PC Core Service stopped.")

    async def _listen_for_hub_signals(self):
        """A background task to listen for signaling messages from the Hub."""
        while self._is_running and self.hub_client.websocket and not self.hub_client.websocket.closed:
            try:
                signal = await self.hub_client.receive_signal()
                await self.p2p_manager.handle_incoming_signal(signal, self.hub_client)
            except Exception as e:
                print(f"Error in Hub signal listener: {e}. Disconnecting from Hub.")
                self.hub_connected.clear()
                await self.local_api.broadcast_event("status_update", await self.get_status())
                break

    # --- High-level methods (API for the UI) ---

    async def get_status(self) -> Dict[str, Any]:
        """Aggregates status from all components."""
        hub_is_connected = self.hub_connected.is_set()
        return {
            "node_id": self.p2p_manager.node_id,
            "hub_status": "Connected" if hub_is_connected else "Disconnected",
            "p2p_peers": list(self.p2p_manager.peers.keys()),
        }

    async def login_to_hub(self, **kwargs):
        """
        Initiates the Hub login flow. Called by the UI.
        This is a non-blocking, fire-and-forget task from the UI's perspective.
        """
        if self.hub_connected.is_set():
            print("Already logged into Hub.")
            return
        
        # Wrap the entire login and listen logic in a background task.
        # This ensures that if it fails, it doesn't crash the main service.
        login_task = asyncio.create_task(self._login_and_listen_task())
        self._background_tasks.add(login_task)

    async def _login_and_listen_task(self):
        """The actual background task for the Hub connection lifecycle."""
        print("Hub login requested by UI...")
        try:
            await self.hub_client.login("google")
            await self.hub_client.connect_signaling_socket()
            
            self.hub_connected.set()
            print("Successfully connected to Federation Hub.")
            
            await self.local_api.broadcast_event("status_update", await self.get_status())
            
            await self._listen_for_hub_signals()

        except Exception as e:
            print(f"Hub connection lifecycle failed: {e}")
            self.hub_connected.clear()
            await self.hub_client.close()
            await self.local_api.broadcast_event("status_update", await self.get_status())

    async def connect_to_peer(self, uri: str):
        """Orchestrates a P2P connection using the "Smart Connect" strategy."""
        try:
            host, port, target_node_id = parse_dpc_uri(uri)
        except ValueError as e:
            raise ValueError(f"Invalid URI provided: {e}")

        # Plan A: Attempt Direct Connection
        try:
            print(f"Smart Connect: Attempting Direct P2P connection to {target_node_id}...")
            await self.p2p_manager.connect_directly(host, port, target_node_id)
            print(f"✅ Smart Connect: Direct P2P connection successful!")
            return
        except Exception as e:
            print(f"⚠️ Smart Connect: Direct connection failed ({e}). Falling back to Hub-assisted connection.")

        # Plan B: Fallback to Hub-Assisted Connection
        if not self.hub_connected.is_set():
            raise ConnectionError("Direct connection failed and Hub is not connected. Please log in to a Hub to connect to peers behind firewalls.")
        
        print(f"Smart Connect: Attempting Hub-assisted P2P connection to {target_node_id}...")
        await self.p2p_manager.connect_via_hub(target_node_id, self.hub_client)

    async def disconnect_from_peer(self, node_id: str):
        """Disconnects from a specific peer."""
        await self.p2p_manager.shutdown_peer_connection(node_id)

    async def on_peer_list_change(self):
        """Callback function that is triggered by P2PManager."""
        print("Peer list changed, broadcasting status update to UI.")
        await self.local_api.broadcast_event("status_update", await self.get_status())

    async def on_p2p_message(self, sender_node_id: str, message: Dict[str, Any]):
        """Callback function that is triggered by P2PManager for any incoming message."""
        print(f"CoreService received message from {sender_node_id}: {message}")
        
        command = message.get("command")
        if command == "SEND_TEXT":
            # This is a chat message. Broadcast it to the UI.
            await self.local_api.broadcast_event(
                "new_p2p_message",
                {
                    "sender_node_id": sender_node_id,
                    "text": message.get("payload", {}).get("text")
                }
            )
        elif command == "GET_CONTEXT":
            # In the future, we would handle GET_CONTEXT here
            pass

    async def send_p2p_message(self, target_node_id: str, text: str, **kwargs):
        """Sends a text message to a peer. Called by the UI."""
        print(f"Sending text message to {target_node_id}: {text}")
        message = create_send_text_message(text=text)
        await self.p2p_manager.send_message_to_peer(target_node_id, message)

    async def execute_ai_query(self, command_id: str, prompt: str, **kwargs):
        """Orchestrates an AI query and sends the response back to the UI."""
        print(f"Orchestrating AI query for command_id {command_id}: '{prompt[:50]}...'")
        
        aggregated_contexts = {'local': self.p2p_manager.local_context}
        # TODO: Add logic to fetch remote contexts based on kwargs['context_ids']
        
        final_prompt = self._assemble_final_prompt(aggregated_contexts, prompt)

        response_payload = {}
        status = "OK"
        try:
            response_content = await self.llm_manager.query(prompt=final_prompt)
            response_payload = {"content": response_content}
        except Exception as e:
            print(f"  - Error during local inference: {e}")
            status = "ERROR"
            response_payload = {"message": str(e)}

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