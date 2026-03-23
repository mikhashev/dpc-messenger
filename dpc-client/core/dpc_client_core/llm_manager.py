# dpc-client/core/dpc-client_core/llm_manager.py

import os
import json
import asyncio
import logging
import base64
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from dpc_client_core.service import CoreService
    from dpc_client_core.managers.agent_manager import DpcAgentManager

# Import client libraries
from openai import AsyncOpenAI
from anthropic import AsyncAnthropic
import ollama

logger = logging.getLogger(__name__)

# Token counting
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logger.warning("tiktoken not available - token counting will use estimation for all models")

# --- Custom Exceptions ---

class ModelNotCachedError(Exception):
    """Raised when a model is not found in local cache and needs to be downloaded."""
    def __init__(self, model_name: str, cache_path: str, download_size_gb: float = 3.0):
        self.model_name = model_name
        self.cache_path = cache_path
        self.download_size_gb = download_size_gb
        super().__init__(f"Model '{model_name}' not found in cache: {cache_path}")

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

# --- Concrete Provider Implementations ---

# Vision-capable Ollama models (for auto-detection)
OLLAMA_VISION_MODELS = [
    "qwen3-vl",         # Qwen3-VL (all variants: 2b, 4b, 8b, 30b, etc.)
    "llava",            # LLaVA variants
    "llama3.2-vision",  # Llama 3.2 vision models
    "ministral-3",      # Ministral 3 vision models (3b, 8b, 14b)
    "bakllava",         # BakLLaVA
    "moondream",        # Moondream
]

# Thinking/reasoning models (for auto-detection)
# These models perform extended reasoning before producing their final response
OLLAMA_THINKING_MODELS = [
    "deepseek-r1",      # DeepSeek R1 (all variants)
    "deepseek-reasoner",
]

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

class OllamaProvider(AIProvider):
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        self.client = ollama.AsyncClient(host=config.get("host"))

    def supports_vision(self) -> bool:
        """Check if this Ollama model supports vision/multimodal inputs."""
        return any(vm in self.model.lower() for vm in OLLAMA_VISION_MODELS)

    def supports_thinking(self) -> bool:
        """Check if this Ollama model is a thinking/reasoning model."""
        return any(tm in self.model.lower() for tm in OLLAMA_THINKING_MODELS)

    async def generate_response(self, prompt: str, **kwargs) -> str:
        try:
            message = {'role': 'user', 'content': prompt}

            # Build options dict for custom parameters
            options = {}
            if self.config.get("context_window"):
                options["num_ctx"] = self.config["context_window"]

            # Add temperature if specified (use kwargs override or config default)
            temp = kwargs.get("temperature", self.temperature)
            if temp != 0.7:  # Only pass if non-default to avoid unnecessary API params
                options["temperature"] = temp

            # Add a timeout to the request
            response = await asyncio.wait_for(
                self.client.chat(
                    model=self.model,
                    messages=[message],
                    options=options if options else None
                ),
                timeout=60.0 # 60 second timeout
            )
            return response['message']['content']
        except asyncio.TimeoutError:
            raise RuntimeError(f"Ollama provider '{self.alias}' timed out after 60 seconds.")
        except Exception as e:
            raise RuntimeError(f"Ollama provider '{self.alias}' failed: {e}") from e

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        Ollama vision API using images parameter.
        Docs: https://docs.ollama.com/capabilities/vision

        Args:
            prompt: Text prompt
            images: List of dicts with keys:
                - path: str (file path)
                - base64: str (optional, base64 data)
                - mime_type: str (optional)
            **kwargs: Additional parameters (temperature, timeout, etc.)

        Returns:
            str: AI response text
        """
        try:
            # Build image list (Ollama accepts paths or base64)
            image_inputs = []
            for img in images:
                if "base64" in img:
                    # Use base64 data if available
                    base64_data = img["base64"]
                    # Strip data URL prefix if present (data:image/png;base64,...)
                    if base64_data.startswith("data:"):
                        base64_data = base64_data.split(",", 1)[1]
                    image_inputs.append(base64_data)
                elif "path" in img:
                    # Use file path (Ollama SDK handles reading)
                    image_inputs.append(str(img["path"]))
                else:
                    raise ValueError("Image must have 'path' or 'base64' key")

            # Build message with images
            message = {
                'role': 'user',
                'content': prompt,
                'images': image_inputs
            }

            # Build options dict for custom parameters
            options = {}
            if self.config.get("context_window"):
                options["num_ctx"] = self.config["context_window"]

            # Vision queries may take longer - use configurable timeout
            timeout = kwargs.get("timeout", 120.0)  # Default 120s for vision

            response = await asyncio.wait_for(
                self.client.chat(
                    model=self.model,
                    messages=[message],
                    options=options if options else None
                ),
                timeout=timeout
            )
            return response['message']['content']
        except asyncio.TimeoutError:
            raise RuntimeError(f"Ollama vision query '{self.alias}' timed out after {kwargs.get('timeout', 120)}s.")
        except Exception as e:
            raise RuntimeError(f"Ollama vision API failed for '{self.alias}': {e}") from e

    async def get_model_info(self) -> Dict[str, Any]:
        """Query Ollama for model information including parameters.

        Returns:
            Dict containing:
                - modelfile: Raw modelfile content
                - parameters: Model parameters string
                - num_ctx: Parsed context window size (or None)
                - details: Model details (family, parameter_size, etc.)
        """
        try:
            response = await self.client.show(model=self.model)

            # Parse num_ctx from modelfile
            num_ctx = None
            modelfile = response.get('modelfile', '')
            if modelfile:
                num_ctx = self._parse_num_ctx_from_modelfile(modelfile)

            # Convert details to dict if it's a Pydantic model
            details = response.get('details')
            if details:
                # Handle Pydantic models (they have model_dump method)
                if hasattr(details, 'model_dump'):
                    details = details.model_dump(exclude_none=True)
                elif hasattr(details, 'dict'):
                    details = details.dict(exclude_none=True)
                elif isinstance(details, dict):
                    details = details
                else:
                    details = {}
            else:
                details = {}

            # Convert modified_at datetime to string if present
            modified_at = response.get('modified_at')
            if modified_at and hasattr(modified_at, 'isoformat'):
                modified_at = modified_at.isoformat()

            return {
                "modelfile": modelfile,
                "parameters": response.get('parameters', ''),
                "num_ctx": num_ctx,
                "details": details,
                "template": response.get('template', ''),
                "modified_at": modified_at,
            }
        except Exception as e:
            raise RuntimeError(f"Failed to get model info for '{self.model}': {e}") from e

    @staticmethod
    def _parse_num_ctx_from_modelfile(modelfile: str) -> Optional[int]:
        """Extract num_ctx parameter from modelfile string.

        Args:
            modelfile: Raw modelfile content

        Returns:
            Context window size as integer, or None if not found
        """
        import re
        match = re.search(r'PARAMETER\s+num_ctx\s+(\d+)', modelfile, re.IGNORECASE)
        return int(match.group(1)) if match else None

    async def close(self) -> None:
        """Close the Ollama async client connection."""
        if hasattr(self.client, 'close'):
            await self.client.close()
            logger.debug(f"OllamaProvider '{self.alias}': Client closed")

class OpenAICompatibleProvider(AIProvider):
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        api_key = config.get("api_key")
        if not api_key:
            api_key_env = config.get("api_key_env")
            if api_key_env:
                api_key = os.getenv(api_key_env)

        if not api_key:
            raise ValueError(f"API key not found for OpenAI compatible provider '{self.alias}'")

        self.client = AsyncOpenAI(base_url=config.get("base_url"), api_key=api_key)

    def supports_vision(self) -> bool:
        """OpenAI vision models: gpt-4o, gpt-4-turbo, gpt-4o-mini"""
        vision_models = ["gpt-4o", "gpt-4-turbo", "gpt-4o-mini"]
        return any(vm in self.model for vm in vision_models)

    def supports_thinking(self) -> bool:
        """Check if this is an OpenAI reasoning model (o1/o3 series)."""
        return any(tm in self.model.lower() for tm in OPENAI_THINKING_MODELS)

    async def generate_response(self, prompt: str, **kwargs) -> str:
        try:
            # Use kwargs override or config default for temperature
            temperature = kwargs.get("temperature", self.temperature)
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
            return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"OpenAI compatible provider '{self.alias}' failed: {e}") from e

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        OpenAI vision API using multimodal content arrays.
        Docs: https://platform.openai.com/docs/guides/vision
        """
        try:
            # Build multimodal message content
            content = [{"type": "text", "text": prompt}]

            for img in images:
                # Encode image to base64 if not already
                if "base64" in img:
                    base64_data = img["base64"]
                    # Strip data URL prefix if present
                    if base64_data.startswith("data:"):
                        base64_data = base64_data.split(",", 1)[1]
                else:
                    with open(img["path"], "rb") as f:
                        base64_data = base64.b64encode(f.read()).decode("utf-8")

                # OpenAI expects data URL format
                mime_type = img.get("mime_type", "image/png")
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{base64_data}",
                        "detail": "high"  # or "low" for faster/cheaper processing
                    }
                })

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=kwargs.get("temperature", self.temperature),
                max_tokens=kwargs.get("max_tokens", 4000)
            )
            return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"OpenAI vision API failed for '{self.alias}': {e}") from e

    async def close(self) -> None:
        """Close the AsyncOpenAI client connection."""
        if hasattr(self.client, 'close'):
            await self.client.close()
            logger.debug(f"OpenAICompatibleProvider '{self.alias}': Client closed")

