"""A Stop pressed in the chat a scheduled task answers in reaches the run.

Since dd51b713 a check_back or a queued chat task shows its live block, with a
Stop button, in the chat it answers in; but the Stop and Kill events were
registered only by `process_message`, so `interrupt_agent` found nothing to stop
and the run went on. The manager's `_task_progress_scope` now registers them under
the reply conversation and passes them to `process()`, and takes them out when
the run ends.

`manager.start()` and `CoreService.interrupt_agent` really run, and the stand-in
agent's `process` runs the real `run_llm_loop`; only the model and the tools are
scripts. Nothing reaches the real ~/.dpc: `Path.home()` is a temporary directory.
"""

import asyncio
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent import run_gate
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.loop import USER_STOP_SUMMARY_REQUEST, run_llm_loop
from dpc_client_core.dpc_agent.utils import AgentRegistry
from dpc_client_core.managers import agent_manager as am
from dpc_client_core.service import CoreService

AGENT = "agent_task_stop_0001"
GROUP = "group-5c0e2a7d41b9"
SUMMARY = "Asked: look at the render. Done: one probe. Stopped before the second. Left: the second."


def _usage():
    return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}


class _Llm:
    """chat() from a script: "gate" waits for `release` and asks for a tool,
    "hang" waits until cancelled, a string is the answer."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def chat(self, messages, **kwargs):
        self.calls.append([dict(m) for m in messages])
        step = self.steps.pop(0)
        if step in ("gate", "hang"):
            self.started.set()
            try:
                if step == "hang":
                    await asyncio.sleep(60)
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
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
    """Stands in for DpcAgent: the real task executor, and a `process` that runs
    the real loop over a scripted model, keeping its trace as the agent does."""

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
        self.got = {}

    async def start_task_processor(self):
        pass

    def stop_task_processor(self):
        pass

    async def process(self, message, conversation_id, emit_progress=None,
                      on_stream_chunk=None, stop_event=None, kill_event=None, **kwargs):
        self.got = {"stop_event": stop_event, "kill_event": kill_event}
        text, usage, trace = await run_llm_loop(
            messages=[{"role": "user", "content": message}],
            tools=_Tools(), llm=_Agent.llm, agent_root=_Agent.root,
            emit_progress=emit_progress or (lambda *a, **k: None),
            task_id=conversation_id, conversation_id=conversation_id,
            on_stream_chunk=on_stream_chunk,
            stop_event=stop_event, kill_event=kill_event,
        )
        self._last_usage, self._last_trace = usage, trace
        return text


@pytest.fixture(autouse=True)
def _no_agent_config(monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    AgentRegistry().register_agent(AGENT, "Task stop test agent")
    # A run a failing test leaves hanging holds its gate; the next test gets its own.
    monkeypatch.setattr(run_gate, "_RUN_GATES", {})
    _Agent.root = tmp_path / "agent_root"
    _Agent.root.mkdir()
    monkeypatch.setattr(am, "DpcAgent", _Agent)

    service = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={}),
        settings=None,
        telegram_manager=None,
        firewall=None,
        p2p_manager=SimpleNamespace(node_id="dpc-node-0000000000000000000000000000beef"),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        send_group_agent_message=AsyncMock(return_value="msg-1"),
    )
    mgr = am.DpcAgentManager(service, {}, agent_id=AGENT)
    mgr._agent_display_name = "Ark"

    async def _init_memory_indexes():
        pass

    monkeypatch.setattr(mgr, "_init_memory_indexes", _init_memory_indexes)
    return mgr


def _core(mgr):
    provider = SimpleNamespace(_managers={AGENT: mgr}, _manager=None, get_manager=lambda aid: mgr)
    svc = CoreService.__new__(CoreService)
    svc.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    return svc


async def _start_run(mgr, task_type, conversation_id):
    await mgr.start()
    mgr._agent._service = mgr.service
    task = SimpleNamespace(id="task-5f1c9a20", task_type=task_type,
                           data={"text": "look at the render again",
                                 "_reply_conversation_id": conversation_id})
    run = asyncio.ensure_future(mgr._agent._execute_task(task))
    await asyncio.wait_for(_Agent.llm.started.wait(), 2)
    return run


@pytest.mark.asyncio
async def test_a_stop_ends_a_check_back_run_with_its_summary(manager):
    _Agent.llm = _Llm("gate", SUMMARY, "a spare answer nobody should get")
    run = await _start_run(manager, "check_back", AGENT)

    assert manager.has_active_loop(AGENT), "the run is not where interrupt_agent looks"
    res = await _core(manager).interrupt_agent(agent_id=AGENT, conversation_id=AGENT)
    assert res["status"] == "stopped" and res["agent_id"] == AGENT
    _Agent.llm.release.set()
    outcome = await asyncio.wait_for(run, 5)

    assert outcome == SUMMARY
    assert manager._agent._last_trace["turn_end"] == "stopped"
    assert _Agent.llm.calls[-1][-1]["content"] == USER_STOP_SUMMARY_REQUEST
    assert manager._interrupt_events == {} and manager._kill_events == {}
    assert not manager.has_active_loop(AGENT)


@pytest.mark.asyncio
async def test_a_second_press_kills_a_chat_task_run_in_a_group(manager):
    _Agent.llm = _Llm("hang", SUMMARY)
    run = await _start_run(manager, "chat", GROUP)
    svc = _core(manager)

    assert (await svc.interrupt_agent(agent_id=AGENT, conversation_id=GROUP))["status"] == "stopped"
    await asyncio.sleep(0.2)
    assert not run.done(), "Stop alone must wait for the call in flight"
    assert (await svc.interrupt_agent(agent_id=AGENT, conversation_id=GROUP, force=True))["status"] == "stopped"
    outcome = await asyncio.wait_for(run, 5)

    assert _Agent.llm.cancelled
    assert outcome.startswith("⚠️ Stopped by user")
    assert manager._agent._last_trace["turn_end"] == "killed"
    assert manager._interrupt_events == {} and manager._kill_events == {}


@pytest.mark.asyncio
async def test_the_events_are_taken_out_when_the_run_is_cancelled(manager):
    _Agent.llm = _Llm("hang")
    run = await _start_run(manager, "check_back", AGENT)
    assert manager._agent.got["stop_event"] is manager._interrupt_events[AGENT]
    assert manager._agent.got["kill_event"] is manager._kill_events[AGENT]

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert manager._interrupt_events == {} and manager._kill_events == {}
    res = await _core(manager).interrupt_agent(agent_id=AGENT, conversation_id=AGENT)
    assert res["status"] == "no_active_loop"
