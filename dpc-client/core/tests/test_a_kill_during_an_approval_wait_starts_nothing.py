"""A Kill that lands while a tier-1 command waits for approval starts nothing afterwards.

Observed 2026-11-10 from the code: `run_shell` blocks its executor thread on the
approval's `threading.Event` (up to `APPROVAL_TTL_SECONDS`); the Kill ended what
existed at that moment and did not touch the pending approval, so a yes that came
after the Kill reached `run_supervised` and the command was born in a killed turn.

ADR-046, amendment of 2026-11-10: the Kill records the turn (`note_kill_of_turn`), a
`tool` spawn of a killed turn is refused (`check_open`, and `register` under the same
lock), and the turn's pending approvals are answered as not approved so the wait ends
at once. Nothing here spawns a real process: `subprocess.Popen` is replaced by a
recorder, and the registry is the in-memory one the conftest resets for each test.
"""

from __future__ import annotations

import asyncio
import threading
import time
import types

import pytest

from dpc_client_core import process_registry as pr
from dpc_client_core.dpc_agent import loop as agent_loop
from dpc_client_core.dpc_agent.tools import browser as browser_tool
from dpc_client_core.dpc_agent.tools import process as process_tool
from dpc_client_core.dpc_agent.tools import shell
from dpc_client_core.process_registry import (
    KILLED_TURNS_MAX,
    TURN_KILLED_NOT_STARTED,
    ProcessRegistryClosed,
    SpawnOrigin,
    spawn_origin,
)
from dpc_client_core.service import CoreService

AGENT = "agent_001"
CHAT = "chat-1"
KILLED = "20261110T100000Z-aaaaaaaa"
OTHER = "20261110T100100Z-bbbbbbbb"


def _origin(turn_id: str, tool: str = "run_shell") -> SpawnOrigin:
    return SpawnOrigin(AGENT, CHAT, tool, 1, turn_id=turn_id)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    shell._pending_approvals.clear()
    # A real Kill also closes the agent's browser; here there is none to close.
    async def _no_browser(agent_id):
        return None
    monkeypatch.setattr(browser_tool, "close_browser_sessions_of", _no_browser)
    yield
    shell._pending_approvals.clear()


