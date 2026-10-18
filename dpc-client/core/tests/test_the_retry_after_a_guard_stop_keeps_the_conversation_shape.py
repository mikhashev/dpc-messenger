"""The post-guard retry asks for an answer without tool calls and keeps the
conversation it sent a moment ago: same system prompt, same structured
messages, same tools, with `tool_choice="none"`.

Until 2026-10-18 the retry sent `tools=None`, which the adapter routes to its
text path: the whole conversation flattened into one user message with
[SYSTEM]/[USER] markers. On llama-server and Strata that prompt shares no
prefix with the cached one (live: 101,547 tokens re-read, reuse 0%). A provider
whose `generate_with_tools` does not name `tool_choice` keeps the old route,
because sending it the tools would let the model call one again.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.guards import ResearchLimitGuard
from dpc_client_core.dpc_agent.hooks import HookRegistry
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.loop import _finalize_after_guard_stop
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.providers import LlamaServerProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

TOOLS = [{"type": "function", "function": {"name": "probe", "description": "", "parameters": {}}}]
HISTORY = [
    {"role": "system", "content": "I am the agent."},
    {"role": "user", "content": "find it"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "t1", "content": "result"},
    {"role": "user", "content": "[RESEARCH_LIMIT] give your answer now"},
]


class _Recorder:
    alias = "local"
    model = "m"

    def __init__(self):
        self.tool_calls = []
        self.prompts = []

    async def generate_response(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return "flat answer"

    def get_last_usage(self):
        return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


class _TakesToolChoice(_Recorder):
    async def generate_with_tools(self, messages, tools, system="", on_chunk=None,
                                  conversation_id=None, reasoning_effort=None, tool_choice="auto"):
        self.tool_calls.append({"messages": messages, "tools": tools, "system": system,
                                "tool_choice": tool_choice})
        return {"content": "structured answer", "tool_calls_raw": [], "thinking": None,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


class _SwallowsKwargs(_Recorder):
    """Ollama's shape: `**kwargs`, and a chat API with no tool_choice."""

    async def generate_with_tools(self, messages, tools, system="", **kwargs):
        self.tool_calls.append(kwargs)
        return {"content": "tool answer", "tool_calls_raw": [], "thinking": None}


def _adapter(provider, tmp_path):
    manager = SimpleNamespace(token_count_manager=None, providers={"local": provider},
                              agent_provider=None, default_provider="local")
    return DpcLlmAdapter(manager, provider_alias="local", ledger=NodeLedger(tmp_path / "ledger"))


def test_a_provider_that_takes_the_word_gets_the_structured_conversation(tmp_path):
    provider = _TakesToolChoice()
    msg, _ = asyncio.run(_adapter(provider, tmp_path).chat(HISTORY, tools=TOOLS, tool_choice="none"))
    assert msg["content"] == "structured answer"
    assert provider.prompts == [], "the conversation was flattened into one prompt"
    (call,) = provider.tool_calls
    assert call["tool_choice"] == "none"
    assert call["system"] == "I am the agent."
    assert [t["name"] for t in call["tools"]] == ["probe"]
    assert [t["role"] for t in call["messages"]] == ["user", "assistant", "user"]


def test_a_provider_that_would_swallow_the_word_gets_no_tools(tmp_path):
    provider = _SwallowsKwargs()
    msg, _ = asyncio.run(_adapter(provider, tmp_path).chat(HISTORY, tools=TOOLS, tool_choice="none"))
    assert msg["content"] == "flat answer" and provider.tool_calls == []
    assert "tool_call" not in provider.prompts[0]  # no tool descriptions injected


def test_without_the_word_the_tools_path_is_unchanged(tmp_path):
    provider = _TakesToolChoice()
    asyncio.run(_adapter(provider, tmp_path).chat(HISTORY, tools=TOOLS))
    assert provider.tool_calls[0]["tool_choice"] == "auto"


# --- llama-server puts it on the wire ---------------------------------------

@pytest.fixture(autouse=True)
def _no_live_supervisors():
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


class _Supervisor:
    port = 8123
    props = {"total_slots": 4}

    async def ensure_running(self):
        return self.props

    def call_slot(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _sent_by_llama_server(**kwargs):
    provider = LlamaServerProvider("local_qwen", {
        "type": "llamacpp_server", "gguf_path": "models/qwen.gguf",
    })
    provider.supervisor = _Supervisor()
    bodies = []

    async def create(**params):
        bodies.append(params)
        msg = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def _ensure():
        return client

    provider._ensure = _ensure
    system, messages = DpcLlmAdapter._convert_messages_to_anthropic(HISTORY)
    tools = DpcLlmAdapter._convert_tools_to_anthropic(TOOLS)
    asyncio.run(provider.generate_with_tools(messages, tools, system=system, **kwargs))
    return bodies[0]


def test_llama_server_sends_tool_choice_none_beside_the_same_tools():
    plain, retry = _sent_by_llama_server(), _sent_by_llama_server(tool_choice="none")
    assert plain["tool_choice"] == "auto" and retry["tool_choice"] == "none"
    assert retry["tools"] == plain["tools"] and retry["messages"] == plain["messages"]


# --- the loop asks for it -----------------------------------------------------

class _Scripted:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools,
                           "tool_choice": kwargs.get("tool_choice")})
        return self.answers.pop(0), {"prompt_tokens": 1, "completion_tokens": 1}


def test_the_retry_sends_the_same_messages_and_tools_with_tool_choice_none():
    hooks = HookRegistry()
    guard = ResearchLimitGuard()
    hooks.register(guard)
    hooks.last_triggered = guard
    llm = _Scripted(
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "function": {"name": "probe"}}]},
        {"role": "assistant", "content": "plain answer"},
    )
    text, _, _ = asyncio.run(_finalize_after_guard_stop(
        hooks, list(HISTORY[:-1]), llm, None, "conv", {}, {}, fallback_reason="stopped",
        task_id="t", tools=TOOLS, reasoning_effort="high",
    ))
    assert text == "plain answer"
    first, retry = llm.calls
    assert retry["tools"] == first["tools"] == TOOLS
    assert (first["tool_choice"], retry["tool_choice"]) == (None, "none")
    assert retry["messages"] == first["messages"]
