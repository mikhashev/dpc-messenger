"""After a guard stop, the finalising call can be stopped, and at the context
edge it is not made at all.

2026-09-23: ContextLimitGuard stopped Johnny at 102% of the window, the
finalising call re-sent a 213965-token prompt to a local llama-server for
7m42s, and nothing could end it.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent.guards import ContextLimitGuard, ResearchLimitGuard
from dpc_client_core.dpc_agent.hooks import HookRegistry
from dpc_client_core.dpc_agent.loop import _finalize_after_guard_stop


def _registry_stopped_by(guard) -> HookRegistry:
    hooks = HookRegistry()
    hooks.register(guard)
    hooks.last_triggered = guard
    return hooks


class _HangingLlm:
    def __init__(self):
        self.cancelled = False
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {"role": "assistant", "content": "late"}, {}


@pytest.mark.asyncio
async def test_a_stop_during_the_finalising_call_cancels_it_and_returns_the_stop_message():
    llm = _HangingLlm()
    stop = asyncio.Event()
    trace: dict = {}

    async def press_stop():
        await asyncio.sleep(0.05)
        stop.set()

    started = time.monotonic()
    presser = asyncio.create_task(press_stop())
    text, _, trace = await _finalize_after_guard_stop(
        _registry_stopped_by(ResearchLimitGuard()), [], llm, None, "group-1", {}, trace,
        fallback_reason="stopped", task_id="t", stop_event=stop,
    )
    await presser
    await asyncio.sleep(0)

    assert time.monotonic() - started < 5
    assert llm.calls == 1 and llm.cancelled
    assert text.startswith("⚠️ Stopped by user")
    assert "[RESEARCH_LIMIT]" in text
    assert trace.get("stopped_by_user") is True


@pytest.mark.asyncio
async def test_a_stop_already_pressed_makes_no_finalising_call():
    llm = SimpleLlm()
    stop = asyncio.Event()
    stop.set()

    text, _, _ = await _finalize_after_guard_stop(
        _registry_stopped_by(ResearchLimitGuard()), [], llm, None, "c", {}, {},
        fallback_reason="stopped", stop_event=stop,
    )

    llm.chat.assert_not_called()
    assert text.startswith("⚠️ Stopped by user")


class SimpleLlm:
    def __init__(self):
        self.chat = AsyncMock(return_value=(
            {"role": "assistant", "content": "final answer"},
            {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "cost": 0.5},
        ))


@pytest.mark.asyncio
async def test_after_a_context_limit_stop_no_finalising_call_is_made():
    guard = ContextLimitGuard()
    guard._seen_ratio = 1.02
    llm = SimpleLlm()
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0, "rounds": 3}

    text, usage_out, _ = await _finalize_after_guard_stop(
        _registry_stopped_by(guard), [], llm, None, "c", usage, {},
        fallback_reason="stopped", stop_event=asyncio.Event(),
    )

    llm.chat.assert_not_called()
    assert text == guard.stop_message()
    assert "102%" in text
    assert usage_out["cost"] == 0.0


@pytest.mark.asyncio
async def test_after_another_guard_the_finalising_call_is_still_made_and_counted():
    llm = SimpleLlm()
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0, "rounds": 3}

    text, usage_out, _ = await _finalize_after_guard_stop(
        _registry_stopped_by(ResearchLimitGuard()), [], llm, None, "c", usage, {},
        fallback_reason="stopped", stop_event=asyncio.Event(),
    )

    llm.chat.assert_awaited_once()
    assert text == "final answer"
    assert usage_out["cost"] == pytest.approx(0.5)
    assert usage_out["rounds"] == 3
