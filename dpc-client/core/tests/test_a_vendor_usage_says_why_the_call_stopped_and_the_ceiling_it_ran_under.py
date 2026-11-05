"""A vendor's usage carries why the call stopped and the ceiling its request carried.

VENDOR-ROWS-CARRY-NO-FINISH-REASON-AND-MAX-TOKENS (Ark's scan of 2026-10-19: 4441 DeepSeek rows,
no finish_reason, no max_tokens). The agent loop reads the pair to tell a reply cut at the output
limit from a finished one (`_output_limit_of`), and a usage row keeps it. The doubles are the
OpenAI SDK's own models (and the gigachat SDK's), so the shape is the one the providers read: a
stream's last chunk may carry the word and the usage together, apart, or the usage first.
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_chunk import ChoiceDelta

from dpc_client_core.llm_manager import reported_counts, reported_engine
from dpc_client_core.providers.base import with_stop
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.gigachat_provider import GigaChatProvider
from dpc_client_core.providers.neuraldeep_provider import NeuralDeepProvider
from dpc_client_core.providers.openai_provider import OpenAICompatibleProvider
from dpc_client_core.providers.zai_provider import ZaiProvider
from tests.test_a_reply_cut_at_the_output_limit_goes_on_and_says_so import _adapter, _through_the_loop

CEILING = 3000
TOOLS = [{"name": "t", "description": "", "input_schema": {"type": "object"}}]
TURN = [{"role": "user", "content": "write it"}]
IMAGE = [{"base64": "AAA", "mime_type": "image/png"}]


def _usage(completion=7):
    return CompletionUsage(prompt_tokens=100, completion_tokens=completion, total_tokens=100 + completion)


def _completion(text="the sen", finish="length", usage=None):
    """A reply as the SDK parses it. `model_construct` for a word the SDK's literal does not list:
    the SDK does not reject a vendor's own word on a response."""
    message = ChatCompletionMessage(role="assistant", content=text)
    choice = (Choice(finish_reason=finish, index=0, message=message) if finish in
              ("stop", "length", "tool_calls", "content_filter", "function_call")
              else Choice.model_construct(finish_reason=finish, index=0, message=message))
    return ChatCompletion(id="x", choices=[choice], created=0, model="m", object="chat.completion",
                          usage=usage or _usage())


def _chunk(text=None, finish=None, usage=None, choices=True):
    """One stream chunk. The usage-only chunk of `stream_options.include_usage` has `choices=[]`."""
    body = [ChunkChoice(delta=ChoiceDelta(content=text), finish_reason=finish, index=0)] if choices else []
    return ChatCompletionChunk(id="x", choices=body, created=0, model="m",
                               object="chat.completion.chunk", usage=usage)


class _Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self.chunks:
                yield chunk
        return gen()


class _Completions:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []

    async def create(self, **params):
        self.bodies.append(params)
        return self.responses.pop(0)


def _provider(make, monkeypatch, *responses):
    config = {"api_key": "k", "max_tokens": CEILING}
    if make == "deepseek":
        provider = DeepSeekProvider("ds", {**config, "model": "deepseek-v4-flash", "thinking": {"enabled": False}})
    elif make == "zai":
        provider = ZaiProvider("z", {**config, "model": "glm-5.2"})
    else:
        provider = NeuralDeepProvider("nd", {**config, "model": "qwen3.8-27b"})
        monkeypatch.setattr(provider, "_price_usage", AsyncMock())
    provider.completions = _Completions(*responses)
    provider.client = NS(chat=NS(completions=provider.completions))
    return provider


VENDORS = pytest.mark.parametrize("make", ["deepseek", "zai", "neuraldeep"])


# --- every path a vendor has ----------------------------------------------------------------------------------

@VENDORS
@pytest.mark.asyncio
async def test_the_plain_path_carries_the_word_and_the_ceiling_the_request_carried(make, monkeypatch):
    provider = _provider(make, monkeypatch, _completion())
    await provider.generate_response("write it")
    usage = provider.get_last_usage()
    assert (usage["finish_reason"], usage["max_tokens"]) == ("length", CEILING)
    assert provider.completions.bodies[0]["max_tokens"] == usage["max_tokens"]


