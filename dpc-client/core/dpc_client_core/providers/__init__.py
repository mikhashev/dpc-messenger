# dpc_client_core/providers/__init__.py — re-exports for backward compat
from .base import AIProvider, ModelNotCachedError, parse_thinking_tags
from .base import OPENAI_THINKING_MODELS, ANTHROPIC_THINKING_MODELS
from .ollama_provider import OllamaProvider, OLLAMA_VISION_MODELS, OLLAMA_THINKING_MODELS
from .openai_provider import OpenAICompatibleProvider
from .anthropic_provider import AnthropicProvider
from .zai_provider import ZaiProvider
from .whisper_provider import LocalWhisperProvider
from .remote_peer_provider import RemotePeerProvider
from .dpc_agent_provider import DpcAgentProvider
from .gemini_provider import GeminiProvider
from .github_models_provider import GitHubModelsProvider
from .gigachat_provider import GigaChatProvider

__all__ = [
    "AIProvider",
    "ModelNotCachedError",
    "parse_thinking_tags",
    "OPENAI_THINKING_MODELS",
    "ANTHROPIC_THINKING_MODELS",
    "OllamaProvider",
    "OLLAMA_VISION_MODELS",
    "OLLAMA_THINKING_MODELS",
    "OpenAICompatibleProvider",
    "AnthropicProvider",
    "ZaiProvider",
    "LocalWhisperProvider",
    "RemotePeerProvider",
    "DpcAgentProvider",
    "GeminiProvider",
    "GitHubModelsProvider",
    "GigaChatProvider",
]
