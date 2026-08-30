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
import base64
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, Optional, List, Tuple, Union

from openai import AsyncOpenAI

from .base import AIProvider, REASONING_OFF
from .deepseek_provider import DeepSeekProvider

from ..managers.llama_server_supervisor import DEFAULTS as SUPERVISOR_DEFAULTS
from ..managers.llama_server_supervisor import LlamaServerSupervisor
from ..managers.llama_server_supervisor import gguf_effort_dictionary

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
    "n_batch", "n_ubatch", "ctx_checkpoints", "checkpoint_min_step",
    "n_parallel", "kv_unified", "cache_reuse", "cache_ram_mib",
    "slot_save_path", "jinja",
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
            # Adoption keeps the running child, and used to keep the whole old
            # config with it — so a key that never reaches the command line was
            # typed, saved, and silently discarded. `start_timeout_s` is the only
            # such key today, and the provider form now offers a control for it,
            # which is what made the drop visible. The flag-bearing subset is
            # equal by the branch condition above, so re-merging here cannot
            # change what the live child was started with; it only lets the
            # non-flag values move without paying for a model re-load.
            previous.config = {
                **SUPERVISOR_DEFAULTS,
                **{k: v for k, v in config.items() if v is not None},
            }
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
                # The successor must not spend VRAM until the predecessor has
                # finished its in-flight work and released the card — this card
                # cannot hold two copies of the model.
                self.supervisor.supersedes(self._retire(previous))
            elif _RETIRING:
                # A rename crosses the key. The old alias is gone from the
                # registry and the new one has never been in it, so the lookup
                # above misses and the successor used to start at once — beside
                # a predecessor that is still draining a generation and still
                # holding the card. The wait belongs to the card, not to the
                # name: whatever is retiring is what has to let go first.
                pending = [task for _s, task in _RETIRING.values() if not task.done()]
                if pending:
                    logger.info(
                        "llamacpp_server '%s': %d retired child(ren) still draining; "
                        "waiting for the card before starting",
                        alias, len(pending),
                    )
                    self.supervisor.supersedes(
                        asyncio.ensure_future(_watch_without_owning(pending))
                    )
        _ACTIVE_SUPERVISORS[alias] = self.supervisor

        self._openai: Optional[AsyncOpenAI] = None
        self._port: Optional[int] = None

        self.max_tokens = config.get("max_tokens", 8192)
        self.thinking_enabled = bool(config.get("thinking", {}).get("enabled", True))
        # The configured effort stays raw: `xhigh` is legal here and must not
        # be folded to `high` on its way in.
        self._reasoning_effort_raw = config.get("reasoning_effort")
        # The words this model's own chat template accepts, read from the file
        # rather than from a table measured against one pin. The constants stay
        # as the fallback for a GGUF whose template does not guard the value.
        self._template_efforts = TEMPLATE_EFFORTS
        self._template_default: Optional[str] = None
        # Whether the words above came from this model or from the table. The UI
        # may only quote them as the model's when they were read from it.
        self._template_efforts_source = "fallback"
        dictionary = gguf_effort_dictionary(config.get("gguf_path") or "")
        if dictionary:
            self._template_efforts, template_default = dictionary
            self._template_default = template_default
            self._template_efforts_source = "model"
            logger.info(
                "llamacpp_server '%s': the model's template accepts %s (default %s)",
                alias, "/".join(self._template_efforts), template_default or "unset",
            )
        self._reasoning_budget = config.get("reasoning_budget_tokens")
        self._mmproj = config.get("mmproj")
        self.top_p = config.get("top_p")
        self.top_k = config.get("top_k")
        self._temperature_explicit = config.get("temperature")
        self.max_retry_seconds = config.get("max_retry_seconds", 600)

        # Present for the inherited DeepSeek scaffolding this class keeps;
        # every entry point that would use `client` is overridden below.
        self._last_thinking: Optional[str] = None
        self._last_usage: Optional[Dict[str, Any]] = None
        self._sampling_inert_logged = False
        self._cot_cache: Dict[str, str] = {}

        self._effort_translation_logged = False
        self._sampling_default_logged = False

    @staticmethod
    def _retire(supervisor: LlamaServerSupervisor) -> Optional["asyncio.Task"]:
        """Drain a superseded child without blocking construction.

        Drain rather than stop, on Mike's rule of 2026-08-22: a settings save
        is applied immediately, but the running child is only replaced once the
        answer it is generating has finished. `stop()` here used to send the
        signal at once, which killed an agent mid-generation — while `close()`
        twenty lines below already drained, so the right behaviour was in the
        file and simply not on this path.

        No deadline: the wait is exactly as long as the work. Returned so the
        successor can wait for the card to be free before it starts; process
        shutdown does not come through here, so this cannot hang the exit.

        When no loop is running (manager built outside an async context) there
        is nothing to schedule on, so the warning is the honest output and the
        OS reaps the child at process exit.
        """
        try:
            return asyncio.get_running_loop().create_task(supervisor.drain(timeout=None))
        except RuntimeError:
            logger.warning(
                "llamacpp_server '%s': no running loop to drain the superseded "
                "child; nothing here signals it and the OS reaps it at process "
                "exit",
                supervisor.alias,
            )
            return None

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
        """Stop the child, then hand the registry back.

        The order is the whole point. This used to pop the supervisor out of
        `_ACTIVE_SUPERVISORS` on the first line and only then close the HTTP
        client — so when the client raised, the drain two lines below never
        ran and `stop_all_supervisors`, the sweep that exists precisely to
        catch a child nobody holds any more, found an empty registry and
        stopped nothing.

        Measured 2026-08-23, 16:13:59: `Error closing provider 'qwen3.8 27b
        Mythos': Event loop is closed`, then `LLMManager shutdown complete`
        with no `llama-server ... stopped` line at all. The child from 15:43
        on port 59649 outlived the service, the next start put a second
        27B server on the same card, and the card looked like it was never
        releasing anything. The safety net was removed by the same call that
        then fell.

        So: the client close can fail as loudly as it likes, the drain runs in
        `finally`, and the registry entry is surrendered last — a child that
        is still running stays reachable from the sweep until it is not.
        """
        try:
            if self._openai is not None and hasattr(self._openai, "close"):
                await self._openai.close()
        except Exception as exc:
            # A dead event loop is the known case (an async tool runs its
            # handler on a throwaway loop, and the client cached here was
            # built on it) but any failure here is survivable — the child is
            # what matters, and it is stopped below either way.
            logger.warning(
                "llamacpp_server '%s': closing the HTTP client failed (%s); "
                "stopping the child anyway",
                self.alias, exc,
            )
        finally:
            self._openai = None
            # drain, not bare stop: in-flight calls get to finish before the
            # child is asked to flush and exit.
            await self.supervisor.drain(timeout=120.0)
            # Only now, with the child actually stopped, is the registry entry
            # surrendered. If the drain above raised, this line is skipped on
            # purpose: the entry stays, `stop_all_supervisors` gets its turn,
            # and a child that cannot be stopped is at least logged as one
            # rather than leaving the process silently.
            if _ACTIVE_SUPERVISORS.get(self.alias) is self.supervisor:
                _ACTIVE_SUPERVISORS.pop(self.alias, None)

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
        if word in self._template_efforts:
            return word
        if word in FLEET_TO_TEMPLATE:
            mapped = FLEET_TO_TEMPLATE[word]
            # A fold onto a rung this model does not have would be the one thing
            # the template refuses outright, so it is dropped instead.
            if mapped not in self._template_efforts:
                logger.warning(
                    "llamacpp_server '%s': effort '%s' folds to '%s', which this "
                    "model's template does not accept (%s) — sending nothing",
                    self.alias, word, mapped, "/".join(self._template_efforts),
                )
                return None
            if not self._effort_translation_logged:
                self._effort_translation_logged = True
                logger.info(
                    "llamacpp_server '%s': effort '%s' -> '%s' — the template's "
                    "dictionary is %s and refuses other words with HTTP 500 "
                    "(measured 2026-08-19)",
                    self.alias, word, mapped, "/".join(self._template_efforts),
                )
            return mapped
        return None

    # The answer reserve of the budget clamp: a budget that leaves no room
    # for content turns "empty answer by truncation" into "clipped answer" —
    # strictly better, and the heavy-turn cure is max_tokens, not a smaller
    # budget (review, 2026-08-20).
    BUDGET_ANSWER_RESERVE = 2048

    def _build_extra_body(
        self,
        reasoning_effort: Optional[str] = None,
        reasoning_budget_tokens: Optional[int] = None,
        effective_max_tokens: Optional[int] = None,
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
            # The clamp: thinking must leave room for the answer inside the
            # output window it shares. A budget above the window never binds
            # (the window ends first) — measured live: budget 10000 at
            # max_tokens 8192 thought 19 019 chars and answered nothing.
            # effective_max_tokens is the SAME number the request carries
            # (per-call in plain/vision, the alias field in tools/stream) —
            # a clamp reading a different window than the wire diverges from
            # the request it guards.
            if effective_max_tokens is not None:
                budget = min(int(budget), effective_max_tokens - self.BUDGET_ANSWER_RESERVE)
                if budget <= 0:
                    budget = 1
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
        """Sampling is always sent: unlike DeepSeek's inert-while-thinking
        dial, this server honours it. `top_k` rides along because the model
        card puts it in its prescription (thinking mode: temperature 1.0,
        top_p 0.95, top_k 20, min_p 0) and a sampling half-configured is how
        a probe ends up greedy without anyone saying so."""
        params: Dict[str, Any] = {"temperature": self._effective_temperature(temperature_override)}
        if self.top_p is not None:
            params["top_p"] = self.top_p
        if self.top_k is not None:
            params["top_k"] = self.top_k
        if (
            not self._sampling_default_logged
            and temperature_override is None
            and self._temperature_explicit is None
            and self.top_p is None
            and self.top_k is None
            and self._template_effort(reasoning_effort) != REASONING_OFF
        ):
            self._sampling_default_logged = True
            logger.info(
                "llamacpp_server '%s': no sampling configured on the alias; using "
                "temperature %s with vendor defaults. The Qwen3.8 card prescribes "
                "temperature 1.0, top_p 0.95, top_k 20, min_p 0 for thinking mode — "
                "set them on the alias unless this model's card says otherwise.",
                self.alias, params["temperature"],
            )
        return params

    def _sampling_on_the_wire(
        self,
        extra_body: Dict[str, Any],
        sampling: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Sampling params the OpenAI SDK can type, plus the ones it cannot.

        `top_k` is not an SDK kwarg — sent top-level it is the 18:43 TypeError
        — but the server reads it fine from the JSON body, so it rides in
        extra_body beside the thinking dialect."""
        if "top_k" in sampling:
            extra_body["top_k"] = sampling.pop("top_k")
        return sampling

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

    @staticmethod
    def _speed_payload(
        prompt_tokens: int,
        completion_tokens: int,
        elapsed_s: Optional[float],
        t_first_chunk_s: Optional[float],
        alias: str,
        model: str,
    ) -> Dict[str, Any]:
        """Per-call speed for the live counter (llama.cpp only; Ollama is out
        of scope by decision). Wall-clock, advisory, not exact: it includes
        RTT to the local server and interpreter pauses; the t0 is taken AFTER
        the server is ensured and the slot entered, so cold start and slot
        queue stay OUT of the number (a cold first call would otherwise read
        as a slow model). The streaming path gets the prefill/decode split
        from the first chunk's arrival (everything before it is prefill);
        non-streaming calls carry total throughput only — a fabricated split
        would be an instrument that looks precise and is not.

        The pinned server exposes no timing surfaces (/slots carries token
        counts only, /metrics is 501 on this pin), so wall-clock boundaries are
        the honest source."""
        if not elapsed_s or elapsed_s <= 0:
            return {}
        out: Dict[str, Any] = {
            "alias": alias,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "elapsed_s": round(elapsed_s, 1),
            "total_tok_s": int((prompt_tokens + completion_tokens) / elapsed_s),
        }
        if t_first_chunk_s and t_first_chunk_s > 0 and (elapsed_s - t_first_chunk_s) > 0:
            out["prefill_tok_s"] = int(prompt_tokens / t_first_chunk_s)
            out["decode_tok_s"] = int(completion_tokens / (elapsed_s - t_first_chunk_s))
        return out

    def _record_usage(
        self,
        raw_usage: Any,
        *,
        path: str,
        conversation_id: Optional[str] = None,
        tool_calls: int = 0,
        effort: Any = "server-default",
        reasoning_text: Optional[str] = None,
        elapsed_s: Optional[float] = None,
        t_first_chunk_s: Optional[float] = None,
        finish_reason: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Same accounting as the parent, under this provider's own name — the
        burn history is grepped by that prefix, and a local box joining the
        `DeepSeek usage:` series would mislabel every line it wrote.

        The pinned server never fills `completion_tokens_details` (measured
        live, 2026-08-20 local wall clock — the A/B ran at 01:36, the vision
        probe of the previous evening at 23:38 was 08-19: different events
        across local midnight, not a drift; 11.8K chars of thinking,
        `reasoning=0` in usage), so when the split is absent and the message
        carried a reasoning block, the reasoning tokens are estimated from
        the text — chars/4, marked `split=estimated` — rather than letting
        the burn history stay blind to the one lever it pays for. The
        estimate is script-blind by design (Cyrillic undercounts ~3-4x,
        TOKEN-ESTIMATE-IS-BLIND-TO-SCRIPT): the total `completion_tokens`
        stays exact so burn cost is never distorted, and the marker says
        what is estimated."""
        usage = self._usage_from_response(raw_usage) if raw_usage is not None else None
        if usage is None:
            return {}
        estimated = False
        if not usage.get("reasoning_tokens") and reasoning_text:
            usage["reasoning_tokens"] = max(1, len(reasoning_text) // 4)
            usage["content_tokens"] = max(0, usage["completion_tokens"] - usage["reasoning_tokens"])
            estimated = True
        # Why the caller gets to see it: `length` is the only signal that
        # separates "the model was cut at the ceiling" from "the model
        # finished on its own", and the two need opposite repairs. It sits on
        # resp.choices[0] on every path and was read by nobody, so a sleep
        # synthesis that died at the cap could not say so — three readers
        # argued for a day over which of the two had happened.
        if finish_reason is not None:
            usage["finish_reason"] = finish_reason
        # The ceiling belongs beside the count it bounds: completion == cap is
        # the fallback truncation signal for the day finish_reason comes back
        # empty, and a count without its window is a number the reader has to
        # already know the answer to. Each call site passes the same
        # expression its request carries as max_tokens.
        if max_tokens is not None:
            usage["max_tokens"] = max_tokens
        self._last_usage = usage
        if elapsed_s is not None and elapsed_s > 0:
            speed = self._speed_payload(
                usage["prompt_tokens"], usage["completion_tokens"],
                elapsed_s, t_first_chunk_s, self.alias, self.model or "",
            )
            # The engine's own per-task timings give every path the phase
            # split, not just the streaming one: the agents' tools path has no
            # first-chunk boundary, but the child's print_timing lines carry
            # exact prompt-eval and eval rates for the finished task.
            timings = None
            if hasattr(self.supervisor, "last_task_timings"):
                try:
                    timings = self.supervisor.last_task_timings()
                except Exception:
                    timings = None
            if timings:
                speed.update({
                    "prefill_tok_s": timings["prefill_tok_s"],
                    "decode_tok_s": timings["decode_tok_s"],
                    "speed_source": "engine",
                })
                # The only place a cache hit is visible. The engine logs a
                # prompt-cache load solely when it FAILS (server-context.cpp:284,
                # b10566) and the save at TRACE, so the whole host cache reads as
                # «two refusals, ever» in a log where it may have worked all week.
                # What it re-evaluated against what we sent is the measurement.
                prefilled = timings.get("engine_prompt_tokens")
                if prefilled is not None and usage["prompt_tokens"] > 0:
                    usage["prefilled_tokens"] = prefilled
                    usage["cached_tokens"] = max(0, usage["prompt_tokens"] - prefilled)
            usage["speed"] = speed
        cached = usage.get("cached_tokens")
        logger.info(
            "llamacpp usage: alias=%s conv=%s prompt=%d, completion=%d "
            "(reasoning=%d/content=%d%s), tool_calls=%d, effort=%s, path=%s"
            "%s%s",
            self.alias, conversation_id or "-", usage["prompt_tokens"],
            usage["completion_tokens"], usage["reasoning_tokens"],
            usage["content_tokens"], ", split=estimated" if estimated else "",
            tool_calls, effort, path,
            f", finish={finish_reason}" if finish_reason else "",
            "" if cached is None else
            f", prefilled={usage['prefilled_tokens']} of {usage['prompt_tokens']}"
            f" (reuse={100 * cached / usage['prompt_tokens']:.1f}%)",
        )
        return usage

    # --- capability surface ----------------------------------------------------

    def supports_vision(self) -> bool:
        """True when the alias names an mmproj, or a live child advertises it.

        /props is the authority once the server is up (probed 2026-08-19:
        `modalities: {vision: true, video: true}` with the projector blob);
        before that, the alias's mmproj is the honest yes — a child started
        without --mmproj serves text and nothing else."""
        if self.supervisor is not None and self.supervisor.props:
            modal = self.supervisor.props.get("modalities") or {}
            if isinstance(modal, dict) and modal.get("vision"):
                return True
        return bool(self._mmproj)

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
            # Speed clock starts AFTER the server is ensured: a cold start
            # (~a minute) inside elapsed would read as a slow model.
            import time as _time
            _t0 = _time.perf_counter()
            # The same expression the request below carries as max_tokens —
            # per-call here, the alias field in tools/stream.
            _eff_max = kwargs.get("max_tokens", self.max_tokens)
            extra_body = self._build_extra_body(
                kwargs.get("reasoning_effort"),
                kwargs.get("reasoning_budget_tokens"),
                effective_max_tokens=_eff_max,
            )
            params: Dict[str, Any] = {
                "model": self._model_name(),
                "max_tokens": kwargs.get("max_tokens", self.max_tokens),
                "messages": [{"role": "user", "content": prompt}],
                "extra_body": extra_body,
                **self._sampling_on_the_wire(extra_body, self._sampling_params(kwargs.get("temperature"))),
            }
            async with self.supervisor.call_slot():
                resp = await client.chat.completions.create(**params)
            _choice = resp.choices[0]
            msg = _choice.message
            self._last_thinking = getattr(msg, "reasoning_content", None)
            self._record_usage(
                getattr(resp, "usage", None),
                path="plain",
                conversation_id=kwargs.get("conversation_id"),
                effort=self._effort_label(kwargs.get("reasoning_effort"), extra_body),
                reasoning_text=self._last_thinking,
                elapsed_s=_time.perf_counter() - _t0,
                finish_reason=getattr(_choice, "finish_reason", None),
                max_tokens=_eff_max,
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
        reasoning_effort: Optional[str] = None,
    ) -> str:
        """Streaming text generation; on_chunk(text, conversation_id) per piece.

        The effort used to be a literal `None` in the body below while the plain
        path two methods up already read the caller's — the same gap DeepSeek had,
        inherited along with the shape."""
        self._last_thinking = None
        self._last_usage = None

        async def _call():
            client = await self._ensure()
            extra_body = self._build_extra_body(
                reasoning_effort, reasoning_budget_tokens,
                effective_max_tokens=self.max_tokens,
            )
            params: Dict[str, Any] = {
                "model": self._model_name(),
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "extra_body": extra_body,
                "stream": True,
                "stream_options": {"include_usage": True},
                **self._sampling_on_the_wire(
                    extra_body, self._sampling_params(reasoning_effort=reasoning_effort)
                ),
            }
            import time as _time
            _t0: Optional[float] = None
            _t_first: Optional[float] = None
            full_text = ""
            thinking_text = ""
            # Carried across chunks: the terminal usage chunk has no choices,
            # so the reason arrives one chunk earlier than the accounting.
            finish_reason = None
            async with self.supervisor.call_slot():
                # Speed clock starts inside the slot: the queue wait before it
                # is contention, not model speed.
                _t0 = _time.perf_counter()
                stream = await client.chat.completions.create(**params)
                async for chunk in stream:
                    if _t_first is None:
                        _t_first = _time.perf_counter() - _t0
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        self._record_usage(
                            chunk_usage,
                            path="plain-stream",
                            conversation_id=conversation_id,
                            effort=self._effort_label(None, extra_body),
                            # The local accumulator, not self._last_thinking:
                            # usage arrives on the terminal chunk, before the
                            # post-loop assignment — the field is still None
                            # here, and the estimate would silently read 0.
                            reasoning_text=thinking_text or None,
                            elapsed_s=_time.perf_counter() - _t0,
                            t_first_chunk_s=_t_first,
                            finish_reason=finish_reason,
                            max_tokens=self.max_tokens,
                        )
                    if not chunk.choices:
                        continue
                    _fr = getattr(chunk.choices[0], "finish_reason", None)
                    if _fr:
                        finish_reason = _fr
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
            import time as _time
            _t0 = _time.perf_counter()  # after ensure: cold start stays out of the number
            extra_body = self._build_extra_body(
                reasoning_effort, reasoning_budget_tokens,
                effective_max_tokens=self.max_tokens,
            )
            params: Dict[str, Any] = {
                "model": self._model_name(),
                "max_tokens": self.max_tokens,
                "messages": openai_messages,
                "tools": openai_tools,
                "tool_choice": "auto",
                "extra_body": extra_body,
                **self._sampling_on_the_wire(extra_body, self._sampling_params(reasoning_effort=reasoning_effort)),
            }
            async with self.supervisor.call_slot():
                resp = await client.chat.completions.create(**params)
            _choice = resp.choices[0]
            msg = _choice.message

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
                reasoning_text=self._last_thinking,
                elapsed_s=_time.perf_counter() - _t0,
                finish_reason=getattr(_choice, "finish_reason", None),
                max_tokens=self.max_tokens,
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
        """Vision on the DPC-owned child: images ride as `image_url` content
        blocks on the same OpenAI-compatible call, the projector comes from
        the alias's mmproj (--mmproj on the child). Probed 2026-08-19: a
        screenshot read accurately at full 262 144 with q4_0 KV; the first
        probe run also showed why thinking stays off unless asked — the
        template's own default (xhigh) spent a whole 300-token window
        thinking and answered nothing."""
        self._last_thinking = None
        self._last_usage = None

        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            b64 = img.get("base64") or self._read_image_as_base64(img.get("path"))
            if not b64:
                # A dropped image is a silent text-only answer unless someone
                # says so here — the caller asked about a picture it will not
                # receive (review note, 2026-08-20).
                logging.getLogger(__name__).warning(
                    "llamacpp_server '%s': vision call dropped an unreadable image "
                    "(path=%s) — answering text-only",
                    self.alias, img.get("path"),
                )
                continue
            if b64.startswith("data:"):
                content.append({"type": "image_url", "image_url": {"url": b64}})
            else:
                mime = img.get("mime_type") or "image/png"
                content.append(
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                )

        async def _call():
            client = await self._ensure()
            import time as _time
            _t0 = _time.perf_counter()  # after ensure: cold start stays out of the number
            effort = kwargs.get("reasoning_effort")
            per_call_budget = kwargs.get("reasoning_budget_tokens")
            if effort is None and per_call_budget is None:
                # A background read, not a reasoning task: thinking off when
                # the caller didn't ask. The ALIAS budget is deliberately not
                # consulted here — it caps thinking for text turns, and
                # letting it fill extra_body would silently re-enable xhigh
                # thinking on every image (the alias carries 10000, so the
                # "empty body" form of this default never fired on production
                # — caught at review, 2026-08-20).
                extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
            else:
                extra_body = self._build_extra_body(
                    effort, per_call_budget,
                    effective_max_tokens=kwargs.get("max_tokens", self.max_tokens),
                )
            params: Dict[str, Any] = {
                "model": self._model_name(),
                "max_tokens": kwargs.get("max_tokens", self.max_tokens),
                "messages": [{"role": "user", "content": content}],
                "extra_body": extra_body,
                **self._sampling_on_the_wire(extra_body, self._sampling_params(kwargs.get("temperature"))),
            }
            async with self.supervisor.call_slot():
                resp = await client.chat.completions.create(**params)
            _choice = resp.choices[0]
            msg = _choice.message
            self._last_thinking = getattr(msg, "reasoning_content", None)
            self._record_usage(
                getattr(resp, "usage", None),
                path="vision",
                conversation_id=kwargs.get("conversation_id"),
                effort=self._effort_label(effort, extra_body),
                reasoning_text=self._last_thinking,
                elapsed_s=_time.perf_counter() - _t0,
                finish_reason=getattr(_choice, "finish_reason", None),
                max_tokens=kwargs.get("max_tokens", self.max_tokens),
            )
            return msg.content or ""

        try:
            return await _call()
        except Exception as e:
            if self._is_retryable(e):
                return await self._retry_with_backoff(_call, e)
            raise RuntimeError(
                f"llamacpp_server vision failed for '{self.alias}': "
                f"{type(e).__name__}: {e}"
            ) from e

    @staticmethod
    def _read_image_as_base64(path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        try:
            return base64.b64encode(Path(path).read_bytes()).decode("ascii")
        except OSError:
            return None

    def _model_name(self) -> str:
        """The server serves exactly one -m model and ignores this field, but
        the SDK requires a string; the GGUF's stem says more than an alias."""
        return self.model or Path(self.config["gguf_path"]).stem


# A child that is draining is in neither list the shutdown used to walk. It leaves
# `_ACTIVE_SUPERVISORS` the moment its alias goes, and the only handle left on it is
# the task `_retire` returns — which `retire_absent` discarded. Between the save that
# retired it and the end of its drain it belonged to nobody, and a shutdown inside
# that window walked past a live twenty-seven-gigabyte process and reported nothing.
# Executed 2026-08-23 against a real child: the process survived the sweep.
#
# Keyed by alias like the main registry, but a second dict rather than a flag on the
# first: an alias can be retired and immediately re-registered by the save that
# renamed it, and the retiring child must not be reachable under the name its
# successor now owns.
_RETIRING: Dict[str, Tuple[LlamaServerSupervisor, "asyncio.Task"]] = {}


async def _watch_without_owning(tasks: List["asyncio.Task"]) -> None:
    """Wait for drains that belong to somebody else, and never cancel them.

    `asyncio.gather` looks like the obvious way to wait for several and is the
    wrong one here: «if the outer Future is cancelled, all children that have
    not completed yet are also cancelled». The successor's wait is exactly the
    thing that gets cancelled — a request times out, a task is torn down — and
    with gather that cancellation travelled down into the predecessor's drain,
    which then never reached `stop()`. Its `_RETIRING` entry was removed by the
    done-callback all the same, so the shutdown sweep could not see it either.

    That is this entry's own defect one layer up, introduced by its fix and
    caught by the live probe on 2026-08-23: the retired child survived the
    shutdown while the sweep reported only the successor. `asyncio.wait` waits
    without owning — cancelling the waiter leaves the tasks alone.
    """
    if tasks:
        await asyncio.wait(tasks)


def retire_absent(known_aliases: Iterable[str]) -> List[str]:
    """Stop children whose alias is gone from the configuration — a rename or a delete.

    The registry is keyed by alias, so a reload that keeps the alias re-registers over
    the same key and a reload that drops it leaves the child reachable from nowhere.
    """
    known = set(known_aliases or ())
    retired = []
    for alias in list(_ACTIVE_SUPERVISORS):
        if alias in known:
            continue
        supervisor = _ACTIVE_SUPERVISORS[alias]
        # `_proc`, not `props`: `props` is the health payload, filled only once the
        # child answers /props, so a child still loading its model reads as one that
        # never started — and the old order popped it out of the registry before
        # deciding, which left exactly that child unreachable by anything.
        if supervisor._proc is None or supervisor._proc.returncode is not None:
            _ACTIVE_SUPERVISORS.pop(alias, None)
            continue
        _ACTIVE_SUPERVISORS.pop(alias, None)
        logger.info(
            "llamacpp_server: alias '%s' is no longer configured; draining its child",
            alias,
        )
        task = LlamaServerProvider._retire(supervisor)
        if task is not None:
            _RETIRING[alias] = (supervisor, task)
            task.add_done_callback(
                lambda _t, _a=alias: _RETIRING.pop(_a, None)
            )
        retired.append(alias)
    return retired


async def stop_all_supervisors() -> List[str]:
    """Stop every live child, whatever provider object happens to hold it.

    Shutdown walks `llm_manager.providers`, and a child can outlive the provider that
    started it, so the registry is the only place that still knows it is running —
    with one exception this function used to walk past: a child whose alias has been
    retired is held only by its drain task, and that task waits as long as the work
    takes. Shutdown is not the moment to wait for it: the drain is cancelled and the
    child stopped, which is the same choice `_retire` records for process exit.
    """
    stopped = []
    for alias in list(_ACTIVE_SUPERVISORS):
        supervisor = _ACTIVE_SUPERVISORS.pop(alias)
        try:
            await supervisor.stop()
            stopped.append(alias)
        except Exception as exc:
            logger.warning(
                "llamacpp_server: could not stop the child of '%s' at shutdown: %s",
                alias, exc,
            )
    for alias in list(_RETIRING):
        supervisor, task = _RETIRING.pop(alias, (None, None))
        if supervisor is None:
            continue
        task.cancel()
        try:
            await supervisor.stop()
            stopped.append(alias)
            logger.warning(
                "llamacpp_server: stopped the still-draining child of retired alias "
                "'%s' — shutdown does not wait for a generation to finish",
                alias,
            )
        except Exception as exc:
            logger.warning(
                "llamacpp_server: could not stop the draining child of '%s': %s",
                alias, exc,
            )
    return stopped
