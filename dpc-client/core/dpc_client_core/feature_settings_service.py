"""
FeatureSettingsService — the config-parity feature keys that belong to their feature
views, not to a settings form (card
THE-MUSIC-FILE-TRANSFER-VISION-AND-KNOWLEDGE-KEYS-HAVE-NO-CONTROL-IN-THE-UI, order 7):
the image size cap where images are sent, the knowledge extraction keys at the
Extract Knowledge action, the synced-history gate at the conversation view, and the
HuggingFace offline mode beside the background-model keys.

Follows the BackgroundModelsService pattern (docs/decisions/001-service-split.md): each
pair of keys is small, no older service owns the writer side, and the readers stay where
they are. Kept out of NetworkService on purpose so the two cards' commits split cleanly.

Responsibilities:
- get/save of [vision] max_image_size_mb (the composer's image cap)
- get/save of [knowledge] cultural_perspectives_enabled and cold_fallback_provider
  (the Extract Knowledge action)
- get/save of [history] reject_unsigned (the synced-record gate)
- get/save of [hf] offline_mode (three states: auto / on / off)

NOT in scope (stays where it is):
- the readers themselves (the P2P image doors, conversation_monitor, run_service's own
  configparser at process start)
- the [file_transfer] keys (the Network form's card owns them)
- get_hf_offline_mode in settings.py (no caller; this service mirrors run_service's
  parse instead, as the card directs)
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class FeatureSettingsService:
    """
    The feature keys of config.ini that their own views write.

    Dependencies injected at construction time (no EventBus):
    - settings: Settings — config read/write
    - local_api: LocalApiServer — UI event broadcasting
    - llm_manager: the provider registry, for refusing a cold_fallback_provider alias
      no loaded provider answers to (knowledge_routing.cold_fallback raises at
      extraction time; the writer stops the typo at save)
    """

    def __init__(self, settings: Any, local_api: Any, llm_manager: Any = None):
        self.settings = settings
        self.local_api = local_api
        self.llm_manager = llm_manager

    async def get_vision_config(self) -> Dict[str, Any]:
        """The composer's image cap (`[vision] max_image_size_mb`)."""
        try:
            return {"status": "success", **self.settings.get_vision_settings()}
        except Exception as e:
            logger.error("Error getting vision config: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def save_vision_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Save the composer's image cap. A key left out keeps its stored value; an
        invalid one is refused with its reason and nothing is written."""
        if not isinstance(config, dict):
            return {"status": "error", "message": "Vision config must be an object of its config.ini keys."}
        try:
            unknown = set(config) - {"max_image_size_mb", "status"}
            if unknown:
                return {"status": "error",
                        "message": f"Unknown vision key(s): {', '.join(sorted(unknown))}"}
            self.settings.set_vision_settings(
                **{k: v for k, v in config.items() if k != "status"})
            stored = self.settings.get_vision_settings()
            await self.local_api.broadcast_event("vision_config_updated", stored)
            return {"status": "success", "message": "Image size setting saved successfully", **stored}
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            logger.error("Error saving vision config: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def get_knowledge_extraction_config(self) -> Dict[str, Any]:
        """The two `[knowledge]` keys of the Extract Knowledge action."""
        try:
            return {"status": "success", **self.settings.get_knowledge_extraction_settings()}
        except Exception as e:
            logger.error("Error getting knowledge extraction config: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def save_knowledge_extraction_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Save the two `[knowledge]` keys. A key left out keeps its stored value; a
        `cold_fallback_provider` no loaded provider answers to is refused here — the
        reader (knowledge_routing.cold_fallback) refuses it at extraction time with
        NoKnowledgeProvider, so the form stops the typo at save."""
        if not isinstance(config, dict):
            return {"status": "error",
                    "message": "Knowledge extraction config must be an object of its config.ini keys."}
        try:
            unknown = set(config) - {"cultural_perspectives_enabled", "cold_fallback_provider", "status"}
            if unknown:
                return {"status": "error",
                        "message": f"Unknown knowledge key(s): {', '.join(sorted(unknown))}"}
            alias = config.get("cold_fallback_provider")
            if isinstance(alias, str) and alias.strip():
                providers = dict(getattr(self.llm_manager, "providers", None) or {})
                if alias.strip() not in providers:
                    loaded = ", ".join(sorted(providers)) or "none"
                    return {"status": "error",
                            "message": f"knowledge.cold_fallback_provider names {alias.strip()!r}, which is "
                                       f"not a loaded provider (loaded: {loaded}); leave it empty for auto."}
            self.settings.set_knowledge_extraction_settings(
                **{k: v for k, v in config.items() if k != "status"})
            stored = self.settings.get_knowledge_extraction_settings()
            await self.local_api.broadcast_event("knowledge_extraction_config_updated", stored)
            return {"status": "success", "message": "Knowledge extraction settings saved successfully",
                    **stored}
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            logger.error("Error saving knowledge extraction config: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def get_history_sync_config(self) -> Dict[str, Any]:
        """The synced-history gate (`[history] reject_unsigned`)."""
        try:
            return {"status": "success", **self.settings.get_history_sync_settings()}
        except Exception as e:
            logger.error("Error getting history sync config: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def save_history_sync_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Save the synced-history gate. A key left out keeps its stored value; an
        invalid one is refused with its reason and nothing is written."""
        if not isinstance(config, dict):
            return {"status": "error",
                    "message": "History sync config must be an object of its config.ini keys."}
        try:
            unknown = set(config) - {"reject_unsigned", "status"}
            if unknown:
                return {"status": "error",
                        "message": f"Unknown history key(s): {', '.join(sorted(unknown))}"}
            self.settings.set_history_sync_settings(
                **{k: v for k, v in config.items() if k != "status"})
            stored = self.settings.get_history_sync_settings()
            await self.local_api.broadcast_event("history_sync_config_updated", stored)
            return {"status": "success", "message": "History sync setting saved successfully", **stored}
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            logger.error("Error saving history sync config: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def get_hf_config(self) -> Dict[str, Any]:
        """`[hf] offline_mode` as run_service reads it: `offline_mode` is one of auto, on,
        off (auto = the key is absent and the per-start cache detection decides)."""
        try:
            return {"status": "success", **self.settings.get_hf_settings()}
        except Exception as e:
            logger.error("Error getting hf config: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    async def save_hf_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Save `[hf] offline_mode`. `offline_mode` is one of auto (the key is removed),
        on (written true) or off (written false) — run_service's getboolean spellings; a
        key left out keeps its stored state."""
        if not isinstance(config, dict):
            return {"status": "error", "message": "HF config must be an object of its config.ini keys."}
        try:
            unknown = set(config) - {"offline_mode", "status"}
            if unknown:
                return {"status": "error",
                        "message": f"Unknown hf key(s): {', '.join(sorted(unknown))}"}
            self.settings.set_hf_settings(**{k: v for k, v in config.items() if k != "status"})
            stored = self.settings.get_hf_settings()
            await self.local_api.broadcast_event("hf_config_updated", stored)
            return {"status": "success", "message": "HuggingFace offline mode saved successfully", **stored}
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            logger.error("Error saving hf config: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}
