"""Each provider says whether its output count already holds the thinking.

`output_includes_thinking` decides what a served call may be billed: `includes`
bills `completion_tokens`, `excludes` bills `completion_tokens +
thinking_tokens`, and `unknown` bills nothing at all. Until now two adapters set
it — DeepSeek from a ratio of its own two numbers, llama.cpp from the way it
parses its own stream — and every other provider left the field empty, so a row
served by Claude or Gemini was analytics only while both vendors document the
answer on their own pages.

These tests pin the declaration to the vendor's word, not to ours:

* Anthropic `includes` — thinking is a share of `output_tokens`.
* Gemini `excludes` — thoughts are the third addend of `totalTokenCount`.
* OpenAI `includes`, but only for an alias pointed at `api.openai.com`; the same
  class fronts any Chat Completions server and answers `unknown` for the rest.
* Z.AI, Ollama, GigaChat, GitHub Models `unknown` — read and not found, which is
  a result and not an omission.

No network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.llm_manager import reported_counts
from dpc_client_core.node_ledger import OUTPUT_INCLUDES_THINKING
from dpc_client_core.providers.anthropic_provider import AnthropicProvider
from dpc_client_core.providers.base import AIProvider
from dpc_client_core.providers.gemini_provider import GeminiProvider
from dpc_client_core.providers.github_models_provider import GitHubModelsProvider
from dpc_client_core.providers.gigachat_provider import GigaChatProvider
from dpc_client_core.providers.ollama_provider import OllamaProvider
from dpc_client_core.providers.openai_provider import OpenAICompatibleProvider
from dpc_client_core.providers.zai_provider import ZaiProvider


# --- fixtures shaped like each vendor's usage object ---------------------------


def _anthropic_usage(thinking=None, **counts):
    """The Messages API usage object: an input count split three ways and an
    output count whose thinking share, when reported, sits in a details block."""
    fields = dict(
        input_tokens=120,
        cache_read_input_tokens=1000,
        cache_creation_input_tokens=40,
        output_tokens=500,
    )
    fields.update(counts)
    if thinking is not None:
        fields["output_tokens_details"] = SimpleNamespace(thinking_tokens=thinking)
    return SimpleNamespace(**fields)


def _anthropic_message(usage, text="hi"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=usage,
    )


def _gemini_response(candidates=500, thoughts=300, prompt=120, cached=0, text="hi"):
    """`usageMetadata` as the google-genai SDK spells it, snake_case."""
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            candidates_token_count=candidates,
            thoughts_token_count=thoughts,
            cached_content_token_count=cached,
            total_token_count=prompt + candidates + thoughts,
        ),
    )


def _openai_usage(completion=500, reasoning=300, prompt=120):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


def _ollama_response(prompt=120, completion=500):
    return SimpleNamespace(prompt_eval_count=prompt, eval_count=completion)


# --- builders ------------------------------------------------------------------


def _mock_messages(provider, message):
    """Stand in for the whole SDK client: every path here calls one method."""
    provider.client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=message))
    )


def _mock_models(provider, **methods):
    provider.client = SimpleNamespace(models=SimpleNamespace(**methods))


def _anthropic(monkeypatch):
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "test-key")
    return AnthropicProvider(
        "claude_test",
        {"model": "claude-sonnet-4-6", "api_key_env": "TEST_ANTHROPIC_KEY"},
    )


def _gemini():
    return GeminiProvider("gemini_test", {"model": "gemini-2.5-pro", "api_key": "test-key"})


def _openai(base_url=None):
    config = {"model": "o3", "api_key": "test-key"}
    if base_url:
        config["base_url"] = base_url
    return OpenAICompatibleProvider("openai_test", config)


def _zai():
    return ZaiProvider("zai_test", {"model": "glm-5.2", "api_key": "test-key"})


def _ollama():
    return OllamaProvider("ollama_test", {"model": "qwen3:8b", "host": "http://127.0.0.1:11434"})


# --- Anthropic: includes -------------------------------------------------------


def test_anthropic_declares_that_its_output_count_holds_the_thinking(monkeypatch):
    usage = _anthropic(monkeypatch)._usage_from(_anthropic_message(_anthropic_usage(thinking=300)))
    assert usage["output_includes_thinking"] == "includes"
    assert usage["completion_tokens"] == 500
    assert usage["reasoning_tokens"] == 300
    # The answer's own tokens are what is left when the share comes out.
    assert usage["content_tokens"] == 200
    assert usage["thinking_source"] == "engine"


def test_anthropic_prompt_tokens_are_the_sum_the_vendor_names(monkeypatch):
    """`input_tokens` is only what followed the last cache breakpoint."""
    usage = _anthropic(monkeypatch)._usage_from(_anthropic_message(_anthropic_usage(thinking=0)))
    assert usage["prompt_tokens"] == 120 + 1000 + 40
    assert usage["cache_read_input_tokens"] == 1000
    assert usage["cache_creation_input_tokens"] == 40
    assert usage["total_tokens"] == 1160 + 500


def test_anthropic_without_a_details_block_still_declares_the_convention(monkeypatch):
    """A response with no split is not a response with no convention: the
    thinking is inside `output_tokens` whether or not the share is reported."""
    usage = _anthropic(monkeypatch)._usage_from(_anthropic_message(_anthropic_usage()))
    assert usage["output_includes_thinking"] == "includes"
    assert usage["reasoning_tokens"] == 0
    assert "thinking_source" not in usage


@pytest.mark.asyncio
async def test_anthropic_records_the_convention_on_the_plain_path(monkeypatch):
    provider = _anthropic(monkeypatch)
    _mock_messages(provider, _anthropic_message(_anthropic_usage(thinking=300), text="answer"))
    assert await provider.generate_response("q") == "answer"
    assert provider.get_last_usage()["output_includes_thinking"] == "includes"
    # ... and the door reads it back unchanged.
    assert reported_counts(provider) == (1160, 500, "includes")


@pytest.mark.asyncio
async def test_anthropic_records_the_convention_on_the_vision_path(monkeypatch):
    provider = _anthropic(monkeypatch)
    _mock_messages(provider, _anthropic_message(_anthropic_usage(thinking=7), text="a cat"))
    answer = await provider.generate_with_vision(
        "what is this", [{"base64": "aGk=", "mime_type": "image/png"}]
    )
    assert answer == "a cat"
    assert provider.get_last_usage()["output_includes_thinking"] == "includes"


@pytest.mark.asyncio
async def test_a_failed_anthropic_call_hands_out_no_previous_counts(monkeypatch):
    """The counts are cleared before the call, so `get_last_usage()` cannot
    answer for a call that never returned with the one before it."""
    provider = _anthropic(monkeypatch)
    _mock_messages(provider, _anthropic_message(_anthropic_usage(thinking=300)))
    await provider.generate_response("q")
    provider.client.messages.create = AsyncMock(side_effect=RuntimeError("upstream is down"))
    with pytest.raises(RuntimeError):
        await provider.generate_response("q")
    assert provider.get_last_usage() is None


# --- Gemini: excludes ----------------------------------------------------------


def test_gemini_declares_that_its_output_count_excludes_the_thinking():
    usage = _gemini()._usage_from(_gemini_response())
    assert usage["output_includes_thinking"] == "excludes"
    # The candidates count alone, with the thoughts beside it rather than inside.
    assert usage["completion_tokens"] == 500
    assert usage["content_tokens"] == 500
    assert usage["reasoning_tokens"] == 300
    assert usage["thinking_source"] == "engine"
    assert usage["total_tokens"] == 920


@pytest.mark.asyncio
async def test_gemini_records_the_convention_on_the_plain_path(monkeypatch):
    provider = _gemini()
    _mock_models(provider, generate_content=lambda **kw: _gemini_response(text="answer"))
    assert await provider.generate_response("q") == "answer"
    assert reported_counts(provider) == (120, 500, "excludes")


@pytest.mark.asyncio
async def test_gemini_stream_records_the_totals_of_its_last_chunk():
    provider = _gemini()
    chunks = [
        _gemini_response(candidates=100, thoughts=300, text="par"),
        _gemini_response(candidates=500, thoughts=300, text="tial"),
    ]
    _mock_models(provider, generate_content_stream=lambda **kw: iter(chunks))
    assert await provider.generate_response_stream("q", None) == "partial"
    usage = provider.get_last_usage()
    assert usage["completion_tokens"] == 500
    assert usage["output_includes_thinking"] == "excludes"


@pytest.mark.asyncio
async def test_gemini_records_the_convention_on_the_vision_path():
    provider = _gemini()
    _mock_models(provider, generate_content=lambda **kw: _gemini_response(text="a cat"))
    answer = await provider.generate_with_vision(
        "what is this", [{"base64": "aGk=", "mime_type": "image/png"}]
    )
    assert answer == "a cat"
    assert provider.get_last_usage()["output_includes_thinking"] == "excludes"


# --- OpenAI: the alias decides, because the class fronts every vendor ----------


def test_an_alias_pointed_at_openai_itself_declares_includes():
    assert _openai().DECLARED_OUTPUT_INCLUDES_THINKING == "includes"
    assert _openai("https://api.openai.com/v1").DECLARED_OUTPUT_INCLUDES_THINKING == "includes"


def test_an_alias_pointed_at_another_server_answers_unknown():
    """LM Studio, vLLM and every other OpenAI-shaped server document their own
    counters, and OpenAI's page does not speak for them."""
    for base_url in ("http://localhost:1234/v1", "https://openrouter.ai/api/v1"):
        assert _openai(base_url).DECLARED_OUTPUT_INCLUDES_THINKING == "unknown"


