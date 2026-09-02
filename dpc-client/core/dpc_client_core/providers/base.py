# dpc_client_core/providers/base.py
# Base class, shared exceptions, shared constants, and shared utilities for all AI providers.

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
    def __init__(self, alias: str, config: Dict[str, Any]):
        self.alias = alias
        self.config = config
        self.model = config.get("model")
        self.temperature = config.get("temperature", 0.7)  # Default temperature for creativity

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