@pytest.fixture
def born(monkeypatch):
    """Every `Popen` the code under test attempts; the list must stay empty."""
    attempts: list = []

    def _recorder(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("a process was started")

    monkeypatch.setattr(process_tool.subprocess, "Popen", _recorder)
    return attempts


class _Service:
    """Records what the approval card is told; the real service is not needed."""

    def __init__(self):
        self.local_api = types.SimpleNamespace(has_clients=True)
        self.p2p_manager = types.SimpleNamespace(get_display_name=lambda: "Mike")
        self.closed: list = []

    async def announce_shell_approval_request(self, **kwargs):
        pass

    async def announce_shell_approval_closed(self, **kwargs):
        self.closed.append(kwargs)


class _Loop:
    """An event loop on a thread of its own, as the service's is for the tool thread."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def _ctx(tmp_path, service, loop):
    return types.SimpleNamespace(
        agent_root=tmp_path, dpc_service=service,
        reply_telegram_chat_id="", _event_loop=loop, _agent=None,
    )


def _ask_in_a_tool_thread(ctx, origin, out):
    """What the dispatcher does: the call runs on a thread that carries the origin."""

    def _run():
        with spawn_origin(origin):
            started = time.monotonic()
            out["result"] = shell._request_approval(ctx, "rm -rf x", "tier1", "", 10)
            out["waited"] = time.monotonic() - started

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def _wait_for_the_question():
    for _ in range(500):
        if shell._pending_approvals:
            return
        time.sleep(0.01)
    raise AssertionError("the approval was never asked")


def test_a_kill_ends_the_approval_wait_at_once_and_nothing_is_started(tmp_path, born):
    assert shell.APPROVAL_TTL_SECONDS >= 60, "the wait this test must not sit through"
    service = _Service()
    with _Loop() as loop:
        out: dict = {}
        thread = _ask_in_a_tool_thread(_ctx(tmp_path, service, loop), _origin(KILLED), out)
        _wait_for_the_question()
        (pending,) = shell._pending_approvals.values()
        assert pending["turn_id"] == KILLED

        agent_loop._end_killed_turn(_origin(KILLED))
        thread.join(5)
        assert not thread.is_alive(), "the tool thread is still waiting for an answer"
        time.sleep(0.2)  # the closure goes to the service's loop, after the thread's return

    assert out["waited"] < 5
    assert "Not run" in out["result"] and TURN_KILLED_NOT_STARTED in out["result"]
    assert shell._pending_approvals == {}
    assert born == []
    assert pr.get_registry().open_entries("tool") == []
    # The card is withdrawn with a closure that is no person's decision.
    assert [(c["resolution"], c["resolved_via"]) for c in service.closed] == [("superseded", "superseded")]


def test_an_approval_that_comes_after_the_kill_finds_nothing_to_approve(tmp_path, born):
    service = _Service()
    core = CoreService.__new__(CoreService)
    core.p2p_manager = service.p2p_manager
    core.local_api = None
    core._get_agent_telegram_bridge = lambda agent_id: None
    with _Loop() as loop:
        out: dict = {}
        thread = _ask_in_a_tool_thread(_ctx(tmp_path, service, loop), _origin(KILLED), out)
        _wait_for_the_question()
        (request_id,) = list(shell._pending_approvals)

        agent_loop._end_killed_turn(_origin(KILLED))
        thread.join(5)
        late = asyncio.run_coroutine_threadsafe(
            core.shell_approve_command(request_id), loop,
        ).result(5)

    assert late["status"] == "error"
    assert born == []
    assert "Not run" in out["result"]


def test_an_approval_already_given_when_the_kill_lands_still_starts_nothing(born):
    """The yes got in first and the thread has not run yet: the spawn is refused at the
    check before `Popen`, the second line behind the release of the wait."""
    pr.get_registry().note_kill_of_turn(KILLED)
    with spawn_origin(_origin(KILLED)):
        result = shell._execute_shell_command("echo hi", None, 5)
    assert result.startswith("⚠️ Not run: ") and TURN_KILLED_NOT_STARTED in result
    assert born == []
    assert pr.get_registry().open_entries("tool") == []


def test_a_command_asked_after_the_kill_is_not_even_put_to_the_user(tmp_path, born):
    pr.get_registry().note_kill_of_turn(KILLED)
    service = _Service()
    with _Loop() as loop:
        out: dict = {}
        thread = _ask_in_a_tool_thread(_ctx(tmp_path, service, loop), _origin(KILLED), out)
        thread.join(5)
    assert not thread.is_alive()
    assert out["waited"] < 5
    assert TURN_KILLED_NOT_STARTED in out["result"]
    assert shell._pending_approvals == {}
    assert born == []


def test_the_kill_through_the_registry_alone_records_the_turn():
    registry = pr.get_registry()
    assert not registry.turn_was_killed(KILLED)
    registry.stop_commands_of(_origin(KILLED), wait_s=0)
    assert registry.turn_was_killed(KILLED)
    assert not registry.turn_was_killed(OTHER)
    # A Kill that names no turn records nothing and ends nothing.
    registry.stop_commands_of(SpawnOrigin(AGENT, CHAT, "run_shell", 1), wait_s=0)
    assert not registry.turn_was_killed("")


def test_a_spawn_registered_after_the_kill_for_that_turn_is_refused():
    registry = pr.get_registry()
    registry.stop_commands_of(_origin(KILLED), wait_s=0)
    with pytest.raises(ProcessRegistryClosed, match="the turn was killed"):
        registry.register(
            7_992_001, launcher="run_shell", launcher_kind="tool", command="sleep 600",
            origin=_origin(KILLED),
        )
    assert registry.open_entries("tool") == []
    # The spawn site that has no origin of its own reads the thread's.
    with spawn_origin(_origin(KILLED)):
        with pytest.raises(ProcessRegistryClosed):
            registry.check_open("run_shell", "tool")


def test_a_spawn_of_another_turn_or_a_later_one_is_not_refused():
    registry = pr.get_registry()
    registry.stop_commands_of(_origin(KILLED), wait_s=0)
    registry.register(
        7_992_002, launcher="run_shell", launcher_kind="tool", command="sleep 600",
        origin=_origin(OTHER),
    )
    assert [e.turn_id for e in registry.open_entries("tool")] == [OTHER]
    registry.check_open("run_shell", "tool", origin=_origin("20261110T100200Z-cccccccc"))
    # No turn, no record to consult: a call outside any turn is never refused for a Kill.
    registry.check_open("run_shell", "tool", origin=SpawnOrigin(AGENT, CHAT, "run_shell", 1))


def test_a_subsystem_started_inside_the_killed_turn_is_the_backends_and_is_not_refused():
    registry = pr.get_registry()
    registry.stop_commands_of(_origin(KILLED), wait_s=0)
    with spawn_origin(_origin(KILLED, tool="strata")):
        registry.check_open("strata", "subsystem")
        registry.check_open()  # the llama-server supervisor's own form
    registry.register(
        7_992_003, launcher="strata", launcher_kind="subsystem", command="strata",
        origin=_origin(KILLED, tool="strata"),
    )


def test_the_backend_stop_still_refuses_with_its_own_words():
    registry = pr.get_registry()
    registry.begin_shutdown()
    with pytest.raises(ProcessRegistryClosed, match="the backend is stopping"):
        registry.check_open("run_shell", "tool", origin=_origin(OTHER))


def test_the_record_of_killed_turns_is_bounded_and_forgets_the_oldest_first():
    registry = pr.get_registry()
    ids = [f"20261110T1000{n // 100:02d}Z-{n:08x}" for n in range(KILLED_TURNS_MAX + 44)]
    for turn_id in ids:
        registry.note_kill_of_turn(turn_id)
    with registry._lock:
        assert len(registry._killed_turns) == KILLED_TURNS_MAX
    assert not registry.turn_was_killed(ids[0])
    assert registry.turn_was_killed(ids[-1])
    # A turn killed again moves to the newest end instead of being counted twice.
    registry.note_kill_of_turn(ids[-KILLED_TURNS_MAX])
    for turn_id in (f"late-{n}" for n in range(KILLED_TURNS_MAX - 1)):
        registry.note_kill_of_turn(turn_id)
    assert registry.turn_was_killed(ids[-KILLED_TURNS_MAX])


# --- follow-ups of 2026-11-11 after review (ADR-046 amendment) ---------------------

from dpc_client_core.dpc_agent.tools import core as core_tool  # noqa: E402
from dpc_client_core.process_registry import TURN_KILLED_STARTED_AND_STOPPED  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_schedule():
    core_tool._pending_schedule_approvals.clear()
    yield
    core_tool._pending_schedule_approvals.clear()


class _ScheduleService(_Service):
    async def announce_schedule_approval_request(self, **kwargs):
        pass

    async def announce_schedule_approval_closed(self, **kwargs):
        self.closed.append(kwargs)


class _RegistryThatCannotRecord:
    """A registry whose record of a killed turn raises; the rest of a Kill goes on."""

    def note_kill_of_turn(self, turn_id):
        raise RuntimeError("registry broke")

    def stop_commands_of(self, origin):
        return []


def test_a_registry_that_fails_to_record_the_kill_still_releases_the_approval(tmp_path, born, monkeypatch, caplog):
    service = _Service()
    with _Loop() as loop:
        out: dict = {}
        thread = _ask_in_a_tool_thread(_ctx(tmp_path, service, loop), _origin(KILLED), out)
        _wait_for_the_question()
        monkeypatch.setattr(agent_loop, "get_registry", lambda: _RegistryThatCannotRecord())
        with caplog.at_level("ERROR"):
            agent_loop._end_killed_turn(_origin(KILLED))
        thread.join(5)
        assert not thread.is_alive(), "the approval was not released when the registry raised"
        time.sleep(0.2)

    assert out["waited"] < 5
    assert out["result"].startswith("⚠️ Not run: ") and TURN_KILLED_NOT_STARTED in out["result"]
    assert born == []
    messages = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("process registry failed" in m for m in messages)
    assert not any("shell approvals" in m for m in messages)


def test_a_failing_shell_release_does_not_stop_the_schedule_release(monkeypatch, caplog):
    def _broken(turn_id):
        raise RuntimeError("shell release broke")

    monkeypatch.setattr(shell, "release_approvals_of_turn", _broken)
    event = threading.Event()
    core_tool._pending_schedule_approvals["r1"] = {
        "event": event, "approved": False, "decided": False, "turn_id": KILLED, "agent_id": AGENT,
    }
    with caplog.at_level("ERROR"):
        agent_loop._end_killed_turn(_origin(KILLED))
    assert event.is_set()
    assert any("shell approvals" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_a_child_born_in_the_register_window_is_reported_as_started_and_stopped(monkeypatch):
    """The Kill lands after the check before `Popen` and before `register`: the child
    existed, so the result must not say it was not started."""
    registry = pr.get_registry()
    stopped: list = []

    def _popen(*args, **kwargs):
        registry.note_kill_of_turn(KILLED)  # the Kill, between the check and register
        return types.SimpleNamespace(pid=7_993_001)

    monkeypatch.setattr(process_tool.subprocess, "Popen", _popen)
    monkeypatch.setattr(process_tool, "_new_command_job", lambda: None)
    monkeypatch.setattr(process_tool, "_kill_process_tree", lambda p: stopped.append(p.pid) or "killed")

    with spawn_origin(_origin(KILLED)):
        result = shell._execute_shell_command("echo hi", None, 5)

    assert stopped == [7_993_001]
    assert result.startswith("⚠️ " + TURN_KILLED_STARTED_AND_STOPPED)
    assert "not started" not in result and "Not run" not in result
    assert "`echo hi`" in result
    assert registry.open_entries("tool") == []


def test_the_refusal_before_the_spawn_still_says_not_started_with_the_mark(born):
    pr.get_registry().note_kill_of_turn(KILLED)
    with spawn_origin(_origin(KILLED)):
        result = shell._execute_shell_command("echo hi", None, 5)
    assert result.startswith("⚠️ Not run: ")
    assert "the command was not started" in result
    assert TURN_KILLED_STARTED_AND_STOPPED not in result


def test_the_backend_stopping_refusal_carries_the_mark_too(born):
    pr.get_registry().begin_shutdown()
    with spawn_origin(_origin(OTHER)):
        result = shell._execute_shell_command("echo hi", None, 5)
    assert result.startswith("⚠️ Not run: the backend is stopping")
    assert born == []


def _classified(tmp_path, text):
    tools = types.SimpleNamespace(execute=lambda name, args, ctx=None: text)
    tc = {"id": "call-1", "function": {"name": "run_shell", "arguments": "{}"}}
    return agent_loop._execute_single_tool(tools, tc, tmp_path)


def test_every_kill_refusal_result_is_an_error_in_the_loops_classifier(tmp_path, born):
    texts = []
    pr.get_registry().note_kill_of_turn(KILLED)
    with spawn_origin(_origin(KILLED)):
        texts.append(shell._execute_shell_command("echo hi", None, 5))  # not started
    texts.append(shell._refusal_before_the_child(TURN_KILLED_STARTED_AND_STOPPED, "echo hi"))
    texts.append(shell._refusal_before_the_child("the backend is stopping; it starts no new process", "echo hi"))
    entry = {"turn_id": KILLED, "command": "echo hi", "event": threading.Event()}
    shell._pending_approvals["x1"] = entry
    shell.release_approvals_of_turn(KILLED)
    texts.append(entry["result"])  # the release of a waiting approval
    assert len(texts) == 4
    for text in texts:
        assert _classified(tmp_path, text)["is_error"] is True, text


def test_an_approved_command_refused_at_the_spawn_keeps_the_mark_first(tmp_path, monkeypatch):
    """The note of who approved sits after the refusal, so the result still starts with the mark."""
    monkeypatch.setattr(
        shell, "_execute_shell_command",
        lambda *a, **k: shell._refusal_before_the_child(TURN_KILLED_NOT_STARTED, "rm -rf x"),
    )
    service = _Service()
    with _Loop() as loop:
        out: dict = {}
        thread = _ask_in_a_tool_thread(_ctx(tmp_path, service, loop), _origin(OTHER), out)
        _wait_for_the_question()
        (entry,) = shell._pending_approvals.values()
        entry["decision"] = "approved"  # what the service writes for a person's yes
        entry["note"] = "Approved by Mike in the interface."
        entry["event"].set()
        thread.join(5)
        assert not thread.is_alive()
    assert out["result"].startswith("⚠️ Not run: ")
    assert out["result"].endswith("Approved by Mike in the interface.")


def test_a_schedule_approval_is_released_by_a_kill_and_withdrawn_as_superseded(tmp_path):
    assert core_tool._SCHEDULE_APPROVAL_TTL_SECONDS >= 30
    service = _ScheduleService()
    out: dict = {}
    with _Loop() as loop:
        ctx = _ctx(tmp_path / AGENT, service, loop)

        def _run():
            with spawn_origin(_origin(KILLED, tool="schedule_task")):
                started = time.monotonic()
                out["result"] = core_tool._await_schedule_approval(
                    ctx, task_type="check_back", when="in 5s", about="x",
                )
                out["waited"] = time.monotonic() - started

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        for _ in range(500):
            if core_tool._pending_schedule_approvals:
                break
            time.sleep(0.01)
        (request_id,) = list(core_tool._pending_schedule_approvals)
        assert core_tool._pending_schedule_approvals[request_id]["turn_id"] == KILLED

        agent_loop._end_killed_turn(_origin(KILLED))
        thread.join(5)
        assert not thread.is_alive(), "the schedule request kept waiting after the Kill"
        time.sleep(0.2)
        assert core_tool.resolve_schedule_approval(request_id, True) is False  # a late yes finds nothing

    assert out["waited"] < 5
    assert out["result"] == (False, "the turn was killed")
    assert core_tool._pending_schedule_approvals == {}
    assert [c["resolution"] for c in service.closed] == ["superseded"]


def test_a_schedule_request_of_a_turn_already_killed_is_not_put_to_the_user(tmp_path):
    pr.get_registry().note_kill_of_turn(KILLED)
    service = _ScheduleService()
    with _Loop() as loop:
        with spawn_origin(_origin(KILLED, tool="schedule_task")):
            result = core_tool._await_schedule_approval(
                _ctx(tmp_path / AGENT, service, loop), task_type="check_back", when="in 5s", about="x",
            )
    assert result == (False, "the turn was killed")
    assert core_tool._pending_schedule_approvals == {}


def test_a_decision_a_person_already_wrote_is_not_overwritten_by_the_release():
    for decision in ("approved", "rejected"):
        event = threading.Event()
        entry = {"turn_id": KILLED, "command": "x", "event": event, "decision": decision, "result": "theirs"}
        shell._pending_approvals["p1"] = entry
        assert shell.release_approvals_of_turn(KILLED) == 0
        assert entry["decision"] == decision and entry["result"] == "theirs"
        assert "turn_killed" not in entry and not event.is_set()
        shell._pending_approvals.clear()

    sched = {"event": threading.Event(), "approved": True, "decided": True, "turn_id": KILLED}
    core_tool._pending_schedule_approvals["s1"] = sched
    assert core_tool.release_schedule_approvals_of_turn(KILLED) == 0
    assert sched["approved"] is True and "turn_killed" not in sched


def test_a_schedule_answer_given_first_is_not_overwritten_and_one_given_after_is_refused():
    event = threading.Event()
    entry = {"event": event, "approved": False, "decided": False, "turn_id": KILLED}
    core_tool._pending_schedule_approvals["s2"] = entry
    assert core_tool.resolve_schedule_approval("s2", True) is True
    assert core_tool.release_schedule_approvals_of_turn(KILLED) == 0
    assert entry["approved"] is True and not entry.get("turn_killed")


def test_the_release_waits_for_the_decision_lock_so_check_and_set_are_one_step():
    event = threading.Event()
    entry = {"turn_id": KILLED, "command": "x", "event": event}
    shell._pending_approvals["p2"] = entry
    done = threading.Event()
    with shell._approval_decision_lock:
        thread = threading.Thread(
            target=lambda: (shell.release_approvals_of_turn(KILLED), done.set()), daemon=True,
        )
        thread.start()
        assert not done.wait(0.3), "the release wrote without taking the lock"
        assert "decision" not in entry
    assert done.wait(5)
    assert entry["decision"] == "rejected" and entry["turn_killed"] is True


def test_a_yes_that_raced_the_release_into_the_entry_starts_nothing(tmp_path, born):
    """The service writes `decision` without the shell's lock. If its write lands after the
    release, the woken thread reads `turn_killed` first."""
    service = _Service()
    with _Loop() as loop:
        out: dict = {}
        thread = _ask_in_a_tool_thread(_ctx(tmp_path, service, loop), _origin(KILLED), out)
        _wait_for_the_question()
        (entry,) = shell._pending_approvals.values()
        agent_loop._end_killed_turn(_origin(KILLED))
        entry["decision"] = "approved"  # what the service writes for a person's yes, late
        thread.join(5)
        assert not thread.is_alive()
    assert out["result"].startswith("⚠️ Not run: ")
    assert born == []
