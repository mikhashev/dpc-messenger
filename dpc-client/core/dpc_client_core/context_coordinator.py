"""
Context Coordinator - Coordinates context requests and responses between peers.

Extracted from service.py as part of Pre-Phase 2 refactoring (Priority 2).
This coordinator handles peer context fetching with firewall filtering.
"""

import logging
import asyncio
import uuid
from typing import Dict, Optional
from dataclasses import asdict
from dpc_protocol.pcm_core import PersonalContext

logger = logging.getLogger(__name__)


class ContextCoordinator:
    """Coordinates context requests and responses with firewall filtering."""

    def __init__(self, service):
        """
        Initialize ContextCoordinator with reference to CoreService.

        Args:
            service: CoreService instance (provides access to managers, etc.)
        """
        self.service = service
        self.firewall = service.firewall
        self.p2p_manager = service.p2p_manager

        # Pending request tracking (for async request-response pattern)
        self._pending_context_requests: Dict[str, asyncio.Future] = {}
        self._pending_device_context_requests: Dict[str, asyncio.Future] = {}

    async def request_peer_context(
        self,
        peer_id: str,
        query: str
    ) -> Optional[PersonalContext]:
        """
        Request personal context from peer.

        Uses async request-response pattern with Future.

        Args:
            peer_id: Node ID of peer to request context from
            query: The query context for filtering

        Returns:
            PersonalContext object or None if failed
        """
        logger.debug("Requesting context from peer: %s", peer_id)

        if peer_id not in self.p2p_manager.peers:
            logger.debug("Peer %s not connected, skipping context request", peer_id)
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
                    "request_id": request_id,
                    "query": query,
                    "requestor_id": self.p2p_manager.node_id
                }
            }

            # Send request using P2PManager
            await self.p2p_manager.send_message_to_peer(peer_id, request_message)

            # Wait for response with timeout
            try:
                context = await asyncio.wait_for(response_future, timeout=5.0)
                return context if context else None

            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for context from %s", peer_id)
                return None
            finally:
                # Clean up pending request
                self._pending_context_requests.pop(request_id, None)

        except Exception as e:
            logger.error("Error requesting context from %s: %s", peer_id, e, exc_info=True)
            return None

    async def request_device_context(
        self,
        peer_id: str
    ) -> Optional[Dict]:
        """
        Request device context from peer.

        Uses async request-response pattern with Future.

        Args:
            peer_id: Node ID of peer to request device context from

        Returns:
            Dict containing device context or None if failed
        """
        logger.debug("Requesting device context from peer: %s", peer_id)

        if peer_id not in self.p2p_manager.peers:
            logger.debug("Peer %s not connected, skipping device context request", peer_id)
            return None

        try:
            # Generate unique request ID
            request_id = str(uuid.uuid4())

            # Create Future to wait for response
            response_future = asyncio.Future()
            self._pending_device_context_requests[request_id] = response_future

            # Create device context request message
            request_message = {
                "command": "REQUEST_DEVICE_CONTEXT",
                "payload": {
                    "request_id": request_id,
                    "requestor_id": self.p2p_manager.node_id
                }
            }

            # Send request using P2PManager
            await self.p2p_manager.send_message_to_peer(peer_id, request_message)

            # Wait for response with timeout
            try:
                device_context_dict = await asyncio.wait_for(response_future, timeout=5.0)
                return device_context_dict if device_context_dict else None

            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for device context from %s", peer_id)
                return None
            finally:
                # Clean up pending request
                self._pending_device_context_requests.pop(request_id, None)

        except Exception as e:
            logger.error("Error requesting device context from %s: %s", peer_id, e, exc_info=True)
            return None

    async def handle_context_request(
        self,
        peer_id: str,
        query: str,
        request_id: str
    ):
        """
        Handle incoming personal context request from peer.

        Applies firewall rules and returns filtered context.

        Args:
            peer_id: Node ID of requesting peer
            query: Query context for filtering
            request_id: Unique request ID for response matching
        """
        logger.debug("Handling context request from %s (request_id: %s)", peer_id, request_id)

        # Apply firewall to filter context based on peer
        filtered_context = self.firewall.filter_context_for_peer(
            context=self.p2p_manager.local_context,
            peer_id=peer_id,
            query=query
        )

        logger.debug("Context filtered by firewall for %s", peer_id)

        # Convert to dict for JSON serialization
        context_dict = asdict(filtered_context)

        # Send response back to peer with request_id
        response = {
            "command": "CONTEXT_RESPONSE",
            "payload": {
                "request_id": request_id,
                "context": context_dict,
                "query": query
            }
        }

        try:
            await self.p2p_manager.send_message_to_peer(peer_id, response)
            logger.debug("Sent filtered context response to %s", peer_id)
        except Exception as e:
            logger.error("Error sending context response to %s: %s", peer_id, e, exc_info=True)

    async def handle_device_context_request(
        self,
        peer_id: str,
        request_id: str
    ):
        """
        Handle incoming device context request from peer.

        Applies firewall rules and returns filtered device context.

        Args:
            peer_id: Node ID of requesting peer
            request_id: Unique request ID for response matching
        """
        logger.debug("Handling device context request from %s (request_id: %s)", peer_id, request_id)

        # Check if device context is available
        if not self.service.device_context:
            logger.debug("Device context not available, sending empty response")
            response = {
                "command": "DEVICE_CONTEXT_RESPONSE",
                "payload": {
                    "request_id": request_id,
                    "device_context": {},
                    "error": "Device context not available"
                }
            }
        else:
            # Apply firewall to filter device context based on peer
            filtered_device_context = self.firewall.filter_device_context_for_peer(
                device_context=self.service.device_context,
                peer_id=peer_id
            )

            logger.debug("Device context filtered by firewall for %s", peer_id)

            # Send response back to peer with request_id
            response = {
                "command": "DEVICE_CONTEXT_RESPONSE",
                "payload": {
                    "request_id": request_id,
                    "device_context": filtered_device_context
                }
            }

        try:
            await self.p2p_manager.send_message_to_peer(peer_id, response)
            logger.debug("Sent filtered device context response to %s", peer_id)
        except Exception as e:
            logger.error("Error sending device context response to %s: %s", peer_id, e, exc_info=True)

    def resolve_context_response(self, request_id: str, context: PersonalContext):
        """
        Resolve pending context request with response.

        Called by message handlers when CONTEXT_RESPONSE arrives.

        Args:
            request_id: Request ID to resolve
            context: PersonalContext object from peer
        """
        if request_id in self._pending_context_requests:
            future = self._pending_context_requests[request_id]
            if not future.done():
                future.set_result(context)
                logger.debug("Resolved context request %s", request_id)

    def resolve_device_context_response(self, request_id: str, device_context: Dict):
        """
        Resolve pending device context request with response.

        Called by message handlers when DEVICE_CONTEXT_RESPONSE arrives.

        Args:
            request_id: Request ID to resolve
            device_context: Device context dict from peer
        """
        if request_id in self._pending_device_context_requests:
            future = self._pending_device_context_requests[request_id]
            if not future.done():
                future.set_result(device_context)
                logger.debug("Resolved device context request %s", request_id)
