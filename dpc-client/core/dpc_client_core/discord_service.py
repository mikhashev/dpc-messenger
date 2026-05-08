"""
DiscordService — Discord bot integration lifecycle.

Mirrors TelegramService pattern. Manages DiscordBotManager lifecycle
and agent interaction via Discord channels.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class DiscordService:
    def __init__(self, core_service_ref, settings):
        self._core = core_service_ref
        self._settings = settings
        self.discord_manager = None
        self.coordinator = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> Optional[asyncio.Task]:
        config = self._settings.get_discord_config()
        if not config.get('enabled'):
            logger.info("Discord integration disabled")
            return None

        from .managers.discord_manager import DiscordBotManager
        from .coordinators.discord_coordinator import DiscordCoordinator
        self.discord_manager = DiscordBotManager(self._core, config)
        self.coordinator = DiscordCoordinator(self._core, self.discord_manager)
        self.discord_manager.set_on_message(self._handle_message)
        self._task = await self.discord_manager.start()
        return self._task

    async def stop(self):
        if self.discord_manager:
            await self.discord_manager.stop()

    async def _handle_message(self, message):
        if not self.discord_manager:
            return
        if self.discord_manager.is_mention(message) and self.coordinator:
            await self.coordinator.handle_mention(message)

    async def send_to_ark_channel(self, text: str) -> bool:
        if not self.discord_manager or not self.discord_manager.is_running:
            return False
        channel_id = self.discord_manager.ark_channel_id
        if not channel_id:
            return False
        return await self.discord_manager.send_message(channel_id, text)

    async def send_morning_brief(self, brief_text: str) -> bool:
        if not self.discord_manager or not self.discord_manager.is_running:
            return False
        channel_id = self.discord_manager.morning_brief_channel_id
        if not channel_id:
            return False
        return await self.discord_manager.send_message(channel_id, f"**Morning Brief**\n\n{brief_text}")
