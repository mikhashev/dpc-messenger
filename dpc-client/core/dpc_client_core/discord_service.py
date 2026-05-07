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
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> Optional[asyncio.Task]:
        config = self._settings.get_discord_config()
        if not config.get('enabled'):
            logger.info("Discord integration disabled")
            return None

        from .managers.discord_manager import DiscordBotManager
        self.discord_manager = DiscordBotManager(self._core, config)
        self.discord_manager.set_on_message(self._handle_message)
        self._task = await self.discord_manager.start()
        return self._task

    async def stop(self):
        if self.discord_manager:
            await self.discord_manager.stop()

    async def _handle_message(self, message):
        if not self.discord_manager:
            return
        if self.discord_manager.is_mention(message):
            text = message.content
            for mention in message.mentions:
                text = text.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
            text = text.strip()
            if not text:
                return
            await self._route_to_agent(text, message)

    def _get_agent_provider(self):
        llm_manager = getattr(self._core, 'llm_manager', None)
        if not llm_manager:
            return None
        return llm_manager.providers.get("dpc_agent")

    async def _route_to_agent(self, text: str, message):
        try:
            provider = self._get_agent_provider()
            if not provider or not hasattr(provider, '_managers'):
                await self.discord_manager.send_message(
                    str(message.channel.id), "Agent not available."
                )
                return
            agent_id = self._settings.get("discord", "agent_id", fallback=None)
            if not agent_id:
                from pathlib import Path
                dpc_home = Path.home() / ".dpc"
                agent_dirs = sorted((dpc_home / "agents").iterdir()) if (dpc_home / "agents").exists() else []
                agent_id = agent_dirs[0].name if agent_dirs else None
            if not agent_id:
                await self.discord_manager.send_message(str(message.channel.id), "No agents configured.")
                return
            if agent_id not in provider._managers:
                await provider._ensure_manager(agent_id=agent_id)
            manager = provider._managers.get(agent_id)
            if not manager:
                await self.discord_manager.send_message(str(message.channel.id), "Agent manager not ready.")
                return
            result = await manager.process_message(agent_id, text)
            response = result.get('response', '') if isinstance(result, dict) else str(result)
            if response:
                await self.discord_manager.send_message(str(message.channel.id), response)
        except Exception:
            logger.exception("Discord agent routing error")
            await self.discord_manager.send_message(
                str(message.channel.id), "Error processing request."
            )

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
