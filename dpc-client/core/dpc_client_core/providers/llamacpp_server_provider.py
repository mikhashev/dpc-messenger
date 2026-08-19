# dpc_client_core/providers/llamacpp_server_provider.py
"""
LlamaServerProvider — the thirteenth provider type: local inference through a
DPC-owned `llama-server` child (ADR-040, route b2).

The class inherits DeepSeekProvider for what is genuinely shared — the
Anthropic→OpenAI message/tool conversion, the retry-with-backoff scaffold, the
usage record — and overrides every part of that provider's *thinking dialect*,
because the two could not disagree more about who owns the effort vocabulary:

- DeepSeek speaks a seven-word wire with `thinking: {type: …}` blocks and a
  vendor that aliases efforts itself.
- llama-server with `--jinja` speaks **the model's own chat template**, and the
  template is the authority: it hard-raises on words it does not know
  (`high`/`max` → HTTP 500 from a jinja `raise_exception`, measured 2026-08-19
  on pin b10472) and defaults to `xhigh` byte-identically when nothing is sent.

So this provider never runs the shared `normalize_reasoning_effort`, which
folds `xhigh` into `high` — that fold is right for DeepSeek and Ollama and
would delete the top of this model's ladder before the template ever saw the
word. The translation table here is local, loud (one log line per alias), and
measured against the template, per Mike's 2026-08-19 direction that the
dictionary comes from the model's jinja file.
"""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional, List, Union

from openai import AsyncOpenAI

from .base import AIProvider, REASONING_OFF
from .deepseek_provider import DeepSeekProvider

from ..managers.llama_server_supervisor import DEFAULTS as SUPERVISOR_DEFAULTS
from ..managers.llama_server_supervisor import LlamaServerSupervisor

logger = logging.getLogger(__name__)

# The template's own dictionary, measured 2026-08-19 on pin b10472 + qwen3.8:
# thinking scales 6 492 / 14 989 / 33 966 chars across these three words (5.2×),
# and the template default is `xhigh` — byte-identical to asking for it.
TEMPLATE_EFFORTS = ("low", "medium", "xhigh")

# The fleet's header words that the template refuses, mapped onto the nearest
# rung it has. `high` and `max` both land on `xhigh` because the model's ladder
# has no second-from-top to preserve — the cardinality fact from the 2026-08-19
# review: three levels on the model, five in our header.
FLEET_TO_TEMPLATE = {"high": "xhigh", "max": "xhigh"}

# One live child per alias across provider reloads. `save_config` drops old
# provider objects without closing them, and a dropped provider holding a
# 30 GB model would otherwise leak the child until process exit.
_ACTIVE_SUPERVISORS: Dict[str, LlamaServerSupervisor] = {}

# The supervisor keys that change what the child process would be started with.
_FLAG_KEYS = (
    "gguf_path", "binary_path", "n_ctx", "cache_type_k", "cache_type_v",
    "n_gpu_layers", "flash_attn", "mmproj", "spec_type", "spec_draft_n_max",
    "n_parallel", "kv_unified", "cache_ram_mib", "slot_save_path", "jinja",
    "extra_args",
)


def _flags_of(config: Dict[str, Any]) -> tuple:
    """The flag-bearing keys, as the supervisor would merge them.

    The supervisor fills its DEFAULTS over the config it is handed, so an
    absent key and an explicitly-defaulted key must compare equal here —
    otherwise a reload of an unchanged alias would read as a flag change."""
    merged = {**SUPERVISOR_DEFAULTS, **{k: v for k, v in config.items() if v is not None}}
    return tuple(merged.get(k) for k in _FLAG_KEYS)


