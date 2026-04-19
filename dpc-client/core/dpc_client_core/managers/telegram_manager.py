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


class _TelegramNetworkErrorFilter(logging.Filter):
    """
    Suppress repetitive Telegram NetworkError log spam.

    The telegram library's network_retry_loop retries polling indefinitely,
    logging an ERROR-level traceback for every failure. When Telegram is
    unavailable this produces hundreds of error lines per minute.

    This filter passes the first `threshold` occurrences, then suppresses
    subsequent messages and emits a summary every `report_every` occurrences.
    """

    def __init__(self, threshold: int = 3, report_every: int = 50):
        super().__init__()
        self._threshold = threshold
        self._report_every = report_every
        self._count = 0

    def reset(self) -> None:
        self._count = 0

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        is_network_noise = (
            'Exception happened while polling' in msg
            or 'Network Retry Loop' in msg
            or 'NetworkError' in msg
            or 'httpx.ReadError' in msg
            or 'httpcore.ReadError' in msg
        )
        if not is_network_noise:
            self._count = 0
            return True

        self._count += 1
        if self._count <= self._threshold:
            return True
        if self._count % self._report_every == 0:
            # Emit a single summary at WARNING so it's visible but not noisy
            logger.warning(
                "Telegram polling: %d consecutive network errors "
                "(Telegram may be unreachable). Continuing to retry silently.",
                self._count,
            )
        return False

