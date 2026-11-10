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
    assert result.startswith("Not run: ") and TURN_KILLED_NOT_STARTED in result
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
