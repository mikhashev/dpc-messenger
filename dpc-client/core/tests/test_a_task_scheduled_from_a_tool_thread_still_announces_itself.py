"""A task scheduled from a tool's worker thread still announces itself.

`schedule_task` emitted TASK_SCHEDULED only when a loop was running in the
calling thread. The schedule_task tool runs in an executor thread, so the log
read "scheduled (no event loop for emission)" and the event never went out. The
agent now remembers the loop it runs on and posts the event onto it.
"""

import asyncio
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.events import EventType
from dpc_client_core.dpc_agent.task_queue import TaskPriority


class _Events:
    def __init__(self):
        self.emitted = []

    async def emit(self, event_type, data=None):
        self.emitted.append((event_type, data))


def _agent(loop):
    agent = DpcAgent.__new__(DpcAgent)
    agent.queue = SimpleNamespace(schedule=lambda **kw: SimpleNamespace(id="task-1"))
    agent.events = _Events()
    agent._event_loop = loop
    return agent


@pytest.mark.asyncio
async def test_a_task_scheduled_from_a_worker_thread_emits_task_scheduled():
    agent = _agent(asyncio.get_running_loop())

    task = await asyncio.to_thread(
        agent.schedule_task, "check_back", {"text": "x"}, TaskPriority.NORMAL, 60)
    await asyncio.sleep(0.05)

    assert task.id == "task-1"
    assert agent.events.emitted == [(EventType.TASK_SCHEDULED, {
        "task_id": "task-1", "task_type": "check_back",
        "priority": "NORMAL", "delay_seconds": 60,
    })]


def test_a_task_scheduled_with_no_loop_known_is_scheduled_without_an_event():
    agent = _agent(None)

    assert agent.schedule_task("check_back", {"text": "x"}).id == "task-1"
    assert agent.events.emitted == []
