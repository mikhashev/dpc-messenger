"""Stop ends the work with a summary of where it stopped; Kill ends the loop at once.

Mike, 2026-10-29: a first Stop ended the run with "Stopped by user after N rounds"
and nothing of what the agent had found, and the second press (Kill) reached the
model call only, so a running tool held the loop until it returned or timed out.

Now a Stop seen at the top of a round, or between two tool calls, makes one more
call with the tools off (`tool_choice="none"`) asking for a summary, and that is
the turn's answer, `turn_end` "stopped". Only the Kill cancels that call. A Kill
while a tool runs abandons the await (every tool runs on a pool thread, which
cannot be cancelled) and ends the processes the call started through the process
registry, `stopped` with `ui_stop`, only those whose spawn origin is that call.

Fake model and fake tools; the one real child is a `python -c sleep` this test
starts through `run_supervised`, and nothing here touches `~/.dpc`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time

import pytest

from dpc_client_core import process_registry as pr
from dpc_client_core.dpc_agent.loop import (
    NOT_RUN_AFTER_STOP,
    USER_STOP_SUMMARY_REQUEST,
    run_llm_loop,
)
from dpc_client_core.dpc_agent.tools.process import run_supervised
from dpc_client_core.process_registry import SpawnOrigin

SUMMARY = "Asked: why the camera is not seen. Done: read dmesg. Stopped before lsusb. Left: lsusb."
SLEEP = [sys.executable, "-c", "import time; time.sleep(600)"]


def _usage():
    return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}


def _call(name, call_id="c1", args=None):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args or {})}}


def _asks(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}, _usage()


def _says(text):
    return {"role": "assistant", "content": text, "tool_calls": []}, _usage()


class _Llm:
    """chat() from a script: a reply, an exception to raise, or "hang"."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.started = asyncio.Event()
        self.cancelled = False

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], **kwargs})
        step = self.steps.pop(0)
        if step == "hang":
            self.started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if isinstance(step, BaseException):
            raise step
        return step


class _Tools:
    """`probe` returns at once; `slow_async` runs a coroutine on a loop of its own
    (as `ToolRegistry.execute` does an async handler); `slow_thread` blocks its
    thread; `spawns` starts a real child through `run_supervised`."""

    _ctx = None

    def __init__(self):
        self.ran: list[str] = []
        self.release = threading.Event()
        self.running = threading.Event()

    def schemas(self, core_only=False, include_restricted=False):
        return [{"name": n} for n in ("probe", "slow_async", "slow_thread", "spawns")]

    def get_timeout(self, name):
        return 60

    def execute(self, name, args, ctx=None):
        self.ran.append(name)
        if name == "slow_async":
            async def _work():
                self.running.set()
                deadline = time.monotonic() + 30
                while not self.release.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                return "late async"
            return asyncio.run(_work())
        if name == "slow_thread":
            self.running.set()
            self.release.wait(30)
            return "late thread"
        if name == "spawns":
            self.running.set()
            return run_supervised(SLEEP, launcher="run_shell", timeout=120).stdout or "spawned"
        return f"probed {args.get('what', '')}"


@pytest.fixture(autouse=True)
def _no_agent_config(monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})


@pytest.fixture
def tools():
    t = _Tools()
    yield t
    t.release.set()


def _stop_after_tool_results(stop, count=1):
    """An emit_progress that presses Stop once `count` tool results were shown
    (the loop emits "✓ name: ..." after each); runs on the loop's own thread."""
    seen = [0]

    def emit(message, *args, **kwargs):
        if isinstance(message, str) and message.startswith(("✓", "❌")):
            seen[0] += 1
            if seen[0] >= count:
                stop.set()
    return emit


async def _run(llm, tools, tmp_path, stop, kill, emit=None, conversation_id="agent_001"):
    return await run_llm_loop(
        messages=[{"role": "user", "content": "find out why the USB camera is not seen"}],
        tools=tools, llm=llm, agent_root=tmp_path / "agent_001",
        emit_progress=emit or (lambda *a, **k: None),
        task_id="t1", conversation_id=conversation_id,
        stop_event=stop, kill_event=kill,
    )


