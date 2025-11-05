# dpc-client/core/dpc_client_core/local_api.py

import asyncio
import json
from typing import TYPE_CHECKING
import websockets
from websockets.server import WebSocketServerProtocol

if TYPE_CHECKING:
    from .service import CoreService

class LocalApiServer:
    def __init__(self, core_service: "CoreService", host: str = "127.0.0.1", port: int = 9999):
        self.core_service = core_service
        self.host = host
        self.port = port
        self.server = None
        self._clients = set()

    async def _register(self, websocket: WebSocketServerProtocol):
        self._clients.add(websocket)
        print(f"UI client connected: {websocket.remote_address}")

    async def _unregister(self, websocket: WebSocketServerProtocol):
        self._clients.remove(websocket)
        print(f"UI client disconnected: {websocket.remote_address}")

    async def _handler(self, websocket: WebSocketServerProtocol):
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

                    handler_method = getattr(self.core_service, command, None)

                    if handler_method and asyncio.iscoroutinefunction(handler_method):
                        print(f"Executing command '{command}' with payload: {payload}")
                        
                        # --- THE CORE FIX ---
                        # For long-running tasks like AI queries, we run them in the background.
                        # The handler method itself will be responsible for sending the response.
                        if command == "execute_ai_query":
                            # Pass the command_id to the handler
                            payload['command_id'] = command_id
                            asyncio.create_task(handler_method(**payload))
                        else:
                            # For short, synchronous-like tasks, we await the result and send it back.
                            result = await handler_method(**payload)
                            response = {"id": command_id, "command": command, "status": "OK", "payload": result}
                            await websocket.send(json.dumps(response))
                        # --------------------

                    else:
                        raise ValueError(f"Unknown or non-async command: {command}")

                except websockets.exceptions.ConnectionClosed:
                    print("UI client connection closed normally.")
                except Exception as e:
                    print(f"Error processing command: {e}")
                    error_response = {
                        "id": command_id if 'command_id' in locals() else None,
                        "command": command if 'command' in locals() else 'unknown',
                        "status": "ERROR",
                        "payload": {"message": str(e)}
                    }
                    await websocket.send(json.dumps(error_response))
        finally:
            await self._unregister(websocket)

    async def start(self):
        print(f"Starting Local API Server on ws://{self.host}:{self.port}")
        self.server = await websockets.serve(self._handler, self.host, self.port)
        print("Local API Server is running.")

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            print("Local API Server stopped.")

    async def broadcast_event(self, event_name: str, payload: dict):
        if not self._clients: return
        message = json.dumps({"event": event_name, "payload": payload})
        await asyncio.gather(*(client.send(message) for client in self._clients))

    async def send_response_to_all(self, command_id: str, command: str, status: str, payload: dict):
        """Helper to send a response to all connected UI clients."""
        if not self._clients: return
        response = {"id": command_id, "command": command, "status": status, "payload": payload}
        await asyncio.gather(*(client.send(json.dumps(response)) for client in self._clients))