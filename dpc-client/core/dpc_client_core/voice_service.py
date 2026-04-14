"""
VoiceService — Whisper model lifecycle and voice transcription configuration.

Extracted from service.py as part of the Grand Refactoring (Phase 1a).
See docs/decisions/001-service-split.md for rationale.

Responsibilities:
- Whisper model preloading and downloading
- Voice transcription provider selection
- Per-conversation transcription settings (persist to disk)
- Transcription capability checking

NOT in scope (stays in service.py or handlers):
- send_voice_message / send_group_voice_message (P2P + file transfer)
- _maybe_transcribe_voice_message (triggered by file transfer completion)
- _retroactively_transcribe_conversation (uses conversation_monitors/KnowledgeService)
- _broadcast_voice_transcription (uses p2p_manager)
- transcribe_audio (remote P2P transcription path)
- _handle_transcription_request (P2P handler)
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional

logger = logging.getLogger(__name__)

DPC_HOME_DIR = Path.home() / ".dpc"


class VoiceService:
    """
    Whisper model lifecycle and voice transcription configuration.

    Dependencies injected at construction time (no EventBus):
    - llm_manager: LLMManager — provider access
    - settings: Settings — voice config read/write
    - local_api: LocalApiServer — UI event broadcasting
    - on_transcription_enabled: Optional callback for retroactive transcription
      (called when user enables auto-transcribe; implemented in service.py
      as _retroactively_transcribe_conversation which needs conversation_monitors)
    """

    def __init__(
        self,
        llm_manager: Any,
        settings: Any,
        local_api: Any,
        on_transcription_enabled: Optional[Callable[[str], Coroutine]] = None,
    ):
        self.llm_manager = llm_manager
        self.settings = settings
        self.local_api = local_api
        self._on_transcription_enabled = on_transcription_enabled

        # Voice transcription state (owned by VoiceService)
        self._voice_transcriptions: Dict[str, Dict[str, Any]] = {}
        self._transcription_locks: Dict[str, asyncio.Lock] = {}
        self._voice_transcription_settings: Dict[str, bool] = {}
        self._load_voice_transcription_settings()

    def get_state(self) -> dict:
        """Agent-readable snapshot of VoiceService state."""
        loaded_providers = []
        for alias, p in self.llm_manager.providers.items():
            if p.config.get("type") == "local_whisper":
                if hasattr(p, "is_model_loaded") and p.is_model_loaded():
                    loaded_providers.append(alias)
        return {
            "whisper_providers_loaded": loaded_providers,
            "active_transcriptions": len(self._voice_transcriptions),
            "transcription_enabled_conversations": sum(
                1 for v in self._voice_transcription_settings.values() if v
            ),
        }

    def _provider_supports_voice(self, provider: Any) -> bool:
        """
        Check if a provider supports voice transcription (Whisper).

        v0.13.1+: Updated to support LocalWhisperProvider.

        Args:
            provider: AIProvider instance

        Returns:
            True if provider supports voice transcription, False otherwise
        """
        provider_type = provider.__class__.__name__.replace("Provider", "")

        # Local Whisper provider (offline transcription)
        if provider_type == "LocalWhisper":
            return True

        # OpenAI/OpenAI-compatible providers (cloud-based transcription)
        if provider_type == "OpenAICompatible":
            return True

        # Other providers (Ollama, Anthropic, Z.AI) do not support voice transcription
        return False

    async def set_voice_provider(self, provider_alias: str) -> Dict[str, Any]:
        """
        Set the default voice provider for transcription.

        v0.13.0+: Sets the voice_provider field in providers.json.

        Args:
            provider_alias: Alias of the provider to set as default for voice transcription

        Returns:
            Dictionary with status and message
        """
        import json

        # Validate provider exists and supports voice
        if provider_alias not in self.llm_manager.providers:
            return {
                "status": "error",
                "message": f"Provider '{provider_alias}' not found"
            }

        provider = self.llm_manager.providers[provider_alias]
        if not self._provider_supports_voice(provider):
            return {
                "status": "error",
                "message": f"Provider '{provider_alias}' does not support voice transcription"
            }

        try:
            # Read current config
            config_path = self.llm_manager.config_path
            with open(config_path, 'r') as f:
                config = json.load(f)

            # Update voice_provider
            config["voice_provider"] = provider_alias

            # Save updated config
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)

            # Reload providers
            self.llm_manager.providers.clear()
            self.llm_manager._load_providers_from_config()

            # Broadcast event
            await self.local_api.broadcast_event("default_providers_updated", {
                "voice_provider": provider_alias
            })

            logger.info(f"Voice provider set to '{provider_alias}'")
            return {
                "status": "success",
                "message": f"Voice provider set to '{provider_alias}'"
            }
        except Exception as e:
            logger.error(f"Failed to set voice provider: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Failed to set voice provider: {e}"
            }

    async def preload_whisper_model(self, provider_alias: str | None = None) -> dict:
        """
        Pre-load Whisper model into memory/GPU for faster first transcription.

        Called by UI when user enables auto-transcribe to avoid delays on first voice message.

        Args:
            provider_alias: Optional provider alias (default: first local_whisper provider)

        Returns:
            dict with status and info
        """
        try:
            # Find Whisper provider
            provider_obj = None

            if provider_alias:
                provider_obj = self.llm_manager.providers.get(provider_alias)
                if not provider_obj or provider_obj.config.get("type") != "local_whisper":
                    return {
                        "status": "error",
                        "error": f"Provider '{provider_alias}' is not a local Whisper provider"
                    }
            else:
                # Find first local_whisper provider
                for provider in self.llm_manager.providers.values():
                    if provider.config.get("type") == "local_whisper":
                        provider_obj = provider
                        provider_alias = provider.alias
                        break

                if not provider_obj:
                    return {
                        "status": "error",
                        "error": "No local Whisper provider configured"
                    }

            # Check if already loaded
            if hasattr(provider_obj, 'is_model_loaded') and provider_obj.is_model_loaded():
                logger.info(f"Whisper model already loaded for provider '{provider_alias}'")
                return {
                    "status": "success",
                    "provider": provider_alias,
                    "already_loaded": True
                }

            # Broadcast loading started event
            await self.local_api.broadcast_event("whisper_model_loading_started", {
                "provider": provider_alias
            })

            # Unload any other loaded Whisper providers to free VRAM before loading
            for alias, p in self.llm_manager.providers.items():
                if alias != provider_alias and p.config.get("type") == "local_whisper":
                    if hasattr(p, 'is_model_loaded') and p.is_model_loaded():
                        logger.info(f"Unloading Whisper provider '{alias}' before loading '{provider_alias}'")
                        await p.unload_model_async()

            # Load model (this runs in thread pool, so it won't block)
            logger.info(f"Pre-loading Whisper model for provider '{provider_alias}'...")

            if hasattr(provider_obj, 'ensure_model_loaded'):
                await provider_obj.ensure_model_loaded()
            else:
                # Fallback: trigger loading via a dummy transcription (silent audio)
                logger.warning(f"Provider '{provider_alias}' doesn't support ensure_model_loaded(), using fallback")
                return {
                    "status": "error",
                    "error": "Provider doesn't support pre-loading"
                }

            # Broadcast loading completed event
            await self.local_api.broadcast_event("whisper_model_loaded", {
                "provider": provider_alias
            })

            logger.info(f"Successfully pre-loaded Whisper model for provider '{provider_alias}'")

            return {
                "status": "success",
                "provider": provider_alias,
                "already_loaded": False
            }

        except Exception as e:
            logger.error(f"Failed to pre-load Whisper model: {e}", exc_info=True)

            # Broadcast loading failed event
            await self.local_api.broadcast_event("whisper_model_loading_failed", {
                "provider": provider_alias if provider_alias else "unknown",
                "error": str(e)
            })

            return {
                "status": "error",
                "error": str(e)
            }

    async def download_whisper_model(self, provider_alias: str | None = None) -> dict:
        """
        Download Whisper model from HuggingFace to local cache.

        Called by UI when user confirms download in the dialog.

        Args:
            provider_alias: Optional provider alias (default: first local_whisper provider)

        Returns:
            dict with status and info
        """
        try:
            # Find Whisper provider
            provider_obj = None

            if provider_alias:
                provider_obj = self.llm_manager.providers.get(provider_alias)
                if not provider_obj or provider_obj.config.get("type") != "local_whisper":
                    return {
                        "status": "error",
                        "error": f"Provider '{provider_alias}' is not a local Whisper provider"
                    }
            else:
                # Find first local_whisper provider
                for provider in self.llm_manager.providers.values():
                    if provider.config.get("type") == "local_whisper":
                        provider_obj = provider
                        provider_alias = provider.alias
                        break

                if not provider_obj:
                    return {
                        "status": "error",
                        "error": "No local Whisper provider configured"
                    }

            # Check if provider supports download
            if not hasattr(provider_obj, 'download_model_async'):
                return {
                    "status": "error",
                    "error": f"Provider '{provider_alias}' doesn't support model download"
                }

            # Broadcast download started event
            await self.local_api.broadcast_event("whisper_model_download_started", {
                "provider": provider_alias,
                "model_name": provider_obj.config.get("model", "unknown")
            })

            # Download model (this runs in thread pool, so it won't block)
            logger.info(f"Starting Whisper model download for provider '{provider_alias}'...")

            result = await provider_obj.download_model_async()

            if result.get("success"):
                # Broadcast download completed event
                await self.local_api.broadcast_event("whisper_model_download_completed", {
                    "provider": provider_alias,
                    "model_name": result.get("model_name"),
                    "cache_path": result.get("cache_path")
                })

                logger.info(f"Successfully downloaded Whisper model for provider '{provider_alias}'")

                return {
                    "status": "success",
                    "provider": provider_alias,
                    "model_name": result.get("model_name"),
                    "message": result.get("message")
                }
            else:
                # Broadcast download failed event
                await self.local_api.broadcast_event("whisper_model_download_failed", {
                    "provider": provider_alias,
                    "error": result.get("message")
                })

                return {
                    "status": "error",
                    "error": result.get("message")
                }

        except Exception as e:
            logger.error(f"Failed to download Whisper model: {e}", exc_info=True)

            # Broadcast download failed event
            await self.local_api.broadcast_event("whisper_model_download_failed", {
                "provider": provider_alias if provider_alias else "unknown",
                "error": str(e)
            })

            return {
                "status": "error",
                "error": str(e)
            }

    def _load_voice_transcription_settings(self) -> None:
        """
        Load per-conversation voice transcription settings from disk.

        Settings stored in ~/.dpc/voice_transcription_settings.json:
        {
            "node_id_1": true,
            "node_id_2": false,
            ...
        }
        """
        settings_file = DPC_HOME_DIR / "voice_transcription_settings.json"
        if settings_file.exists():
            try:
                import json
                with open(settings_file, 'r') as f:
                    self._voice_transcription_settings = json.load(f)
                logger.debug(f"Loaded voice transcription settings for {len(self._voice_transcription_settings)} conversations")
            except Exception as e:
                logger.warning(f"Failed to load voice transcription settings: {e}")
                self._voice_transcription_settings = {}
        else:
            logger.debug("No voice transcription settings file found, using empty settings")
            self._voice_transcription_settings = {}

    def _save_voice_transcription_settings(self) -> None:
        """Save per-conversation voice transcription settings to disk."""
        settings_file = DPC_HOME_DIR / "voice_transcription_settings.json"
        try:
            import json
            with open(settings_file, 'w') as f:
                json.dump(self._voice_transcription_settings, f, indent=2)
            logger.debug(f"Saved voice transcription settings for {len(self._voice_transcription_settings)} conversations")
        except Exception as e:
            logger.error(f"Failed to save voice transcription settings: {e}")

    def _is_transcription_needed(self) -> bool:
        """
        Check if any active conversation has auto-transcribe enabled.

        Returns:
            True if at least one conversation needs transcription, False otherwise.

        Used to determine if Whisper model can be safely unloaded.
        """
        # Check if global auto-transcribe is enabled
        value = self.settings.get('voice_messages', 'auto_transcribe', fallback='true')
        global_enabled = value.lower() in ('true', '1', 'yes')

        if not global_enabled:
            return False  # Global disable overrides all

        # Check per-conversation settings
        for node_id, enabled in self._voice_transcription_settings.items():
            if enabled:
                return True  # At least one conversation needs it

        return False  # No conversations need transcription

    async def _check_transcription_capability(self, check_model_loaded: bool = True) -> bool:
        """
        Check if this node has transcription capability (provider available AND ready).

        Args:
            check_model_loaded: If True, check if local_whisper model is loaded in memory.
                               If False, only check if provider exists (allow lazy loading).
                               Use False for senders, True for receivers.

        For local_whisper providers, optionally checks if model is actually loaded in memory.
        This prevents failed transcription attempts when model is not ready.

        Returns:
            True if at least one transcription provider is available (and loaded if check_model_loaded=True)
        """
        provider_priority = self.settings.get_voice_transcription_provider_priority()
        logger.debug(f"Checking transcription capability with provider priority: {provider_priority} (check_loaded={check_model_loaded})")
        logger.debug(f"Available LLM providers: {list(self.llm_manager.providers.keys())}")

        for provider_alias in provider_priority:
            # Check if provider exists in LLM manager
            if provider_alias in self.llm_manager.providers:
                provider = self.llm_manager.providers[provider_alias]
                # Check if provider supports voice (has whisper or audio capabilities)
                provider_type = provider.config.get("type", "")
                logger.debug(f"Provider '{provider_alias}' has type '{provider_type}'")

                if provider_type in ["local_whisper", "openai", "openai_compatible"]:
                    # For local_whisper, optionally check if model is actually loaded
                    if check_model_loaded and provider_type == "local_whisper":
                        if hasattr(provider, 'is_model_loaded') and not provider.is_model_loaded():
                            logger.info(f"Provider '{provider_alias}' exists but model not loaded yet, skipping")
                            continue  # Try next provider

                    logger.info(f"Found transcription capability: {provider_alias} ({provider_type})")
                    return True
            else:
                logger.debug(f"Provider '{provider_alias}' not found in LLM manager")

        logger.warning(f"No transcription providers found in priority list: {provider_priority}")
        return False

    async def get_voice_transcription_config(self) -> Dict[str, Any]:
        """
        Get voice transcription configuration.

        UI Integration: Called when user opens voice transcription settings.

        Returns:
            Dict with voice transcription settings
        """
        try:
            return {
                "status": "success",
                "enabled": self.settings.get_voice_transcription_enabled(),
                "sender_transcribes": self.settings.get_voice_transcription_sender_transcribes(),
                "recipient_delay_seconds": self.settings.get_voice_transcription_recipient_delay_seconds(),
                "provider_priority": self.settings.get_voice_transcription_provider_priority(),
                "show_transcriber_name": self.settings.get_voice_transcription_show_transcriber_name(),
                "cache_enabled": self.settings.get_voice_transcription_cache_enabled(),
                "fallback_to_openai": self.settings.get_voice_transcription_fallback_to_openai()
            }
        except Exception as e:
            logger.error("Error getting voice transcription config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def save_voice_transcription_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Save voice transcription configuration.

        UI Integration: Called when user saves voice transcription settings.

        Args:
            config: Voice transcription configuration dict

        Returns:
            Dict with status and message
        """
        try:
            # Update settings
            for key, value in config.items():
                if key == "status":  # Skip status field from request
                    continue
                config_value = str(value) if not isinstance(value, list) else ",".join(value)
                self.settings.set('voice_transcription', key, config_value)

            # Persist to disk
            self.settings.save()

            # Broadcast update event to UI
            await self.local_api.broadcast_event("voice_transcription_config_updated", config)

            return {
                "status": "success",
                "message": "Voice transcription settings saved successfully"
            }

        except Exception as e:
            logger.error("Error saving voice transcription config: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def set_conversation_transcription(self, node_id: str, enabled: bool) -> Dict[str, Any]:
        """
        Set per-conversation auto-transcription setting (v0.13.2+ UI checkbox control).

        UI Integration: Called when user toggles "Auto Transcribe" checkbox in chat header.

        Args:
            node_id: Peer node ID for the conversation
            enabled: True to enable auto-transcription, False to disable

        Returns:
            Dict with status and message
        """
        try:
            # Update in-memory setting
            self._voice_transcription_settings[node_id] = enabled

            # Persist to disk
            self._save_voice_transcription_settings()

            logger.info(f"Set auto-transcription for {node_id}: {enabled}")

            # If enabling auto-transcribe, retroactively transcribe previous untranscribed voice messages
            if enabled:
                if self._on_transcription_enabled:
                    asyncio.create_task(self._on_transcription_enabled(node_id))

            # Check if we should unload the model
            if not enabled:
                # Check if any other conversation still needs transcription
                if not self._is_transcription_needed():
                    logger.info("Auto-transcribe disabled for all conversations, unloading Whisper model...")

                    # Find the local_whisper provider
                    whisper_provider = None
                    for alias, provider in self.llm_manager.providers.items():
                        if provider.config.get('type') == 'local_whisper':
                            if hasattr(provider, 'unload_model_async'):
                                whisper_provider = provider
                                break

                    # Unload the model
                    if whisper_provider:
                        try:
                            await whisper_provider.unload_model_async()

                            # Broadcast unload event to UI
                            await self.local_api.broadcast_event("whisper_model_unloaded", {
                                "reason": "auto_transcribe_disabled"
                            })

                            logger.info("Whisper model unloaded successfully")
                        except Exception as e:
                            logger.error(f"Failed to unload Whisper model: {e}", exc_info=True)
                    else:
                        logger.debug("No local_whisper provider found to unload")

            return {
                "status": "success",
                "message": f"Auto-transcription {'enabled' if enabled else 'disabled'} for conversation"
            }

        except Exception as e:
            logger.error(f"Error setting conversation transcription for {node_id}: {e}", exc_info=True)
            return {
                "status": "error",
                "message": str(e)
            }

    async def get_conversation_transcription(self, node_id: str) -> Dict[str, Any]:
        """
        Get per-conversation auto-transcription setting (v0.13.2+ UI checkbox state).

        UI Integration: Called when chat loads to restore checkbox state.

        Args:
            node_id: Peer node ID for the conversation

        Returns:
            Dict with status and enabled flag
        """
        try:
            # Default to True if not set (backward compatibility)
            enabled = self._voice_transcription_settings.get(node_id, True)

            return {
                "status": "success",
                "enabled": enabled
            }

        except Exception as e:
            logger.error(f"Error getting conversation transcription for {node_id}: {e}", exc_info=True)
            return {
                "status": "error",
                "message": str(e),
                "enabled": True  # Default to enabled on error
            }
