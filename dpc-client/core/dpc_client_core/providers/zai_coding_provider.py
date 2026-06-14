# dpc_client_core/providers/zai_coding_provider.py

import os
import json
import base64
import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, Any, Optional, List, Union

from openai import AsyncOpenAI

from .base import AIProvider

logger = logging.getLogger(__name__)

ZAI_CODING_DEFAULT_BASE_URL = "https://api.z.ai/api/coding/paas/v4"


class ZaiCodingProvider(AIProvider):
    """
    Z.AI GLM provider over the **Coding Plan** endpoint (OpenAI-compatible).

    Uses the dedicated Coding API (https://api.z.ai/api/coding/paas/v4) via the
    OpenAI SDK, so inference draws from a GLM Coding Plan subscription rather than
    the prepaid PaaS balance / the Claude-Code-only anthropic endpoint (which
    triggers Fair-Usage 1313 when called programmatically).

    The agent layer (llm_adapter._chat_native_tools) speaks Anthropic shapes to
    providers, so generate_with_tools converts Anthropic -> OpenAI on the way in
    and OpenAI tool_calls -> Anthropic-style tool_use objects on the way out.
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        api_key = config.get("api_key")
        if not api_key:
            api_key_env = config.get("api_key_env", "ZAI_API_KEY")
            if api_key_env:
                api_key = os.getenv(api_key_env)
        if not api_key:
            raise ValueError(f"API key not found for Z.AI Coding provider '{self.alias}'")

        base_url = config.get("base_url", ZAI_CODING_DEFAULT_BASE_URL)
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        self.max_tokens = config.get("max_tokens", 8192)

        # GLM extended thinking (enabled by default; reasoning returned in reasoning_content)
        self.thinking_enabled = config.get("thinking", {}).get("enabled", True)

        self.top_p = config.get("top_p")  # None => API default

        # Exponential backoff with a time budget (default 10 min)
        self.max_retry_seconds = config.get("max_retry_seconds", 600)

        self._last_thinking: Optional[str] = None

    def supports_vision(self) -> bool:
        """GLM-V models (glm-4.6v, glm-4.5v, etc.) support vision."""
        return True

    def supports_thinking(self) -> bool:
        return True

    def get_thinking_params(self) -> Dict[str, Any]:
        if self.thinking_enabled:
            return {"thinking": {"type": "enabled"}}
        return {}

    def get_last_thinking(self) -> Optional[str]:
        return self._last_thinking

    # --- retry helpers (1313 Fair-Usage is NOT retryable: it's an account penalty) ---

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        err_str = str(error).lower()
        if "1313" in err_str:
            return False
        return any(indicator in err_str for indicator in [
            "429", "500", "502", "503",
            "bad gateway", "service unavailable", "internal server error",
            "timed out", "timeout", "connection reset", "connection error",
            "overloaded", "rate limit", "internal network failure",
            "high traffic", "high concurrency", "high frequency",
        ]) or isinstance(error, (ConnectionError, OSError)) or type(error).__name__ in (
            "APIConnectionError", "APITimeoutError", "InternalServerError",
        )

    async def _retry_with_backoff(self, fn, last_error: Exception):
        delay = 3
        elapsed = 0
        attempt = 0
        while elapsed < self.max_retry_seconds:
            attempt += 1
            logger.warning(
                "Z.AI Coding retry %d, waiting %ds (elapsed %ds/%ds): %s",
                attempt, delay, elapsed, self.max_retry_seconds, last_error,
            )
            await asyncio.sleep(delay)
            elapsed += delay
            try:
                return await fn()
            except Exception as e:
                if not self._is_retryable(e):
                    raise
                last_error = e
                delay = min(delay * 2, 192)
        raise RuntimeError(
            f"Z.AI Coding provider '{self.alias}' failed after {attempt} retries "
            f"({elapsed}s elapsed): {last_error}"
        ) from last_error

    def _build_extra_body(self) -> Optional[Dict[str, Any]]:
        if self.thinking_enabled:
            return {"thinking": {"type": "enabled"}}
        return None

    def _effective_temperature(self, override: Optional[float] = None) -> float:
        # Z.AI docs: temperature should be 1.0 when deep thinking is enabled.
        if self.thinking_enabled:
            return 1.0
        return override if override is not None else self.temperature

    # --- plain text generation ---

    async def generate_response(self, prompt: str, **kwargs) -> str:
        """Non-streaming text generation."""
        self._last_thinking = None

        async def _call():
            params: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self._effective_temperature(kwargs.get("temperature")),
            }
            if self.top_p is not None:
                params["top_p"] = self.top_p
            extra = self._build_extra_body()
            if extra:
                params["extra_body"] = extra
            resp = await self.client.chat.completions.create(**params)
            msg = resp.choices[0].message
            self._last_thinking = getattr(msg, "reasoning_content", None)
            return msg.content or ""

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                return await self._retry_with_backoff(_call, e)
            raise RuntimeError(
                f"Z.AI Coding provider '{self.alias}' failed: {type(e).__name__}: {e}"
            ) from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
    ) -> str:
        """Streaming text generation. Calls on_chunk(text, conversation_id) per token."""
        self._last_thinking = None

        async def _call():
            params: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self._effective_temperature(),
                "stream": True,
            }
            if self.top_p is not None:
                params["top_p"] = self.top_p
            extra = self._build_extra_body()
            if extra:
                params["extra_body"] = extra

            full_text = ""
            thinking_text = ""
            stream = await self.client.chat.completions.create(**params)
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    thinking_text += reasoning
                text = getattr(delta, "content", None)
                if text:
                    full_text += text
                    if on_chunk:
                        await on_chunk(text, conversation_id)
            if thinking_text:
                self._last_thinking = thinking_text
                logger.info("GLM Coding streaming thinking: %d chars", len(thinking_text))
            return full_text

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                result = await self._retry_with_backoff(_call, e)
                if on_chunk and result:
                    await on_chunk(result, conversation_id)
                return result
            logger.error("Z.AI Coding streaming failed: %s", e, exc_info=True)
            raise RuntimeError(
                f"Z.AI Coding streaming provider '{self.alias}' failed: {type(e).__name__}: {e}"
            ) from e

    # --- native tool calling (Anthropic-shape in, OpenAI on the wire, Anthropic-shape out) ---

    @staticmethod
    def _anthropic_to_openai_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for t in tools:
            # Accept Anthropic shape {name, description, input_schema}; tolerate OpenAI passthrough.
            if "function" in t:
                out.append(t)
                continue
            out.append({
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        return out

    @staticmethod
    def _anthropic_to_openai_messages(
        system: Union[str, List[Dict[str, Any]]],
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if system:
            sys_text = system if isinstance(system, str) else "".join(
                b.get("text", "") for b in system if isinstance(b, dict)
            )
            if sys_text:
                out.append({"role": "system", "content": sys_text})

        for m in messages:
            role = m.get("role")
            content = m.get("content")

            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue

            blocks = content if isinstance(content, list) else []

            if role == "assistant":
                text_parts: List[str] = []
                tool_calls: List[Dict[str, Any]] = []
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        text_parts.append(b.get("text", ""))
                    elif bt == "tool_use":
                        tool_calls.append({
                            "id": b.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input", {})),
                            },
                        })
                msg: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)
                continue

            if role == "user":
                tool_results = [
                    b for b in blocks
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                if tool_results:
                    for tr in tool_results:
                        # Anthropic tool_result.content may be a string or a list
                        # of content blocks; flatten the list form to text.
                        tr_content = tr.get("content", "")
                        if isinstance(tr_content, list):
                            tr_content = "".join(
                                b.get("text", "") for b in tr_content
                                if isinstance(b, dict)
                            )
                        out.append({
                            "role": "tool",
                            "tool_call_id": tr.get("tool_use_id", ""),
                            "content": str(tr_content),
                        })
                else:
                    text_parts = [
                        b.get("text", "") for b in blocks
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    out.append({"role": "user", "content": "".join(text_parts)})
                continue

            # Fallback: stringify unknown block content
            out.append({"role": role or "user", "content": json.dumps(blocks)})

        return out

    async def generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system: Union[str, List[Dict[str, Any]]] = "",
        on_chunk: Optional[callable] = None,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Native tool calling. Returns {content, tool_calls_raw, thinking, usage}
        where tool_calls_raw items expose .id/.name/.input (Anthropic tool_use shape)
        as consumed by llm_adapter._chat_native_tools.
        """
        self._last_thinking = None
        openai_messages = self._anthropic_to_openai_messages(system, messages)
        openai_tools = self._anthropic_to_openai_tools(tools)

        async def _call():
            params: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": openai_messages,
                "tools": openai_tools,
                "tool_choice": "auto",
                "temperature": self._effective_temperature(),
            }
            if self.top_p is not None:
                params["top_p"] = self.top_p
            extra = self._build_extra_body()
            if extra:
                params["extra_body"] = extra

            resp = await self.client.chat.completions.create(**params)
            msg = resp.choices[0].message

            content = msg.content or ""
            thinking = getattr(msg, "reasoning_content", None)
            self._last_thinking = thinking

            tool_calls_raw = []
            for tc in (msg.tool_calls or []):
                try:
                    input_data = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except (json.JSONDecodeError, TypeError):
                    input_data = {}
                tool_calls_raw.append(
                    SimpleNamespace(id=tc.id, name=tc.function.name, input=input_data)
                )

            if on_chunk and content:
                await on_chunk(content, conversation_id)

            u = resp.usage
            _details = getattr(u, "prompt_tokens_details", None)
            _cached = getattr(_details, "cached_tokens", 0) if _details else 0
            usage = {
                "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                "total_tokens": getattr(u, "total_tokens", 0) or 0,
                "cache_read_input_tokens": _cached or 0,
            }
            logger.info(
                "Z.AI Coding usage: prompt=%d, completion=%d, cache_read=%d, tool_calls=%d",
                usage["prompt_tokens"], usage["completion_tokens"],
                usage["cache_read_input_tokens"], len(tool_calls_raw),
            )
            return {
                "content": content,
                "tool_calls_raw": tool_calls_raw,
                "thinking": thinking,
                "usage": usage,
            }

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                return await self._retry_with_backoff(_call, e)
            raise RuntimeError(
                f"Z.AI Coding native tool calling failed for '{self.alias}': "
                f"{type(e).__name__}: {e}"
            ) from e

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """OpenAI-format vision (image_url data URLs) for GLM-V models."""
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "base64" in img:
                base64_data = img["base64"]
                if base64_data.startswith("data:"):
                    base64_data = base64_data.split(",", 1)[1]
            else:
                with open(img["path"], "rb") as f:
                    base64_data = base64.b64encode(f.read()).decode("utf-8")
            mime_type = img.get("mime_type", "image/png")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{base64_data}"},
            })

        async def _call():
            resp = await self.client.chat.completions.create(
                model=self.model,
                max_tokens=kwargs.get("max_tokens", self.max_tokens),
                messages=[{"role": "user", "content": content}],
                temperature=self._effective_temperature(kwargs.get("temperature")),
            )
            return resp.choices[0].message.content or ""

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                return await self._retry_with_backoff(_call, e)
            raise RuntimeError(
                f"Z.AI Coding vision failed for '{self.alias}': {type(e).__name__}: {e}"
            ) from e

    async def close(self) -> None:
        if hasattr(self.client, "close"):
            await self.client.close()
            logger.debug("ZaiCodingProvider '%s': Client closed", self.alias)
