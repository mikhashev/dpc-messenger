# dpc-client/core/dpc-client_core/llm_manager.py

import os
import json
import asyncio
import inspect
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable, Tuple

from .providers import (
    AIProvider, ModelNotCachedError, parse_thinking_tags,
    OPENAI_THINKING_MODELS, ANTHROPIC_THINKING_MODELS,
    OllamaProvider, OLLAMA_VISION_MODELS, OLLAMA_THINKING_MODELS,
    OpenAICompatibleProvider, AnthropicProvider, ZaiProvider,
    DeepSeekProvider,
    LlamaServerProvider,
    LocalWhisperProvider, RemotePeerProvider, DpcAgentProvider,
    GeminiProvider, GitHubModelsProvider, GigaChatProvider,
)
from .node_ledger import OUTPUT_INCLUDES_THINKING, THINKING_SOURCES
from .providers.base import normalize_reasoning_effort

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
    # One Z.AI type, on the prepaid platform API (api/paas/v4). The two it replaces
    # both drew the GLM Coding Plan subscription — `zai` through api/anthropic and
    # `zai_coding` through api/coding/paas/v4 — which the vendor licenses only to its
    # own list of supported tools, and this product is not on it. A config still
    # naming `zai_coding` fails to load loudly rather than silently falling back,
    # which is the behaviour we want: the alias has to be repointed, not inherited.
    "zai": ZaiProvider,
    "deepseek": DeepSeekProvider,  # DeepSeek pay-per-token (OpenAI-compatible, V4 thinking)
    "llamacpp_server": LlamaServerProvider,  # DPC-owned llama-server child (ADR-040 route b2)
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

    # GLM-5 series (standalone-API window unconfirmed; [1m] 1M variant is Coding-Plan-only.
    # Conservative fallback — set context_window in providers.json to override per provider.)
    "glm-5": 200000,
    "glm-5.1": 204800,
    "glm-5.2": 200000,
    "glm-5-turbo": 200000,

    # Default fallback
    "default": 4096
}

def flatten_messages(messages: List[Dict[str, Any]], system: Any = "") -> str:
    """The Anthropic-shaped conversation as the one prompt string a provider
    whose only entry point is a prompt can be given.

    The two existing pieces, composed rather than reimplemented: the
    providers' `system`-and-blocks converter, then the agent adapter's
    role-marker rendering. The imports are deferred because
    `dpc_agent.llm_adapter` names `LLMManager`.
    """
    from .dpc_agent.llm_adapter import messages_to_prompt
    from .providers.ollama_provider import OllamaProvider

    return messages_to_prompt(OllamaProvider._anthropic_to_openai_messages(system, messages))


def entry_point_for(provider: Any, *, tools: bool, streaming: bool, images: bool = False) -> Tuple[str, Any]:
    """`(name, bound method or None)`: the one provider entry point
    `query_messages` calls for a request shaped like this.

    The three-way choice lives here rather than in the `if` below so that a
    caller standing in front of the door — the gateway, which refuses by name
    what it cannot carry — asks about the same path that will actually run.
    None is «this provider has no entry point for this request»: for tools that
    is the refusal `query_messages` already raised.

    `images` is image blocks inside the turns. Only `generate_with_tools` takes
    the turns un-flattened, so only it can carry them, and only for a provider
    whose `supports_vision()` says yes; the two prompt paths render text and
    would lose the picture, so with images they answer None as well.
    """
    if tools:
        method = getattr(provider, "generate_with_tools", None)
        if images and method is not None and not provider.supports_vision():
            method = None
        return "generate_with_tools", method
    if streaming and hasattr(provider, "generate_response_stream"):
        return "generate_response_stream", None if images else provider.generate_response_stream
    return "generate_response", None if images else getattr(provider, "generate_response", None)


def accepts_reasoning_effort(entry_point: Any) -> bool:
    """Whether one provider entry point takes the shared effort word.

    Asked of the signature, not of a table of provider names: the three entry
    points were written at different times and only some of them grew the
    parameter — `ZaiProvider.generate_with_tools` has neither it nor `**kwargs`,
    so handing it the word raises `TypeError` deep inside the call. A path that
    cannot take the word must be refused by name before the call, never sent
    the request with the word dropped (ADR-041 D4).
    """
    if entry_point is None:
        return False
    try:
        parameters = inspect.signature(entry_point).parameters
    except (TypeError, ValueError):  # a builtin or a C callable: assume not
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return "reasoning_effort" in parameters


def reported_served_effort(provider: Any) -> Optional[str]:
    """The rung the provider says its last call ran on, or None.

    The provider is the only party that knows: a door derives the word from
    what it passed or from the alias's configuration, and an entry point may
    run another rung — llama-server's vision path runs with thinking off where
    no rung was named, whatever the alias configures for its text turns.

    None is «no word came back», which is every provider with no effort channel
    and every call whose usage the vendor did not report. The door's own
    derivation stands there, unchanged.
    """
    word = (provider.get_last_usage() or {}).get("served_effort")
    return word if isinstance(word, str) and word else None


