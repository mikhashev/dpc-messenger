# dpc_client_core/providers/deepseek_provider.py

import os
import json
import base64
import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, Any, Optional, List, Union

from openai import AsyncOpenAI

from .base import AIProvider, REASONING_OFF, network_client_bounds, normalize_reasoning_effort

logger = logging.getLogger(__name__)

DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"

# The wire accepts more words than we offer: `none, minimal, low, medium, high,
# xhigh, max` (the server names them itself when it refuses one), running three
# actual efforts — `medium` and `xhigh` both resolve to *high*, per the vendor's
# published table. We speak the shared four from `base.REASONING_EFFORTS` and
# keep no local copy of that list: the copy we used to keep is what let
# `xhigh -> max` survive as an escalation nobody had checked against the vendor.


class DeepSeekProvider(AIProvider):
    """
    DeepSeek provider over the **OpenAI-compatible** endpoint (https://api.deepseek.com).

    DeepSeek is pay-per-token with very high concurrency limits (V4-Flash 2500 /
    V4-Pro 500). It was built as the agents' fallback when Z.AI's GLM Coding Plan
    tripped Fair-Usage 1313; that plan is no longer a route this product may take
    at all, and `ZaiProvider` now speaks to Z.AI's prepaid platform API instead.
    Models: deepseek-v4-flash (cheap default), deepseek-v4-pro.

    Architecture mirrors ZaiProvider: the agent layer
    (llm_adapter._chat_native_tools) hands providers Anthropic-shaped
    messages/tools, so generate_with_tools converts Anthropic -> OpenAI on the way
    in and OpenAI tool_calls -> Anthropic-style tool_use objects on the way out.

    DeepSeek-specific (vs ZaiProvider):
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
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url,
                                  **network_client_bounds(config))
        # Kept for the REST balance endpoint (/user/balance); the openai SDK doesn't cover it.
        self._api_key = api_key
        self._base_url = base_url

        self.max_tokens = config.get("max_tokens", 8192)

        # DeepSeek V4 thinking (enabled by default; reasoning returned in
        # reasoning_content). When disabled we must send {type: disabled} to
        # override DeepSeek's default-on behaviour.
        self.thinking_enabled = config.get("thinking", {}).get("enabled", True)

        # Optional reasoning effort (top-of-body via extra_body).
        self._reasoning_effort = self._normalize_effort(config.get("reasoning_effort"))

        self.top_p = config.get("top_p")  # None => API default
        self._temperature_explicit = config.get("temperature")  # None unless user set it

        # Exponential backoff with a time budget (default 10 min)
        self.max_retry_seconds = config.get("max_retry_seconds", 600)

        self._last_thinking: Optional[str] = None
        # The token accounting of the last call, whichever entry point made it.
        # Cleared at the start of every call so a failure cannot be priced with
        # the numbers of the success before it.
        self._last_usage: Optional[Dict[str, Any]] = None

        # Said once per provider: the reason is a property of the alias, not of
        # the call, so repeating it per request would bury the log.
        self._sampling_inert_logged = False

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

    def get_last_usage(self) -> Optional[Dict[str, Any]]:
        """What the vendor said the last call cost in tokens, or None.

        The caller that most needs this is the one that used to guess: the
        agent's text path counts tokens locally and prices the guess, which on
        this provider is wrong in both directions at once — the cache split is
        invisible, so every prompt token bills as a miss (dearer), and
        reasoning tokens do not appear in the answer text (cheaper)."""
        return self._last_usage

    @staticmethod
    def _usage_from_response(u: Any) -> Dict[str, Any]:
        """The vendor's token accounting, in this project's shape.

        DeepSeek reports the cache split natively (prompt_tokens = hit + miss)
        and leaves the OpenAI-compat `prompt_tokens_details.cached_tokens` at
        0, so the native fields win and the compat one is only a fallback."""
        prompt_tokens = getattr(u, "prompt_tokens", 0) or 0
        _hit = getattr(u, "prompt_cache_hit_tokens", None)
        _miss = getattr(u, "prompt_cache_miss_tokens", None)
        if _hit is None:
            _details = getattr(u, "prompt_tokens_details", None)
            _hit = getattr(_details, "cached_tokens", 0) if _details else 0
        if _miss is None:
            _miss = max(0, prompt_tokens - (_hit or 0))
        _completion = getattr(u, "completion_tokens", 0) or 0
        _ctd = getattr(u, "completion_tokens_details", None)
        _reasoning = (getattr(_ctd, "reasoning_tokens", 0) or 0) if _ctd else 0
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": _completion,
            "reasoning_tokens": _reasoning,
            "content_tokens": max(0, _completion - _reasoning),
            "total_tokens": getattr(u, "total_tokens", 0) or 0,
            "cache_read_input_tokens": _hit or 0,
            "prompt_cache_hit_tokens": _hit or 0,
            "prompt_cache_miss_tokens": _miss or 0,
        }

    def _effort_label(self, requested: Optional[str], extra_body: Dict[str, Any]) -> str:
        """What the usage line should say this call asked for.

        Read out of the request body it lies, and it lies about the loudest
        choice there is: `off` never travels as an effort — it turns the
        thinking block off — so a body with no `reasoning_effort` key was
        reported as `server-default`, i.e. as nobody having expressed a
        preference. Found by the live acceptance of `34b3933d` on 2026-08-16,
        where the room was switched to `off` at 20:37:13 and the four calls
        that followed all logged `server-default`; a colleague read those lines
        as evidence the room was on `high`.

        Four words, and the two silences are kept apart on purpose: `off` (this
        call asked for it), `alias-off` (this alias is configured never to
        think), a level, or `server-default` (nobody said anything)."""
        if extra_body.get("thinking", {}).get("type") == "disabled":
            return REASONING_OFF if self._normalize_effort(requested) == REASONING_OFF else "alias-off"
        return extra_body.get("reasoning_effort", "server-default")

    def _record_usage(
        self,
        raw_usage: Any,
        *,
        path: str,
        conversation_id: Optional[str] = None,
        tool_calls: int = 0,
        effort: Any = "server-default",
    ) -> Dict[str, Any]:
        """Keep the accounting and write the one line the burn history is made of.

        `path` is new and it is not decoration: until 2026-08-16 this line was
        written by the tool path alone, so the 5,406 lines the burn rate was
        measured from are all `tools`. Plain calls joining that series unmarked
        would change what the file means with nothing in the file to say so."""
        usage = self._usage_from_response(raw_usage) if raw_usage is not None else None
        if usage is None:
            return {}
        self._last_usage = usage
        logger.info(
            "DeepSeek usage: alias=%s conv=%s prompt=%d (hit=%d/miss=%d), "
            "completion=%d (reasoning=%d/content=%d), tool_calls=%d, effort=%s, path=%s",
            self.alias, conversation_id or "-", usage["prompt_tokens"],
            usage["prompt_cache_hit_tokens"], usage["prompt_cache_miss_tokens"],
            usage["completion_tokens"], usage["reasoning_tokens"],
            usage["content_tokens"], tool_calls, effort, path,
        )
        return usage

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
        """The shared vocabulary, so this provider and Ollama read one word the
        same way. Returns None for empty or unrecognised (-> the caller's
        fallback).

        This used to map `xhigh -> max` to match another tool's spelling. The
        vendor's own table says `xhigh` means *high*
        (api-docs.deepseek.com/guides/thinking_mode), and `max` is a different,
        dearer effort — so the rewrite was quietly upgrading whoever asked for
        `xhigh`, not translating them."""
        return normalize_reasoning_effort(value)

    def _thinking_for_call(self, reasoning_effort: Optional[str] = None) -> bool:
        """Whether this one call reasons: the header's `off` beats the alias.

        Until now the only switch was `thinking.enabled` in providers.json —
        per alias, therefore shared by every conversation that alias serves.
        The header could raise the effort and never lower it past the floor,
        because the vendor's own `none` effort does **not** disable anything
        while the request still carries `thinking: {type: enabled}`; the off
        switch is the thinking block itself, which no per-call path reached."""
        return self.thinking_enabled and self._normalize_effort(reasoning_effort) != REASONING_OFF

    def _build_extra_body(self, reasoning_effort: Optional[str] = None) -> Dict[str, Any]:
        """DeepSeek thinking toggle. Always sent — {type: disabled} is required to
        override the default-on thinking. A per-call reasoning_effort (e.g. a UI
        toggle) wins over the provider-config default; a None/invalid override falls
        back to the config value, so callers that pass nothing keep the configured
        effort (no silent downgrade). Either is only sent when thinking is enabled.

        `off` is not an effort and never reaches the wire as one: it turns the
        thinking block to disabled, which is the only thing DeepSeek listens to."""
        thinking = self._thinking_for_call(reasoning_effort)
        body: Dict[str, Any] = {
            "thinking": {"type": "enabled" if thinking else "disabled"}
        }
        # No second filter for `off` here, though one looks natural: `off` is
        # the one word that makes `thinking` false above, so the branch below is
        # already unreachable for it. Written as a guard it would never fire —
        # and a test can pass over a guard that cannot fire without exercising
        # anything, which is how a defence becomes decoration.
        effort = self._normalize_effort(reasoning_effort) or self._reasoning_effort
        if thinking and effort:
            body["reasoning_effort"] = effort
        return body

    def _sampling_params(
        self,
        temperature_override: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """`temperature` and `top_p` — or neither, while thinking is on.

        The vendor documents both (with the two penalties) as accepted and
        ignored in thinking mode. We had never checked that, and a change that
        stops sending a field must not rest on somebody's sentence, so it was
        measured on `deepseek-v4-flash`, 5 replies per cell to "name one
        animal": thinking **off**, temperature 0.0 returned the same word 5/5
        and 2.0 returned 5 different ones — the probe can see the field. With
        thinking **on**, temperature 0.0 returned 4 different words out of 5.
        A temperature that is honoured collapses that cell; this one did not.

        So the number changes no answer, and sending it changes only what the
        operator believes: both live aliases configure 0.6 and think on every
        call, which makes the field in the editor a dial wired to nothing.
        Withholding it says so in the log instead. If DeepSeek ever begins
        honouring it, this is one condition to delete.

        The condition is the *call's* thinking state, not the alias's: a header
        set to Off turns the field back into a live control for that call, and
        withholding it there would take away sampling exactly where it works."""
        if not self._thinking_for_call(reasoning_effort):
            params: Dict[str, Any] = {"temperature": self._effective_temperature(temperature_override)}
            if self.top_p is not None:
                params["top_p"] = self.top_p
            return params
        if not self._sampling_inert_logged:
            self._sampling_inert_logged = True
            logger.info(
                "DeepSeekProvider '%s': temperature=%s and top_p=%s not sent — the "
                "API ignores them while thinking is enabled (measured 2026-08-15). "
                "Turn thinking off for this alias if you need to steer sampling.",
                self.alias,
                self._effective_temperature(temperature_override),
                self.top_p,
            )
        return {}

    def _effective_temperature(self, override: Optional[float] = None) -> float:
        if override is not None:
            return override
        if self._temperature_explicit is not None:
            return self._temperature_explicit
        return 1.0

    # --- plain text generation ---

    async def generate_response(self, prompt: str, **kwargs) -> str:
        """Non-streaming text generation.

        Returns the text, and leaves the accounting on `get_last_usage()` —
        sleep synthesis, the AI chat and everything else without tools comes
        through here, and until 2026-08-16 every one of those calls was priced
        by a guess with no record of its own."""
        self._last_thinking = None
        self._last_usage = None

        async def _call():
            # The caller's effort, not the alias ceiling. This read used to be
            # missing: `_build_extra_body()` took no argument here while the tools
            # path passed one, so sleep, knowledge extraction and Local AI Chat
            # could only ever run at the alias default — max on both DeepSeek
            # aliases at the time it was measured. The label at the bottom of this
            # function always read the caller's wish correctly, which is why the
            # gap survived: nothing lied, the wish simply never reached the wire.
            effort = kwargs.get("reasoning_effort")
            extra_body = self._build_extra_body(effort)
            params: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "extra_body": extra_body,
                **self._sampling_params(kwargs.get("temperature"), reasoning_effort=effort),
            }
            resp = await self.client.chat.completions.create(**params)
            msg = resp.choices[0].message
            self._last_thinking = getattr(msg, "reasoning_content", None)
            self._record_usage(
                getattr(resp, "usage", None),
                path="plain",
                conversation_id=kwargs.get("conversation_id"),
                effort=self._effort_label(kwargs.get("reasoning_effort"), extra_body),
            )
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
        reasoning_effort: str = None,
    ) -> str:
        """Streaming text generation. Calls on_chunk(text, conversation_id) per token.

        `reasoning_effort` had no parameter to arrive through until 2026-08-24, so a
        caller could not have lowered it if it wanted to — the signature itself was
        half of the defect."""
        self._last_thinking = None
        self._last_usage = None

        async def _call():
            extra_body = self._build_extra_body(reasoning_effort)
            params: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "extra_body": extra_body,
                "stream": True,
                # A stream reports its usage only if asked, in a final chunk
                # that carries no choices — which is why the skip below now
                # reads the chunk before discarding it.
                "stream_options": {"include_usage": True},
                **self._sampling_params(reasoning_effort=reasoning_effort),
            }

            full_text = ""
            thinking_text = ""
            stream = await self.client.chat.completions.create(**params)
            async for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    self._record_usage(
                        chunk_usage,
                        path="plain-stream",
                        conversation_id=conversation_id,
                        effort=self._effort_label(None, extra_body),
                    )
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
                # `_call` is the whole stream: the retry re-runs it, and it delivers
                # every token through `on_chunk` on its way to returning `full_text`.
                # Sending that return value to `on_chunk` as well put the answer on the
                # wire a second time, after the reader already had it token by token.
                #
                # It does not double the bill — `on_chunk` reaches
                # `agent_manager.emit_stream_chunk`, which only appends and broadcasts.
                # It does something that lasts longer: `_raw` becomes the answer twice,
                # so `_streaming_raw` stops matching `response` (`agent_manager.py:1168`)
                # where it is normally None, and the doubled text is written to
                # conversation history and read back as context by every later turn.
                # (Found by Ark in `zai_provider.py`, 2026-08-23; these two were the
                # rest of the class.)
                return await self._retry_with_backoff(_call, e)
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
        self._last_usage = None
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
                "extra_body": extra_body,
                **self._sampling_params(reasoning_effort=reasoning_effort),
            }

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

            usage = self._record_usage(
                resp.usage,
                path="tools",
                conversation_id=conversation_id,
                tool_calls=len(tool_calls_raw),
                effort=self._effort_label(reasoning_effort, extra_body),
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
