"""With no usage from the provider, the estimate counts the whole prompt:
system, tool schemas and tool-call arguments, in both the counter branch and
the chars/4 branch; the completion counts native tool arguments and thinking.
A usage the provider did report passes through untouched."""

import asyncio
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter


class _Provider:
    def __init__(self, raw):
        self.raw = raw

    async def generate_with_tools(self, **kwargs):
        return self.raw


class _Counter:
    def count_tokens(self, text, model):
        return len(text) // 4


def _adapter(counter):
    mgr = SimpleNamespace(token_count_manager=counter, providers={})
    adapter = DpcLlmAdapter(mgr, provider_alias="test")
    adapter._token_counter = counter
    return adapter


def _tool(description="d"):
    return [{"function": {"name": "t", "description": description, "parameters": {}}}]


def _usage(counter, *, system="s", tools=None, history=None, raw=None):
    messages = [{"role": "system", "content": system}] + (history or [{"role": "user", "content": "hi"}])
    raw = raw if raw is not None else {"content": "ok", "tool_calls_raw": [], "thinking": ""}
    _msg, usage = asyncio.run(
        _adapter(counter)._chat_native_tools(_Provider(raw), messages, tools or _tool()))
    return usage


@pytest.fixture(params=[None, _Counter()], ids=["chars4", "counter"])
def counter(request):
    return request.param


def test_the_estimate_grows_with_a_large_tool_schema(counter):
    small = _usage(counter)["prompt_tokens"]
    big = _usage(counter, tools=_tool("x" * 4000))["prompt_tokens"]
    assert big >= small + 900


def test_the_estimate_grows_with_a_large_system_prompt(counter):
    small = _usage(counter)["prompt_tokens"]
    big = _usage(counter, system="x" * 4000)["prompt_tokens"]
    assert big >= small + 900


def test_the_estimate_grows_with_large_tool_call_arguments(counter):
    call = {"id": "c1", "type": "function",
            "function": {"name": "write", "arguments": '{"text": "' + "x" * 4000 + '"}'}}
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    small = _usage(counter)["prompt_tokens"]
    big = _usage(counter, history=history)["prompt_tokens"]
    assert big >= small + 900


def test_the_completion_counts_native_tool_arguments_and_thinking(counter):
    block = SimpleNamespace(id="c1", name="write", input={"text": "x" * 4000})
    base = _usage(counter)
    with_call = _usage(counter, raw={"content": "", "tool_calls_raw": [block], "thinking": ""})
    assert with_call["completion_tokens"] >= base["completion_tokens"] + 900
    with_thinking = _usage(counter, raw={"content": "ok", "tool_calls_raw": [], "thinking": "y" * 4000})
    assert with_thinking["completion_tokens"] >= base["completion_tokens"] + 900
    assert with_thinking["output_includes_thinking"] == "includes"
    assert base["output_includes_thinking"] == "excludes"


def test_a_usage_the_provider_reported_is_left_as_it_was(counter):
    reported = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    usage = _usage(counter, tools=_tool("x" * 4000),
                   raw={"content": "ok", "tool_calls_raw": [], "thinking": "", "usage": reported})
    assert usage["prompt_tokens"] == 7 and usage["completion_tokens"] == 3
    assert usage["total_tokens"] == 10