# Telegram Bot API limits
TELEGRAM_MESSAGE_MAX_LENGTH = 4096  # Text messages
TELEGRAM_CAPTION_MAX_LENGTH = 1024  # Caption for media (voice, photo, document)


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
            NOTE (v0.15.0): Currently forwards as N separate 1:1 messages.
            Future enhancement will support group chat bridging (single message
            to DPC group with proper attribution). See telegram_coordinator.py
            _forward_to_p2p_peers() for implementation details and FIXME comments.

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

        # Historical message fetching settings (v0.15.0+)
        self.fetch_history_on_startup = config.get("fetch_history_on_startup", True)
        self.history_fetch_limit = config.get("history_fetch_limit", 100)
        self.history_max_age_hours = config.get("history_max_age_hours", 24)
        self.history_message_types = config.get("history_message_types",
            ["text", "voice", "photo", "document", "video"])
        self.drop_pending_updates_config = config.get("drop_pending_updates", False)

        # Bot instance (lazy loaded)
        self.application = None
        self.bot_instance = None

        # State tracking
        self._running = False
        self._start_lock = asyncio.Lock()

        # Message queue for outgoing messages (rate limiting)
        self._outgoing_queue: asyncio.Queue = asyncio.Queue()
        self._sender_task: Optional[asyncio.Task] = None

        # Network error log filter (installed on polling start, removed on stop)
        self._network_filter = _TelegramNetworkErrorFilter(threshold=3, report_every=50)
        self._filtered_loggers: list[logging.Logger] = []

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
                from telegram.error import InvalidToken, NetworkError, Conflict

                # Suppress verbose stacktraces for transient PTB network errors (LOW-1).
                from ._telegram_logging import install_telegram_log_filter
                install_telegram_log_filter()

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

                # Video messages handler
                video_handler = MessageHandler(
                    filters.VIDEO,
                    bridge.handle_video_message
                )
                self.application.add_handler(video_handler)

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
                    # Polling mode (development) — retry on network errors
                    _max_retries = 3
                    _base_delay = 5
                    for _attempt in range(_max_retries):
                        try:
                            await self.application.initialize()
                            await self.application.start()
                            break
                        except NetworkError as e:
                            if _attempt < _max_retries - 1:
                                _delay = _base_delay * (2 ** _attempt)
                                logger.warning(
                                    "Telegram startup attempt %d/%d failed (%s), "
                                    "retrying in %ds...",
                                    _attempt + 1, _max_retries, e, _delay,
                                )
                                await asyncio.sleep(_delay)
                                self.application = (
                                    Application.builder()
                                    .token(self.bot_token)
                                    .build()
                                )
                            else:
                                raise

                    # Stop bot immediately when a Conflict error is detected (another instance running)
                    async def _conflict_error_handler(update, context):
                        if isinstance(context.error, Conflict):
                            logger.error(
                                "Telegram bot conflict: another instance is already running. "
                                "Stop the other DPC Messenger process and restart."
                            )
                            await self._broadcast_error_event(
                                "Bot Already Running",
                                "Another DPC Messenger instance is already using this Telegram bot. "
                                "Stop the other instance first."
                            )
                            # Stop polling to avoid infinite conflict loop
                            asyncio.create_task(self.stop())
                            return
                        raise context.error

                    self.application.add_error_handler(_conflict_error_handler)

                    # Determine if we should drop pending updates
                    # Only drop if history fetching is disabled (to avoid duplicates)
                    # If history fetching is enabled, we'll fetch them manually first
                    drop_updates = not self.fetch_history_on_startup and self.drop_pending_updates_config

                    if self.fetch_history_on_startup:
                        try:
                            logger.info("Fetching historical Telegram messages...")
                            await self._fetch_historical_messages()
                            logger.info("Historical message fetch complete")
                        except Exception as e:
                            logger.error(f"Failed to fetch historical messages: {e}", exc_info=True)
                            drop_updates = True  # Fallback to prevent duplicates
                            logger.warning("Falling back to drop_pending_updates=True to prevent duplicates")

                    # Install network error filter to suppress log spam when
                    # Telegram is unreachable (library retries indefinitely).
                    self._network_filter.reset()
                    self._filtered_loggers = [
                        logging.getLogger('telegram.ext._updater'),
                        logging.getLogger('telegram.ext._utils.networkloop'),
                        logging.getLogger('httpcore'),
                        logging.getLogger('httpx'),
                    ]
                    for _lg in self._filtered_loggers:
                        _lg.addFilter(self._network_filter)

                    await self.application.updater.start_polling(drop_pending_updates=drop_updates)
                    logger.info(f"Telegram bot started (polling mode, drop_updates={drop_updates})")

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
                error_msg = "Telegram library not installed. Run: poetry install"
                await self._broadcast_error_event("Library Missing", error_msg)
                raise

            except InvalidToken as e:
                logger.error(f"Invalid Telegram bot token: {e}", exc_info=True)
                error_msg = (
                    "Your Telegram bot token is invalid or expired. "
                    "Please regenerate it in BotFather and update config.ini."
                )
                await self._broadcast_error_event("Invalid Bot Token", error_msg)
                # Don't re-raise - allow service to continue without Telegram

            except NetworkError as e:
                logger.error(f"Telegram network error: {e}", exc_info=True)
                error_msg = "Cannot connect to Telegram servers. Check your internet connection."
                await self._broadcast_error_event("Network Error", error_msg)
                # Don't re-raise - allow service to continue without Telegram

            except Exception as e:
                logger.error(f"Failed to start Telegram bot: {e}", exc_info=True)
                error_msg = f"Telegram bot error: {str(e)}"
                await self._broadcast_error_event("Telegram Bot Error", error_msg)
                # Don't re-raise - allow service to continue without Telegram

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

            # Remove network error filter from all loggers
            for _lg in self._filtered_loggers:
                _lg.removeFilter(self._network_filter)
            self._filtered_loggers.clear()

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

    async def _broadcast_error_event(self, title: str, message: str):
        """Broadcast a Telegram error event to the UI.

        Args:
            title: Error title (e.g., "Invalid Bot Token")
            message: User-friendly error message
        """
        if self.service and self.service.local_api:
            await self.service.local_api.broadcast_event("telegram_error", {
                "title": title,
                "message": message,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

    async def send_message(
        self,
        chat_id: Any,
        text: str,
        parse_mode: str = "HTML",
        disable_preview: bool = False,
        skip_whitelist_check: bool = False
    ) -> bool:
        """
        Send a text message to a Telegram chat.

        Args:
            chat_id: Target chat ID
            text: Message text
            parse_mode: Parse mode (HTML, Markdown, None)
            disable_preview: Disable link preview
            skip_whitelist_check: If True, bypass whitelist check (for system messages)

        Returns:
            True if message sent successfully, False otherwise
        """
        if not self._running:
            logger.warning("Cannot send message: bot is not running")
            return False

        if not skip_whitelist_check and not self.is_allowed(chat_id):
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

    def _split_long_message(self, text: str, max_length: int = TELEGRAM_MESSAGE_MAX_LENGTH) -> List[str]:
        """
        Split a long message into chunks respecting word boundaries and HTML tags.

        Tries to split at sentence boundaries (periods, newlines) first,
        then falls back to word boundaries (spaces). Handles HTML tag
        preservation to avoid breaking HTML formatting across chunks.

        Args:
            text: The message text to split
            max_length: Maximum length per chunk (default: 4096)

        Returns:
            List of message chunks, each under max_length characters
        """
        if len(text) <= max_length:
            return [text]

        chunks = []
        remaining_text = text

        # Known HTML tags that need to be preserved
        # Format: (open_tag, close_tag)
        html_tags = [
            ('<b>', '</b>'),
            ('<i>', '</i>'),
            ('<u>', '</u>'),
            ('<s>', '</s>'),
            ('<code>', '</code>'),
            ('<pre>', '</pre>'),
            ('<a href=', '</a>'),  # Special handling needed
        ]

        while remaining_text:
            # Reserve space for part indicator (e.g., "\n\n(1/3)")
            # We'll add this later, so reserve ~20 chars
            chunk_size = max_length - 20

            if len(remaining_text) <= chunk_size:
                chunks.append(remaining_text)
                break

            # Try to split at sentence boundary (period + space, or newline)
            chunk_end = chunk_size
            split_points = ['.\n', '. ', '\n\n', '\n']

            for split_point in split_points:
                # Look for split point before the chunk end
                pos = remaining_text.rfind(split_point, 0, chunk_size)
                if pos > chunk_size // 2:  # Ensure we don't split too early
                    chunk_end = pos + len(split_point)
                    break

            # If no sentence boundary found, try word boundary (space)
            if chunk_end == chunk_size:
                pos = remaining_text.rfind(' ', 0, chunk_size)
                if pos > chunk_size // 2:
                    chunk_end = pos + 1

            # Still no good split point, force split at max length
            if chunk_end == chunk_size:
                chunk_end = chunk_size

            chunk = remaining_text[:chunk_end]
            remaining_text = remaining_text[chunk_end:]

            # Check for unclosed HTML tags in this chunk
            for open_tag, close_tag in html_tags:
                open_count = chunk.count(open_tag)
                close_count = chunk.count(close_tag)

                if open_count > close_count:
                    # Unclosed tag(s) - close them in this chunk
                    chunk += close_tag * (open_count - close_count)
                    # Reopen them in next chunk
                    if remaining_text:
                        remaining_text = open_tag * (open_count - close_count) + remaining_text

            chunks.append(chunk)

        return chunks

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
        """Send a text message via Telegram Bot API.

        Handles long messages by splitting them into multiple parts.
        Telegram has a 4096 character limit per message.
        """
        try:
            from telegram import Bot

            bot: Bot = self.application.bot
            text = msg["text"]

            # Check if message needs splitting
            if len(text) > TELEGRAM_MESSAGE_MAX_LENGTH:
                chunks = self._split_long_message(text, TELEGRAM_MESSAGE_MAX_LENGTH)
                logger.info(
                    f"Splitting message into {len(chunks)} parts "
                    f"(original: {len(text)} chars)"
                )

                # Send each chunk with part indicator
                for i, chunk in enumerate(chunks, 1):
                    part_indicator = f"\n\n({i}/{len(chunks)})"
                    await bot.send_message(
                        chat_id=msg["chat_id"],
                        text=chunk + part_indicator,
                        parse_mode=msg.get("parse_mode"),
                        disable_web_page_preview=msg.get("disable_web_page_preview", False)
                    )
                    logger.debug(
                        f"Sent text message part {i}/{len(chunks)} "
                        f"to chat {msg['chat_id']} ({len(chunk)} chars)"
                    )
                    # Small delay between parts to respect rate limits
                    await asyncio.sleep(0.05)
            else:
                # Send single message
                await bot.send_message(
                    chat_id=msg["chat_id"],
                    text=text,
                    parse_mode=msg.get("parse_mode"),
                    disable_web_page_preview=msg.get("disable_web_page_preview", False)
                )
                logger.debug(f"Sent text message to chat {msg['chat_id']}")

        except Exception as e:
            logger.error(f"Failed to send text message: {e}", exc_info=True)

    async def _send_voice_message(self, msg: Dict[str, Any]):
        """Send a voice message via Telegram Bot API.

        Truncates caption if it exceeds 1024 characters (Telegram limit).
        """
        try:
            from telegram import Bot

            bot: Bot = self.application.bot
            caption = msg.get("caption")

            # Truncate caption if too long (Telegram limit: 1024)
            if caption and len(caption) > TELEGRAM_CAPTION_MAX_LENGTH:
                caption = caption[:TELEGRAM_CAPTION_MAX_LENGTH - 3] + "..."
                logger.debug(f"Truncated voice caption to {TELEGRAM_CAPTION_MAX_LENGTH} chars")

            with open(msg["file_path"], "rb") as f:
                await bot.send_voice(
                    chat_id=msg["chat_id"],
                    voice=f,
                    caption=caption,
                    duration=msg.get("duration")
                )
            logger.debug(f"Sent voice message to chat {msg['chat_id']}")
        except Exception as e:
            logger.error(f"Failed to send voice message: {e}", exc_info=True)

    async def _send_photo_message(self, msg: Dict[str, Any]):
        """Send a photo message via Telegram Bot API.

        Truncates caption if it exceeds 1024 characters (Telegram limit).
        """
        try:
            from telegram import Bot

            bot: Bot = self.application.bot
            caption = msg.get("caption")

            # Truncate caption if too long (Telegram limit: 1024)
            if caption and len(caption) > TELEGRAM_CAPTION_MAX_LENGTH:
                caption = caption[:TELEGRAM_CAPTION_MAX_LENGTH - 3] + "..."
                logger.debug(f"Truncated photo caption to {TELEGRAM_CAPTION_MAX_LENGTH} chars")

            with open(msg["file_path"], "rb") as f:
                await bot.send_photo(
                    chat_id=msg["chat_id"],
                    photo=f,
                    caption=caption
                )
            logger.debug(f"Sent photo to chat {msg['chat_id']}")
        except Exception as e:
            logger.error(f"Failed to send photo: {e}", exc_info=True)

    async def _send_document_message(self, msg: Dict[str, Any]):
        """Send a document message via Telegram Bot API.

        Truncates caption if it exceeds 1024 characters (Telegram limit).
        """
        try:
            from telegram import Bot

            bot: Bot = self.application.bot
            caption = msg.get("caption")

            # Truncate caption if too long (Telegram limit: 1024)
            if caption and len(caption) > TELEGRAM_CAPTION_MAX_LENGTH:
                caption = caption[:TELEGRAM_CAPTION_MAX_LENGTH - 3] + "..."
                logger.debug(f"Truncated document caption to {TELEGRAM_CAPTION_MAX_LENGTH} chars")

            with open(msg["file_path"], "rb") as f:
                await bot.send_document(
                    chat_id=msg["chat_id"],
                    document=f,
                    caption=caption
                )
            logger.debug(f"Sent document to chat {msg['chat_id']}")
        except Exception as e:
            logger.error(f"Failed to send document: {e}", exc_info=True)

    # Historical message fetching methods (v0.15.0+)

    async def _fetch_historical_messages(self):
        """
        Fetch historical messages from Telegram (within 24-hour window).

        This is called on bot startup to retrieve messages sent while DPC was offline.

        Note: Telegram Bot API limitations:
        - Only works for messages sent within the last 24 hours
        - Maximum 100 messages per request
        - Only incoming messages (sent TO the bot), not outgoing
        """
        from datetime import datetime, timedelta, timezone

        if not self.application or not self.application.bot:
            logger.error("Cannot fetch history: bot not initialized")
            return

        bot = self.application.bot
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.history_max_age_hours)

        logger.info(f"Fetching messages newer than {cutoff_time.isoformat()} (max_age: {self.history_max_age_hours}h)")

        # Process only chats with active conversation links (not all whitelisted chats)
        # This respects user's deletion of conversation links from UI
        if not hasattr(self.service, 'telegram_bridge') or self.service.telegram_bridge is None:
            logger.warning("TelegramBridge not initialized, skipping history fetch")
            return

        linked_chat_ids = self.service.telegram_bridge.conversation_map.keys()
        if not linked_chat_ids:
            logger.info("No active conversation links, skipping history fetch")
            return

        for chat_id in linked_chat_ids:
            try:
                await self._fetch_chat_history(bot, chat_id, cutoff_time)
                await asyncio.sleep(0.5)  # Rate limiting delay between chats
            except Exception as e:
                logger.error(f"Failed to fetch history for chat {chat_id}: {e}", exc_info=True)

    async def _fetch_chat_history(self, bot, chat_id: str, cutoff_time: datetime):
        """Fetch historical messages for a specific chat."""
        last_update_id = self.service.settings.get_telegram_last_update_id(chat_id)
        logger.info(f"Fetching history for chat {chat_id} (last_update_id: {last_update_id})")

        updates = []
        offset = last_update_id + 1 if last_update_id > 0 else None

        while len(updates) < self.history_fetch_limit:
            try:
                batch = await bot.get_updates(
                    timeout=10,
                    offset=offset,
                    limit=min(100, self.history_fetch_limit - len(updates))
                )

                if not batch:
                    break

                for update in batch:
                    # Check cutoff time
                    if update.message and update.message.date:
                        if update.message.date < cutoff_time:
                            logger.debug(f"Reached cutoff time for chat {chat_id}")
                            break

                    # Check message type filter
                    if self._should_process_message_type(update):
                        updates.append(update)
                        if update.update_id > last_update_id:
                            last_update_id = update.update_id

                if len(updates) >= self.history_fetch_limit:
                    break

                if batch:
                    offset = batch[-1].update_id + 1

                await asyncio.sleep(0.3)  # Rate limiting between batches

            except Exception as e:
                logger.error(f"Error fetching batch for chat {chat_id}: {e}")
                break

        # Process updates
        logger.info(f"Processing {len(updates)} historical messages for chat {chat_id}")
        for update in updates:
            try:
                await self._process_historical_update(update)
            except Exception as e:
                logger.error(f"Error processing historical update {update.update_id}: {e}")

        # Save last update_id
        if last_update_id > 0:
            self.service.settings.set_telegram_last_update_id(chat_id, last_update_id)
            logger.info(f"Saved last_update_id {last_update_id} for chat {chat_id}")

    def _should_process_message_type(self, update) -> bool:
        """Check if message type should be processed during history fetch."""
        if not update.message:
            return False

        msg = update.message
        if "text" in self.history_message_types and msg.text:
            return True
        if "voice" in self.history_message_types and msg.voice:
            return True
        if "photo" in self.history_message_types and msg.photo:
            return True
        if "document" in self.history_message_types and msg.document:
            return True
        if "video" in self.history_message_types and msg.video:
            return True
        return False

    async def _process_historical_update(self, update):
        """Process historical update through existing handlers."""
        # Create a mock context for the handler
        class MockContext:
            def __init__(self, bot):
                self.bot = bot
                self._data = {}

        context = MockContext(self.application.bot)
        bridge = self.service.telegram_bridge

        # Route to appropriate handler based on message type
        if update.message and update.message.text:
            await bridge.handle_text_message(update, context)
        elif update.message and update.message.voice:
            await bridge.handle_voice_message(update, context)
        elif update.message and update.message.photo:
            await bridge.handle_photo_message(update, context)
        elif update.message and update.message.document:
            await bridge.handle_document_message(update, context)
        elif update.message and update.message.video:
            await bridge.handle_video_message(update, context)

    # Agent-Telegram Linking Methods (v0.15.0+)

    async def link_agent_to_chat(self, agent_id: str, chat_id: str) -> Dict[str, Any]:
        """
        Link an agent to a Telegram chat.

        This method:
        1. Validates the chat_id exists in the whitelist
        2. Updates the agent registry with the telegram_chat_id
        3. Emits an event for UI updates

        Args:
            agent_id: Agent to link
            chat_id: Telegram chat ID (numeric string)

        Returns:
            Dict with success status and message

        Raises:
            ValueError: If chat_id is not whitelisted or invalid
        """
        try:
            # Import AgentRegistry
            from ..dpc_agent.utils import AgentRegistry

            # Validate chat_id format
            if not isinstance(chat_id, str):
                raise ValueError("chat_id must be a string")
            if not chat_id.lstrip('-').isdigit():
                raise ValueError("chat_id must be a numeric string")

            # Check if chat_id is whitelisted
            if not self.is_allowed(chat_id):
                raise ValueError(f"chat_id {chat_id} is not in the Telegram whitelist")

            # Update agent registry
            registry = AgentRegistry()
            agent_meta = registry.link_agent_to_telegram(agent_id, chat_id)

            if not agent_meta:
                return {
                    "success": False,
                    "error": f"Agent {agent_id} not found"
                }

            logger.info(f"Linked agent {agent_id} to Telegram chat {chat_id}")

            # Emit event for UI updates
            await self.service.local_api.broadcast_event("agent_telegram_linked", {
                "agent_id": agent_id,
                "chat_id": chat_id,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

            return {
                "success": True,
                "agent_id": agent_id,
                "chat_id": chat_id,
                "message": f"Agent {agent_id} linked to Telegram chat {chat_id}"
            }

        except ValueError as e:
            logger.warning(f"Failed to link agent to Telegram: {e}")
            return {
                "success": False,
                "error": str(e)
            }
        except Exception as e:
            logger.error(f"Unexpected error linking agent to Telegram: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to link agent: {str(e)}"
            }

    async def unlink_agent_from_chat(self, agent_id: str) -> Dict[str, Any]:
        """
        Remove Telegram chat linkage for an agent.

        Args:
            agent_id: Agent to unlink

        Returns:
            Dict with success status and message
        """
        try:
            from ..dpc_agent.utils import AgentRegistry

            registry = AgentRegistry()
            agent_meta = registry.unlink_agent_from_telegram(agent_id)

            if not agent_meta:
                return {
                    "success": False,
                    "error": f"Agent {agent_id} not found"
                }

            logger.info(f"Unlinked agent {agent_id} from Telegram chat")

            # Emit event for UI updates
            await self.service.local_api.broadcast_event("agent_telegram_unlinked", {
                "agent_id": agent_id,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

            return {
                "success": True,
                "agent_id": agent_id,
                "message": f"Agent {agent_id} unlinked from Telegram"
            }

        except Exception as e:
            logger.error(f"Failed to unlink agent from Telegram: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to unlink agent: {str(e)}"
            }

    async def get_agent_linked_chat(self, agent_id: str) -> Optional[str]:
        """
        Get the telegram_chat_id for an agent.

        Args:
            agent_id: Agent to query

        Returns:
            Telegram chat ID or None if not linked
        """
        try:
            from ..dpc_agent.utils import AgentRegistry

            registry = AgentRegistry()
            return registry.get_agent_linked_chat(agent_id)

        except Exception as e:
            logger.error(f"Failed to get agent linked chat: {e}", exc_info=True)
            return None

    async def list_linked_agents(self) -> List[Dict[str, Any]]:
        """
        List all agents with Telegram links.

        Returns:
            List of agent metadata dicts with telegram_enabled=True
        """
        try:
            from ..dpc_agent.utils import AgentRegistry

            registry = AgentRegistry()
            return registry.list_linked_agents()

        except Exception as e:
            logger.error(f"Failed to list linked agents: {e}", exc_info=True)
            return []
