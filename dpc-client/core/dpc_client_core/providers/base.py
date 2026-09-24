# dpc_client_core/providers/base.py
# Base class, shared exceptions, shared constants, and shared utilities for all AI providers.

import itertools
import json
import math
import logging
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)

# --- Custom Exceptions ---

class ModelNotCachedError(Exception):
    """Raised when a model is not found in local cache and needs to be downloaded.

    One class for every model this app may fetch — Whisper and the embedding
    model both raise it, so there is one catcher shape and one consent dialog
    for either, per the 2026-09-24 owner decision. `size_bytes`/`size_source`
    replace the old hardcoded `download_size_gb=3.0`: a caller now gets a
    measured or API-derived number, or an honest `None` ("size unknown") —
    see `providers/model_sizes.py`. `download_size_gb` is kept and derived
    from `size_bytes` for the two or three older catchers that still read it.
    """
    def __init__(
        self,
        model_name: str,
        cache_path: str,
        revision: Optional[str] = None,
        size_bytes: Optional[int] = None,
        size_source: Optional[str] = None,
        download_size_gb: Optional[float] = None,
    ):
        self.model_name = model_name
        self.cache_path = cache_path
        self.revision = revision
        self.size_bytes = size_bytes
        self.size_source = size_source
        self.download_size_gb = (
            download_size_gb if download_size_gb is not None
            else (round(size_bytes / (1024 ** 3), 2) if size_bytes else None)
        )
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


