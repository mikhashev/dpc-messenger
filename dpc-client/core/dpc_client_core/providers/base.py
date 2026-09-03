# dpc_client_core/providers/base.py
# Base class, shared exceptions, shared constants, and shared utilities for all AI providers.

import itertools
import logging
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)

# --- Custom Exceptions ---

class ModelNotCachedError(Exception):
    """Raised when a model is not found in local cache and needs to be downloaded."""
    def __init__(self, model_name: str, cache_path: str, download_size_gb: float = 3.0):
        self.model_name = model_name
        self.cache_path = cache_path
        self.download_size_gb = download_size_gb
        super().__init__(f"Model '{model_name}' not found in cache: {cache_path}")

class ProviderRetryCancelled(Exception):
    """The interface abandoned a call that was waiting out a backoff.

    An ordinary exception on purpose. Cancelling the surrounding task would do
    the same job and would also skip every `except Exception` between here and
    the caller — including the one that answers the UI — leaving the request
    that was cancelled with no reply at all.
    """


# --- Shared reasoning-effort vocabulary ---

# The words the chat header offers, in ascending order of intent. They are an
# ordinal, not a calibration: each provider maps them onto whatever its own API
# and model actually do, and the same word buys different depths in different
# places. Ollama takes these four and never sees `max`: the Python SDK types
# the field `Literal['low','medium','high']`, so we send `high` in its place.
# Whether that loses anything is per model — on qwen3.8 a fixed seed made the
# two byte-identical (2026-08-15), on muse-glimmer two independent sweeps
# separated them. This comment said "the daemon treats max as high" until
# 2026-08-16, which was one model's result written as the daemon's rule.
# DeepSeek accepts
# seven words and runs three efforts, aliasing `medium` and `xhigh` onto `high`
# — that one is the vendor's published table, not our measurement; ours was too
# weak to separate them and only agrees with it. A shared *spelling* is the most
# that can be shared: a shared mapping would have to be wrong somewhere.
REASONING_EFFORTS = ("low", "medium", "high", "max")

# The foot of the same scale, and deliberately not a member of it: `off` is not
# an amount of thinking, and code that iterates the levels must not offer it as
# one. It sits here so the header can carry a single ordered control — off, low,
# medium, high — in which the contradictory state (an effort chosen while
# thinking is off) cannot be expressed at all. Each provider translates it into
# its own way of saying no: Ollama `think=False`, the one value every model
# accepts; DeepSeek `thinking: {type: disabled}`, because its own `none` effort
# does not disable anything while the request still asks to think.
REASONING_OFF = "off"


def normalize_reasoning_effort(value: Optional[str]) -> Optional[str]:
    """The requested effort as one of `REASONING_EFFORTS`, or `REASONING_OFF`,
    or None if it is neither.

    `xhigh` is folded into `high` because that is what the one vendor who
    publishes a table says it means (api-docs.deepseek.com/guides/thinking_mode:
    `xhigh -> high`), and because Ollama's daemon refuses the word outright.
    Rewriting it to `max` — which this codebase did until 2026-08-15 — sent a
    caller asking for one notch above high to the most expensive effort the API
    has, which is an escalation wearing the clothes of a translation.

    Returning None for anything else is deliberate: an unknown word must not be
    guessed at. What the caller's provider does with None is the provider's
    decision, and it should say so in the log rather than substitute silently.
    """
    word = (value or "").strip().lower()
    if word == "xhigh":
        word = "high"
    if word == REASONING_OFF:
        return REASONING_OFF
    return word if word in REASONING_EFFORTS else None


# --- Shared thinking model constants ---

OPENAI_THINKING_MODELS = [
    "o1", "o1-mini", "o1-preview", "o1-pro",
    "o3", "o3-mini", "o3-pro",
    "o4-mini",
]

ANTHROPIC_THINKING_MODELS = [
    "claude-3-7",       # Claude 3.7 Sonnet (extended thinking)
    "claude-opus-4",    # Claude Opus 4 (extended thinking)
    "claude-sonnet-4",  # Claude Sonnet 4 (extended thinking)
    "claude-haiku-4",   # Claude Haiku 4 (extended thinking)
]


