"""A scheduled task's answer is kept in the 1:1 agent chat it answers in.

A check_back or a queued chat task that answered in a 1:1 agent chat returned its
text to the queue and nothing else: a group got the post and Telegram the send,
but the agent's own chat got neither the prompt nor the answer, so the person
never saw what the agent came back with. The task progress scope now records
both the way `process_message` records a chat run - the prompt as a user-side
record marked with its `task_type`, the answer with the run's trace and its
`turn_end` - and pushes the history to the open chat. A group keeps its post as
the record (ADR-031 §3): no agent-side record there.

`manager.start()` really runs, so does the real ConversationMonitor; only the
model and the tools are scripts. Nothing reaches the real ~/.dpc: `Path.home()`
is a temporary directory.
"""

import asyncio
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent import run_gate
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.loop import run_llm_loop
from dpc_client_core.dpc_agent.utils import AgentRegistry
from dpc_client_core.managers import agent_manager as am
from dpc_client_core.service import CoreService

AGENT = "agent_task_record_0001"
GROUP = "group-3d7f0b9e12a4"
NODE = "dpc-node-0000000000000000000000000000cafe"
PROMPT = "look at the render again"
ANSWER = "The render finished: 240 frames, no errors."
SUMMARY = "Asked: the render. Done: one probe. Stopped before the second. Left: the second."
TOOL_CALLS = [{"tool": "shell", "input": "{}", "output": "ok", "is_error": False,
               "duration_ms": 3, "round": 1}]


def _usage():
    return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}


