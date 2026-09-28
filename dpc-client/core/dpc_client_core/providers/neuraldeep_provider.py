# dpc_client_core/providers/neuraldeep_provider.py

import asyncio
import os
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, Any, Optional, List, Union

from openai import AsyncOpenAI

from .base import (AIProvider, REASONING_OFF, anthropic_to_openai_messages,
                   configured_reasoning_default, image_base64,
                   network_client_bounds, normalize_reasoning_effort,
                   numeric_setting, positive_ceiling)
from ..dpc_agent.pricing import (COST_BASIS_CHARGED, COST_BASIS_LIST_PRICE_REFERENCE,
                                 COST_BASIS_UNKNOWN, NEURALDEEP_CURRENCY, compute_cost_rub)
from .neuraldeep_prices import REFRESH_INTERVAL, RETRY_AFTER_FAILURE, price_source

logger = logging.getLogger(__name__)

NEURALDEEP_DEFAULT_BASE_URL = "https://api.neuraldeep.ru/v1"
NOREASON_SUFFIX = "-noreason"

# --- /v1/limits reading policy (coddy-agent's internal/session/provider_usage.go,
# providerUsageTTL/providerUsageFloor/providerUsageBackoffCap) --------------------

# An automatic read inside this age is served from the cache without a request.
LIMITS_CACHE_TTL = timedelta(seconds=20)
# The least gap between two actual /limits requests for one provider instance.
LIMITS_FETCH_FLOOR = timedelta(seconds=15)
# A vendor-requested pause (Retry-After / Retry-After-Ms) is honored up to this,
# so a hub asking for hours cannot freeze reads until a restart.
LIMITS_BACKOFF_CAP = timedelta(minutes=5)
# P2b, ADR-041 D5 amendment 2026-09-29: how old the *last successful* /limits
# read may be and still stand in for a read that just failed. Both doors ask
# `get_balance()`, never `_read_limits()` directly, so one ceiling here is one
# ceiling on both — the peer door and the gateway's owner path read the same
# cache, aged the same way, and diverge only in what they do once it is too
# old (the peer door refuses; the gateway's owner path proceeds, ADR-041 D3:
# a guest's ceiling is bounded fail-closed, the owner's own loopback is not).
STALE_QUOTA_MAX_AGE = timedelta(minutes=5)
# The schema this decoder understands (coddy's neuralDeepUsageSchema); a
# different number means fields may have been renamed, and reading it anyway
# risks painting wrong numbers.
LIMITS_SCHEMA = 1

# `get_balance()`'s `error` values on a failed read; the shape stays backward
# compatible (is_available/balance_infos/billing_mode/limits/quota) otherwise.
ERROR_KEY_REJECTED = "key_rejected"
ERROR_KEY_BLOCKED = "key_blocked"
ERROR_INVALID = "invalid"
ERROR_UNAVAILABLE = "unavailable"


class NeuralDeepLimitsError(RuntimeError):
    """A `/v1/limits` read failed; `kind` is one of the ERROR_* constants."""
    kind = ERROR_UNAVAILABLE


class NeuralDeepKeyRejected(NeuralDeepLimitsError):
    """HTTP 401: the key is sticky-rejected until the provider is re-created."""
    kind = ERROR_KEY_REJECTED


class NeuralDeepKeyBlocked(NeuralDeepLimitsError):
    """HTTP 403: the key/account is blocked, not merely out of quota."""
    kind = ERROR_KEY_BLOCKED


class NeuralDeepLimitsInvalid(NeuralDeepLimitsError):
    """The payload does not look like a schema-1 `/v1/limits` answer."""
    kind = ERROR_INVALID

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

# `cost_basis` on a priced usage record (words from `pricing.COST_BASES`):
# a wallet key is `charged`, a subscription key `list_price_reference`, and a
# key whose /limits could not be read `unknown`.

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


# --- the key's model list (GET /v1/models), for the providers editor ---