@VENDORS
@pytest.mark.asyncio
async def test_the_tools_path_carries_them_on_the_usage_it_hands_back_and_the_one_it_keeps(make, monkeypatch):
    provider = _provider(make, monkeypatch, _completion())
    result = await provider.generate_with_tools(TURN, TOOLS)
    for usage in (result["usage"], provider.get_last_usage()):
        assert (usage["finish_reason"], usage["max_tokens"]) == ("length", CEILING)


@VENDORS
@pytest.mark.asyncio
async def test_the_vision_path_carries_the_ceiling_the_call_named_over_the_alias(make, monkeypatch):
    provider = _provider(make, monkeypatch, _completion())
    await provider.generate_with_vision("what is this", IMAGE, max_tokens=1234)
    usage = provider.get_last_usage()
    assert (usage["finish_reason"], usage["max_tokens"]) == ("length", 1234)
    assert provider.completions.bodies[0]["max_tokens"] == 1234


@VENDORS
@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["together", "word_first", "usage_first"])
async def test_a_stream_takes_the_word_and_the_usage_in_either_order(make, order, monkeypatch):
    """Strata sends the word and the usage on one chunk, llama-server and the OpenAI API apart,
    the word first; a gateway can send the usage first. The row says the same in all three."""
    chunks = {
        "together": [_chunk("the sen"), _chunk(finish="length", usage=_usage())],
        "word_first": [_chunk("the sen"), _chunk(finish="length"), _chunk(usage=_usage(), choices=False)],
        "usage_first": [_chunk("the sen"), _chunk(usage=_usage(), choices=False), _chunk(finish="length")],
    }[order]
    provider = _provider(make, monkeypatch, _Stream(chunks))
    seen = []

    async def on_chunk(text, conversation_id):
        seen.append(text)

    assert await provider.generate_response_stream("write it", on_chunk) == "the sen"
    usage = provider.get_last_usage()
    assert (usage["finish_reason"], usage["max_tokens"]) == ("length", CEILING)
    assert provider.completions.bodies[0]["max_tokens"] == CEILING


@VENDORS
@pytest.mark.asyncio
async def test_a_stream_that_ended_with_no_usage_records_nothing_and_does_not_borrow_an_earlier_call(make, monkeypatch):
    provider = _provider(make, monkeypatch, _completion(), _Stream([_chunk("hi"), _chunk(finish="stop")]))
    await provider.generate_response("first")
    assert provider.get_last_usage()["finish_reason"] == "length"
    await provider.generate_response_stream("second", AsyncMock())
    assert provider.get_last_usage() is None


# --- the vendor's words -------------------------------------------------------------------------------------

@VENDORS
@pytest.mark.asyncio
@pytest.mark.parametrize("word, kept", [("stop", "stop"), ("length", "length"), ("tool_calls", "tool_calls"),
                                         ("sensitive", None), ("content_filter", None)])
async def test_the_openai_words_are_kept_and_a_word_with_no_equal_is_left_out(make, word, kept, monkeypatch):
    provider = _provider(make, monkeypatch, _completion(finish=word))
    await provider.generate_response("write it")
    usage = provider.get_last_usage()
    assert usage.get("finish_reason") == kept
    assert usage["max_tokens"] == CEILING  # the ceiling does not depend on the word


@pytest.mark.parametrize("vendor_word, kept", [("max_tokens", "length"), (" LENGTH ", "length"),
                                                ("function_call", "tool_calls"), ("MAX_TOKENS", "length")])
def test_a_vendors_own_word_for_the_limit_is_read_as_length(vendor_word, kept):
    assert with_stop({}, vendor_word)["finish_reason"] == kept


@pytest.mark.parametrize("ceiling", [None, 0, -1, "", False])
def test_a_request_with_no_ceiling_puts_none_in_the_usage(ceiling):
    assert "max_tokens" not in with_stop({}, "stop", ceiling)


@VENDORS
@pytest.mark.asyncio
async def test_what_the_door_and_the_ledger_read_is_the_same_pair(make, monkeypatch):
    provider = _provider(make, monkeypatch, _completion())
    await provider.generate_response("write it")
    engine = reported_engine(provider)
    assert (engine["finish_reason"], engine["max_tokens"]) == ("length", CEILING)


