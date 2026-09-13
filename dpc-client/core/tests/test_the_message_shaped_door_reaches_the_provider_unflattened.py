"""The message-shaped door reaches the provider with the turns intact.

`LLMManager.query` takes a flat prompt and returns finished text, so a caller
holding a `messages` array has to flatten it, and a provider that streams or
calls tools natively cannot be reached from above (ADR-041 M1).
`query_messages` stands beside it: the conversation in the shape the
providers' own `generate_with_tools` already accepts, optional tools, an
optional chunk callback, and the dict `query(return_metadata=True)` returns
so a usage row keeps its counts.

What is asserted here is what today's code could not do: a provider double
that sees a list rather than a string, a callback called more than once whose
pieces rejoin into the answer, tools arriving at a provider that has them, a
refusal by name at one that does not, a whole answer with `streamed: False`
where there is no stream, and the stop reason the provider reported reaching
the caller in the word the provider used.

The doubles subclass `AIProvider`, so `supports_thinking`, `get_last_usage`
and the rest are the real ones. Cross-platform: pure asyncio, no files but a
providers.json in tmp_path.
"""

import json

import pytest

from dpc_client_core.llm_manager import LLMManager, flatten_messages
from dpc_client_core.providers import AIProvider

ALIAS = "served_local"
MODEL = "qwen3:8b"
SYSTEM = "be brief"
MESSAGES = [
    {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    {"role": "assistant", "content": "hello"},
    {"role": "user", "content": "and now?"},
]
FLAT = "[SYSTEM]\nbe brief\n\n[USER]\nhi\n\n[ASSISTANT]\nhello\n\n[USER]\nand now?"
ANSWER = "the whole answer"
PIECES = ("the ", "whole ", "answer")
TOOLS = [{"name": "read_file", "description": "read one file",
          "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]


class _Plain(AIProvider):
    """A provider with nothing but the prompt door, as most of them are."""

    def __init__(self, answer=ANSWER, alias=ALIAS):
        super().__init__(alias, {"type": "ollama", "model": MODEL})
        self.prompts = []
        self.answer = answer

    async def generate_response(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.answer


class _Streaming(_Plain):
    async def generate_response_stream(self, prompt, on_chunk=None, conversation_id=None):
        self.prompts.append(prompt)
        for piece in PIECES:
            await on_chunk(piece, conversation_id)
        return "".join(PIECES)


class _WithTools(_Plain):
    """A provider carrying the one entry point that takes messages un-flattened."""

    def __init__(self, tool_calls=(), finish_reason=None):
        super().__init__()
        self.seen = []
        self.tool_calls = list(tool_calls)
        self.finish_reason = finish_reason

    async def generate_with_tools(self, messages, tools, system="", on_chunk=None, conversation_id=None):
        self.seen.append({"messages": messages, "tools": tools, "system": system})
        if on_chunk:
            await on_chunk(ANSWER, conversation_id)
        self._record_last_usage({"finish_reason": self.finish_reason} if self.finish_reason else None)
        return {"content": ANSWER, "tool_calls_raw": self.tool_calls, "thinking": None, "usage": {}}


class _Reporting(_Plain):
    """A provider that says what it stopped on, as llama-server does."""

    def __init__(self, finish_reason):
        super().__init__()
        self.finish_reason = finish_reason

    async def generate_response(self, prompt, **kwargs):
        self._record_last_usage({"prompt_tokens": 3, "completion_tokens": 4,
                                 "finish_reason": self.finish_reason})
        return await super().generate_response(prompt, **kwargs)


class _Thinking(_Plain):
    """A thinking provider whose vendor reported the reasoning tokens."""

    REPORTED = 168

    def __init__(self):
        super().__init__(answer="<think>a long chain of reasoning</think>the answer")

    def supports_thinking(self):
        return True

    async def generate_response(self, prompt, **kwargs):
        self._record_last_usage({"reasoning_tokens": self.REPORTED})
        return await super().generate_response(prompt, **kwargs)


class _Call:
    """A returned tool call in the shape the providers hand back."""

    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


def _manager(tmp_path, provider, alias=ALIAS):
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": []}), encoding="utf-8")
    manager = LLMManager(config_path=config)
    manager.providers = {alias: provider}
    manager.default_provider = alias
    return manager


class _Chunks:
    def __init__(self):
        self.pieces = []

    async def __call__(self, text, conversation_id=None):
        self.pieces.append(text)


# --- (1) the turns reach the provider as turns ---------------------------------


@pytest.mark.asyncio
async def test_the_door_hands_the_provider_the_message_list_and_not_a_prompt(tmp_path):
    provider = _WithTools()
    manager = _manager(tmp_path, provider)

    result = await manager.query_messages(MESSAGES, system=SYSTEM, tools=TOOLS, return_metadata=True)

    (seen,) = provider.seen
    assert seen["messages"] == MESSAGES, "the provider was handed something other than the turns"
    assert isinstance(seen["messages"], list) and not isinstance(seen["messages"], str)
    assert seen["messages"][0]["content"] == [{"type": "text", "text": "hi"}], "the blocks were squashed"
    assert seen["system"] == SYSTEM
    assert provider.prompts == [], "a prompt was rendered and sent as well"
    assert result["flattened"] is False


def test_the_rendering_used_where_a_provider_takes_only_a_prompt_is_the_gateways_own(tmp_path):
    assert flatten_messages(MESSAGES, SYSTEM) == FLAT
    assert flatten_messages(MESSAGES) == FLAT.split("\n\n", 1)[1]


# --- (2) a chunk callback on a provider that streams ---------------------------


@pytest.mark.asyncio
async def test_a_chunk_callback_on_a_streaming_provider_is_called_per_piece_and_the_pieces_are_the_answer(tmp_path):
    provider = _Streaming()
    manager = _manager(tmp_path, provider)
    chunks = _Chunks()

    result = await manager.query_messages(MESSAGES, system=SYSTEM, on_chunk=chunks, return_metadata=True)

    assert len(chunks.pieces) > 1, "the answer arrived in one piece, which is not a stream"
    assert chunks.pieces == list(PIECES)
    assert "".join(chunks.pieces) == result["response"] == "".join(PIECES)
    assert result["streamed"] is True
    assert provider.prompts == [FLAT], "the stream takes a prompt, and it is the gateway's own rendering"


# --- (3) the metadata a usage row is built from --------------------------------


@pytest.mark.asyncio
async def test_the_door_returns_every_key_query_returns_and_five_that_name_what_it_did(tmp_path):
    provider = _Plain()
    manager = _manager(tmp_path, provider)

    old = await manager.query(FLAT, return_metadata=True)
    new = await manager.query_messages(MESSAGES, system=SYSTEM, return_metadata=True)

    assert set(old) <= set(new), f"the row would lose {sorted(set(old) - set(new))}"
    assert set(new) - set(old) == {"streamed", "flattened", "tools_used", "tool_calls", "finish_reason"}
    for key in ("response", "provider", "model", "tokens_used", "prompt_tokens",
                "response_tokens", "model_max_tokens", "vision_used", "thinking", "thinking_tokens"):
        assert new[key] == old[key], key


@pytest.mark.asyncio
async def test_the_reasoning_tokens_are_the_vendors_number_on_both_doors(tmp_path):
    manager = _manager(tmp_path, _Thinking())

    old = await manager.query(FLAT, return_metadata=True)
    new = await manager.query_messages(MESSAGES, system=SYSTEM, return_metadata=True)

    assert old["thinking_tokens"] == new["thinking_tokens"] == _Thinking.REPORTED
    assert old["thinking"] == new["thinking"] == "a long chain of reasoning"
    assert new["response"] == "the answer"


# --- (4) tools arrive, or the alias refuses by name ----------------------------


@pytest.mark.asyncio
async def test_the_tools_a_caller_gave_reach_a_provider_that_has_them(tmp_path):
    call = _Call("call_1", "read_file", {"path": "a.txt"})
    provider = _WithTools(tool_calls=[call])
    manager = _manager(tmp_path, provider)

    result = await manager.query_messages(MESSAGES, system=SYSTEM, tools=TOOLS, return_metadata=True)

    (seen,) = provider.seen
    assert seen["tools"] == TOOLS
    assert result["tools_used"] is True
    assert result["tool_calls"] == [
        {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.txt"}}
    ]


@pytest.mark.asyncio
async def test_a_provider_without_a_tool_path_refuses_by_name_instead_of_answering_as_if_none_were_asked(tmp_path):
    manager = _manager(tmp_path, _Plain())

    with pytest.raises(ValueError) as refused:
        await manager.query_messages(MESSAGES, system=SYSTEM, tools=TOOLS)

    assert ALIAS in str(refused.value) and "generate_with_tools" in str(refused.value)


# --- (5) a provider with no stream answers whole, and says so -------------------


@pytest.mark.asyncio
async def test_a_provider_without_a_stream_answers_whole_and_says_it_did_not_stream(tmp_path):
    provider = _Plain()
    manager = _manager(tmp_path, provider)
    chunks = _Chunks()

    result = await manager.query_messages(MESSAGES, system=SYSTEM, on_chunk=chunks, return_metadata=True)

    assert result["streamed"] is False, "a whole answer was passed off as a stream"
    assert result["response"] == ANSWER
    assert chunks.pieces == [ANSWER], "the answer never reached the callback the caller gave"
    assert provider.prompts == [FLAT]


# --- (6) the stop reason the provider reported ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", ["length", "content_filter"])
async def test_the_stop_reason_the_provider_reported_reaches_the_caller_unchanged(tmp_path, reported):
    manager = _manager(tmp_path, _Reporting(reported))

    result = await manager.query_messages(MESSAGES, system=SYSTEM, return_metadata=True)

    assert result["finish_reason"] == reported


@pytest.mark.asyncio
async def test_a_provider_that_reported_no_stop_reason_leaves_it_unset_rather_than_stop(tmp_path):
    manager = _manager(tmp_path, _Plain())

    result = await manager.query_messages(MESSAGES, system=SYSTEM, return_metadata=True)

    assert result["finish_reason"] is None


@pytest.mark.asyncio
async def test_a_tool_round_carries_its_own_stop_reason(tmp_path):
    provider = _WithTools(tool_calls=[_Call("call_1", "read_file", {})], finish_reason="tool_calls")
    manager = _manager(tmp_path, provider)

    result = await manager.query_messages(MESSAGES, system=SYSTEM, tools=TOOLS, return_metadata=True)

    assert result["finish_reason"] == "tool_calls"
