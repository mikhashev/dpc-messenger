"""
Agent Telegram Bridge - Two-way communication with DPC Agent via Telegram.

Creates a separate Telegram bot for DPC Agent interaction.
This is distinct from the existing TelegramBotManager which handles
user communication - this is specifically for agent monitoring and control.

Features:
- Receives events from AgentEventEmitter → Telegram notifications
- Receives messages from Telegram → DPC Agent tasks
- Filters events by type
- Formats events as Telegram messages
- Supports multiple chat IDs for notifications
- Rate limiting to avoid spam
- Two-way communication (send tasks to agent)

Setup:
1. Create new bot via @BotFather (different from main DPC bot)
2. Get bot token
3. Get chat_id via @userinfobot
4. Configure in ~/.dpc/config.ini [dpc_agent_telegram] section

Usage (from Telegram):
- Send any message to the bot → Agent will process it
- Use /status to check agent status
- Use /help to see available commands
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set

from ..dpc_agent.events import AgentEvent, EventType

if TYPE_CHECKING:
    from .agent_manager import DpcAgentManager

log = logging.getLogger(__name__)

# Telegram message limits
TELEGRAM_MESSAGE_MAX_LENGTH = 4096

# Event formatting
EVENT_EMOJIS = {
    "agent_started": "🚀",
    "agent_stopped": "🛑",
    "task_scheduled": "📋",
    "task_started": "▶️",
    "task_completed": "✅",
    "task_failed": "❌",
    "thought_started": "💭",
    "thought_completed": "🧠",
    "tool_executed": "🔧",
    "evolution_cycle_started": "🔄",
    "evolution_cycle_completed": "🧬",
    "code_modified": "📝",
    "identity_updated": "👤",
    "scratchpad_updated": "📝",
    "knowledge_updated": "📚",
    "budget_warning": "⚠️",
    "rate_limit_hit": "🚫",
}


def escape_markdown(text: str) -> str:
    """
    Escape special characters for Telegram Markdown v2.

    Characters that need escaping: _ * [ ] ( ) ~ ` > # + - = | { } . !
    """
    special_chars = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special_chars else c for c in text)


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""
    max_events_per_minute: int = 20
    cooldown_seconds: float = 3.0  # Minimum time between same-type events


class AgentTelegramBridge:
    """
    Two-way bridge between DPC Agent and Telegram.

    Features:
    - Forwards agent events to Telegram (notifications)
    - Receives messages from Telegram and forwards to agent (tasks)
    - Supports commands: /status, /help

    Example:
        >>> bridge = AgentTelegramBridge(bot_token, chat_ids, event_filter)
        >>> bridge.set_message_handler(agent_manager.process_message)
        >>> await bridge.start()
        >>> # Events are forwarded to Telegram, messages are sent to agent
        >>> await bridge.stop()
    """

    def __init__(
        self,
        bot_token: str,
        allowed_chat_ids: List[str],
        event_filter: Optional[List[str]] = None,
        rate_limit: Optional[RateLimitConfig] = None,
    ):
        """
        Initialize agent Telegram bridge.

        Args:
            bot_token: Telegram bot token
            allowed_chat_ids: Chat IDs to send notifications to
            event_filter: List of event types to forward (None = all important events)
            rate_limit: Rate limiting configuration
        """
        self.bot_token = bot_token
        self.allowed_chat_ids = [str(cid) for cid in allowed_chat_ids]  # Ensure strings
        self.event_filter = set(event_filter) if event_filter else self._default_event_filter()
        self.rate_limit = rate_limit or RateLimitConfig()

        self._bot = None
        self._application = None  # telegram.ext.Application
        self._enabled = False
        self._session = None

        # Rate limiting state
        self._event_times: Dict[str, List[float]] = {}  # event_type -> list of timestamps
        self._last_event_time: Dict[str, float] = {}  # event_type -> last timestamp

        # Message handler callback (set by agent_manager)
        self._message_handler: Optional[Callable] = None
        self._agent_manager: Optional["DpcAgentManager"] = None

        log.info(f"AgentTelegramBridge initialized for {len(allowed_chat_ids)} chat(s), "
                f"filter={len(self.event_filter)} event types")

    def _default_event_filter(self) -> Set[str]:
        """Get default event filter - important events only."""
        return {
            # Tasks
            EventType.TASK_STARTED.value,
            EventType.TASK_COMPLETED.value,
            EventType.TASK_FAILED.value,
            # Evolution
            EventType.EVOLUTION_CYCLE_COMPLETED.value,
            EventType.CODE_MODIFIED.value,
            # Budget warnings
            EventType.BUDGET_WARNING.value,
            EventType.RATE_LIMIT_HIT.value,
        }

    def set_message_handler(self, handler: Callable, agent_manager: "DpcAgentManager" = None):
        """
        Set the callback for handling incoming Telegram messages.

        Args:
            handler: Async function(message: str, chat_id: str) -> str
            agent_manager: Optional agent manager reference for status commands
        """
        self._message_handler = handler
        self._agent_manager = agent_manager
        log.info("Message handler set for incoming Telegram messages")

    async def start(self) -> bool:
        """
        Start the Telegram bot with polling for incoming messages.

        Returns:
            True if started successfully, False otherwise
        """
        if self._enabled:
            log.warning("AgentTelegramBridge already running")
            return True

        if not self.bot_token:
            log.warning("No bot token configured, Telegram bridge disabled")
            return False

        if not self.allowed_chat_ids:
            log.warning("No chat IDs configured, Telegram bridge disabled")
            return False

        try:
            # Import telegram library
            from telegram import Bot
            from telegram.ext import Application, MessageHandler, filters, CommandHandler
            from telegram.request import HTTPXRequest

            # Create session with timeout
            request = HTTPXRequest(
                connection_pool_size=5,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
            )
            self._bot = Bot(token=self.bot_token, request=request)

            # Verify bot is valid
            me = await self._bot.get_me()
            log.info(f"Agent Telegram bridge started: @{me.username}")

            # Create application for polling
            self._application = Application.builder().token(self.bot_token).request(request).build()

            # Add handlers for commands
            self._application.add_handler(CommandHandler("start", self._handle_start_command))
            self._application.add_handler(CommandHandler("help", self._handle_help_command))
            self._application.add_handler(CommandHandler("status", self._handle_status_command))

            # Add handler for regular messages (non-commands)
            self._application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

            # Initialize and start polling
            await self._application.initialize()
            await self._application.start()
            await self._application.updater.start_polling(drop_pending_updates=True)

            self._enabled = True
            log.info("Agent Telegram bridge polling started (two-way communication enabled)")
            return True

        except ImportError:
            log.error("python-telegram-bot not installed. Install with: pip install python-telegram-bot")
            return False
        except Exception as e:
            log.error(f"Failed to start agent Telegram bridge: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        """Stop the bridge and polling."""
        self._enabled = False

        # Stop the application and updater
        if self._application:
            try:
                if self._application.updater and self._application.updater.running:
                    await self._application.updater.stop()
                await self._application.stop()
                await self._application.shutdown()
            except Exception as e:
                log.debug(f"Error stopping application: {e}")
            self._application = None

        if self._session:
            try:
                await self._session.shutdown()
            except Exception:
                pass
            self._session = None

        self._bot = None
        log.info("Agent Telegram bridge stopped")

    async def _handle_start_command(self, update, context):
        """Handle /start command."""
        chat_id = str(update.effective_chat.id)

        if chat_id not in self.allowed_chat_ids:
            await update.message.reply_text("⛔ Unauthorized. Your chat ID is not in the allowed list.")
            return

        welcome = """🤖 *DPC Agent Bot*

Welcome! You can send messages to the DPC Agent.

*Commands:*
/help - Show available commands
/status - Check agent status

*Usage:*
Just send any message and the agent will process it.

Configure event types in `~/.dpc/config.ini` [dpc_agent_telegram] section.
"""
        await update.message.reply_text(welcome, parse_mode="Markdown")

    async def _handle_help_command(self, update, context):
        """Handle /help command."""
        chat_id = str(update.effective_chat.id)

        if chat_id not in self.allowed_chat_ids:
            return

        help_text = """🤖 *DPC Agent Bot - Help*

*Commands:*
/start - Initialize bot
/help - Show this help
/status - Check agent status

*Sending Tasks:*
Just type a message and the agent will process it.

*Examples:*
• "Show me the weather forecast"
• "Check my recent git commits"
• "What files are in memory/?"
• "Schedule a task to review code in 5 minutes"

*Tips:*
• Be specific in your requests
• The agent has access to configured tools
• Check firewall rules if tools seem unavailable
"""
        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def _handle_status_command(self, update, context):
        """Handle /status command."""
        chat_id = str(update.effective_chat.id)

        if chat_id not in self.allowed_chat_ids:
            return

        status_lines = ["🤖 *DPC Agent Status*"]

        # Get agent manager status if available
        if self._agent_manager:
            try:
                status = self._agent_manager.get_status()
                status_lines.append(f"📊 Initialized: `{status.get('initialized', False)}`")
                if "agent" in status:
                    agent_status = status["agent"]
                    status_lines.append(f"🧠 Consciousness: `{agent_status.get('consciousness_running', False)}`")
                    status_lines.append(f"📋 Task Queue: `{agent_status.get('queue_enabled', False)}`")
            except Exception as e:
                status_lines.append(f"❌ Error getting status: {escape_markdown(str(e))}")
        else:
            status_lines.append("⚠️ Agent manager not connected")

        # Bridge status
        status_lines.append(f"\n📡 Bridge: `{'Online' if self._enabled else 'Offline'}`")
        status_lines.append(f"💬 Allowed chats: `{len(self.allowed_chat_ids)}`")

        await update.message.reply_text("\n".join(status_lines), parse_mode="Markdown")

    async def _handle_message(self, update, context):
        """Handle incoming text message."""
        chat_id = str(update.effective_chat.id)
        message_text = update.message.text

        # Check authorization
        if chat_id not in self.allowed_chat_ids:
            log.warning(f"Unauthorized message from chat_id={chat_id}")
            await update.message.reply_text("⛔ Unauthorized. Your chat ID is not in the allowed list.")
            return

        # Check if we have a message handler
        if not self._message_handler:
            await update.message.reply_text("⚠️ Message handler not configured. Cannot process message.")
            return

        # Send "processing" indicator
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        log.info(f"Processing Telegram message from chat {chat_id}: {message_text[:50]}...")

        try:
            # Call the message handler (agent_manager.process_message)
            response = await self._message_handler(
                message=message_text,
                conversation_id=f"telegram-{chat_id}",
                include_context=True,
            )

            # Send response (truncate if needed)
            if len(response) > TELEGRAM_MESSAGE_MAX_LENGTH:
                # Split long messages
                chunks = self._split_message(response, TELEGRAM_MESSAGE_MAX_LENGTH - 100)
                for i, chunk in enumerate(chunks):
                    prefix = f"📄 *Part {i+1}/{len(chunks)}*\n\n" if len(chunks) > 1 else ""
                    await update.message.reply_text(prefix + chunk, parse_mode="Markdown")
            else:
                await update.message.reply_text(response, parse_mode="Markdown")

        except Exception as e:
            log.error(f"Error processing Telegram message: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error processing message: {str(e)[:200]}")

    def _split_message(self, text: str, max_length: int) -> List[str]:
        """Split a long message into chunks."""
        if len(text) <= max_length:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_length:
                chunks.append(text)
                break

            # Try to split at newline
            split_pos = text.rfind('\n', 0, max_length)
            if split_pos == -1:
                split_pos = max_length

            chunks.append(text[:split_pos])
            text = text[split_pos:].lstrip('\n')

        return chunks

    def is_enabled(self) -> bool:
        """Check if bridge is enabled."""
        return self._enabled and self._bot is not None

    async def handle_event(self, event: AgentEvent) -> bool:
        """
        Handle an agent event and forward to Telegram.

        This is the callback for AgentEventEmitter.

        Args:
            event: The agent event

        Returns:
            True if sent successfully, False otherwise
        """
        if not self._enabled or not self._bot:
            return False

        # Filter events
        if event.type.value not in self.event_filter:
            log.debug(f"Event filtered out: {event.type.value} not in {self.event_filter}")
            return False

        # Rate limit check
        if not self._check_rate_limit(event.type.value):
            log.warning(f"Rate limited event: {event.type.value}")
            return False

        log.info(f"Sending Telegram notification for event: {event.type.value}")

        # Format message
        message = self._format_event(event)

        if not message:
            return False

        # Send to all allowed chats
        success = True
        for chat_id in self.allowed_chat_ids:
            try:
                await self._send_message(chat_id, message)
            except Exception as e:
                log.error(f"Failed to send Telegram message to {chat_id}: {e}")
                success = False

        return success

    def _check_rate_limit(self, event_type: str) -> bool:
        """
        Check if event should be rate limited.

        Args:
            event_type: Type of event

        Returns:
            True if should send, False if rate limited
        """
        now = time.time()

        # Check cooldown for same event type
        last_time = self._last_event_time.get(event_type, 0)
        if now - last_time < self.rate_limit.cooldown_seconds:
            return False

        # Check per-minute limit (global)
        minute_ago = now - 60
        recent_events = [
            t for t in self._event_times.get("global", [])
            if t > minute_ago
        ]

        if len(recent_events) >= self.rate_limit.max_events_per_minute:
            return False

        # Update tracking
        self._last_event_time[event_type] = now
        self._event_times.setdefault("global", []).append(now)

        # Clean old entries
        self._event_times["global"] = [t for t in self._event_times["global"] if t > minute_ago]

        return True

    async def _send_message(self, chat_id: str, text: str) -> None:
        """
        Send a message to a Telegram chat.

        Args:
            chat_id: Target chat ID
            text: Message text (Markdown format)
        """
        # Truncate if needed
        if len(text) > TELEGRAM_MESSAGE_MAX_LENGTH:
            text = text[:TELEGRAM_MESSAGE_MAX_LENGTH - 50] + "\n\n... (truncated)"

        await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            disable_notification=False,
        )

    def _format_event(self, event: AgentEvent) -> str:
        """
        Format event for Telegram message.

        Args:
            event: The agent event

        Returns:
            Formatted Markdown string
        """
        emoji = EVENT_EMOJIS.get(event.type.value, "📍")
        timestamp = event.timestamp[11:19] if event.timestamp else "?"  # Just time

        # Event type as title
        title = event.type.value.replace("_", " ").title()

        lines = [
            f"{emoji} *{title}*",
            f"⏰ `{timestamp}`",
        ]

        # Add event-specific details
        data = event.data

        # Task events
        if "task_id" in data:
            lines.append(f"📋 Task: `{data['task_id']}`")
        if "task_type" in data:
            lines.append(f"📁 Type: {data['task_type']}")
        if "message_preview" in data:
            preview = str(data["message_preview"])[:150]
            lines.append(f"💬 Preview: {preview}")
        if "conversation_id" in data:
            lines.append(f"🔗 Conv: `{data['conversation_id'][:30]}`")

        # Tool events
        if "tool" in data:
            lines.append(f"🔧 Tool: `{data['tool']}`")

        # Thought events
        if "thought_type" in data:
            lines.append(f"💭 Thought: {data['thought_type']}")
        if "thought_number" in data:
            lines.append(f"#️⃣ Number: {data['thought_number']}")

        # Evolution events
        if "cycle_id" in data:
            lines.append(f"🔄 Cycle: `{data['cycle_id']}`")
        if "cycle_number" in data:
            lines.append(f"#️⃣ Cycle #: {data['cycle_number']}")
        if "files_modified" in data:
            lines.append(f"📄 Files: {data['files_modified']}")
        if "changes_applied" in data:
            lines.append(f"✏️ Changes: {data['changes_applied']}")

        # Code modified
        if "path" in data:
            lines.append(f"📄 Path: `{data['path']}`")

        # Description/result
        if "description" in data:
            desc = str(data["description"])[:200]
            lines.append(f"📝 {desc}")
        if "result" in data and data["result"]:
            result = str(data["result"])[:200]
            lines.append(f"📄 Result: {result}")

        # Error
        if "error" in data:
            error = str(data["error"])[:200]
            lines.append(f"❌ Error: {error}")

        return "\n".join(lines)

    async def send_test_message(self) -> bool:
        """
        Send a test message to verify bridge is working.

        Returns:
            True if sent successfully
        """
        if not self._enabled or not self._bot:
            return False

        test_message = """🤖 *DPC Agent Telegram Bridge*

✅ Connection successful!

You will receive notifications for agent events:
• Task completions and failures
• Evolution cycles
• Code modifications
• Budget warnings

Configure event types in `~/.dpc/config.ini` [dpc_agent_telegram] section.
"""

        for chat_id in self.allowed_chat_ids:
            try:
                await self._send_message(chat_id, test_message)
            except Exception as e:
                log.error(f"Failed to send test message to {chat_id}: {e}")
                return False

        return True

    def get_status(self) -> Dict[str, Any]:
        """Get bridge status."""
        return {
            "enabled": self._enabled,
            "bot_connected": self._bot is not None,
            "chat_count": len(self.allowed_chat_ids),
            "event_filter": list(self.event_filter),
            "rate_limit": {
                "max_per_minute": self.rate_limit.max_events_per_minute,
                "cooldown_seconds": self.rate_limit.cooldown_seconds,
            },
        }


def create_telegram_bridge_callback(bridge: AgentTelegramBridge):
    """
    Create a callback function for AgentEventEmitter.

    Usage:
        bridge = AgentTelegramBridge(bot_token, chat_ids)
        await bridge.start()
        emitter.add_listener(create_telegram_bridge_callback(bridge))

    Args:
        bridge: The AgentTelegramBridge instance

    Returns:
        Callback function suitable for add_listener()
    """
    async def callback(event: AgentEvent) -> None:
        await bridge.handle_event(event)

    callback.__name__ = "telegram_bridge_callback"
    return callback
