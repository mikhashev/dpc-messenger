# dpc_client_core/providers/base.py
# Base class, shared exceptions, shared constants, and shared utilities for all AI providers.

import logging
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)

# --- Custom Exceptions ---

class ModelNotCachedError(Exception):
    """Raised when a model is not found in local cache and needs to be downloaded."""
    def __init__(self, model_name: str, cache_path: str, download_size_gb: float = 3.0):
        self.model_name = model_name
        self.cache_path = cache_path
        self.download_size_gb = download_size_gb
        super().__init__(f"Model '{model_name}' not found in cache: {cache_path}")

# --- Shared thinking model constants ---

OPENAI_THINKING_MODELS = [
    "o1", "o1-mini", "o1-preview", "o1-pro",
    "o3", "o3-mini", "o3-pro",
    "o4-mini",
]

ANTHROPIC_THINKING_MODELS = [
    "claude-3-7",       # Claude 3.7 Sonnet (extended thinking)
    "claude-opus-4",    # Claude Opus 4 (extended thinking)
    "claude-sonnet-4",  # Claude Sonnet 4 (extended thinking)
    "claude-haiku-4",   # Claude Haiku 4 (extended thinking)
]


def parse_thinking_tags(content: str) -> Tuple[str, Optional[str]]:
    r"""
    Parse <think\>...</think\> tags from model response content.

    Used by DeepSeek R1 and similar models that embed thinking/reasoning
    in their response using XML-style tags.

    Args:
        content: Raw response content that may contain <think\> tags

    Returns:
        Tuple of (final_content, thinking_content):
        - final_content: Content with <think\> tags removed
        - thinking_content: Extracted thinking text, or None if no tags found
    """
    import re

    # Pattern matches <think\>...</think\> with any content inside (including newlines)
    think_pattern = r'<think\s*>(.*?)</think\s*>'
    matches = re.findall(think_pattern, content, re.DOTALL | re.IGNORECASE)

    if matches:
        # Join multiple thinking blocks with newlines
        thinking = '\n'.join(match.strip() for match in matches if match.strip())

        # Remove thinking tags from final content
        final_content = re.sub(think_pattern, '', content, flags=re.DOTALL | re.IGNORECASE).strip()

        return final_content, thinking if thinking else None

    return content, None


# --- Abstract Base Class for all Providers ---

class AIProvider:
    """Abstract base class for all AI providers."""
    def __init__(self, alias: str, config: Dict[str, Any]):
        self.alias = alias
        self.config = config
        self.model = config.get("model")
        self.temperature = config.get("temperature", 0.7)  # Default temperature for creativity

    async def generate_response(self, prompt: str, **kwargs) -> str:
        """
        Generates a response from the AI model.

        Args:
            prompt: The input prompt text
            **kwargs: Additional arguments (e.g., conversation_id) for compatibility

        Returns:
            The AI model's response text
        """
        raise NotImplementedError

    def supports_vision(self) -> bool:
        """Returns True if this provider supports vision API (multimodal queries)."""
        return False

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        Generates a response from the AI model with image inputs (vision API).

        Args:
            prompt: Text prompt
            images: List of image dicts with keys:
                - path: str (absolute path to image file)
                - mime_type: str (e.g., "image/png")
                - base64: str (optional, if already encoded)
            **kwargs: Additional parameters (temperature, max_tokens, etc.)

        Returns:
            str: AI response text

        Raises:
            NotImplementedError: If provider doesn't support vision
        """
        raise NotImplementedError(f"Vision API not implemented for {self.__class__.__name__}")

    def supports_thinking(self) -> bool:
        r"""
        Returns True if this provider supports thinking/reasoning mode.

        Thinking mode models perform extended reasoning before producing
        their final response. Examples include:
        - DeepSeek R1 (with <think\> tags)
        - Claude Extended Thinking (Claude 3.7+, Claude 4+)
        - OpenAI o1/o3 (reasoning models)

        Returns:
            bool: True if thinking mode is supported, False by default
        """
        return False

    def get_thinking_params(self) -> Dict[str, Any]:
        """
        Return provider-specific thinking parameters.

        Override this method to return parameters like:
        - budget_tokens (Claude)
        - reasoning_effort (OpenAI o1/o3)

        Returns:
            Dict with thinking parameters, empty by default
        """
        return {}

    def get_state(self) -> dict:
        return {"alias": self.alias, "model": self.model, "type": self.config.get("type")}
