"""
TelegramDiscordService — the [telegram]/[discord] switches, tokens and limits.

Follows the BackgroundModelsService pattern (docs/decisions/001-service-split.md):
the Telegram and Discord readers are owned by TelegramService, DiscordService
and DiscordCoordinator, and the config-parity audit
(audit/config_ui_parity_2026-11-27.md) found no UI writer for any of these keys
(card THE-TELEGRAM-AND-DISCORD-SETTINGS-CAN-ONLY-BE-TYPED-INTO-CONFIG-INI).

Responsibilities:
- get/save of [telegram] enabled and bot_token
- get/save of [discord] enabled and bot_token, and of the live
  [discord.guardrails] and [discord.conversations] limits

NOT in scope (stays where it is):
- the readers themselves (telegram_service.py, discord_service.py,
  discord_coordinator.py)
- the dead [telegram] history keys (the dead-key card owns them)
- the other [telegram]/[discord] wiring keys (allowed_chat_ids, webhooks,
  guild/channel/agent/mirror ids, bot_token_env) — later cards

A stored token is never sent back as text: the getter answers `token_set`
only, the save reply says only that it saved, and the broadcast carries the
stored shape (token_set booleans and the non-secret values), never a value.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# What each section of the form may write. The dead [telegram] history keys
# (fetch_history_on_startup, history_*, drop_pending_updates) are knowingly
# absent: their getters have no caller (the audit's dead rows), and the
# dead-key card owns them.
_TELEGRAM_KEYS = frozenset({"enabled", "bot_token", "clear_bot_token"})
_DISCORD_KEYS = frozenset({
    "enabled", "bot_token", "clear_bot_token",
    "rate_limit_per_user_max", "rate_limit_per_user_window",
    "rate_limit_global_max", "rate_limit_global_window",
    "response_delay_seconds", "ttl_minutes", "max_messages",
})


class TelegramDiscordService:
    """
    The Telegram and Discord keys of config.ini the form covers.

    Dependencies injected at construction time (no EventBus):
    - settings: Settings — config read/write
    - local_api: LocalApiServer — UI event broadcasting
    """

    def __init__(self, settings: Any, local_api: Any):
        self.settings = settings
        self.local_api = local_api

    async def get_telegram_discord_config(self) -> Dict[str, Any]:
        """
        Get the switches, the token presence flags and the Discord limits.

        UI Integration: Called when user opens the Telegram and Discord form.
        A stored token is answered as `token_set` and nothing else — never its
        value, prefix or length; see the settings docstring for when a written
        value is in force (per key, and said so in the form).

        Returns:
            Dict with the Telegram and Discord form settings
        """
        try:
            config = self.settings.get_telegram_discord_settings()
            return {"status": "success", **config}
        except Exception as e:
            logger.error("Error getting Telegram/Discord config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def save_telegram_discord_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Save the switches, the tokens and the Discord limits.

        UI Integration: Called when user saves either section of the Telegram
        and Discord form. The config dict holds a `telegram` and/or a `discord`
        object whose keys are the config.ini keys; a key left out keeps its
        stored value, an empty `bot_token` keeps the stored token (only
        `clear_bot_token: true` removes it), and an invalid value is refused
        with its reason and nothing is written. The reply and the broadcast
        never carry a token value, only the `token_set` booleans of the stored
        shape — a token travels exactly once, in the save command itself.

        Args:
            config: Telegram and Discord configuration dict

        Returns:
            Dict with status and message
        """
        if not isinstance(config, dict):
            return {
                "status": "error",
                "message": "Telegram/Discord config must be an object with telegram and/or discord sections."
            }
        try:
            unknown = set(config) - {"telegram", "discord"} - {"status"}
            if unknown:
                return {
                    "status": "error",
                    "message": f"Unknown Telegram/Discord section(s): {', '.join(sorted(unknown))}"
                }
            for name, allowed in (("telegram", _TELEGRAM_KEYS), ("discord", _DISCORD_KEYS)):
                section = config.get(name)
                if section is None:
                    continue
                if not isinstance(section, dict):
                    return {
                        "status": "error",
                        "message": f"{name} must be an object of its config.ini keys."
                    }
                unknown_keys = set(section) - allowed
                if unknown_keys:
                    return {
                        "status": "error",
                        "message": f"Unknown {name} key(s): {', '.join(sorted(unknown_keys))}"
                    }
            self.settings.set_telegram_discord_settings(
                telegram=config.get("telegram"),
                discord=config.get("discord"),
            )
            stored = self.settings.get_telegram_discord_settings()
            await self.local_api.broadcast_event("telegram_discord_config_updated", stored)
            return {
                "status": "success",
                "message": "Telegram and Discord settings saved successfully"
            }
        except ValueError as e:
            return {
                "status": "error",
                "message": str(e)
            }
        except Exception as e:
            logger.error("Error saving Telegram/Discord config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }
