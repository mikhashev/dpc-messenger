# dpc_client_core/providers/zai_provider.py

import os
import re
import json
import base64
import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, Any, Optional, List, Union

from openai import AsyncOpenAI

from .base import AIProvider

logger = logging.getLogger(__name__)

# The Z.AI **open platform** endpoint — prepaid pay-per-token, and the only route
# this product is licensed to take. The vendor's own words, read 2026-08-23 at
# docs.z.ai/api-reference/introduction: "Z.ai Platform's general API endpoint is
# as follows: https://api.z.ai/api/paas/v4".
ZAI_DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"

# The three base URLs that draw a GLM Coding Plan **subscription** rather than the
# prepaid balance. They are listed together in the vendor's own protocol table at
# docs.z.ai/devpack/tool/others, under a heading that names the Coding Plan.
#
# We are not allowed to use any of them. The same page says: "The GLM Coding Plan
# is limited to use within the following officially supported tools and product
# environments; users may not use their subscription benefits for tools or
# scenarios outside of this scope" — and lists 19 products (16 coding agents plus
# OpenClaw, Hermes Agent and SillyTavern). D-PC Messenger is not one of them.
#
# This is not a preference. docs.z.ai/devpack/usage-policy: "Violations of the
# Usage Rules may trigger risk control measures, including rate limiting, account
# freezing, or other restrictions. Accounts with more than three violations may
# be banned." This account has already been banned for a month once, and the same
# account is the one the owner's ZCode subscription runs on — ZCode *is* on the
# supported list, so his own use is legitimate and ours is what puts it at risk.
_SUBSCRIPTION_BASE_URLS = (
    "api.z.ai/api/anthropic",          # Anthropic Messages
    "api.z.ai/api/coding/paas/v4",     # OpenAI Chat Completions
    "api.z.ai/api/v1",                 # OpenAI Responses
)


# The vision models on the vendor's list, 2026-08-23: glm-5v-turbo, glm-4.6v,
# glm-4.6v-flashx, glm-4.6v-flash, glm-4.5v, glm-ocr. Matched by the naming rule
# rather than enumerated, because the rule is what the vendor keeps stable — a
# version number followed by `v` — and a hard list goes stale one release later
# while a text model never accidentally grows a `v`.
_ZAI_VISION_RE = re.compile(r"glm-\d+(?:\.\d+)?v\b|glm-ocr", re.I)


def _is_subscription_url(base_url: str) -> bool:
    """True when a base URL draws the Coding Plan subscription instead of prepaid.

    Compared on the path rather than the whole string so a trailing slash, a
    scheme difference or a proxy host in front cannot slip one through.
    """
    normalised = (base_url or "").rstrip("/").lower()
    return any(marker in normalised for marker in _SUBSCRIPTION_BASE_URLS)


