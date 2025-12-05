"""
P2P Coordinator - Coordinates P2P connection lifecycle.

Extracted from service.py as part of Pre-Phase 2 refactoring (Priority 2).
This coordinator provides a clean API layer between CoreService and P2PManager.
"""

import logging
from typing import List
import websockets

logger = logging.getLogger(__name__)


class P2PCoordinator:
    """Coordinates P2P connection lifecycle and messaging."""

    def __init__(self, service):
        """
        Initialize P2PCoordinator with reference to CoreService.

        Args:
            service: CoreService instance (provides access to managers, etc.)
        """
        self.service = service
        self.p2p_manager = service.p2p_manager
        self.hub_client = service.hub_client

    async def connect_via_uri(self, uri: str):
        """
        Connect to peer using dpc:// URI (Direct TLS).

        Supports local network and external IP connections.

        Args:
            uri: dpc:// URI with host, port, and node_id query parameter
        """
        from dpc_protocol.utils import parse_dpc_uri

        logger.info("Orchestrating direct connection to %s", uri)

        # Parse the URI to extract host, port, and node_id
        host, port, target_node_id = parse_dpc_uri(uri)

        # Use connect_directly from P2PManager
        await self.p2p_manager.connect_directly(host, port, target_node_id)

    async def connect_via_hub(self, node_id: str):
        """
        Connect to peer via Hub using WebRTC (with NAT traversal).

        Args:
            node_id: Target peer's node ID

        Raises:
            ConnectionError: If Hub is not connected
        """
        logger.info("Orchestrating WebRTC connection to %s via Hub", node_id)

        # Check if Hub is connected
        if not self.hub_client.websocket or self.hub_client.websocket.state != websockets.State.OPEN:
            raise ConnectionError("Not connected to Hub. Cannot establish WebRTC connection.")

        # Use WebRTC connection via Hub
        await self.p2p_manager.connect_via_hub(
            target_node_id=node_id,
            hub_client=self.hub_client
        )

    async def disconnect(self, node_id: str):
        """
        Disconnect from peer.

        Args:
            node_id: Peer's node ID to disconnect from
        """
        await self.p2p_manager.shutdown_peer_connection(node_id)

    async def test_port_connectivity(self, uri: str) -> dict:
        """
        Test port connectivity before attempting full connection.

        Args:
            uri: dpc:// URI with host, port, and node_id query parameter

        Returns:
            Dict with keys:
            - success (bool): Whether port is accessible
            - message (str): Diagnostic message
            - host (str): Target host
            - port (int): Target port
            - node_id (str): Target node ID
        """
        from dpc_protocol.utils import parse_dpc_uri

        # Parse the URI to extract host and port
        host, port, target_node_id = parse_dpc_uri(uri)

        # Test port connectivity
        success, message = await self.p2p_manager.test_port_connectivity(host, port)

        return {
            "success": success,
            "message": message,
            "host": host,
            "port": port,
            "node_id": target_node_id
        }

    async def send_message(self, target_node_id: str, text: str):
        """
        Send text message to connected peer.

        Args:
            target_node_id: Peer's node ID
            text: Message text

        Raises:
            Exception: If sending fails
        """
        logger.debug("Sending text message to %s: %s", target_node_id, text)

        message = {
            "command": "SEND_TEXT",
            "payload": {
                "text": text
            }
        }

        try:
            await self.p2p_manager.send_message_to_peer(target_node_id, message)
        except Exception as e:
            logger.error("Error sending message to %s: %s", target_node_id, e, exc_info=True)
            raise

    def get_connected_peers(self) -> List[str]:
        """
        Get list of connected peer node IDs.

        Returns:
            List of node IDs currently connected
        """
        return list(self.p2p_manager.peers.keys())

    async def broadcast_to_peers(self, message: dict):
        """
        Broadcast message to all connected peers.

        Used by ConsensusManager for votes and proposals.

        Args:
            message: Message dict to broadcast
        """
        for peer_id in self.get_connected_peers():
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, message)
            except Exception as e:
                logger.warning("Failed to broadcast to %s: %s", peer_id, e)