# --- a provider that reads no counts ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_openai_compatible_alias_reports_the_word_alone_and_no_counts(monkeypatch):
    provider = OpenAICompatibleProvider("oa", {"model": "gpt-x", "api_key": "k"})
    provider.client = NS(chat=NS(completions=_Completions(_completion(), _completion(finish="stop"))))
    await provider.generate_response("write it")
    assert provider.get_last_usage() == {"finish_reason": "length"}
    assert reported_counts(provider) is None  # the recount stays in front of it
    await provider.generate_response("again")
    assert provider.get_last_usage() == {"finish_reason": "stop"}


@pytest.mark.asyncio
async def test_an_openai_compatible_call_that_failed_leaves_no_word_from_the_one_before(monkeypatch):
    provider = OpenAICompatibleProvider("oa", {"model": "gpt-x", "api_key": "k"})
    provider.client = NS(chat=NS(completions=_Completions(_completion())))
    await provider.generate_response("first")
    with pytest.raises(RuntimeError):
        await provider.generate_response("second")  # the double has no reply left
    assert provider.get_last_usage() is None


class _Giga:
    """The gigachat SDK's client as the provider uses it: an async context manager with `achat` and `astream`."""

    def __init__(self, reply=None, chunks=()):
        self.reply, self.chunks = reply, chunks

    def __call__(self, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def achat(self, prompt):
        return self.reply

    async def astream(self, prompt):
        for chunk in self.chunks:
            yield chunk


def _giga(reply=None, chunks=()):
    provider = GigaChatProvider("giga", {"api_key": "k", "model": "GigaChat-2-Pro"})
    provider._GigaChat = _Giga(reply, chunks)
    return provider


def _giga_reply(text="the sen", finish="length"):
    from gigachat.models import ChatCompletion, Choices, Messages, Usage
    return ChatCompletion(
        choices=[Choices(message=Messages(role="assistant", content=text), index=0, finish_reason=finish)],
        created=0, model="GigaChat-2-Pro", object="chat.completion",
        usage=Usage(prompt_tokens=100, completion_tokens=7, total_tokens=107))


def _giga_chunk(text=None, finish=None):
    from gigachat.models import ChatCompletionChunk, ChoicesChunk, MessagesChunk
    return ChatCompletionChunk(
        choices=[ChoicesChunk(delta=MessagesChunk(content=text), index=0, finish_reason=finish)],
        created=0, model="GigaChat-2-Pro", object="chat.completion.chunk")


@pytest.mark.asyncio
@pytest.mark.parametrize("word, kept", [("length", "length"), ("stop", "stop"), ("blacklist", None), ("error", None)])
async def test_gigachat_plain_reports_the_word_alone(word, kept):
    provider = _giga(reply=_giga_reply(finish=word))
    assert await provider.generate_response("write it") == "the sen"
    assert provider.get_last_usage() == ({"finish_reason": kept} if kept else None)


@pytest.mark.asyncio
async def test_gigachat_stream_keeps_the_word_past_a_chunk_that_names_none():
    provider = _giga(chunks=[_giga_chunk("the "), _giga_chunk("sen", finish="length"), _giga_chunk()])
    assert await provider.generate_response_stream("write it", AsyncMock()) == "the sen"
    assert provider.get_last_usage() == {"finish_reason": "length"}
    assert reported_counts(provider) is None


# --- through the real adapter and loop --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_gigachat_stream_cut_at_the_limit_goes_on_through_the_real_loop(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    provider = _giga()
    replies = [[_giga_chunk("the sen"), _giga_chunk(finish="length")], [_giga_chunk("tence"), _giga_chunk(finish="stop")]]

    class _Two(_Giga):
        async def astream(self, prompt):
            for chunk in replies.pop(0):
                yield chunk

    provider._GigaChat = _Two()
    llm = _adapter(provider, tmp_path)
    text, _, _ = await _through_the_loop(llm, tmp_path, on_stream_chunk=AsyncMock())
    assert text.startswith("the sentence\n\n⚠️ Continued")
    rows = list(llm._ledger.rows())
    assert [r["finish_reason"] for r in rows] == ["length", "stop"]
    assert all(r["counts_source"] == "ours" for r in rows)