MODEL_KINDS = ("chat", "embedding", "rerank", "stt", "other")
# Chat families by id prefix. Anything not recognised is `other`, not `chat`:
# offering an OCR or TTS model as a chat model is the worse mistake.
_CHAT_PREFIXES = ("qwen", "gemma", "gpt-oss", "kimi", "llama", "mistral",
                  "deepseek", "glm", "gpt-", "t-pro", "t-lite", "gigachat",
                  "yandexgpt", "phi-", "command-")
_NOT_CHAT_MARKERS = ("ocr", "tts", "diariz", "speech", "audio", "image", "moderation")


def model_kind(model_id: str) -> str:
    """chat / embedding / rerank / stt / other, from the id alone.

    The endpoint returns OpenAI-shaped rows (id, object, owned_by) with no
    type, so the kind is read from the name; an unfamiliar name is `other`."""
    name = (model_id or "").strip().lower()
    if "rerank" in name:
        return "rerank"
    # `frida` is NeuralDeep's RU-first embedding model (1536-dim per llms.txt);
    # its id carries no marker, so it is named here — seen on the live list 2026-09-27.
    if "embed" in name or name == "frida" or name.startswith(("bge-", "e5-", "multilingual-e5", "gte-")):
        return "embedding"
    if any(m in name for m in ("whisper", "gigaam", "stt", "asr", "transcri")):
        return "stt"
    if any(m in name for m in _NOT_CHAT_MARKERS):
        return "other"
    base = base_model(name)
    if base in _REASONING_MODELS or base in _VISION_MODELS or base.startswith(_CHAT_PREFIXES):
        return "chat"
    return "other"


def sorted_models(model_ids: List[str]) -> List[Dict[str, str]]:
    """[{id, kind}] with chat first, each `-noreason` twin right after its base."""
    rows = [{"id": mid, "kind": model_kind(mid)} for mid in dict.fromkeys(model_ids) if mid]
    return sorted(rows, key=lambda r: (MODEL_KINDS.index(r["kind"]), base_model(r["id"]),
                                       r["id"].lower().endswith(NOREASON_SUFFIX), r["id"]))


