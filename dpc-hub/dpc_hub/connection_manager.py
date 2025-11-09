"""
WebSocket connection manager for signaling.

Manages active WebSocket connections for WebRTC signaling between peers.
"""

import logging
from typing import Dict
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages WebSocket connections for P2P signaling.
    
    Maps node_id to active WebSocket connections.
    Handles connection lifecycle and message relay.
    """
    
    def __init__(self):
        """Initialize the connection manager"""
        # Maps node_id to WebSocket connection
        self.active_connections: Dict[str, WebSocket] = {}
        logger.info("ConnectionManager initialized")
    
    async def connect(self, websocket: WebSocket, node_id: str):
        """
        Accept and register a new WebSocket connection.
        
        Args:
            websocket: WebSocket connection
            node_id: User's node ID
        """
        await websocket.accept()
        
        # If user already has a connection, close the old one
        if node_id in self.active_connections:
            old_socket = self.active_connections[node_id]
            logger.warning(f"Replacing existing connection for {node_id}")
            try:
                await old_socket.close(code=1000, reason="New connection established")
            except Exception as e:
                logger.error(f"Error closing old connection: {e}")
        
        self.active_connections[node_id] = websocket
        logger.info(f"WebSocket connected for node: {node_id} (total: {len(self.active_connections)})")
    
    def disconnect(self, node_id: str):
        """
        Remove a WebSocket connection.
        
        Args:
            node_id: User's node ID
        """
        if node_id in self.active_connections:
            del self.active_connections[node_id]
            logger.info(f"WebSocket disconnected for node: {node_id} (remaining: {len(self.active_connections)})")
    
    def is_connected(self, node_id: str) -> bool:
        """
        Check if a node is currently connected.
        
        Args:
            node_id: Node ID to check
        
        Returns:
            True if node has active connection
        """
        return node_id in self.active_connections
    
    async def send_personal_message(self, message: str, node_id: str) -> bool:
        """
        Send a message to a specific node.
        
        Args:
            message: Message string to send (usually JSON)
            node_id: Target node ID
        
        Returns:
            True if message was sent successfully, False otherwise
        """
        if node_id not in self.active_connections:
            logger.warning(f"Attempted to send message to disconnected node: {node_id}")
            return False
        
        try:
            websocket = self.active_connections[node_id]
            await websocket.send_text(message)
            logger.debug(f"Sent message to {node_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send message to {node_id}: {e}")
            # Clean up dead connection
            self.disconnect(node_id)
            return False
    
    async def broadcast(self, message: str, exclude_node: str = None):
        """
        Broadcast a message to all connected nodes.
        
        Args:
            message: Message string to broadcast
            exclude_node: Optional node_id to exclude from broadcast
        """
        disconnected_nodes = []
        
        for node_id, connection in self.active_connections.items():
            if exclude_node and node_id == exclude_node:
                continue
            
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.error(f"Failed to broadcast to {node_id}: {e}")
                disconnected_nodes.append(node_id)
        
        # Clean up failed connections
        for node_id in disconnected_nodes:
            self.disconnect(node_id)
        
        logger.info(f"Broadcast to {len(self.active_connections) - len(disconnected_nodes)} nodes")
    
    def get_connection_count(self) -> int:
        """
        Get the number of active connections.
        
        Returns:
            Number of active WebSocket connections
        """
        return len(self.active_connections)
    
    def get_connected_nodes(self) -> list[str]:
        """
        Get list of all connected node IDs.
        
        Returns:
            List of node_id strings
        """
        return list(self.active_connections.keys())
    
    async def close_all(self):
        """
        Close all WebSocket connections.
        
        Used during shutdown.
        """
        logger.info(f"Closing all {len(self.active_connections)} WebSocket connections")
        
        for node_id, websocket in list(self.active_connections.items()):
            try:
                await websocket.close(code=1001, reason="Server shutting down")
            except Exception as e:
                logger.error(f"Error closing connection for {node_id}: {e}")
        
        self.active_connections.clear()
        logger.info("All WebSocket connections closed")


# Global instance
manager = ConnectionManager()


def get_manager() -> ConnectionManager:
    """
    Get the global connection manager instance.
    
    Returns:
        ConnectionManager instance
    """
    return manager