def never_connected(error: BaseException) -> bool:
    """True when the connection was never established.

    A 429 or a 502 means the service answered and is busy; a connect timeout means
    nothing answered, and waiting inside one call does not bring a route back. Both
    belong in the same retryable set and want different amounts of patience.
    """
    seen: set = set()
    e: Optional[BaseException] = error
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if type(e).__name__ in ("ConnectTimeout", "ConnectError", "ConnectionRefusedError"):
            return True
        e = e.__cause__ or e.__context__
    return False


# Set once at startup by whoever can reach the interface. A module-level slot
# rather than a constructor argument because providers are built in several
# places and none of them holds the interface.
_retry_observer: Optional[Any] = None


def set_retry_observer(observer: Optional[Any]) -> None:
    """Register the callable that carries retry notices to the interface.

    It is called from the retry loop and must not raise: a provider that failed
    to tell anyone is still retrying, and an exception here would replace a
    recoverable error with an unrecoverable one.
    """
    global _retry_observer
    _retry_observer = observer


# One flag per wait, by the id carried in the notice. Setting it is how the
# interface says stop; the loop watches it beside every sleep and every call.
_retry_waiters: Dict[str, Any] = {}
_retry_seq = itertools.count(1)


def register_retry_waiter(retry_id: str, flag: Any) -> None:
    """Remember the flag this wait watches, so `cancel_retry` can set it."""
    _retry_waiters[retry_id] = flag


def forget_retry_waiter(retry_id: str) -> None:
    _retry_waiters.pop(retry_id, None)


def cancel_retry(retry_id: str) -> bool:
    """Stop waiting, and abandon the request with it.

    Returns False when the id is unknown or the wait already ended — the
    ordinary case for a click that lands just as the call recovers, and not an
    error.
    """
    flag = _retry_waiters.get(retry_id)
    if flag is None or flag.is_set():
        return False
    flag.set()
    return True


def _drop(future) -> None:
    """Let go of a future whose answer we no longer want.

    Deliberately not a coroutine. Awaiting a cancelled future here would open a
    window in which the surrounding task's own cancellation arrives and gets
    swallowed by the same `except` — which is how shutdown stops working. The
    callback exists only to retrieve the result, so a discarded task does not
    log «exception was never retrieved».
    """
    future.cancel()
    future.add_done_callback(lambda f: f.cancelled() or f.exception())