# --- read and not found --------------------------------------------------------


def test_zai_leaves_the_convention_unknown_on_every_path():
    provider = _zai()
    assert provider.DECLARED_OUTPUT_INCLUDES_THINKING == "unknown"
    usage = provider._usage_from(SimpleNamespace(usage=_openai_usage()))
    assert usage["output_includes_thinking"] == "unknown"
    assert usage["completion_tokens"] == 500


def test_ollama_leaves_the_convention_unknown_on_every_path():
    provider = _ollama()
    assert provider.DECLARED_OUTPUT_INCLUDES_THINKING == "unknown"
    usage = provider._usage_from(_ollama_response())
    assert usage == {
        "prompt_tokens": 120,
        "completion_tokens": 500,
        "total_tokens": 620,
        "output_includes_thinking": "unknown",
        "served_effort": None,
    }
    provider._log_usage(_ollama_response(), "plain")
    assert reported_counts(provider) == (120, 500, "unknown")


def test_gigachat_and_github_models_leave_the_convention_unknown():
    assert GigaChatProvider.DECLARED_OUTPUT_INCLUDES_THINKING == "unknown"
    assert GitHubModelsProvider.DECLARED_OUTPUT_INCLUDES_THINKING == "unknown"


def test_a_provider_that_says_nothing_says_unknown():
    """The default is the fail-closed one: a new adapter is not billed until
    somebody reads its vendor's page."""
    assert AIProvider.DECLARED_OUTPUT_INCLUDES_THINKING == "unknown"