class AnthropicProvider(AIProvider):
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        api_key_env = config.get("api_key_env")
        api_key = os.getenv(api_key_env) if api_key_env else None

        if not api_key:
            raise ValueError(f"API key environment variable not set for Anthropic provider '{self.alias}'")

        self.client = AsyncAnthropic(api_key=api_key)

        # Read max_tokens from config (optional, defaults to 4096  if not specified)
        # Set to None or omit from config to use model's maximum
        self.max_tokens = config.get("max_tokens", 4096)

        # Thinking/reasoning configuration (Claude Extended Thinking)
        # Claude 3.7+ and Claude 4+ support extended thinking with budget_tokens
        self.thinking_enabled = config.get("thinking", {}).get("enabled", False)
        self.thinking_budget_tokens = config.get("thinking", {}).get("budget_tokens", 10000)

        # Store last thinking content for retrieval by LLMManager
        self._last_thinking: Optional[str] = None

    def supports_vision(self) -> bool:
        """Claude 3+ models support vision"""
        vision_models = ["claude-3", "claude-opus", "claude-sonnet", "claude-haiku"]
        return any(vm in self.model for vm in vision_models)

    def supports_thinking(self) -> bool:
        """Check if this Claude model supports extended thinking (Claude 3.7+/4+)."""
        return any(tm in self.model.lower() for tm in ANTHROPIC_THINKING_MODELS)

    def get_thinking_params(self) -> Dict[str, Any]:
        """Return Claude-specific thinking parameters."""
        if self.supports_thinking() and self.thinking_enabled:
            return {
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget_tokens
                }
            }
        return {}

    async def generate_response(self, prompt: str, **kwargs) -> str:
        try:
            # Determine max_tokens value
            # When thinking is enabled, max_tokens must be > budget_tokens
            effective_max_tokens = self.max_tokens if self.max_tokens else 4096

            # Build API parameters
            api_params = {
                "model": self.model,
                "max_tokens": effective_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": kwargs.get("temperature", self.temperature),
            }

            # Add extended thinking if enabled and supported
            if self.supports_thinking() and self.thinking_enabled:
                # Ensure max_tokens > budget_tokens (API requirement)
                if effective_max_tokens <= self.thinking_budget_tokens:
                    # Set max_tokens to budget + buffer for actual response
                    effective_max_tokens = self.thinking_budget_tokens + 4096
                    api_params["max_tokens"] = effective_max_tokens
                    logger.info(f"Adjusted max_tokens to {effective_max_tokens} to exceed budget_tokens ({self.thinking_budget_tokens})")

                api_params["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget_tokens
                }
                logger.info(f"Claude extended thinking enabled (budget={self.thinking_budget_tokens} tokens)")
            elif self.supports_thinking() and not self.thinking_enabled:
                logger.info(f"Claude model {self.model} supports thinking but it's disabled in config")
            else:
                logger.debug(f"Claude model {self.model} does not support extended thinking")

            message = await self.client.messages.create(**api_params)

            # Parse content blocks - handle both thinking and text blocks
            thinking_text = None
            final_text = None

            for block in message.content:
                if hasattr(block, 'type'):
                    if block.type == "thinking":
                        thinking_text = getattr(block, 'thinking', None)
                    elif block.type == "text":
                        final_text = getattr(block, 'text', None)

            # Store thinking for retrieval by LLMManager
            self._last_thinking = thinking_text

            if thinking_text:
                logger.info(f"Claude extended thinking: {len(thinking_text)} chars")

            # Return text content (only from text blocks, never from thinking blocks)
            if final_text:
                return final_text
            elif message.content:
                # Fallback: look for any text block in content
                for block in message.content:
                    if hasattr(block, 'type') and block.type == "text" and hasattr(block, 'text'):
                        return block.text
                # No text block found - return empty rather than repr of thinking block
                logger.warning(f"No text block found in response, only thinking blocks")
                return ""
            else:
                return ""

        except Exception as e:
            raise RuntimeError(f"Anthropic provider '{self.alias}' failed: {e}") from e

    def get_last_thinking(self) -> Optional[str]:
        """Get the thinking content from the last response (for Claude extended thinking)."""
        return self._last_thinking

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        Anthropic vision API using multimodal content blocks.
        Docs: https://docs.anthropic.com/claude/docs/vision
        """
        try:
            # Build multimodal content array
            content = []

            # Add images first
            for img in images:
                # Encode image to base64 if not already
                if "base64" in img:
                    base64_data = img["base64"]
                    # Strip data URL prefix if present
                    if base64_data.startswith("data:"):
                        base64_data = base64_data.split(",", 1)[1]
                else:
                    with open(img["path"], "rb") as f:
                        base64_data = base64.b64encode(f.read()).decode("utf-8")

                mime_type = img.get("mime_type", "image/png")
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": base64_data
                    }
                })

            # Add text prompt after images
            content.append({"type": "text", "text": prompt})

            response = await self.client.messages.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=kwargs.get("temperature", self.temperature),
                max_tokens=kwargs.get("max_tokens", self.max_tokens or 4096)
            )
            return response.content[0].text
        except Exception as e:
            raise RuntimeError(f"Anthropic vision API failed for '{self.alias}': {e}") from e

    async def close(self) -> None:
        """Close the AsyncAnthropic client connection."""
        if hasattr(self.client, 'close'):
            await self.client.close()
            logger.debug(f"AnthropicProvider '{self.alias}': Client closed")

class ZaiProvider(AIProvider):
    """
    Z.AI provider for GLM models (GLM-4.7, GLM-4.6, GLM-4.5, etc.)

    Uses Anthropic-compatible endpoint (https://api.z.ai/api/anthropic)
    instead of PaaS endpoint to avoid prepaid balance requirements.

    All GLM models support extended thinking via API parameter.
    """
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        # API key handling (supports both plaintext and env var)
        api_key = config.get("api_key")
        if not api_key:
            api_key_env = config.get("api_key_env", "ZAI_API_KEY")
            if api_key_env:
                api_key = os.getenv(api_key_env)

        if not api_key:
            raise ValueError(f"API key not found for Z.AI provider '{self.alias}'")

        # Use Anthropic-compatible endpoint (same as law7-services)
        base_url = config.get("base_url", "https://api.z.ai/api/anthropic")
        self.client = AsyncAnthropic(api_key=api_key, base_url=base_url)

        # Read max_tokens from config (optional, defaults to 8192 if not specified)
        # Matches law7-services default for GLM models
        self.max_tokens = config.get("max_tokens", 8192)

        # Thinking/reasoning configuration (GLM Extended Thinking)
        # All GLM models support extended thinking with budget_tokens
        self.thinking_enabled = config.get("thinking", {}).get("enabled", True)
        self.thinking_budget_tokens = config.get("thinking", {}).get("budget_tokens", 10000)

        # Store last thinking content for retrieval by LLMManager
        self._last_thinking: Optional[str] = None

    def supports_vision(self) -> bool:
        """All GLM models support vision via Z.AI's Anthropic-compatible endpoint."""
        return True

    def supports_thinking(self) -> bool:
        """All GLM models support extended thinking."""
        return True

    def get_thinking_params(self) -> Dict[str, Any]:
        """Return GLM-specific thinking parameters."""
        if self.thinking_enabled:
            return {
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget_tokens
                }
            }
        return {}

    def get_last_thinking(self) -> Optional[str]:
        """Get the thinking content from the last response."""
        return self._last_thinking

    async def generate_response(self, prompt: str, **kwargs) -> str:
        """Generate text response using Z.AI GLM model with extended thinking"""
        try:
            # Determine max_tokens value
            # When thinking is enabled, max_tokens must be > budget_tokens
            effective_max_tokens = self.max_tokens if self.max_tokens else 8192

            # Build API parameters
            api_params = {
                "model": self.model,
                "max_tokens": effective_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": kwargs.get("temperature", self.temperature),
            }

            # Add extended thinking if enabled (all GLM models support it)
            if self.thinking_enabled:
                # Ensure max_tokens > budget_tokens (API requirement)
                if effective_max_tokens <= self.thinking_budget_tokens:
                    # Set max_tokens to budget + buffer for actual response
                    effective_max_tokens = self.thinking_budget_tokens + 4096
                    api_params["max_tokens"] = effective_max_tokens
                    logger.info(f"Adjusted max_tokens to {effective_max_tokens} to exceed budget_tokens ({self.thinking_budget_tokens})")

                api_params["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget_tokens
                }
                logger.info(f"GLM extended thinking enabled (budget={self.thinking_budget_tokens} tokens)")
            else:
                logger.debug(f"GLM extended thinking disabled for {self.model}")

            message = await self.client.messages.create(**api_params)

            # Parse content blocks - handle both thinking and text blocks
            thinking_text = None
            final_text = None

            for block in message.content:
                if hasattr(block, 'type'):
                    if block.type == "thinking":
                        thinking_text = getattr(block, 'thinking', None)
                    elif block.type == "text":
                        final_text = getattr(block, 'text', None)

            # Store thinking for retrieval by LLMManager
            self._last_thinking = thinking_text

            if thinking_text:
                logger.info(f"GLM extended thinking: {len(thinking_text)} chars")

            # Return text content (only from text blocks, never from thinking blocks)
            if final_text:
                return final_text
            elif message.content:
                # Fallback: look for any text block in content
                for block in message.content:
                    if hasattr(block, 'type') and block.type == "text" and hasattr(block, 'text'):
                        return block.text
                # No text block found - return empty rather than repr of thinking block
                logger.warning(f"No text block found in response, only thinking blocks")
                return ""
            else:
                return ""

        except Exception as e:
            raise RuntimeError(f"Z.AI provider '{self.alias}' failed: {e}") from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None
    ) -> str:
        """
        Generate text response with streaming.

        Args:
            prompt: User message text
            on_chunk: Async callback for each text chunk: await on_chunk(chunk, conversation_id)
            conversation_id: Optional conversation ID for chunk callbacks

        Returns:
            Full response text (accumulated from all chunks)
        """
        try:
            # Determine max_tokens value
            effective_max_tokens = self.max_tokens if self.max_tokens else 8192

            # Build API parameters
            api_params = {
                "model": self.model,
                "max_tokens": effective_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self.temperature,
            }

            # Add extended thinking if enabled (all GLM models support it)
            if self.thinking_enabled:
                if effective_max_tokens <= self.thinking_budget_tokens:
                    effective_max_tokens = self.thinking_budget_tokens + 4096
                    api_params["max_tokens"] = effective_max_tokens

                api_params["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget_tokens
                }
                logger.info(f"GLM streaming with thinking enabled (budget={self.thinking_budget_tokens} tokens)")
            else:
                logger.debug(f"GLM streaming without thinking for {self.model}")

            # Note: Do NOT add stream=True - messages.stream() is already a streaming method

            # Reset thinking at the start of each call (prevent stale values)
            self._last_thinking = None

            # Stream response
            full_text = ""
            thinking_text = ""

            async with self.client.messages.stream(**api_params) as stream:
                async for text in stream.text_stream:
                    full_text += text
                    # Call the chunk callback
                    if on_chunk:
                        await on_chunk(text, conversation_id)

                # After streaming, always check final message for thinking blocks.
                # text_stream only yields text tokens; thinking blocks are separate
                # and must be read from the final message.
                if self.thinking_enabled:
                    try:
                        final_message = await stream.get_final_message()
                        for block in final_message.content:
                            if hasattr(block, 'type') and block.type == "thinking":
                                thinking_text = getattr(block, 'thinking', "")
                                if thinking_text:
                                    self._last_thinking = thinking_text
                                    logger.info(f"GLM streaming thinking: {len(thinking_text)} chars")
                    except Exception as e:
                        logger.debug(f"Could not get final message for thinking: {e}")

            # If no text produced but thinking was done, return empty string so the
            # agent loop can detect and retry with a re-prompt instead of sending
            # the useless placeholder to the user.
            if not full_text and thinking_text:
                logger.warning("GLM extended thinking produced no text output, will retry for text response")
                full_text = ""
            elif not full_text:
                logger.warning("GLM streaming produced no output")

            logger.info(f"GLM streaming completed: {len(full_text)} chars")
            return full_text

        except RuntimeError as e:
            # Handle "Event loop is closed" during shutdown gracefully
            if "Event loop is closed" in str(e):
                logger.debug(f"Z.AI streaming cleanup skipped (event loop closed)")
                return full_text  # Return what we have
            raise
        except Exception as e:
            logger.error(f"Z.AI streaming failed: {e}", exc_info=True)
            raise RuntimeError(f"Z.AI streaming provider '{self.alias}' failed: {e}") from e

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        Z.AI vision API for GLM-V models (glm-4.6v-flash, glm-4.5v, glm-4.0v)
        Uses Anthropic-compatible image format.
        """
        try:
            # Build multimodal message content (Anthropic format)
            content = [{"type": "text", "text": prompt}]

            for img in images:
                # Encode image to base64 if not already
                if "base64" in img:
                    base64_data = img["base64"]
                    if base64_data.startswith("data:"):
                        base64_data = base64_data.split(",", 1)[1]
                else:
                    with open(img["path"], "rb") as f:
                        base64_data = base64.b64encode(f.read()).decode("utf-8")

                mime_type = img.get("mime_type", "image/png")
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": base64_data
                    }
                })

            response = await self.client.messages.create(
                model=self.model,
                max_tokens=kwargs.get("max_tokens", 8192),
                messages=[{"role": "user", "content": content}]
            )
            return response.content[0].text
        except Exception as e:
            raise RuntimeError(f"Z.AI vision API failed for '{self.alias}': {e}") from e

    async def close(self) -> None:
        """Close the AsyncAnthropic client connection."""
        if hasattr(self.client, 'close'):
            await self.client.close()
            logger.debug(f"ZaiProvider '{self.alias}': Client closed")


class LocalWhisperProvider(AIProvider):
    """
    Local Whisper transcription provider with multi-platform GPU support.

    Supports:
    - openai/whisper-large-v3 (1.55B params, 99 languages, MIT license)
    - **MLX** acceleration (Apple Silicon - M1/M2/M3/M4)
    - **CUDA** acceleration with torch.compile (NVIDIA GPUs)
    - **MPS** acceleration (macOS Metal Performance Shaders)
    - Flash Attention 2 (optional, 20% additional speedup for CUDA)
    - Chunked long-form transcription (speed vs accuracy trade-off)
    - Lazy loading (model loads on first transcription request)

    Performance:
    - NVIDIA RTX 3060 (CUDA): ~10-13x real-time, ~3GB VRAM
    - Apple M1/M2 (MLX): ~10-15x real-time, unified memory
    - CPU: ~1-2x real-time, ~6GB RAM

    Reference: https://huggingface.co/openai/whisper-large-v3
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        # Model configuration
        self.model_name = config.get("model", "openai/whisper-large-v3")
        self.device = config.get("device", "auto")  # 'mlx', 'cuda', 'mps', 'cpu', or 'auto'
        self.compile_model = config.get("compile_model", True)
        self.use_flash_attention = config.get("use_flash_attention", False)
        self.chunk_length_s = config.get("chunk_length_s", 30)
        self.batch_size = config.get("batch_size", 16)
        self.language = config.get("language", "auto")
        self.task = config.get("task", "transcribe")  # 'transcribe' or 'translate'
        self.lazy_loading = config.get("lazy_loading", True)

        # Model state
        self.pipeline = None
        self.model_loaded = False
        self._load_lock = None  # Will be set to asyncio.Lock when needed
        self._detected_device = None  # Store detected device for state preservation

        logger.info(f"LocalWhisperProvider '{alias}' initialized (model={self.model_name}, device={self.device})")

    def _detect_device(self) -> str:
        """
        Auto-detect the best available device.

        Priority: MLX > CUDA > MPS > CPU

        Returns:
            Device string: 'mlx', 'cuda', 'mps', or 'cpu'
        """
        import platform

        # 1. Check for Apple MLX (Apple Silicon - M1/M2/M3/M4)
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            try:
                import mlx.core as mx
                logger.info("MLX detected (Apple Silicon) - Using Apple GPU for local transcription")
                return "mlx"
            except ImportError:
                logger.debug("MLX not available (install with: poetry install -E mlx)")

        # 2. Check for CUDA (NVIDIA GPUs)
        try:
            import torch
            if torch.cuda.is_available():
                device_name = torch.cuda.get_device_name(0)
                logger.info(f"CUDA detected: {device_name} - Using NVIDIA GPU for local transcription")
                return "cuda"
        except ImportError:
            logger.debug("PyTorch not available for CUDA detection")

        # 3. Check for MPS (macOS Metal Performance Shaders)
        if platform.system() == "Darwin":
            try:
                import torch
                if torch.backends.mps.is_available():
                    logger.info("MPS detected - Using macOS Metal GPU for local transcription")
                    return "mps"
            except (ImportError, AttributeError):
                logger.debug("MPS not available")

        # 4. Fallback to CPU
        logger.info("No GPU detected - Using CPU for local transcription (slower)")
        return "cpu"

    def _load_model(self):
        """
        Load Whisper model lazily on first use.

        Uses mlx-whisper for MLX devices (Apple Silicon) or
        transformers.pipeline for PyTorch devices (CUDA/MPS/CPU).
        Model is cached in self.pipeline for subsequent transcriptions.
        """
        if self.model_loaded:
            return

        import time
        logger.info(f"Loading Whisper model '{self.model_name}' (this may take a minute on first use)...")
        start_time = time.time()

        try:
            # Determine device
            if self.device == "auto":
                device = self._detect_device()
                self._detected_device = device  # Store for state preservation
            else:
                device = self.device
                self._detected_device = device

            # MLX path (Apple Silicon)
            if device == "mlx":
                try:
                    import mlx_whisper
                    logger.info("Loading Whisper model with MLX (Apple Silicon optimization)...")

                    # mlx-whisper loads model on first transcribe() call
                    # Store device info for later use
                    self.pipeline = "mlx"  # Marker for MLX mode
                    self.model_loaded = True

                    elapsed = time.time() - start_time
                    logger.info(f"MLX Whisper initialized in {elapsed:.1f} seconds (lazy model loading)")
                    return

                except ImportError as mlx_error:
                    logger.warning(f"MLX not available: {mlx_error} - Falling back to PyTorch")
                    device = "cpu"  # Fallback to CPU if MLX not installed

            # PyTorch path (CUDA/MPS/CPU)
            import torch
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

            # Determine HuggingFace cache directory
            cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
            os.makedirs(cache_dir, exist_ok=True)

            # Model dtype (float16 for GPU, float32 for CPU)
            torch_dtype = torch.float16 if device in ("cuda", "mps") else torch.float32

            # Load model with optimizations
            model_kwargs = {}
            if device == "cuda":
                if self.use_flash_attention:
                    model_kwargs["attn_implementation"] = "flash_attention_2"
                else:
                    model_kwargs["attn_implementation"] = "sdpa"

            try:
                model = AutoModelForSpeechSeq2Seq.from_pretrained(
                    self.model_name,
                    torch_dtype=torch_dtype,
                    low_cpu_mem_usage=True,
                    use_safetensors=True,
                    cache_dir=cache_dir,
                    local_files_only=True,  # Never check HuggingFace after initial download
                    **model_kwargs
                )
            except OSError as e:
                if "local_files_only" in str(e) or "offline mode" in str(e).lower():
                    logger.warning(
                        f"Whisper model '{self.model_name}' not found in cache. "
                        f"Download required (~3GB). User will be prompted."
                    )
                    # Raise custom exception to trigger user download prompt
                    raise ModelNotCachedError(
                        model_name=self.model_name,
                        cache_path=cache_dir,
                        download_size_gb=3.0
                    ) from e
                else:
                    raise

            # Move model to device with CUDA fallback handling
            try:
                model.to(device)
            except (RuntimeError, AssertionError) as e:
                if "NVIDIA" in str(e) or "CUDA" in str(e) or "not compiled" in str(e).lower():
                    # CUDA initialization failed (no GPU or driver), force CPU
                    logger.warning(f"Failed to initialize {device}: {e}")
                    logger.info("Forcing CPU mode for Whisper model")
                    device = "cpu"
                    torch_dtype = torch.float32  # CPU needs float32
                    model.to(device)
                else:
                    raise

            # Load processor
            processor = AutoProcessor.from_pretrained(
                self.model_name,
                cache_dir=cache_dir,
                local_files_only=True  # Never check HuggingFace after initial download
            )

            # Create pipeline with chunking for long-form audio
            self.pipeline = pipeline(
                "automatic-speech-recognition",
                model=model,
                tokenizer=processor.tokenizer,
                feature_extractor=processor.feature_extractor,
                chunk_length_s=self.chunk_length_s,
                batch_size=self.batch_size,
                dtype=torch_dtype,  # Use dtype (not torch_dtype) for pipeline (v0.15.1+)
                device=device,
                generate_kwargs={
                    "language": self.language if self.language != "auto" else None,
                    "task": self.task
                },
                ignore_warning=True  # Suppress "chunk_length_s is experimental" warning
            )

            # Apply torch.compile for 4.5x speedup (PyTorch 2.4+, CUDA only)
            if self.compile_model and device == "cuda":
                logger.info("Applying torch.compile optimization (4.5x speedup)...")
                self.pipeline.model = torch.compile(self.pipeline.model)

            self.model_loaded = True
            elapsed = time.time() - start_time
            logger.info(f"Whisper model loaded in {elapsed:.1f} seconds (device={device})")

        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}", exc_info=True)
            raise RuntimeError(f"Failed to load local Whisper model: {e}") from e

    def is_model_loaded(self) -> bool:
        """Check if Whisper model is loaded in memory."""
        return self.model_loaded

    async def ensure_model_loaded(self) -> None:
        """
        Ensure Whisper model is loaded, loading it if necessary.

        This method is safe to call multiple times (idempotent) and uses
        a lock to prevent concurrent loading attempts.
        """
        if self.model_loaded:
            return  # Already loaded

        # Initialize lock if needed (lazy init for asyncio compatibility)
        if self._load_lock is None:
            import asyncio
            self._load_lock = asyncio.Lock()

        # Acquire lock and double-check loaded status
        async with self._load_lock:
            if self.model_loaded:
                return  # Another task loaded it while we waited

            # Load model in thread pool (blocking operation)
            import asyncio
            await asyncio.to_thread(self._load_model)

    def _unload_model(self) -> None:
        """
        Unload the Whisper model from memory and free GPU VRAM.

        This method:
        - Moves PyTorch model to CPU (frees VRAM)
        - Deletes model references
        - Clears CUDA cache
        - Handles both MLX and PyTorch backends

        Called when auto-transcribe is disabled for all conversations.
        """
        if not self.model_loaded:
            logger.info("Whisper model already unloaded")
            return

        logger.info(f"Unloading Whisper model '{self.model_name}' from memory...")

        try:
            # Handle MLX backend (Apple Silicon)
            if self.pipeline == "mlx":
                # MLX manages memory automatically, just delete references
                if hasattr(self, 'whisper_model'):
                    del self.whisper_model
                logger.info("MLX Whisper model references cleared")

            # Handle PyTorch backend (CUDA/MPS/CPU)
            elif self.pipeline is not None:
                import torch

                # Check if model is on GPU
                device = None
                if hasattr(self.pipeline, 'model') and hasattr(self.pipeline.model, 'device'):
                    device = str(self.pipeline.model.device)

                # Move model to CPU first (frees GPU VRAM)
                if device and 'cuda' in device:
                    logger.info(f"Moving Whisper model from {device} to CPU...")
                    self.pipeline.model.to('cpu')
                elif device and 'mps' in device:
                    logger.info(f"Moving Whisper model from {device} to CPU...")
                    self.pipeline.model.to('cpu')

                # Delete pipeline reference
                del self.pipeline

                # Force garbage collection
                import gc
                gc.collect()

                # Clear CUDA cache if available
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    logger.info("CUDA cache cleared, ~3GB VRAM freed")
                elif torch.backends.mps.is_available():
                    # MPS doesn't have explicit cache clearing, but memory is freed
                    logger.info("MPS memory freed")
                else:
                    logger.info("CPU memory freed")

            # Reset state
            self.pipeline = None
            self.model_loaded = False

            logger.info(f"Whisper model '{self.model_name}' unloaded successfully")

        except Exception as e:
            logger.error(f"Error unloading Whisper model: {e}", exc_info=True)
            # Still reset state even on error
            self.pipeline = None
            self.model_loaded = False

    async def unload_model_async(self) -> None:
        """
        Async wrapper for unload_model that ensures thread-safe unloading.

        Uses the same lock as model loading to prevent race conditions.
        Checks if model is currently in use before unloading.
        """
        # Initialize lock if needed (for consistency with ensure_model_loaded)
        if self._load_lock is None:
            import asyncio
            self._load_lock = asyncio.Lock()

        async with self._load_lock:
            # Check if model is loaded
            if not self.model_loaded:
                logger.debug("Whisper model not loaded, skipping unload")
                return

            # Run unload in thread pool (blocking I/O)
            import asyncio
            await asyncio.to_thread(self._unload_model)

            logger.info("Whisper model unloaded (async)")

    async def download_model_async(self, progress_callback=None) -> Dict[str, Any]:
        """
        Download the Whisper model from HuggingFace.

        This method temporarily disables offline mode to allow downloading.
        Called when user confirms download in the UI dialog.

        Args:
            progress_callback: Optional async callback function(step: str, progress: float)
                              to report download progress (0.0 to 1.0)

        Returns:
            Dict with keys:
                - success: bool
                - message: str (success or error message)
                - model_name: str
                - cache_path: str

        Raises:
            Exception: If download fails
        """
        import asyncio
        import time

        logger.info(f"Starting download of Whisper model '{self.model_name}' (~3GB)...")

        if progress_callback:
            await progress_callback("Preparing download", 0.0)

        def _download():
            """Synchronous download function to run in thread pool."""
            import torch
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

            # Determine cache directory
            cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
            os.makedirs(cache_dir, exist_ok=True)

            try:
                # Determine device for dtype
                if self.device == "auto":
                    device = self._detect_device()
                else:
                    device = self.device

                torch_dtype = torch.float16 if device in ("cuda", "mps") else torch.float32

                logger.info(f"Downloading model from HuggingFace: {self.model_name}")

                # Download model (this will cache it)
                model = AutoModelForSpeechSeq2Seq.from_pretrained(
                    self.model_name,
                    torch_dtype=torch_dtype,
                    low_cpu_mem_usage=True,
                    use_safetensors=True,
                    cache_dir=cache_dir
                    # Note: NOT using local_files_only, so it downloads from HuggingFace
                )

                logger.info(f"Downloading processor config from HuggingFace: {self.model_name}")

                # Download processor (also cached)
                processor = AutoProcessor.from_pretrained(
                    self.model_name,
                    cache_dir=cache_dir
                )

                logger.info(f"Model and processor downloaded successfully to {cache_dir}")

                # Don't load model into memory yet, just download
                del model
                del processor

                import gc
                gc.collect()

                return {
                    "success": True,
                    "message": f"Model '{self.model_name}' downloaded successfully (~3GB)",
                    "model_name": self.model_name,
                    "cache_path": cache_dir
                }

            except Exception as e:
                logger.error(f"Failed to download Whisper model: {e}", exc_info=True)
                return {
                    "success": False,
                    "message": f"Download failed: {str(e)}",
                    "model_name": self.model_name,
                    "cache_path": cache_dir
                }

        # Run download in thread pool (it's blocking I/O)
        if progress_callback:
            await progress_callback("Downloading model files", 0.3)

        result = await asyncio.to_thread(_download)

        if progress_callback:
            await progress_callback("Download complete" if result["success"] else "Download failed", 1.0)

        return result

    async def transcribe(self, audio_path: str) -> Dict[str, Any]:
        """
        Transcribe audio file using local Whisper model.

        Args:
            audio_path: Path to audio file (webm, wav, mp3, etc.)

        Returns:
            Dict with keys:
                - text: Transcription text
                - language: Detected language code (e.g., 'en', 'es')
                - duration: Audio duration in seconds
                - provider: 'local_whisper'

        Raises:
            RuntimeError: If transcription fails
        """
        import asyncio

        # Initialize lock on first use
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()

        # Load model if not already loaded (lazy loading)
        if not self.model_loaded:
            async with self._load_lock:
                # Double-check after acquiring lock
                if not self.model_loaded:
                    # Run synchronous model loading in thread pool
                    await asyncio.to_thread(self._load_model)

        # Clear CUDA cache before transcription to free fragmented memory (v0.14.1+)
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            free_mem_gb = torch.cuda.mem_get_info()[0] / 1024**3
            logger.debug(f"Cleared CUDA cache before transcription. Free memory: {free_mem_gb:.2f} GB")

        try:
            # MLX path (Apple Silicon)
            if self.pipeline == "mlx":
                import mlx_whisper
                import librosa

                logger.info(f"Transcribing audio with MLX Whisper: model={self.model_name}")
                start_time = asyncio.get_event_loop().time()

                # Load audio for duration calculation
                audio_array, sample_rate = librosa.load(audio_path, sr=16000)
                duration_seconds = len(audio_array) / sample_rate

                # Run MLX transcription in thread pool
                def mlx_transcribe():
                    return mlx_whisper.transcribe(
                        audio_path,
                        path_or_hf_repo=self.model_name,
                        language=self.language if self.language != "auto" else None,
                        task=self.task
                    )

                result = await asyncio.to_thread(mlx_transcribe)

                elapsed = asyncio.get_event_loop().time() - start_time
                text = result.get("text", "").strip()
                detected_language = result.get("language", "unknown")

                logger.info(f"MLX transcription completed in {elapsed:.1f}s ({duration_seconds/elapsed:.1f}x real-time): {len(text)} chars")

                return {
                    "text": text,
                    "language": detected_language,
                    "duration": duration_seconds,
                    "provider": "local_whisper_mlx"
                }

            # PyTorch path (CUDA/MPS/CPU)
            else:
                import librosa

                # Load audio file
                try:
                    audio_array, sample_rate = librosa.load(audio_path, sr=16000)  # Whisper requires 16kHz
                except Exception as load_error:
                    logger.error(f"Failed to load audio file with librosa: {load_error}")
                    raise RuntimeError(f"Failed to load audio file '{audio_path}'. The file may be corrupted or in an unsupported format.") from load_error

                # Calculate duration
                duration_seconds = len(audio_array) / sample_rate

                # Validate audio was loaded successfully
                if duration_seconds == 0:
                    logger.error(f"Audio file appears to be empty or corrupted: {audio_path}")
                    raise RuntimeError(f"Audio file '{audio_path}' appears to be empty or corrupted. Loaded {len(audio_array)} samples at {sample_rate}Hz.")

                logger.info(f"Transcribing audio with local Whisper: {duration_seconds:.1f}s, model={self.model_name}")
                start_time = asyncio.get_event_loop().time()

                # Adaptive batch_size for long audio (up to 5 minutes / 300 seconds) - v0.14.1+
                # Reduces VRAM usage for long Telegram voice messages by processing fewer chunks simultaneously
                adaptive_batch_size = self.batch_size
                if duration_seconds > 180:  # 3+ minutes
                    adaptive_batch_size = max(1, self.batch_size // 8)  # 16 → 2
                    logger.info(f"Long audio ({duration_seconds:.0f}s), reducing batch_size: {self.batch_size} → {adaptive_batch_size}")
                elif duration_seconds > 120:  # 2+ minutes
                    adaptive_batch_size = max(1, self.batch_size // 4)  # 16 → 4
                    logger.info(f"Long audio ({duration_seconds:.0f}s), reducing batch_size: {self.batch_size} → {adaptive_batch_size}")
                elif duration_seconds > 60:  # 1+ minutes
                    adaptive_batch_size = max(1, self.batch_size // 2)  # 16 → 8
                    logger.info(f"Medium audio ({duration_seconds:.0f}s), reducing batch_size: {self.batch_size} → {adaptive_batch_size}")

                # Run transcription in thread pool (I/O + CPU bound)
                result = await asyncio.to_thread(
                    self.pipeline,
                    audio_array,
                    batch_size=adaptive_batch_size
                )

                elapsed = asyncio.get_event_loop().time() - start_time
                text = result.get("text", "").strip()

                # Try to extract language from result
                detected_language = "unknown"
                chunks = result.get("chunks")
                if chunks:
                    # Convert chunks to list if it's a generator (avoids StopIteration in async context)
                    chunks_list = list(chunks) if hasattr(chunks, '__iter__') else chunks
                    if len(chunks_list) > 0:
                        detected_language = chunks_list[0].get("language", "unknown")

                logger.info(f"Local transcription completed in {elapsed:.1f}s ({duration_seconds/elapsed:.1f}x real-time): {len(text)} chars")

                # Clear CUDA cache after transcription to free memory for next transcription (v0.14.1+)
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    logger.debug(f"Cleared CUDA cache after transcription")

                # Force garbage collection to clean up Python objects (v0.18.1+)
                # This helps prevent memory accumulation when transcribing multiple voice messages
                import gc
                gc.collect()
                logger.debug("Python garbage collection completed after transcription")

                return {
                    "text": text,
                    "language": detected_language,
                    "duration": duration_seconds,
                    "provider": "local_whisper"
                }

        except Exception as e:
            error_msg = str(e)

            # Check if CUDA OOM error - provide helpful error message (v0.14.1+)
            if "CUDA out of memory" in error_msg:
                logger.warning(f"GPU OOM for {duration_seconds:.0f}s audio (need more VRAM or use turbo model)")

                # Provide helpful error message for UI toast
                raise RuntimeError(
                    f"Voice message too long for available GPU VRAM ({duration_seconds:.0f}s). "
                    f"Try: 1) Use 'openai/whisper-large-v3-turbo' model (less VRAM), "
                    f"2) Send shorter voice messages, or 3) Close other GPU applications"
                ) from e
            else:
                logger.error(f"Local transcription failed: {e}", exc_info=True)
                raise RuntimeError(f"Local Whisper transcription failed: {e}") from e

    async def generate_response(self, prompt: str, **kwargs) -> str:
        """Not implemented - LocalWhisperProvider only supports transcription."""
        raise NotImplementedError("LocalWhisperProvider does not support text generation")

    def supports_vision(self) -> bool:
        """LocalWhisperProvider does not support vision."""
        return False


class RemotePeerProvider(AIProvider):
    """
    Remote peer inference provider.

    This provider enables using a remote peer's AI model for inference,
    allowing users to leverage more powerful models on other machines
    or share compute resources within trusted peer groups.

    Configuration example (~/.dpc/providers.json):
    {
        "alias": "remote_alice_llama70b",
        "type": "remote_peer",
        "peer_id": "dpc-node-alice-123",
        "model": "llama3:70b",
        "provider": "ollama_text",  // Optional: remote provider alias
        "timeout": 60,
        "context_window": 131072
    }

    Security: Remote inference requests are controlled by the firewall
    in privacy_rules.json under the 'compute' section. The remote peer
    must have compute sharing enabled and the requester whitelisted.
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        # Remote peer configuration
        self.peer_id = config.get("peer_id")
        self.model = config.get("model")  # Model to request on remote peer
        self.remote_provider = config.get("provider")  # Optional: specific provider on remote
        self.timeout = config.get("timeout", 60.0)

        if not self.peer_id:
            raise ValueError(f"RemotePeerProvider '{alias}' requires 'peer_id' in config")

        # CoreService reference (injected by LLMManager)
        self._service = None

        logger.info(f"RemotePeerProvider '{alias}' initialized (peer={self.peer_id}, "
                   f"model={self.model}, timeout={self.timeout}s)")

    def set_service(self, service: "CoreService") -> None:
        """
        Inject CoreService reference for remote inference.

        Called by CoreService during initialization.
        """
        self._service = service
        logger.debug(f"RemotePeerProvider '{self.alias}': CoreService injected")

    async def generate_response(
        self,
        prompt: str,
        conversation_id: str = None,
        images: list = None,
        **kwargs
    ) -> str:
        """
        Generate response using remote peer's AI model.

        Args:
            prompt: The prompt to send to the remote peer
            conversation_id: Optional conversation ID (not used for remote)
            images: Optional list of images for vision queries
            **kwargs: Additional arguments (ignored for remote)

        Returns:
            Response text from the remote peer's model

        Raises:
            RuntimeError: If CoreService not injected or remote inference fails
        """
        if not self._service:
            raise RuntimeError(f"RemotePeerProvider '{self.alias}': CoreService not injected")

        logger.info(f"RemotePeerProvider '{self.alias}': Requesting inference from peer {self.peer_id}")

        try:
            # Use CoreService's remote inference method
            response = await self._service._request_inference_from_peer(
                peer_id=self.peer_id,
                prompt=prompt,
                model=self.model,
                provider=self.remote_provider,
                images=images,
                timeout=self.timeout
            )
            return response
        except asyncio.TimeoutError:
            raise RuntimeError(f"Remote inference to peer {self.peer_id} timed out after {self.timeout}s")
        except Exception as e:
            logger.error(f"RemotePeerProvider '{self.alias}': Remote inference failed: {e}")
            raise RuntimeError(f"Remote inference failed: {e}")

    def supports_vision(self) -> bool:
        """RemotePeerProvider supports vision if the remote model supports it."""
        return True  # Assume remote peer can handle vision if model supports it


class DpcAgentProvider(AIProvider):
    """
    Embedded autonomous AI agent provider.

    This provider exposes the embedded DpcAgent as an AI provider option,
    enabling access to:
    - 40+ tools for file operations, web search, memory management
    - Persistent identity and scratchpad memory
    - Background consciousness (optional)
    - Evolution: autonomous self-modification within sandbox (~/.dpc/agent/)
      (configured in privacy_rules.json under dpc_agent.evolution)

    Configuration example (~/.dpc/providers.json):
    {
        "alias": "dpc_agent",
        "type": "dpc_agent",
        "tools": ["repo_read", "repo_list", "web_search", "update_scratchpad"],
        "background_consciousness": false,
        "budget_usd": 50,
        "max_rounds": 200,
        "context_window": 200000
    }

    Note: Evolution settings are configured in ~/.dpc/privacy_rules.json:
    {
        "dpc_agent": {
            "evolution": {
                "enabled": true,
                "interval_minutes": 60,
                "auto_apply": false
            }
        }
    }
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        # Agent configuration
        self.enabled_tools = config.get("tools", [])  # Tool whitelist
        self.background_consciousness = config.get("background_consciousness", False)
        self.budget_usd = config.get("budget_usd", 50.0)
        self.max_rounds = config.get("max_rounds", 200)

        # Remote peer inference ( v0.18.1+ KISS approach)
        # If set, agent routes inference to this peer instead of using local models
        self.peer_id = config.get("peer_id")  # Remote peer node ID
        self.remote_model = config.get("remote_model")  # Model preference on remote peer
        self.remote_provider = config.get("remote_provider")  # Provider preference on remote peer
        self.timeout = config.get("timeout", 180)  # Timeout for remote inference (default 3 minutes)

        # Note: Evolution settings are read from firewall (privacy_rules.json)
        # not from provider config - see agent_manager.py

        # Set model name for token counting (uses underlying provider's model)
        self.model = "dpc_agent"  # Identifier for token counting

        # Agent managers (per-agent architecture)
        self._manager = None  # DEPRECATED: Single manager (backwards compatibility)
        self._managers: Dict[str, "DpcAgentManager"] = {}  # NEW: Multiple managers (one per agent)
        self._service = None  # Injected by LLMManager during initialization

        logger.info(f"DpcAgentProvider '{alias}' initialized (tools={len(self.enabled_tools)}, "
                   f"budget=${self.budget_usd}, consciousness={self.background_consciousness})")

    def set_service(self, service: "CoreService") -> None:
        """
        Inject CoreService reference for LLMManager access.

        Called by CoreService during initialization to enable
        the agent to use DPC's AI providers.
        """
        self._service = service
        logger.debug(f"DpcAgentProvider '{self.alias}': CoreService injected")

    def get_manager(self, agent_id: str) -> "DpcAgentManager":
        """
        Get or create a manager for a specific agent.

        Args:
            agent_id: The agent ID to get/create manager for

        Returns:
            DpcAgentManager instance for the specified agent

        Raises:
            RuntimeError: If CoreService not injected
        """
        # Check if manager already exists for this agent
        if agent_id in self._managers:
            return self._managers[agent_id]

        # Validate service reference
        if self._service is None:
            raise RuntimeError(
                f"DpcAgentProvider '{self.alias}' requires CoreService reference. "
                "Ensure the provider is properly initialized by CoreService."
            )

        # Import here to avoid circular imports
        from dpc_client_core.managers.agent_manager import DpcAgentManager

        # Create new manager for this agent
        logger.debug(f"DpcAgentProvider '{self.alias}': Creating new manager for agent '{agent_id}'")
        manager = DpcAgentManager(self._service, {
            "tools": self.enabled_tools,
            "background_consciousness": self.background_consciousness,
            "budget_usd": self.budget_usd,
            "max_rounds": self.max_rounds,
        }, agent_id=agent_id)  # Pass agent_id for per-agent configuration

        # Cache for reuse
        self._managers[agent_id] = manager
        logger.info(f"DpcAgentProvider '{self.alias}': Created manager for agent '{agent_id}'")
        return manager

    async def _ensure_manager(self, agent_id: Optional[str] = None) -> "DpcAgentManager":
        """
        Ensure the agent manager is initialized.

        Args:
            agent_id: Optional specific agent ID to load (for per-agent managers)

        Returns:
            DpcAgentManager instance

        Raises:
            RuntimeError: If CoreService not injected or initialization fails
        """
        # NEW: If agent_id provided, use per-agent manager
        if agent_id:
            manager = self.get_manager(agent_id)
            # Ensure the manager is started (lazy initialization)
            if manager._agent is None:
                await manager.start()
                logger.info(f"DpcAgentProvider '{self.alias}': Per-agent manager started for '{agent_id}'")
            return manager

        # FALLBACK: Use legacy single manager for backwards compatibility
        if self._manager is not None:
            return self._manager

        if self._service is None:
            raise RuntimeError(
                f"DpcAgentProvider '{self.alias}' requires CoreService reference. "
                "Ensure the provider is properly initialized by CoreService."
            )

        # Import here to avoid circular imports
        from dpc_client_core.managers.agent_manager import DpcAgentManager

        # Create manager with configuration
        # Note: Evolution settings are read from firewall (privacy_rules.json)
        self._manager = DpcAgentManager(self._service, {
            "tools": self.enabled_tools,
            "background_consciousness": self.background_consciousness,
            "budget_usd": self.budget_usd,
            "max_rounds": self.max_rounds,
        }, agent_id=None)  # No agent_id for singleton manager

        # Start the agent
        await self._manager.start()
        logger.info(f"DpcAgentProvider '{self.alias}': Agent manager started (singleton mode)")

        return self._manager

    async def generate_response(self, prompt: str, conversation_id: str = None, agent_llm_provider: str = None, **kwargs) -> str:
        """
        Process a message through the autonomous agent.

        Args:
            prompt: User message text
            conversation_id: Optional conversation ID for progress tracking
            agent_llm_provider: Optional underlying LLM provider for this agent (Phase 3)
            **kwargs: Additional arguments (ignored)

        Returns:
            Agent's response text

        Raises:
            RuntimeError: If agent processing fails
        """
        try:
            # NEW: Extract agent_id from conversation_id for per-agent manager selection
            agent_id = None
            if conversation_id and conversation_id.startswith("agent_"):
                agent_id = conversation_id
                logger.debug(f"DpcAgentProvider '{self.alias}': Extracted agent_id '{agent_id}' from conversation_id")

            # NEW: Pass agent_id to _ensure_manager for per-agent manager selection
            manager = await self._ensure_manager(agent_id=agent_id)

            # Use provided conversation_id or generate one
            if not conversation_id:
                import hashlib
                conversation_id = hashlib.md5(prompt.encode()).hexdigest()[:16]

            # Process through agent with DPC context
            response = await manager.process_message(
                message=prompt,
                conversation_id=conversation_id,
                include_context=True,
                agent_llm_provider=agent_llm_provider,  # Phase 3: per-agent provider selection
            )

            logger.info(f"DpcAgentProvider '{self.alias}': Generated response ({len(response)} chars)")
            return response

        except Exception as e:
            logger.error(f"DpcAgentProvider '{self.alias}' failed: {e}", exc_info=True)
            raise RuntimeError(f"Embedded agent failed: {e}") from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
        agent_llm_provider: str = None,
        **kwargs
    ) -> str:
        """
        Process a message through the autonomous agent with streaming.

        Args:
            prompt: User message text
            on_chunk: Async callback for each text chunk: await on_chunk(chunk, conversation_id)
            conversation_id: Optional conversation ID for progress tracking
            agent_llm_provider: Optional underlying LLM provider for this agent (Phase 3)
            **kwargs: Additional arguments (ignored)

        Returns:
            Agent's response text (accumulated from all chunks)
        """
        try:
            # NEW: Extract agent_id from conversation_id for per-agent manager selection
            agent_id = None
            if conversation_id and conversation_id.startswith("agent_"):
                agent_id = conversation_id
                logger.debug(f"DpcAgentProvider '{self.alias}': Extracted agent_id '{agent_id}' from conversation_id (streaming)")

            # NEW: Pass agent_id to _ensure_manager for per-agent manager selection
            manager = await self._ensure_manager(agent_id=agent_id)

            # Use provided conversation_id or generate one
            if not conversation_id:
                import hashlib
                conversation_id = hashlib.md5(prompt.encode()).hexdigest()[:16]

            # Note: We don't pass on_chunk to manager - the manager handles
            # broadcasting directly via local_api. This avoids callback chain issues.
            response = await manager.process_message(
                message=prompt,
                conversation_id=conversation_id,
                include_context=True,
                on_stream_chunk=None,  # Manager handles broadcast directly
                agent_llm_provider=agent_llm_provider,  # Phase 3: per-agent provider selection
            )

            logger.info(f"DpcAgentProvider '{self.alias}': Generated streaming response ({len(response)} chars)")
            return response

        except Exception as e:
            logger.error(f"DpcAgentProvider '{self.alias}' streaming failed: {e}", exc_info=True)
            raise RuntimeError(f"Embedded agent streaming failed: {e}") from e

    def supports_vision(self) -> bool:
        """
        The agent supports vision through VLM tools.

        Returns:
            True (agent has analyze_screenshot and vlm_query tools)
        """
        return True

    async def generate_with_vision(
        self, prompt: str, images: List[Dict[str, Any]], **kwargs
    ) -> str:
        """
        Handle vision queries by routing through the agent.

        The agent can use VLM tools (analyze_screenshot, vlm_query)
        to process images.

        Args:
            prompt: Text prompt
            images: List of image dicts with path/mime_type/base64
            **kwargs: Additional parameters (ignored by agent)

        Returns:
            Agent's response (may include image analysis)
        """
        # For now, delegate to text generation
        # The agent can use VLM tools internally if needed
        # Future: Inject image info into prompt for agent awareness
        enhanced_prompt = prompt

        if images:
            image_info = []
            for img in images:
                if "path" in img:
                    image_info.append(f"[Image: {img['path']}]")
                elif "base64" in img:
                    image_info.append("[Image: base64 data]")

            if image_info:
                enhanced_prompt = f"{prompt}\n\nAttached images:\n" + "\n".join(image_info)

        return await self.generate_response(enhanced_prompt)

    def supports_thinking(self) -> bool:
        """
        The agent supports extended thinking via background consciousness.

        Returns:
            True if background_consciousness is enabled
        """
        return self.background_consciousness

    def get_thinking_params(self) -> Dict[str, Any]:
        """
        Return agent-specific thinking parameters.

        Returns:
            Dict with consciousness configuration
        """
        return {
            "consciousness_mode": "background" if self.background_consciousness else "disabled",
            "enabled": self.background_consciousness,
        }

    def get_status(self) -> Dict[str, Any]:
        """
        Get agent status information.

        Returns:
            Dict with agent status (initialized, config, etc.)
        """
        if self._manager is None:
            return {
                "initialized": False,
                "alias": self.alias,
            }

        return {
            "initialized": True,
            "alias": self.alias,
            "manager_status": self._manager.get_status(),
        }

    async def shutdown(self) -> None:
        """
        Shutdown the agent manager gracefully.
        """
        if self._manager is not None:
            await self._manager.stop()
            self._manager = None
            logger.info(f"DpcAgentProvider '{self.alias}': Agent manager stopped")


class GeminiProvider(AIProvider):
    """
    Google Gemini provider using the google-genai SDK.

    Supports all Gemini models (gemini-2.0-flash, gemini-1.5-pro, etc.)
    with native vision (all models are multimodal) and thinking support
    for gemini-2.0-flash-thinking-exp.

    Auth: GEMINI_API_KEY environment variable (Google AI Studio key).
    """

    THINKING_MODELS = ["gemini-2.0-flash-thinking-exp"]

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError:
            raise RuntimeError(
                f"GeminiProvider '{alias}': Install google-genai — "
                "run: poetry add google-genai"
            )
        api_key = config.get("api_key")
        if not api_key:
            api_key_env = config.get("api_key_env", "GEMINI_API_KEY")
            api_key = os.getenv(api_key_env)
        if not api_key:
            raise ValueError(
                f"GeminiProvider '{alias}': No API key found. "
                "Set GEMINI_API_KEY or specify api_key_env in config."
            )
        self._genai = genai
        self._types = genai_types
        self.client = genai.Client(api_key=api_key)
        logger.info(f"GeminiProvider '{alias}': Initialized with model '{self.model}'")

    def supports_vision(self) -> bool:
        return True  # All Gemini models are natively multimodal

    def supports_thinking(self) -> bool:
        return any(m in self.model for m in self.THINKING_MODELS)

    async def generate_response(self, prompt: str, **kwargs) -> str:
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                )
            )
            return response.text
        except Exception as e:
            raise RuntimeError(f"GeminiProvider '{self.alias}' failed: {e}") from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
    ) -> str:
        loop = asyncio.get_event_loop()
        try:
            chunks = await loop.run_in_executor(
                None,
                lambda: list(self.client.models.generate_content_stream(
                    model=self.model,
                    contents=prompt,
                ))
            )
            full_text = ""
            for chunk in chunks:
                text = chunk.text or ""
                full_text += text
                if on_chunk and text:
                    await on_chunk(text, conversation_id)
            return full_text
        except Exception as e:
            raise RuntimeError(f"GeminiProvider '{self.alias}' streaming failed: {e}") from e

    async def generate_with_vision(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        **kwargs,
    ) -> str:
        parts = []
        for img in images:
            parts.append(
                self._types.Part.from_bytes(
                    data=img["data"],
                    mime_type=img.get("media_type", "image/jpeg"),
                )
            )
        parts.append(prompt)
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.client.models.generate_content(
                    model=self.model,
                    contents=parts,
                )
            )
            return response.text
        except Exception as e:
            raise RuntimeError(f"GeminiProvider '{self.alias}' vision failed: {e}") from e


