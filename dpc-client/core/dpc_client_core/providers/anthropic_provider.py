# dpc_client_core/providers/anthropic_provider.py

import os
import logging
from typing import Dict, Any, Optional, List

from anthropic import AsyncAnthropic

from .base import AIProvider, ANTHROPIC_THINKING_MODELS, image_base64, network_client_bounds

logger = logging.getLogger(__name__)


class AnthropicProvider(AIProvider):
    """Claude over the Messages API.

    Output count convention (`includes`), from
    https://platform.claude.com/docs/en/build-with-claude/extended-thinking:
    "monitor the `usage.output_tokens_details.thinking_tokens` field in the
    response, which reports how many of the billed output tokens were internal
    reasoning." Thinking is a share of `output_tokens`, not a number beside it,
    so a tariff bills `output_tokens` once and never adds the share again.

    The prompt side is three fields, not one, from
    https://platform.claude.com/docs/en/build-with-claude/prompt-caching:
    `input_tokens` is "Number of input tokens which were not read from or used
    to create a cache", and the page gives the sum this adapter reports as
    `prompt_tokens`: "total_input_tokens = cache_read_input_tokens +
    cache_creation_input_tokens + input_tokens".
    """

    DECLARED_OUTPUT_INCLUDES_THINKING = "includes"

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        api_key_env = config.get("api_key_env")
        api_key = os.getenv(api_key_env) if api_key_env else None

        if not api_key:
            raise ValueError(f"API key environment variable not set for Anthropic provider '{self.alias}'")

        self.client = AsyncAnthropic(api_key=api_key, **network_client_bounds(config))

        # Read max_tokens from config (optional, defaults to 4096  if not specified)
        # Set to None or omit from config to use model's maximum
        self.max_tokens = config.get("max_tokens", 4096)

        # Thinking/reasoning configuration (Claude Extended Thinking)
        # Claude 3.7+ and Claude 4+ support extended thinking with budget_tokens
        self.thinking_enabled = config.get("thinking", {}).get("enabled", False)
        self.thinking_budget_tokens = config.get("thinking", {}).get("budget_tokens", 10000)

        # Store last thinking content for retrieval by LLMManager
        self._last_thinking: Optional[str] = None

    def _usage_from(self, message: Any) -> Dict[str, Any]:
        """The vendor's token accounting for one message, in this project's shape.

        `reasoning_tokens` is the vendor's own split where the response carries
        `output_tokens_details`; where it does not, the zero is «not reported»
        and `content_tokens` falls back to the whole output. Neither reading
        changes the convention: thinking is inside `completion_tokens` either way.
        """
        u = getattr(message, "usage", None)
        if u is None:
            return {}
        after_last_breakpoint = getattr(u, "input_tokens", 0) or 0
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(u, "cache_creation_input_tokens", 0) or 0
        prompt_tokens = after_last_breakpoint + cache_read + cache_creation
        completion = getattr(u, "output_tokens", 0) or 0
        details = getattr(u, "output_tokens_details", None)
        thinking = (getattr(details, "thinking_tokens", 0) or 0) if details else 0
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion,
            "reasoning_tokens": thinking,
            "content_tokens": max(0, completion - thinking),
            "total_tokens": prompt_tokens + completion,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "output_includes_thinking": self.DECLARED_OUTPUT_INCLUDES_THINKING,
        }
        if thinking:
            usage["thinking_source"] = "engine"
        return usage

    def _log_usage(self, message: Any, path: str) -> Dict[str, Any]:
        """Record and announce what the call just cost, and hand the dict back."""
        usage = self._usage_from(message)
        self._record_last_usage(usage)
        if usage:
            logger.info(
                "Anthropic usage: alias=%s model=%s prompt=%d (cache_read=%d), "
                "completion=%d (thinking=%d), path=%s",
                self.alias, self.model, usage["prompt_tokens"],
                usage["cache_read_input_tokens"], usage["completion_tokens"],
                usage["reasoning_tokens"], path,
            )
        return usage

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
        self._last_thinking = None
        # Cleared before the call, so a failure leaves no previous call's counts
        # for `get_last_usage()` to hand out as this one's.
        self._record_last_usage(None)
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
            self._log_usage(message, "plain")

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
        self._record_last_usage(None)
        # Built before the try: a refusal here is about the request, not about
        # the API, and must not be reported as the API having failed.
        content = []
        for img in images:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.get("mime_type", "image/png"),
                    "data": image_base64(img, self.alias),
                }
            })
        content.append({"type": "text", "text": prompt})

        try:
            response = await self.client.messages.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=kwargs.get("temperature", self.temperature),
                max_tokens=kwargs.get("max_tokens", self.max_tokens or 4096)
            )
            self._log_usage(response, "vision")
            return response.content[0].text
        except Exception as e:
            raise RuntimeError(f"Anthropic vision API failed for '{self.alias}': {e}") from e

    async def close(self) -> None:
        """Close the AsyncAnthropic client connection."""
        if hasattr(self.client, 'close'):
            await self.client.close()
            logger.debug(f"AnthropicProvider '{self.alias}': Client closed")
