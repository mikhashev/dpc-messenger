"""A scheduled task run shows its progress in the chat it answers in.

A check_back or a queued chat task ran `DpcAgent.process` with no progress and
no stream callback, so the chat it answered in showed no live activity block,
only the final post. The manager now installs `_task_progress_scope` on its
agent when it starts it, and the task executor enters it around the two task
types that run the model: the early "thinking" line, a progress callback and a
text-chunk callback bound to the reply conversation, and the clear at the end,
including when the run fails.

`manager.start()` really runs here, so dropping the wiring there turns these
red as surely as dropping the executor's half. Only the model is a stub: the
stand-in agent borrows the real executor methods and its `process` calls its
callbacks the way the loop does. Nothing reaches the real ~/.dpc: `Path.home()`
is a temporary directory.
"""

import asyncio
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.utils import AgentRegistry
from dpc_client_core.managers import agent_manager as am

AGENT = "agent_task_progress_0001"
GROUP = "group-970e5c7006a0"


class _Agent:
    """Stands in for DpcAgent: the real task executor, a model that is a script."""

    _execute_task = DpcAgent._execute_task
    _execute_task_guarded = DpcAgent._execute_task_guarded
    _convert_task_data_to_prompt = DpcAgent._convert_task_data_to_prompt

    fail = False

    def __init__(self, run_gate=None, **kwargs):
        self._run_gate = run_gate
        self._task_handlers = {}
        self._service = None
        self._telegram_send_fn = None
        self.display_name = "Ark"
        self.agent_root = SimpleNamespace(name=AGENT)
        self._task_progress_scope = None
        self.seen = []

    async def start_task_processor(self):
        pass

    def stop_task_processor(self):
        pass

    async def process(self, message, conversation_id, emit_progress=None,
                      on_stream_chunk=None, **kwargs):
        self.seen.append((message, conversation_id, dict(kwargs)))
        if self.fail:
            raise RuntimeError("the model is down")
        if emit_progress is not None:
            emit_progress("Executing shell...", "shell", 1)
            emit_progress("→ shell", None, 2, [{"name": "shell"}], 41.5)
        if on_stream_chunk is not None:
            await on_stream_chunk("looking", conversation_id)
        return "the run's answer"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    AgentRegistry().register_agent(AGENT, "Task progress test agent")
    _Agent.fail = False
    monkeypatch.setattr(am, "DpcAgent", _Agent)

    service = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={}),
        settings=None,
        telegram_manager=None,
        firewall=None,
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        send_group_agent_message=AsyncMock(return_value="msg-1"),
    )
    mgr = am.DpcAgentManager(service, {}, agent_id=AGENT)
    mgr._agent_display_name = "Ark"

    async def _init_memory_indexes():
        pass

    monkeypatch.setattr(mgr, "_init_memory_indexes", _init_memory_indexes)
    return mgr


def _sent(manager):
    return [(c.args[0], c.args[1]) for c in manager.service.local_api.broadcast_event.call_args_list]


async def _run(manager, task_type, data, conversation_id=GROUP):
    await manager.start()
    agent = manager._agent
    agent._service = manager.service
    task = SimpleNamespace(id="task-2692780f", task_type=task_type,
                           data={"text": "look again", "_reply_conversation_id": conversation_id, **data})
    try:
        return await agent._execute_task(task)
    finally:
        await asyncio.sleep(0.05)  # the progress events are sent from tasks


def _check_progress_for(manager, conversation_id):
    events = _sent(manager)
    names = [name for name, _ in events]
    assert names[0] == "agent_progress", f"no early thinking line: {names}"
    assert events[0][1]["message"] == "thinking"
    assert events[0][1]["conversation_id"] == conversation_id
    assert events[0][1]["agent_name"] == "Ark" and events[0][1]["agent_id"] == AGENT
    rounds = [p for n, p in events if n == "agent_progress" and p["round"] is not None]
    assert [p["round"] for p in rounds] == [1, 2]
    assert all(p["conversation_id"] == conversation_id for p in rounds)
    assert rounds[1]["speed"] == 41.5 and rounds[1]["tool_calls"]
    chunks = [p for n, p in events if n == "agent_text_chunk"]
    assert [(c["conversation_id"], c["chunk"]) for c in chunks] == [(conversation_id, "looking")]
    assert names[-1] == "agent_progress_clear"
    assert events[-1][1] == {"conversation_id": conversation_id}


@pytest.mark.asyncio
async def test_a_check_back_run_shows_its_progress_in_the_group_it_answers_in(manager):
    outcome = await _run(manager, "check_back", {"_check_back_depth": 1})

    assert outcome == "the run's answer"
    assert manager._agent.seen[0][2]["check_back_depth"] == 1
    _check_progress_for(manager, GROUP)
    manager.service.send_group_agent_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_chat_task_run_shows_its_progress_in_the_chat_it_answers_in(manager):
    outcome = await _run(manager, "chat", {}, conversation_id=AGENT)

    assert outcome == "the run's answer"
    _check_progress_for(manager, AGENT)


@pytest.mark.asyncio
async def test_a_failed_task_run_still_clears_its_activity_line(manager):
    _Agent.fail = True

    with pytest.raises(RuntimeError):
        await _run(manager, "check_back", {})

    names = [name for name, _ in _sent(manager)]
    assert names == ["agent_progress", "agent_progress_clear"]


@pytest.mark.asyncio
async def test_a_task_type_that_does_not_run_the_model_shows_no_activity(manager):
    await manager.start()
    manager._agent._service = manager.service
    seen = []
    manager._agent._task_handlers["reminder"] = lambda data: seen.append(data) or "sent"

    outcome = await manager._agent._execute_task(
        SimpleNamespace(id="task-r", task_type="reminder", data={"message": "x"}))
    await asyncio.sleep(0.05)

    assert outcome == "sent" and seen and _sent(manager) == []
