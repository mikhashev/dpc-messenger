# dpc-client/core/dpc-client_core/llm_manager.py

import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable

from .providers import (
    AIProvider, ModelNotCachedError, parse_thinking_tags,
    OPENAI_THINKING_MODELS, ANTHROPIC_THINKING_MODELS,
    OllamaProvider, OLLAMA_VISION_MODELS, OLLAMA_THINKING_MODELS,
    OpenAICompatibleProvider, AnthropicProvider, ZaiProvider,
    LocalWhisperProvider, RemotePeerProvider, DpcAgentProvider,
    GeminiProvider, GitHubModelsProvider, GigaChatProvider,
)

logger = logging.getLogger(__name__)

# Token counting
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logger.warning("tiktoken not available - token counting will use estimation for all models")

# --- Provider classes are now in dpc_client_core/providers/ ---
# The following names are re-exported here for backward compatibility:
# ModelNotCachedError, AIProvider, parse_thinking_tags,
# OLLAMA_VISION_MODELS, OLLAMA_THINKING_MODELS, OPENAI_THINKING_MODELS, ANTHROPIC_THINKING_MODELS,
# OllamaProvider, OpenAICompatibleProvider, AnthropicProvider, ZaiProvider,
# LocalWhisperProvider, RemotePeerProvider, DpcAgentProvider,
# GeminiProvider, GitHubModelsProvider, GigaChatProvider

# --- The Manager Class ---

PROVIDER_MAP = {
    "ollama": OllamaProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
    "zai": ZaiProvider,
    "local_whisper": LocalWhisperProvider,  # v0.13.1+: Local Whisper transcription
    "dpc_agent": DpcAgentProvider,  # Embedded autonomous AI agent
    "remote_peer": RemotePeerProvider,  # v0.18.0+: Remote peer inference
    # Subscription plan providers (v0.21.0+)
    "gemini": GeminiProvider,          # Google Gemini / AI Studio
    "github_models": GitHubModelsProvider,  # GitHub Models (free/Pro)
    "gigachat": GigaChatProvider,      # GigaChat by Sberbank
}

# Default context window sizes for common models (in tokens)
MODEL_CONTEXT_WINDOWS = {
    # Ollama models
    "llama3.1:8b": 131072,  # 128K tokens
    "llama3.1:13b": 131072,
    "llama3.1:70b": 131072,
    "llama3.2:1b": 131072,
    "llama3.2:3b": 131072,
    "mistral:7b": 8192,
    "mixtral:8x7b": 32768,
    "qwen2.5:7b": 32768,
    "deepseek-coder-v2:16b": 131072,
    "codellama:7b": 16384,

    # Ollama vision models
    "qwen3-vl:2b": 262144,     # 256K tokens
    "qwen3-vl:4b": 262144,
    "qwen3-vl:8b": 262144,
    "qwen3-vl:30b": 262144,
    "qwen3-vl:32b": 262144,
    "llama3.2-vision:11b": 131072,  # 128K tokens
    "llama3.2-vision:90b": 131072,
    "ministral-3:3b": 262144,   # 256K tokens
    "ministral-3:8b": 262144,
    "ministral-3:14b": 262144,
    "llava:7b": 4096,
    "llava:13b": 4096,
    "llava:34b": 4096,

    # OpenAI models
    "gpt-4": 8192,
    "gpt-4-32k": 32768,
    "gpt-4-turbo": 128000,
    "gpt-4o": 128000,
    "gpt-3.5-turbo": 16384,
    "gpt-3.5-turbo-16k": 16384,

    # Anthropic models
    "claude-3-opus-20240229": 200000,
    "claude-3-sonnet-20240229": 200000,
    "claude-3-haiku-20240307": 200000,
    "claude-3-5-sonnet-20240620": 200000,
    "claude-sonnet-4-5-20250929": 200000,
    "claude-haiku-4-5": 200000,  # Claude Haiku 4.5 (shorthand model name)
    "claude-opus-4-1": 200000,   # Claude Opus 4.1 (200K context)
    "claude-opus-4-5": 200000,   # Claude Opus 4.5 (shorthand model name)

    # Z.AI models (GLM series) - from docs.z.ai
    "glm-4.7": 128000,  # 128K tokens (estimated)
    "glm-4.6": 128000,  # 128K tokens (estimated)
    "glm-4.6v-flash": 128000,  # Vision model
    "glm-4.5": 128000,  # 128K tokens (estimated)
    "glm-4.5v": 128000,  # Vision model
    "glm-4.5-air": 128000,
    "glm-4.5-airx": 128000,
    "glm-4.5-flash": 128000,
    "glm-4-plus": 128000,
    "glm-4.0v": 128000,  # Vision model
    "glm-4-128-0414-128k": 131072,  # 128K explicit in name
    "autoglm-phone-multilingal": 32768,  # Conservative estimate

    # Default fallback
    "default": 4096
}