def declared_reasoning_words(provider: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    """`(words, default)` this alias can be asked for, in three answers.

    The class states first which words it can put on the wire at all
    (`AIProvider.reasoning_words_served`); only where it answers «the shared
    scale» does the model's own chat template get the last word:

    - a list the model named, with the default it named beside it — present
      only when the ladder was read from the model, so a fallback table never
      reaches the UI, a peer or a refusal wearing the model's name;
    - `(None, None)`: the shared scale stands in;
    - `([], None)`: no effort word reaches this class's engine. An empty
      vocabulary is a statement, not a silence: both doors refuse every word
      for such an alias and its menu row promises none.

    One reader for all three — the provider rows, the peer menu and the
    gateway's door — so an alias cannot be told it knows one set of words while
    it is offered another. An object that is not a provider declares nothing
    and is answered by the template rule alone; that is not the same thing as a
    provider declaring no effort channel.
    """
    ask = getattr(provider, "reasoning_words_served", None)
    if callable(ask):
        try:
            words = ask()
        except Exception as e:
            logger.debug("Provider %r could not state its effort words: %s", provider, e)
            words = []
        if words is not None:
            return [w for w in words if isinstance(w, str) and w.strip()], None
    if getattr(provider, "_template_efforts_source", None) != "model":
        return None, None
    return list(provider._template_efforts), provider._template_default


def reasoning_word_for(provider: Any, word: Optional[str]) -> Optional[str]:
    """The word `provider` would actually send when asked for `word`, or None
    when it would send nothing.

    A provider whose ladder is its model's own answers for itself — the
    llama-server one folds `high` and `max` onto the rung its template has —
    and for the rest the answer is the normalised word, checked against the
    vocabulary the alias declared. An alias that declared none sends nothing,
    `off` included: a class with no effort channel has no way of saying no
    either, and a row naming `off` there would name a knob nobody turned.

    Asked before a call, this says whether the request can be served at all;
    asked after one, it says which rung it ran on, which is not always the word
    the caller used.
    """
    if word is None:
        return None
    resolve = getattr(provider, "_template_effort", None)
    if callable(resolve):
        try:
            return resolve(word)
        except Exception as e:  # a provider that cannot answer has not refused
            logger.debug("Provider %r could not resolve effort %r: %s", provider, word, e)
            return normalize_reasoning_effort(word)
    folded = normalize_reasoning_effort(word)
    words, _default = declared_reasoning_words(provider)
    if words is None:
        return folded
    return folded if folded is not None and folded in words else None


def numeric_setting(value: Any) -> Optional[Any]:
    """`value` as a number a menu row may quote, or None.

    A finite int or float and not a bool: `True` is an int here and NaN is a
    float, and either one on a row is a dial nobody set.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def positive_ceiling(value: Any) -> Optional[int]:
    """`value` as an output ceiling, or None.

    A ceiling is a positive whole number of tokens; Ollama's `num_predict` also
    takes -1 and -2, which mean «no ceiling», and those are absent rather than
    reported as a limit.
    """
    number = numeric_setting(value)
    return int(number) if number is not None and number > 0 else None


def configured_reasoning_default(provider: Any) -> Optional[str]:
    """The alias's configured `reasoning_effort` resolved onto its own ladder,
    and the default its model's template named where nothing is configured.

    The rule for a class that reads `reasoning_effort` off its alias config.
    A class that reads some other key states its own default instead
    (`AIProvider.reasoning_default_served`): reading this one on its behalf
    reports a word that class never sends.
    """
    configured = (getattr(provider, "config", None) or {}).get("reasoning_effort")
    if isinstance(configured, str) and configured.strip():
        return reasoning_word_for(provider, configured)
    return declared_reasoning_words(provider)[1]


def effective_reasoning_default(provider: Any) -> Optional[str]:
    """The rung this alias runs at when nobody asks for one, or None.

    The class answers for itself, because which config key decides the rung is
    a fact about the code that builds the request. One reader for the menu row
    and the peer door, so the `reasoning_default` a row promises is the rung a
    guest that asks for nothing is actually served.

    None is «no word describes it»: a class with no effort channel, an alias
    whose template named no default and whose configuration names nothing, or a
    configured word the alias has no rung for — never `off`, which is a rung.
    """
    ask = getattr(provider, "reasoning_default_served", None)
    if callable(ask):
        try:
            return ask()
        except Exception as e:
            logger.debug("Provider %r could not state its default effort: %s", provider, e)
            return None
    return configured_reasoning_default(provider)


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

# The waits that have announced themselves, by the same id, holding the attempt
# number the last notice carried. An id enters on the first `announce_retry` and
# leaves on the first `announce_retry_finished`; that pairing is what makes the
# closing notice idempotent, and is what bounds this — `_retry_with_backoff`
# closes in a `finally` every id its loop opened.
_retry_announced: Dict[str, int] = {}


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
    retry_id = payload.get("retry_id")
    if retry_id:
        # Before the notice goes out, not after: an observer that raises may
        # still have left a row on screen, and an unneeded close deletes a key
        # that is not there while a missing one leaves the row forever.
        _retry_announced[retry_id] = int(payload.get("attempt") or 0)
    try:
        _retry_observer("provider_retry", payload)
    except Exception:
        logger.debug("retry observer raised on provider_retry", exc_info=True)


def announce_retry_finished(retry_id: str, alias: str, outcome: str,
                            attempts: Optional[int] = None) -> None:
    """Close the notice a run of retries opened: `recovered`, `failed`,
    `cancelled`, or `abandoned` when the surrounding task was cancelled.

    Idempotent by `retry_id`, and silent for an id that never announced a wait
    — so the caller may be a `finally` that announces unconditionally, and a
    wait abandoned before its first notice opens no row to clear.

    `attempts` defaults to the number the last notice carried, which is what
    such a `finally` does not know.
    """
    announced = _retry_announced.pop(retry_id, None)
    if announced is None or _retry_observer is None:
        return
    try:
        _retry_observer("provider_retry_finished",
                        {"retry_id": retry_id, "alias": alias, "outcome": outcome,
                         "attempts": announced if attempts is None else attempts})
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


# --- Shared image handling ---


def image_base64(img: Dict[str, Any], alias: str) -> str:
    """The pixels of one image, or a refusal naming the provider and the missing field.

    An image can arrive here straight off the wire (REMOTE_INFERENCE_REQUEST,
    through `llm_manager.query(images=…)`), and `path` there is the sender's
    original filename — DPTP §3.4 requires `base64` and promises the receiver
    nothing about `path`. Opening it resolves nowhere on another machine and, on
    a like one, may resolve to a different file, which the model would then
    describe as though it were the image that was sent. So no base64 is a refusal
    rather than a local file, and the refusal names the alias because it reaches
    the caller through `llm_manager`.

    The returned string is the raw base64: a `data:` prefix is stripped here so
    each call site does not carry its own copy of that.
    """
    data = img.get("base64")
    if not data:
        raise ValueError(
            f"Provider '{alias}' was given an image carrying no base64 data "
            f"(keys present: {sorted(img)}). DPTP §3.4 requires base64; 'path' is "
            f"the sender's original filename, not a file this machine may open."
        )
    return data.split(",", 1)[1] if data.startswith("data:") else data


# --- Shared Anthropic -> OpenAI conversation ---


def anthropic_to_openai_messages(
    system: Any,
    messages: List[Dict[str, Any]],
    reasoning_echo: bool = False,
    *,
    provider: Any = None,
) -> List[Dict[str, Any]]:
    """The Anthropic Messages conversation as OpenAI chat messages: the one
    converter behind DeepSeek, Z.AI and llama-server, and, reshaped onto its
    native wire, Ollama.

    An `image` block stays in its own turn at its own position: that turn
    becomes an array of text and `image_url` parts; a turn with no image keeps
    its joined string. Beside `tool_result` blocks, the `role: tool` messages
    come first — the OpenAI shape wants them directly after the assistant's
    tool calls — and the rest of the turn follows as one `role: user` message.

    `reasoning_echo` pads `reasoning_content` onto replayed tool-call turns
    (DeepSeek thinking mode). `provider`, when given, is asked
    `supports_vision()` at the first image, and a no refuses rather than
    sending the turn with the picture gone. Without it nobody is asked:
    `flatten_messages` renders text and sends no picture anywhere.
    """
    alias = getattr(provider, "alias", None) or "unknown"
    vision_confirmed = provider is None

    def image_part(block: Dict[str, Any], where: str) -> Dict[str, Any]:
        nonlocal vision_confirmed
        if not vision_confirmed:
            if not provider.supports_vision():
                raise ValueError(
                    f"Provider '{alias}' (model: {getattr(provider, 'model', 'unknown')}) has no "
                    f"vision path, and {where} is an image; send the conversation to a "
                    "vision-capable alias, or without the image."
                )
            vision_confirmed = True
        source = block.get("source")
        kind = source.get("type") if isinstance(source, dict) else None
        data = source.get("data") if kind == "base64" else None
        if not data:
            # A url or file source would have this node fetch on a caller's behalf.
            raise ValueError(
                f"Provider '{alias}' was given {where} whose source is not "
                f"{{type: base64, media_type, data}} (source type: {kind!r})."
            )
        media_type = source.get("media_type") or "image/png"
        return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}

    out: List[Dict[str, Any]] = []
    if system:
        sys_text = system if isinstance(system, str) else "".join(
            b.get("text", "") for b in system if isinstance(b, dict)
        )
        if sys_text:
            out.append({"role": "system", "content": sys_text})

    for position, m in enumerate(messages):
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
            tool_messages: List[Dict[str, Any]] = []
            parts: List[Dict[str, Any]] = []  # text and image parts, in block order
            for index, b in enumerate(blocks):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                where = f"messages[{position}] content[{index}]"
                if bt == "tool_result":
                    tr_content = b.get("content", "")
                    if isinstance(tr_content, list):
                        # `role: tool` carries only a string in the OpenAI shape, so
                        # an image a tool returned rides in the user message after.
                        for inner_index, inner in enumerate(tr_content):
                            if isinstance(inner, dict) and inner.get("type") == "image":
                                parts.append(image_part(inner, f"{where} content[{inner_index}]"))
                        tr_content = "".join(
                            inner.get("text", "") for inner in tr_content
                            if isinstance(inner, dict)
                        )
                    tool_messages.append({
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id", ""),
                        "content": str(tr_content),
                    })
                elif bt == "text":
                    parts.append({"type": "text", "text": b.get("text", "")})
                elif bt == "image":
                    parts.append(image_part(b, where))
            out.extend(tool_messages)
            if any(p["type"] == "image_url" for p in parts):
                out.append({"role": "user", "content": [
                    p for p in parts if p["type"] == "image_url" or p["text"]
                ]})
            else:
                text = "".join(p["text"] for p in parts)
                # No empty user turn after tool results: a chat template may refuse one.
                if not tool_messages or text.strip():
                    out.append({"role": "user", "content": text})
            continue

        # Fallback: stringify unknown block content
        out.append({"role": role or "user", "content": json.dumps(blocks)})

    return out


def image_blocks_in_turns(messages: Any) -> int:
    """How many `image` blocks the Anthropic-shaped turns hold: those standing
    in a turn, and those a tool returned inside its `tool_result`.

    One count for every reader that decides on it — `query_messages` and the
    predicate it asks, the host's gate in front of that call, the guest's check
    of a peer's menu row, and the log lines that say what a call carried — so
    that none of them can see a picture another missed. Anything that is not a
    list of dicts counts nothing."""
    count = 0
    for turn in messages if isinstance(messages, list) else []:
        content = turn.get("content") if isinstance(turn, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            inner = block.get("content") if block.get("type") == "tool_result" else None
            count += sum(
                1 for b in [block, *(inner if isinstance(inner, list) else [])]
                if isinstance(b, dict) and b.get("type") == "image"
            )
    return count


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
    # Whether this vendor's own output counter already holds the reasoning
    # tokens, in the ledger's three words (`node_ledger.OUTPUT_INCLUDES_THINKING`).
    # It rides every usage dict recorded here: the tariff bills `includes` on
    # `completion_tokens`, `excludes` on `completion_tokens + thinking_tokens`,
    # and `unknown` on nothing. A subclass overrides it from the vendor's own
    # documentation, quoting the URL and the sentence in its docstring; where
    # the documentation does not settle it the answer stays `unknown`.
    DECLARED_OUTPUT_INCLUDES_THINKING: str = "unknown"
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
            # Every exit passes here, including the one the loop cannot catch:
            # a task cancelled from outside raises `BaseException` straight
            # through its `except Exception`. The call is idempotent, so a loop
            # that already closed its own notice is not closed twice.
            announce_retry_finished(retry_id, self.alias, "abandoned")

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

    def reasoning_words_served(self) -> Optional[List[str]]:
        """The effort words a caller may ask this alias for, `off` included.

        `[]` — this base's answer — is «no effort word reaches this engine»:
        the class builds no request an effort could ride on, so both doors
        refuse every word for it and its menu row promises none. A subclass
        that does send one overrides: `None` where the shared scale
        (`REASONING_OFF` + `REASONING_EFFORTS`) is the ladder, a list where only
        part of it crosses.

        Declared per class rather than derived, the way
        `DECLARED_OUTPUT_INCLUDES_THINKING` is: what leaves this process is a
        fact about the code that builds the body, and a class that forgets to
        say serves no effort rather than a guessed one.
        """
        return []

    def reasoning_default_served(self) -> Optional[str]:
        """The effort word this alias runs at when the caller names none.

        None here, because a class that sends no effort has no default to
        report — and None is not `off`, which is a rung somebody turned to. A
        subclass that reads `reasoning_effort` off its alias config answers
        `configured_reasoning_default(self)`; one that reads another key answers
        from that key.
        """
        return None

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

        The copy carries `DECLARED_OUTPUT_INCLUDES_THINKING` where the dict did
        not name a convention itself, so no path can record counts whose meaning
        the reader has to guess. A dict that names one keeps it: a provider that
        reads the convention off the response knows more than its class does.
        """
        if not usage:
            self._last_usage = None
            return
        stored = dict(usage)
        stored.setdefault("output_includes_thinking", self.DECLARED_OUTPUT_INCLUDES_THINKING)
        self._last_usage = stored

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

    def effective_settings(self) -> Dict[str, Any]:
        """The dials a call on this alias will actually run at, fail-closed.

        What a menu row carries under `settings` (DPTP §3.5): `temperature`,
        `top_p`, `top_k`, `max_output_tokens` — the ceiling on one answer,
        whatever the provider's own field for it is called — and `variant`, the
        build behind the model name where the host can read one.

        A key the provider cannot vouch for is absent, and absent reads as «not
        stated», never «none applies». The base therefore reports only the
        temperature its own scaffolding sends, and only when the configuration
        states one: `max_tokens` is read by three of the classes here and
        ignored by the rest, so a base reporting it would promise a ceiling
        that aliases of the other types never send. A provider that always
        sends a value of its own overrides this; one that sends no sampling at
        all overrides it with nothing.
        """
        value = numeric_setting((self.config or {}).get("temperature"))
        return {} if value is None else {"temperature": value}

    def get_state(self) -> dict:
        return {"alias": self.alias, "model": self.model, "type": self.config.get("type")}
