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
ui_logger = logging.getLogger("dpc_ui")

_UI_LOG_LEVELS = {
    "debug": ui_logger.debug,
    "info":  ui_logger.info,
    "warn":  ui_logger.warning,
    "error": ui_logger.error,
}

# Explicit allowlist of commands the UI is permitted to invoke.
# Any command not listed here is rejected, regardless of whether a method
# with that name exists on CoreService.
ALLOWED_COMMANDS: frozenset = frozenset({
    # Status & connection
    "get_status",
    "connect_to_peer",
    "connect_via_dht",
    "disconnect_from_peer",
    # Hub
    "login_to_hub",
    "disconnect_from_hub",
    # Providers
    "list_providers",
    "get_providers_list",
    "get_default_providers",
    "get_providers_config",
    "save_providers_config",
    "query_ollama_model_info",
    "query_remote_providers",
    # Personal context & instructions
    "get_personal_context",
    "save_personal_context",
    "get_instructions",
    "save_instructions",
    "create_instruction_set",
    "delete_instruction_set",
    "set_default_instruction_set",
    "reload_instructions",
    "get_available_templates",
    "import_instruction_template",
    "get_wizard_template",
    "ai_assisted_instruction_creation",
    "ai_assisted_instruction_creation_remote",
    # Firewall
    "get_firewall_rules",
    "save_firewall_rules",
    # AI queries
    "execute_ai_query",
    # Conversation history
    "get_conversation_history",
    "delete_conversation",
    "end_conversation_session",
    "get_conversation_settings",
    "set_conversation_persist_history",
    "toggle_auto_knowledge_detection",
    # File transfer
    "send_file",
    "accept_file_transfer",
    "cancel_file_transfer",
    # Images
    "send_image",
    "send_p2p_image",
    # P2P messaging
    "send_p2p_message",
    # Voice & transcription
    "send_voice_message",
    "preload_whisper_model",
    "download_whisper_model",
    "get_voice_transcription_config",
    "save_voice_transcription_config",
    "set_conversation_transcription",
    "get_conversation_transcription",
    "transcribe_audio",
    # Session management
    "propose_new_session",
    "vote_new_session",
    # Group chat
    "create_group_chat",
    "send_group_message",
    "send_group_agent_message",
    "send_cc_agent_response",
    "send_group_image",
    "send_group_voice_message",
    "send_group_file",
    "add_group_member",
    "remove_group_member",
    "leave_group",
    "delete_group",
    "get_groups",
    # Knowledge
    "vote_knowledge_commit",
    # Telegram
    "get_telegram_status",
    "send_to_telegram",
    "link_telegram_chat",
    "delete_telegram_conversation_link",
    "link_agent_telegram",
    "unlink_agent_telegram",
    # Session archive
    "get_session_archive_info",
    "clear_session_archives",
    # Agents
    "create_agent",
    "list_agents",
    "get_agent_config",
    "update_agent_config",
    "delete_agent",
    "list_agent_profiles",
    "get_agent_permissions",  # Agent permissions transparency (v0.22.0)
    # Agent Task Board (v0.20.0)
    "get_agent_tasks",
    "get_agent_learning",
    "get_agent_task_result",
    "schedule_agent_task",
    "cancel_agent_task",
    # Frontend logging relay
    "ui_log",
})


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

    # Truncate any *audio_base64 or *voice*base64 field if present
    for key in list(sanitized.keys()):
        if 'base64' in key and isinstance(sanitized[key], str) and len(sanitized[key]) > max_length:
            original = sanitized[key]
            sanitized[key] = f"{original[:max_length]}... ({len(original)} chars)"

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

                    if command not in ALLOWED_COMMANDS:
                        logger.warning("Rejected disallowed command: '%s'", command)
                        raise ValueError(f"Unknown or non-async command: {command}")

                    # ui_log is handled directly here — never dispatched to CoreService
                    if command == "ui_log":
                        level = payload.get("level", "info").lower()
                        context = payload.get("context", "ui")
                        msg = payload.get("message", "")
                        log_fn = _UI_LOG_LEVELS.get(level, ui_logger.info)
                        # Sanitize surrogates — emojis from JS UTF-16 can produce lone surrogates
                        safe_msg = msg.encode("utf-8", errors="replace").decode("utf-8")
                        log_fn("[%s] %s", context, safe_msg)
                        await websocket.send(json.dumps({"id": command_id, "command": command, "status": "OK", "payload": {}}))
                        continue

                    handler_method = getattr(self.core_service, command, None)

                    if handler_method and asyncio.iscoroutinefunction(handler_method):
                        # Sanitize payload for logging (truncate large base64 strings)
                        sanitized_payload = _sanitize_payload_for_logging(payload)
                        # Special logging for image/voice commands
                        if command in ["send_image", "send_p2p_image", "send_voice_message", "transcribe_audio",
                                       "send_group_voice_message", "send_group_image"]:
                            logger.debug("Executing command '%s' (payload contains media data)", command)
                        else:
                            logger.debug("Executing command '%s' with payload: %s", command, sanitized_payload)

                        # For long-running tasks, run them in the background
                        # get_status can take 30s on Linux (IP detection), execute_ai_query for AI
                        if command in ["execute_ai_query", "get_status"]:
                            # Pass the command_id to the handler
                            payload['command_id'] = command_id
                            payload['_websocket'] = websocket
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