class LLMManager:
    """
    Manages all configured AI providers.
    """
    def __init__(self, config_path: Path = Path.home() / ".dpc" / "providers.json"):
        self.config_path = config_path
        self.providers: Dict[str, AIProvider] = {}
        self.default_provider: str | None = None
        self.vision_provider: str | None = None  # Vision-specific provider for auto-selection
        self.voice_provider: str | None = None  # v0.13.0+: Voice transcription provider for auto-selection
        self.background_provider: str | None = None  # v0.21.0+: Background tasks provider (sleep consolidation)

        # Callback for re-injecting CoreService after providers reload (v0.18.0+)
        self._on_providers_reload_callback: Optional[Callable[[], None]] = None

        # Token counting manager (Phase 4 refactor - v0.12.1)
        from dpc_client_core.managers.token_count_manager import TokenCountManager
        self.token_count_manager = TokenCountManager()

        self._load_providers_from_config()

    def set_on_providers_reload(self, callback: Callable[[], None]) -> None:
        """
        Register a callback to be called after providers are reloaded.

        Used by CoreService to re-inject itself into dpc_agent and remote_peer
        providers after configuration changes.

        Args:
            callback: Function to call after providers reload
        """
        self._on_providers_reload_callback = callback

    def _ensure_config_exists(self):
        """Creates a default providers.json file if one doesn't exist."""
        if not self.config_path.exists():
            logger.warning("Provider config file not found at %s", self.config_path)
            logger.info("Creating a default template with a local Ollama provider")

            self.config_path.parent.mkdir(parents=True, exist_ok=True)

            default_config = {
                "_comment": "AI Provider Configuration - Manage your local and cloud AI providers",
                "default_provider": "ollama_text",
                "vision_provider": "ollama_vision",
                "voice_provider": "local_whisper_large",  # v0.13.0+: Local Whisper or OpenAI-compatible
                "agent_provider": "dpc_agent",  # v0.18.0+: AI Agent provider (dpc_agent or any other provider)
                "background_provider": None,  # v0.21.0+: Separate provider for agent background tasks (sleep consolidation)
                "providers": [
                    {
                        "alias": "ollama_text",
                        "type": "ollama",
                        "model": "llama3.1:8b",
                        "host": "http://127.0.0.1:11434",
                        "context_window": 16384,
                        "_note": "Fast text model for regular chat queries"
                    },
                    {
                        "alias": "ollama_vision",
                        "type": "ollama",
                        "model": "qwen3-vl:8b",
                        "host": "http://127.0.0.1:11434",
                        "context_window": 16384,
                        "_note": "Vision model for image analysis"
                    },
                    {
                        "alias": "local_whisper_large",
                        "type": "local_whisper",
                        "model": "openai/whisper-large-v3",
                        "device": "auto",
                        "compile_model": False,
                        "use_flash_attention": False,
                        "chunk_length_s": 30,
                        "batch_size": 16,
                        "language": "auto",
                        "task": "transcribe",
                        "lazy_loading": True,
                        "_note": "Local Whisper transcription - GPU accelerated (CUDA, MLX)"
                    },
                    {
                        "alias": "dpc_agent",
                        "type": "dpc_agent",
                        "_note": "Embedded autonomous AI agent for task automation - uses default AI provider"
                    }
                ],
                "_examples": {
                    "_comment": "Example configurations - uncomment and add to providers array above",
                    "ollama_vision_alternatives": [
                        {
                            "alias": "ollama_qwen_vision",
                            "type": "ollama",
                            "model": "qwen3-vl:8b",
                            "host": "http://127.0.0.1:11434",
                            "context_window": 262144,
                            "_note": "Qwen3-VL 8B - excellent vision model (256K context)"
                        },
                        {
                            "alias": "ollama_ministral_vision",
                            "type": "ollama",
                            "model": "ministral-3:8b",
                            "host": "http://127.0.0.1:11434",
                            "context_window": 262144,
                            "_note": "Ministral 3 8B - fast vision model (256K context)"
                        }
                    ],
                    "ollama_small_models": [
                        {
                            "alias": "ollama_small",
                            "type": "ollama",
                            "model": "llama3.2:3b",
                            "host": "http://127.0.0.1:11434",
                            "context_window": 131072,
                            "_note": "Small model for resource-constrained systems (~2GB RAM)"
                        },
                        {
                            "alias": "ollama_tiny",
                            "type": "ollama",
                            "model": "llama3.2:1b",
                            "host": "http://127.0.0.1:11434",
                            "context_window": 131072,
                            "_note": "Tiny model for embedded devices (~1GB RAM)"
                        }
                    ],
                    "lm_studio": {
                        "alias": "lm_studio",
                        "type": "openai_compatible",
                        "model": "lmstudio-community/Meta-Llama-3-8B-Instruct-GGUF",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "api_key": "lm-studio",
                        "_note": "Local LM Studio - OpenAI-compatible API"
                    },
                    "openai": {
                        "alias": "gpt4o",
                        "type": "openai_compatible",
                        "model": "gpt-4o",
                        "base_url": "https://api.openai.com/v1",
                        "api_key_env": "OPENAI_API_KEY",
                        "context_window": 128000,
                        "_note": "OpenAI GPT-4o - powerful vision-capable model",
                        "_setup": "Set environment variable: export OPENAI_API_KEY='sk-...'"
                    },
                    "anthropic": [
                        {
                            "alias": "claude_sonnet",
                            "type": "anthropic",
                            "model": "claude-sonnet-4-5",
                            "api_key_env": "ANTHROPIC_API_KEY",
                            "context_window": 200000,
                            "_note": "Claude Sonnet 4.5 - most capable (vision-capable, 200K context)",
                            "_setup": "Set environment variable: export ANTHROPIC_API_KEY='sk-ant-...'"
                        },
                        {
                            "alias": "claude_haiku",
                            "type": "anthropic",
                            "model": "claude-haiku-4-5",
                            "api_key_env": "ANTHROPIC_API_KEY",
                            "context_window": 200000,
                            "_note": "Claude Haiku 4.5 - fast and affordable (vision-capable, 200K context)"
                        }
                    ]
                },
                "_instructions": {
                    "default_provider": "Provider used for all text-only queries (no images)",
                    "vision_provider": "Provider used for image analysis queries (screenshots, photos, diagrams)",
                    "voice_provider": "v0.13.0+: Provider used for voice transcription (local_whisper or OpenAI-compatible)",
                    "model_installation": {
                        "ollama": "Install models: ollama pull llama3.1:8b && ollama pull qwen3-vl:8b",
                        "alternative_vision": "Other vision models: ollama pull qwen3-vl:8b OR ollama pull ministral-3:8b",
                        "small_models": "For low RAM: ollama pull llama3.2:3b (2GB) OR ollama pull llama3.2:1b (1GB)"
                    },
                    "supported_types": "ollama (local, free), openai_compatible (GPT, LM Studio), anthropic (Claude)",
                    "vision_capable_models": {
                        "ollama": "llama3.2-vision, qwen3-vl, ministral-3, llava (all sizes)",
                        "openai": "gpt-4o, gpt-4-turbo, gpt-4o-mini",
                        "anthropic": "claude-3+, claude-opus-4-5, claude-sonnet-4-5, claude-haiku-4-5"
                    },
                    "context_windows": {
                        "128K": "llama3.1, llama3.2-vision, gpt-4o (efficient for most use cases)",
                        "256K": "qwen3-vl, ministral-3 (excellent for long documents)",
                        "200K": "claude-3+, claude-4.5 (best for complex analysis)"
                    },
                    "vram_requirements": {
                        "1GB": "llama3.2:1b (tiny, embedded GPUs)",
                        "2GB": "llama3.2:3b (small, budget GPUs)",
                        "8GB": "llama3.1:8b, qwen3-vl:8b, ministral-3:8b (recommended - RTX 3060)",
                        "12GB": "llama3.1:13b (RTX 3060 12GB, RTX 4060 Ti)",
                        "16GB": "llama3.2-vision:11b (RTX 4060 Ti 16GB, RTX 4080)",
                        "24GB+": "llama3.1:70b, llama3.2-vision:90b (RTX 4090, A5000, professional)"
                    },
                    "api_key_setup": {
                        "linux_mac": "Add to ~/.bashrc: export OPENAI_API_KEY='sk-...' && export ANTHROPIC_API_KEY='sk-ant-...'",
                        "windows_cmd": "setx OPENAI_API_KEY \"sk-...\" && setx ANTHROPIC_API_KEY \"sk-ant-...\"",
                        "windows_powershell": "$env:OPENAI_API_KEY='sk-...'; [Environment]::SetEnvironmentVariable('OPENAI_API_KEY', 'sk-...', 'User')"
                    }
                }
            }

            with open(self.config_path, 'w') as f:
                json.dump(default_config, f, indent=2)
            logger.info("Default provider config created at %s", self.config_path)

    def _load_providers_from_config(self):
        """Reads the config file and initializes all defined providers."""
        self._ensure_config_exists()
        logger.info("Loading AI providers from %s", self.config_path)
        if not self.config_path.exists():
            logger.warning("Provider config file not found at %s - no providers loaded", self.config_path)
            return

        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)

            self.default_provider = config.get("default_provider")
            self.vision_provider = config.get("vision_provider")  # Load vision provider for auto-selection
            self.voice_provider = config.get("voice_provider")  # v0.13.0+: Load voice provider for auto-selection
            self.agent_provider = config.get("agent_provider")  # v0.18.0+: Load agent provider for AI agent
            self.background_provider = config.get("background_provider")  # v0.21.0+: Separate provider for agent background tasks (sleep consolidation)

            for provider_config in config.get("providers", []):
                alias = provider_config.get("alias")
                provider_type = provider_config.get("type")

                if not alias or not provider_type:
                    logger.warning("Skipping invalid provider config: %s", provider_config)
                    continue

                if provider_type in PROVIDER_MAP:
                    provider_class = PROVIDER_MAP[provider_type]
                    try:
                        self.providers[alias] = provider_class(alias, provider_config)
                        logger.info("Successfully loaded provider '%s' of type '%s'", alias, provider_type)
                    except (ValueError, KeyError) as e:
                        logger.error("Error loading provider '%s': %s", alias, e)
                else:
                    logger.warning("Unknown provider type '%s' for alias '%s'", provider_type, alias)

            if self.default_provider and self.default_provider not in self.providers:
                logger.warning("Default provider '%s' not found in loaded providers", self.default_provider)
                self.default_provider = None

            if self.agent_provider and self.agent_provider not in self.providers:
                logger.warning("Agent provider '%s' not found in loaded providers", self.agent_provider)
                self.agent_provider = None

            if self.background_provider and self.background_provider not in self.providers:
                logger.warning("Background provider '%s' not found in loaded providers", self.background_provider)
                self.background_provider = None

        except Exception as e:
            logger.error("Error parsing provider config file: %s", e, exc_info=True)

    def save_config(self, config_dict: Dict[str, Any]):
        """
        Save provider configuration to JSON file and reload providers.

        Preserves the loaded Whisper model state across reloads (v0.14.1+).

        Args:
            config_dict: Dictionary containing providers configuration
        """
        try:
            # Preserve Whisper model state before clearing providers
            whisper_state = {}
            for alias, provider in list(self.providers.items()):
                if provider.config.get('type') == 'local_whisper':
                    if hasattr(provider, 'is_model_loaded') and provider.is_model_loaded():
                        # Save the loaded state
                        whisper_state[alias] = {
                            'model_loaded': True,
                            'pipeline': provider.pipeline,
                            'device': getattr(provider, '_detected_device', provider.device),
                            'load_lock': getattr(provider, '_load_lock', None)
                        }
                        logger.debug(f"Preserving loaded Whisper model state for '{alias}'")

            with open(self.config_path, 'w') as f:
                json.dump(config_dict, f, indent=2)
            logger.info("Provider configuration saved to %s", self.config_path)

            # Reload providers
            self.providers.clear()
            self._load_providers_from_config()

            # Restore Whisper model state for providers that were loaded
            for alias, state in whisper_state.items():
                if alias in self.providers:
                    provider = self.providers[alias]
                    if hasattr(provider, 'pipeline') and hasattr(provider, 'model_loaded'):
                        # Restore the loaded state
                        provider.pipeline = state['pipeline']
                        provider.model_loaded = state['model_loaded']
                        if hasattr(provider, '_detected_device'):
                            provider._detected_device = state['device']
                        if state.get('load_lock'):
                            provider._load_lock = state['load_lock']
                        logger.info(f"Restored loaded Whisper model state for '{alias}' (model stays in memory)")

            # Call callback to re-inject CoreService into dpc_agent/remote_peer providers
            if self._on_providers_reload_callback:
                try:
                    self._on_providers_reload_callback()
                    logger.debug("Providers reload callback executed")
                except Exception as cb_err:
                    logger.warning("Error in providers reload callback: %s", cb_err)

        except Exception as e:
            logger.error("Error saving provider config: %s", e, exc_info=True)
            raise

    def get_active_model_name(self) -> str:
        """
        Returns the name of the currently active AI model.
        
        Returns:
            String like "llama3.1:8b" or None if no model is loaded
        """
        # Use default_provider (not active_provider)
        if not self.default_provider:
            return None
        
        # Get the provider object (not a dict, but an AIProvider instance)
        provider = self.providers.get(self.default_provider)
        if not provider:
            return None
        
        # Get the model name from the provider object
        model = provider.model
        if not model:
            return None
        
        # Get provider type from config
        provider_type = provider.config.get('type', '')
        
        # Format based on provider type
        if provider_type == 'ollama':
            return model  # e.g., "llama3.1:8b"
        elif provider_type == 'openai_compatible':
            return f"OpenAI {model}"
        elif provider_type == 'anthropic':
            return f"Claude {model}"
        else:
            return model

    def find_provider_by_model(self, model_name: str) -> str | None:
        """
        Find a provider alias by model name.

        Args:
            model_name: The model name to search for (e.g., "claude-haiku-4-5")

        Returns:
            Provider alias if found, None otherwise
        """
        for alias, provider in self.providers.items():
            if provider.model == model_name:
                return alias
        return None

    def count_tokens(self, text: str, model: str) -> int:
        """Count tokens in text for a given model.

        REFACTORED (Phase 4 - v0.12.1): Delegates to TokenCountManager
        for better separation of concerns and centralized token counting logic.

        Uses:
        - tiktoken for OpenAI/Anthropic (accurate BPE)
        - HuggingFace transformers for Ollama (accurate model-specific)
        - Character estimation fallback (4 chars ≈ 1 token)

        Args:
            text: The text to count tokens for
            model: The model name (e.g., "gpt-4", "llama3.1:8b")

        Returns:
            Token count
        """
        return self.token_count_manager.count_tokens(text, model)

    def get_context_window(self, model: str) -> int:
        """
        Get the context window size for a given model.

        Priority:
        1. Check provider config (providers.toml) for context_window field
        2. Check hardcoded MODEL_CONTEXT_WINDOWS dict
        3. Return default if not found

        Args:
            model: The model name (e.g., "gpt-4", "llama3.1:8b")

        Returns:
            Context window size in tokens
        """
        # Phase 6: Check provider config first (providers.toml can override)
        for alias, provider in self.providers.items():
            if provider.model == model:
                # Check if provider config has context_window field
                context_window_config = provider.config.get('context_window')
                if context_window_config:
                    try:
                        return int(context_window_config)
                    except (ValueError, TypeError):
                        logger.warning("Invalid context_window value in provider '%s' config: %s",
                                     alias, context_window_config)

        # Check direct match in hardcoded defaults
        if model in MODEL_CONTEXT_WINDOWS:
            return MODEL_CONTEXT_WINDOWS[model]

        # Check for partial matches (e.g., "gpt-4" matches "gpt-4-0613")
        for known_model, window_size in MODEL_CONTEXT_WINDOWS.items():
            if known_model in model or model in known_model:
                return window_size

        # Return default
        logger.warning("Context window size unknown for model '%s' - using default: %d",
                      model, MODEL_CONTEXT_WINDOWS['default'])
        return MODEL_CONTEXT_WINDOWS["default"]

    async def query(self, prompt: str, provider_alias: str | None = None, return_metadata: bool = False,
                    images: Optional[List[Dict[str, Any]]] = None, **kwargs):
        """
        Routes a query to the specified provider, or auto-selects based on query type.

        Auto-selection logic (when provider_alias is None):
        - If images present and vision_provider configured → use vision_provider
        - If images present and no vision_provider → find first vision-capable provider
        - If no images → use default_provider

        Args:
            prompt: The prompt to send to the LLM
            provider_alias: Optional provider alias to use (overrides auto-selection)
            return_metadata: If True, returns dict with 'response', 'provider', 'model', 'tokens_used', 'model_max_tokens'. If False, returns just the response string.
            images: Optional list of image dicts for vision API (multimodal queries). Each dict should contain:
                - path: str (absolute path to image file)
                - mime_type: str (e.g., "image/png")
                - base64: str (optional, if already encoded)
            **kwargs: Additional parameters passed to vision API (temperature, max_tokens, etc.)

        Returns:
            str if return_metadata=False, dict if return_metadata=True
        """
        # Auto-select provider based on query type
        if provider_alias is None:
            if images:
                # Vision query: prefer vision_provider, fallback to first vision-capable
                if self.vision_provider and self.vision_provider in self.providers:
                    alias_to_use = self.vision_provider
                    logger.info("Auto-selected vision provider '%s' for image query", alias_to_use)
                else:
                    # Find first vision-capable provider
                    alias_to_use = None
                    for alias, provider in self.providers.items():
                        if provider.supports_vision():
                            alias_to_use = alias
                            logger.info("Auto-selected vision-capable provider '%s' (no vision_provider configured)", alias_to_use)
                            break

                    if not alias_to_use:
                        raise ValueError("No vision-capable provider found. Please configure a vision_provider or add a vision-capable model.")
            else:
                # Text-only query: use default provider
                alias_to_use = self.default_provider
        else:
            # Explicit provider specified
            alias_to_use = provider_alias

        if not alias_to_use:
            raise ValueError("No provider specified and no default provider is set.")

        if alias_to_use not in self.providers:
            raise ValueError(f"Provider '{alias_to_use}' is not configured or failed to load.")

        provider = self.providers[alias_to_use]

        # Check if vision is requested but provider doesn't support it
        if images:
            if not provider.supports_vision():
                raise ValueError(f"Provider '{alias_to_use}' (model: {provider.model}) does not support vision API. "
                               f"Use a vision-capable model like gpt-4o, gpt-4-turbo, or claude-3+.")
            logger.info("Routing vision query to provider '%s' with model '%s' (%d images)",
                       alias_to_use, provider.model, len(images))
            response = await provider.generate_with_vision(prompt, images, **kwargs)
        else:
            logger.info("Routing query to provider '%s' with model '%s'", alias_to_use, provider.model)
            response = await provider.generate_response(prompt, **kwargs)

        # Check if this is a thinking model and extract thinking content
        thinking_content = None
        thinking_tokens = None
        if provider.supports_thinking():
            logger.info("Provider '%s' supports thinking mode", provider.model)

            # First, check if provider stores thinking separately (e.g., Claude extended thinking)
            if hasattr(provider, 'get_last_thinking'):
                thinking_content = provider.get_last_thinking()
                if thinking_content:
                    logger.info("Retrieved stored thinking content (%d chars)", len(thinking_content))

            # If no stored thinking, try parsing <think\> tags from response (e.g., DeepSeek R1)
            if not thinking_content:
                response, thinking_content = parse_thinking_tags(response)
                if thinking_content:
                    logger.info("Parsed thinking tags from response (%d chars)", len(thinking_content))

            if thinking_content:
                thinking_tokens = self.count_tokens(thinking_content, provider.model)
        else:
            logger.debug("Provider '%s' does not support thinking mode", provider.model)

        if return_metadata:
            # Count tokens in prompt and response
            prompt_tokens = self.count_tokens(prompt, provider.model)
            response_tokens = self.count_tokens(response, provider.model)
            total_tokens = prompt_tokens + response_tokens

            # Get model's context window
            context_window = self.get_context_window(provider.model)

            return {
                "response": response,
                "provider": alias_to_use,
                "model": provider.model,
                "tokens_used": total_tokens,
                "prompt_tokens": prompt_tokens,
                "response_tokens": response_tokens,
                "model_max_tokens": context_window,
                "vision_used": bool(images),  # Indicate if vision API was used
                "thinking": thinking_content,  # Thinking/reasoning content (if any)
                "thinking_tokens": thinking_tokens,  # Tokens used for thinking
            }
        return response

    async def shutdown(self) -> None:
        """
        Shutdown all providers gracefully.

        Closes async HTTP clients to prevent 'Event loop is closed' errors
        during application shutdown.
        """
        logger.info("Shutting down LLMManager...")
        for alias, provider in self.providers.items():
            if hasattr(provider, 'close'):
                try:
                    await provider.close()
                except Exception as e:
                    logger.warning(f"Error closing provider '{alias}': {e}")
            if hasattr(provider, 'shutdown'):
                try:
                    await provider.shutdown()
                except Exception as e:
                    logger.warning(f"Error shutting down provider '{alias}': {e}")
        logger.info("LLMManager shutdown complete")

