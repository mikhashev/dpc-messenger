"""
DiscordCoordinator — bidirectional DPC ↔ Discord message routing.

Mirrors TelegramCoordinator pattern but simplified:
- Discord → Agent: @mention routes to configured agent
- Agent → Discord: agent responses forwarded to source channel
- Conversation linking: channel_id ↔ agent conversation_id

ADR-025 Task 004.
"""

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from fnmatch import fnmatch
from urllib.parse import urlparse
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from ..managers.discord_manager import DiscordBotManager
    from ..service import CoreService

logger = logging.getLogger(__name__)

DPC_HOME = Path.home() / ".dpc"


class DiscordCoordinator:
    def __init__(self, service: "CoreService", discord_manager: "DiscordBotManager"):
        self.service = service
        self.discord_manager = discord_manager
        self._channel_to_conversation: Dict[str, str] = {}
        self._conversation_to_channel: Dict[str, str] = {}
        self._user_conversations: Dict[str, str] = {}
        self._user_timestamps: Dict[str, List[float]] = defaultdict(list)
        self._global_timestamps: List[float] = []
        self._load_links()

    def _links_path(self) -> Path:
        return DPC_HOME / "discord_conversation_links.json"

    def _load_links(self):
        path = self._links_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._channel_to_conversation = data.get("channel_to_conversation", {})
                self._conversation_to_channel = data.get("conversation_to_channel", {})
                self._user_conversations = data.get("user_conversations", {})
                logger.info("Loaded %d Discord conversation links, %d per-user", len(self._channel_to_conversation), len(self._user_conversations))
            except Exception as e:
                logger.warning("Failed to load Discord links: %s", e)

    def _save_links(self):
        try:
            self._links_path().write_text(json.dumps({
                "channel_to_conversation": self._channel_to_conversation,
                "conversation_to_channel": self._conversation_to_channel,
                "user_conversations": self._user_conversations,
            }, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to save Discord links: %s", e)

    def link(self, channel_id: str, conversation_id: str):
        self._channel_to_conversation[channel_id] = conversation_id
        self._conversation_to_channel[conversation_id] = channel_id
        self._save_links()
        logger.info("Linked Discord channel %s ↔ conversation %s", channel_id, conversation_id)

    def get_conversation_id(self, channel_id: str) -> Optional[str]:
        return self._channel_to_conversation.get(channel_id)

    def get_user_conversation_id(self, discord_user_id: str) -> str:
        """Get or create a per-user conversation ID for Discord user."""
        if discord_user_id not in self._user_conversations:
            conv_id = f"discord-user-{discord_user_id}"
            self._user_conversations[discord_user_id] = conv_id
            self._save_links()
            logger.info("Created per-user conversation %s for Discord user %s", conv_id, discord_user_id)
        return self._user_conversations[discord_user_id]

    def get_channel_id(self, conversation_id: str) -> Optional[str]:
        return self._conversation_to_channel.get(conversation_id)

    def _get_agent_id(self) -> Optional[str]:
        settings = getattr(self.service, 'settings', None)
        if settings:
            agent_id = settings.get("discord", "agent_id", fallback=None)
            if agent_id:
                return agent_id
        agents_dir = DPC_HOME / "agents"
        if agents_dir.exists():
            dirs = sorted(agents_dir.iterdir())
            if dirs:
                return dirs[0].name
        return None

    def _get_rate_config(self) -> dict:
        settings = getattr(self.service, 'settings', None)
        if not settings:
            return {"per_user_max": 10, "per_user_window": 600, "global_max": 60, "global_window": 3600, "delay": 3}
        return {
            "per_user_max": int(settings.get("discord.guardrails", "rate_limit_per_user_max", fallback="10")),
            "per_user_window": int(settings.get("discord.guardrails", "rate_limit_per_user_window", fallback="600")),
            "global_max": int(settings.get("discord.guardrails", "rate_limit_global_max", fallback="60")),
            "global_window": int(settings.get("discord.guardrails", "rate_limit_global_window", fallback="3600")),
            "delay": int(settings.get("discord.guardrails", "response_delay_seconds", fallback="3")),
        }

    def _check_rate_limit(self, discord_user_id: str) -> Optional[str]:
        """Check per-user and global rate limits. Returns error message or None."""
        now = time.time()
        cfg = self._get_rate_config()

        self._user_timestamps[discord_user_id] = [
            t for t in self._user_timestamps[discord_user_id] if now - t < cfg["per_user_window"]
        ]
        if len(self._user_timestamps[discord_user_id]) >= cfg["per_user_max"]:
            wait = int(cfg["per_user_window"] - (now - self._user_timestamps[discord_user_id][0]))
            return f"Please wait ~{wait}s before sending another message."

        self._global_timestamps = [t for t in self._global_timestamps if now - t < cfg["global_window"]][-cfg["global_max"]:]
        if len(self._global_timestamps) >= cfg["global_max"]:
            return "The bot is busy right now. Please try again in a few minutes."

        self._user_timestamps[discord_user_id].append(now)
        self._global_timestamps.append(now)
        return None

    _URL_RE = re.compile(r'https?://[^\s<>\)]+')

    def _get_url_whitelist(self) -> list:
        settings = getattr(self.service, 'settings', None)
        if not settings:
            return []
        raw = settings.get("discord.guardrails", "url_whitelist", fallback="")
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _filter_urls(self, text: str) -> tuple:
        """Filter non-whitelisted URLs from user text.

        Returns (filtered_text, blocked_domains). Blocked URLs are replaced
        with [link removed]. Agent-initiated browsing is unaffected.
        """
        whitelist = self._get_url_whitelist()
        if not whitelist:
            return text, []

        blocked = []
        def _replace(match):
            url = match.group(0)
            parsed = urlparse(url)
            host_path = f"{parsed.netloc}{parsed.path}"
            for pattern in whitelist:
                if fnmatch(host_path, pattern) or fnmatch(parsed.netloc, pattern):
                    return url
            blocked.append(parsed.netloc)
            return "[link removed]"

        filtered = self._URL_RE.sub(_replace, text)
        return filtered, blocked

    async def handle_mention(self, message) -> None:
        text = message.content
        for mention in message.mentions:
            text = text.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
        text = text.strip()
        if not text:
            return

        channel_id = str(message.channel.id)
        sender = str(message.author.display_name or message.author.name)
        discord_user_id = str(message.author.id)

        rate_error = self._check_rate_limit(discord_user_id)
        if rate_error:
            await self.discord_manager.send_message(channel_id, rate_error)
            return

        text, blocked_domains = self._filter_urls(text)
        if blocked_domains:
            logger.info("URL whitelist blocked domains from %s: %s", sender, blocked_domains)

        agent_id = self._get_agent_id()
        if not agent_id:
            await self.discord_manager.send_message(channel_id, "No agents configured.")
            return

        provider = self._get_agent_provider()
        if not provider:
            await self.discord_manager.send_message(channel_id, "Agent provider not available.")
            return

        if agent_id not in provider._managers:
            await provider._ensure_manager(agent_id=agent_id)
        manager = provider._managers.get(agent_id)
        if not manager:
            await self.discord_manager.send_message(channel_id, "Agent not ready.")
            return

        discord_user_id = str(message.author.id)
        user_conv_id = self.get_user_conversation_id(discord_user_id)

        if not self.get_conversation_id(channel_id):
            self.link(channel_id, agent_id)

        try:
            response = await manager.process_message(
                message=f"[Discord — {sender}]: {text}",
                conversation_id=user_conv_id,
                sender_name=f"{sender} (Discord)",
                message_source="discord",
            )
            if response:
                delay = self._get_rate_config()["delay"]
                if delay > 0:
                    await asyncio.sleep(delay)
                sanitized = self._sanitize_output(response)
                await self.discord_manager.send_message(channel_id, sanitized)
                agent_name = getattr(manager, 'display_name', None) or agent_id
                await self._echo_response_to_mirror(response, agent_name)
        except Exception:
            logger.exception("Discord agent routing error")
            await self.discord_manager.send_message(channel_id, "Error processing request.")

    async def mirror_to_dpc_group(self, message, text_override: str = None, sender_override: str = None) -> None:
        """Mirror a Discord message to the linked DPC group chat."""
        channel_id = str(message.channel.id) if message else ""
        sender = sender_override or str(message.author.display_name or message.author.name)
        text = text_override or message.content
        if message and not text_override:
            for mention in getattr(message, 'mentions', []):
                name = mention.display_name or mention.name
                text = text.replace(f'<@{mention.id}>', f'@{name}').replace(f'<@!{mention.id}>', f'@{name}')

        settings = getattr(self.service, 'settings', None)
        mirror_group_id = settings.get("discord", "mirror_group_id", fallback=None) if settings else None
        if not mirror_group_id:
            return

        try:
            from ..conversation_monitor import Message as ConvMessage
            from datetime import datetime, timezone
            monitor = self.service._get_or_create_conversation_monitor(mirror_group_id)
            msg = ConvMessage(
                message_id=f"discord-{message.id}",
                conversation_id=mirror_group_id,
                sender_node_id=f"discord-{message.author.id}",
                sender_name=f"{sender} (Discord)",
                text=text,
                timestamp=datetime.now(timezone.utc).isoformat(),
                sender_type="human",
            )
            await monitor.on_message(msg)
            monitor.save_history()
            await self.service.local_api.broadcast_event("group_text_received", {
                "group_id": mirror_group_id,
                "text": text,
                "sender_name": f"{sender} (Discord)",
                "sender_type": "human",
                "sender_node_id": f"discord-{message.author.id}",
                "message_id": f"discord-{message.id}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            logger.debug("Mirrored Discord message from %s to DPC group %s", sender, mirror_group_id)
        except Exception as e:
            logger.warning("Failed to mirror Discord message: %s", e)

    async def _echo_response_to_mirror(self, text: str, agent_name: str) -> None:
        """Echo agent response to the DPC mirror group so the team sees both sides."""
        settings = getattr(self.service, 'settings', None)
        mirror_group_id = settings.get("discord", "mirror_group_id", fallback=None) if settings else None
        if not mirror_group_id:
            return
        try:
            await self.service.send_group_agent_message(mirror_group_id, agent_name, text)
        except Exception as e:
            logger.debug("Failed to echo agent response to mirror: %s", e)

    _SANITIZE_PATTERNS = [
        (re.compile(r'[A-Z]:\\Users\\[^\s\\]+\\[^\s]*', re.IGNORECASE), '[path]'),
        (re.compile(r'~\/\.dpc\/[^\s]*'), '[path]'),
        (re.compile(r'/home/[^\s/]+/[^\s]*'), '[path]'),
        (re.compile(r'dpc-node-[0-9a-f]{16,}'), '[node]'),
        (re.compile(r'agent_[0-9a-f]{8,}'), '[agent]'),
        (re.compile(r'agent_\d{3,}\b'), '[agent]'),
    ]

    def _sanitize_output(self, text: str) -> str:
        """Strip internal paths, node IDs, and agent identifiers before Discord send."""
        for pattern, replacement in self._SANITIZE_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    async def forward_to_discord(self, conversation_id: str, text: str, sender_name: str = "Agent") -> bool:
        channel_id = self.get_channel_id(conversation_id)
        if not channel_id:
            return False
        sanitized = self._sanitize_output(text)
        prefix = f"**{sender_name}:** " if sender_name and sender_name != "Agent" else ""
        return await self.discord_manager.send_message(channel_id, f"{prefix}{sanitized}")

    def _get_agent_provider(self):
        llm_manager = getattr(self.service, 'llm_manager', None)
        if not llm_manager:
            return None
        return llm_manager.providers.get("dpc_agent")
