"""
P2P Coordinator - Coordinates P2P connection lifecycle and request handling.

Extracted from service.py as part of Pre-Phase 2 refactoring (Priority 2).
This coordinator provides a clean API layer between CoreService and P2PManager.
Expanded in Phase C Step 5 with incoming P2P request handlers.
"""

import asyncio
import logging
from typing import Dict, List
import websockets

logger = logging.getLogger(__name__)


class P2PCoordinator:
    """Coordinates P2P connection lifecycle, messaging, and request handling."""

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

    # ─────────────────────────────────────────────────────────────
    # Incoming P2P request handlers (Phase C Step 5 Batch 1)
    # ─────────────────────────────────────────────────────────────

    async def handle_inference_request(self, peer_id: str, request_id: str, prompt: str, model: str = None, provider: str = None, images: list = None):
        """Handle incoming remote inference request from a peer."""
        from dpc_protocol.protocol import create_remote_inference_response

        logger.debug("Handling inference request from %s (request_id: %s, images: %s)", peer_id, request_id, "yes" if images else "no")

        if not self.service.firewall.can_request_inference(peer_id, model):
            logger.warning("Access denied: %s cannot request inference%s", peer_id, f" for model {model}" if model else "")
            error_response = create_remote_inference_response(
                request_id=request_id,
                error=f"Access denied: You are not authorized to request inference" + (f" for model {model}" if model else "")
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as e:
                logger.error("Error sending inference error response to %s: %s", peer_id, e, exc_info=True)
            return

        try:
            logger.info("Running inference for %s (model: %s, provider: %s)", peer_id, model or 'default', provider or 'default')

            provider_alias_to_use = provider
            if model and not provider:
                found_alias = self.service.llm_manager.find_provider_by_model(model)
                if found_alias:
                    provider_alias_to_use = found_alias
                    logger.debug("Found provider '%s' for model '%s'", found_alias, model)
                else:
                    raise ValueError(f"No provider found for model '{model}'")

            result = await self.service.llm_manager.query(prompt, provider_alias=provider_alias_to_use, images=images, return_metadata=True)
            logger.info("Inference completed successfully for %s", peer_id)

            actual_model = result.get("model", model)
            success_response = create_remote_inference_response(
                request_id=request_id,
                response=result["response"],
                tokens_used=result.get("tokens_used"),
                prompt_tokens=result.get("prompt_tokens"),
                response_tokens=result.get("response_tokens"),
                model_max_tokens=result.get("model_max_tokens"),
                model=actual_model,
                provider=result.get("provider"),
                thinking=result.get("thinking"),
                thinking_tokens=result.get("thinking_tokens")
            )
            await self.p2p_manager.send_message_to_peer(peer_id, success_response)
            logger.debug("Sent inference result to %s", peer_id)

        except Exception as e:
            logger.error("Inference failed for %s: %s", peer_id, e, exc_info=True)
            error_response = create_remote_inference_response(request_id=request_id, error=str(e))
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as send_err:
                logger.error("Error sending inference error response to %s: %s", peer_id, send_err, exc_info=True)

    async def handle_transcription_request(self, peer_id: str, request_id: str, audio_base64: str, mime_type: str, model: str = None, provider: str = None, language: str = "auto", task: str = "transcribe"):
        """Handle incoming remote transcription request from a peer."""
        from dpc_protocol.protocol import create_remote_transcription_response
        import base64
        import tempfile
        import os

        logger.debug("Handling transcription request from %s (request_id: %s, mime_type: %s)", peer_id, request_id, mime_type)

        if not self.service.firewall.can_request_transcription(peer_id, model):
            logger.warning("Access denied: %s cannot request transcription%s", peer_id, f" for model {model}" if model else "")
            error_response = create_remote_transcription_response(
                request_id=request_id,
                error=f"Access denied: You are not authorized to request transcription" + (f" for model {model}" if model else "")
            )
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as e:
                logger.error("Error sending transcription error response to %s: %s", peer_id, e, exc_info=True)
            return

        temp_audio_path = None
        try:
            logger.info("Running transcription for %s (model: %s, provider: %s, language: %s, task: %s)",
                       peer_id, model or 'default', provider or 'default', language, task)

            audio_data = base64.b64decode(audio_base64)
            ext_map = {
                "audio/webm": ".webm", "audio/opus": ".opus", "audio/ogg": ".ogg",
                "audio/wav": ".wav", "audio/mp3": ".mp3", "audio/mp4": ".mp4", "audio/mpeg": ".mp3"
            }
            file_ext = ext_map.get(mime_type, ".webm")

            with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
                temp_audio_path = temp_file.name
                temp_file.write(audio_data)

            provider_alias_to_use = provider
            if model and not provider:
                found_alias = self.service.llm_manager.find_provider_by_model(model)
                if found_alias:
                    provider_alias_to_use = found_alias
                else:
                    raise ValueError(f"No provider found for model '{model}'")

            provider_instance = self.service.llm_manager.providers.get(provider_alias_to_use)
            if not hasattr(provider_instance, 'transcribe'):
                raise ValueError(f"Provider '{provider_alias_to_use}' does not support transcription")

            if language != "auto":
                provider_instance.language = language
            if task:
                provider_instance.task = task

            result = await provider_instance.transcribe(temp_audio_path)
            logger.info("Transcription completed successfully for %s", peer_id)

            success_response = create_remote_transcription_response(
                request_id=request_id,
                text=result.get("text", ""),
                language=result.get("language"),
                duration_seconds=result.get("duration"),
                provider=result.get("provider") or provider_alias_to_use
            )
            await self.p2p_manager.send_message_to_peer(peer_id, success_response)

        except Exception as e:
            logger.error("Transcription failed for %s: %s", peer_id, e, exc_info=True)
            error_response = create_remote_transcription_response(request_id=request_id, error=str(e))
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, error_response)
            except Exception as send_err:
                logger.error("Error sending transcription error response to %s: %s", peer_id, send_err, exc_info=True)
        finally:
            if temp_audio_path and os.path.exists(temp_audio_path):
                try:
                    os.unlink(temp_audio_path)
                except Exception as cleanup_err:
                    logger.warning("Failed to delete temporary audio file %s: %s", temp_audio_path, cleanup_err)

    async def handle_get_providers_request(self, peer_id: str):
        """Handle GET_PROVIDERS request — send available providers filtered by firewall."""
        from dpc_protocol.protocol import create_providers_response

        logger.debug("Handling GET_PROVIDERS request from %s", peer_id)

        has_compute_access = self.service.firewall.can_request_inference(peer_id)
        has_transcription_access = self.service.firewall.can_request_transcription(peer_id)

        if not has_compute_access and not has_transcription_access:
            logger.warning("Access denied: %s cannot access compute or transcription resources", peer_id)
            response = create_providers_response([])
            try:
                await self.p2p_manager.send_message_to_peer(peer_id, response)
            except Exception as e:
                logger.error("Error sending providers response to %s: %s", peer_id, e, exc_info=True)
            return

        all_providers = []
        for alias, provider in self.service.llm_manager.providers.items():
            model = provider.model
            provider_type = provider.config.get("type", "unknown")
            provider_info = {
                "alias": alias, "model": model, "type": provider_type,
                "supports_vision": provider.supports_vision(),
                "supports_voice": self.service._provider_supports_voice(provider)
            }
            all_providers.append(provider_info)

        filtered_providers = []
        for provider_info in all_providers:
            provider_type = provider_info["type"]
            model = provider_info["model"]
            if provider_type == "local_whisper":
                if has_transcription_access and self.service.firewall.can_request_transcription(peer_id, model):
                    filtered_providers.append(provider_info)
            else:
                if has_compute_access and self.service.firewall.can_request_inference(peer_id, model):
                    filtered_providers.append(provider_info)

        logger.debug("Sending %d providers to %s (filtered from %d total)",
                    len(filtered_providers), peer_id[:20], len(all_providers))

        response = create_providers_response(filtered_providers)
        try:
            await self.p2p_manager.send_message_to_peer(peer_id, response)
        except Exception as e:
            logger.error("Error sending providers response to %s: %s", peer_id, e, exc_info=True)

    async def handle_providers_response(self, peer_id: str, providers: list):
        """Handle PROVIDERS_RESPONSE — store in metadata and resolve pending requests."""
        logger.debug("Received %d providers from %s", len(providers), peer_id)

        if peer_id not in self.service.peer_metadata:
            self.service.peer_metadata[peer_id] = {}
        self.service.peer_metadata[peer_id]["providers"] = providers

        pending_future = self.service._pending_providers_requests.pop(peer_id, None)
        if pending_future and not pending_future.done():
            pending_future.set_result(providers)
            logger.debug("Resolved pending providers request for %s", peer_id)

        await self.service.local_api.broadcast_event("peer_providers_updated", {
            "node_id": peer_id,
            "providers": providers
        })

    # ─────────────────────────────────────────────────────────────
    # File transfer coordination (Phase C Step 5 Batch 2)
    # ─────────────────────────────────────────────────────────────

    async def send_file(self, node_id: str, file_path: str, file_size_bytes: int = None):
        """Send a file to a peer via P2P file transfer."""
        from pathlib import Path

        file = Path(file_path)
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        transfer_id = await self.service.file_transfer_manager.send_file(node_id, file)

        size_bytes = file.stat().st_size
        return {
            "transfer_id": transfer_id,
            "status": "pending",
            "filename": file.name,
            "size_bytes": size_bytes
        }

    async def accept_file_transfer(self, transfer_id: str):
        """Accept an incoming file transfer offer."""
        transfer = self.service.file_transfer_manager.active_transfers.get(transfer_id)
        if not transfer:
            raise ValueError(f"Unknown transfer: {transfer_id}")
        if transfer.direction != "download":
            raise ValueError(f"Transfer {transfer_id} is not a download")

        await self.p2p_manager.send_message_to_peer(transfer.node_id, {
            "command": "FILE_ACCEPT",
            "payload": {"transfer_id": transfer_id}
        })
        return {"transfer_id": transfer_id, "status": "accepted"}

    async def cancel_file_transfer(self, transfer_id: str, reason: str = "user_cancelled"):
        """Cancel an active file transfer."""
        transfer = self.service.file_transfer_manager.active_transfers.get(transfer_id)
        await self.service.file_transfer_manager.cancel_transfer(transfer_id, reason)

        if transfer:
            await self.service.local_api.broadcast_event("file_transfer_cancelled", {
                "transfer_id": transfer_id,
                "node_id": transfer.node_id,
                "filename": transfer.filename,
                "direction": transfer.direction,
                "reason": reason,
                "status": "cancelled"
            })
        return {"transfer_id": transfer_id, "status": "cancelled", "reason": reason}

    # ─────────────────────────────────────────────────────────────
    # Outgoing P2P requests (Phase C Step 5 Batch 3)
    # ─────────────────────────────────────────────────────────────

    async def request_inference_from_peer(self, peer_id: str, prompt: str, model: str = None, provider: str = None, images: list = None, timeout: float = 240.0) -> str:
        """Request remote inference from a specific peer."""
        import uuid
        from dpc_protocol.protocol import create_remote_inference_request

        logger.debug("Requesting inference from peer: %s (images: %s)", peer_id, 'yes' if images else 'no')

        if peer_id not in self.p2p_manager.peers:
            raise ConnectionError(f"Peer {peer_id} is not connected")

        try:
            request_id = str(uuid.uuid4())
            response_future = asyncio.Future()
            self.service._pending_inference_requests[request_id] = response_future

            request_message = create_remote_inference_request(
                request_id=request_id, prompt=prompt,
                model=model, provider=provider, images=images
            )
            await self.p2p_manager.send_message_to_peer(peer_id, request_message)

            try:
                result = await asyncio.wait_for(response_future, timeout=timeout)
                logger.info("Received inference result from %s", peer_id)
                return result
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for inference from %s", peer_id)
                raise TimeoutError(f"Inference request to {peer_id} timed out after {timeout}s")
            finally:
                self.service._pending_inference_requests.pop(request_id, None)
        except Exception as e:
            logger.error("Error requesting inference from %s: %s", peer_id, e, exc_info=True)
            raise

    async def request_transcription_from_peer(
        self, peer_id: str, audio_base64: str, mime_type: str,
        model: str = None, provider: str = None, language: str = "auto",
        task: str = "transcribe", timeout: float = 120.0
    ) -> Dict[str, any]:
        """Request remote transcription from a specific peer."""
        import uuid
        from dpc_protocol.protocol import create_remote_transcription_request

        logger.debug("Requesting transcription from peer: %s (mime_type: %s, language: %s)", peer_id, mime_type, language)

        if peer_id not in self.p2p_manager.peers:
            raise ConnectionError(f"Peer {peer_id} is not connected")

        try:
            request_id = str(uuid.uuid4())
            response_future = asyncio.Future()
            self.service._pending_transcription_requests[request_id] = response_future

            request_message = create_remote_transcription_request(
                request_id=request_id, audio_base64=audio_base64,
                mime_type=mime_type, model=model, provider=provider,
                language=language, task=task
            )
            await self.p2p_manager.send_message_to_peer(peer_id, request_message)

            try:
                result = await asyncio.wait_for(response_future, timeout=timeout)
                logger.info("Received transcription result from %s: %d chars", peer_id, len(result.get("text", "")))
                return result
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for transcription from %s", peer_id)
                raise TimeoutError(f"Transcription request to {peer_id} timed out after {timeout}s")
            finally:
                self.service._pending_transcription_requests.pop(request_id, None)
        except Exception as e:
            logger.error("Error requesting transcription from %s: %s", peer_id, e, exc_info=True)
            raise

    async def aggregate_contexts(self, query: str, peer_ids: list = None) -> dict:
        """Aggregate contexts from local user and connected peers."""
        from dpc_protocol.pcm_core import PersonalContext

        contexts = {}
        contexts[self.p2p_manager.node_id] = self.p2p_manager.local_context

        if peer_ids is None:
            peer_ids = list(self.p2p_manager.peers.keys())

        if peer_ids:
            tasks = [self.service._request_context_from_peer(peer_id, query) for peer_id in peer_ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for peer_id, result in zip(peer_ids, results):
                if isinstance(result, PersonalContext):
                    contexts[peer_id] = result
                elif result is not None:
                    logger.error("Error getting context from %s: %s", peer_id, result)

        return contexts
