"""A second Stop press (Kill) abandons the model call in flight.

A local model can spend minutes prefilling a 170k-token prompt on its first
call. Stop is only read between rounds, so the run sat on "Stopping..." until
the model answered. Kill cancels the call task, which closes the request; the
adapter never reaches its usage-row write, and the run ends with a stopped line.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.loop import run_llm_loop
from dpc_client_core.managers.agent_manager import DpcAgentManager
from dpc_client_core.service import CoreService

GROUP = "group-b88b65076b85"


class HangingLlm:
    """chat() awaits an Event like a prefilling llama-server; usage is written after it returns."""

    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = False
        self.usage_rows: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.usage_rows.append({"task_id": kwargs.get("task_id")})
        return {"role": "assistant", "content": "final answer", "tool_calls": []}, {
            "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0,
        }


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return [{"name": "noop"}]

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        return "ok"


@pytest.fixture(autouse=True)
def _no_agent_config(monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})


def _loop(llm, tmp_path, stop, kill):
    return run_llm_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=_Tools(), llm=llm, agent_root=tmp_path,
        emit_progress=lambda *a, **k: None,
        task_id="t1", stop_event=stop, kill_event=kill,
    )


@pytest.mark.asyncio
async def test_stop_alone_does_not_end_a_call_in_flight_but_kill_does(tmp_path):
    llm = HangingLlm()
    stop, kill = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(_loop(llm, tmp_path, stop, kill))
    await asyncio.wait_for(llm.started.wait(), 2)

    stop.set()  # first press
    await asyncio.sleep(0.3)
    assert not task.done(), "Stop alone must wait for the round boundary"

    began = time.monotonic()
    kill.set()  # second press
    text, _usage, trace = await asyncio.wait_for(task, 2)

    assert time.monotonic() - began < 1.0
    assert llm.cancelled
    assert llm.usage_rows == []
    assert text.startswith("⚠️ Stopped by user")
    assert "killed mid-call" in text
    assert trace["stopped_by_user"] is True and trace["killed_mid_call"] is True


@pytest.mark.asyncio
async def test_without_a_kill_the_call_returns_normally(tmp_path):
    llm = HangingLlm()
    llm.release.set()
    text, _u, trace = await _loop(llm, tmp_path, asyncio.Event(), asyncio.Event())
    assert text == "final answer"
    assert len(llm.usage_rows) == 1
    assert not trace.get("killed_mid_call")


def _manager(agent_id, running_in=()):
    m = DpcAgentManager.__new__(DpcAgentManager)
    m.agent_id = agent_id
    m._interrupt_events = {cid: asyncio.Event() for cid in running_in}
    m._kill_events = {cid: asyncio.Event() for cid in running_in}
    return m


def _service(*managers):
    provider = SimpleNamespace(
        _managers={m.agent_id: m for m in managers}, _manager=None,
        get_manager=lambda aid: None,
    )
    svc = CoreService.__new__(CoreService)
    svc.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    return svc


@pytest.mark.asyncio
async def test_force_sets_the_kill_event_of_the_agent_running_in_the_group():
    ark = _manager("agent_001")
    johnny = _manager("agent_johnny", running_in=(GROUP,))
    svc = _service(ark, johnny)

    res = await svc.interrupt_agent(agent_id="agent_001", conversation_id=GROUP)
    assert res["status"] == "stopped"
    assert johnny._interrupt_events[GROUP].is_set()
    assert not johnny._kill_events[GROUP].is_set()

    res = await svc.interrupt_agent(agent_id="agent_001", conversation_id=GROUP, force=True)
    assert res["agent_id"] == "agent_johnny"
    assert johnny._kill_events[GROUP].is_set()


@pytest.mark.asyncio
async def test_force_in_a_solo_chat_reaches_the_named_agent():
    solo = _manager("agent_001", running_in=("agent_001",))
    res = await _service(solo).interrupt_agent(agent_id="agent_001", conversation_id="agent_001", force=True)
    assert res["status"] == "stopped"
    assert solo._kill_events["agent_001"].is_set()