async def sleep_unless_cancelled(seconds: float, flag: Any) -> None:
    """Wait out the backoff, or stop the moment the flag is set.

    The wait is `asyncio.sleep`, raced against the flag, and not
    `wait_for(flag.wait(), timeout=...)`. The two are equivalent against a real
    clock and not at all against a frozen one: `wait_for` measures with the
    event loop's own timer, so a test that fakes `time.monotonic` — as the
    budget tests do — leaves that timer unable to fire and the loop waits
    forever. Going through `asyncio.sleep` keeps the wait where every such test
    already patches it.
    """
    import asyncio
    if seconds <= 0:
        return
    sleeper = asyncio.ensure_future(asyncio.sleep(seconds))
    stop = asyncio.ensure_future(flag.wait())
    try:
        await asyncio.wait({sleeper, stop}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        _drop(sleeper)
        _drop(stop)
        raise
    if stop.done():
        _drop(sleeper)
        raise ProviderRetryCancelled("the wait was cancelled")
    _drop(stop)


async def call_unless_cancelled(fn, timeout: float, flag: Any):
    """Run `fn`, but stop waiting on it if the flag is set.

    A hung call can hold the rest of the budget on its own, so the flag has to
    reach here too and not only the sleep before it.
    """
    import asyncio
    call = asyncio.ensure_future(fn())
    stop = asyncio.ensure_future(flag.wait())
    try:
        done, _ = await asyncio.wait(
            {call, stop}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    except BaseException:
        _drop(call)
        _drop(stop)
        raise
    _drop(stop)
    if call in done:
        return call.result()
    _drop(call)
    if stop in done:
        raise ProviderRetryCancelled("the call was abandoned")
    raise asyncio.TimeoutError()


def announce_retry(payload: Dict[str, Any]) -> None:
    """Say that an attempt is about to wait, and for how long."""
    if _retry_observer is None:
        return
    try:
        _retry_observer("provider_retry", payload)
    except Exception:
        logger.debug("retry observer raised on provider_retry", exc_info=True)


def announce_retry_finished(retry_id: str, alias: str, outcome: str, attempts: int) -> None:
    """Say how a run of retries ended: `recovered` or `failed`.

    Sent when the loop ends, so an interface showing "retry 3" always has
    something that clears it: `recovered`, `failed`, or `cancelled`. The one
    ending that sends nothing is the surrounding task being cancelled, which
    happens at shutdown, when there is no interface left to tell.
    """
    if _retry_observer is None:
        return
    try:
        _retry_observer("provider_retry_finished",
                        {"retry_id": retry_id, "alias": alias,
                         "outcome": outcome, "attempts": attempts})
    except Exception:
        logger.debug("retry observer raised on provider_retry_finished", exc_info=True)


def parse_thinking_tags(content: str) -> Tuple[str, Optional[str]]:
    r"""
    Parse <think\>...</think\> tags from model response content.

    Used by DeepSeek R1 and similar models that embed thinking/reasoning
    in their response using XML-style tags.

    Args:
        content: Raw response content that may contain <think\> tags

    Returns:
        Tuple of (final_content, thinking_content):
        - final_content: Content with <think\> tags removed
        - thinking_content: Extracted thinking text, or None if no tags found
    """
    import re

    # Pattern matches <think\>...</think\> with any content inside (including newlines)
    think_pattern = r'<think\s*>(.*?)</think\s*>'
    matches = re.findall(think_pattern, content, re.DOTALL | re.IGNORECASE)

    if matches:
        # Join multiple thinking blocks with newlines
        thinking = '\n'.join(match.strip() for match in matches if match.strip())

        # Remove thinking tags from final content
        final_content = re.sub(think_pattern, '', content, flags=re.DOTALL | re.IGNORECASE).strip()

        return final_content, thinking if thinking else None

    return content, None


# --- Shared network bounds ---

# The openai and anthropic SDKs default to read=600 with two automatic retries;
# a client built with no timeout inherits half an hour on a dead socket. `read`
# is httpx's wait for any byte, so streaming resets it and a non-streamed
# reasoning call is the case that needs `timeout_seconds` raised.
NETWORK_CONNECT_TIMEOUT = 10.0
NETWORK_READ_TIMEOUT = 300.0
NETWORK_WRITE_TIMEOUT = 60.0
NETWORK_POOL_TIMEOUT = 10.0
# Retries multiply the wait. One keeps the transient 429/5xx handling.
NETWORK_MAX_RETRIES = 1


def network_client_bounds(config: Dict[str, Any],
                          default_retries: int = NETWORK_MAX_RETRIES) -> Dict[str, Any]:
    """Client kwargs — `timeout` and `max_retries` — for an SDK backed by httpx.

    Overridable per provider via `timeout_seconds`, `connect_timeout_seconds`,
    `write_timeout_seconds` and `max_retries`. A value that is not a positive
    number falls back to the shared bound, because `0` means «no timeout» to
    httpx and that is the state this exists to prevent.

    `default_retries=0` is for a provider that retries in its own code: two
    layers multiply, and the one with backoff is the one worth keeping.
    """
    import httpx

    def _positive(value, fallback):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        return number if number > 0 else fallback

    try:
        retries = int(config.get("max_retries", default_retries))
    except (TypeError, ValueError):
        retries = default_retries

    return {
        "timeout": httpx.Timeout(
            connect=_positive(config.get("connect_timeout_seconds"), NETWORK_CONNECT_TIMEOUT),
            read=_positive(config.get("timeout_seconds"), NETWORK_READ_TIMEOUT),
            write=_positive(config.get("write_timeout_seconds"), NETWORK_WRITE_TIMEOUT),
            pool=NETWORK_POOL_TIMEOUT,
        ),
        "max_retries": max(0, retries),
    }


# --- Abstract Base Class for all Providers ---

class AIProvider:
    """Abstract base class for all AI providers."""

    # What the retry log calls this provider. One word, because it is read in a
    # log line beside the alias.
    RETRY_LABEL = "Provider"
    # Wall-clock budget for `_retry_with_backoff`, overridden per instance from
    # config by the providers that retry.
    max_retry_seconds: float = 600

    def __init__(self, alias: str, config: Dict[str, Any]):
        self.alias = alias
        self.config = config
        self.model = config.get("model")
        self.temperature = config.get("temperature", 0.7)  # Default temperature for creativity

    def _on_non_retryable(self, error: Exception) -> None:
        """Last look at an error that is about to escape the retry loop.

        A provider with something to say about a particular failure says it here
        rather than by owning a copy of the loop.
        """

    async def _retry_with_backoff(self, fn, last_error: Exception):
        """Retry `fn` on a growing delay until the wall-clock budget is spent.

        The budget is wall time, calls included: counting only the sleeps would
        let a call that always times out spend ten call-lengths rather than ten
        minutes, so both the sleep and the call are clipped to what is left.

        Everything retryable gets the whole budget, a connection that never
        opened included — a route can come back inside it. What bounds the wait
        instead is the caller: every attempt is announced through
        `announce_retry` under a `retry_id`, and `cancel_retry(retry_id)` ends
        both the sleep and any call in flight. Cancelling the surrounding task
        still works too, for shutdown.
        """
        import asyncio
        import time

        started = time.monotonic()
        deadline = started + self.max_retry_seconds
        delay = 3
        attempt = 0
        retry_id = f"{self.alias}:{next(_retry_seq)}"
        stop = asyncio.Event()
        register_retry_waiter(retry_id, stop)
        try:
            return await self._backoff_loop(
                fn, last_error, retry_id, stop, started, deadline, delay, attempt
            )
        finally:
            forget_retry_waiter(retry_id)

    def _cancelled(self, retry_id: str, attempt: int, started: float) -> "ProviderRetryCancelled":
        """Close the notice and build the error the chat will show.

        The message carries the alias because it lands where an answer would
        have been, and «the wait was cancelled» does not say whose.
        """
        import time
        announce_retry_finished(retry_id, self.alias, "cancelled", attempt)
        return ProviderRetryCancelled(
            f"{self.RETRY_LABEL} provider '{self.alias}' was stopped after "
            f"{attempt} {'retry' if attempt == 1 else 'retries'} "
            f"({int(time.monotonic() - started)}s elapsed)"
        )

    async def _backoff_loop(self, fn, last_error, retry_id, stop, started, deadline, delay, attempt):
        """The wait itself. Split out only so the registry entry above is
        removed on every exit, cancellation included."""
        import time

        while time.monotonic() < deadline:
            attempt += 1
            elapsed = int(time.monotonic() - started)
            logger.warning(
                "%s retry %d, waiting %ds (elapsed %ds/%ds): %s",
                self.RETRY_LABEL, attempt, delay, elapsed,
                self.max_retry_seconds, last_error,
            )
            announce_retry({
                "retry_id": retry_id,
                "provider": self.RETRY_LABEL,
                "alias": self.alias,
                "attempt": attempt,
                "waiting_seconds": delay,
                "elapsed_seconds": elapsed,
                "budget_seconds": self.max_retry_seconds,
                "error": str(last_error) or type(last_error).__name__,
                "unreachable": never_connected(last_error),
            })
            try:
                await sleep_unless_cancelled(
                    min(delay, max(0.0, deadline - time.monotonic())), stop
                )
            except ProviderRetryCancelled:
                raise self._cancelled(retry_id, attempt, started) from None
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                result = await call_unless_cancelled(fn, left, stop)
                announce_retry_finished(retry_id, self.alias, "recovered", attempt)
                return result
            except ProviderRetryCancelled:
                raise self._cancelled(retry_id, attempt, started) from None
            except Exception as e:
                last_error = e
                if time.monotonic() >= deadline:
                    break
                if not self._is_retryable(e):
                    self._on_non_retryable(e)
                    announce_retry_finished(retry_id, self.alias, "failed", attempt)
                    raise
                delay = min(delay * 2, 192)
        announce_retry_finished(retry_id, self.alias, "failed", attempt)
        raise RuntimeError(
            f"{self.RETRY_LABEL} provider '{self.alias}' failed after {attempt} retries "
            f"({int(time.monotonic() - started)}s elapsed): {last_error}"
        ) from last_error

    async def generate_response(self, prompt: str, **kwargs) -> str:
        """
        Generates a response from the AI model.

        Args:
            prompt: The input prompt text
            **kwargs: Additional arguments (e.g., conversation_id) for compatibility

        Returns:
            The AI model's response text
        """
        raise NotImplementedError

    def supports_vision(self) -> bool:
        """Returns True if this provider supports vision API (multimodal queries)."""
        return False

    async def generate_with_vision(self, prompt: str, images: List[Dict[str, Any]], **kwargs) -> str:
        """
        Generates a response from the AI model with image inputs (vision API).

        Args:
            prompt: Text prompt
            images: List of image dicts with keys:
                - path: str (absolute path to image file)
                - mime_type: str (e.g., "image/png")
                - base64: str (optional, if already encoded)
            **kwargs: Additional parameters (temperature, max_tokens, etc.)

        Returns:
            str: AI response text

        Raises:
            NotImplementedError: If provider doesn't support vision
        """
        raise NotImplementedError(f"Vision API not implemented for {self.__class__.__name__}")

    def supports_thinking(self) -> bool:
        r"""
        Returns True if this provider supports thinking/reasoning mode.

        Thinking mode models perform extended reasoning before producing
        their final response. Examples include:
        - DeepSeek R1 (with <think\> tags)
        - Claude Extended Thinking (Claude 3.7+, Claude 4+)
        - OpenAI o1/o3 (reasoning models)

        Returns:
            bool: True if thinking mode is supported, False by default
        """
        return False

    def get_thinking_params(self) -> Dict[str, Any]:
        """
        Return provider-specific thinking parameters.

        Override this method to return parameters like:
        - budget_tokens (Claude)
        - reasoning_effort (OpenAI o1/o3)

        Returns:
            Dict with thinking parameters, empty by default
        """
        return {}

    def get_last_usage(self) -> Optional[Dict[str, Any]]:
        """What the vendor said the last call cost in tokens, or None.

        The contract lives here rather than on one provider because of what its
        absence did: `DeepSeekProvider` was the only class carrying it, three
        others built a `usage` dict inside their tools path and returned it
        inline, and the single reader reached for the method through `hasattr`
        (`dpc_agent/llm_adapter.py`). So one provider was priced by what it
        reported and every other one by an estimate the loop computed for
        itself. A fourth private copy is not the risk; a second unread one is.

        `None` means «this provider has not reported anything for the last
        call», which is not the same as «the call was free» — a caller that
        needs a number when there is none must say so, rather than read a zero.
        """
        stored = getattr(self, "_last_usage", None)
        return dict(stored) if stored else None

    def _record_last_usage(self, usage: Optional[Dict[str, Any]]) -> None:
        """Store what the vendor reported for the call that just finished.

        Copied on the way in and on the way out, so a caller that edits the dict
        it was handed does not edit the provider's record of the call.
        """
        self._last_usage = dict(usage) if usage else None

    def supports_balance(self) -> bool:
        """Returns True if this provider can report account balance (pay-per-use APIs)."""
        return False

    async def get_balance(self) -> Dict[str, Any]:
        """
        Return the provider account balance.

        Returns:
            Provider-specific balance payload.

        Raises:
            NotImplementedError: If the provider has no balance API (subscription/local).
        """
        raise NotImplementedError(f"Balance API not implemented for {self.__class__.__name__}")

    def get_state(self) -> dict:
        return {"alias": self.alias, "model": self.model, "type": self.config.get("type")}
