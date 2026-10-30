"""Kill ends every process of the turn and the agent's browser; Stop ends neither.

Mike, 2026-10-30: Kill left the Camoufox window Warren had opened, and ended only
the one run_shell of the round it landed in. Now the loop names the turn on every
spawn origin (`turn_id`), and the Kill ends every open tool entry of that agent,
chat and turn, whatever its round and tool, then closes the agent's browser
sessions with the orderly close (cookies written back), killing the browser's
processes only when that close does not finish in `KILL_CLOSE_TIMEOUT_SECONDS`.
An earlier turn's process stays (an agent may start a server on purpose), and so
does another chat's, a subsystem, and an entry with no turn.

Fake model and tools; fake pids are never signalled (the tree kill is replaced).
The real children are `python -c sleep` started here. `DPC_HOME` is a tmp dir.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time

import pytest

from dpc_client_core import process_registry as pr
from dpc_client_core.dpc_agent.loop import run_llm_loop
from dpc_client_core.dpc_agent.tools import browser as B
from dpc_client_core.process_registry import SPAWN_ORIGIN, SpawnOrigin

SLEEP = [sys.executable, "-c", "import time; time.sleep(600)"]
AGENT = "agent_001"
CHAT = "chat-1"


def _usage():
    return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}


def _call(name, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps({})}}


def _asks(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}, _usage()


def _says(text):
    return {"role": "assistant", "content": text, "tool_calls": []}, _usage()


class _Llm:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.started = asyncio.Event()

    async def chat(self, messages, **kwargs):
        step = self.steps.pop(0)
        if step == "hang":
            self.started.set()
            await asyncio.sleep(60)
        return step


class _Tools:
    """`run_shell` and `git_tool` each register a fake pid under the origin the
    loop set, `strata` registers a subsystem the same way, `hang` blocks."""

    _ctx = None
    PIDS = {"run_shell": 7_991_001, "git_tool": 7_991_002, "strata": 7_991_003}

    def __init__(self):
        self.origins: list = []
        self.running = threading.Event()
        self.release = threading.Event()

    def schemas(self, core_only=False, include_restricted=False):
        return [{"name": n} for n in ("run_shell", "git_tool", "strata", "hang")]

    def get_timeout(self, name):
        return 60

    def execute(self, name, args, ctx=None):
        if name == "hang":
            self.running.set()
            self.release.wait(30)
            return "late"
        self.origins.append(SPAWN_ORIGIN.get())
        kind = "subsystem" if name == "strata" else "tool"
        pr.get_registry().register(self.PIDS[name], launcher=name, launcher_kind=kind, command=name)
        return f"{name} started"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    monkeypatch.setenv("DPC_HOME", str(tmp_path / "dpc_home"))
    saved = (dict(B._active_browser_sessions), dict(B._fetch_sessions))
    B._active_browser_sessions.clear()
    B._fetch_sessions.clear()
    yield
    B._active_browser_sessions.clear()
    B._active_browser_sessions.update(saved[0])
    B._fetch_sessions.clear()
    B._fetch_sessions.update(saved[1])


@pytest.fixture
def killed(monkeypatch):
    """The tree kill replaced: the fake pids are recorded, never signalled."""
    aimed: list = []
    monkeypatch.setattr(pr, "_kill_tree_of", lambda target: aimed.append(target) or "killed")
    return aimed


@pytest.fixture
def tools():
    t = _Tools()
    yield t
    t.release.set()


def _seed_other_entries():
    """What the Kill of this turn must leave: an earlier turn in the same chat,
    another chat, and an entry with no turn at all."""
    registry = pr.get_registry()
    registry.register(7_991_101, launcher="run_shell", launcher_kind="tool", command="server",
                      origin=SpawnOrigin(AGENT, CHAT, "run_shell", 1, turn_id="20261030T080000Z-earlier0"))
    registry.register(7_991_102, launcher="run_shell", launcher_kind="tool", command="other chat",
                      origin=SpawnOrigin(AGENT, "chat-2", "run_shell", 1, turn_id="20261030T080000Z-otherchat"))
    registry.register(7_991_103, launcher="run_shell", launcher_kind="tool", command="no turn",
                      origin=SpawnOrigin(AGENT, CHAT, "run_shell", 1))


class _FakeSession:
    """Stands in for an AuthBrowser in the per-agent registry; records its close."""

    def __init__(self, agent_id):
        self._agent_id = agent_id
        self._headed = False
        self._executor = None
        self.closed = threading.Event()

    def _get_executor(self):
        if self._executor is None:
            self._executor = B._PinnedThread(f"fake-{self._agent_id}")
        return self._executor

    def close(self):
        self.closed.set()


async def _run(llm, tools, tmp_path, stop, kill, emit=None):
    return await run_llm_loop(
        messages=[{"role": "user", "content": "start the server and look at the page"}],
        tools=tools, llm=llm, agent_root=tmp_path / AGENT,
        emit_progress=emit or (lambda *a, **k: None),
        task_id="t1", conversation_id=CHAT, stop_event=stop, kill_event=kill,
    )


async def _wait_closed(pids, timeout=10.0):
    registry = pr.get_registry()
    deadline = time.monotonic() + timeout
    while {e.pid for e in registry.open_entries()} & set(pids):
        assert time.monotonic() < deadline, "the Kill did not end the turn's processes"
        await asyncio.sleep(0.02)


def _open_pids():
    return sorted(e.pid for e in pr.get_registry().open_entries())


# --- the loop: what the Kill of a turn reaches ---------------------------------------------


@pytest.mark.asyncio
async def test_a_kill_ends_the_processes_of_earlier_rounds_and_other_tools_of_the_turn(tmp_path, tools, killed):
    _seed_other_entries()
    session = _FakeSession(AGENT)
    other = _FakeSession("agent_002")
    B._active_browser_sessions[AGENT] = session
    B._active_browser_sessions["agent_002"] = other
    llm = _Llm(_asks(_call("run_shell", "c1"), _call("strata", "c2")), _asks(_call("git_tool", "c3")),
               _asks(_call("hang", "c4")), _says("never asked"))
    stop, kill = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(_run(llm, tools, tmp_path, stop, kill))
    await asyncio.get_running_loop().run_in_executor(None, tools.running.wait, 20)
    stop.set()
    kill.set()
    _text, _u, trace = await asyncio.wait_for(task, 10)
    await _wait_closed([7_991_001, 7_991_002])

    assert trace["turn_end"] == "killed"
    turns = {o.turn_id for o in tools.origins}
    assert len(turns) == 1 and all(turns), f"every call of the turn carries one turn id: {turns}"
    assert sorted(killed) == [7_991_001, 7_991_002], "round 1's run_shell and round 2's git_tool"
    ended = {e.pid: (e.exit_reason, e.stop_kind, e.round) for e in pr.get_registry().entries() if e.ended_at}
    assert ended == {7_991_001: ("stopped", "ui_stop", 1), 7_991_002: ("stopped", "ui_stop", 2)}
    # The earlier turn, the other chat, the entry with no turn and the subsystem stay.
    assert _open_pids() == [7_991_003, 7_991_101, 7_991_102, 7_991_103]
    assert session.closed.wait(10), "the agent's browser session was not closed"
    assert AGENT not in B._active_browser_sessions
    assert not other.closed.is_set() and B._active_browser_sessions["agent_002"] is other


@pytest.mark.asyncio
async def test_a_kill_during_the_model_call_ends_what_the_turn_started(tmp_path, tools, killed):
    llm = _Llm(_asks(_call("run_shell", "c1")), "hang")
    stop, kill = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(_run(llm, tools, tmp_path, stop, kill))
    await asyncio.wait_for(llm.started.wait(), 10)
    kill.set()
    _text, _u, trace = await asyncio.wait_for(task, 10)
    await _wait_closed([7_991_001])

    assert trace["turn_end"] == "killed"
    assert killed == [7_991_001]


@pytest.mark.asyncio
async def test_a_stop_ends_no_process_and_leaves_the_browser(tmp_path, tools, killed):
    _seed_other_entries()
    session = _FakeSession(AGENT)
    B._active_browser_sessions[AGENT] = session
    stop, kill = asyncio.Event(), asyncio.Event()

    def emit(message, *args, **kwargs):
        if isinstance(message, str) and message.startswith("✓"):
            stop.set()

    llm = _Llm(_asks(_call("run_shell", "c1")), _says("Started the server; stopped before the page."))
    _text, _u, trace = await _run(llm, tools, tmp_path, stop, kill, emit)
    await asyncio.sleep(0.3)  # a Kill's thread, had one started, would have run by now

    assert trace["turn_end"] == "stopped"
    assert killed == []
    assert _open_pids() == [7_991_001, 7_991_101, 7_991_102, 7_991_103]
    assert not session.closed.is_set() and B._active_browser_sessions[AGENT] is session


# --- the registry's door ---------------------------------------------------------------------


def test_an_origin_with_a_turn_matches_the_turn_and_nothing_without_one(killed):
    registry = pr.get_registry()
    turn = "20261030T084500Z-thisturn"
    registry.register(7_992_001, launcher="run_shell", launcher_kind="tool", command="a",
                      origin=SpawnOrigin(AGENT, CHAT, "run_shell", 2, turn_id=turn))
    registry.register(7_992_002, launcher="browser_navigate", launcher_kind="tool", command="b",
                      origin=SpawnOrigin(AGENT, CHAT, "browser_navigate", 5, turn_id=turn))
    registry.register(7_992_003, launcher="run_shell", launcher_kind="tool", command="c",
                      origin=SpawnOrigin(AGENT, CHAT, "run_shell", 5))
    registry.register(7_992_004, launcher="run_shell", launcher_kind="tool", command="d",
                      origin=SpawnOrigin("agent_002", CHAT, "run_shell", 5, turn_id=turn))
    registry.register(7_992_005, launcher="browser", launcher_kind="subsystem", command="e",
                      origin=SpawnOrigin(AGENT, CHAT, "browser_navigate", 5, turn_id=turn))

    ended = registry.stop_commands_of(SpawnOrigin(AGENT, CHAT, "run_shell", 5, turn_id=turn), wait_s=0)

    assert sorted(killed) == [7_992_001, 7_992_002]
    assert {(e.pid, e.turn_id) for e in ended} == {(7_992_001, turn), (7_992_002, turn)}
    assert _open_pids() == [7_992_003, 7_992_004, 7_992_005]


# --- the browser: the Kill's close ------------------------------------------------------------


class _Page:
    url = "https://example.org/"


class _Driver:
    """Stands in for the Camoufox context manager: its exit ends the real child,
    as Playwright's exit ends the driver process."""

    def __init__(self, child):
        self.child = child

    def __exit__(self, *exc):
        self.child.kill()
        self.child.wait(timeout=10)