class GitHubModelsProvider(AIProvider):
    """
    GitHub Models provider using the azure-ai-inference SDK.

    Provides access to GPT-4o, Llama, Phi, Mistral and other models
    hosted at https://models.inference.ai.azure.com using a GitHub
    Personal Access Token (models:read permission required).

    Free tier: 15 RPM / 150 RPD (low-complexity models),
               10 RPM / 50 RPD (high-complexity models).

    Auth: GITHUB_TOKEN environment variable.
    """

    ENDPOINT = "https://models.inference.ai.azure.com"
    VISION_MODELS = ["gpt-4o", "llama-3.2-11b-vision"]

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        try:
            from azure.ai.inference import ChatCompletionsClient
            from azure.core.credentials import AzureKeyCredential
            self._UserMessage = None  # lazy import in methods
        except ImportError:
            raise RuntimeError(
                f"GitHubModelsProvider '{alias}': Install azure-ai-inference — "
                "run: poetry add azure-ai-inference"
            )
        token = config.get("api_key")
        if not token:
            token = os.getenv(config.get("api_key_env", "GITHUB_TOKEN"))
        if not token:
            raise ValueError(
                f"GitHubModelsProvider '{alias}': No token found. "
                "Set GITHUB_TOKEN or specify api_key_env in config."
            )
        self.client = ChatCompletionsClient(
            endpoint=self.ENDPOINT,
            credential=AzureKeyCredential(token),
        )
        logger.info(
            f"GitHubModelsProvider '{alias}': Initialized with model '{self.model}' "
            f"at {self.ENDPOINT}"
        )

    def supports_vision(self) -> bool:
        return any(m in self.model for m in self.VISION_MODELS)

    async def generate_response(self, prompt: str, **kwargs) -> str:
        from azure.ai.inference.models import UserMessage
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.client.complete(
                    messages=[UserMessage(prompt)],
                    model=self.model,
                    temperature=self.temperature,
                )
            )
            return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"GitHubModelsProvider '{self.alias}' failed: {e}") from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
    ) -> str:
        from azure.ai.inference.models import UserMessage

        def _stream():
            result = []
            with self.client.complete(
                messages=[UserMessage(prompt)],
                model=self.model,
                temperature=self.temperature,
                stream=True,
            ) as stream:
                for update in stream:
                    if update.choices and update.choices[0].delta:
                        result.append(update.choices[0].delta.content or "")
            return result

        loop = asyncio.get_event_loop()
        try:
            chunks = await loop.run_in_executor(None, _stream)
            full_text = ""
            for text in chunks:
                full_text += text
                if on_chunk and text:
                    await on_chunk(text, conversation_id)
            return full_text
        except Exception as e:
            raise RuntimeError(
                f"GitHubModelsProvider '{self.alias}' streaming failed: {e}"
            ) from e

    async def close(self) -> None:
        self.client.close()
        logger.debug(f"GitHubModelsProvider '{self.alias}': Client closed")