# --- Stop: the work ends, one call writes where it stopped ---------------------------


@pytest.mark.asyncio
async def test_a_stop_at_the_top_of_a_round_ends_with_the_models_summary(tmp_path, tools):
    stop, kill = asyncio.Event(), asyncio.Event()
    llm = _Llm(_asks(_call("probe", args={"what": "dmesg"})), _says(SUMMARY))

    text, _u, trace = await _run(llm, tools, tmp_path, stop, kill, _stop_after_tool_results(stop))

    assert text == SUMMARY
    assert trace["turn_end"] == "stopped" and trace["stopped_by_user"] is True
    assert len(llm.calls) == 2, "exactly one call after the stop"
    summary_call = llm.calls[1]
    assert summary_call["tool_choice"] == "none", "the summary call is made with the tools off"
    assert summary_call["messages"][-1] == {"role": "user", "content": USER_STOP_SUMMARY_REQUEST}
    assert tools.ran == ["probe"]


@pytest.mark.asyncio
async def test_a_stop_between_tool_calls_runs_none_of_the_rest_then_summarises(tmp_path, tools):
    stop, kill = asyncio.Event(), asyncio.Event()
    llm = _Llm(
        _asks(_call("probe", "c1", {"what": "dmesg"}), _call("probe", "c2", {"what": "lsusb"}),
              _call("probe", "c3", {"what": "v4l2"})),
        _says(SUMMARY),
    )

    text, _u, trace = await _run(llm, tools, tmp_path, stop, kill, _stop_after_tool_results(stop))

    assert tools.ran == ["probe"], "the calls after the stop were run"
    assert text == SUMMARY and trace["turn_end"] == "stopped"
    assert len(llm.calls) == 2
    sent = llm.calls[1]["messages"]
    results = {m["tool_call_id"]: m["content"] for m in sent if m["role"] == "tool"}
    assert results["c1"] == "probed dmesg"
    # Every call has its result, so the summary request is a valid one.
    assert results["c2"] == results["c3"] == NOT_RUN_AFTER_STOP
    assert sent[-1]["content"] == USER_STOP_SUMMARY_REQUEST


@pytest.mark.asyncio
async def test_a_stop_before_anything_was_done_makes_no_call(tmp_path, tools):
    stop = asyncio.Event()
    stop.set()
    llm = _Llm()
    text, _u, trace = await _run(llm, tools, tmp_path, stop, asyncio.Event())
    assert llm.calls == [] and text == "⚠️ Stopped by user after 0 rounds."
    assert trace["turn_end"] == "stopped"


@pytest.mark.asyncio
async def test_a_summary_call_that_fails_falls_back_to_the_stop_text(tmp_path, tools):
    stop, kill = asyncio.Event(), asyncio.Event()
    llm = _Llm(_asks(_call("probe")), RuntimeError("provider down"))

    text, _u, trace = await _run(llm, tools, tmp_path, stop, kill, _stop_after_tool_results(stop))

    assert text.startswith("⚠️ Stopped by user after 1 rounds.")
    assert "No summary" in text and "RuntimeError" in text
    assert trace["turn_end"] == "stopped"


# --- Kill: the loop ends at once -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_kill_during_the_summary_call_cancels_it(tmp_path, tools):
    stop, kill = asyncio.Event(), asyncio.Event()
    llm = _Llm(_asks(_call("probe")), "hang")
    task = asyncio.ensure_future(_run(llm, tools, tmp_path, stop, kill, _stop_after_tool_results(stop)))
    await asyncio.wait_for(llm.started.wait(), 5)
    assert llm.calls[-1]["tool_choice"] == "none", "the hanging call is the summary call"

    began = time.monotonic()
    kill.set()
    text, _u, trace = await asyncio.wait_for(task, 5)

    assert time.monotonic() - began < 1.0
    assert llm.cancelled
    assert trace["turn_end"] == "killed"
    assert text == "⚠️ Stopped by user after 1 rounds."


