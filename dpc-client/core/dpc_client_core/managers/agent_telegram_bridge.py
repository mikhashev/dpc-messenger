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
import base64
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
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
    "agent_message": "🤖",
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
        transcription_enabled: bool = True,
        agent_id: str = "",
        unified_conversation: bool = False,
    ):
        """
        Initialize agent Telegram bridge.

        Args:
            bot_token: Telegram bot token
            allowed_chat_ids: Chat IDs to send notifications to
            event_filter: List of event types to forward (None = all important events)
            rate_limit: Rate limiting configuration
            transcription_enabled: Enable voice message transcription (default: True)
            agent_id: Agent ID this bridge belongs to (used for unified conversation)
            unified_conversation: When True, Telegram messages share conversation history
                                  with the DPC chat UI (conversation_id = agent_id)
        """
        self.bot_token = bot_token
        self.allowed_chat_ids = [str(cid) for cid in allowed_chat_ids]  # Ensure strings
        self.event_filter = set(event_filter) if event_filter else self._default_event_filter()
        self.rate_limit = rate_limit or RateLimitConfig()
        self.transcription_enabled = transcription_enabled
        self._agent_id = agent_id
        self._unified_conversation = unified_conversation

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

        # Pending knowledge commit proposals awaiting Telegram approval
        # Maps proposal_id -> chat_id so vote callbacks can identify who to respond to
        self._pending_proposals: Dict[str, str] = {}

        # Semaphore to limit concurrent Telegram API calls (prevents pool exhaustion)
        self._send_semaphore = asyncio.Semaphore(3)  # Max 3 concurrent sends

        # Consecutive network error counter for log suppression
        self._network_error_count = 0

        log.info(f"AgentTelegramBridge initialized for {len(allowed_chat_ids)} chat(s), "
                f"filter={len(self.event_filter)} event types, transcription={transcription_enabled}")

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
            # Agent-initiated messages
            EventType.AGENT_MESSAGE.value,
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
            from telegram.ext import Application, MessageHandler, filters, CommandHandler, CallbackQueryHandler
            from telegram.request import HTTPXRequest

            # Create session with timeout
            request = HTTPXRequest(
                connection_pool_size=10,  # Increased from 5 to prevent pool exhaustion
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
                pool_timeout=60,  # Add explicit pool timeout
            )

            # Create application for polling (this manages the bot instance)
            self._application = Application.builder().token(self.bot_token).request(request).build()

            # Add handlers for commands
            self._application.add_handler(CommandHandler("start", self._handle_start_command))
            self._application.add_handler(CommandHandler("help", self._handle_help_command))
            self._application.add_handler(CommandHandler("status", self._handle_status_command))
            self._application.add_handler(CommandHandler("clear", self._handle_clear_command))
            self._application.add_handler(CommandHandler("newsession", self._handle_newsession_command))
            self._application.add_handler(CommandHandler("endsession", self._handle_endsession_command))

            # Add handler for inline keyboard votes on knowledge commit proposals
            self._application.add_handler(CallbackQueryHandler(self._handle_vote_callback, pattern=r"^vote:"))

            # Add handler for regular messages (non-commands)
            self._application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

            # Add handler for voice messages
            self._application.add_handler(MessageHandler(filters.VOICE, self._handle_voice_message))

            # Add handler for photo messages (vision analysis)
            self._application.add_handler(MessageHandler(filters.PHOTO, self._handle_photo_message))

            # Initialize application (creates bot instance internally)
            await self._application.initialize()

            # Use the Application's bot instance (not a separate one)
            # This ensures proper event loop integration
            self._bot = self._application.bot

            # Verify bot is valid
            me = await self._bot.get_me()
            log.info(f"Agent Telegram bridge started: @{me.username}")

            # Start polling
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
            log.warning(f"Unauthorized /start from chat_id {chat_id}, silent drop")
            return

        welcome = """🤖 *DPC Agent Bot*

Welcome\\! You can send messages to the DPC Agent\\.

*Commands:*
/help \\- Show available commands
/status \\- Check agent status
/newsession \\- Start a new session
/endsession \\- End session and save knowledge

*Usage:*
Just send any message and the agent will process it\\.
You can also send voice messages for transcription\\.

Configure event types in `~/.dpc/config.ini` \\[dpc\\_agent\\_telegram\\] section\\.
"""
        await update.message.reply_text(welcome, parse_mode="MarkdownV2")

    async def _handle_help_command(self, update, context):
        """Handle /help command."""
        chat_id = str(update.effective_chat.id)

        if chat_id not in self.allowed_chat_ids:
            return

        help_text = """🤖 *DPC Agent Bot \\- Help*

*Commands:*
/start \\- Initialize bot
/help \\- Show this help
/status \\- Check agent status
/clear \\- Clear conversation history and start fresh
/newsession \\- Start a new session \\(clears history\\)
/endsession \\- End session and extract knowledge to personal context

*Sending Tasks:*
Just type a message and the agent will process it\\.
You can also send voice messages for transcription\\.

*Examples:*
• "Show me the weather forecast"
• "Check my recent git commits"
• "What files are in memory/?"
• "Schedule a task to review code in 5 minutes"

*Voice Messages:*
Send a voice message and it will be transcribed and processed\\.

*Session Management:*
• /clear — instant history reset \\(no knowledge saved\\)
• /newsession — same as /clear
• /endsession — extracts knowledge, shows inline approve/reject buttons
• You can approve or reject the knowledge proposal directly here

*Tips:*
• Be specific in your requests
• The agent has access to configured tools
• Check firewall rules if tools seem unavailable
"""
        await update.message.reply_text(help_text, parse_mode="MarkdownV2")

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

        await update.message.reply_text("\n".join(status_lines), parse_mode="MarkdownV2")

    async def _handle_clear_command(self, update, context):
        """Handle /clear command - reset conversation context."""
        chat_id = str(update.effective_chat.id)

        if chat_id not in self.allowed_chat_ids:
            return

        conversation_id = self._agent_id if self._unified_conversation and self._agent_id else f"telegram-{chat_id}"

        # Reset the conversation monitor
        if self._agent_manager:
            success = self._agent_manager.reset_conversation(conversation_id)
            if success:
                await update.message.reply_text(
                    "✅ *Conversation Cleared*\n\n"
                    "Context and history have been reset\\. "
                    "You can start a fresh conversation now\\.",
                    parse_mode="MarkdownV2"
                )
            else:
                await update.message.reply_text(
                    "ℹ️ *No Conversation Found*\n\n"
                    "No existing conversation to clear\\. "
                    "Start chatting with the agent first\\.",
                    parse_mode="MarkdownV2"
                )
        else:
            await update.message.reply_text("⚠️ Agent manager not available\\.")

    async def _handle_newsession_command(self, update, context):
        """Handle /newsession command — clear history and start a fresh session."""
        chat_id = str(update.effective_chat.id)

        if chat_id not in self.allowed_chat_ids:
            return

        conversation_id = self._agent_id if self._unified_conversation and self._agent_id else f"telegram-{chat_id}"
        service = getattr(self._agent_manager, 'service', None) if self._agent_manager else None

        if not service:
            await update.message.reply_text("⚠️ Service not available\\.", parse_mode="MarkdownV2")
            return

        try:
            result = await service.propose_new_session(conversation_id)
            if result.get("status") == "success":
                await update.message.reply_text(
                    "🔄 *New Session Started*\n\n"
                    "Conversation history has been cleared\\. "
                    "Start fresh\\!",
                    parse_mode="MarkdownV2"
                )
                # Push empty history to DPC chat UI if unified
                if self._unified_conversation:
                    await self._broadcast_history_to_ui(conversation_id)
            else:
                msg = escape_markdown(result.get("message", "Unknown error"))
                await update.message.reply_text(f"❌ Failed to start new session: {msg}", parse_mode="MarkdownV2")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {escape_markdown(str(e)[:200])}", parse_mode="MarkdownV2")

    def _build_proposal_message(self, proposal_id: str, proposal) -> tuple:
        """Build Telegram message text and keyboard for a knowledge proposal.

        Returns (text, InlineKeyboardMarkup) so both /endsession and
        notify_knowledge_proposal use identical formatting.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        # Build entries preview
        entries = getattr(proposal, 'entries', []) or []
        entries_lines = []
        for i, entry in enumerate(entries, 1):
            content = getattr(entry, 'content', str(entry))
            confidence = getattr(entry, 'confidence', 1.0)
            tags = getattr(entry, 'tags', [])
            tag_str = f" \\[{escape_markdown(', '.join(tags))}\\]" if tags else ""
            entries_lines.append(
                f"{i}\\. {escape_markdown(content[:120])} _{confidence:.0%} confidence_{tag_str}"
            )

        topic = escape_markdown(getattr(proposal, 'topic', 'Unknown') or 'Unknown')
        summary = escape_markdown(getattr(proposal, 'summary', '') or '')
        avg_conf = getattr(proposal, 'avg_confidence', 1.0)
        entries_text = "\n".join(entries_lines) if entries_lines else "_No entries_"

        text = (
            f"📚 *Knowledge Proposal*\n\n"
            f"*Topic:* {topic}\n"
            f"*Summary:* {summary}\n"
            f"*Confidence:* {avg_conf:.0%} \\| *Entries:* {len(entries)}\n\n"
            f"*Entries:*\n{entries_text}\n\n"
            f"Review and vote:"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"vote:{proposal_id}:approve"),
                InlineKeyboardButton("🔄 Request Changes", callback_data=f"vote:{proposal_id}:request_changes"),
                InlineKeyboardButton("❌ Reject", callback_data=f"vote:{proposal_id}:reject"),
            ]
        ])
        return text, keyboard

    async def _handle_endsession_command(self, update, context):
        """Handle /endsession command — end session and trigger knowledge extraction."""
        chat_id = str(update.effective_chat.id)

        if chat_id not in self.allowed_chat_ids:
            return

        conversation_id = self._agent_id if self._unified_conversation and self._agent_id else f"telegram-{chat_id}"
        service = getattr(self._agent_manager, 'service', None) if self._agent_manager else None

        if not service:
            await update.message.reply_text("⚠️ Service not available\\.", parse_mode="MarkdownV2")
            return

        await update.message.reply_text(
            "🧠 *Ending Session\\.\\.\\.*\n\nAnalyzing conversation for knowledge\\.\\.\\.",
            parse_mode="MarkdownV2"
        )

        try:
            result = await service.end_conversation_session(
                conversation_id,
                initiated_by="telegram",
            )
            status = result.get("status", "unknown")

            if status == "success":
                proposal_id = result.get("proposal_id")
                if proposal_id:
                    # Fetch the proposal object so we can show its content
                    proposal = None
                    if (hasattr(service, 'consensus_manager')
                            and proposal_id in service.consensus_manager.sessions):
                        proposal = service.consensus_manager.sessions[proposal_id].proposal

                    self._pending_proposals[proposal_id] = chat_id

                    if proposal:
                        text, keyboard = self._build_proposal_message(proposal_id, proposal)
                    else:
                        # Fallback: no proposal object available
                        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                        text = "📚 *Knowledge proposal created\\!*\n\nReview and vote:"
                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Approve", callback_data=f"vote:{proposal_id}:approve"),
                            InlineKeyboardButton("🔄 Request Changes", callback_data=f"vote:{proposal_id}:request_changes"),
                            InlineKeyboardButton("❌ Reject", callback_data=f"vote:{proposal_id}:reject"),
                        ]])

                    await update.message.reply_text(
                        f"✅ *Session Ended*\n\n{text}",
                        parse_mode="MarkdownV2",
                        reply_markup=keyboard,
                    )
                else:
                    await update.message.reply_text(
                        "✅ *Session Ended*\n\n"
                        "No new knowledge found in this conversation\\.",
                        parse_mode="MarkdownV2"
                    )
            else:
                msg = escape_markdown(result.get("message", "Unknown error"))
                await update.message.reply_text(f"❌ Failed to end session: {msg}", parse_mode="MarkdownV2")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {escape_markdown(str(e)[:200])}", parse_mode="MarkdownV2")

    async def _handle_vote_callback(self, update, context):
        """
        Handle inline keyboard vote callback for knowledge commit proposals.

        Callback data format: "vote:{proposal_id}:{approve|reject}"
        """
        query = update.callback_query
        await query.answer()  # Acknowledge the callback to stop the loading spinner

        chat_id = str(query.message.chat.id)
        if chat_id not in self.allowed_chat_ids:
            await query.edit_message_text("⛔ Unauthorized.")
            return

        parts = (query.data or "").split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("❌ Invalid vote data.")
            return

        _, proposal_id, vote = parts
        service = getattr(self._agent_manager, 'service', None) if self._agent_manager else None
        if not service:
            await query.edit_message_text("⚠️ Service not available.")
            return

        try:
            result = await service.vote_knowledge_commit(proposal_id, vote)
            status = result.get("status", "unknown")

            if status == "success":
                vote_labels = {
                    "approve": "✅ Vote recorded — waiting for agent review\\.",
                    "reject": "❌ Vote recorded — waiting for agent review\\.",
                    "request_changes": "🔄 Vote recorded — agent will be asked to revise\\.",
                }
                label = vote_labels.get(vote, "Vote recorded\\.")
                # Edit the proposal message to show vote was cast; final result
                # will arrive via notify_knowledge_result once all votes are in.
                await query.edit_message_text(label, parse_mode="MarkdownV2")
            else:
                msg = escape_markdown(result.get("message", "Unknown error"))
                await query.edit_message_text(
                    f"❌ *Vote failed:* {msg}",
                    parse_mode="MarkdownV2",
                )
                self._pending_proposals.pop(proposal_id, None)

        except Exception as e:
            log.error(f"Error handling vote callback: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Error: {escape_markdown(str(e)[:200])}", parse_mode="MarkdownV2")

    async def notify_knowledge_proposal(
        self,
        proposal_id: str,
        proposal=None,
        chat_ids: Optional[List[str]] = None,
    ) -> None:
        """Send a Telegram notification for an auto-detected knowledge commit proposal.

        Args:
            proposal_id: The proposal ID from consensus_manager
            proposal: KnowledgeCommitProposal object (for showing entries/summary)
            chat_ids: Optional list of chat IDs to notify (defaults to all allowed_chat_ids)
        """
        if not self._enabled or not self._bot:
            return

        targets = [str(c) for c in chat_ids] if chat_ids else self.allowed_chat_ids

        try:
            if proposal:
                text, keyboard = self._build_proposal_message(proposal_id, proposal)
            else:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                text = "📚 *Knowledge Proposal*\n\nReview and vote:"
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"vote:{proposal_id}:approve"),
                    InlineKeyboardButton("🔄 Request Changes", callback_data=f"vote:{proposal_id}:request_changes"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"vote:{proposal_id}:reject"),
                ]])

            for chat_id in targets:
                if chat_id in self.allowed_chat_ids:
                    self._pending_proposals[proposal_id] = chat_id
                    try:
                        await self._bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode="MarkdownV2",
                            reply_markup=keyboard,
                        )
                    except Exception as e:
                        log.warning(f"Failed to notify chat {chat_id} of knowledge proposal: {e}")
        except Exception as e:
            log.error(f"notify_knowledge_proposal error: {e}", exc_info=True)

    async def notify_knowledge_result(
        self,
        proposal_id: str,
        status: str,
        topic: str,
        vote_comments: Optional[Dict[str, str]] = None,
        change_requests: Optional[list] = None,
    ) -> None:
        """Send the final voting result back to Telegram after all votes are in.

        Called by service.py callbacks (_on_commit_approved, _on_commit_rejected,
        _on_commit_revision_needed) so the user learns the actual outcome rather
        than just "vote recorded".

        Args:
            proposal_id: The proposal that was finalized
            status: "approved", "rejected", or "revision_needed"
            topic: Topic name for context
            vote_comments: Dict of node_id → comment from all voters
            change_requests: List of {node_id, comment} for revision_needed case
        """
        if not self._enabled or not self._bot:
            return

        chat_id = self._pending_proposals.pop(proposal_id, None)
        if not chat_id:
            return  # Not a Telegram-originated proposal

        try:
            topic_esc = escape_markdown(topic or "")
            if status == "approved":
                text = f"✅ *Knowledge Saved*\n\n*Topic:* {topic_esc}\n\nThe agent approved and the knowledge has been added to your personal context\\."
            elif status == "rejected":
                reasons = ""
                if vote_comments:
                    lines = [f"• {escape_markdown(c)}" for c in vote_comments.values() if c]
                    if lines:
                        reasons = "\n\n*Reasons:*\n" + "\n".join(lines)
                text = f"❌ *Knowledge Rejected*\n\n*Topic:* {topic_esc}{reasons}"
            elif status == "revision_needed":
                changes = ""
                if change_requests:
                    lines = [f"• {escape_markdown(cr.get('comment', ''))}" for cr in change_requests if cr.get('comment')]
                    if lines:
                        changes = "\n\n*Requested changes:*\n" + "\n".join(lines)
                text = f"🔄 *Revision Requested*\n\n*Topic:* {topic_esc}{changes}\n\nThe agent will revise and resubmit for voting\\."
            else:
                text = f"⏱ *Voting timed out*\n\n*Topic:* {topic_esc}"

            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="MarkdownV2",
            )
        except Exception as e:
            log.error(f"notify_knowledge_result error: {e}", exc_info=True)

    async def _handle_message(self, update, context):
        """Handle incoming text message."""
        chat_id = str(update.effective_chat.id)
        message_text = update.message.text

        # Check authorization — silent drop
        if chat_id not in self.allowed_chat_ids:
            log.warning(f"Unauthorized message from chat_id={chat_id}, silent drop")
            return

        # Check if we have a message handler
        if not self._message_handler:
            await update.message.reply_text("⚠️ Message handler not configured. Cannot process message.")
            return

        # Send "processing" indicator
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        # Build sender attribution for history (shown in DPC chat UI)
        tg_user = update.effective_user
        tg_display_name = (tg_user.first_name or tg_user.username or "Telegram User") if tg_user else "Telegram User"
        sender_name = f"{tg_display_name} (Telegram)"

        log.info(f"Processing Telegram message from chat {chat_id} ({sender_name}): {message_text[:50]}...")

        try:
            # Use agent_id as conversation_id when unified_conversation is enabled,
            # so Telegram messages share history with the DPC chat UI.
            conversation_id = self._agent_id if self._unified_conversation and self._agent_id else f"telegram-{chat_id}"

            # Pattern D: Check if message is @CC-only (no @Ark) — skip Ark processing,
            # save to history and broadcast cc_agent_mention for real CC to pick up
            import re
            _tg_mentions = {m.lower() for m in re.findall(r'@(\w+)\b', message_text or '', re.IGNORECASE)}
            if "cc" in _tg_mentions and "ark" not in _tg_mentions:
                log.info(f"@CC-only Telegram message in {conversation_id} — skipping Ark, broadcasting for CC")
                # Save to history so CC can see it
                if self._agent_manager:
                    monitor = self._agent_manager._get_or_create_agent_monitor(conversation_id)
                    from dpc_client_core.dpc_agent.utils import utc_now_iso
                    monitor.add_message(
                        role="user",
                        content=message_text,
                        timestamp=utc_now_iso(),
                        sender_node_id=getattr(self._agent_manager.service.p2p_manager, "node_id", "telegram"),
                        sender_name=sender_name,
                    )
                    monitor.save_history()
                    # Broadcast cc_agent_mention for CC's MCP bridge
                    service = getattr(self._agent_manager, 'service', None)
                    if service:
                        await service._check_agent_cc_mention(
                            conversation_id, message_text, "", self._agent_manager
                        )
                        # Push updated history to DPC UI
                        if self._unified_conversation:
                            await self._broadcast_history_to_ui(conversation_id)
                return  # Skip Ark's process_message

            # B1a: Save user message to monitor and broadcast to UI BEFORE agent
            # processing, so the message appears in DPC UI immediately (not after response).
            # B1b: Prefix sender_name so Ark sees message source (Telegram vs DPC UI).
            if self._agent_manager and self._unified_conversation:
                monitor = self._agent_manager._get_or_create_agent_monitor(conversation_id)
                from dpc_client_core.dpc_agent.utils import utc_now_iso
                monitor.add_message(
                    role="user",
                    content=message_text,
                    timestamp=utc_now_iso(),
                    sender_node_id=getattr(self._agent_manager.service.p2p_manager, "node_id", "telegram"),
                    sender_name=sender_name,
                )
                monitor.save_history()
                await self._broadcast_history_to_ui(conversation_id)
                skip_history = True
            else:
                skip_history = False

            # Call the message handler (agent_manager.process_message)
            response = await self._message_handler(
                message=message_text,
                conversation_id=conversation_id,
                include_context=True,
                sender_name=sender_name,
                telegram_chat_id=chat_id,  # Enables reply routing for scheduled tasks
                _skip_history=skip_history,
            )

            # Send response (escape for MarkdownV2, split if needed)
            try:
                if len(response) > TELEGRAM_MESSAGE_MAX_LENGTH:
                    # Split long messages
                    chunks = self._split_message(response, TELEGRAM_MESSAGE_MAX_LENGTH - 100)
                    for i, chunk in enumerate(chunks):
                        prefix = f"📄 *Part {i+1}/{len(chunks)}*\n\n" if len(chunks) > 1 else ""
                        await update.message.reply_text(prefix + escape_markdown(chunk), parse_mode="MarkdownV2")
                else:
                    await update.message.reply_text(escape_markdown(response), parse_mode="MarkdownV2")
            except Exception as send_err:
                log.warning(f"MarkdownV2 send failed, falling back to plain text: {send_err}")
                await update.message.reply_text(response[:TELEGRAM_MESSAGE_MAX_LENGTH])
            finally:
                # Always push updated history to DPC chat UI in unified_conversation mode
                if self._unified_conversation and self._agent_manager:
                    await self._broadcast_history_to_ui(conversation_id)

        except Exception as e:
            error_str = str(e)
            log.error(f"Error processing Telegram message: {e}", exc_info=True)

            # Check for context limit errors
            if "too large" in error_str.lower() or "context" in error_str.lower() and "token" in error_str.lower():
                await update.message.reply_text(
                    "⚠️ *Context Limit Reached*\n\n"
                    "The conversation has reached its token limit\\.\n\n"
                    "Use `/clear` to start a fresh conversation\\.",
                    parse_mode="MarkdownV2"
                )
            else:
                await update.message.reply_text(f"❌ Error processing message: {escape_markdown(error_str[:200])}", parse_mode="MarkdownV2")

    async def _handle_voice_message(self, update, context):
        """
        Handle incoming voice message from Telegram with transcription.

        Downloads the voice file, transcribes it using Whisper, and processes
        the transcribed text through the agent.

        Args:
            update: Telegram Update object
            context: Telegram Context object
        """
        chat_id = str(update.effective_chat.id)
        voice = update.message.voice

        # Check authorization — silent drop
        if chat_id not in self.allowed_chat_ids:
            log.warning(f"Unauthorized voice message from chat_id={chat_id}, silent drop")
            return

        # Check if we have a message handler
        if not self._message_handler:
            await update.message.reply_text("⚠️ Message handler not configured. Cannot process voice message.")
            return

        # Get voice metadata
        duration = voice.duration
        file_size = voice.file_size
        file_id = voice.file_id

        log.info(f"Processing voice message from chat {chat_id} (duration: {duration}s, size: {file_size} bytes)")

        # Send "recording audio" action
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_voice")

        try:
            # Download voice file
            voice_filename = f"agent_voice_{update.message.message_id}.ogg"
            # Use agent-specific storage instead of legacy ~/.dpc/agent/ path
            voice_dir = self._agent_manager.agent_root / "voice"
            voice_dir.mkdir(parents=True, exist_ok=True)
            voice_path = voice_dir / voice_filename

            # Download from Telegram
            from telegram import Bot
            file = await self._bot.get_file(file_id)
            await file.download_to_drive(voice_path)
            log.info(f"Downloaded voice file to {voice_path}")

            # Transcribe if enabled
            transcription_text = None

            if self.transcription_enabled:
                try:
                    # Get transcription service via agent_manager -> service
                    if self._agent_manager and hasattr(self._agent_manager, 'service'):
                        service = self._agent_manager.service

                        # Check if service has transcribe_audio method
                        if hasattr(service, 'transcribe_audio'):
                            # Read file and encode as base64
                            with open(voice_path, "rb") as f:
                                audio_data = f.read()
                                audio_base64 = base64.b64encode(audio_data).decode("utf-8")

                            # Transcribe using service method
                            transcription_result = await service.transcribe_audio(
                                audio_base64=audio_base64,
                                mime_type="audio/ogg"
                            )

                            transcription_text = transcription_result.get("text", "")
                            provider = transcription_result.get("provider", "unknown")

                            log.info(f"Transcribed voice message ({len(transcription_text)} chars, provider: {provider})")

                            # Send transcription back to user
                            if transcription_text:
                                await update.message.reply_text(
                                    f"📝 *Transcription:*\n{escape_markdown(transcription_text)}",
                                    parse_mode="MarkdownV2"
                                )
                            else:
                                await update.message.reply_text("⚠️ No speech detected in voice message.")
                                return
                        else:
                            log.warning("Service does not have transcribe_audio method")
                            await update.message.reply_text("⚠️ Transcription service not available.")
                            return
                    else:
                        log.warning("No access to service for transcription")
                        await update.message.reply_text("⚠️ Transcription service not available.")
                        return

                except Exception as e:
                    log.error(f"Failed to transcribe voice message: {e}", exc_info=True)
                    await update.message.reply_text(f"❌ Transcription failed: {str(e)[:100]}")
                    return
            else:
                await update.message.reply_text("⚠️ Voice transcription is disabled.")
                return

            # If we have transcription, process through agent
            if transcription_text:
                # Send "typing" action
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")

                # Build sender attribution for history
                tg_user = update.effective_user
                tg_display_name = (tg_user.first_name or tg_user.username or "Telegram User") if tg_user else "Telegram User"
                sender_name = f"{tg_display_name} (Telegram)"

                log.info(f"Processing transcribed voice message from chat {chat_id} ({sender_name}): {transcription_text[:50]}...")

                # Use agent_id as conversation_id when unified_conversation is enabled
                conversation_id = self._agent_id if self._unified_conversation and self._agent_id else f"telegram-{chat_id}"

                # Call the message handler (agent_manager.process_message)
                response = await self._message_handler(
                    message=transcription_text,
                    conversation_id=conversation_id,
                    include_context=True,
                    sender_name=sender_name,
                    telegram_chat_id=chat_id,  # Enables reply routing for scheduled tasks
                )

                # Send response (escape for MarkdownV2, split if needed)
                try:
                    if len(response) > TELEGRAM_MESSAGE_MAX_LENGTH:
                        # Split long messages
                        chunks = self._split_message(response, TELEGRAM_MESSAGE_MAX_LENGTH - 100)
                        for i, chunk in enumerate(chunks):
                            prefix = f"📄 *Part {i+1}/{len(chunks)}*\n\n" if len(chunks) > 1 else ""
                            await update.message.reply_text(prefix + escape_markdown(chunk), parse_mode="MarkdownV2")
                    else:
                        await update.message.reply_text(escape_markdown(response), parse_mode="MarkdownV2")
                except Exception as send_err:
                    log.warning(f"MarkdownV2 send failed, falling back to plain text: {send_err}")
                    await update.message.reply_text(response[:TELEGRAM_MESSAGE_MAX_LENGTH])
                finally:
                    # Always push updated history to DPC chat UI in unified_conversation mode
                    if self._unified_conversation and self._agent_manager:
                        await self._broadcast_history_to_ui(conversation_id)

            # Clean up voice file
            try:
                voice_path.unlink(missing_ok=True)
            except Exception:
                pass

        except Exception as e:
            log.error(f"Error processing voice message: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error processing voice message: {str(e)[:200]}")

    async def _handle_photo_message(self, update, context):
        """
        Handle incoming photo message from Telegram.

        Downloads the highest-resolution photo, encodes it as base64, and passes
        it to the agent via process_message(). The agent's llm_adapter will use
        native vision if the configured provider supports it, or pre-analyze the
        image with an auto-selected vision model and inject the description as text.
        """
        chat_id = str(update.effective_chat.id)

        # Check authorization — silent drop
        if chat_id not in self.allowed_chat_ids:
            log.warning(f"Unauthorized photo from chat_id={chat_id}, silent drop")
            return

        if not self._message_handler:
            await update.message.reply_text("⚠️ Message handler not configured. Cannot process photo.")
            return

        # Telegram sends multiple sizes; pick the largest (last in the list)
        photo = update.message.photo[-1]
        caption = update.message.caption or ""

        log.info(f"Processing photo from chat {chat_id} (file_id={photo.file_id}, caption={caption[:50]!r})")
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        try:
            # Download photo from Telegram
            file = await self._bot.get_file(photo.file_id)
            photo_bytes = await file.download_as_bytearray()
            image_base64 = base64.b64encode(bytes(photo_bytes)).decode("utf-8")

            # Build sender attribution
            tg_user = update.effective_user
            tg_display_name = (tg_user.first_name or tg_user.username or "Telegram User") if tg_user else "Telegram User"
            sender_name = f"{tg_display_name} (Telegram)"

            # Use caption as message text; fall back to a generic prompt
            message_text = caption if caption else "Please analyze this image."

            conversation_id = self._agent_id if self._unified_conversation and self._agent_id else f"telegram-{chat_id}"

            response = await self._message_handler(
                message=message_text,
                conversation_id=conversation_id,
                include_context=True,
                sender_name=sender_name,
                telegram_chat_id=chat_id,
                image_base64=image_base64,
                image_mime="image/jpeg",
                image_caption=caption or None,
            )

            # Send response
            try:
                if len(response) > TELEGRAM_MESSAGE_MAX_LENGTH:
                    chunks = self._split_message(response, TELEGRAM_MESSAGE_MAX_LENGTH - 100)
                    for i, chunk in enumerate(chunks):
                        prefix = f"📄 *Part {i+1}/{len(chunks)}*\n\n" if len(chunks) > 1 else ""
                        await update.message.reply_text(prefix + escape_markdown(chunk), parse_mode="MarkdownV2")
                else:
                    await update.message.reply_text(escape_markdown(response), parse_mode="MarkdownV2")
            except Exception as send_err:
                log.warning(f"MarkdownV2 send failed, falling back to plain text: {send_err}")
                await update.message.reply_text(response[:TELEGRAM_MESSAGE_MAX_LENGTH])
            finally:
                if self._unified_conversation and self._agent_manager:
                    await self._broadcast_history_to_ui(conversation_id)

        except Exception as e:
            log.error(f"Error processing photo message: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error processing photo: {str(e)[:200]}")

    async def _broadcast_history_to_ui(self, conversation_id: str) -> None:
        """
        Broadcast updated conversation history to the DPC chat UI via WebSocket.

        Called after processing a Telegram message in unified_conversation mode so
        the DPC chat panel reflects the Telegram exchange in real time.
        """
        try:
            service = getattr(self._agent_manager, 'service', None)
            if not service:
                return

            # Read history from agent's ConversationMonitor (the actual source of truth),
            # NOT from service.get_conversation_history() which looks up P2P conversations
            # and returns empty for agent conversations — causing UI to go blank (B1 fix).
            monitor = self._agent_manager._agent_monitors.get(conversation_id)
            messages = monitor.get_message_history() if monitor else []
            tokens_used = monitor.current_token_count if monitor else 0
            token_limit = monitor.token_limit if monitor else 0

            # Token warning — same threshold check as UI-triggered agent queries
            if monitor and token_limit > 0 and monitor.should_suggest_extraction():
                usage_percent = tokens_used / token_limit
                await service.local_api.broadcast_event("token_limit_warning", {
                    "conversation_id": conversation_id,
                    "tokens_used": tokens_used,
                    "token_limit": token_limit,
                    "usage_percent": usage_percent,
                    "history_tokens": tokens_used,  # for agent, current_token_count = history_tokens
                    "context_estimated": getattr(monitor, '_last_context_estimated', 0),
                })
                log.warning(f"[_broadcast_history_to_ui] Token Warning - {conversation_id}: "
                            f"{usage_percent * 100:.1f}% of context window used ({tokens_used}/{token_limit})")

            # Include thinking content from the last LLM response so the UI can show
            # the "Thoughts" collapsible (same as execute_ai_query direct path).
            # thinking lives in agent._last_usage["thinking"] after process() returns.
            thinking = None
            try:
                agent = self._agent_manager.agent  # property — may raise RuntimeError
                last_usage = getattr(agent, '_last_usage', {}) or {}
                thinking = last_usage.get('thinking')
            except Exception:
                pass  # thinking is optional; don't block the broadcast

            await service.local_api.broadcast_event("agent_history_updated", {
                "conversation_id": conversation_id,
                "messages": messages,
                "message_count": len(messages),
                "tokens_used": tokens_used,
                "token_limit": token_limit,
                "thinking": thinking,
                "context_estimated": getattr(monitor, '_last_context_estimated', 0),
            })
            log.debug(f"[_broadcast_history_to_ui] Pushed {len(messages)} messages for {conversation_id}")
        except Exception as e:
            log.warning(f"[_broadcast_history_to_ui] Failed to push history: {e}")

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
        log.debug(f"[handle_event] Received: {event.type.value}, enabled={self._enabled}, bot={self._bot is not None}")

        if not self._enabled or not self._bot:
            log.warning(f"[handle_event] Bridge not ready (enabled={self._enabled}, bot={self._bot is not None})")
            return False

        # Filter events
        if event.type.value not in self.event_filter:
            log.debug(f"Event filtered out: {event.type.value} not in {self.event_filter}")
            return False

        # Rate limit check
        if not self._check_rate_limit(event.type.value):
            log.warning(f"Rate limited event: {event.type.value}")
            return False

        log.debug(f"Sending Telegram notification for event: {event.type.value}")

        # Format message
        try:
            message = self._format_event(event)
        except Exception as e:
            log.error(f"[handle_event] Failed to format event: {e}", exc_info=True)
            return False

        if not message:
            log.warning(f"[handle_event] Empty message for event {event.type.value}")
            return False

        # Send to all allowed chats
        try:
            from telegram.error import NetworkError, TimedOut
            _network_exc = (NetworkError, TimedOut)
        except ImportError:
            _network_exc = (Exception,)  # type: ignore[assignment]

        success = True
        for chat_id in self.allowed_chat_ids:
            try:
                log.debug(f"[handle_event] Calling _send_message for chat_id={chat_id}")
                await self._send_message(chat_id, message)
                log.debug(f"[handle_event] _send_message completed for chat_id={chat_id}")
            except _network_exc:
                # Already logged as WARNING inside _send_single_message — no need to repeat
                success = False
            except Exception as e:
                log.error(f"[handle_event] Failed to send Telegram message to {chat_id}: {e}", exc_info=True)
                success = False

        log.debug(f"[handle_event] Completed with success={success}")
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

        Splits long messages into multiple parts instead of truncating.

        Args:
            chat_id: Target chat ID
            text: Message text (Markdown format)
        """
        # Split long messages instead of truncating
        if len(text) > TELEGRAM_MESSAGE_MAX_LENGTH:
            chunks = self._split_message(text, TELEGRAM_MESSAGE_MAX_LENGTH - 100)
            log.debug(f"[_send_message] Splitting message into {len(chunks)} parts")
            for i, chunk in enumerate(chunks):
                # Add part indicator for multi-part messages
                if len(chunks) > 1:
                    chunk = f"[{i+1}/{len(chunks)}]\n{chunk}"
                await self._send_single_message(chat_id, chunk)
                # Add small delay between chunks to avoid rate limiting
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.1)  # 100ms between chunks
            return

        await self._send_single_message(chat_id, text)

    async def _send_single_message(self, chat_id: str, text: str) -> None:
        """
        Send a single message to a Telegram chat (internal helper).

        Uses semaphore to limit concurrent API calls and prevent connection pool exhaustion.

        Args:
            chat_id: Target chat ID
            text: Message text (Markdown format)
        """
        async with self._send_semaphore:
            log.debug(f"[_send_message] Sending to chat_id={chat_id}, text_len={len(text)}")

            try:
                from telegram.error import NetworkError, TimedOut
            except ImportError:
                NetworkError = Exception  # type: ignore[misc,assignment]
                TimedOut = Exception      # type: ignore[misc,assignment]

            try:
                result = await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="MarkdownV2",
                    disable_notification=False,
                )
                log.debug(f"[_send_message] Success! message_id={result.message_id}")
                self._network_error_count = 0
                return result
            except (NetworkError, TimedOut) as e:
                # Network unavailable — skip Markdown fallback (same outcome), log at WARNING
                self._network_error_count += 1
                if self._network_error_count <= 3 or self._network_error_count % 50 == 0:
                    log.warning(
                        "[_send_message] Telegram unreachable (error #%d): %s",
                        self._network_error_count, e,
                    )
                raise
            except Exception as e:
                # Non-network failure (e.g. Markdown parse error) — try plain text fallback
                log.debug(f"[_send_message] Send failed ({type(e).__name__}), retrying without Markdown: {e}")
                try:
                    result = await self._bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=None,
                        disable_notification=False,
                    )
                    log.debug(f"[_send_message] Success without Markdown! message_id={result.message_id}")
                    self._network_error_count = 0
                    return result
                except (NetworkError, TimedOut) as e2:
                    self._network_error_count += 1
                    if self._network_error_count <= 3 or self._network_error_count % 50 == 0:
                        log.warning(
                            "[_send_message] Telegram unreachable on fallback (error #%d): %s",
                            self._network_error_count, e2,
                        )
                    raise
                except Exception as e2:
                    log.error(f"[_send_message] Failed even without Markdown: {e2}", exc_info=True)
                    raise

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

        # Event type as title (escape for Markdown)
        title = escape_markdown(event.type.value.replace("_", " ").title())

        lines = [
            f"{emoji} *{title}*",
            f"⏰ `{escape_markdown(timestamp)}`",
        ]

        # Add event-specific details
        data = event.data

        # Task events
        if "task_id" in data:
            lines.append(f"📋 Task: `{escape_markdown(str(data['task_id']))}`")
        if "task_type" in data:
            lines.append(f"📁 Type: {escape_markdown(str(data['task_type']))}")
        if "message_preview" in data:
            preview = escape_markdown(str(data["message_preview"])[:150])
            lines.append(f"💬 Preview: {preview}")
        if "conversation_id" in data:
            lines.append(f"🔗 Conv: `{escape_markdown(str(data['conversation_id'][:30]))}`")

        # Tool events
        if "tool" in data:
            lines.append(f"🔧 Tool: `{escape_markdown(str(data['tool']))}`")

        # Thought events
        if "thought_type" in data:
            lines.append(f"💭 Thought: {escape_markdown(str(data['thought_type']))}")
        if "thought_number" in data:
            lines.append(f"#️⃣ Number: {data['thought_number']}")

        # Evolution events
        if "cycle_id" in data:
            lines.append(f"🔄 Cycle: `{escape_markdown(str(data['cycle_id']))}`")
        if "cycle_number" in data:
            lines.append(f"#️⃣ Cycle #: {data['cycle_number']}")
        if "files_modified" in data:
            lines.append(f"📄 Files: {data['files_modified']}")
        if "changes_applied" in data:
            lines.append(f"✏️ Changes: {data['changes_applied']}")

        # Code modified
        if "path" in data:
            lines.append(f"📄 Path: `{escape_markdown(str(data['path']))}`")

        # Description/result - escape these as they contain free-form text
        if "description" in data:
            desc = escape_markdown(str(data["description"])[:500])  # Increased from 200
            lines.append(f"📝 {desc}")
        # Note: result is intentionally not included in the task_completed notification.
        # The full response is already sent directly via reply_text before this notification,
        # so including a truncated copy here would be confusing and redundant.

        # Error
        if "error" in data:
            error = escape_markdown(str(data["error"])[:500])  # Increased from 200
            lines.append(f"❌ Error: {error}")

        # Agent-initiated message (special formatting)
        if event.type == EventType.AGENT_MESSAGE:
            priority = data.get("priority", "normal")
            priority_emojis = {"urgent": "🔴", "high": "🟠", "normal": "🟡", "low": "🟢"}
            priority_emoji = priority_emojis.get(priority, "📍")

            # Rebuild lines for AGENT_MESSAGE with priority
            lines = [f"{priority_emoji} *Message from Agent* \\({priority}\\)"]

            if "message" in data:
                # Escape markdown for proper Telegram Markdown v2 escaping
                # No truncation - _send_message will split if needed
                msg = escape_markdown(str(data["message"]))
                lines.append(f"\n{msg}")

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
            "transcription_enabled": self.transcription_enabled,
            "unified_conversation": self._unified_conversation,
            "rate_limit": {
                "max_per_minute": self.rate_limit.max_events_per_minute,
                "cooldown_seconds": self.rate_limit.cooldown_seconds,
            },
        }