async def fetch_model_ids(base_url: str, api_key: str, timeout: float = 10.0) -> List[str]:
    """The model ids GET {base_url}/models returns for this key.

    Raises PermissionError on 401/403 and lets httpx errors through."""
    import httpx
    url = (base_url or NEURALDEEP_DEFAULT_BASE_URL).rstrip("/") + "/models"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code in (401, 403):
        raise PermissionError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    body = resp.json()
    rows = body.get("data", []) if isinstance(body, dict) else body
    return [str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")]


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
      - prices are roubles from the vendor's live list (`neuraldeep_prices`);
        the usage dict carries `cost_amount` with `cost_currency`, the list's
        `cost_price_list_at` and `cost_basis`, never a dollar `cost`.
      - one image per request (vendor docs).
    """

    RETRY_LABEL = "NeuralDeep"
    # The unit of every `cost_amount` this provider reports, and of its daily
    # ceiling in `compute.vendor_quotas`.
    BILLING_CURRENCY = NEURALDEEP_CURRENCY
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
        # `billing_mode` from /limits, read at most once per REFRESH_INTERVAL.
        self._billing_mode: Optional[str] = None
        self._billing_mode_read_at: Optional[datetime] = None
        self._billing_mode_failed_at: Optional[datetime] = None

        # --- /v1/limits cache (see LIMITS_CACHE_TTL/LIMITS_FETCH_FLOOR above) ---
        self._limits_cache: Optional[Dict[str, Any]] = None
        self._limits_cache_at: Optional[datetime] = None
        self._limits_last_attempt_at: Optional[datetime] = None
        self._limits_key_rejected = False
        self._limits_backoff_until: Optional[datetime] = None
        # Which kind of failure put us into backoff (ERROR_KEY_BLOCKED for a
        # 403, None for a plain non-2xx) — so a read that lands inside the
        # backoff window with no cache to fall back on reports the same
        # kind get_balance would have reported the first time, instead of
        # collapsing to "unavailable".
        self._limits_backoff_kind: Optional[str] = None
        # Concurrent readers (get_balance, _billing_mode_now, the
        # get_provider_balances fan-out) join this fetch instead of each
        # starting their own — coddy's `inflight` (internal/session/
        # provider_usage.go:452-510).
        self._limits_inflight: Optional["asyncio.Future[Dict[str, Any]]"] = None

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
        return usage

    async def _billing_mode_now(self) -> Optional[str]:
        """This key's `billing_mode`, re-read from /limits once a day.

        `get_balance()` no longer raises on a failed read (it returns an
        `error`-carrying dict instead, see `_read_limits`/`_error_balance`),
        so a failure is read off that `error` key here rather than off an
        exception — a transient 503, an empty backoff, an invalid schema or
        a rejected key must all still count as *failures* and keep the last
        known `_billing_mode`, or a single bad read would flip every priced
        call's `cost_basis` to `unknown` for a full day (the vendor ceiling
        counts `unknown` as spend, which is exactly the wrong direction for
        a subscription key)."""
        now = datetime.now(timezone.utc)
        if self._billing_mode_read_at and now - self._billing_mode_read_at < REFRESH_INTERVAL:
            return self._billing_mode
        if self._billing_mode_failed_at and now - self._billing_mode_failed_at < RETRY_AFTER_FAILURE:
            return self._billing_mode
        try:
            balance = await self.get_balance()
            if balance.get("error"):
                raise NeuralDeepLimitsError(balance.get("error_detail") or balance["error"])
            self._billing_mode = balance.get("billing_mode")
            self._billing_mode_read_at = now
            self._billing_mode_failed_at = None
        except Exception as exc:
            self._billing_mode_failed_at = now
            logger.warning("NeuralDeep %s: /limits unreadable (%s: %s); billing_mode stays %s",
                           self.alias, type(exc).__name__, exc, self._billing_mode)
        return self._billing_mode

    async def _price_usage(self, usage: Dict[str, Any], model: str) -> None:
        """Add the RUB cost from the live list, or `cost_amount` None with the reason."""
        usage["cost_amount"] = None
        price_list = await price_source().current()
        if price_list is None:
            usage["cost_unpriced_reason"] = "no price list: fetch failed and nothing cached"
            return
        usage["cost_price_list_at"] = price_list.fetched_at_iso
        row = price_list.rows.get((model or "").strip().lower())
        if row is None:
            usage["cost_unpriced_reason"] = f"model {model!r} is not in the price list"
            return
        amount = compute_cost_rub(
            row, usage["prompt_tokens"], usage["completion_tokens"],
            cache_hit_tokens=usage["cache_read_input_tokens"],
            output_includes_thinking=self.DECLARED_OUTPUT_INCLUDES_THINKING,
        )
        if amount is None:
            usage["cost_unpriced_reason"] = (
                f"model {model!r} has no per-token price in the list "
                f"(billing={row.get('billing')!r})")
            return
        usage["cost_amount"] = amount
        usage["cost_currency"] = NEURALDEEP_CURRENCY
        mode = await self._billing_mode_now()
        usage["cost_basis"] = (
            COST_BASIS_LIST_PRICE_REFERENCE if mode == "subscription"
            else COST_BASIS_CHARGED if mode else COST_BASIS_UNKNOWN)

    async def _record_usage(self, raw_usage: Any, *, path: str, model: str,
                            served_effort: Optional[str], tool_calls: int = 0) -> Dict[str, Any]:
        if raw_usage is None:
            return {}
        usage = self._usage_from_response(raw_usage, model)
        usage["served_effort"] = served_effort
        try:
            await self._price_usage(usage, model)
        except Exception as exc:  # a price must never fail the call
            logger.error("NeuralDeep %s: pricing failed", self.alias, exc_info=True)
            usage["cost_amount"] = None
            usage["cost_unpriced_reason"] = f"pricing failed: {type(exc).__name__}"
        self._record_last_usage(usage)
        logger.info(
            "NeuralDeep usage: alias=%s model=%s prompt=%d (cached=%d), completion=%d "
            "(reasoning=%s), cost=%s %s %s, tool_calls=%d, effort=%s, path=%s",
            self.alias, model, usage["prompt_tokens"], usage["cache_read_input_tokens"],
            usage["completion_tokens"], usage.get("reasoning_tokens"),
            f"{usage['cost_amount']:.4f}" if usage.get("cost_amount") is not None
            else "unpriced", usage.get("cost_currency", ""),
            usage.get("cost_basis") or usage.get("cost_unpriced_reason", ""),
            tool_calls, served_effort, path,
        )
        return usage

    # --- balance ---

    def supports_balance(self) -> bool:
        return True

    def reports_billing_mode(self) -> bool:
        return True

    @staticmethod
    def _validate_limits_payload(payload: Any) -> None:
        """Schema-1 validation, mirroring coddy's `NeuralDeepUsage.validate()`
        (internal/llm/neuraldeep_usage.go:231-248): a renamed or dropped block
        must read as invalid, never as an account with nothing left — a
        missing `decision` must not be read as "blocked" and a missing `chat`
        must not be read as "no windows"."""
        if not isinstance(payload, dict):
            raise NeuralDeepLimitsInvalid("payload is not an object")
        if payload.get("schema") != LIMITS_SCHEMA:
            raise NeuralDeepLimitsInvalid(f"schema {payload.get('schema')!r}, want {LIMITS_SCHEMA}")
        if not str(payload.get("tier") or "").strip():
            raise NeuralDeepLimitsInvalid("payload without tier")
        if not isinstance(payload.get("decision"), dict):
            raise NeuralDeepLimitsInvalid("payload without decision")
        if not isinstance(payload.get("chat"), dict):
            raise NeuralDeepLimitsInvalid("payload without chat")
        key = payload.get("key")
        if not isinstance(key, dict) or not str(key.get("billing_mode") or "").strip():
            raise NeuralDeepLimitsInvalid("payload without key.billing_mode")
        blocked_models = payload.get("blocked_models")
        if blocked_models is not None and not isinstance(blocked_models, list):
            raise NeuralDeepLimitsInvalid("blocked_models is not a list")

    @staticmethod
    def _parse_retry_after(headers: Any) -> Optional[timedelta]:
        """Retry-After (seconds or an HTTP-date) or Retry-After-Ms; None when
        the response asked for no pause (coddy's `parseUsageRetryAfter`)."""
        ms = headers.get("retry-after-ms")
        if ms:
            try:
                value = float(ms)
                if value > 0:
                    return timedelta(milliseconds=value)
            except (TypeError, ValueError):
                pass
        raw = (headers.get("retry-after") or "").strip()
        if not raw:
            return None
        try:
            seconds = float(raw)
            return timedelta(seconds=seconds) if seconds > 0 else None
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            delta = when - datetime.now(timezone.utc)
            return delta if delta.total_seconds() > 0 else None
        except (TypeError, ValueError, IndexError):
            return None

    async def _read_limits(self) -> Dict[str, Any]:
        """The current `/v1/limits` payload, honoring the cache TTL, the fetch
        floor, a vendor backoff and the sticky 401 (coddy's
        `internal/session/provider_usage.go`). Raises a `NeuralDeepLimitsError`
        subclass on failure and never performs more than one HTTP request per
        `LIMITS_FETCH_FLOOR`, nor any once the key has been rejected."""
        if self._limits_key_rejected:
            raise NeuralDeepKeyRejected(
                f"NeuralDeep key for '{self.alias}' was rejected (401); "
                "re-create the provider (a config change or restart) to retry")

        now = datetime.now(timezone.utc)
        if self._limits_cache is not None and self._limits_cache_at is not None \
                and now - self._limits_cache_at < LIMITS_CACHE_TTL:
            return self._limits_cache
        if self._limits_backoff_until is not None and now < self._limits_backoff_until:
            if self._limits_cache is not None:
                return self._limits_cache
            if self._limits_backoff_kind == ERROR_KEY_BLOCKED:
                raise NeuralDeepKeyBlocked(
                    f"NeuralDeep '{self.alias}': in backoff (blocked, 403) "
                    f"until {self._limits_backoff_until.isoformat()}")
            raise NeuralDeepLimitsError(
                f"NeuralDeep '{self.alias}': in backoff until {self._limits_backoff_until.isoformat()}")
        if self._limits_inflight is None and self._limits_last_attempt_at is not None \
                and now - self._limits_last_attempt_at < LIMITS_FETCH_FLOOR:
            if self._limits_cache is not None:
                return self._limits_cache
            raise NeuralDeepLimitsError(
                f"NeuralDeep '{self.alias}': under the fetch floor, no cache yet")

        return await self._read_limits_coalesced()

    async def _read_limits_coalesced(self) -> Dict[str, Any]:
        """Concurrent callers join one in-flight `/limits` fetch instead of
        each starting their own — coddy's `usageStartFetchLocked`/`inflight`
        (internal/session/provider_usage.go:452-510): a burst of readers
        (`get_balance`, `_billing_mode_now`, the `get_provider_balances`
        fan-out) must cost exactly one HTTP request, not one per caller.
        No `await` happens between checking `self._limits_inflight` and
        setting it, so this is race-free on the single-threaded event loop."""
        if self._limits_inflight is not None:
            return await self._limits_inflight
        fut: "asyncio.Future[Dict[str, Any]]" = asyncio.get_event_loop().create_future()
        self._limits_inflight = fut
        try:
            payload = await self._fetch_limits()
        except asyncio.CancelledError:
            # The leader's own cancellation still propagates below, but a
            # joiner must not see it as *its* cancellation — it gets a plain
            # failure instead, same as any other unreadable fetch.
            if not fut.done():
                fut.set_exception(NeuralDeepLimitsError(
                    f"NeuralDeep '{self.alias}': /limits fetch was cancelled"))
                fut.exception()
            raise
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
                fut.exception()  # mark retrieved: no "exception never retrieved" if nobody joined
            raise
        else:
            if not fut.done():
                fut.set_result(payload)
            return payload
        finally:
            self._limits_inflight = None

    async def _fetch_limits(self) -> Dict[str, Any]:
        """The one HTTP GET of `/v1/limits` a coalesced read performs.

        403 backs off like any other non-2xx (5 min cap absent a vendor
        Retry-After) rather than raising once and being polled again every
        floor interval forever; 401 alone is sticky, because only 401 means
        the key itself is wrong."""
        import httpx
        attempted_at = datetime.now(timezone.utc)
        url = self._base_url.rstrip("/") + "/limits"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        self._limits_last_attempt_at = attempted_at
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code == 401:
            self._limits_key_rejected = True
            raise NeuralDeepKeyRejected(f"NeuralDeep key for '{self.alias}' rejected (401)")
        if resp.status_code == 403:
            arrived = datetime.now(timezone.utc)
            retry_after = self._parse_retry_after(resp.headers)
            capped = min(retry_after, LIMITS_BACKOFF_CAP) if retry_after is not None else LIMITS_BACKOFF_CAP
            self._limits_backoff_until = arrived + capped
            self._limits_backoff_kind = ERROR_KEY_BLOCKED
            raise NeuralDeepKeyBlocked(f"NeuralDeep key for '{self.alias}' blocked (403)")
        if resp.status_code < 200 or resp.status_code >= 300:
            arrived = datetime.now(timezone.utc)
            retry_after = self._parse_retry_after(resp.headers)
            if retry_after is not None:
                capped = min(retry_after, LIMITS_BACKOFF_CAP)
                self._limits_backoff_until = arrived + capped
            # Reset regardless of whether a Retry-After header was present —
            # this is a non-403 failure, so a kind left over from an earlier
            # 403 (ERROR_KEY_BLOCKED) must not survive to mislabel the next
            # backoff raise (line ~504) as key-blocked when it wasn't.
            self._limits_backoff_kind = None
            stale = self._stale_cache_within(STALE_QUOTA_MAX_AGE)
            if stale is not None:
                return stale
            raise NeuralDeepLimitsError(
                f"NeuralDeep '{self.alias}': /limits answered HTTP {resp.status_code}")

        payload = resp.json()
        self._validate_limits_payload(payload)
        arrived = datetime.now(timezone.utc)
        self._limits_cache, self._limits_cache_at = payload, arrived
        self._limits_backoff_until = None
        self._limits_backoff_kind = None
        return payload

    @staticmethod
    def _error_balance(kind: str, detail: str) -> Dict[str, Any]:
        """The shape `get_balance()` answers with on a failed read: the same
        keys as a success, all empty/None, plus `error`/`error_detail`."""
        return {
            "is_available": False,
            "balance_infos": [],
            "billing_mode": None,
            "limits": {},
            "quota": None,
            "error": kind,
            "error_detail": detail,
        }

    def _stale_cache_within(self, max_age: timedelta) -> Optional[Dict[str, Any]]:
        """The last successfully read `/limits` payload, if one exists and is
        no older than `max_age`; None otherwise. Distinct from `_read_limits`'s
        own short `LIMITS_CACHE_TTL`/backoff caches (which serve a *fresh*
        read from cache to save a request): this is the P2b fallback a
        *failed* read reaches for, so both doors see the same last-known-good
        state instead of one building `billing_mode=None` from a transient
        failure and the other reading its own separately-cached quota."""
        if self._limits_cache is None or self._limits_cache_at is None:
            return None
        age = datetime.now(timezone.utc) - self._limits_cache_at
        return self._limits_cache if age <= max_age else None

    def _quota_age_sec(self) -> Optional[float]:
        """How old the cache backing this `get_balance()` answer is, in
        seconds — P2b: exposed so a caller can log or reason about staleness
        rather than treat every answer as equally fresh."""
        if self._limits_cache_at is None:
            return None
        return (datetime.now(timezone.utc) - self._limits_cache_at).total_seconds()

    async def get_balance(self) -> Dict[str, Any]:
        """The wallet from GET /v1/limits, in the shape the balance pill reads.

        Documented in https://neuraldeep.ru/llms-full.txt ("Остатки лимитов"):
        read-only, spends no quota. `billing_mode` is `subscription` or the
        wallet; the wallet balance exists either way. The raw payload rides
        along under `limits`, and `quota` (A-VENDOR-KEYS-QUOTA-WINDOWS-ARE-READ-
        AND-NEVER-SHOWN) is the same facts normalized so a host serving this
        key to a peer can see the request quota left, not only the wallet.

        The read goes through `_read_limits()`, so it is cached, floored and
        never talks to the network past a rejected key. On failure this method
        first reaches for the last successfully read payload, if one is no
        older than `STALE_QUOTA_MAX_AGE` (P2b, ADR-041 D5 amendment
        2026-09-29): a `/limits` fetch failing for a few seconds must not make
        this key read as `billing_mode=None` — which the peer door refuses
        fail-closed and the gateway's owner path waves through fail-open — when
        this node in fact knows, to within five minutes, what that key's
        billing_mode and quota are. Only past that age, or with no cache at
        all, does this return `_error_balance()`, with `error` naming the kind
        (`key_rejected`, `key_blocked`, `invalid`, `unavailable`) rather than
        raising. `quota_age_sec` on every answer says how old the underlying
        read is — `0` (or close to it) on a fresh read, larger on a reused one,
        `None` where nothing has ever been read."""
        try:
            limits = await self._read_limits()
        except NeuralDeepLimitsError as exc:
            stale = self._stale_cache_within(STALE_QUOTA_MAX_AGE)
            if stale is not None:
                logger.warning(
                    "NeuralDeep %s: /limits read failed (%s: %s); serving the last known-good "
                    "read from %.0fs ago instead of billing_mode=None (P2b, <= %s)",
                    self.alias, type(exc).__name__, exc, self._quota_age_sec() or 0.0,
                    STALE_QUOTA_MAX_AGE,
                )
                limits = stale
            else:
                return self._error_balance(getattr(exc, "kind", ERROR_UNAVAILABLE), str(exc))
        except Exception as exc:
            stale = self._stale_cache_within(STALE_QUOTA_MAX_AGE)
            if stale is not None:
                limits = stale
            else:
                return self._error_balance(ERROR_UNAVAILABLE, str(exc))

        wallet = limits.get("wallet") or {}
        key = limits.get("key") or {}
        decision = limits.get("decision") or {}
        balance = wallet.get("balance_rub")
        spent_30d = wallet.get("spent_rub_30d")
        balance_info = None
        if balance is not None:
            balance_info = {
                "currency": NEURALDEEP_CURRENCY,
                "total_balance": f"{float(balance):.2f}",
            }
            if spent_30d is not None:
                balance_info["spent_30d"] = f"{float(spent_30d):.2f}"
        try:
            quota = self._quota_from_limits(limits)
        except Exception as exc:  # a quota block must never fail the whole read
            logger.error("NeuralDeep %s: quota normalization failed", self.alias, exc_info=True)
            quota = None
        return {
            "is_available": key.get("status", "ok") == "ok" and decision.get("can_request", True),
            "balance_infos": [] if balance_info is None else [balance_info],
            "billing_mode": key.get("billing_mode"),
            "limits": limits,
            "quota": quota,
            "quota_age_sec": self._quota_age_sec(),
        }

    @staticmethod
    def _quota_from_limits(limits: Dict[str, Any]) -> Dict[str, Any]:
        """Provider-neutral normalization of the vendor's raw `/v1/limits` shape
        (see `get_balance`'s docstring), so another provider can fill the same
        shape later. Missing fields are omitted / None, never a crash — a host
        showing this should never break on a payload shape it hasn't seen yet."""
        key = limits.get("key") or {}
        decision = limits.get("decision") or {}
        chat = limits.get("chat") or {}

        def _window(entry: Optional[Dict[str, Any]], name: str, unit: str = "requests") -> Optional[Dict[str, Any]]:
            if not isinstance(entry, dict):
                return None
            return {
                "name": name,
                "unit": unit,
                "used": entry.get("used"),
                "limit": entry.get("limit"),
                "remaining": entry.get("remaining"),
                "resets_at": entry.get("resets_at"),
                "reset_in_sec": entry.get("reset_in_sec"),
                # The raw wire word, kept beside the normalized `name` (P1b):
                # the vendor spells the ISO week window "iso-week", and a
                # consumer keyed only on the normalized name never sees it.
                "vendor_window": entry.get("window"),
            }

        windows: List[Dict[str, Any]] = []
        session_raw = chat.get("session")
        session = _window(session_raw, (session_raw or {}).get("window") or "3h")
        if session:
            windows.append(session)
        week_raw = chat.get("week")
        # P1b: the vendor spells the week window "iso-week", not "week" —
        # normalized here, once, so every downstream reader keyed on "week"
        # (guest_vendor_quota's `_WINDOW_LENGTHS`, telegram_footer) finds it.
        week_name = str((week_raw or {}).get("window") or "week").lower()
        if week_name == "iso-week":
            week_name = "week"
        week = _window(week_raw, week_name)
        if week:
            windows.append(week)
        rpm = _window(chat.get("rpm"), "minute")
        if rpm:
            windows.append(rpm)

        blocked_models: List[Dict[str, Any]] = []
        for entry in limits.get("blocked_models") or []:
            if not isinstance(entry, dict):
                continue
            model = str(entry.get("model") or "").strip()
            if not model:
                continue
            blocked_models.append({
                "model": model,
                "blocker": entry.get("blocker"),
                "resets_at": entry.get("resets_at"),
                "reset_in_sec": entry.get("reset_in_sec"),
            })

        return {
            "tier": limits.get("tier"),
            "billing_mode": key.get("billing_mode"),
            "can_request": decision.get("can_request"),
            "blockers": decision.get("blockers") or [],
            "retry_after_sec": decision.get("retry_after_sec"),
            "windows": windows,
            "parallel_limit": limits.get("parallel_limit"),
            "observed_at": limits.get("observed_at"),
            # A model gate covers part of the catalogue, not the chat class as
            # a whole: `decision.can_request` stays true while only one model
            # is closed (coddy's own scar, external/cli/usage.go:136-138 — a
            # month of 429s on an account that read healthy). See
            # `model_blocked()` for the matching this list is read through.
            "blocked_models": blocked_models,
            "daily_capacity": NeuralDeepProvider._daily_capacity_from_limits(limits),
            "night": NeuralDeepProvider._night_from_limits(limits),
        }

    @staticmethod
    def _daily_capacity_from_limits(limits: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """How much of today's *money* budget is used — a spend ceiling, not
        a request-count window, so it is kept out of `windows` rather than
        forced into that shape. No longer display-only since ADR-041 D5's
        amendment of 2026-09-29: `exhausted` is read by
        `guest_vendor_quota.guest_vendor_quota_refusal` to refuse a call on
        this key, guest or owner, once the vendor's own daily gate trips.
        `pct_used` arrives
        already on a 0-100 scale: the hub computes it as
        `round(min(spend/budget, 1) * 100, 1)` (coddy's plan,
        docs/plans/neuraldeep-usage.md:290-291; its own test uses 12.5 to
        mean 12.5%), so this must never be multiplied again before display.
        None-safe — an absent or malformed `daily_capacity` block must read
        as "not reported", never crash the whole quota normalization."""
        entry = limits.get("daily_capacity")
        if not isinstance(entry, dict):
            return None
        return {
            "pct_used": entry.get("pct_used"),
            "exhausted": entry.get("exhausted"),
            "resets_at": entry.get("resets_at"),
        }

    @staticmethod
    def _night_from_limits(limits: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Display-only: is the night capacity multiplier active right now,
        and by how much. Every other number this payload carries is already
        effective with the multiplier applied (coddy's plan, :156-157: "every
        number is already effective (the night x2 is applied)") —
        `capacity_factor` is informational only and must never be applied
        again on top of a window's own numbers."""
        entry = limits.get("night")
        if not isinstance(entry, dict):
            return None
        return {
            "active": entry.get("active"),
            "capacity_factor": entry.get("capacity_factor"),
            "window_start_msk": entry.get("window_start_msk"),
            "window_end_msk": entry.get("window_end_msk"),
        }

    def model_blocked(self, model: str) -> Optional[Dict[str, Any]]:
        """The `blocked_models` entry naming `model` — required: pass the
        wire model of the call in question (`_wire_model(effort)`), not this
        alias's default model, matched exactly and case-
        insensitively against the raw block's `model` field — the same
        comparison coddy's own `modelBlocked` makes (external/cli/usage.go:
        139-153): no base/twin normalization on the block itself, so a block
        on `qwen3.8-27b-noreason` matches only that wire name, and a block on
        the base matches only the base.

        Read-only: this never triggers a network call, only looks at
        whatever `/v1/limits` payload is already cached (`None` before the
        first successful read)."""
        if not self._limits_cache:
            return None
        want = model.strip().lower()
        if not want:
            return None
        for entry in self._quota_from_limits(self._limits_cache).get("blocked_models", []):
            if str(entry.get("model", "")).strip().lower() == want:
                return entry
        return None

    # --- retry: uses AIProvider._is_retryable unchanged ---

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
            await self._record_usage(getattr(resp, "usage", None), path="plain",
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
                    await self._record_usage(chunk_usage, path="plain-stream",
                                             model=params["model"],
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

            usage = await self._record_usage(
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
            await self._record_usage(getattr(resp, "usage", None), path="vision",
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
