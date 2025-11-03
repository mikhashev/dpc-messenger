# dpc-hub/dpc_hub/connection_manager.py

from fastapi import WebSocket
from typing import Dict

class ConnectionManager:
    def __init__(self):
        # Maps node_id to an active WebSocket connection
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, node_id: str):
        await websocket.accept()
        self.active_connections[node_id] = websocket

    def disconnect(self, node_id: str):
        if node_id in self.active_connections:
            del self.active_connections[node_id]

    async def send_personal_message(self, message: str, node_id: str):
        if node_id in self.active_connections:
            await self.active_connections[node_id].send_text(message)

    async def broadcast(self, message: str):
        for connection in self.active_connections.values():
            await connection.send_text(message)

manager = ConnectionManager()