# dpc_client_core/providers/neuraldeep_provider.py

import os
import json
import logging
from types import SimpleNamespace
from typing import Dict, Any, Optional, List, Union

from openai import AsyncOpenAI

from .base import (AIProvider, REASONING_OFF, anthropic_to_openai_messages,
                   configured_reasoning_default, image_base64,
                   network_client_bounds, normalize_reasoning_effort,
                   numeric_setting, positive_ceiling)
from ..dpc_agent.pricing import NEURALDEEP_CURRENCY, compute_cost_rub

logger = logging.getLogger(__name__)

NEURALDEEP_DEFAULT_BASE_URL = "https://api.neuraldeep.ru/v1"
NOREASON_SUFFIX = "-noreason"

# Per model id, from the model list in https://neuraldeep.ru/llms-full.txt
# (read 2026-09-26). Vision was also checked live on qwen3.8-27b.
_VISION_MODELS = frozenset({"qwen3.8-27b", "qwen3.6-35b-a3b", "qwen3.6-fp8", "gemma-4-31b"})
_REASONING_MODELS = frozenset({
    "qwen3.8-27b", "qwen3.6-35b-a3b", "qwen3.6-fp8", "gemma-4-31b",
    "gpt-oss-120b", "gpt-oss-20b", "kimi-k2.6",
})
# Models with a published `-noreason` twin: the only way to turn thinking off,
# because the gateway drops `chat_template_kwargs.enable_thinking=false`.
_NOREASON_TWINS = frozenset({"qwen3.8-27b", "qwen3.6-35b-a3b", "qwen3.6-fp8", "gemma-4-31b"})

# The shared effort word -> what each model's `reasoning_effort` accepts, per
# the vendor's "Reasoning по моделям" section. qwen3.8-27b does not know `high`
# and silently falls back to `low`, so the top of our scale goes to `xhigh`.
_EFFORT_WIRE: Dict[str, Dict[str, str]] = {
    "gpt-oss-120b": {"low": "low", "medium": "medium", "high": "high", "max": "high"},
    "gpt-oss-20b": {"low": "low", "medium": "medium", "high": "high", "max": "high"},
    "qwen3.8-27b": {"low": "low", "medium": "medium", "high": "xhigh", "max": "xhigh"},
}


def base_model(model: Optional[str]) -> str:
    """The model id without its `-noreason` suffix, lowercased."""
    name = (model or "").strip().lower()
    return name[: -len(NOREASON_SUFFIX)] if name.endswith(NOREASON_SUFFIX) else name