# --- Self-testing block ---
async def main_test():
    logger.info("--- Testing LLMManager ---")

    # Create a dummy providers.json for testing
    dummy_config = {
        "default_provider": "local_ollama",
        "providers": [
            {
                "alias": "local_ollama",
                "type": "ollama",
                "model": "llama3.1:8b",
                "host": "http://127.0.0.1:11434"
            }
        ]
    }

    dpc_dir = Path.home() / ".dpc"
    dpc_dir.mkdir(exist_ok=True)
    test_config_path = dpc_dir / "providers.json"
    with open(test_config_path, 'w') as f:
        json.dump(dummy_config, f, indent=2)

    try:
        manager = LLMManager(config_path=test_config_path)

        if not manager.providers:
            logger.warning("No providers were loaded - cannot run test query")
            return

        logger.info("Testing query with default provider")
        response = await manager.query("What is the capital of France?")
        logger.info("Response: %s", response)

        logger.info("Testing query with specified provider")
        response = await manager.query("What is the capital of Germany?", provider_alias="local_ollama")
        logger.info("Response: %s", response)

    except Exception as e:
        logger.error("An error occurred during testing: %s", e, exc_info=True)
    finally:
        # Clean up the dummy config
        if test_config_path.exists():
            test_config_path.unlink()
        logger.info("--- Test finished ---")

if __name__ == '__main__':
    # To run this test:
    # 1. Make sure Ollama is running.
    # 2. Navigate to `dpc-client/core/`
    # 3. Run: `poetry run python dpc_client_core/llm_manager.py`
    asyncio.run(main_test())