def create_telegram_bridge_callback(bridge: AgentTelegramBridge, agent_id: Optional[str] = None):
    """
    Create a callback function for AgentEventEmitter.

    Usage:
        bridge = AgentTelegramBridge(bot_token, chat_ids)
        await bridge.start()
        emitter.add_listener(create_telegram_bridge_callback(bridge, agent_id="agent_001"))

    Args:
        bridge: The AgentTelegramBridge instance
        agent_id: If set, only handle events whose conversation_id matches this agent.
                  Events without a conversation_id (e.g. evolution, lifecycle) are always handled.

    Returns:
        Callback function suitable for add_listener()
    """
    async def callback(event: AgentEvent) -> None:
        # Filter out events that belong to a different agent conversation
        if agent_id is not None:
            event_conv_id = event.data.get("conversation_id")
            event_agent_id = event.data.get("agent_id")
            if event_conv_id is not None and event_conv_id != agent_id:
                log.debug(
                    f"[TelegramBridge Callback] Skipping event {event.type.value} "
                    f"for conversation '{event_conv_id}' (bridge owns '{agent_id}')"
                )
                return
            if event_agent_id is not None and event_agent_id != agent_id:
                log.debug(
                    f"[TelegramBridge Callback] Skipping event {event.type.value} "
                    f"from agent '{event_agent_id}' (bridge owns '{agent_id}')"
                )
                return

        log.debug(f"[TelegramBridge Callback] Received event: {event.type.value}, bridge_enabled={bridge._enabled}")
        try:
            result = await bridge.handle_event(event)
            log.debug(f"[TelegramBridge Callback] Event handled, result={result}")
        except Exception as e:
            log.error(f"[TelegramBridge Callback] Error handling event: {e}", exc_info=True)

    callback.__name__ = "telegram_bridge_callback"
    return callback
