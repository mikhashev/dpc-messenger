"""A vendor's usage row keeps the prompt cache split its price was computed from.

DeepSeek reports `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`, and
`_price_usage` prices from them, yet 0 of 4434 deepseek_flash rows in
usage-2026-10.jsonl carried either: a forecast of a vendor route had to back
the hit rate out of the dollar total
(LEDGER-STORES-TOTAL-COST-BUT-NOT-THE-CACHE-SPLIT-THAT-PRODUCED-IT). The split
now lands in the columns a local engine already fills: `cached_tokens` is the
hit, `prefilled_tokens` the prompt read fresh.

Each vendor's usage dict is built by that provider's own builder, so a renamed
key there fails here. Cross-platform: pure asyncio, no network.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.node_ledger import NodeLedger, prompt_cache_split
from dpc_client_core.providers.anthropic_provider import AnthropicProvider
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.gemini_provider import GeminiProvider
from dpc_client_core.providers.neuraldeep_provider import NeuralDeepProvider
from dpc_client_core.providers.zai_provider import ZaiProvider

MESSAGES = [{"role": "user", "content": "read the file"}]
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "read", "parameters": {"type": "object", "properties": {}},
}}]


def _declared(cls):
    """A stand-in `self` for a usage builder: the builders read only the declared convention."""
    return SimpleNamespace(DECLARED_OUTPUT_INCLUDES_THINKING=cls.DECLARED_OUTPUT_INCLUDES_THINKING)


def _openai_usage(prompt, cached, completion=20):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached), completion_tokens_details=None,
    )


def _deepseek():
    return DeepSeekProvider._usage_from_response(SimpleNamespace(
        prompt_tokens=1000, prompt_cache_hit_tokens=900, prompt_cache_miss_tokens=100,
        completion_tokens=20, total_tokens=1020, completion_tokens_details=None,
    ))


def _zai():
    return ZaiProvider._usage_from(_declared(ZaiProvider), SimpleNamespace(usage=_openai_usage(1000, 640)))


def _neuraldeep():
    return NeuralDeepProvider._usage_from_response(_declared(NeuralDeepProvider), _openai_usage(1000, 640), "qwen3.8")


def _gemini():
    return GeminiProvider._usage_from(_declared(GeminiProvider), SimpleNamespace(usage_metadata=SimpleNamespace(
        prompt_token_count=1000, candidates_token_count=20, thoughts_token_count=0,
        cached_content_token_count=640, total_token_count=1020,
    )))


def _anthropic():
    # 50 after the last breakpoint + 800 read from the cache + 150 written into it.
    return AnthropicProvider._usage_from(_declared(AnthropicProvider), SimpleNamespace(usage=SimpleNamespace(
        input_tokens=50, cache_read_input_tokens=800, cache_creation_input_tokens=150,
        output_tokens=20, output_tokens_details=None,
    )))


class _Vendor:
    alias, model = "vendor", "vendor-model"

    def __init__(self, usage):
        self._usage = usage

    async def generate_with_tools(self, messages, tools, **kwargs):
        return {"content": "ok", "tool_calls_raw": [], "thinking": None, "usage": dict(self._usage)}

    def get_last_usage(self):
        return {}


async def _row_for(tmp_path, usage):
    ledger = NodeLedger(tmp_path / "ledger")
    manager = SimpleNamespace(
        token_count_manager=None, providers={"vendor": _Vendor(usage)},
        agent_provider=None, default_provider="vendor",
    )
    await DpcLlmAdapter(manager, provider_alias="vendor", caller="agent_test", ledger=ledger).chat(
        MESSAGES, tools=TOOLS,
    )
    (row,) = list(ledger.rows())
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("build, prefilled, cached", [
    pytest.param(_deepseek, 100, 900, id="deepseek-both-halves"),
    pytest.param(_zai, 360, 640, id="zai-hit-only"),
    pytest.param(_neuraldeep, 360, 640, id="neuraldeep-hit-only"),
    pytest.param(_gemini, 360, 640, id="gemini-hit-only"),
    # Cache creation is read fresh on the call that writes it: prefill, not a hit.
    pytest.param(_anthropic, 200, 800, id="anthropic-creation-is-prefill"),
])
async def test_a_vendor_row_carries_the_split_its_usage_reported(tmp_path, build, prefilled, cached):
    row = await _row_for(tmp_path, build())

    assert (row["prefilled_tokens"], row["cached_tokens"]) == (prefilled, cached)
    assert row["prefilled_tokens"] + row["cached_tokens"] == row["prompt_tokens"]
    # The engine-only columns stay the engine's: a vendor said nothing about them.
    assert row["prompt_ms"] is None and row["predicted_ms"] is None


@pytest.mark.asyncio
async def test_a_vendor_that_reports_no_split_leaves_both_null(tmp_path):
    row = await _row_for(tmp_path, {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020})

    assert (row["prefilled_tokens"], row["cached_tokens"]) == (None, None)


def test_a_local_engines_own_words_win_over_a_vendor_key():
    usage = {"prompt_tokens": 46976, "prefilled_tokens": 1792, "cached_tokens": 45184,
             "cache_read_input_tokens": 0}

    assert prompt_cache_split(usage) == {"prefilled_tokens": 1792, "cached_tokens": 45184}


def test_a_hit_without_its_miss_leaves_the_rest_of_the_prompt_as_prefill():
    usage = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 900}

    assert prompt_cache_split(usage) == {"prefilled_tokens": 100, "cached_tokens": 900}


def test_a_hit_larger_than_its_prompt_is_not_written():
    usage = {"prompt_tokens": 100, "cache_read_input_tokens": 900}

    assert prompt_cache_split(usage) == {"prefilled_tokens": None, "cached_tokens": None}


def test_a_hit_without_a_prompt_count_names_only_the_hit():
    assert prompt_cache_split({"cache_read_input_tokens": 640}) == {"prefilled_tokens": None, "cached_tokens": 640}