class _Llm:
    """chat() from a script: "gate" waits for `release` and asks for a tool,
    a string is the answer."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, messages, **kwargs):
        step = self.steps.pop(0)
        if step == "gate":
            self.started.set()
            await self.release.wait()
            call = {"id": "c1", "type": "function",
                    "function": {"name": "probe", "arguments": json.dumps({})}}
            return {"role": "assistant", "content": "", "tool_calls": [call]}, _usage()
        return {"role": "assistant", "content": step, "tool_calls": []}, _usage()


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return [{"name": "probe"}]

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        return "probed"


class _Agent:
    """Stands in for DpcAgent: the real task executor; `process` either answers
    from a script, publishing usage and trace as the agent does, or runs the
    real loop over a scripted model when `llm` is set."""

    _execute_task = DpcAgent._execute_task
    _execute_task_guarded = DpcAgent._execute_task_guarded
    _convert_task_data_to_prompt = DpcAgent._convert_task_data_to_prompt

    llm = None
    root = None

    def __init__(self, run_gate=None, **kwargs):
        self._run_gate = run_gate
        self._task_handlers = {}
        self._service = None
        self._telegram_send_fn = None
        self.display_name = "Ark"
        self.agent_root = SimpleNamespace(name=AGENT)
        self._task_progress_scope = None
        self._last_trace = {}
        self._last_usage = {}

    async def start_task_processor(self):
        pass

    def stop_task_processor(self):
        pass

    async def process(self, message, conversation_id, emit_progress=None,
                      on_stream_chunk=None, stop_event=None, kill_event=None, **kwargs):
        if _Agent.llm is not None:
            text, usage, trace = await run_llm_loop(
                messages=[{"role": "user", "content": message}],
                tools=_Tools(), llm=_Agent.llm, agent_root=_Agent.root,
                emit_progress=emit_progress or (lambda *a, **k: None),
                task_id=conversation_id, conversation_id=conversation_id,
                stop_event=stop_event, kill_event=kill_event,
            )
            self._last_usage, self._last_trace = usage, trace
            return text
        self._last_usage = {"thinking": "checked the frame count"}
        self._last_trace = {"accumulated_tool_calls": TOOL_CALLS}
        return ANSWER


@pytest.fixture(autouse=True)
def _no_agent_config(monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    AgentRegistry().register_agent(AGENT, "Task record test agent")
    monkeypatch.setattr(run_gate, "_RUN_GATES", {})
    _Agent.llm = None
    _Agent.root = tmp_path / "agent_root"
    _Agent.root.mkdir()
    monkeypatch.setattr(am, "DpcAgent", _Agent)

    service = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={}),
        settings=None,
        telegram_manager=None,
        firewall=None,
        p2p_manager=SimpleNamespace(node_id=NODE),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        send_group_agent_message=AsyncMock(return_value="msg-1"),
    )
    mgr = am.DpcAgentManager(service, {"context_window": 32768}, agent_id=AGENT)
    mgr._agent_display_name = "Ark"

    async def _init_memory_indexes():
        pass

    monkeypatch.setattr(mgr, "_init_memory_indexes", _init_memory_indexes)
    return mgr


def _task(task_type, conversation_id):
    return SimpleNamespace(id="task-81c4e2d0", task_type=task_type,
                           data={"text": PROMPT, "_reply_conversation_id": conversation_id})


async def _run(mgr, task_type, conversation_id):
    await mgr.start()
    mgr._agent._service = mgr.service
    outcome = await mgr._agent._execute_task(_task(task_type, conversation_id))
    await asyncio.sleep(0.05)  # the progress events are sent from tasks
    return outcome


def _on_disk(tmp_path, conversation_id):
    path = tmp_path / ".dpc" / "conversations" / conversation_id / "history.json"
    return json.loads(path.read_text(encoding="utf-8"))["messages"]


def _pushes(mgr, conversation_id):
    return [c.args[1] for c in mgr.service.local_api.broadcast_event.call_args_list
            if c.args[0] == "agent_history_updated" and c.args[1]["conversation_id"] == conversation_id]


def _check_the_run_is_kept(mgr, tmp_path, task_type):
    records = _on_disk(tmp_path, AGENT)
    assert [r["role"] for r in records] == ["user", "assistant"], records
    prompt, answer = records

    assert prompt["content"] == PROMPT
    assert prompt["task_type"] == task_type, "the prompt does not say it came from the scheduler"
    assert "source" not in prompt, "source is the transport field; the scheduler is not one"
    assert prompt["sender_node_id"] == NODE
    assert prompt["sender_name"] == f"Scheduled {task_type}"
    assert prompt["sender_name"] != "User"

    assert answer["content"] == ANSWER
    assert answer["sender_name"] == "Ark" and answer["sender_node_id"] == AGENT
    assert answer["thinking"] == "checked the frame count"
    assert answer["tool_calls"] == TOOL_CALLS
    assert "turn_end" not in answer

    pushes = _pushes(mgr, AGENT)
    assert len(pushes) == 2, "the prompt and then the answer reach the open chat"
    assert [m["content"] for m in pushes[0]["messages"]] == [PROMPT]
    assert [m["content"] for m in pushes[-1]["messages"]] == [PROMPT, ANSWER]


@pytest.mark.asyncio
async def test_a_check_back_in_a_one_to_one_chat_is_kept_in_its_history(manager, tmp_path):
    assert await _run(manager, "check_back", AGENT) == ANSWER
    _check_the_run_is_kept(manager, tmp_path, "check_back")


@pytest.mark.asyncio
async def test_a_chat_task_in_a_one_to_one_chat_is_kept_the_same_way(manager, tmp_path):
    assert await _run(manager, "chat", AGENT) == ANSWER
    _check_the_run_is_kept(manager, tmp_path, "chat")


@pytest.mark.asyncio
async def test_a_group_check_back_leaves_only_its_post(manager, tmp_path):
    assert await _run(manager, "check_back", GROUP) == ANSWER

    manager.service.send_group_agent_message.assert_awaited_once()
    assert not (tmp_path / ".dpc" / "conversations" / GROUP).exists()
    assert GROUP not in manager._agent_monitors
    assert _pushes(manager, GROUP) == []


@pytest.mark.asyncio
async def test_a_stopped_check_back_is_kept_with_its_turn_end(manager, tmp_path):
    _Agent.llm = _Llm("gate", SUMMARY, "a spare answer nobody should get")
    await manager.start()
    manager._agent._service = manager.service
    run = asyncio.ensure_future(manager._agent._execute_task(_task("check_back", AGENT)))
    await asyncio.wait_for(_Agent.llm.started.wait(), 2)

    provider = SimpleNamespace(_managers={AGENT: manager}, _manager=None, get_manager=lambda a: manager)
    svc = CoreService.__new__(CoreService)
    svc.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    assert (await svc.interrupt_agent(agent_id=AGENT, conversation_id=AGENT))["status"] == "stopped"
    _Agent.llm.release.set()
    assert await asyncio.wait_for(run, 5) == SUMMARY

    prompt, answer = _on_disk(tmp_path, AGENT)
    assert prompt["content"] == PROMPT
    assert answer["content"] == SUMMARY and answer["turn_end"] == "stopped"


@pytest.mark.asyncio
async def test_a_task_with_no_chat_to_answer_in_writes_no_history(manager, tmp_path):
    await manager.start()
    manager._agent._service = manager.service
    task = SimpleNamespace(id="task-0a0b0c0d", task_type="chat", data={"text": PROMPT})

    assert await manager._agent._execute_task(task) == ANSWER
    assert manager._agent_monitors == {}
    assert not (tmp_path / ".dpc" / "conversations" / "task-0a0b0c0d").exists()
