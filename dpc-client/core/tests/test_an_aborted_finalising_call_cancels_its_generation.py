"""A finalising call that is abandoned ends its generation (loop audit 2026-11-09)."""
import asyncio

import pytest

from dpc_client_core.dpc_agent import loop as L


class _FakeLlm:
    """`chat` hangs until cancelled (or returns at once when `hang` is False)."""

    def __init__(self, hang=True, unwind_s=0.0):
        self.hang, self.unwind_s = hang, unwind_s
        self.started = asyncio.Event()
        self.cancelled = False

    async def chat(self, messages, **kw):
        self.started.set()
        if not self.hang:
            return {"content": "ok"}, {"request_id": 7}
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            if self.unwind_s:
                await asyncio.sleep(self.unwind_s)
            raise


def _call(llm, cancel_on=None):
    return L._one_finalising_call(llm, [], None, {}, cancel_on=cancel_on)


async def _t_cancelling_the_finalising_call_cancels_its_chat_task():
    llm = _FakeLlm()
    t = asyncio.ensure_future(_call(llm, asyncio.Event()))
    await llm.started.wait()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert llm.cancelled


async def _t_cancelling_without_a_cancel_event_cancels_its_chat_task():
    llm = _FakeLlm()
    t = asyncio.ensure_future(_call(llm))
    await llm.started.wait()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert llm.cancelled


async def _t_the_cancel_event_firing_cancels_the_chat_task_and_returns_none():
    llm, ev = _FakeLlm(), asyncio.Event()
    t = asyncio.ensure_future(_call(llm, ev))
    await llm.started.wait()
    ev.set()
    assert await t is None
    assert llm.cancelled


async def _t_a_normal_completion_is_unchanged():
    llm = _FakeLlm(hang=False)
    reply = await _call(llm, asyncio.Event())
    assert reply["content"] == "ok"
    assert not llm.cancelled


async def _t_a_kill_still_raises_call_killed_within_the_bound(monkeypatch):
    monkeypatch.setattr(L, "CANCEL_UNWIND_WAIT_S", 0.05)
    llm, ev = _FakeLlm(unwind_s=5), asyncio.Event()  # unwinds slower than the bound
    t = asyncio.ensure_future(L._chat_unless_killed(llm.chat([]), ev))
    await llm.started.wait()
    ev.set()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(L.CallKilled):
        await t
    assert loop.time() - t0 < 1.0
    assert llm.cancelled


def test_cancelling_the_finalising_call_cancels_its_chat_task():
    asyncio.run(_t_cancelling_the_finalising_call_cancels_its_chat_task())


def test_cancelling_without_a_cancel_event_cancels_its_chat_task():
    asyncio.run(_t_cancelling_without_a_cancel_event_cancels_its_chat_task())


def test_the_cancel_event_firing_cancels_the_chat_task_and_returns_none():
    asyncio.run(_t_the_cancel_event_firing_cancels_the_chat_task_and_returns_none())


def test_a_normal_completion_is_unchanged():
    asyncio.run(_t_a_normal_completion_is_unchanged())


def test_a_kill_still_raises_call_killed_within_the_bound(monkeypatch):
    asyncio.run(_t_a_kill_still_raises_call_killed_within_the_bound(monkeypatch))
