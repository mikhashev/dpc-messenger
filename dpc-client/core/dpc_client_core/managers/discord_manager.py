"""
Discord Bot Manager — Discord integration for DPC Messenger.

Mirrors TelegramBotManager pattern: bot lifecycle, whitelist, message sending.
Uses discord.py library for Gateway WebSocket connection.

Setup:
1. Create Application on discord.com/developers/applications
2. Create Bot, enable Message Content Intent
3. Generate invite URL with bot + applications.commands scopes
4. Configure in config.ini [discord] section
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Set

try:
    import discord
    from discord import Intents, Message
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

if TYPE_CHECKING:
    from ..service import CoreService

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_MAX_LENGTH = 2000


class DiscordBotManager:
    def __init__(self, service: "CoreService", config: Dict[str, Any]):
        self.service = service
        self.bot_token = config.get("bot_token", "")
        self.guild_id = config.get("guild_id", "")
        self.allowed_channel_ids: Set[str] = set(str(c) for c in config.get("allowed_channel_ids", []))
        self.ark_channel_id = config.get("ark_channel_id", "")
        self.morning_brief_channel_id = config.get("morning_brief_channel_id", "") or self.ark_channel_id
        self._client: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._on_message_callback: Optional[Callable] = None

    async def start(self) -> Optional[asyncio.Task]:
        if not HAS_DISCORD:
            logger.warning("discord.py not installed. Run: pip install discord.py")
            return None
        if not self.bot_token:
            logger.warning("Discord bot token not configured, skipping")
            return None

        intents = Intents.default()
        intents.message_content = True
        intents.guilds = True

        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_ready():
            logger.info("Discord bot connected as %s (guilds: %d)", self._client.user, len(self._client.guilds))
            self._running = True

        @self._client.event
        async def on_message(message: Message):
            if message.author == self._client.user:
                return
            if not self._is_allowed(message):
                return
            if self._on_message_callback:
                try:
                    await self._on_message_callback(message)
                except Exception:
                    logger.exception("Error in Discord message callback")

        self._task = asyncio.create_task(self._run_bot())
        return self._task

    async def _run_bot(self):
        try:
            await self._client.start(self.bot_token)
        except discord.LoginFailure:
            logger.error("Discord login failed — check bot token")
        except Exception:
            logger.exception("Discord bot error")
        finally:
            self._running = False

    async def stop(self):
        if self._client and not self._client.is_closed():
            await self._client.close()
        if self._task and not self._task.done():
            self._task.cancel()
        self._running = False
        logger.info("Discord bot stopped")

    def _is_allowed(self, message: Message) -> bool:
        if not self.allowed_channel_ids:
            return True
        return str(message.channel.id) in self.allowed_channel_ids

    def is_mention(self, message: Message) -> bool:
        if not self._client or not self._client.user:
            return False
        return self._client.user in message.mentions

    def set_on_message(self, callback: Callable):
        self._on_message_callback = callback

    async def send_message(self, channel_id: str, text: str) -> bool:
        if not self._client or not self._running:
            return False
        try:
            channel = self._client.get_channel(int(channel_id))
            if not channel:
                channel = await self._client.fetch_channel(int(channel_id))
            if not channel:
                logger.error("Discord channel %s not found", channel_id)
                return False
            for chunk in self._split_message(text):
                await channel.send(chunk)
            return True
        except Exception:
            logger.exception("Failed to send Discord message to %s", channel_id)
            return False

    async def create_thread_and_reply(self, message, text: str, thread_name: str = None) -> bool:
        """Create a thread from a Discord message and reply in it."""
        try:
            name = thread_name or text[:97] + "..." if len(text) > 100 else text[:100] or "Discussion"
            thread = await message.create_thread(name=name, auto_archive_duration=60)
            for chunk in self._split_message(text):
                await thread.send(chunk)
            return True
        except Exception:
            logger.exception("Failed to create thread")
            return False

    @staticmethod
    def _split_message(text: str) -> list[str]:
        if len(text) <= DISCORD_MESSAGE_MAX_LENGTH:
            return [text]
        chunks = []
        while text:
            if len(text) <= DISCORD_MESSAGE_MAX_LENGTH:
                chunks.append(text)
                break
            split_at = text.rfind('\n', 0, DISCORD_MESSAGE_MAX_LENGTH)
            if split_at == -1:
                split_at = DISCORD_MESSAGE_MAX_LENGTH
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip('\n')
        return chunks

    @property
    def is_running(self) -> bool:
        return self._running