@pytest.fixture
def child():
    proc = subprocess.Popen(SLEEP)
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def _browser_on(child, agent_id):
    """An AuthBrowser whose launched tree is `child`, entered in the registry the
    way `_open` enters a real launch."""
    ab = B.AuthBrowser(agent_id=agent_id, headed=True)
    ab._browser_pids = {child.pid}
    ab._register_browser_pids()
    ab._cm = _Driver(child)
    ab._page = _Page()
    ab._context = object()
    B._active_browser_sessions[agent_id] = ab
    return ab


def _browser_entry(pid):
    return [e for e in pr.get_registry().entries() if e.pid == pid and e.launcher == "browser"]


def test_a_launched_browser_is_in_the_registry_as_the_browser_subsystem(child):
    _browser_on(child, "agent_kill")
    (entry,) = _browser_entry(child.pid)
    assert (entry.launcher_kind, entry.is_open) == ("subsystem", True)


def test_the_kill_closes_the_agents_browser_writes_cookies_and_records_browser_closed(child, caplog):
    ab = _browser_on(child, "agent_kill")
    written: list = []
    ab._persist_session_cookies = lambda: written.append(True)  # type: ignore[method-assign]
    other = _FakeSession("agent_other")
    B._active_browser_sessions["agent_other"] = other

    with caplog.at_level(logging.INFO, logger=B.log.name):
        closed = asyncio.run(B.close_browser_sessions_of("agent_kill"))

    assert closed == 1
    assert written == [True], "the orderly close writes the cookies back"
    assert child.poll() is not None
    (entry,) = _browser_entry(child.pid)
    assert (entry.exit_reason, entry.stop_kind) == ("stopped", "browser_closed")
    kill_lines = [r for r in caplog.records
                  if r.levelno == logging.INFO and r.getMessage() == "Kill: closing the browser session of agent_kill"]
    assert len(kill_lines) == 1
    assert "agent_kill" not in B._active_browser_sessions
    assert not other.closed.is_set() and B._active_browser_sessions["agent_other"] is other


