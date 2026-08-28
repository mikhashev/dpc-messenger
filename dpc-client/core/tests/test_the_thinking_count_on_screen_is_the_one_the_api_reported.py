"""The screen showed 190 where the API had said 168, for the same text.

One quantity, three producers: the number the provider was handed by the API,
the provider's own chars/4 estimate when the API reports no split, and a third
one computed here from the thinking text. The interface was shown the third.

`count_tokens` reaches a real tokenizer only for names containing `gpt-4`,
`gpt-3.5` or `claude`, or a colon (the Ollama `model:tag` shape). `deepseek-v4-flash`
has none of those, so it fell to `len(text) // 4` — which overshoots on English
prose and produced the 190. Measured on 2026-08-24: the log line for that call
read `reasoning=168`, the count DeepSeek bills for.

Real fakes rather than mocks: the point is which of two numbers is chosen, so
each double simply holds the numbers.
"""

import pytest

from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.managers.token_count_manager import TokenCountManager


class _Provider:
    """The three things `query` asks of a provider on this path."""

    def __init__(self, thinking: str, usage):
        self.model = "deepseek-v4-flash"   # no tokenizer matches this name
        self.alias = "deepseek_flash"
        # `get_context_window` reads it for a per-alias override; empty means
        # «no override», which is the ordinary case.
        self.config = {}
        self._thinking = thinking
        self._usage = usage

    def supports_thinking(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return False

    def get_last_thinking(self):
        return self._thinking

    def get_last_usage(self):
        return self._usage

    async def generate_response(self, prompt, **kwargs):
        return "answer"


def _manager(provider):
    """An LLMManager without config or disk, carrying the real token counter.

    The counter is the real one on purpose: the fallback branch has to fall
    through the same tokenizer dispatch production uses, or the test would be
    asserting against a stub of my own making rather than against the rule.
    """
    m = LLMManager.__new__(LLMManager)
    m.providers = {"deepseek_flash": provider}
    m.default_provider = "deepseek_flash"
    m.vision_provider = None
    m.token_count_manager = TokenCountManager()
    m.provider_configs = {}
    return m


# Long enough that chars/4 and a real count cannot coincide by accident.
THINKING = "We need to respond to the user's question about the weather. " * 12


@pytest.mark.asyncio
async def test_the_reported_count_wins_over_a_second_opinion():
    provider = _Provider(THINKING, {"reasoning_tokens": 168, "completion_tokens": 186})
    result = await _manager(provider).query("hi", return_metadata=True)

    assert result["thinking_tokens"] == 168
    assert result["thinking_tokens"] != len(THINKING) // 4, "that is the estimate this replaces"


@pytest.mark.asyncio
async def test_without_a_reported_count_it_still_counts_for_itself():
    """A provider whose API says nothing about reasoning must not lose the
    number entirely — the estimate is the fallback, not the default."""
    provider = _Provider(THINKING, {"completion_tokens": 186})
    result = await _manager(provider).query("hi", return_metadata=True)

    assert result["thinking_tokens"] == len(THINKING) // 4


@pytest.mark.asyncio
async def test_a_zero_is_not_a_report():
    """`reasoning_tokens: 0` beside real thinking text means the field was not
    filled, not that the model thought for nothing — falling back is right."""
    provider = _Provider(THINKING, {"reasoning_tokens": 0, "completion_tokens": 186})
    result = await _manager(provider).query("hi", return_metadata=True)

    assert result["thinking_tokens"] == len(THINKING) // 4


@pytest.mark.asyncio
async def test_no_usage_at_all_does_not_raise():
    provider = _Provider(THINKING, None)
    result = await _manager(provider).query("hi", return_metadata=True)

    assert result["thinking_tokens"] == len(THINKING) // 4
