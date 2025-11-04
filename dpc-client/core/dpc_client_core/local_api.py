# dpc-client/core/dpc_client_core/local_api.py

import asyncio
import json
from typing import TYPE_CHECKING
import websockets
from websockets.server import WebSocketServerProtocol

if TYPE_CHECKING:
    # This avoids a circular import error but allows for type hinting
    from .service import CoreService

class LocalApiServer:
    """
    Runs a local WebSocket server to provide an API for the UI frontend.
    """
    def __init__(self, core_service: "CoreService", host: str = "127.0.0.1", port: int = 9999):
        self.core_service = core_service
        self.host = host
        self.port = port
        self.server = None
        self._clients = set()

    async def _register(self, websocket: WebSocketServerProtocol):
        """Registers a new UI client."""
        self._clients.add(websocket)
        print(f"UI client connected: {websocket.remote_address}")

    async def _unregister(self, websocket: WebSocketServerProtocol):
        """Unregisters a UI client."""
        self._clients.remove(websocket)
        print(f"UI client disconnected: {websocket.remote_address}")

    async def _handler(self, websocket: WebSocketServerProtocol):
        """Handles incoming messages from a single UI client."""
        await self._register(websocket)
        try:
            async for message_str in websocket:
                try:
                    message = json.loads(message_str)
                    command_id = message.get("id")
                    command = message.get("command")
                    payload = message.get("payload", {})

                    if not command_id or not command:
                        raise ValueError("'id' and 'command' are required fields.")

                    # --- Command Router ---
                    # Find the method on CoreService that matches the command
                    handler_method = getattr(self.core_service, command, None)

                    if handler_method and asyncio.iscoroutinefunction(handler_method):
                        print(f"Executing command '{command}' with payload: {payload}")
                        result = await handler_method(**payload)
                        response = {
                            "id": command_id,
                            "status": "OK",
                            "payload": result
                        }
                    else:
                        raise ValueError(f"Unknown or non-async command: {command}")

                except Exception as e:
                    print(f"Error processing command: {e}")
                    response = {
                        "id": command_id if 'command_id' in locals() else None,
                        "status": "ERROR",
                        "payload": {"message": str(e)}
                    }
                
                await websocket.send(json.dumps(response))

        finally:
            await self._unregister(websocket)

    async def start(self):
        """Starts the WebSocket API server."""
        print(f"Starting Local API Server on ws://{self.host}:{self.port}")
        self.server = await websockets.serve(self._handler, self.host, self.port)
        print("Local API Server is running.")

    async def stop(self):
        """Stops the WebSocket API server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            print("Local API Server stopped.")

    async def broadcast_event(self, event_name: str, payload: dict):
        """Sends an event to all connected UI clients."""
        if not self._clients:
            return
            
        message = json.dumps({
            "event": event_name,
            "payload": payload
        })
        # Use asyncio.gather to send to all clients concurrently
        await asyncio.gather(*(client.send(message) for client in self._clients))