class NeuralDeepProvider(AIProvider):
    """NeuralDeep (neuraldeep.ru), an OpenAI-compatible gateway over vLLM.

    Same shape as DeepSeekProvider: AsyncOpenAI on the wire, Anthropic-shaped
    messages and tools from the agent layer converted both ways, usage recorded
    on every path. What differs, all from the 2026-09-26 probe:

      - thinking off is a *model switch* to `<model>-noreason`; the gateway
        ignores `enable_thinking=false`. Models without a twin cannot stop.
      - `completion_tokens_details.reasoning_tokens` can be null, and was once
        larger than `completion_tokens` (297 vs 296); it is clamped.
      - the answer after reasoning starts with "\\n\\n"; it is stripped.
      - prices are roubles (`pricing.NEURALDEEP_RATES_RUB`); the usage dict
        carries `cost_amount` with `cost_currency`, never a dollar `cost`.
      - one image per request (vendor docs).
    """

    RETRY_LABEL = "NeuralDeep"
    # Probe 2026-09-26, qwen3.8-27b: completion_tokens=63 with reasoning_tokens=57,
    # i.e. reasoning is counted inside completion (vLLM counts every generated token).
    DECLARED_OUTPUT_INCLUDES_THINKING = "includes"

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        api_key = config.get("api_key")
        if not api_key:
            api_key_env = config.get("api_key_env", "NEURALDEEP_API_KEY")
            if api_key_env:
                api_key = os.getenv(api_key_env)
        if not api_key:
            raise ValueError(f"API key not found for NeuralDeep provider '{self.alias}'")

        base_url = config.get("base_url", NEURALDEEP_DEFAULT_BASE_URL)
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url,
                                  **network_client_bounds(config, default_retries=0))
        # For GET /v1/limits, which the openai SDK does not cover.
        self._api_key = api_key
        self._base_url = base_url

        self.max_tokens = config.get("max_tokens", 8192)
        self.thinking_enabled = config.get("thinking", {}).get("enabled", True)
        self._reasoning_effort = normalize_reasoning_effort(config.get("reasoning_effort"))
        self.top_p = config.get("top_p")
        self._temperature_explicit = config.get("temperature")
        self.max_retry_seconds = config.get("max_retry_seconds", 600)

        self._last_thinking: Optional[str] = None
        self._last_usage: Optional[Dict[str, Any]] = None

    # --- capabilities ---

    def supports_vision(self) -> bool:
        return base_model(self.model) in _VISION_MODELS

    def supports_thinking(self) -> bool:
        name = (self.model or "").strip().lower()
        return not name.endswith(NOREASON_SUFFIX) and base_model(name) in _REASONING_MODELS

    def get_thinking_params(self) -> Dict[str, Any]:
        return {}

    def get_last_thinking(self) -> Optional[str]:
        return self._last_thinking

    def reasoning_words_served(self) -> Optional[List[str]]:
        """`off` where a `-noreason` twin exists, and the levels only where the
        model has a `reasoning_effort` that changes anything."""
        if not self.supports_thinking():
            return []
        base = base_model(self.model)
        words: List[str] = [REASONING_OFF] if base in _NOREASON_TWINS else []
        if base in _EFFORT_WIRE:
            words += ["low", "medium", "high", "max"]
        return words

    def reasoning_default_served(self) -> Optional[str]:
        return configured_reasoning_default(self)

    # --- request shaping ---

    def _thinks(self, reasoning_effort: Optional[str] = None) -> bool:
        """Whether this call reasons. `off` only takes effect where a twin exists."""
        if not self.supports_thinking():
            return False
        wants_off = (normalize_reasoning_effort(reasoning_effort) == REASONING_OFF
                     or not self.thinking_enabled)
        return not (wants_off and base_model(self.model) in _NOREASON_TWINS)

    def _wire_model(self, reasoning_effort: Optional[str] = None) -> str:
        if self.supports_thinking() and not self._thinks(reasoning_effort):
            return base_model(self.model) + NOREASON_SUFFIX
        return self.model

    def _effort_word(self, reasoning_effort: Optional[str] = None) -> Optional[str]:
        """The shared word this call runs at, or None for the model's default."""
        if not self._thinks(reasoning_effort):
            return REASONING_OFF if self.supports_thinking() else None
        requested = normalize_reasoning_effort(reasoning_effort)
        if requested in (None, REASONING_OFF):
            requested = self._reasoning_effort
        if requested in (None, REASONING_OFF):
            return None
        return requested if base_model(self.model) in _EFFORT_WIRE else None

    def _request_params(self, reasoning_effort: Optional[str] = None,
                        temperature: Optional[float] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"model": self._wire_model(reasoning_effort),
                                  "max_tokens": self.max_tokens}
        word = self._effort_word(reasoning_effort)
        if word and word != REASONING_OFF:
            params["extra_body"] = {
                "reasoning_effort": _EFFORT_WIRE[base_model(self.model)][word],
            }
        temp = temperature if temperature is not None else self._temperature_explicit
        if temp is not None:
            params["temperature"] = temp
        if self.top_p is not None:
            params["top_p"] = self.top_p
        return params

    def effective_settings(self) -> Dict[str, Any]:
        settings: Dict[str, Any] = {}
        ceiling = positive_ceiling(self.max_tokens)
        if ceiling is not None:
            settings["max_output_tokens"] = ceiling
        for key, value in (("temperature", self._temperature_explicit), ("top_p", self.top_p)):
            if numeric_setting(value) is not None:
                settings[key] = value
        return settings

    # --- usage ---

    def get_last_usage(self) -> Optional[Dict[str, Any]]:
        return dict(self._last_usage) if self._last_usage else None

    def _usage_from_response(self, u: Any, model: str) -> Dict[str, Any]:
        prompt_tokens = getattr(u, "prompt_tokens", 0) or 0
        completion = getattr(u, "completion_tokens", 0) or 0
        details = getattr(u, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        ctd = getattr(u, "completion_tokens_details", None)
        reasoning = getattr(ctd, "reasoning_tokens", None) if ctd else None
        usage: Dict[str, Any] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion,
            "total_tokens": getattr(u, "total_tokens", 0) or 0,
            "cache_read_input_tokens": cached,
            "output_includes_thinking": self.DECLARED_OUTPUT_INCLUDES_THINKING,
        }
        if reasoning is not None:
            if reasoning > completion:
                logger.warning(
                    "NeuralDeep %s reported reasoning_tokens=%d > completion_tokens=%d; "
                    "clamped to completion", model, reasoning, completion,
                )
                reasoning = completion
            usage["reasoning_tokens"] = reasoning
            usage["content_tokens"] = completion - reasoning
            usage["thinking_source"] = "engine"
        amount = compute_cost_rub(
            model, prompt_tokens, completion, cache_hit_tokens=cached,
            output_includes_thinking=self.DECLARED_OUTPUT_INCLUDES_THINKING,
        )
        if amount is not None:
            usage["cost_amount"] = amount
            usage["cost_currency"] = NEURALDEEP_CURRENCY
        return usage

    def _record_usage(self, raw_usage: Any, *, path: str, model: str,
                      served_effort: Optional[str], tool_calls: int = 0) -> Dict[str, Any]:
        if raw_usage is None:
            return {}
        usage = self._usage_from_response(raw_usage, model)
        usage["served_effort"] = served_effort
        self._record_last_usage(usage)
        logger.info(
            "NeuralDeep usage: alias=%s model=%s prompt=%d (cached=%d), completion=%d "
            "(reasoning=%s), cost=%s %s, tool_calls=%d, effort=%s, path=%s",
            self.alias, model, usage["prompt_tokens"], usage["cache_read_input_tokens"],
            usage["completion_tokens"], usage.get("reasoning_tokens"),
            f"{usage['cost_amount']:.4f}" if "cost_amount" in usage else "unpriced",
            usage.get("cost_currency", ""), tool_calls, served_effort, path,
        )
        return usage

    # --- balance ---

    def supports_balance(self) -> bool:
        return True

    async def get_balance(self) -> Dict[str, Any]:
        """The wallet from GET /v1/limits, in the shape the balance pill reads.

        Documented in https://neuraldeep.ru/llms-full.txt ("Остатки лимитов"):
        read-only, spends no quota. `billing_mode` is `subscription` or the
        wallet; the wallet balance exists either way. The raw payload rides
        along under `limits`."""
        import httpx
        url = self._base_url.rstrip("/") + "/limits"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            limits = resp.json()
        wallet = limits.get("wallet") or {}
        key = limits.get("key") or {}
        decision = limits.get("decision") or {}
        balance = wallet.get("balance_rub")
        return {
            "is_available": key.get("status", "ok") == "ok" and decision.get("can_request", True),
            "balance_infos": [] if balance is None else [{
                "currency": NEURALDEEP_CURRENCY,
                "total_balance": f"{float(balance):.2f}",
            }],
            "billing_mode": key.get("billing_mode"),
            "limits": limits,
        }

    # --- retry ---

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        err_str = str(error).lower()
        return any(indicator in err_str for indicator in [
            "429", "500", "502", "503",
            "bad gateway", "service unavailable", "internal server error",
            "timed out", "timeout", "connection reset", "connection error",
            "overloaded", "rate limit",
        ]) or isinstance(error, (ConnectionError, OSError)) or type(error).__name__ in (
            "APIConnectionError", "APITimeoutError", "InternalServerError",
        )

    # --- plain text ---

    async def generate_response(self, prompt: str, **kwargs) -> str:
        self._last_thinking = None
        self._last_usage = None
        effort = kwargs.get("reasoning_effort")

        async def _call():
            params = self._request_params(effort, kwargs.get("temperature"))
            resp = await self.client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}], **params,
            )
            msg = resp.choices[0].message
            self._last_thinking = getattr(msg, "reasoning_content", None)
            self._record_usage(getattr(resp, "usage", None), path="plain",
                               model=params["model"], served_effort=self._effort_word(effort))
            return (msg.content or "").lstrip()

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                return await self._retry_with_backoff(_call, e)
            raise RuntimeError(
                f"NeuralDeep provider '{self.alias}' failed: {type(e).__name__}: {e}"
            ) from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
        reasoning_effort: str = None,
    ) -> str:
        self._last_thinking = None
        self._last_usage = None

        async def _call():
            params = self._request_params(reasoning_effort)
            stream = await self.client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                stream_options={"include_usage": True},
                **params,
            )
            full_text = ""
            thinking_text = ""
            async for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    self._record_usage(chunk_usage, path="plain-stream", model=params["model"],
                                       served_effort=self._effort_word(reasoning_effort))
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    thinking_text += reasoning
                text = getattr(delta, "content", None)
                if text and not full_text:
                    text = text.lstrip()
                if text:
                    full_text += text
                    if on_chunk:
                        await on_chunk(text, conversation_id)
            if thinking_text:
                self._last_thinking = thinking_text
            return full_text

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                # The retry re-runs the whole stream through on_chunk; do not
                # send its return value to on_chunk a second time.
                return await self._retry_with_backoff(_call, e)
            raise RuntimeError(
                f"NeuralDeep streaming provider '{self.alias}' failed: {type(e).__name__}: {e}"
            ) from e

    # --- native tool calling (Anthropic shape in and out, OpenAI on the wire) ---

    @staticmethod
    def _anthropic_to_openai_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for t in tools:
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

    _anthropic_to_openai_messages = staticmethod(anthropic_to_openai_messages)

    async def generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system: Union[str, List[Dict[str, Any]]] = "",
        on_chunk: Optional[callable] = None,
        conversation_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Returns {content, tool_calls_raw, thinking, usage}; tool_calls_raw
        items expose .id/.name/.input as llm_adapter._chat_native_tools reads."""
        self._last_thinking = None
        self._last_usage = None
        openai_messages = self._anthropic_to_openai_messages(system, messages, provider=self)
        openai_tools = self._anthropic_to_openai_tools(tools)

        async def _call():
            params = self._request_params(reasoning_effort)
            resp = await self.client.chat.completions.create(
                messages=openai_messages, tools=openai_tools, tool_choice="auto", **params,
            )
            msg = resp.choices[0].message
            content = (msg.content or "").lstrip()
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

            usage = self._record_usage(
                resp.usage, path="tools", model=params["model"],
                served_effort=self._effort_word(reasoning_effort),
                tool_calls=len(tool_calls_raw),
            )
            return {"content": content, "tool_calls_raw": tool_calls_raw,
                    "thinking": thinking, "usage": usage}

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                return await self._retry_with_backoff(_call, e)
            raise RuntimeError(
                f"NeuralDeep native tool calling failed for '{self.alias}': "
                f"{type(e).__name__}: {e}"
            ) from e

    # --- vision ---

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """OpenAI-format vision (image_url data URL). The gateway takes one
        image per request, so more is refused here rather than at the API."""
        if not self.supports_vision():
            raise ValueError(f"NeuralDeep model '{self.model}' has no vision path")
        if len(images) != 1:
            raise ValueError(
                f"NeuralDeep provider '{self.alias}' takes exactly one image per request "
                f"(vendor limit); got {len(images)}"
            )
        img = images[0]
        mime_type = img.get("mime_type", "image/png")
        content: List[Dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime_type};base64,{image_base64(img, self.alias)}"}},
        ]
        effort = kwargs.get("reasoning_effort")
        self._last_thinking = None
        self._last_usage = None

        async def _call():
            params = self._request_params(effort, kwargs.get("temperature"))
            if kwargs.get("max_tokens"):
                params["max_tokens"] = kwargs["max_tokens"]
            resp = await self.client.chat.completions.create(
                messages=[{"role": "user", "content": content}], **params,
            )
            msg = resp.choices[0].message
            self._last_thinking = getattr(msg, "reasoning_content", None)
            self._record_usage(getattr(resp, "usage", None), path="vision",
                               model=params["model"], served_effort=self._effort_word(effort))
            return (msg.content or "").lstrip()

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                return await self._retry_with_backoff(_call, e)
            raise RuntimeError(
                f"NeuralDeep vision failed for '{self.alias}': {type(e).__name__}: {e}"
            ) from e

    async def close(self) -> None:
        if hasattr(self.client, "close"):
            await self.client.close()
