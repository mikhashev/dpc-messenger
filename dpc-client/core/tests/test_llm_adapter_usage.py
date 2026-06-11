"""Tests for provider usage handling in DpcLlmAdapter and the context guard reserve.

Covers the silent-drop scenario: a provider that drops an over-limit request
returns explicit zero usage (prompt=0, completion=0) instead of an error.
That signal must reach run_llm_loop's fail-fast check untouched — the
fallback estimate is only for providers that return no usage at all.
"""

import asyncio
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.agent import CONTEXT_ROUND_RESERVE_TOKENS


class _DropProvider:
    """Simulates Z.AI silently dropping an over-limit request."""

    async def generate_with_tools(self, **kwargs):
        return {
            "content": "",
            "tool_calls_raw": [],
            "thinking": "",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


class _NoUsageProvider:
    """Simulates a provider that returns no usage block at all."""

    async def generate_with_tools(self, **kwargs):
        return {"content": "hello", "tool_calls_raw": [], "thinking": ""}


def _adapter() -> DpcLlmAdapter:
    mgr = SimpleNamespace(token_count_manager=None, providers={})
    return DpcLlmAdapter(mgr, provider_alias="test")


MESSAGES = [{"role": "user", "content": "x" * 4000}]
TOOLS = [{"function": {"name": "t", "description": "", "parameters": {}}}]


def test_explicit_zero_usage_passes_through():
    _msg, usage = asyncio.run(
        _adapter()._chat_native_tools(_DropProvider(), MESSAGES, TOOLS)
    )
    assert usage["prompt_tokens"] == 0
    assert usage["completion_tokens"] == 0


def test_zero_usage_triggers_loop_fail_fast_condition():
    _msg, usage = asyncio.run(
        _adapter()._chat_native_tools(_DropProvider(), MESSAGES, TOOLS)
    )
    fires = (
        "prompt_tokens" in usage
        and usage.get("prompt_tokens") == 0
        and usage.get("completion_tokens") == 0
    )
    assert fires


def test_missing_usage_falls_back_to_estimate():
    _msg, usage = asyncio.run(
        _adapter()._chat_native_tools(_NoUsageProvider(), MESSAGES, TOOLS)
    )
    assert usage["prompt_tokens"] > 0
    assert usage["total_tokens"] > 0


@pytest.mark.parametrize(
    "ctx_window,estimated,blocked",
    [
        (204800, 192529, True),  # observed silent-drop case at 94% of window
        (204800, 188000, False),
        (131072, 115000, True),
        (131072, 114000, False),
        (32768, 26500, True),
        (32768, 26000, False),
        (8192, 6600, True),
        (8192, 6500, False),
    ],
)
def test_context_guard_reserve(ctx_window, estimated, blocked):
    reserve = max(
        int(ctx_window * 0.05),
        min(CONTEXT_ROUND_RESERVE_TOKENS, int(ctx_window * 0.2)),
    )
    assert (ctx_window - estimated < reserve) == blocked
