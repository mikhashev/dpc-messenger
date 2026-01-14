"""Handler for TELEGRAM_MESSAGE command - Telegram bot integration (v0.14.0+)."""

from typing import Dict, Any, Optional
from . import MessageHandler


class TelegramIncomingHandler(MessageHandler):
    """
    Handles internal Telegram-related messages.

    Note: Most Telegram message handling is done directly by TelegramBridge
    via python-telegram-bot callbacks (handle_text_message, handle_voice_message).
    This handler is for any internal DPC messages related to Telegram status,
    configuration changes, or routing from other components.

    The TelegramBridge is called directly by the telegram library's update handlers,
    so this handler is mainly for completeness and future extensibility.
    """

    @property
    def command_name(self) -> str:
        return "TELEGRAM_MESSAGE"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """
        Handle TELEGRAM_MESSAGE message.

        Args:
            sender_node_id: Node ID (should be telegram-bridge or similar)
            payload: Message payload containing:
                - telegram_chat_id: Source Telegram chat
                - message_type: Type of message (text, voice, status, etc.)
                - data: Message data
        """
        message_type = payload.get("message_type", "unknown")
        telegram_chat_id = payload.get("telegram_chat_id")
        data = payload.get("data", {})

        self.logger.debug(
            f"TELEGRAM_MESSAGE from {sender_node_id} "
            f"(chat_id: {telegram_chat_id}, type: {message_type})"
        )

        # Most Telegram handling is done by TelegramBridge directly
        # This handler is for any internal routing or status updates

        if message_type == "status_update":
            # Handle status update (connected, disconnected, etc.)
            await self._handle_status_update(telegram_chat_id, data)
        elif message_type == "config_change":
            # Handle configuration change
            await self._handle_config_change(data)
        else:
            self.logger.debug(f"Unhandled Telegram message type: {message_type}")

        return None

    async def _handle_status_update(self, telegram_chat_id: str, data: Dict[str, Any]):
        """Handle Telegram bot status update."""
        status = data.get("status")  # connected, disconnected, error
        self.logger.info(f"Telegram status update for chat {telegram_chat_id}: {status}")

        # Could trigger UI notifications or other actions here
        # Status is already broadcast by TelegramBotManager

    async def _handle_config_change(self, data: Dict[str, Any]):
        """Handle Telegram configuration change."""
        self.logger.info(f"Telegram config change: {data}")
        # Could trigger config reload here
