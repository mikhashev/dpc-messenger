"""
PrivacyLoggingService — the [system] collection switches and the log levels.

Follows the BackgroundModelsService pattern (docs/decisions/001-service-split.md):
no older service owns these keys, and the config-parity audit
(audit/config_ui_parity_2026-11-27.md) found no UI writer for them (card
THE-PRIVACY-COLLECTION-AND-LOGGING-SETTINGS-HAVE-NO-CONTROL-IN-THE-UI).

Responsibilities:
- get/save of [system] auto_collect_device_info and collect_ai_models
- get/save of [logging] level and console_level, and of the [logging.modules]
  level overrides

NOT in scope (stays where it is):
- the readers themselves (service start and run_service.setup_logging)
- the dead [system] keys collect_hardware_specs and collect_dev_tools
  (the dead-key card owns them) and the non-level [logging] keys
  (console, file, max_bytes, backup_count — a later step of this card)
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class PrivacyLoggingService:
    """
    The device-information collection switches and the log levels of config.ini.

    Dependencies injected at construction time (no EventBus):
    - settings: Settings — config read/write
    - local_api: LocalApiServer — UI event broadcasting
    """

    def __init__(self, settings: Any, local_api: Any):
        self.settings = settings
        self.local_api = local_api

    async def get_privacy_logging_config(self) -> Dict[str, Any]:
        """
        Get the collection switches and the log levels.

        UI Integration: Called when user opens the privacy and logging form. The
        getter answers what the readers would resolve now; see the settings
        docstring for when a written value is in force (per key, and said so in
        the form).

        Returns:
            Dict with the collection switches and log levels
        """
        try:
            config = self.settings.get_privacy_logging_settings()
            return {"status": "success", **config}
        except Exception as e:
            logger.error("Error getting privacy and logging config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def save_privacy_logging_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Save the collection switches and the log levels.

        UI Integration: Called when user saves either section of the privacy and
        logging form. The config dict's keys are the config.ini keys; a key left
        out keeps its stored value. An invalid value is refused with its reason
        and nothing is written. Nothing here touches the running process: the
        switches are read at the next device-context collection and the levels
        are applied by the next backend start (run_service.setup_logging) — no
        live path exists, and the form says so per key.

        Args:
            config: Privacy and logging configuration dict

        Returns:
            Dict with status and message
        """
        known = {"auto_collect_device_info", "collect_ai_models", "level",
                 "console_level", "module_levels"}
        try:
            unknown = set(config) - known - {"status"}
            if unknown:
                return {
                    "status": "error",
                    "message": f"Unknown privacy/logging key(s): {', '.join(sorted(unknown))}"
                }
            self.settings.set_privacy_logging_settings(
                **{k: v for k, v in config.items() if k != "status"})
            stored = self.settings.get_privacy_logging_settings()
            await self.local_api.broadcast_event("privacy_logging_config_updated", stored)
            return {
                "status": "success",
                "message": "Privacy and logging settings saved successfully"
            }
        except ValueError as e:
            return {
                "status": "error",
                "message": str(e)
            }
        except Exception as e:
            logger.error("Error saving privacy and logging config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }
