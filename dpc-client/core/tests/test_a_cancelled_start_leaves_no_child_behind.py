"""A start that is interrupted stops the child it spawned, and no start spawns over a live one.

The incident, 2026-10-22: an agent killed mid-call cancelled its call while the
Strata launcher it had just spawned on :64276 was still loading. Cancellation is
a BaseException, the start cleaned up only on Exception, so the launcher stayed
alive with no /props and nobody holding it. The next agent's start saw a live
child without /props, which matched neither "running" nor "dead", and spawned a
second launcher on the same port; that one died at bind, its death went
unobserved, and an hour later a peer's start was refused as an engine death
"3563 s before the death". The first launcher was left an orphan on the port.

No engine runs here: the process, /health, /props, /metrics and the smoke are
faked at the supervisor's own seams, as in `test_strata_supervisor.py`.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import LlamaServerError, LlamaServerSupervisor
from tests.test_strata_supervisor import (
    _FakeProc,
    _install,
    _started_through_the_real_launch,
    _sup,
)

STOP_SIGNAL = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT


@pytest.fixture
def home(tmp_path, monkeypatch):
    dpc = tmp_path / "dpc-home"
    dpc.mkdir()
    monkeypatch.setattr(base_mod, "DPC_HOME", dpc)
    return dpc


def _strata(tmp_path, monkeypatch, healthy):
    """A Strata supervisor whose /health answers `healthy[0]`; returns it with
    the list of spawned procs and, per spawn, the procs still alive at that moment."""
    sup = _sup(_install(tmp_path), start_timeout_s=30.0)
    spawned = _started_through_the_real_launch(
        sup, monkeypatch, metrics={"engine": {"version": "0.1.45"}})
    alive_at_spawn = []
    real_spawn = sup._spawn

    async def spawn(cmd, env):
        alive_at_spawn.append([p for p in spawned if p.returncode is None])
        return await real_spawn(cmd, env)

    async def health():
        return healthy[0]

    sup._spawn, sup._health_ok = spawn, health
    return sup, spawned, alive_at_spawn


async def _until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def _start_and_cancel(sup, spawned):
    """Begin a start, cancel it once its child is spawned, as an agent kill does."""
    n = len(spawned)
    task = asyncio.ensure_future(sup.ensure_running())
    await _until(lambda: len(spawned) > n)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestACancelledStrataStart:
    @pytest.mark.asyncio
    async def test_a_start_cancelled_while_loading_stops_its_launcher(self, tmp_path, home, monkeypatch):
        sup, spawned, _ = _strata(tmp_path, monkeypatch, healthy=[False])
        await _start_and_cancel(sup, spawned)
        assert ("signal", STOP_SIGNAL) in spawned[0].calls, "the spawned launcher was stopped"
        assert sup._proc is None and sup.props is None
        assert sup.failure is None, "a cancelled start is not an engine death"

    @pytest.mark.asyncio
    async def test_a_start_cancelled_during_the_smoke_stops_its_launcher(self, tmp_path, home, monkeypatch):
        sup, spawned, _ = _strata(tmp_path, monkeypatch, healthy=[True])
        in_smoke = asyncio.Event()

        async def slow_post(path, body, timeout):
            in_smoke.set()
            await asyncio.sleep(60)

        sup._post = slow_post
        task = asyncio.ensure_future(sup.ensure_running())
        await asyncio.wait_for(in_smoke.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ("signal", STOP_SIGNAL) in spawned[0].calls
        assert sup._proc is None and sup.props is None, "an unsmoked launcher is not left 'running'"

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_start(self, tmp_path, home, monkeypatch):
        sup, spawned, _ = _strata(tmp_path, monkeypatch, healthy=[True])
        results = await asyncio.gather(*(sup.ensure_running() for _ in range(3)))
        assert len(spawned) == 1
        assert all(r == results[0] for r in results)
        await sup.stop()

    @pytest.mark.asyncio
    async def test_two_cancelled_starts_then_a_peer_start_succeeds(self, tmp_path, home, monkeypatch):
        """The incident of 2026-10-22, in order: Johnny's start cancelled,
        Warren's start cancelled, then a peer's request starts the launcher."""
        healthy = [False]
        sup, spawned, alive_at_spawn = _strata(tmp_path, monkeypatch, healthy)
        await _start_and_cancel(sup, spawned)
        await _start_and_cancel(sup, spawned)
        healthy[0] = True
        assert sup.start_for_peer("peer-1") is True
        await asyncio.wait_for(sup.peer_start_task, 5)

        assert sup.failure is None, "no earlier exit is charged to the peer's start"
        assert len(spawned) == 3 and sup._proc is spawned[2] and sup.props
        assert alive_at_spawn == [[], [], []], "no launcher spawned while another was alive"
        assert all(("signal", STOP_SIGNAL) in p.calls for p in spawned[:2])
        await sup.stop()

    @pytest.mark.asyncio
    async def test_the_state_the_incident_left_does_not_fail_the_next_start(self, tmp_path, home, monkeypatch):
        """What the 10:16 peer start met: `_proc` is a launcher that died inside
        its own start, never having answered /props."""
        sup, spawned, _ = _strata(tmp_path, monkeypatch, healthy=[True])
        sup._proc = _FakeProc(returncode=1)
        assert await sup.ensure_running()
        assert sup.failure is None and sup._proc is spawned[0]
        await sup.stop()

    @pytest.mark.asyncio
    async def test_a_live_launcher_without_props_is_stopped_before_a_new_start(
        self, tmp_path, home, monkeypatch,
    ):
        sup, spawned, alive_at_spawn = _strata(tmp_path, monkeypatch, healthy=[True])
        leftover = _FakeProc()
        sup._proc = leftover
        assert await sup.ensure_running()
        assert ("signal", STOP_SIGNAL) in leftover.calls
        assert leftover.returncode is not None
        assert len(spawned) == 1 and sup._proc is spawned[0]
        await sup.stop()

    @pytest.mark.asyncio
    async def test_a_second_cancellation_does_not_cut_the_stop_and_the_next_start_waits(
        self, tmp_path, home, monkeypatch,
    ):
        healthy = [False]
        sup, spawned, alive_at_spawn = _strata(tmp_path, monkeypatch, healthy)
        release = asyncio.Event()

        class _SlowToExit(_FakeProc):
            async def wait(self):
                await release.wait()
                self.returncode = 0
                return 0

        first = _SlowToExit()
        real_spawn = sup._spawn

        async def spawn(cmd, env):
            proc = await real_spawn(cmd, env)
            if not spawned[:-1]:
                spawned[-1] = first
                return first
            return proc

        sup._spawn = spawn
        task = asyncio.ensure_future(sup.ensure_running())
        await _until(lambda: len(spawned) == 1)
        task.cancel()
        await _until(lambda: ("signal", STOP_SIGNAL) in first.calls)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert first.returncode is None, "the launcher is still exiting"

        healthy[0] = True
        nxt = asyncio.ensure_future(sup.ensure_running())
        await asyncio.sleep(0.2)
        assert len(spawned) == 1, "no second launcher while the first still holds the port"
        release.set()
        assert await asyncio.wait_for(nxt, 5)
        assert len(spawned) == 2 and first.returncode == 0
        await sup.stop()

    @pytest.mark.asyncio
    async def test_a_real_death_after_a_healthy_start_still_fails_closed(self, tmp_path, home, monkeypatch):
        sup, spawned, _ = _strata(tmp_path, monkeypatch, healthy=[True])
        del sup._start_watch  # the real watcher, which the shared helper switches off
        assert await sup.ensure_running()
        spawned[0].returncode = 3221226505
        await asyncio.wait_for(sup._watch_task, 5)
        assert sup.failure is not None and "3221226505" in sup.failure["reason"]
        with pytest.raises(LlamaServerError, match="will not start it again"):
            await sup.ensure_running()
        assert len(spawned) == 1
        sup.reset_failure()
        assert await sup.ensure_running()
        assert len(spawned) == 2
        await sup.stop()

    @pytest.mark.asyncio
    async def test_a_started_launcher_found_dead_by_a_call_still_fails_closed(
        self, tmp_path, home, monkeypatch,
    ):
        sup, spawned, _ = _strata(tmp_path, monkeypatch, healthy=[True])
        assert await sup.ensure_running()
        spawned[0].returncode = 1
        with pytest.raises(LlamaServerError, match="will not start it again"):
            await sup.ensure_running()
        assert len(spawned) == 1


class TestACancelledLlamaServerStart:
    def _sup(self, tmp_path, monkeypatch, healthy):
        monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path)
        monkeypatch.setattr(base_mod, "ensure_binary", lambda config: Path("C:/bin/llama-server.exe"))
        monkeypatch.setattr(base_mod, "_free_vram_mib", lambda: None)
        monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)
        sup = LlamaServerSupervisor("local_qwen38", {"gguf_path": "D:/m.gguf", "start_timeout_s": 30.0})
        spawned, alive_at_spawn = [], []

        async def spawn(cmd, env):
            alive_at_spawn.append([p for p in spawned if p.returncode is None])
            proc = _FakeProc()
            spawned.append(proc)
            return proc

        async def health():
            return healthy[0]

        async def get(path):
            return {"total_slots": 1}

        sup._spawn, sup._health_ok, sup._get = spawn, health, get
        return sup, spawned, alive_at_spawn

    @pytest.mark.asyncio
    async def test_a_start_cancelled_while_loading_stops_its_child(self, tmp_path, monkeypatch):
        sup, spawned, _ = self._sup(tmp_path, monkeypatch, healthy=[False])
        await _start_and_cancel(sup, spawned)
        assert ("signal", STOP_SIGNAL) in spawned[0].calls
        assert sup._proc is None

    @pytest.mark.asyncio
    async def test_a_cancelled_start_then_a_new_one_spawns_over_nothing(self, tmp_path, monkeypatch):
        healthy = [False]
        sup, spawned, alive_at_spawn = self._sup(tmp_path, monkeypatch, healthy)
        await _start_and_cancel(sup, spawned)
        healthy[0] = True
        assert await sup.ensure_running() == {"total_slots": 1}
        assert alive_at_spawn == [[], []]
        await sup.stop()

    @pytest.mark.asyncio
    async def test_a_live_child_without_props_is_stopped_before_a_new_start(self, tmp_path, monkeypatch):
        sup, spawned, alive_at_spawn = self._sup(tmp_path, monkeypatch, healthy=[True])
        leftover = _FakeProc()
        sup._proc = leftover
        assert await sup.ensure_running()
        assert ("signal", STOP_SIGNAL) in leftover.calls
        assert alive_at_spawn == [[]]
        await sup.stop()

    @pytest.mark.asyncio
    async def test_launch_refuses_to_spawn_over_a_live_child(self, tmp_path, monkeypatch):
        sup, spawned, _ = self._sup(tmp_path, monkeypatch, healthy=[True])
        live = _FakeProc()
        sup._proc, sup.port = live, 5000
        with pytest.raises(LlamaServerError, match="still running on :5000"):
            await sup._launch(Path("C:/bin/llama-server.exe"))
        assert spawned == [] and sup._proc is live
