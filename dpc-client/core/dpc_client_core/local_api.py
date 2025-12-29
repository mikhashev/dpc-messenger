# dpc-client/core/dpc_client_core/local_api.py

import asyncio
import json
import logging
from typing import TYPE_CHECKING
import websockets
from websockets.server import WebSocketServerProtocol

if TYPE_CHECKING:
    from .service import CoreService

logger = logging.getLogger(__name__)


def _sanitize_payload_for_logging(payload: dict, max_length: int = 30) -> dict:
    """
    Sanitize payload for logging by truncating large base64 strings.

    Args:
        payload: The payload dict to sanitize
        max_length: Maximum length for base64 strings (default: 30 characters)

    Returns:
        Sanitized copy of payload with truncated base64 strings
    """
    sanitized = payload.copy()

    # Truncate image_base64 field if present
    if 'image_base64' in sanitized and isinstance(sanitized['image_base64'], str):
        original = sanitized['image_base64']
        if len(original) > max_length:
            sanitized['image_base64'] = f"{original[:max_length]}... ({len(original)} chars)"

    return sanitized


class LocalApiServer:
    def __init__(self, core_service: "CoreService", host: str = "127.0.0.1", port: int = 9999):
        self.core_service = core_service
        self.host = host
        self.port = port
        self.server = None
        self._clients = set()

    async def _register(self, websocket: WebSocketServerProtocol):
        self._clients.add(websocket)
        logger.info("UI client connected: %s", websocket.remote_address)

    async def _unregister(self, websocket: WebSocketServerProtocol):
        self._clients.remove(websocket)
        logger.info("UI client disconnected: %s", websocket.remote_address)

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
                        # Sanitize payload for logging (truncate large base64 strings)
                        sanitized_payload = _sanitize_payload_for_logging(payload)
                        # Special logging for image commands
                        if command in ["send_image", "send_p2p_image"]:
                            logger.debug("Executing command '%s' (payload contains image data)", command)
                        else:
                            logger.debug("Executing command '%s' with payload: %s", command, sanitized_payload)

                        # For long-running tasks like AI queries, run them in the background
                        if command == "execute_ai_query":
                            # Pass the command_id to the handler
                            payload['command_id'] = command_id
                            asyncio.create_task(handler_method(**payload))
                        else:
                            # For short tasks, await the result and send it back
                            result = await handler_method(**payload)
                            response = {"id": command_id, "command": command, "status": "OK", "payload": result}
                            await websocket.send(json.dumps(response))

                    else:
                        raise ValueError(f"Unknown or non-async command: {command}")

                except websockets.exceptions.ConnectionClosed:
                    logger.debug("UI client connection closed normally")
                    break
                except Exception as e:
                    logger.error("Error processing command: %s", e, exc_info=True)
                    error_response = {
                        "id": command_id if 'command_id' in locals() else None,
                        "command": command if 'command' in locals() else 'unknown',
                        "status": "ERROR",
                        "payload": {"message": str(e)}
                    }
                    try:
                        await websocket.send(json.dumps(error_response))
                    except:
                        pass
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Connection closed during message processing")
        except Exception as e:
            logger.error("Handler error: %s", e, exc_info=True)
        finally:
            await self._unregister(websocket)

    async def start(self):
        logger.info("Starting Local API Server on ws://%s:%d", self.host, self.port)

        # Configure WebSocket server with more lenient timeouts
        self.server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
            ping_interval=20,      # Send ping every 20 seconds (default is 20)
            ping_timeout=60,       # Wait 60 seconds for pong (default is 20) - INCREASED
            close_timeout=10,      # Wait 10 seconds when closing (default is 10)
            max_size=10 * 1024 * 1024,  # Max message size: 10MB
        )
        logger.info("Local API Server is running")

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("Local API Server stopped")

    async def broadcast_event(self, event_name: str, payload: dict):
        if not self._clients: 
            return
        message = json.dumps({"event": event_name, "payload": payload})
        # Send to all clients, but don't fail if one client is disconnected
        results = await asyncio.gather(
            *(client.send(message) for client in self._clients),
            return_exceptions=True
        )
        # Log any errors but don't crash
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning("Failed to send broadcast to client: %s", result)

    async def send_response_to_all(self, command_id: str, command: str, status: str, payload: dict):
        """Helper to send a response to all connected UI clients."""
        if not self._clients:
            return
        response = {"id": command_id, "command": command, "status": status, "payload": payload}
        message = json.dumps(response)
        # Send to all clients, but don't fail if one client is disconnected
        results = await asyncio.gather(
            *(client.send(message) for client in self._clients),
            return_exceptions=True
        )
        # Log any errors but don't crash
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning("Failed to send response to client: %s", result)