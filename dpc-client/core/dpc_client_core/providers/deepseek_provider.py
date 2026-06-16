# dpc_client_core/providers/deepseek_provider.py

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

DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"

# DeepSeek reasoning effort levels accepted on the wire. "xhigh" is mapped to
# "max" (matches hermes/pi). Anything else → omit (server default, currently high).
_VALID_REASONING_EFFORT = {"low", "medium", "high", "max"}


class DeepSeekProvider(AIProvider):
    """
    DeepSeek provider over the **OpenAI-compatible** endpoint (https://api.deepseek.com).

    DeepSeek is pay-per-token with very high concurrency limits (V4-Flash 2500 /
    V4-Pro 500), so it is the agents' fallback when Z.AI's GLM Coding Plan trips
    Fair-Usage 1313. Models: deepseek-v4-flash (cheap default), deepseek-v4-pro.

    Architecture mirrors ZaiCodingProvider: the agent layer
    (llm_adapter._chat_native_tools) hands providers Anthropic-shaped
    messages/tools, so generate_with_tools converts Anthropic -> OpenAI on the way
    in and OpenAI tool_calls -> Anthropic-style tool_use objects on the way out.

    DeepSeek-specific (vs ZaiCodingProvider):
      - **reasoning_content echo (critical):** DeepSeek V4 thinking is default-on;
        once thinking is enabled, EVERY replayed assistant message carrying
        tool_calls must include `reasoning_content`, or round-2+ fails with HTTP
        400 ("The reasoning_content in the thinking mode must be passed back to the
        API"). The agent adapter drops thinking on replay, so we pad with a single
        space (" ") — DeepSeek V4 Pro rejects an empty string. Verified pattern in
        hermes (chat_completion_helpers.py) and pi (openai-completions.ts).
      - thinking toggle is explicit: extra_body.thinking {type: enabled|disabled}
        (+ optional reasoning_effort). Must send {type: disabled} to actually turn
        off the default-on thinking.
      - 1313 is NOT special-cased (DeepSeek never emits it).
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        api_key = config.get("api_key")
        if not api_key:
            api_key_env = config.get("api_key_env", "DEEPSEEK_API_KEY")
            if api_key_env:
                api_key = os.getenv(api_key_env)
        if not api_key:
            raise ValueError(f"API key not found for DeepSeek provider '{self.alias}'")

        base_url = config.get("base_url", DEEPSEEK_DEFAULT_BASE_URL)
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        # Kept for the REST balance endpoint (/user/balance); the openai SDK doesn't cover it.
        self._api_key = api_key
        self._base_url = base_url

        self.max_tokens = config.get("max_tokens", 8192)

        # DeepSeek V4 thinking (enabled by default; reasoning returned in
        # reasoning_content). When disabled we must send {type: disabled} to
        # override DeepSeek's default-on behaviour.
        self.thinking_enabled = config.get("thinking", {}).get("enabled", True)

        # Optional reasoning effort (top-of-body via extra_body). xhigh -> max.
        self._reasoning_effort = self._normalize_effort(config.get("reasoning_effort"))

        self.top_p = config.get("top_p")  # None => API default
        self._temperature_explicit = config.get("temperature")  # None unless user set it

        # Exponential backoff with a time budget (default 10 min)
        self.max_retry_seconds = config.get("max_retry_seconds", 600)

        self._last_thinking: Optional[str] = None

        # CoT replay cache (Phase 3): reasoning_content keyed by tool_call id so a
        # later round can resend the real chain-of-thought instead of a placeholder.
        self._cot_cache: Dict[str, str] = {}

    def supports_vision(self) -> bool:
        """DeepSeek V4 text/reasoning models are not multimodal."""
        return False

    def supports_thinking(self) -> bool:
        return True

    def get_thinking_params(self) -> Dict[str, Any]:
        if self.thinking_enabled:
            return {"thinking": {"type": "enabled"}}
        return {}

    def get_last_thinking(self) -> Optional[str]:
        return self._last_thinking

    def supports_balance(self) -> bool:
        return True

    async def get_balance(self) -> Dict[str, Any]:
        """Query the DeepSeek account balance via GET /user/balance.

        Returns the raw DeepSeek payload: {is_available, balance_infos: [{currency,
        total_balance, granted_balance, topped_up_balance}]}. Balance is shared across
        every provider on the same key (cost lands on the account, not the model).
        The balance endpoint lives at the API root, so a trailing /v1 in base_url (a
        valid chat base_url) is stripped. Raises on transport/HTTP errors.
        """
        import httpx
        base = self._base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        url = base + "/user/balance"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()

    # --- retry helpers (DeepSeek is pay-per-token; no 1313 Fair-Usage penalty) ---

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        err_str = str(error).lower()
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
                "DeepSeek retry %d, waiting %ds (elapsed %ds/%ds): %s",
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
            f"DeepSeek provider '{self.alias}' failed after {attempt} retries "
            f"({elapsed}s elapsed): {last_error}"
        ) from last_error

    @staticmethod
    def _normalize_effort(value: Optional[str]) -> Optional[str]:
        """Strip/lowercase, map xhigh -> max, validate against the accepted set.
        Returns None for empty/invalid (-> server default)."""
        raw = (value or "").strip().lower()
        if raw == "xhigh":
            raw = "max"
        return raw if raw in _VALID_REASONING_EFFORT else None

    def _build_extra_body(self, reasoning_effort: Optional[str] = None) -> Dict[str, Any]:
        """DeepSeek thinking toggle. Always sent — {type: disabled} is required to
        override the default-on thinking. A per-call reasoning_effort (e.g. a UI
        toggle) wins over the provider-config default; a None/invalid override falls
        back to the config value, so callers that pass nothing keep the configured
        effort (no silent downgrade). Either is only sent when thinking is enabled."""
        body: Dict[str, Any] = {
            "thinking": {"type": "enabled" if self.thinking_enabled else "disabled"}
        }
        effort = self._normalize_effort(reasoning_effort) or self._reasoning_effort
        if self.thinking_enabled and effort:
            body["reasoning_effort"] = effort
        return body

    def _effective_temperature(self, override: Optional[float] = None) -> float:
        if override is not None:
            return override
        if self._temperature_explicit is not None:
            return self._temperature_explicit
        return 1.0

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
                "extra_body": self._build_extra_body(),
            }
            if self.top_p is not None:
                params["top_p"] = self.top_p
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
                f"DeepSeek provider '{self.alias}' failed: {type(e).__name__}: {e}"
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
                "extra_body": self._build_extra_body(),
                "stream": True,
            }
            if self.top_p is not None:
                params["top_p"] = self.top_p

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
                logger.info("DeepSeek streaming thinking: %d chars", len(thinking_text))
            return full_text

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                result = await self._retry_with_backoff(_call, e)
                if on_chunk and result:
                    await on_chunk(result, conversation_id)
                return result
            logger.error("DeepSeek streaming failed: %s", e, exc_info=True)
            raise RuntimeError(
                f"DeepSeek streaming provider '{self.alias}' failed: {type(e).__name__}: {e}"
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
        reasoning_echo: bool = False,
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
                thinking_text = ""
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
                    elif bt == "thinking":
                        thinking_text += b.get("thinking", "")
                msg: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                    if reasoning_echo:
                        # DeepSeek thinking mode requires reasoning_content on every
                        # assistant message that carries tool_calls, or replaying it
                        # on the next round returns HTTP 400. The agent adapter drops
                        # thinking blocks on replay, so thinking_text is normally
                        # empty -> pad with a single space (V4 Pro rejects "").
                        msg["reasoning_content"] = thinking_text or " "
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
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Native tool calling. Returns {content, tool_calls_raw, thinking, usage}
        where tool_calls_raw items expose .id/.name/.input (Anthropic tool_use shape)
        as consumed by llm_adapter._chat_native_tools.
        """
        self._last_thinking = None
        openai_messages = self._anthropic_to_openai_messages(
            system, messages, reasoning_echo=self.thinking_enabled
        )
        openai_tools = self._anthropic_to_openai_tools(tools)

        # Restore the REAL reasoning_content on replayed assistant tool-call messages.
        # _anthropic_to_openai_messages pads with " " because the agent adapter drops
        # thinking on replay; look the CoT back up by tool_call id (cached when we first
        # produced those calls) so DeepSeek sees its own prior reasoning across rounds,
        # not a placeholder. Falls back to " " when the CoT is not cached. (Phase 3)
        if self.thinking_enabled and self._cot_cache:
            for m in openai_messages:
                if m.get("role") == "assistant" and m.get("tool_calls") and m.get("reasoning_content") in (None, "", " "):
                    for tc in m["tool_calls"]:
                        cot = self._cot_cache.get(tc.get("id"))
                        if cot:
                            m["reasoning_content"] = cot
                            break

        async def _call():
            extra_body = self._build_extra_body(reasoning_effort)
            params: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": openai_messages,
                "tools": openai_tools,
                "tool_choice": "auto",
                "temperature": self._effective_temperature(),
                "extra_body": extra_body,
            }
            if self.top_p is not None:
                params["top_p"] = self.top_p

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

            # Cache this round's reasoning_content keyed by tool_call id so the NEXT
            # round replays the real CoT (see the injection above). Bounded to avoid
            # unbounded growth across tasks. (Phase 3)
            if thinking and tool_calls_raw:
                for tcr in tool_calls_raw:
                    self._cot_cache[tcr.id] = thinking
                if len(self._cot_cache) > 1000:
                    self._cot_cache.clear()

            if on_chunk and content:
                await on_chunk(content, conversation_id)

            u = resp.usage
            prompt_tokens = getattr(u, "prompt_tokens", 0) or 0
            # DeepSeek reports the cache split natively (prompt_tokens = hit + miss)
            # and leaves the OpenAI-compat prompt_tokens_details.cached_tokens at 0,
            # so prefer the native fields and fall back only if they are absent.
            _hit = getattr(u, "prompt_cache_hit_tokens", None)
            _miss = getattr(u, "prompt_cache_miss_tokens", None)
            if _hit is None:
                _details = getattr(u, "prompt_tokens_details", None)
                _hit = getattr(_details, "cached_tokens", 0) if _details else 0
            if _miss is None:
                _miss = max(0, prompt_tokens - (_hit or 0))
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                "total_tokens": getattr(u, "total_tokens", 0) or 0,
                "cache_read_input_tokens": _hit or 0,
                "prompt_cache_hit_tokens": _hit or 0,
                "prompt_cache_miss_tokens": _miss or 0,
            }
            logger.info(
                "DeepSeek usage: prompt=%d (hit=%d/miss=%d), completion=%d, tool_calls=%d, effort=%s",
                usage["prompt_tokens"], usage["prompt_cache_hit_tokens"],
                usage["prompt_cache_miss_tokens"], usage["completion_tokens"],
                len(tool_calls_raw), extra_body.get("reasoning_effort", "server-default"),
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
                f"DeepSeek native tool calling failed for '{self.alias}': "
                f"{type(e).__name__}: {e}"
            ) from e

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """OpenAI-format vision. DeepSeek V4 text models are not multimodal — kept
        for interface completeness; supports_vision() returns False so the manager
        will not route vision here."""
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
                f"DeepSeek vision failed for '{self.alias}': {type(e).__name__}: {e}"
            ) from e

    async def close(self) -> None:
        if hasattr(self.client, "close"):
            await self.client.close()
            logger.debug("DeepSeekProvider '%s': Client closed", self.alias)
