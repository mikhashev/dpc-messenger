"""A guard's stop notice reaches the model as user-side text, and the agent's
own system prompt stays the system prompt of the finalising call.

Until 2026-10-18 the loop appended the notice as a second system message and
the adapter's converter took the last system message for `system`, so the
final call went out without the agent's identity, scratchpad and the rest of
its head — and a local engine re-read the whole prompt (live: 114,454 tokens
fell to 78,534, reuse 0%).
"""

from __future__ import annotations

import asyncio

from dpc_client_core.dpc_agent.guards import ResearchLimitGuard
from dpc_client_core.dpc_agent.hooks import HookRegistry
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.loop import _finalize_after_guard_stop
from dpc_client_core.providers.base import anthropic_to_openai_messages

SYSTEM_BLOCKS = [
    {"type": "text", "text": "I am Johnny. Scratchpad: ...", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "Identity and skills."},
]


def _history():
    return [
        {"role": "system", "content": SYSTEM_BLOCKS},
        {"role": "user", "content": "find it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ]


def _convert(messages):
    return DpcLlmAdapter._convert_messages_to_anthropic(messages)


def test_a_later_system_message_does_not_replace_the_first():
    system, turns = _convert(_history() + [{"role": "system", "content": "[RESEARCH_LIMIT] stop"}])
    assert system == SYSTEM_BLOCKS
    texts = [b.get("text") for b in turns[-1]["content"] if b.get("type") == "text"]
    assert texts == ["[RESEARCH_LIMIT] stop"]


def test_text_after_tool_results_joins_their_turn_and_the_roles_alternate():
    for late in ({"role": "system", "content": "stop"}, {"role": "user", "content": "stop"}):
        history = _history()
        _, turns = _convert(history + [late])
        roles = [t["role"] for t in turns]
        assert all(a != b for a, b in zip(roles, roles[1:])), roles
        kinds = [b["type"] for b in turns[-1]["content"]]
        assert kinds == ["tool_result", "text"]
        # The loop's own message list is not touched by the join.
        assert history[-1] == {"role": "tool", "tool_call_id": "t1", "content": "result"}


def test_two_text_turns_stay_apart():
    """Group history holds consecutive user turns; joining them would change
    how the OpenAI-shape converter renders them, and with it the prefix."""
    _, turns = _convert([{"role": "system", "content": "S"},
                         {"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    assert [t["role"] for t in turns] == ["user", "user"]


def test_the_shared_converter_sends_one_system_message_at_the_head():
    out = anthropic_to_openai_messages("S", [
        {"role": "user", "content": "a"},
        {"role": "system", "content": "note"},
        {"role": "system", "content": [{"type": "text", "text": "block note"}]},
    ])
    assert [m["role"] for m in out] == ["system", "user", "user", "user"]
    assert out[0]["content"] == "S"
    assert [m["content"] for m in out[2:]] == ["note", "block note"]


class _Recording:
    def __init__(self):
        self.sent = []

    async def chat(self, messages, tools=None, **kwargs):
        self.sent.append([dict(m) for m in messages])
        return {"role": "assistant", "content": "final"}, {"prompt_tokens": 1, "completion_tokens": 1}


def test_the_finalising_call_keeps_the_agents_system_prompt():
    hooks = HookRegistry()
    guard = ResearchLimitGuard()
    hooks.register(guard)
    hooks.last_triggered = guard
    llm = _Recording()
    text, _, _ = asyncio.run(_finalize_after_guard_stop(
        hooks, _history(), llm, None, "conv", {}, {}, fallback_reason="stopped", task_id="t",
    ))
    assert text == "final"
    sent = llm.sent[0]
    assert [m["role"] for m in sent].count("system") == 1
    assert sent[-1]["role"] == "user" and sent[-1]["content"].startswith("[RESEARCH_LIMIT]")
    system, _ = _convert(sent)
    assert system == SYSTEM_BLOCKS