class ZaiProvider(AIProvider):
    """Z.AI GLM provider over the prepaid pay-per-token platform API.

    One provider, one route: `https://api.z.ai/api/paas/v4`, billed against the
    platform account's balance. The two providers this replaces both drew the
    GLM Coding Plan subscription — one through the Anthropic-compatible endpoint,
    one through the coding endpoint — and both were outside the vendor's terms
    for a product like this one. See the constants above for the quotations.

    The agent layer (`llm_adapter._chat_native_tools`) speaks Anthropic shapes to
    providers, so `generate_with_tools` converts Anthropic -> OpenAI on the way in
    and OpenAI `tool_calls` -> Anthropic-style `tool_use` objects on the way out.
    That conversion is inherited unchanged from the provider this replaces; what
    changed is the endpoint it points at and the fact that every path now records
    what it spent, because a prepaid route whose spend is invisible is worse than
    no route at all.
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        api_key = config.get("api_key")
        if not api_key:
            api_key_env = config.get("api_key_env", "ZAI_API_KEY")
            if api_key_env:
                api_key = os.getenv(api_key_env)
        if not api_key:
            raise ValueError(f"API key not found for Z.AI provider '{self.alias}'")

        base_url = config.get("base_url", ZAI_DEFAULT_BASE_URL)

        # Refuse rather than warn. A warning here would be a line in a log nobody
        # reads while the account accumulates violations toward a ban, and the
        # operator who typed the subscription URL would have no idea. The failure
        # has to be at construction, where it is attributable to the config that
        # caused it.
        if _is_subscription_url(base_url):
            raise ValueError(
                f"Z.AI provider '{self.alias}' is configured with a GLM Coding Plan "
                f"subscription endpoint ({base_url}). The Coding Plan may only be used "
                f"from the vendor's officially supported tools, and this product is not "
                f"one of them; calling it from here is a terms violation that counts "
                f"against the whole account. Use the prepaid platform API instead: "
                f"{ZAI_DEFAULT_BASE_URL}"
            )

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.base_url = base_url

        self.max_tokens = config.get("max_tokens", 8192)

        # GLM extended thinking (enabled by default; reasoning arrives in
        # `reasoning_content`).
        self.thinking_enabled = config.get("thinking", {}).get("enabled", True)

        self.top_p = config.get("top_p")  # None => API default
        self._temperature_explicit = config.get("temperature")  # None unless user set it

        # Exponential backoff with a time budget (default 10 min)
        self.max_retry_seconds = config.get("max_retry_seconds", 600)

        self._last_thinking: Optional[str] = None

    def supports_vision(self) -> bool:
        """Only the V models and the OCR model, not every GLM.

        This used to return an unconditional True while the docstring already named
        the V models — the sentence was right and the code did not implement it. It
        is not cosmetic: `llm_manager` picks the **first** provider whose
        `supports_vision()` is true when an image query has no configured vision
        provider (`:617-620`), so a `glm-4.7` alias would volunteer for image work
        and fail at the API instead of being passed over here.
        """
        return bool(_ZAI_VISION_RE.search(self.model or ""))

    def supports_thinking(self) -> bool:
        return True

    def get_thinking_params(self) -> Dict[str, Any]:
        if self.thinking_enabled:
            return {"thinking": {"type": "enabled"}}
        return {}

    def get_last_thinking(self) -> Optional[str]:
        return self._last_thinking

    # --- retry helpers ---

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        err_str = str(error).lower()
        # 1313 is the Coding Plan's Fair-Usage code. On the prepaid platform API it
        # should be unreachable, so seeing it does not mean "retry later" — it means
        # this call reached the subscription, which is the thing this provider exists
        # to stop. Kept non-retryable, and shouted about at the call sites, because a
        # retry would spend a second violation on the same mistake.
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

    def _note_if_subscription_error(self, error: Exception) -> None:
        """A 1313 from here is a canary, not a hiccup — say so at ERROR."""
        if "1313" in str(error).lower():
            logger.error(
                "Z.AI provider '%s' received Fair-Usage 1313 from %s. That code belongs "
                "to the GLM Coding Plan subscription, which this product may not use — "
                "the call should not have been able to reach it. Check base_url and the "
                "API key's plan before retrying anything.",
                self.alias, self.base_url,
            )

    async def _retry_with_backoff(self, fn, last_error: Exception):
        delay = 3
        elapsed = 0
        attempt = 0
        while elapsed < self.max_retry_seconds:
            attempt += 1
            logger.warning(
                "Z.AI retry %d, waiting %ds (elapsed %ds/%ds): %s",
                attempt, delay, elapsed, self.max_retry_seconds, last_error,
            )
            await asyncio.sleep(delay)
            elapsed += delay
            try:
                return await fn()
            except Exception as e:
                if not self._is_retryable(e):
                    self._note_if_subscription_error(e)
                    raise
                last_error = e
                delay = min(delay * 2, 192)
        raise RuntimeError(
            f"Z.AI provider '{self.alias}' failed after {attempt} retries "
            f"({elapsed}s elapsed): {last_error}"
        ) from last_error

    def _build_extra_body(self) -> Optional[Dict[str, Any]]:
        if self.thinking_enabled:
            return {"thinking": {"type": "enabled"}}
        return None

    def _effective_temperature(self, override: Optional[float] = None) -> float:
        if override is not None:
            return override
        if self._temperature_explicit is not None:
            return self._temperature_explicit
        return 1.0

    def _usage_from(self, resp) -> Dict[str, int]:
        """Normalise the SDK's usage object into the shape the cost meter reads.

        Every path calls this, not only the tool path. This is a prepaid provider:
        a call whose tokens are not recorded is money the burn digest, the alert
        thresholds and the runway cannot see — the exact hole this project already
        paid for once on the DeepSeek plain path.
        """
        u = getattr(resp, "usage", None)
        if u is None:
            return {}
        details = getattr(u, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) if details else 0
        return {
            "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
            "total_tokens": getattr(u, "total_tokens", 0) or 0,
            "cache_read_input_tokens": cached or 0,
        }

    def _log_usage(self, usage: Dict[str, int], path: str, tool_calls: int = 0) -> None:
        if not usage:
            return
        self._record_last_usage(usage)
        logger.info(
            "Z.AI usage: alias=%s model=%s prompt=%d (cache_read=%d), completion=%d, "
            "tool_calls=%d, path=%s",
            self.alias, self.model,
            usage.get("prompt_tokens", 0), usage.get("cache_read_input_tokens", 0),
            usage.get("completion_tokens", 0), tool_calls, path,
        )

    # --- plain text generation ---

    async def generate_response(self, prompt: str, **kwargs) -> str:
        """Non-streaming text generation."""
        self._last_thinking = None

        async def _call():
            params: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": kwargs.get("max_tokens", self.max_tokens),
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
            self._log_usage(self._usage_from(resp), path="plain")
            return msg.content or ""

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                return await self._retry_with_backoff(_call, e)
            self._note_if_subscription_error(e)
            raise RuntimeError(
                f"Z.AI provider '{self.alias}' failed: {type(e).__name__}: {e}"
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
                # Without this a stream reports no usage at all, and the whole call
                # is invisible to the cost meter. The chunk that carries it has an
                # empty `choices`, which is why the loop below reads usage before it
                # tests for choices rather than after.
                "stream_options": {"include_usage": True},
            }
            if self.top_p is not None:
                params["top_p"] = self.top_p
            extra = self._build_extra_body()
            if extra:
                params["extra_body"] = extra

            full_text = ""
            thinking_text = ""
            usage: Dict[str, int] = {}
            stream = await self.client.chat.completions.create(**params)
            async for chunk in stream:
                chunk_usage = self._usage_from(chunk)
                if chunk_usage:
                    usage = chunk_usage
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
                logger.info("Z.AI streaming thinking: %d chars", len(thinking_text))
            self._log_usage(usage, path="plain-stream")
            return full_text

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                # `_call` is the whole stream: the retry re-runs it, and it delivers
                # every token through `on_chunk` on its way to returning `full_text`.
                # Sending that return value to `on_chunk` as well — which is what this
                # branch used to do — put the entire answer on the wire a second time,
                # after the reader had already received it token by token.
                #
                # Traced 2026-08-23 rather than assumed: `on_chunk` is
                # `agent_manager.emit_stream_chunk`, which appends to `_stream_chunks`
                # and broadcasts one WebSocket event — no usage, no cost, so this does
                # *not* double-bill. It does something more durable. `_raw` becomes the
                # answer twice, which makes `_streaming_raw` differ from `response`
                # (`agent_manager.py:1168-1169`) where it is normally None, so the
                # doubled text is persisted into conversation history and read back as
                # context by every later turn. (Ark, code review 2026-08-23; the same
                # three lines live in `deepseek_provider.py:461` and
                # `llamacpp_server_provider.py:729`.)
                return await self._retry_with_backoff(_call, e)
            self._note_if_subscription_error(e)
            logger.error("Z.AI streaming failed: %s", e, exc_info=True)
            raise RuntimeError(
                f"Z.AI streaming provider '{self.alias}' failed: {type(e).__name__}: {e}"
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

            usage = self._usage_from(resp)
            self._log_usage(usage, path="tools", tool_calls=len(tool_calls_raw))
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
            self._note_if_subscription_error(e)
            raise RuntimeError(
                f"Z.AI native tool calling failed for '{self.alias}': "
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
            self._log_usage(self._usage_from(resp), path="vision")
            return resp.choices[0].message.content or ""

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                return await self._retry_with_backoff(_call, e)
            self._note_if_subscription_error(e)
            raise RuntimeError(
                f"Z.AI vision failed for '{self.alias}': {type(e).__name__}: {e}"
            ) from e

    async def close(self) -> None:
        if hasattr(self.client, "close"):
            await self.client.close()
            logger.debug("ZaiProvider '%s': Client closed", self.alias)