# --- the words themselves ------------------------------------------------------


def test_every_declared_word_is_one_the_ledger_accepts(monkeypatch):
    """A provider may not coin a fourth word: the ledger validates what it is
    handed and would turn anything else back into `unknown` silently."""
    declared = [
        AIProvider.DECLARED_OUTPUT_INCLUDES_THINKING,
        _anthropic(monkeypatch).DECLARED_OUTPUT_INCLUDES_THINKING,
        _gemini().DECLARED_OUTPUT_INCLUDES_THINKING,
        _openai().DECLARED_OUTPUT_INCLUDES_THINKING,
        _zai().DECLARED_OUTPUT_INCLUDES_THINKING,
        _ollama().DECLARED_OUTPUT_INCLUDES_THINKING,
        GigaChatProvider.DECLARED_OUTPUT_INCLUDES_THINKING,
        GitHubModelsProvider.DECLARED_OUTPUT_INCLUDES_THINKING,
    ]
    assert all(word in OUTPUT_INCLUDES_THINKING for word in declared)


def test_a_dict_that_names_its_own_convention_keeps_it():
    """The class constant is a default and not an override: llama.cpp and
    DeepSeek read the convention off the response and must win."""
    provider = _ollama()
    provider._record_last_usage(
        {"prompt_tokens": 1, "completion_tokens": 2, "output_includes_thinking": "includes"}
    )
    assert provider.get_last_usage()["output_includes_thinking"] == "includes"


def test_no_usage_is_still_no_usage():
    provider = _ollama()
    provider._record_last_usage(None)
    assert provider.get_last_usage() is None
    assert reported_counts(provider) is None