def test_a_close_that_does_not_finish_in_time_is_replaced_by_the_kill_of_the_browser(child, monkeypatch):
    monkeypatch.setattr(B, "KILL_CLOSE_TIMEOUT_SECONDS", 0.3)
    ab = _browser_on(child, "agent_wedged")
    ab.close = lambda: time.sleep(5)  # type: ignore[method-assign]  # a driver that never answers

    began = time.monotonic()
    closed = asyncio.run(B.close_browser_sessions_of("agent_wedged"))

    assert closed == 1
    assert time.monotonic() - began < 4.0
    child.wait(timeout=5)
    (entry,) = _browser_entry(child.pid)
    assert (entry.exit_reason, entry.stop_kind) == ("stopped", "browser_closed")
    assert ab not in B.get_active_camoufox_browsers()


class _GonePage:
    def title(self):
        raise Exception("Target page, context or browser has been closed")


def _gone_session(agent_id, closes):
    ab = B.AuthBrowser(agent_id=agent_id, headed=True)
    ab._page = _GonePage()

    def close():
        closes.append(threading.current_thread().name)
        time.sleep(0.1)

    ab.close = close  # type: ignore[method-assign]
    B._active_browser_sessions[agent_id] = ab
    return ab


def test_a_sweep_and_a_kill_on_one_loop_close_the_session_once():
    closes: list = []
    _gone_session("agent_race", closes)

    async def both():
        return await asyncio.gather(B.sweep_closed_windows(), B.close_browser_sessions_of("agent_race"))

    released, closed = asyncio.run(both())

    assert len(closes) == 1 and released + closed == 1
    assert "agent_race" not in B._active_browser_sessions


def test_a_sweep_and_a_kill_from_its_own_thread_close_the_session_once():
    for attempt in range(20):
        closes: list = []
        _gone_session(f"agent_race_{attempt}", closes)
        outcome: dict = {}

        def kill(agent_id=f"agent_race_{attempt}"):
            outcome["kill"] = asyncio.run(B.close_browser_sessions_of(agent_id))

        thread = threading.Thread(target=kill)
        thread.start()
        released = asyncio.run(B.sweep_closed_windows())
        thread.join(10)

        assert len(closes) == 1, f"attempt {attempt}: closed {len(closes)} times"
        assert released + outcome["kill"] == 1