class LlamaServerProvider(DeepSeekProvider):
    """Local `llama-server` behind the OpenAI-compatible face.

    Lifecycle: the first call starts the child through `LlamaServerSupervisor`
    (fetch-verify the pin, spawn, /health, /props) and points an AsyncOpenAI
    client at `http://127.0.0.1:<port>/v1`; `close()` drains and stops the
    child. Nothing is listening until the first call, so loading this provider
    costs nothing on a box that never routes to it.
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        # AIProvider directly, not DeepSeekProvider.__init__: there is no API
        # key to require, and DeepSeek's effort normalisation must not run on
        # the configured word (see the module docstring for the xhigh fold).
        AIProvider.__init__(self, alias, config)

        if not config.get("gguf_path"):
            raise ValueError(
                f"llamacpp_server provider '{alias}' needs a gguf_path pointing "
                "at the model file to serve"
            )

        # Adopt a live child left by a previous load of the same alias when its
        # flags are unchanged (a reload must not re-load a 30 GB model), and
        # retire it loudly when they are not.
        previous = _ACTIVE_SUPERVISORS.get(alias)
        if (
            previous is not None
            and previous.props is not None
            and _flags_of(previous.config) == _flags_of(config)
        ):
            self.supervisor = previous
            logger.info(
                "llamacpp_server '%s': adopting the already-running llama-server "
                "(flags unchanged by the config reload)",
                alias,
            )
        else:
            if previous is not None and previous.props is not None:
                logger.warning(
                    "llamacpp_server '%s': flags changed by a config reload; the "
                    "old child will be stopped and the model re-loaded",
                    alias,
                )
            self.supervisor = LlamaServerSupervisor(alias, config)
            if previous is not None:
                self._retire(previous)
        _ACTIVE_SUPERVISORS[alias] = self.supervisor

        self._openai: Optional[AsyncOpenAI] = None
        self._port: Optional[int] = None

        self.max_tokens = config.get("max_tokens", 8192)
        self.thinking_enabled = bool(config.get("thinking", {}).get("enabled", True))
        # The configured effort stays raw: `xhigh` is legal here and must not
        # be folded to `high` on its way in.
        self._reasoning_effort_raw = config.get("reasoning_effort")
        self._reasoning_budget = config.get("reasoning_budget_tokens")
        self.top_p = config.get("top_p")
        self._temperature_explicit = config.get("temperature")
        self.max_retry_seconds = config.get("max_retry_seconds", 600)

        # Present for the inherited DeepSeek scaffolding this class keeps;
        # every entry point that would use `client` is overridden below.
        self._last_thinking: Optional[str] = None
        self._last_usage: Optional[Dict[str, Any]] = None
        self._sampling_inert_logged = False
        self._cot_cache: Dict[str, str] = {}

        self._effort_translation_logged = False

    @staticmethod
    def _retire(supervisor: LlamaServerSupervisor) -> None:
        """Stop a superseded child without blocking construction.

        The signal alone makes the child exit and flush; the await in stop()
        just reaps it. When no loop is running (manager built outside async
        context) there is nothing to schedule the reap on, so the warning is
        the honest output and the OS reaps the child at process exit.
        """
        try:
            asyncio.get_running_loop().create_task(supervisor.stop())
        except RuntimeError:
            logger.warning(
                "llamacpp_server '%s': no running loop to stop the superseded "
                "child; it was signalled below if possible",
                supervisor.alias,
            )

    # --- lifecycle -----------------------------------------------------------

    async def _ensure(self) -> AsyncOpenAI:
        """The server is up (started if this is the first call) and the client
        aimed at it. Each retry re-runs this, so a child that died mid-call is
        restarted by the attempt that follows its connection error."""
        props = await self.supervisor.ensure_running()
        if self._openai is None or self._port != self.supervisor.port:
            self._port = self.supervisor.port
            # The server ignores the key but the SDK insists on one.
            self._openai = AsyncOpenAI(
                api_key="local", base_url=f"http://127.0.0.1:{self.supervisor.port}/v1"
            )
        return self._openai

    async def close(self) -> None:
        if _ACTIVE_SUPERVISORS.get(self.alias) is self.supervisor:
            _ACTIVE_SUPERVISORS.pop(self.alias, None)
        if self._openai is not None and hasattr(self._openai, "close"):
            await self._openai.close()
        # drain, not bare stop: in-flight calls get to finish before the child
        # is asked to flush and exit.
        await self.supervisor.drain(timeout=120.0)
        self._openai = None

    # --- the thinking dialect -------------------------------------------------

    def _template_effort(self, requested: Optional[str]) -> Optional[str]:
        """One header word → one word the template accepts (or `off`, or None).

        A per-call word beats the alias config; None everywhere means nobody
        said anything and nothing is sent — which the template answers with
        its own default, `xhigh`."""
        raw = self._reasoning_effort_raw if requested is None else requested
        word = (raw or "").strip().lower()
        if word == REASONING_OFF:
            return REASONING_OFF
        if word in TEMPLATE_EFFORTS:
            return word
        if word in FLEET_TO_TEMPLATE:
            mapped = FLEET_TO_TEMPLATE[word]
            if not self._effort_translation_logged:
                self._effort_translation_logged = True
                logger.info(
                    "llamacpp_server '%s': effort '%s' -> '%s' — the template's "
                    "dictionary is %s and refuses other words with HTTP 500 "
                    "(measured 2026-08-19)",
                    self.alias, word, mapped, "/".join(TEMPLATE_EFFORTS),
                )
            return mapped
        return None

    def _build_extra_body(
        self,
        reasoning_effort: Optional[str] = None,
        reasoning_budget_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """The llama-server request-body dialect.

        `chat_template_kwargs.reasoning_effort` is how `--jinja` servers take
        the effort; `enable_thinking` is the off switch (verification of the
        off path is the cheap probe left open in ADR-040 step 3 — until it
        runs, this line is design pinned by tests, not by a measurement).
        `reasoning_budget_tokens` rides top-level, per-request: a per-call
        value beats the alias config, and neither is sent when thinking is
        off. The budget is what keeps the template default (`xhigh`) from
        spending a whole output window thinking and answering nothing — the
        233K Probe D failure mode."""
        word = self._template_effort(reasoning_effort)
        body: Dict[str, Any] = {}
        if word == REASONING_OFF:
            body["chat_template_kwargs"] = {"enable_thinking": False}
            return body
        if word:
            body["chat_template_kwargs"] = {"reasoning_effort": word}
        budget = (
            reasoning_budget_tokens
            if reasoning_budget_tokens is not None
            else self._reasoning_budget
        )
        # The budget is sent even when no effort word was: the template's own
        # default is `xhigh`, and capping only named efforts would leave the
        # strongest thinking as the one unbounded path — exactly backwards.
        if word != REASONING_OFF and budget:
            body["reasoning_budget_tokens"] = int(budget)
        return body

    def _effort_label(self, requested: Optional[str], extra_body: Dict[str, Any]) -> str:
        """What the usage line says this call asked for — read out of the body
        that was actually built, for the same reason as DeepSeek's: the two
        silences (`off` sent something; nobody sent anything) must not read
        the same."""
        kwargs = extra_body.get("chat_template_kwargs", {})
        if kwargs.get("enable_thinking") is False:
            return REASONING_OFF
        return kwargs.get("reasoning_effort", "server-default")

    def _sampling_params(
        self,
        temperature_override: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Temperature is always sent: unlike DeepSeek's inert-while-thinking
        dial, this server honours it (every 2026-08-19 probe ran temp 0 with
        thinking on and got the determinism it asked for)."""
        params: Dict[str, Any] = {"temperature": self._effective_temperature(temperature_override)}
        if self.top_p is not None:
            params["top_p"] = self.top_p
        return params

    def _effective_temperature(self, override: Optional[float] = None) -> float:
        if override is not None:
            return override
        if self._temperature_explicit is not None:
            return self._temperature_explicit
        return 1.0

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """Connection-class failures only.

        The template's refusal of an unknown effort word arrives as HTTP 500
        with a deterministic jinja exception — retrying it burns the whole
        backoff budget to hear the same refusal again, so `500`/`internal
        server error` are deliberately absent from this list (they are present
        in the DeepSeek version this class otherwise inherits from)."""
        err_str = str(error).lower()
        return any(indicator in err_str for indicator in [
            "429", "502", "503",
            "bad gateway", "service unavailable",
            "timed out", "timeout", "connection reset", "connection error",
            "overloaded", "rate limit",
        ]) or isinstance(error, (ConnectionError, OSError)) or type(error).__name__ in (
            "APIConnectionError", "APITimeoutError",
        )

    def _record_usage(
        self,
        raw_usage: Any,
        *,
        path: str,
        conversation_id: Optional[str] = None,
        tool_calls: int = 0,
        effort: Any = "server-default",
    ) -> Dict[str, Any]:
        """Same accounting as the parent, under this provider's own name — the
        burn history is grepped by that prefix, and a local box joining the
        `DeepSeek usage:` series would mislabel every line it wrote."""
        usage = self._usage_from_response(raw_usage) if raw_usage is not None else None
        if usage is None:
            return {}
        self._last_usage = usage
        logger.info(
            "llamacpp usage: alias=%s conv=%s prompt=%d, completion=%d "
            "(reasoning=%d/content=%d), tool_calls=%d, effort=%s, path=%s",
            self.alias, conversation_id or "-", usage["prompt_tokens"],
            usage["completion_tokens"], usage["reasoning_tokens"],
            usage["content_tokens"], tool_calls, effort, path,
        )
        return usage

    # --- capability surface ----------------------------------------------------

    def supports_vision(self) -> bool:
        """Multimodal needs --mmproj and a vision-capable GGUF; not wired yet."""
        return False

    def supports_thinking(self) -> bool:
        return True

    def get_thinking_params(self) -> Dict[str, Any]:
        return {}

    def supports_balance(self) -> bool:
        return False

    # --- entry points ------------------------------------------------------------

    async def generate_response(self, prompt: str, **kwargs) -> str:
        """Non-streaming text generation; the accounting lands on
        `get_last_usage()` like every other provider."""
        self._last_thinking = None
        self._last_usage = None

        async def _call():
            client = await self._ensure()
            extra_body = self._build_extra_body(
                kwargs.get("reasoning_effort"),
                kwargs.get("reasoning_budget_tokens"),
            )
            params: Dict[str, Any] = {
                "model": self._model_name(),
                "max_tokens": kwargs.get("max_tokens", self.max_tokens),
                "messages": [{"role": "user", "content": prompt}],
                "extra_body": extra_body,
                **self._sampling_params(kwargs.get("temperature")),
            }
            async with self.supervisor.call_slot():
                resp = await client.chat.completions.create(**params)
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
                f"llamacpp_server provider '{self.alias}' failed: {type(e).__name__}: {e}"
            ) from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: Optional[callable] = None,
        conversation_id: Optional[str] = None,
        reasoning_budget_tokens: Optional[int] = None,
    ) -> str:
        """Streaming text generation; on_chunk(text, conversation_id) per piece."""
        self._last_thinking = None
        self._last_usage = None

        async def _call():
            client = await self._ensure()
            extra_body = self._build_extra_body(None, reasoning_budget_tokens)
            params: Dict[str, Any] = {
                "model": self._model_name(),
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "extra_body": extra_body,
                "stream": True,
                "stream_options": {"include_usage": True},
                **self._sampling_params(),
            }
            full_text = ""
            thinking_text = ""
            async with self.supervisor.call_slot():
                stream = await client.chat.completions.create(**params)
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
                logger.info("llamacpp streaming thinking: %d chars", len(thinking_text))
            return full_text

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                result = await self._retry_with_backoff(_call, e)
                if on_chunk and result:
                    await on_chunk(result, conversation_id)
                return result
            logger.error("llamacpp streaming failed: %s", e, exc_info=True)
            raise RuntimeError(
                f"llamacpp_server streaming provider '{self.alias}' failed: "
                f"{type(e).__name__}: {e}"
            ) from e

    async def generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system: Union[str, List[Dict[str, Any]]] = "",
        on_chunk: Optional[callable] = None,
        conversation_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        reasoning_budget_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Native tool calling, Anthropic-shape in and out, on the local server."""
        self._last_thinking = None
        self._last_usage = None
        openai_messages = self._anthropic_to_openai_messages(system, messages)
        openai_tools = self._anthropic_to_openai_tools(tools)
        # No reasoning_content padding on replay: the HTTP-400-if-absent rule is
        # DeepSeek's, not the template's — qwen3.8's template accepts an
        # assistant tool-call message with no reasoning attached.

        async def _call():
            client = await self._ensure()
            extra_body = self._build_extra_body(reasoning_effort, reasoning_budget_tokens)
            params: Dict[str, Any] = {
                "model": self._model_name(),
                "max_tokens": self.max_tokens,
                "messages": openai_messages,
                "tools": openai_tools,
                "tool_choice": "auto",
                "extra_body": extra_body,
                **self._sampling_params(reasoning_effort=reasoning_effort),
            }
            async with self.supervisor.call_slot():
                resp = await client.chat.completions.create(**params)
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
                f"llamacpp_server native tool calling failed for '{self.alias}': "
                f"{type(e).__name__}: {e}"
            ) from e

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        raise NotImplementedError(
            f"Vision is not wired for llamacpp_server '{self.alias}' yet "
            "(needs --mmproj and a multimodal GGUF)"
        )

    def _model_name(self) -> str:
        """The server serves exactly one -m model and ignores this field, but
        the SDK requires a string; the GGUF's stem says more than an alias."""
        return self.model or Path(self.config["gguf_path"]).stem