class GigaChatProvider(AIProvider):
    """
    GigaChat provider by Sberbank using the official gigachat SDK.

    Supports GigaChat-2-Pro, GigaChat-2-Max (vision), GigaChat-2-Lite.
    All models have 128K context window.

    SSL Certificate: Sberbank requires the Russian НУЦ Минцифры CA cert.
    Install once into the virtualenv's certifi bundle:
        curl -k "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt" -w "\\n" >> $(python -m certifi)
    Or set ca_bundle_file in provider config to the cert path.

    Auth: GIGACHAT_CREDENTIALS environment variable (authorization key
    from https://developers.sber.ru/studio).

    Scope values:
        GIGACHAT_API_PERS  — personal/individual (free tier, default)
        GIGACHAT_API_B2B   — business
        GIGACHAT_API_CORP  — corporate
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        try:
            from gigachat import GigaChat
            self._GigaChat = GigaChat
        except ImportError:
            raise RuntimeError(
                f"GigaChatProvider '{alias}': Install gigachat — "
                "run: poetry add gigachat"
            )
        credentials = config.get("api_key")
        if not credentials:
            credentials = os.getenv(config.get("api_key_env", "GIGACHAT_CREDENTIALS"))
        if not credentials:
            raise ValueError(
                f"GigaChatProvider '{alias}': No credentials found. "
                "Set GIGACHAT_CREDENTIALS or specify api_key_env in config."
            )
        self._client_kwargs: Dict[str, Any] = dict(
            credentials=credentials,
            scope=config.get("scope", "GIGACHAT_API_PERS"),
            model=self.model,
            verify_ssl_certs=config.get("verify_ssl", True),
        )
        ca_bundle = config.get("ca_bundle_file", "")
        if ca_bundle:
            self._client_kwargs["ca_bundle_file"] = ca_bundle
        logger.info(
            f"GigaChatProvider '{alias}': Initialized with model '{self.model}', "
            f"scope={self._client_kwargs['scope']}"
        )

    def supports_vision(self) -> bool:
        return "Max" in self.model  # Only GigaChat-2-Max supports vision

    async def generate_response(self, prompt: str, **kwargs) -> str:
        try:
            async with self._GigaChat(**self._client_kwargs) as client:
                response = await client.achat(prompt)
                return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"GigaChatProvider '{self.alias}' failed: {e}") from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
    ) -> str:
        try:
            full_text = ""
            async with self._GigaChat(**self._client_kwargs) as client:
                async for chunk in client.astream(prompt):
                    text = chunk.choices[0].delta.content or ""
                    full_text += text
                    if on_chunk and text:
                        await on_chunk(text, conversation_id)
            return full_text
        except Exception as e:
            raise RuntimeError(
                f"GigaChatProvider '{self.alias}' streaming failed: {e}"
            ) from e

    async def close(self) -> None:
        pass  # Context manager handles cleanup per-request


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