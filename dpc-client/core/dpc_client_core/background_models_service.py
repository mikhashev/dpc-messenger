"""
BackgroundModelsService — the node-level device and backend keys of the background models.

Follows the VoiceService pattern (docs/decisions/001-service-split.md): no older service
owns these keys — KnowledgeService is PCM/consensus, VoiceService is voice — and the
config-parity audit (audit/config_ui_parity_2026-11-27.md) found none of them had a UI
writer (card A-NODE-HAS-NO-ONE-SWITCH-THAT-KEEPS-ITS-BACKGROUND-MODELS-OFF-THE-GPU).

Responsibilities:
- get/save of [memory] embedding_device
- get/save of [knowledge_graph] gliner_device and backend

NOT in scope (stays where it is):
- the readers themselves (service start, knowledge_graph.KnowledgeGraph)
- [local_transcription] device — Whisper's key, covered by its own section
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class BackgroundModelsService:
    """
    The node-level background-model keys of config.ini: where each background model
    runs and which store the agent graphs use.

    Dependencies injected at construction time (no EventBus):
    - settings: Settings — config read/write
    - local_api: LocalApiServer — UI event broadcasting
    """

    def __init__(self, settings: Any, local_api: Any):
        self.settings = settings
        self.local_api = local_api

    async def get_background_model_config(self) -> Dict[str, Any]:
        """
        Get the background model configuration (the three node-level keys).

        UI Integration: Called when user opens the background models form. The getter
        answers what the readers would resolve now; see the settings docstring for
        when a written value is in force (per key, and said so in the form).

        Returns:
            Dict with the background model settings
        """
        try:
            config = self.settings.get_background_model_settings()
            return {"status": "success", **config}
        except Exception as e:
            logger.error("Error getting background model config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def save_background_model_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Save the background model configuration (the three node-level keys).

        UI Integration: Called when user saves the background models form. The config
        dict's keys are the config.ini keys; a key left out keeps its stored value. An
        invalid value is refused with its reason and nothing is written. When a saved
        value is in force is not the same for the three keys — the next backend start
        for embedding_device, the next graph open (or first model load) for the two
        [knowledge_graph] keys — and the form says so per key rather than here.

        Args:
            config: Background model configuration dict

        Returns:
            Dict with status and message
        """
        known = {"embedding_device", "gliner_device", "backend"}
        try:
            unknown = set(config) - known - {"status"}
            if unknown:
                return {
                    "status": "error",
                    "message": f"Unknown background model key(s): {', '.join(sorted(unknown))}"
                }
            self.settings.set_background_model_settings(
                **{k: v for k, v in config.items() if k != "status"})
            stored = self.settings.get_background_model_settings()
            await self.local_api.broadcast_event("background_model_config_updated", stored)
            return {
                "status": "success",
                "message": "Background model settings saved successfully"
            }
        except ValueError as e:
            return {
                "status": "error",
                "message": str(e)
            }
        except Exception as e:
            logger.error("Error saving background model config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }
