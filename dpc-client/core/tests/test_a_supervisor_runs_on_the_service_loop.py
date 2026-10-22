"""A local engine started from an agent tool lives on the service loop, not on the tool's.

`ToolRegistry.execute` runs an async tool on a loop of its own, in a thread, and
closes that loop when the call returns. describe_image and read_document reach
`llm_manager.query` from there, and a local provider's first call starts its
engine. The child, the Strata watcher, the start lock and the shielded stops were
then bound to a loop that no longer ran; at the next shutdown (2026-10-21 21:59)
the log had "Task was destroyed but it is pending ... _watch_engine()", "could
not stop the child ... attached to a different loop" and "Event loop is closed".

The service binds its loop (`bind_owner_loop`); a supervisor call arriving on
another running loop is run there. No engine runs here: the process and the HTTP
seams are faked as in `test_strata_supervisor.py`, and the fake process is bound
to the loop it was spawned on, as an asyncio subprocess is.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import LlamaServerSupervisor, bind_owner_loop
from dpc_client_core.managers.strata_supervisor import PeerStartRefused, StrataSupervisor, serving_a_peer
from dpc_client_core.providers import llamacpp_server_provider as provider_mod
from tests.test_strata_supervisor import _FakeProc, _install, _started_through_the_real_launch, _sup

STOP_SIGNAL = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT

# What the 21:59 shutdown logged, and what a lock shared by two loops raises.
LOOP_ERRORS = (
    "Task was destroyed but it is pending",
    "attached to a different loop",
    "Event loop is closed",
    "bound to a different event loop",
    "could not stop",
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    dpc = tmp_path / "dpc-home"
    dpc.mkdir()
    monkeypatch.setattr(base_mod, "DPC_HOME", dpc)
    return dpc


@pytest.fixture(autouse=True)
def unbound_after():
    yield
    bind_owner_loop(None)


@pytest.fixture
def registry(monkeypatch):
    """Empty supervisor registries, so the shutdown sweep sees only this test's."""
    active = {}
    monkeypatch.setattr(provider_mod, "_ACTIVE_SUPERVISORS", active)
    monkeypatch.setattr(provider_mod, "_RETIRING", {})
    return active


class _LoopBoundProc(_FakeProc):
    """A child bound to the loop that spawned it, as an asyncio subprocess is."""

    def __init__(self):
        super().__init__()
        self.loop = asyncio.get_running_loop()

    def send_signal(self, sig):
        if self.loop.is_closed():
            raise RuntimeError("Event loop is closed")
        super().send_signal(sig)

    async def wait(self):
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError(f"{self!r} is attached to a different loop")
        return await super().wait()


class _PerCallLoop:
    """One async tool call exactly as `ToolRegistry.execute` runs it: a thread,
    `new_event_loop`, `run_until_complete`, `shutdown_asyncgens`, `close`."""

    def __init__(self, make_coro):
        self.result = None
        self.error = None
        self.loop = None
        self.done = threading.Event()
        threading.Thread(target=self._run, args=(make_coro,), daemon=True).start()

    def _run(self, make_coro):
        loop = self.loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            self.result = loop.run_until_complete(make_coro())
        except BaseException as e:  # CancelledError included: the test reads it
            self.error = e
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            asyncio.set_event_loop(None)
            self.done.set()

    async def join(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while not self.done.is_set():
            if time.monotonic() > deadline:
                raise AssertionError("the per-call loop did not finish")
            await asyncio.sleep(0.01)
        return self.result


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def _no_supervisor_task_left():
    """Wait for the owner-side tasks a forwarded call left running. A cancelled
    caller returns before its owner-side task has finished its cleanup; a test
    that ends first closes the loop under that task, and its "Task was destroyed
    but it is pending" lands in whichever test the garbage collector runs in."""
    def pending():
        here = asyncio.current_task()
        return [t for t in asyncio.all_tasks()
                if t is not here and "Supervisor." in repr(t.get_coro())]
    await _until(lambda: not pending())


def _strata(tmp_path, monkeypatch, healthy=None):
    """A Strata supervisor through the real `_launch`es, with the real watcher."""
    sup = _sup(_install(tmp_path), start_timeout_s=30.0)
    _started_through_the_real_launch(sup, monkeypatch, metrics={"engine": {"version": "0.1.45"}})
    del sup._start_watch  # the watcher is the task that outlived its loop
    spawned = []

    async def spawn(cmd, env):
        proc = _LoopBoundProc()
        spawned.append(proc)
        return proc

    sup._spawn = spawn
    if healthy is not None:
        async def health():
            return healthy[0]

        sup._health_ok = health
    return sup, spawned


def _llama(monkeypatch):
    monkeypatch.setattr(base_mod, "ensure_binary", lambda config: Path("C:/bin/llama-server.exe"))
    monkeypatch.setattr(base_mod, "_free_vram_mib", lambda: None)
    monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)
    sup = LlamaServerSupervisor("local_qwen38", {"gguf_path": "D:/m.gguf", "start_timeout_s": 30.0})
    spawned = []

    async def spawn(cmd, env):
        proc = _LoopBoundProc()
        spawned.append(proc)
        return proc

    async def health():
        return True

    async def get(path):
        return {"total_slots": 1}

    sup._spawn, sup._health_ok, sup._get = spawn, health, get
    sup._read_fit_memo = lambda: {}
    sup._write_fit_memo = lambda *a: None
    return sup, spawned


def _loop_errors(caplog):
    return [r.getMessage() for r in caplog.records if any(e in r.getMessage() for e in LOOP_ERRORS)]


class TestAStartFromAToolCall:
    @pytest.mark.asyncio
    async def test_the_strata_launcher_and_its_watcher_outlive_the_tool_loop(
        self, tmp_path, home, monkeypatch, registry, caplog,
    ):
        owner = asyncio.get_running_loop()
        bind_owner_loop(owner)
        sup, spawned = _strata(tmp_path, monkeypatch)
        registry[sup.alias] = sup

        call = _PerCallLoop(lambda: sup.ensure_running())
        await call.join()
        assert call.error is None and call.result, call.error
        assert call.loop.is_closed()
        assert [p.loop for p in spawned] == [owner], "the launcher was spawned on the service loop"
        watcher = sup._watch_task
        assert watcher is not None and watcher.get_loop() is owner
        await asyncio.sleep(0.05)
        assert not watcher.done(), "the watcher still polls after the tool's loop closed"
        assert getattr(sup._start_lock, "_loop", None) in (None, owner)

        with caplog.at_level(logging.WARNING):
            stopped = await provider_mod.stop_all_supervisors()
            gc.collect()
        assert stopped == [sup.alias]
        assert ("signal", STOP_SIGNAL) in spawned[0].calls and spawned[0].returncode == 0
        assert watcher.cancelled()
        assert sup._proc is None
        assert _loop_errors(caplog) == []

    @pytest.mark.asyncio
    async def test_a_llama_server_child_is_spawned_on_the_service_loop(self, home, monkeypatch, registry, caplog):
        owner = asyncio.get_running_loop()
        bind_owner_loop(owner)
        sup, spawned = _llama(monkeypatch)
        registry[sup.alias] = sup

        call = _PerCallLoop(lambda: sup.ensure_running())
        await call.join()
        assert call.error is None, call.error
        assert [p.loop for p in spawned] == [owner]
        with caplog.at_level(logging.WARNING):
            assert await provider_mod.stop_all_supervisors() == [sup.alias]
        assert ("signal", STOP_SIGNAL) in spawned[0].calls
        assert _loop_errors(caplog) == []

    @pytest.mark.asyncio
    async def test_cancelling_the_tool_call_cancels_the_start_and_its_cleanup_runs(
        self, tmp_path, home, monkeypatch,
    ):
        owner = asyncio.get_running_loop()
        bind_owner_loop(owner)
        sup, spawned = _strata(tmp_path, monkeypatch, healthy=[False])

        async def start_then_cancel():
            task = asyncio.ensure_future(sup.ensure_running())
            deadline = time.monotonic() + 5
            while not spawned and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            task.cancel()
            await task

        call = _PerCallLoop(start_then_cancel)
        await call.join()
        assert isinstance(call.error, asyncio.CancelledError)
        assert [p.loop for p in spawned] == [owner]
        await _until(lambda: ("signal", STOP_SIGNAL) in spawned[0].calls and sup._proc is None)
        await _until(lambda: sup._abandoned_stop is None or sup._abandoned_stop.done())
        await _no_supervisor_task_left()
        assert spawned[0].returncode == 0, "the launcher was stopped on the loop it lives on"
        assert sup.props is None and sup.failure is None

    @pytest.mark.asyncio
    async def test_two_tool_calls_on_two_loops_share_one_start(self, tmp_path, home, monkeypatch, caplog):
        owner = asyncio.get_running_loop()
        bind_owner_loop(owner)
        healthy = [False]
        sup, spawned = _strata(tmp_path, monkeypatch, healthy=healthy)

        with caplog.at_level(logging.WARNING):
            first = _PerCallLoop(lambda: sup.ensure_running())
            await _until(lambda: len(spawned) == 1)
            second = _PerCallLoop(lambda: sup.ensure_running())
            await _until(lambda: len(sup._start_lock._waiters or ()) == 1)
            healthy[0] = True
            await first.join()
            await second.join()
        assert first.error is None and second.error is None, (first.error, second.error)
        assert len(spawned) == 1 and spawned[0].loop is owner
        assert first.result == second.result
        assert _loop_errors(caplog) == []
        await sup.stop()
        await _no_supervisor_task_left()

    @pytest.mark.asyncio
    async def test_a_peer_mark_travels_to_the_service_loop(self, tmp_path, home, monkeypatch):
        bind_owner_loop(asyncio.get_running_loop())
        sup, spawned = _strata(tmp_path, monkeypatch)

        async def as_a_peer():
            with serving_a_peer("dpc-node-peer"):
                return await sup.ensure_running()

        call = _PerCallLoop(as_a_peer)
        await call.join()
        assert isinstance(call.error, PeerStartRefused)
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_peer_start_begun_from_a_foreign_loop_runs_on_the_service_loop(
        self, tmp_path, home, monkeypatch,
    ):
        owner = asyncio.get_running_loop()
        bind_owner_loop(owner)
        sup, spawned = _strata(tmp_path, monkeypatch)

        async def begin():
            return sup.start_for_peer("dpc-node-peer")

        call = _PerCallLoop(begin)
        assert await call.join() is True
        await _until(lambda: sup.peer_start_task.done())
        assert [p.loop for p in spawned] == [owner] and sup.props
        assert not sup._watch_task.done()
        await sup.stop()
        await _no_supervisor_task_left()


