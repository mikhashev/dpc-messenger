"""
Telegram Bot Manager - Telegram Integration for DPC Messenger (v0.14.0+)

This module implements Telegram bot integration with the following features:
- Voice message transcription using local Whisper model
- Two-way messaging bridge (Telegram <-> DPC)
- Private/whitelist-only access control
- Webhook and polling mode support

Architecture:
- TelegramBotManager: Bot lifecycle, whitelist, message sending
- TelegramBridge: Message translation and routing (in telegram_coordinator.py)
- TelegramIncomingHandler: Routes Telegram messages to DPC conversations

Use Cases:
- Forward voice messages from Telegram for transcription
- Bridge Telegram chats with DPC P2P conversations
- Send/receive messages bidirectionally

Privacy:
- Whitelist enforcement: only authorized chat_ids can interact
- Voice files stored locally (same as P2P voice messages)
- Transcriptions follow same privacy model as DPC messages

Setup:
1. Create bot via @BotFather
2. Get bot token
3. Get chat_id via @userinfobot
4. Configure in ~/.dpc/config.ini [telegram] section
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Set, List, TYPE_CHECKING
from datetime import datetime, timezone

if TYPE_CHECKING:
    from ..service import CoreService

logger = logging.getLogger(__name__)


class TelegramBotManager:
    """
    Manages Telegram bot lifecycle, whitelist enforcement, and message sending.

    This manager handles the low-level Telegram Bot API interactions:
    - Bot initialization and lifecycle (start/stop)
    - Webhook or long polling mode operation
    - Whitelist access control (only authorized chat_ids)
    - Sending messages, voice, and files to Telegram
    - Error handling and rate limiting

    Attributes:
        service: CoreService instance for access to other components
        bot_token: Telegram bot token from @BotFather
        allowed_chat_ids: Set of whitelisted chat IDs (private access)
        use_webhook: Whether to use webhook mode (vs polling)
        webhook_url: Public URL for webhook (production)
        webhook_port: Local port for webhook server
        transcription_enabled: Whether to auto-transcribe voice messages
        bridge_to_p2p: Whether to forward Telegram messages to P2P peers

    Example:
        >>> manager = TelegramBotManager(service, config)
        >>> await manager.start()
        >>> await manager.send_message(123456789, "Hello from DPC!")
        >>> await manager.stop()
    """

    def __init__(self, service: "CoreService", config: Dict[str, Any]):
        """
        Initialize Telegram Bot Manager.

        Args:
            service: CoreService instance
            config: Telegram configuration dict with keys:
                - bot_token: Bot token from @BotFather
                - allowed_chat_ids: List of whitelisted chat IDs
                - use_webhook: Boolean for webhook mode
                - webhook_url: Public URL for webhooks
                - webhook_port: Local port for webhook server
                - transcription_enabled: Enable voice transcription
                - bridge_to_p2p: Forward to P2P peers
        """
        self.service = service
        self.bot_token = config.get("bot_token", "")
        self.allowed_chat_ids: Set[str] = set(config.get("allowed_chat_ids", []))
        self.use_webhook = config.get("use_webhook", False)
        self.webhook_url = config.get("webhook_url", "")
        self.webhook_port = config.get("webhook_port", 8443)
        self.transcription_enabled = config.get("transcription_enabled", True)
        self.bridge_to_p2p = config.get("bridge_to_p2p", False)

        # Bot instance (lazy loaded)
        self.application = None
        self.bot_instance = None

        # State tracking
        self._running = False
        self._start_lock = asyncio.Lock()

        # Message queue for outgoing messages (rate limiting)
        self._outgoing_queue: asyncio.Queue = asyncio.Queue()
        self._sender_task: Optional[asyncio.Task] = None

        logger.info(
            f"TelegramBotManager initialized (webhook={self.use_webhook}, "
            f"whitelist={len(self.allowed_chat_ids)} chat_ids, "
            f"transcription={self.transcription_enabled})"
        )

    def is_allowed(self, chat_id: Any) -> bool:
        """
        Check if a chat_id is whitelisted for access.

        Args:
            chat_id: Telegram chat ID (int or str)

        Returns:
            True if chat_id is in whitelist, False otherwise
        """
        # Convert to string for comparison (Telegram IDs can be int or str)
        chat_id_str = str(chat_id)
        return chat_id_str in self.allowed_chat_ids

    async def start(self):
        """
        Start the Telegram bot.

        Initializes the bot application and starts either:
        - Webhook mode: Server listening on webhook_port
        - Polling mode: Long polling get_updates loop

        Raises:
            RuntimeError: If bot is already running
            Exception: If bot token is invalid or initialization fails
        """
        async with self._start_lock:
            if self._running:
                logger.warning("Telegram bot is already running")
                return

            if not self.bot_token:
                logger.error("Cannot start Telegram bot: no bot_token configured")
                return

            try:
                # Import telegram library (lazy import for optional dependency)
                from telegram import Update
                from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

                # Build bot application
                self.application = (
                    Application.builder()
                    .token(self.bot_token)
                    .build()
                )

                # Register handlers (will be processed by TelegramBridge)
                # We use a catch-all message handler that forwards to the bridge
                from ..coordinators.telegram_coordinator import TelegramBridge
                if not hasattr(self.service, 'telegram_bridge') or self.service.telegram_bridge is None:
                    logger.error("TelegramBridge not initialized, cannot register handlers")
                    return

                bridge = self.service.telegram_bridge

                # Text messages handler
                text_handler = MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    bridge.handle_text_message
                )
                self.application.add_handler(text_handler)

                # Voice messages handler
                voice_handler = MessageHandler(
                    filters.VOICE,
                    bridge.handle_voice_message
                )
                self.application.add_handler(voice_handler)

                # Photo messages handler
                photo_handler = MessageHandler(
                    filters.PHOTO,
                    bridge.handle_photo_message
                )
                self.application.add_handler(photo_handler)

                # Document messages handler
                document_handler = MessageHandler(
                    filters.Document.ALL,
                    bridge.handle_document_message
                )
                self.application.add_handler(document_handler)

                # Start the bot
                if self.use_webhook:
                    # Webhook mode (production)
                    if not self.webhook_url:
                        logger.error("Webhook mode enabled but webhook_url not configured")
                        return

                    await self.application.start()
                    await self.application.bot.set_webhook(url=self.webhook_url)
                    await self.application.updater.start_webhook(
                        listen="0.0.0.0",
                        port=self.webhook_port,
                        webhook_url=self.webhook_url
                    )
                    logger.info(f"Telegram bot started (webhook mode on port {self.webhook_port})")
                else:
                    # Polling mode (development)
                    await self.application.initialize()
                    await self.application.start()
                    await self.application.updater.start_polling(drop_pending_updates=True)
                    logger.info("Telegram bot started (polling mode)")

                # Start outgoing message sender task
                self._sender_task = asyncio.create_task(self._message_sender_loop())
                self._sender_task.set_name("telegram_sender")

                self._running = True
                logger.info("Telegram bot started successfully")

                # Broadcast connection event to UI
                await self.service.local_api.broadcast_event("telegram_connected", {
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })

            except ImportError as e:
                logger.error(f"Failed to import telegram library: {e}")
                logger.error("Install with: poetry install")
                raise
            except Exception as e:
                logger.error(f"Failed to start Telegram bot: {e}", exc_info=True)
                raise

    async def stop(self):
        """
        Stop the Telegram bot gracefully.

        Shuts down the bot application and stops all background tasks.
        """
        async with self._start_lock:
            if not self._running:
                logger.warning("Telegram bot is not running")
                return

            logger.info("Stopping Telegram bot...")

            # Stop sender task
            if self._sender_task and not self._sender_task.done():
                self._sender_task.cancel()
                try:
                    await self._sender_task
                except asyncio.CancelledError:
                    pass

            # Stop bot application
            if self.application:
                try:
                    await self.application.updater.stop()
                    await self.application.stop()
                    await self.application.shutdown()
                except Exception as e:
                    logger.error(f"Error stopping bot application: {e}")

            self._running = False
            logger.info("Telegram bot stopped")

            # Broadcast disconnection event to UI
            await self.service.local_api.broadcast_event("telegram_disconnected", {
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

    async def send_message(
        self,
        chat_id: Any,
        text: str,
        parse_mode: str = "HTML",
        disable_preview: bool = False
    ) -> bool:
        """
        Send a text message to a Telegram chat.

        Args:
            chat_id: Target chat ID
            text: Message text
            parse_mode: Parse mode (HTML, Markdown, None)
            disable_preview: Disable link preview

        Returns:
            True if message sent successfully, False otherwise
        """
        if not self._running:
            logger.warning("Cannot send message: bot is not running")
            return False

        if not self.is_allowed(chat_id):
            logger.warning(f"Cannot send to chat_id {chat_id}: not in whitelist")
            return False

        try:
            # Queue message for sending (handles rate limiting)
            await self._outgoing_queue.put({
                "type": "message",
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_preview
            })
            return True
        except Exception as e:
            logger.error(f"Failed to queue message: {e}", exc_info=True)
            return False

    async def send_voice(
        self,
        chat_id: Any,
        file_path: Path,
        caption: Optional[str] = None,
        duration: Optional[int] = None
    ) -> bool:
        """
        Send a voice message to a Telegram chat.

        Args:
            chat_id: Target chat ID
            file_path: Path to voice file
            caption: Optional caption
            duration: Optional duration in seconds

        Returns:
            True if voice sent successfully, False otherwise
        """
        if not self._running:
            logger.warning("Cannot send voice: bot is not running")
            return False

        if not self.is_allowed(chat_id):
            logger.warning(f"Cannot send to chat_id {chat_id}: not in whitelist")
            return False

        if not file_path.exists():
            logger.error(f"Voice file not found: {file_path}")
            return False

        try:
            # Queue voice for sending
            await self._outgoing_queue.put({
                "type": "voice",
                "chat_id": chat_id,
                "file_path": file_path,
                "caption": caption,
                "duration": duration
            })
            return True
        except Exception as e:
            logger.error(f"Failed to queue voice: {e}", exc_info=True)
            return False

    async def send_photo(
        self,
        chat_id: Any,
        file_path: Path,
        caption: Optional[str] = None
    ) -> bool:
        """
        Send a photo to a Telegram chat.

        Args:
            chat_id: Target chat ID
            file_path: Path to photo file
            caption: Optional caption

        Returns:
            True if photo sent successfully, False otherwise
        """
        if not self._running:
            logger.warning("Cannot send photo: bot is not running")
            return False

        if not self.is_allowed(chat_id):
            logger.warning(f"Cannot send to chat_id {chat_id}: not in whitelist")
            return False

        if not file_path.exists():
            logger.error(f"Photo file not found: {file_path}")
            return False

        try:
            await self._outgoing_queue.put({
                "type": "photo",
                "chat_id": chat_id,
                "file_path": file_path,
                "caption": caption
            })
            return True
        except Exception as e:
            logger.error(f"Failed to queue photo: {e}", exc_info=True)
            return False

    async def send_document(
        self,
        chat_id: Any,
        file_path: Path,
        caption: Optional[str] = None
    ) -> bool:
        """
        Send a document/file to a Telegram chat.

        Args:
            chat_id: Target chat ID
            file_path: Path to document file
            caption: Optional caption

        Returns:
            True if document sent successfully, False otherwise
        """
        if not self._running:
            logger.warning("Cannot send document: bot is not running")
            return False

        if not self.is_allowed(chat_id):
            logger.warning(f"Cannot send to chat_id {chat_id}: not in whitelist")
            return False

        if not file_path.exists():
            logger.error(f"Document file not found: {file_path}")
            return False

        try:
            await self._outgoing_queue.put({
                "type": "document",
                "chat_id": chat_id,
                "file_path": file_path,
                "caption": caption
            })
            return True
        except Exception as e:
            logger.error(f"Failed to queue document: {e}", exc_info=True)
            return False

    async def _message_sender_loop(self):
        """
        Background task that sends queued messages to Telegram.

        Handles rate limiting by processing messages sequentially.
        Telegram rate limit: 30 messages/second for bots.
        """
        logger.info("Telegram message sender loop started")

        while self._running:
            try:
                # Get message from queue with timeout
                msg = await asyncio.wait_for(
                    self._outgoing_queue.get(),
                    timeout=1.0
                )

                # Send based on message type
                if msg["type"] == "message":
                    await self._send_text_message(msg)
                elif msg["type"] == "voice":
                    await self._send_voice_message(msg)
                elif msg["type"] == "photo":
                    await self._send_photo_message(msg)
                elif msg["type"] == "document":
                    await self._send_document_message(msg)
                else:
                    logger.warning(f"Unknown message type: {msg['type']}")

                # Small delay to respect rate limits
                await asyncio.sleep(0.05)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in message sender loop: {e}", exc_info=True)
                await asyncio.sleep(1)  # Back off on error

        logger.info("Telegram message sender loop stopped")

    async def _send_text_message(self, msg: Dict[str, Any]):
        """Send a text message via Telegram Bot API."""
        try:
            from telegram import Bot

            bot: Bot = self.application.bot
            await bot.send_message(
                chat_id=msg["chat_id"],
                text=msg["text"],
                parse_mode=msg.get("parse_mode"),
                disable_web_page_preview=msg.get("disable_web_page_preview", False)
            )
            logger.debug(f"Sent text message to chat {msg['chat_id']}")
        except Exception as e:
            logger.error(f"Failed to send text message: {e}", exc_info=True)

    async def _send_voice_message(self, msg: Dict[str, Any]):
        """Send a voice message via Telegram Bot API."""
        try:
            from telegram import Bot

            bot: Bot = self.application.bot
            with open(msg["file_path"], "rb") as f:
                await bot.send_voice(
                    chat_id=msg["chat_id"],
                    voice=f,
                    caption=msg.get("caption"),
                    duration=msg.get("duration")
                )
            logger.debug(f"Sent voice message to chat {msg['chat_id']}")
        except Exception as e:
            logger.error(f"Failed to send voice message: {e}", exc_info=True)

    async def _send_photo_message(self, msg: Dict[str, Any]):
        """Send a photo message via Telegram Bot API."""
        try:
            from telegram import Bot

            bot: Bot = self.application.bot
            with open(msg["file_path"], "rb") as f:
                await bot.send_photo(
                    chat_id=msg["chat_id"],
                    photo=f,
                    caption=msg.get("caption")
                )
            logger.debug(f"Sent photo to chat {msg['chat_id']}")
        except Exception as e:
            logger.error(f"Failed to send photo: {e}", exc_info=True)

    async def _send_document_message(self, msg: Dict[str, Any]):
        """Send a document message via Telegram Bot API."""
        try:
            from telegram import Bot

            bot: Bot = self.application.bot
            with open(msg["file_path"], "rb") as f:
                await bot.send_document(
                    chat_id=msg["chat_id"],
                    document=f,
                    caption=msg.get("caption")
                )
            logger.debug(f"Sent document to chat {msg['chat_id']}")
        except Exception as e:
            logger.error(f"Failed to send document: {e}", exc_info=True)