async def _kill_while_running(llm, tools, tmp_path, *, conversation_id="agent_001", ready=None):
    stop, kill = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(_run(llm, tools, tmp_path, stop, kill, conversation_id=conversation_id))
    ready = ready or tools.running.is_set
    deadline = time.monotonic() + 20
    while not ready():
        assert time.monotonic() < deadline, "the tool never started"
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    began = time.monotonic()
    stop.set()  # first press, as the UI sends it before the second
    kill.set()
    result = await asyncio.wait_for(task, 10)
    return result, time.monotonic() - began


@pytest.mark.parametrize("tool", ["slow_async", "slow_thread"])
@pytest.mark.asyncio
async def test_a_kill_during_a_long_tool_ends_the_loop_at_once(tmp_path, tools, tool):
    llm = _Llm(_asks(_call(tool)), _says("never asked"))

    (text, _u, trace), took = await _kill_while_running(llm, tools, tmp_path)

    assert took < 1.0, f"the loop waited {took:.1f}s for a tool sleeping 30 s"
    assert trace["turn_end"] == "killed" and trace["stopped_by_user"] is True
    assert "killed during a tool call in round 1" in text
    assert len(llm.calls) == 1, "a kill makes no summary call"
    assert not tools.release.is_set(), "the tool was released, not abandoned"


@pytest.mark.asyncio
async def test_a_kill_during_a_tool_ends_the_processes_that_call_started(tmp_path, tools):
    registry = pr.get_registry()
    llm = _Llm(_asks(_call("spawns")), _says("never asked"))
    try:
        (_text, _u, trace), took = await _kill_while_running(
            llm, tools, tmp_path, conversation_id="chat-1", ready=registry.open_entries)
        assert took < 1.0 and trace["turn_end"] == "killed"

        deadline = time.monotonic() + 20
        while registry.open_entries() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        (entry,) = registry.entries()
        assert (entry.agent_id, entry.conversation_id, entry.tool, entry.round) == (
            "agent_001", "chat-1", "spawns", 1)
        assert entry.ended_at, "the child the killed call started is still running"
        assert (entry.exit_reason, entry.stop_kind) == ("stopped", "ui_stop")
    finally:
        for e in registry.open_entries():  # only this test's own child
            handle = registry.handle_of(e.pid)
            if handle is not None:
                handle.kill()


# --- the registry's door: the turn's own children only ---------------------------------


def test_only_the_children_of_that_turn_are_killed():
    registry = pr.get_registry()
    call = SpawnOrigin(agent_id="agent_a", conversation_id="chat-1", tool="run_shell", round=4, turn_id="t1")
    registry.register(7_990_001, launcher="run_shell", launcher_kind="tool", command="x", origin=call)
    others = [
        SpawnOrigin(agent_id="agent_a", conversation_id="chat-1", tool="run_shell", round=3, turn_id="t0"),
        SpawnOrigin(agent_id="agent_b", conversation_id="chat-1", tool="run_shell", round=4, turn_id="t1"),
        SpawnOrigin(agent_id="agent_a", conversation_id="chat-2", tool="run_shell", round=4, turn_id="t1"),
        None,  # a lost attribution
    ]
    for pid, origin in enumerate(others, start=7_990_002):
        registry.register(pid, launcher="run_shell", launcher_kind="tool", command="x", origin=origin)
    registry.register(7_990_009, launcher="browser", launcher_kind="subsystem", command="x", origin=call)
    killed: list = []

    ended = registry.stop_commands_of(call, kill_tree=lambda t: killed.append(t) or "killed", wait_s=0)

    assert killed == [7_990_001]
    assert [(e.pid, e.exit_reason, e.stop_kind) for e in ended] == [(7_990_001, "stopped", "ui_stop")]
    assert sorted(e.pid for e in registry.open_entries()) == [7_990_002, 7_990_003, 7_990_004, 7_990_005, 7_990_009]
    assert registry.stop_commands_of(SpawnOrigin(), kill_tree=lambda t: killed.append(t)) == []
    # A Kill that names no turn ends nothing, rather than guessing one call.
    turnless = SpawnOrigin(agent_id="agent_a", conversation_id="chat-1", tool="run_shell", round=3)
    assert registry.stop_commands_of(turnless, kill_tree=lambda t: killed.append(t)) == []
