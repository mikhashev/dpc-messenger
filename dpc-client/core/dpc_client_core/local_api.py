# dpc-client/core/dpc_client_core/local_api.py

import asyncio
import json
import logging
import os
import secrets
import stat
import weakref
from pathlib import Path
from typing import TYPE_CHECKING
import websockets
from websockets.server import WebSocketServerProtocol

if TYPE_CHECKING:
    from .service import CoreService

logger = logging.getLogger(__name__)
ui_logger = logging.getLogger("dpc_ui")

# Local API authentication: a fresh random token is generated on every backend
# startup and written to ~/.dpc/.ws_token. The frontend reads this file via a
# Tauri command and presents the token as the first message on each WebSocket
# connection. Without this, any local process could connect to ws://127.0.0.1:9999
# and invoke backend commands.
WS_TOKEN_PATH = Path.home() / ".dpc" / ".ws_token"
AUTH_TIMEOUT_SECONDS = 5.0

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
    "list_all_tools",
    "get_cc_config",
    # AI queries
    "execute_ai_query",
    # Conversation history
    "get_conversation_history",
    "delete_conversation",
    "end_conversation_session",
    "get_conversation_settings",
    "set_conversation_persist_history",
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
    "set_group_agents",
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
    "update_group_topic",
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
    "get_agent_model_config",
    "save_agent_model_config",
    # Agent Task Board (v0.20.0)
    "get_agent_tasks",
    "get_agent_learning",
    "get_agent_task_result",
    "schedule_agent_task",
    "cancel_agent_task",
    "interrupt_agent",
    # Frontend logging relay
    "ui_log",
    # Sleep Consolidation (ADR-014)
    "toggle_sleep",
    "trigger_group_sleep",
    "activate_group_chat",
    # Reload from disk
    "reload_personal_context",
    "reload_firewall",
    # Web auth headless approval (ADR-029 Task 008)
    "web_auth_approve_headless",
    "web_auth_reject_headless",
    # Shell approval (ADR-030 v2)
    "shell_approve_command",
    "shell_reject_command",
    "shell_add_to_whitelist",
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
        # Per-client send lock — serializes concurrent client.send() calls
        # on the same WebSocket connection. Prevents WS frame interleaving
        # when broadcast_event / send_response_to_all / inline _handler sends
        # overlap (S145: agent_progress fire-and-forget create_task pattern
        # produced 558 corrupted frames in 50ms during browse_page start,
        # eating web_auth_popup_request → T10 modal never appeared).
        # WeakKeyDictionary so abnormal disconnects (where _unregister
        # doesn't run) don't leak Lock objects — entries vanish as soon as
        # the WebSocketServerProtocol is garbage-collected.
        self._client_locks: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        self._auth_token: str = ""

    def _generate_and_persist_auth_token(self) -> None:
        """Generate a fresh 256-bit auth token and write it to ~/.dpc/.ws_token.

        Called once at startup. The frontend reads this file via a Tauri
        command and sends the token as the first WebSocket message.
        """
        self._auth_token = secrets.token_urlsafe(32)
        try:
            WS_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            WS_TOKEN_PATH.write_text(self._auth_token, encoding="utf-8")
            # Restrict to user-only read/write. On Linux/macOS this prevents
            # other local users from reading the token. On Windows the mode
            # bits are largely advisory; NTFS ACLs inherited from the user's
            # home directory already restrict access to the same user.
            try:
                os.chmod(WS_TOKEN_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
            except OSError as chmod_err:
                logger.debug("chmod 0o600 on %s skipped: %s", WS_TOKEN_PATH, chmod_err)
            logger.info("Local API auth token written to %s", WS_TOKEN_PATH)
        except OSError as e:
            logger.error("Failed to write auth token to %s: %s", WS_TOKEN_PATH, e)
            raise

    async def _authenticate(self, websocket: WebSocketServerProtocol) -> bool:
        """Validate the first message on a new WebSocket connection.

        Expects {"command": "auth", "token": "<token>"} within AUTH_TIMEOUT_SECONDS.
        On success: sends an OK response and returns True.
        On failure: closes the connection with code 1008 (policy violation),
        logs the rejection with peer address, and returns False.
        """
        peer_addr = websocket.remote_address
        peer_addr_str = f"{peer_addr[0]}:{peer_addr[1]}" if peer_addr else "unknown"
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=AUTH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Local API auth timeout from %s", peer_addr_str)
            await websocket.close(code=1008, reason="auth timeout")
            return False
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Local API connection from %s closed before auth", peer_addr_str)
            return False

        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Local API auth: malformed first message from %s", peer_addr_str)
            await websocket.close(code=1008, reason="auth required")
            return False

        if message.get("command") != "auth" or not isinstance(message.get("token"), str):
            logger.warning("Local API auth: missing auth command from %s", peer_addr_str)
            await websocket.close(code=1008, reason="auth required")
            return False

        # Constant-time comparison to avoid timing attacks
        if not secrets.compare_digest(message["token"], self._auth_token):
            logger.warning("Local API auth: invalid token from %s", peer_addr_str)
            await websocket.close(code=1008, reason="invalid token")
            return False

        await websocket.send(json.dumps({
            "id": message.get("id"),
            "command": "auth",
            "status": "OK",
            "payload": {},
        }))
        logger.info("Local API client authenticated from %s", peer_addr_str)
        return True

    async def _register(self, websocket: WebSocketServerProtocol):
        self._clients.add(websocket)
        self._client_locks[websocket] = asyncio.Lock()
        logger.info("UI client connected: %s", websocket.remote_address)

    async def _unregister(self, websocket: WebSocketServerProtocol):
        self._clients.remove(websocket)
        self._client_locks.pop(websocket, None)
        logger.info("UI client disconnected: %s", websocket.remote_address)

    async def _send_locked(self, client: WebSocketServerProtocol, message: str) -> None:
        """Send a message to a registered client under its per-client lock.

        Acquiring the lock before send() serializes concurrent send paths
        (multiple broadcast_event coroutines, inline command responses)
        so two frames never interleave their bytes on the wire.
        """
        lock = self._client_locks.get(client)
        if lock is None:
            await client.send(message)
            return
        try:
            async with lock:
                await client.send(message)
        except RuntimeError as e:
            if "bound to a different event loop" in str(e):
                await client.send(message)
            else:
                raise

    async def _handler(self, websocket: WebSocketServerProtocol):
        if not await self._authenticate(websocket):
            return
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
                        await self._send_locked(websocket, json.dumps({"id": command_id, "command": command, "status": "OK", "payload": {}}))
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
                            await self._send_locked(websocket, json.dumps(response))
                            # Mike S141: pair every "Executing command" with a
                            # "Response sent" so the request → response chain
                            # is greppable. Past Bug A class (sendCommand
                            # fire-and-forget instead of Promise) hid silent
                            # backend success behind UI error toasts; with
                            # this pair anyone diagnosing "command sent but
                            # UI shows error" can confirm in one grep whether
                            # backend actually answered.
                            logger.debug("Response sent for '%s' (id=%s)", command, command_id)

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
                        await self._send_locked(websocket, json.dumps(error_response))
                    except:
                        pass
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Connection closed during message processing")
        except Exception as e:
            logger.error("Handler error: %s", e, exc_info=True)
        finally:
            await self._unregister(websocket)

    async def start(self):
        self._generate_and_persist_auth_token()
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
        # Send to all clients under per-client locks, but don't fail if one
        # client is disconnected. Locking is required because fire-and-forget
        # broadcast callers (e.g. agent_manager._emit_progress) create
        # concurrent broadcast_event coroutines that would otherwise interleave
        # WS frame bytes on the same client connection.
        results = await asyncio.gather(
            *(self._send_locked(client, message) for client in self._clients),
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
        # Per-client locks (see broadcast_event for rationale).
        results = await asyncio.gather(
            *(self._send_locked(client, message) for client in self._clients),
            return_exceptions=True
        )
        # Log any errors but don't crash
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning("Failed to send response to client: %s", result)