def reported_thinking_source(usage: Dict[str, Any]) -> Optional[str]:
    """The provenance word from a provider's usage dict, or None where it has
    none: a reasoning count with no word behind it is left unattributed rather
    than credited to the engine that may not have made it."""
    word = usage.get("thinking_source")
    return word if word in THINKING_SOURCES else None


def reported_counts(provider: Any) -> Optional[Tuple[int, int, str]]:
    """`(prompt, completion, output_includes_thinking)` as the engine reported
    them, or None where it reported nothing countable.

    The recount this stands in front of is an estimate over the visible text: it
    never sees the system prompt, the chat template or an image's tokens, and it
    counts an answer the thinking was already taken out of — the numbers a
    tariff would be charged on. The convention travels with the counts, because
    a number made under `includes` and billed as `excludes` pays for the
    reasoning twice.
    """
    usage = provider.get_last_usage() or {}
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    convention = usage.get("output_includes_thinking")
    return prompt, completion, convention if convention in OUTPUT_INCLUDES_THINKING else "unknown"


def _tool_use_block(call: Any) -> Dict[str, Any]:
    """One returned tool call as an Anthropic `tool_use` block. Providers hand
    these back as `SimpleNamespace(id, name, input)`; a mapping is read too."""
    read = (lambda key: call.get(key)) if isinstance(call, dict) else (lambda key: getattr(call, key, None))
    return {"type": "tool_use", "id": read("id"), "name": read("name"), "input": read("input") or {}}


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
        self.knowledge_provider: str | None = None  # Extracts knowledge; unset means the conversation's own model

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
                        "model": "openai/whisper-large-v3-turbo",
                        "device": "auto",
                        "compile_model": True,
                        "language": "auto",
                        "task": "transcribe",
                        "lazy_loading": True,
                        "_note": "Local Whisper transcription via PyTorch (CUDA/MPS/CPU auto-detect)"
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
            # Absent means «walk the chain», not «use the text default» —
            # the extraction prompt carries the whole conversation.
            self.knowledge_provider = config.get("knowledge_provider")

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

            if self.knowledge_provider and self.knowledge_provider not in self.providers:
                logger.warning("Knowledge provider '%s' not found in loaded providers", self.knowledge_provider)
                self.knowledge_provider = None

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

            preserved_managers = {
                alias: provider._managers
                for alias, provider in list(self.providers.items())
                if getattr(provider, "_managers", None)
            }

            # Reload providers
            self.providers.clear()
            self._load_providers_from_config()

            # A dropped alias takes its provider object with it; the child it started
            # is only reachable through the supervisor registry after that.
            from .providers.llamacpp_server_provider import retire_absent
            retire_absent(self.providers.keys())

            for alias, managers in preserved_managers.items():
                new_provider = self.providers.get(alias)
                if new_provider is not None and hasattr(new_provider, "_managers"):
                    new_provider._managers.update(managers)
                    logger.info("Preserved %d agent manager(s) for '%s' across providers reload", len(managers), alias)

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

    def lookup_context_window(self, model: str) -> Optional[int]:
        """
        Strict context window lookup: returns None when the model is unknown
        (no provider config override and no MODEL_CONTEXT_WINDOWS match),
        so callers can distinguish "unknown" from a real window size.

        Priority:
        1. Check provider config (providers.toml) for context_window field
        2. Check hardcoded MODEL_CONTEXT_WINDOWS dict (exact, then partial match)
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

        return None

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
        window = self.lookup_context_window(model)
        if window is not None:
            return window

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
        thinking_source = None
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
                # Prefer the number the provider was given by the API over one we
                # compute again from the text. DeepSeek reports
                # `completion_tokens_details.reasoning_tokens` — the count it bills
                # — and this used to ignore it and re-count with `count_tokens`,
                # which for a model name carrying no `gpt`/`claude` and no colon
                # falls through to `len(text) // 4`. On a measured call that read
                # 190 on screen where the API had said 168: an estimate displayed
                # beside a measurement we already held.
                #
                # `get_last_usage()` is on `AIProvider`, so this needs no guard and
                # no knowledge of which provider answered. Providers that estimate
                # the split themselves (llama-server, when the server reports none)
                # put their estimate in the same field — still their own number over
                # the same text, and better than a second opinion computed here.
                usage = provider.get_last_usage() or {}
                reported = usage.get("reasoning_tokens")
                if isinstance(reported, int) and reported > 0:
                    thinking_tokens = reported
                    thinking_source = reported_thinking_source(usage)
                else:
                    thinking_tokens = self.count_tokens(thinking_content, provider.model)
                    thinking_source = "estimated"
        else:
            logger.debug("Provider '%s' does not support thinking mode", provider.model)

        if return_metadata:
            counted = reported_counts(provider)
            if counted is None:
                prompt_tokens = self.count_tokens(prompt, provider.model)
                response_tokens = self.count_tokens(response, provider.model)
                counts_source, output_includes_thinking = "ours", "excludes"
            else:
                prompt_tokens, response_tokens, output_includes_thinking = counted
                counts_source = "engine"
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
                # ... and who made that number: the engine where it reported the
                # split, this door or the provider where one estimated it, None
                # where nothing said. A row copies the word; it never infers one.
                "thinking_source": thinking_source,
                # Whose numbers those are, and what is inside them: the engine's
                # where it reported any, and the recount over the visible text —
                # which the thinking is already out of — only where it did not.
                "counts_source": counts_source,
                "output_includes_thinking": output_includes_thinking,
                # The effort word this door passed to the provider, normalised
                # as the provider will read it; None is «none was applied»,
                # which is not `off`. What the provider's own configuration
                # then does is the provider's, and is not claimed here.
                "served_effort": normalize_reasoning_effort(kwargs.get("reasoning_effort")),
                # ... and the rung the provider says it ran on, which is the
                # word a usage row wants and the only one that cannot be wrong.
                "provider_served_effort": reported_served_effort(provider),
            }
        return response

    async def query_messages(
        self,
        messages: List[Dict[str, Any]],
        *,
        system: Any = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        on_chunk: Optional[Callable] = None,
        conversation_id: Optional[str] = None,
        provider_alias: str | None = None,
        return_metadata: bool = False,
        reasoning_effort: Optional[str] = None,
    ):
        """A conversation, optional tools and an optional chunk callback in;
        `query(return_metadata=True)`'s dict plus five keys out.

        Beside `query`, not instead of it: `query` takes a flat prompt, so a
        caller above it must flatten `messages`, and a provider that streams
        or calls tools natively is unreachable from there. `messages` is the
        Anthropic Messages shape `generate_with_tools` already accepts, which
        is also the only provider entry point taking the list un-flattened.
        `finish_reason` stays in the vocabulary the providers report it in.

        `reasoning_effort` is the word from `REASONING_EFFORTS` (or `off`) the
        caller asks the model to think at: it travels to whichever of the three
        entry points this request takes and comes back as `served_effort`,
        exactly as the same kwarg does through `query`. A path whose signature
        cannot take the word raises rather than dropping it — an answer that
        thought less than it was asked to must not come back looking like one
        that did. `None` asks for nothing and reaches no provider, which is
        what every caller written before this parameter existed keeps doing.
        """
        if not isinstance(messages, list) or not messages:
            raise ValueError("query_messages needs a non-empty list of messages.")

        alias_to_use = provider_alias or self.default_provider
        if not alias_to_use:
            raise ValueError("No provider specified and no default provider is set.")
        if alias_to_use not in self.providers:
            raise ValueError(f"Provider '{alias_to_use}' is not configured or failed to load.")
        provider = self.providers[alias_to_use]

        # Rendered on every route, not only the ones that send it: the counts
        # below are then `query`'s counts over `query`'s prompt, so a usage row
        # built from this dict is the row the conversation left before.
        prompt_text = flatten_messages(messages, system)

        tool_calls: List[Dict[str, Any]] = []
        path_usage: Dict[str, Any] = {}
        # Image blocks in the turns, including those a tool returned inside its result.
        image_count = 0
        for m in messages:
            content = m.get("content") if isinstance(m, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                inner = block.get("content") if block.get("type") == "tool_result" else None
                image_count += sum(
                    1 for b in [block, *(inner if isinstance(inner, list) else [])]
                    if isinstance(b, dict) and b.get("type") == "image"
                )
        path, entry_point = entry_point_for(
            provider, tools=bool(tools), streaming=on_chunk is not None, images=image_count > 0,
        )
        if tools and getattr(provider, "generate_with_tools", None) is None:
            raise ValueError(
                f"Provider '{alias_to_use}' (model: {provider.model}) has no native "
                f"tool-calling path, and {len(tools)} tool(s) were asked for. Use an "
                "alias whose provider implements generate_with_tools."
            )
        if image_count and entry_point is None:
            raise ValueError(
                f"Provider '{alias_to_use}' (model: {provider.model}) cannot take the "
                f"{image_count} image(s) in this conversation on its {path} path: images "
                "travel in the turns only through generate_with_tools, to a provider "
                "whose supports_vision() is true. Use a vision-capable alias with tools, "
                "or send the conversation without images."
            )
        effort_kwargs: Dict[str, Any] = {}
        if reasoning_effort is not None:
            if not accepts_reasoning_effort(entry_point):
                raise ValueError(
                    f"Provider '{alias_to_use}' (model: {provider.model}) takes no reasoning "
                    f"effort on its {path} path, and '{reasoning_effort}' was asked for. Send "
                    "the request without an effort, or to an alias whose provider takes one "
                    "on this path."
                )
            effort_kwargs["reasoning_effort"] = reasoning_effort
        if path == "generate_with_tools":
            logger.info("Routing tool query to provider '%s' with model '%s' (%d tools)",
                        alias_to_use, provider.model, len(tools))
            raw = await entry_point(
                messages, tools, system=system, on_chunk=on_chunk, conversation_id=conversation_id,
                **effort_kwargs,
            ) or {}
            response = raw.get("content") or ""
            tool_calls = [_tool_use_block(call) for call in raw.get("tool_calls_raw") or []]
            path_usage = raw.get("usage") or {}
            streamed, flattened, tools_used = False, False, True
        elif path == "generate_response_stream":
            logger.info("Routing streaming query to provider '%s' with model '%s'",
                        alias_to_use, provider.model)
            response = await entry_point(prompt_text, on_chunk, conversation_id, **effort_kwargs)
            streamed, flattened, tools_used = True, True, False
        else:
            logger.info("Routing query to provider '%s' with model '%s'", alias_to_use, provider.model)
            response = await entry_point(prompt_text, **effort_kwargs)
            streamed, flattened, tools_used = False, True, False
            if on_chunk is not None:
                logger.info("Provider '%s' has no generate_response_stream: the answer is "
                            "delivered whole and 'streamed' says so", alias_to_use)
                await on_chunk(response, conversation_id)

        # None means the provider reported nothing, and stays None: a constant
        # here is what leaves a tool round indistinguishable from a finished
        # sentence, which is the defect this door exists to end.
        finish_reason = (provider.get_last_usage() or {}).get("finish_reason")
        if finish_reason is None:
            finish_reason = path_usage.get("finish_reason")

        # The rule `query` applies, including the part that matters: the
        # reasoning-token count is the one the vendor reported, never one
        # recomputed from the text. The two copies must move together.
        thinking_content = None
        thinking_tokens = None
        thinking_source = None
        if provider.supports_thinking():
            if hasattr(provider, 'get_last_thinking'):
                thinking_content = provider.get_last_thinking()
            if not thinking_content:
                response, thinking_content = parse_thinking_tags(response)
            if thinking_content:
                usage = provider.get_last_usage() or {}
                reported = usage.get("reasoning_tokens")
                if isinstance(reported, int) and reported > 0:
                    thinking_tokens = reported
                    thinking_source = reported_thinking_source(usage)
                else:
                    thinking_tokens = self.count_tokens(thinking_content, provider.model)
                    thinking_source = "estimated"

        if return_metadata:
            counted = reported_counts(provider)
            if counted is None:
                prompt_tokens = self.count_tokens(prompt_text, provider.model)
                response_tokens = self.count_tokens(response, provider.model)
                counts_source, output_includes_thinking = "ours", "excludes"
            else:
                prompt_tokens, response_tokens, output_includes_thinking = counted
                counts_source = "engine"
            return {
                # `query`'s ten keys, because a usage row is built from them.
                "response": response,
                "provider": alias_to_use,
                "model": provider.model,
                "tokens_used": prompt_tokens + response_tokens,
                "prompt_tokens": prompt_tokens,
                "response_tokens": response_tokens,
                "model_max_tokens": self.get_context_window(provider.model),
                "vision_used": image_count > 0,  # a refusal above leaves no other way here
                "thinking": thinking_content,
                "thinking_tokens": thinking_tokens,
                "thinking_source": thinking_source,  # `query`'s rule, same two copies
                "counts_source": counts_source,  # same rule as `query`
                "output_includes_thinking": output_includes_thinking,
                # The effort word this door passed to the provider, normalised
                # as the provider will read it — `query`'s rule, and the two
                # copies must move together. None is «no effort control was
                # applied», which is not `off`.
                "served_effort": normalize_reasoning_effort(reasoning_effort),
                "provider_served_effort": reported_served_effort(provider),
                # ... and what the provider was given, did, and stopped on.
                "streamed": streamed,
                "flattened": flattened,
                "tools_used": tools_used,
                "tool_calls": tool_calls,
                "finish_reason": finish_reason,
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

        # A llama-server child can outlive the provider that started it — a config
        # reload drops provider objects without closing them — so the last word on
        # what is still running belongs to the supervisor registry, not to this dict.
        from .providers.llamacpp_server_provider import stop_all_supervisors
        orphaned = await stop_all_supervisors()
        if orphaned:
            logger.warning(
                "Stopped %d llama-server child(ren) no provider was holding any more: %s",
                len(orphaned), ", ".join(orphaned),
            )
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