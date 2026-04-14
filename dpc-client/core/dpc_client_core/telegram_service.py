"""
TelegramService — Telegram bot integration lifecycle and messaging glue.

Extracted from service.py as part of the Grand Refactoring (Phase 1c).
See docs/decisions/001-service-split.md for rationale.

Responsibilities:
- TelegramBotManager and TelegramBridge initialization and lifecycle
- DPC ↔ Telegram message forwarding
- Conversation linking/unlinking
- Agent-Telegram bridge management
- Config migration helpers

NOT in scope:
- TelegramBotManager internals (managers/telegram_manager.py — stays as-is)
- TelegramBridge internals (coordinators/telegram_coordinator.py — stays as-is)
- Group chat bridging fix: documented below but deferred to Phase 2 (requires
  group chat infrastructure not yet available).

Group Bridging Limitation (telegram_coordinator.py:1056-1082):
  Current: sends N separate 1:1 messages to each peer (O(n) network cost).
  Future: single DPC group message when Phase 2 multiparty chat is available.
  Tracked in audit/issues — do NOT fix without Phase 2 group infrastructure.

Note: TelegramBotManager and TelegramBridge receive core_service_ref because
they were designed to call back into CoreService. Refactoring those managers
is out of scope for Phase 1c; they are left as-is.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DPC_HOME_DIR = Path.home() / ".dpc"


class TelegramService:
    """Manages Telegram bot integration lifecycle and messaging.

    Owns:
    - telegram_manager (TelegramBotManager) — bot lifecycle, whitelist, sending
    - telegram_bridge (TelegramBridge) — message translation and routing

    Injected:
    - core_service_ref — forwarded to TelegramBotManager/TelegramBridge constructors
      (they were designed to call back into CoreService directly)
    - llm_manager — for agent manager access
    - p2p_manager — for display name
    - settings — for config reads
    - dpc_home_dir — for file paths
    """

    def __init__(
        self,
        core_service_ref,
        settings,
        llm_manager,
        p2p_manager,
        dpc_home_dir: Path,
    ):
        self.llm_manager = llm_manager
        self.p2p_manager = p2p_manager
        self.settings = settings
        self.dpc_home_dir = dpc_home_dir

        self.telegram_manager = None
        self.telegram_bridge = None
        self._telegram_task: Optional[asyncio.Task] = None

        telegram_config = settings.get_telegram_config()
        if telegram_config.get('enabled', False):
            try:
                from .managers.telegram_manager import TelegramBotManager
                from .coordinators.telegram_coordinator import TelegramBridge

                self.telegram_manager = TelegramBotManager(core_service_ref, telegram_config)
                self.telegram_bridge = TelegramBridge(core_service_ref, self.telegram_manager)

                logger.info(
                    "Telegram bot integration initialized (whitelist: %d chat_ids)",
                    len(telegram_config['allowed_chat_ids']),
                )
            except ImportError as e:
                logger.warning(
                    "Failed to import telegram library: %s. Install with: poetry install", e
                )
            except Exception as e:
                logger.error("Failed to initialize Telegram integration: %s", e, exc_info=True)

    # ─────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────

    async def start(self) -> Optional[asyncio.Task]:
        """Start Telegram bot manager and agent bridges.

        Returns the background task (so CoreService can add it to _background_tasks),
        or None if Telegram is not enabled.
        """
        if not self.telegram_manager:
            return None

        logger.info("Starting Telegram bot integration...")

        async def _start_with_error_handling():
            try:
                await self.telegram_manager.start()
            except Exception as e:
                logger.error("Telegram bot task failed: %s", e, exc_info=True)

        self._telegram_task = asyncio.create_task(_start_with_error_handling())
        self._telegram_task.set_name("telegram_bot")

        await self._start_agent_telegram_bridges()

        return self._telegram_task

    async def stop(self) -> None:
        """Stop Telegram bot manager."""
        if self.telegram_manager:
            logger.info("Stopping Telegram bot...")
            await self.telegram_manager.stop()

    def get_state(self) -> dict:
        """Agent-readable snapshot of Telegram service state."""
        if not self.telegram_manager:
            return {"enabled": False}
        return {
            "enabled": True,
            "connected": getattr(self.telegram_manager, '_running', False),
            "bridge_active": self.telegram_bridge is not None,
            "conversation_links": (
                len(self.telegram_bridge.conversation_map)
                if self.telegram_bridge else 0
            ),
        }

    # ─────────────────────────────────────────────────────────────
    # DPC → Telegram messaging
    # ─────────────────────────────────────────────────────────────

    async def send_to_telegram(
        self,
        conversation_id: str,
        text: str,
        attachments: Optional[List[Dict]] = None,
        voice_audio_base64: Optional[str] = None,
        voice_duration_seconds: Optional[float] = None,
        voice_mime_type: Optional[str] = None,
        file_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send message from DPC to Telegram chat."""
        try:
            if not self.telegram_bridge:
                return {"status": "error", "message": "Telegram integration not enabled"}

            # Handle file path from UI (send file/image directly to Telegram)
            if file_path:
                import mimetypes
                path = Path(file_path)
                if not path.exists():
                    return {"status": "error", "message": f"File not found: {file_path}"}

                telegram_chat_id = self.telegram_bridge._get_linked_chat_id(conversation_id)
                if not telegram_chat_id:
                    return {
                        "status": "error",
                        "message": "No linked Telegram chat for this conversation",
                    }

                mime_type, _ = mimetypes.guess_type(str(path))
                if mime_type and mime_type.startswith("image/"):
                    success = await self.telegram_manager.send_photo(
                        telegram_chat_id, path, caption=text if text else None
                    )
                else:
                    success = await self.telegram_manager.send_document(
                        telegram_chat_id, path, caption=text if text else None
                    )

                if not success:
                    return {"status": "error", "message": "Failed to send file to Telegram"}
                return {"status": "success", "message": "File sent to Telegram"}

            # Handle voice data from UI
            if voice_audio_base64:
                import base64
                from datetime import datetime, timezone

                try:
                    audio_data = base64.b64decode(voice_audio_base64)
                except Exception as e:
                    return {"status": "error", "message": f"Failed to decode voice audio: {e}"}

                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                extension = (voice_mime_type or "audio/webm").split("/")[-1].split(";")[0].strip()
                filename = f"voice_{timestamp}.{extension}"

                voice_dir = self.dpc_home_dir / "conversations" / conversation_id / "files"
                voice_dir.mkdir(parents=True, exist_ok=True)
                voice_path = voice_dir / filename

                if voice_path.exists():
                    stem = voice_path.stem
                    suffix = voice_path.suffix
                    counter = 1
                    while voice_path.exists():
                        voice_path = voice_dir / f"{stem}_{counter}{suffix}"
                        counter += 1

                try:
                    with open(voice_path, "wb") as f:
                        f.write(audio_data)
                    logger.info(
                        "Saved Telegram voice message: %s (%d bytes, %ss)",
                        voice_path, len(audio_data), voice_duration_seconds,
                    )
                except Exception as e:
                    return {"status": "error", "message": f"Failed to save voice file: {e}"}

                voice_attachment = {
                    "type": "voice",
                    "filename": filename,
                    "file_path": str(voice_path),
                    "size_bytes": len(audio_data),
                    "voice_metadata": {
                        "duration_seconds": voice_duration_seconds,
                        "sample_rate": int(
                            self.settings.get("voice_messages", "default_sample_rate", "48000")
                        ),
                        "channels": int(
                            self.settings.get("voice_messages", "default_channels", "1")
                        ),
                        "codec": self.settings.get(
                            "voice_messages", "default_codec", "opus"
                        ),
                        "recorded_at": __import__('datetime').datetime.now(
                            __import__('datetime').timezone.utc
                        ).isoformat(),
                    },
                }

                if not attachments:
                    attachments = []
                attachments.append(voice_attachment)

            sender_name = self.p2p_manager.get_display_name() or "You"
            await self.telegram_bridge.forward_dpc_to_telegram(
                conversation_id=conversation_id,
                sender_name=sender_name,
                text=text,
                attachments=attachments or [],
            )
            return {"status": "success", "message": "Sent to Telegram"}

        except Exception as e:
            logger.error("Failed to send to Telegram: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    # ─────────────────────────────────────────────────────────────
    # Conversation linking
    # ─────────────────────────────────────────────────────────────

    async def link_telegram_chat(
        self, conversation_id: str, telegram_chat_id: str
    ) -> Dict[str, Any]:
        """Link a DPC conversation to a Telegram chat."""
        try:
            if not self.telegram_bridge:
                return {"status": "error", "message": "Telegram integration not enabled"}

            # Validate chat_id is in whitelist
            if str(telegram_chat_id) not in [str(c) for c in self.telegram_manager.allowed_chat_ids]:
                return {
                    "status": "error",
                    "message": f"Chat ID {telegram_chat_id} is not in the whitelist. "
                               f"Add it to allowed_chat_ids in config.ini",
                }

            self.telegram_bridge.link_conversation(telegram_chat_id, conversation_id)
            logger.info("Linked Telegram chat %s to DPC conversation %s", telegram_chat_id, conversation_id)
            return {
                "status": "success",
                "message": f"Linked Telegram chat {telegram_chat_id} to {conversation_id}",
            }
        except Exception as e:
            logger.error("Failed to link Telegram chat: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def get_telegram_status(self) -> Dict[str, Any]:
        """Get Telegram bot integration status."""
        try:
            if not self.telegram_manager:
                return {
                    "status": "success",
                    "enabled": False,
                    "connected": False,
                    "message": "Telegram integration not enabled",
                }

            conversation_links = {}
            if self.telegram_bridge:
                for telegram_chat_id, conversation_id in self.telegram_bridge.conversation_map.items():
                    conversation_links[conversation_id] = telegram_chat_id

            return {
                "status": "success",
                "enabled": True,
                "connected": self.telegram_manager._running,
                "webhook_mode": self.telegram_manager.use_webhook,
                "whitelist_count": len(self.telegram_manager.allowed_chat_ids),
                "transcription_enabled": self.telegram_manager.transcription_enabled,
                "bridge_to_p2p": self.telegram_manager.bridge_to_p2p,
                "conversation_links_count": (
                    len(self.telegram_bridge.conversation_map) if self.telegram_bridge else 0
                ),
                "conversation_links": conversation_links,
            }
        except Exception as e:
            logger.error("Failed to get Telegram status: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def delete_telegram_conversation_link(self, conversation_id: str) -> Dict[str, Any]:
        """Delete a Telegram conversation link and all associated data from the backend."""
        try:
            if not self.telegram_bridge:
                return {"status": "error", "message": "Telegram bridge not initialized"}

            if not conversation_id.startswith("telegram-"):
                return {
                    "status": "error",
                    "message": f"Invalid Telegram conversation ID format: {conversation_id}",
                }

            telegram_chat_id = conversation_id.replace("telegram-", "")
            removed = self.telegram_bridge.remove_conversation_link(telegram_chat_id)

            import shutil
            conversation_folder = Path.home() / ".dpc" / "conversations" / conversation_id
            if conversation_folder.exists():
                shutil.rmtree(conversation_folder)
                logger.info("Deleted conversation folder: %s", conversation_folder)
            else:
                logger.debug("Conversation folder not found (already clean): %s", conversation_folder)

            history_file = Path.home() / ".dpc" / "groups" / f"{conversation_id}_history.json"
            if history_file.exists():
                history_file.unlink()
                logger.info("Deleted conversation history file: %s", history_file)

            if removed:
                logger.info("Deleted Telegram conversation link: %s", conversation_id)
                return {"status": "success", "message": f"Conversation {conversation_id} deleted"}
            else:
                return {
                    "status": "error",
                    "message": f"Conversation link not found: {conversation_id}",
                }
        except Exception as e:
            logger.error("Failed to delete Telegram conversation link: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    # ─────────────────────────────────────────────────────────────
    # Agent-Telegram bridge management
    # ─────────────────────────────────────────────────────────────

    async def link_agent_telegram(
        self,
        agent_id: str,
        bot_token: str,
        chat_ids: List[str],
        event_filter: Optional[List[str]] = None,
        max_events_per_minute: int = 20,
        cooldown_seconds: float = 3.0,
        transcription_enabled: bool = True,
        unified_conversation: bool = False,
    ) -> Dict[str, Any]:
        """Link an agent to Telegram with full configuration."""
        try:
            from .dpc_agent.utils import AgentRegistry

            registry = AgentRegistry()
            agent = registry.get_agent(agent_id)
            if not agent:
                return {"status": "error", "message": f"Agent not found: {agent_id}"}

            try:
                registry.link_agent_to_telegram(
                    agent_id=agent_id,
                    bot_token=bot_token,
                    chat_ids=chat_ids,
                    event_filter=event_filter,
                    max_events_per_minute=max_events_per_minute,
                    cooldown_seconds=cooldown_seconds,
                    transcription_enabled=transcription_enabled,
                    unified_conversation=unified_conversation,
                )
            except ValueError as e:
                return {"status": "error", "message": str(e)}

            dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
            if dpc_agent_provider:
                if hasattr(dpc_agent_provider, '_managers') and agent_id in dpc_agent_provider._managers:
                    await self._restart_agent_telegram_bridge(agent_id)
                else:
                    try:
                        await dpc_agent_provider._ensure_manager(agent_id=agent_id)
                        logger.info("Started agent manager and Telegram bridge for %s", agent_id)
                    except Exception as e:
                        logger.warning("Could not start agent manager for %s: %s", agent_id, e)

            return {
                "status": "success",
                "message": f"Agent {agent_id} linked to Telegram successfully",
                "agent_id": agent_id,
                "chat_ids": chat_ids,
            }
        except Exception as e:
            logger.error("Failed to link agent to Telegram: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def _start_agent_telegram_bridges(self) -> None:
        """Auto-start agent Telegram bridges on service startup."""
        try:
            from .dpc_agent.utils import AgentRegistry

            registry = AgentRegistry()
            agents = registry.list_agents()

            dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
            if not dpc_agent_provider:
                return

            for agent in agents:
                agent_id = agent.get("agent_id", "")
                if not agent_id or not agent.get("telegram_enabled", False):
                    continue
                try:
                    await dpc_agent_provider._ensure_manager(agent_id=agent_id)
                    logger.info("Auto-started Telegram bridge for agent %s on service startup", agent_id)
                except Exception as e:
                    logger.warning("Failed to auto-start Telegram bridge for agent %s: %s", agent_id, e)
        except Exception as e:
            logger.warning("Failed to auto-start agent Telegram bridges: %s", e)

    def _get_telegram_bridge_for_conversation(self, conversation_id: str):
        """Find the AgentTelegramBridge associated with a conversation, if any."""
        try:
            dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
            if not dpc_agent_provider or not hasattr(dpc_agent_provider, '_managers'):
                return None

            managers = dpc_agent_provider._managers

            if conversation_id in managers:
                mgr = managers[conversation_id]
                bridge = getattr(mgr, '_telegram_bridge', None)
                if bridge and bridge.is_enabled():
                    return bridge

            if conversation_id.startswith("telegram-"):
                chat_id = conversation_id[len("telegram-"):]
                for mgr in managers.values():
                    bridge = getattr(mgr, '_telegram_bridge', None)
                    if bridge and bridge.is_enabled() and chat_id in bridge.allowed_chat_ids:
                        return bridge
        except Exception as e:
            logger.debug("_get_telegram_bridge_for_conversation: %s", e)
        return None

    async def _restart_agent_telegram_bridge(self, agent_id: str) -> None:
        """Restart Telegram bridge for a running agent with new configuration."""
        dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
        if not dpc_agent_provider or not hasattr(dpc_agent_provider, '_managers'):
            return
        if agent_id not in dpc_agent_provider._managers:
            return

        agent_manager = dpc_agent_provider._managers[agent_id]

        if hasattr(agent_manager, "_telegram_bridge") and agent_manager._telegram_bridge:
            try:
                await agent_manager._telegram_bridge.stop()
                logger.info("Stopped Telegram bridge for agent %s", agent_id)
            except Exception as e:
                logger.error("Error stopping Telegram bridge for agent %s: %s", agent_id, e, exc_info=True)

        try:
            await agent_manager._start_telegram_bridge()
            logger.info("Restarted Telegram bridge for agent %s", agent_id)
        except Exception as e:
            logger.error("Error restarting Telegram bridge for agent %s: %s", agent_id, e, exc_info=True)

    async def unlink_agent_telegram(self, agent_id: str) -> Dict[str, Any]:
        """Unlink an agent from Telegram (removes all Telegram configuration)."""
        try:
            from .dpc_agent.utils import AgentRegistry

            registry = AgentRegistry()
            agent = registry.get_agent(agent_id)
            if not agent:
                return {"status": "error", "message": f"Agent not found: {agent_id}"}

            registry.unlink_agent_from_telegram(agent_id)

            dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
            if (
                dpc_agent_provider
                and hasattr(dpc_agent_provider, '_managers')
                and agent_id in dpc_agent_provider._managers
            ):
                agent_manager = dpc_agent_provider._managers[agent_id]
                if hasattr(agent_manager, "_telegram_bridge") and agent_manager._telegram_bridge:
                    try:
                        await agent_manager._telegram_bridge.stop()
                        agent_manager._telegram_bridge = None
                        logger.info("Stopped Telegram bridge for agent %s", agent_id)
                    except Exception as e:
                        logger.error(
                            "Error stopping Telegram bridge for agent %s: %s", agent_id, e, exc_info=True
                        )

            return {
                "status": "success",
                "message": f"Agent {agent_id} unlinked from Telegram",
            }
        except Exception as e:
            logger.error("Failed to unlink agent from Telegram: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def migrate_telegram_config(self) -> Dict[str, Any]:
        """Migrate global [dpc_agent_telegram] config to per-agent config."""
        try:
            from .dpc_agent.utils import migrate_global_telegram_to_agents

            result = migrate_global_telegram_to_agents()

            if result.get("status") == "success" and result.get("migrated_count", 0) > 0:
                dpc_agent_provider = self.llm_manager.providers.get("dpc_agent")
                for agent_id in result.get("migrated_agents", []):
                    if (
                        dpc_agent_provider
                        and hasattr(dpc_agent_provider, '_managers')
                        and agent_id in dpc_agent_provider._managers
                    ):
                        await self._restart_agent_telegram_bridge(agent_id)

            return result
        except Exception as e:
            logger.error("Failed to migrate Telegram config: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def get_agent_telegram_status(self, agent_id: str) -> Dict[str, Any]:
        """Get Telegram link status for an agent."""
        try:
            from .dpc_agent.utils import AgentRegistry

            registry = AgentRegistry()
            agent = registry.get_agent(agent_id)

            if not agent:
                return {"status": "error", "message": f"Agent {agent_id} not found"}

            linked_chat_id = registry.get_agent_linked_chat(agent_id)

            return {
                "status": "success",
                "agent_id": agent_id,
                "telegram_enabled": agent.get("telegram_enabled", False),
                "telegram_chat_id": linked_chat_id,
                "telegram_linked_at": agent.get("telegram_linked_at"),
            }
        except Exception as e:
            logger.error("Failed to get agent Telegram status: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}
