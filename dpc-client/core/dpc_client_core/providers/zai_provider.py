# dpc_client_core/providers/zai_provider.py

import os
import base64
import asyncio
import logging
from typing import Dict, Any, Optional, List

from anthropic import AsyncAnthropic

from .base import AIProvider

logger = logging.getLogger(__name__)


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

        # Sampling parameters
        self.top_p = config.get("top_p")  # None = use API default; 0.9 reduces unlikely token tails (language mixing)

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
            if self.top_p is not None:
                api_params["top_p"] = self.top_p

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
            is_overloaded = "1305" in str(e) or "overloaded" in str(e).lower()
            if is_overloaded:
                logger.warning(f"Z.AI batch overloaded (1305), retrying in 3s: {e}")
                await asyncio.sleep(3)
                try:
                    message = await self.client.messages.create(**api_params)
                    final_text = next(
                        (getattr(b, 'text', None) for b in message.content
                         if hasattr(b, 'type') and b.type == "text"),
                        ""
                    )
                    return final_text or ""
                except Exception as retry_e:
                    raise RuntimeError(f"Z.AI provider '{self.alias}' failed after retry: {retry_e}") from retry_e
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
            if self.top_p is not None:
                api_params["top_p"] = self.top_p

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
            is_overloaded = "1305" in str(e) or "overloaded" in str(e).lower()
            if is_overloaded:
                logger.warning(f"Z.AI streaming overloaded (1305), falling back to batch mode: {e}")
                # Brief backoff before retrying — Z.AI uses HTTP 429 for overload (1305) as well
                # as rate limiting; a short wait improves batch success rate on an overloaded server
                await asyncio.sleep(2)
                result = await self.generate_response(prompt)
                # Emit full result as one chunk so UI streaming display and Raw output still work
                if on_chunk and result:
                    await on_chunk(result, conversation_id)
                return result
            logger.error(f"Z.AI streaming failed: {e}", exc_info=True)
            raise RuntimeError(f"Z.AI streaming provider '{self.alias}' failed: {e}") from e

    async def generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system: str = "",
        on_chunk: Optional[callable] = None,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Native Anthropic SDK tool calling for GLM-4.7.

        Accepts Anthropic-format messages and tools directly.
        Returns dict: {content, tool_calls_raw, thinking, usage}
        """
        effective_max_tokens = max(self.max_tokens or 8192, 8192)

        api_params: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": effective_max_tokens,
            "messages": messages,
            "tools": tools,
        }
        if system:
            api_params["system"] = system

        # Enable extended thinking if configured (test: Z.AI may support interleaved thinking + tools)
        if self.thinking_enabled:
            if effective_max_tokens <= self.thinking_budget_tokens:
                effective_max_tokens = self.thinking_budget_tokens + 4096
                api_params["max_tokens"] = effective_max_tokens
            api_params["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget_tokens,
            }
            logger.info(f"GLM tool calling with thinking enabled (budget={self.thinking_budget_tokens} tokens)")

        self._last_thinking = None

        def _extract_blocks(content_blocks):
            """Extract text, tool_use, and thinking blocks from content."""
            full_text = ""
            tool_calls_raw = []
            for block in content_blocks:
                if not hasattr(block, "type"):
                    continue
                if block.type == "text":
                    full_text += getattr(block, "text", "")
                elif block.type == "tool_use":
                    tool_calls_raw.append(block)
                elif block.type == "thinking":
                    thinking_val = getattr(block, "thinking", "")
                    if thinking_val:
                        self._last_thinking = thinking_val
                        logger.info(f"GLM tool calling thinking: {len(thinking_val)} chars")
            return full_text, tool_calls_raw

        try:
            if on_chunk:
                full_text = ""
                tool_calls_raw = []
                async with self.client.messages.stream(**api_params) as stream:
                    async for text in stream.text_stream:
                        full_text += text
                        await on_chunk(text, conversation_id)
                    final_message = await stream.get_final_message()
                full_text, tool_calls_raw = _extract_blocks(final_message.content)
                usage_obj = final_message.usage
            else:
                message = await self.client.messages.create(**api_params)
                full_text, tool_calls_raw = _extract_blocks(message.content)
                usage_obj = message.usage

            return {
                "content": full_text,
                "tool_calls_raw": tool_calls_raw,
                "thinking": self._last_thinking,
                "usage": {
                    "prompt_tokens": usage_obj.input_tokens,
                    "completion_tokens": usage_obj.output_tokens,
                    "total_tokens": usage_obj.input_tokens + usage_obj.output_tokens,
                },
            }

        except Exception as e:
            is_overloaded = "1305" in str(e) or "overloaded" in str(e).lower()
            if is_overloaded:
                logger.warning(f"Z.AI tool calling overloaded (1305), retrying in 3s: {e}")
                await asyncio.sleep(3)
                message = await self.client.messages.create(**api_params)
                full_text, tool_calls_raw = _extract_blocks(message.content)
                usage_obj = message.usage
                return {
                    "content": full_text,
                    "tool_calls_raw": tool_calls_raw,
                    "thinking": self._last_thinking,
                    "usage": {
                        "prompt_tokens": usage_obj.input_tokens,
                        "completion_tokens": usage_obj.output_tokens,
                        "total_tokens": usage_obj.input_tokens + usage_obj.output_tokens,
                    },
                }
            raise RuntimeError(f"Z.AI native tool calling failed for '{self.alias}': {e}") from e

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
