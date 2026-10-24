"""DeepSeek takes `tool_choice`, so the post-guard retry stays on the native path.

Until 2026-10-25 `DeepSeekProvider.generate_with_tools` hard-coded
`tool_choice: "auto"` and did not name the parameter, so `takes_tool_choice`
said no and the adapter sent the retry down its text path: the conversation
flattened into one prompt with no tools (prompt cache hit 1 792 of 95 830), and
the model wrote its write_file call into the reply as DSML. The vendor
documents `none` (api-docs.deepseek.com/api/create-chat-completion, read
2026-10-25); no live call was made to see it honoured.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter, takes_tool_choice
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider

TOOLS = [{"type": "function", "function": {"name": "write_file", "description": "",
                                           "parameters": {"type": "object"}}}]
HISTORY = [
    {"role": "system", "content": "I am the agent."},
    {"role": "user", "content": "save the write-up"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "t1", "content": "result"},
    {"role": "user", "content": "[RESEARCH_LIMIT] give your answer now"},
]


def _provider():
    p = DeepSeekProvider("deepseek_flash", {"api_key": "k", "model": "deepseek-v4-flash"})
    msg = SimpleNamespace(content="the answer", reasoning_content=None, tool_calls=None)
    p.client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
    ))
    p.flattened = []

    async def _flat(prompt, **kwargs):
        p.flattened.append(prompt)
        return "flat answer"

    p.generate_response = _flat
    p.generate_response_stream = _flat
    return p


def _sent(p):
    _, kwargs = p.client.chat.completions.create.call_args
    return kwargs


def test_deepseek_names_tool_choice_so_the_adapter_may_send_it():
    assert takes_tool_choice(_provider())


def test_deepseek_puts_none_on_the_wire_beside_the_same_tools_and_messages():
    plain, retry = _provider(), _provider()
    system, messages = DpcLlmAdapter._convert_messages_to_anthropic(HISTORY)
    tools = DpcLlmAdapter._convert_tools_to_anthropic(TOOLS)
    asyncio.run(plain.generate_with_tools(messages, tools, system=system))
    asyncio.run(retry.generate_with_tools(messages, tools, system=system, tool_choice="none"))
    assert _sent(plain)["tool_choice"] == "auto" and _sent(retry)["tool_choice"] == "none"
    assert _sent(retry)["tools"] == _sent(plain)["tools"]
    assert _sent(retry)["messages"] == _sent(plain)["messages"]


def test_the_adapter_keeps_a_deepseek_retry_on_the_native_path(tmp_path):
    p = _provider()
    manager = SimpleNamespace(token_count_manager=None, providers={"deepseek_flash": p},
                              agent_provider=None, default_provider="deepseek_flash")
    adapter = DpcLlmAdapter(manager, provider_alias="deepseek_flash",
                            ledger=NodeLedger(tmp_path / "ledger"))
    msg, _ = asyncio.run(adapter.chat(HISTORY, tools=TOOLS, tool_choice="none"))
    assert msg["content"] == "the answer"
    assert p.flattened == [], "the retry was flattened into one prompt"
    sent = _sent(p)
    assert sent["tool_choice"] == "none"
    assert [t["function"]["name"] for t in sent["tools"]] == ["write_file"]
    assert sent["messages"][0] == {"role": "system", "content": "I am the agent."}
