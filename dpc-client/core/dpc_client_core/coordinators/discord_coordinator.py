"""
DiscordCoordinator — bidirectional DPC ↔ Discord message routing.

Mirrors TelegramCoordinator pattern but simplified:
- Discord → Agent: @mention routes to configured agent
- Agent → Discord: agent responses forwarded to source channel
- Conversation linking: channel_id ↔ agent conversation_id

ADR-025 Task 004.
"""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

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
                logger.info("Loaded %d Discord conversation links", len(self._channel_to_conversation))
            except Exception as e:
                logger.warning("Failed to load Discord links: %s", e)

    def _save_links(self):
        try:
            self._links_path().write_text(json.dumps({
                "channel_to_conversation": self._channel_to_conversation,
                "conversation_to_channel": self._conversation_to_channel,
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

    async def handle_mention(self, message) -> None:
        text = message.content
        for mention in message.mentions:
            text = text.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
        text = text.strip()
        if not text:
            return

        channel_id = str(message.channel.id)
        sender = str(message.author.display_name or message.author.name)

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

        if not self.get_conversation_id(channel_id):
            self.link(channel_id, agent_id)

        try:
            response = await manager.process_message(
                message=f"[Discord — {sender}]: {text}",
                conversation_id=agent_id,
                sender_name=f"{sender} (Discord)",
            )
            if response:
                await self.discord_manager.send_message(channel_id, response)
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

    async def forward_to_discord(self, conversation_id: str, text: str, sender_name: str = "Agent") -> bool:
        channel_id = self.get_channel_id(conversation_id)
        if not channel_id:
            return False
        prefix = f"**{sender_name}:** " if sender_name and sender_name != "Agent" else ""
        return await self.discord_manager.send_message(channel_id, f"{prefix}{text}")

    def _get_agent_provider(self):
        llm_manager = getattr(self.service, 'llm_manager', None)
        if not llm_manager:
            return None
        return llm_manager.providers.get("dpc_agent")