class _GatedProc(_LoopBoundProc):
    """A child that exits only once `gate` is set: a stop in progress."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()

    async def wait(self):
        if self.returncode is None:
            await self.gate.wait()
        return await super().wait()


class TestAStopHelperCalledFromAToolCall:
    """`_stop_shielded` and `_await_abandoned_stop` make and wait on loop-bound
    tasks; called from a per-call loop they forward themselves, whoever calls."""

    @pytest.mark.asyncio
    async def test_a_cancelled_shielded_stop_from_a_tool_loop_finishes_on_the_service_loop(
        self, home, monkeypatch, caplog,
    ):
        owner = asyncio.get_running_loop()
        bind_owner_loop(owner)
        sup, spawned = _llama(monkeypatch)

        async def spawn(cmd, env):
            proc = _GatedProc()
            spawned.append(proc)
            return proc

        sup._spawn = spawn
        await sup.ensure_running()
        child = spawned[0]

        async def stop_then_cancel():
            task = asyncio.ensure_future(sup._stop_shielded())
            deadline = time.monotonic() + 5
            while ("signal", STOP_SIGNAL) not in child.calls and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            task.cancel()
            await task

        with caplog.at_level(logging.WARNING):
            call = _PerCallLoop(stop_then_cancel)
            await call.join()
            assert isinstance(call.error, asyncio.CancelledError)
            assert call.loop.is_closed()
            stop = sup._abandoned_stop
            assert stop is not None and stop.get_loop() is owner, "the shielded stop lives on the service loop"
            assert not stop.done(), "the cancel left the stop running"

            waiter = _PerCallLoop(lambda: sup._await_abandoned_stop())
            await asyncio.sleep(0.05)
            child.gate.set()
            await waiter.join(timeout=5.0)
            assert waiter.error is None, waiter.error
            await _until(lambda: sup._proc is None)
            await _no_supervisor_task_left()
            gc.collect()
        assert child.returncode == 0 and sup._abandoned_stop is None
        assert _loop_errors(caplog) == []


class TestTheSameLoopAndNoOwner:
    @pytest.mark.asyncio
    async def test_a_call_on_the_service_loop_runs_inline(self, tmp_path, home, monkeypatch):
        bind_owner_loop(asyncio.get_running_loop())
        sup, spawned = _strata(tmp_path, monkeypatch)
        caller = asyncio.current_task()
        tasks_at_spawn = []
        real_spawn = sup._spawn

        async def spawn(cmd, env):
            tasks_at_spawn.append(asyncio.current_task())
            return await real_spawn(cmd, env)

        sup._spawn = spawn
        await sup.ensure_running()
        assert tasks_at_spawn == [caller], "no forwarding task on the owner loop itself"
        await sup.stop()

    @pytest.mark.asyncio
    async def test_without_an_owner_a_tool_call_keeps_its_own_loop(self, tmp_path, home, monkeypatch):
        sup, spawned = _strata(tmp_path, monkeypatch)
        sup._start_watch = lambda: None  # its loop closes: nothing may be left on it

        call = _PerCallLoop(lambda: sup.ensure_running())
        await call.join()
        assert call.error is None, call.error
        assert spawned[0].loop is call.loop, "unbound, the caller's loop is used, as before"

    @pytest.mark.asyncio
    async def test_an_owner_that_no_longer_runs_is_not_waited_on(self, tmp_path, home, monkeypatch):
        dead = asyncio.new_event_loop()
        dead.close()
        bind_owner_loop(dead)
        sup, spawned = _strata(tmp_path, monkeypatch)
        await sup.ensure_running()
        assert spawned[0].loop is asyncio.get_running_loop()
        await sup.stop()


def test_every_lifecycle_coroutine_runs_on_the_owner_loop():
    """A public coroutine added to a supervisor without the decorator is the
    defect this file is about, waiting for its first tool call."""
    import inspect

    missing = []
    for cls in (LlamaServerSupervisor, StrataSupervisor):
        for name, fn in vars(cls).items():
            if inspect.iscoroutinefunction(fn) and not name.startswith("_"):
                if not getattr(fn, "runs_on_the_owner_loop", False):
                    missing.append(f"{cls.__name__}.{name}")
    for fn in (
        StrataSupervisor._fail_closed, LlamaServerSupervisor._stop_shielded,
        LlamaServerSupervisor._await_abandoned_stop,
        provider_mod.stop_all_supervisors, provider_mod.stop_local_engine,
    ):
        if not getattr(fn, "runs_on_the_owner_loop", False):
            missing.append(fn.__qualname__)
    assert missing == []


def test_the_service_binds_its_own_loop_at_start():
    """Without the bind every call takes the caller's loop and the decorator is inert.
    Read from the source: starting a CoreService here would start the whole node."""
    import inspect

    from dpc_client_core.service import CoreService

    assert "bind_owner_loop(asyncio.get_running_loop())" in inspect.getsource(CoreService.start)


@pytest.mark.asyncio
async def test_the_service_unbinds_its_loop_once_the_engines_are_stopped(monkeypatch):
    """After the supervisors stop, a late call from a tool's loop runs where it is
    made rather than being forwarded into a loop that is about to close. Driven
    through the real `CoreService.shutdown` with every component stubbed."""
    from unittest.mock import AsyncMock, Mock

    from dpc_client_core.dpc_agent import index_writer
    from dpc_client_core.service import CoreService

    monkeypatch.setattr(index_writer, "begin_shutdown", lambda: None)
    owner = asyncio.get_running_loop()
    bind_owner_loop(owner)
    bound_while_stopping = []

    class _LLM:
        providers = {}

        async def shutdown(self):
            bound_while_stopping.append(base_mod._OWNER_LOOP is owner)

    svc = CoreService.__new__(CoreService)
    svc._background_tasks = set()
    svc.conversation_monitors = {}
    svc.llm_manager = _LLM()
    svc.p2p_manager, svc.local_api, svc.hub_client = AsyncMock(), AsyncMock(), AsyncMock()
    svc.file_server = Mock()
    svc.gateway = None
    await svc.shutdown()
    assert bound_while_stopping == [True], "the engines are stopped on the bound loop"
    assert base_mod._OWNER_LOOP is None
