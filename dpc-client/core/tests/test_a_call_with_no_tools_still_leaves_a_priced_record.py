"""Every DeepSeek call must leave one artefact, whether or not it used tools.

Until now `DeepSeek usage:` was written by `generate_with_tools` alone. Sleep
synthesis, the AI chat and anything else with no tools went through
`generate_response`, which returns a string and threw `resp.usage` away — 174
such calls in the same window that produced our burn figure, priced by no
artefact anywhere. So every number this project has quoted is a lower bound
over the tool path.

Worse than missing: on that path the adapter counts tokens itself and prices
the estimate, and the estimate is wrong in **both** directions at once — the
cache split is unknown, so every prompt token is billed as cache-miss (dearer
than the truth), and reasoning tokens are invisible in the response text
(cheaper than the truth). Which one dominates cannot be said without the real
numbers, which is the point.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.providers.base import AIProvider
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter


def _make(config=None):
    cfg = {"api_key": "test-key", "model": "deepseek-v4-flash"}
    if config:
        cfg.update(config)
    return DeepSeekProvider("deepseek_test", cfg)


def _resp(content="ok", **usage_fields):
    fields = {
        "prompt_tokens": 1000,
        "completion_tokens": 900,
        "total_tokens": 1900,
        "prompt_cache_hit_tokens": 320,
        "prompt_cache_miss_tokens": 680,
        "completion_tokens_details": SimpleNamespace(reasoning_tokens=850),
    }
    fields.update(usage_fields)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, reasoning_content="cot", tool_calls=[]))],
        usage=SimpleNamespace(**fields),
    )


# --- the provider keeps what it was told ---


@pytest.mark.asyncio
async def test_a_plain_call_records_the_same_fields_as_a_tool_call():
    p = _make()
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    await p.generate_response("hi")

    u = p.get_last_usage()
    assert u["prompt_tokens"] == 1000
    assert u["prompt_cache_hit_tokens"] == 320
    assert u["prompt_cache_miss_tokens"] == 680
    assert u["reasoning_tokens"] == 850
    assert u["content_tokens"] == 50


@pytest.mark.asyncio
async def test_the_line_says_which_path_it_came_from(caplog):
    """The burn history is parsed out of these lines. Plain calls joining the
    series unannounced would change what the existing 5,406 lines mean without
    anyone being able to see it in the file."""
    p = _make()
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    with caplog.at_level(logging.INFO):
        await p.generate_response("hi")

    said = [r.getMessage() for r in caplog.records if "DeepSeek usage" in r.getMessage()]
    assert len(said) == 1
    assert "path=plain" in said[0]
    assert "tool_calls=0" in said[0]
    assert "hit=320/miss=680" in said[0]


@pytest.mark.asyncio
async def test_the_tool_path_keeps_its_own_name_and_its_own_return(caplog):
    p = _make()
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    with caplog.at_level(logging.INFO):
        result = await p.generate_with_tools(messages=[{"role": "user", "content": "hi"}], tools=[])

    said = [r.getMessage() for r in caplog.records if "DeepSeek usage" in r.getMessage()]
    assert len(said) == 1
    assert "path=tools" in said[0]
    assert result["usage"]["prompt_cache_hit_tokens"] == 320


@pytest.mark.asyncio
async def test_a_new_call_does_not_inherit_the_last_ones_numbers():
    """A failure after a success would otherwise be priced as the success."""
    p = _make()
    p.client.chat.completions.create = AsyncMock(return_value=_resp())
    await p.generate_response("hi")
    assert p.get_last_usage() is not None

    p.client.chat.completions.create = AsyncMock(side_effect=Exception("400 bad request"))
    with pytest.raises(RuntimeError):
        await p.generate_response("hi again")
    assert p.get_last_usage() is None


@pytest.mark.asyncio
async def test_a_streamed_call_asks_for_the_usage_it_needs():
    """A stream reports usage only if the request asks for it, and the chunk
    that carries it has no choices — the loop used to skip exactly that one."""
    p = _make()

    class _Stream:
        def __aiter__(self):
            async def gen():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok", reasoning_content=None))], usage=None)
                yield SimpleNamespace(choices=[], usage=SimpleNamespace(
                    prompt_tokens=1000, completion_tokens=900, total_tokens=1900,
                    prompt_cache_hit_tokens=320, prompt_cache_miss_tokens=680,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=850),
                ))
            return gen()

    p.client.chat.completions.create = AsyncMock(return_value=_Stream())
    chunks = []

    async def _on_chunk(text, conv):
        chunks.append(text)

    text = await p.generate_response_stream("hi", on_chunk=_on_chunk)

    assert text == "ok" and chunks == ["ok"]
    _, kwargs = p.client.chat.completions.create.call_args
    assert kwargs["stream_options"] == {"include_usage": True}
    assert p.get_last_usage()["prompt_cache_hit_tokens"] == 320


# --- the adapter stops guessing when it is told ---


class _PricedProvider:
    """A provider that reports real usage, as DeepSeek does."""

    model = "deepseek-v4-flash"

    def __init__(self):
        self.usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 900,
            "total_tokens": 1900,
            "reasoning_tokens": 850,
            "content_tokens": 50,
            "cache_read_input_tokens": 320,
            "prompt_cache_hit_tokens": 320,
            "prompt_cache_miss_tokens": 680,
        }

    async def generate_response(self, prompt, **kwargs):
        return "short answer"

    def get_last_usage(self):
        return self.usage


class _SilentProvider(AIProvider):
    """A provider with no usage to report for this call.

    It subclasses `AIProvider` because every provider in `PROVIDER_MAP` does,
    and because the accessor now lives on that base: a double shaped like the
    old world — no `get_last_usage` at all — would be testing a class that can
    no longer be registered. What it still models is the case that matters
    here: the method exists and answers `None`, so the adapter has to fall back
    to its own estimate.
    """

    def __init__(self):
        super().__init__("test", {"type": "ollama", "model": "qwen3.8:latest"})

    async def generate_response(self, prompt, **kwargs):
        return "short answer"


def _adapter(provider):
    mgr = SimpleNamespace(
        token_count_manager=None,
        providers={"test": provider},
        agent_provider=None,
        default_provider="test",
    )
    return DpcLlmAdapter(mgr, provider_alias="test")


MESSAGES = [{"role": "user", "content": "x" * 4000}]


def test_the_real_numbers_beat_the_estimate():
    _msg, usage = asyncio.run(_adapter(_PricedProvider()).chat(MESSAGES))
    assert usage["prompt_tokens"] == 1000
    assert usage["completion_tokens"] == 900  # the estimate would see ~3 tokens of text
    assert usage["reasoning_tokens"] == 850


def test_the_cache_split_reaches_the_price():
    """Hit tokens bill at a tenth of miss tokens, so a priced call that ignores
    the split overcharges — silently, and always in the same direction."""
    from dpc_client_core.dpc_agent.pricing import compute_cost_usd

    _msg, usage = asyncio.run(_adapter(_PricedProvider()).chat(MESSAGES))
    priced = compute_cost_usd(
        "test", 1000, 900, model="deepseek-v4-flash",
        cache_hit_tokens=320, cache_miss_tokens=680,
    )
    blind = compute_cost_usd("test", 1000, 900, model="deepseek-v4-flash")
    # Both halves matter: the second alone passes for the wrong reason, because
    # the estimate this replaces is small enough to sit under `blind` too.
    assert usage["cost"] == pytest.approx(priced)
    assert usage["cost"] < blind


def test_a_provider_with_nothing_to_report_still_gets_an_estimate():
    _msg, usage = asyncio.run(_adapter(_SilentProvider()).chat(MESSAGES))
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0


# --- the effort the line reports must be the one the call asked for ---
#
# Found by the live acceptance of 34b3933d (2026-08-16, 20:37 local): the room
# was switched to `off` at 20:37:13 and the four calls that followed logged
# `effort=server-default`. `off` never reaches the wire as an effort — it turns
# the thinking block off — so a label read out of the request body reports the
# loudest choice an operator can make as no choice at all. Ark read those lines
# as "the group is on high", which is what a mislabelled record does next.


@pytest.mark.asyncio
async def test_a_call_that_asked_for_off_says_off(caplog):
    p = _make()
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    with caplog.at_level(logging.INFO):
        await p.generate_with_tools(
            messages=[{"role": "user", "content": "hi"}], tools=[], reasoning_effort="off"
        )

    said = [r.getMessage() for r in caplog.records if "DeepSeek usage" in r.getMessage()][0]
    assert "effort=off" in said


@pytest.mark.asyncio
async def test_an_alias_that_never_thinks_is_not_the_same_as_no_preference(caplog):
    """Two different silences: this alias was configured not to think, and
    nobody expressed a preference. A burn parser needs to tell them apart."""
    p = _make({"thinking": {"enabled": False}})
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    with caplog.at_level(logging.INFO):
        await p.generate_response("hi")

    said = [r.getMessage() for r in caplog.records if "DeepSeek usage" in r.getMessage()][0]
    assert "effort=alias-off" in said


@pytest.mark.asyncio
async def test_a_level_is_still_reported_as_itself(caplog):
    p = _make()
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    with caplog.at_level(logging.INFO):
        await p.generate_with_tools(
            messages=[{"role": "user", "content": "hi"}], tools=[], reasoning_effort="high"
        )

    said = [r.getMessage() for r in caplog.records if "DeepSeek usage" in r.getMessage()][0]
    assert "effort=high" in said


@pytest.mark.asyncio
async def test_nothing_asked_anywhere_is_still_server_default(caplog):
    """No per-call effort, no configured one: the server picks, and the line
    must not invent a word for that."""
    p = _make()
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    with caplog.at_level(logging.INFO):
        await p.generate_response("hi")

    said = [r.getMessage() for r in caplog.records if "DeepSeek usage" in r.getMessage()][0]
    assert "effort=server-default" in said
