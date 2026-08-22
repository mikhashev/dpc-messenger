"""A llama-server child outlives the provider that started it when an alias is renamed or dropped,
so the supervisor registry — not llm_manager.providers — has to be what shutdown and reload consult."""

import asyncio
import json
from pathlib import Path

import pytest

from dpc_client_core.providers import llamacpp_server_provider as lsp


class FakeProc:
    """Only the two attributes the sweep reads, with the real meanings.

    `returncode is None` is a live process; a number is one that has exited.
    """

    def __init__(self, pid: int = 4242, returncode=None):
        self.pid = pid
        self.returncode = returncode


class FakeSupervisor:
    def __init__(self, alias: str, started: bool = True, raises: bool = False,
                 loading: bool = False, slow_drain: bool = False):
        self.alias = alias
        # Two independent facts, and conflating them is the defect this file
        # grew a case for: `props` is the health payload, which arrives only
        # after the child answers /props, while `_proc` is the process. A child
        # still loading a 27 GB model has a live `_proc` and no `props` at all —
        # `loading=True` is exactly that state, and the sweep must still reach it.
        self.props = None if (loading or not started) else {"vision": False}
        self._proc = FakeProc() if (started or loading) else None
        self.raises = raises
        self.slow_drain = slow_drain
        self.stopped = False
        self.drained = False

    async def stop(self):
        if self.raises:
            raise OSError("the child is already gone")
        self.stopped = True

    async def drain(self, timeout=None):
        # Faithful to the real one: refuse new work, let the in-flight calls
        # finish, then stop. This double had only `stop`, which is why a change
        # of the call site broke it — a stub shaped for yesterday's caller
        # cannot notice that today's caller is different.
        self.drained = True
        if self.slow_drain:
            # The real drain waits as long as the generation takes, which is the
            # whole window this file is about: between the rename and the end of
            # that wait the child is held by the drain task alone.
            await asyncio.sleep(30)
        await self.stop()


@pytest.fixture(autouse=True)
def clean_registry():
    lsp._ACTIVE_SUPERVISORS.clear()
    lsp._RETIRING.clear()
    yield
    lsp._ACTIVE_SUPERVISORS.clear()
    lsp._RETIRING.clear()


class TestAnAliasThatLeftTheConfiguration:
    @pytest.mark.asyncio
    async def test_its_child_is_stopped(self):
        gone = FakeSupervisor("llama.cpp-abl")
        lsp._ACTIVE_SUPERVISORS["llama.cpp-abl"] = gone

        retired = lsp.retire_absent(["qwen3.8 27b Mythos", "deepseek_flash"])
        await asyncio.sleep(0.05)

        assert retired == ["llama.cpp-abl"]
        # Drained, not killed: an alias can leave the configuration while an
        # agent is still generating on its child.
        assert gone.drained is True
        assert gone.stopped is True
        assert "llama.cpp-abl" not in lsp._ACTIVE_SUPERVISORS

    @pytest.mark.asyncio
    async def test_an_alias_that_survived_the_reload_keeps_its_child(self):
        kept = FakeSupervisor("llama.cpp")
        lsp._ACTIVE_SUPERVISORS["llama.cpp"] = kept

        retired = lsp.retire_absent(["llama.cpp", "deepseek_flash"])
        await asyncio.sleep(0)

        assert retired == []
        assert kept.stopped is False
        assert lsp._ACTIVE_SUPERVISORS["llama.cpp"] is kept

    @pytest.mark.asyncio
    async def test_a_supervisor_that_never_started_is_not_reported_as_a_stopped_child(self):
        never_ran = FakeSupervisor("typo-alias", started=False)
        lsp._ACTIVE_SUPERVISORS["typo-alias"] = never_ran

        retired = lsp.retire_absent(["llama.cpp"])
        await asyncio.sleep(0)

        assert retired == []
        assert never_ran.stopped is False


class TestShutdownReachesAChildNoProviderHolds:
    @pytest.mark.asyncio
    async def test_every_registered_child_is_stopped_and_the_registry_is_emptied(self):
        one = FakeSupervisor("llama.cpp-abl")
        two = FakeSupervisor("llama.cpp")
        lsp._ACTIVE_SUPERVISORS.update({"llama.cpp-abl": one, "llama.cpp": two})

        stopped = await lsp.stop_all_supervisors()

        assert sorted(stopped) == ["llama.cpp", "llama.cpp-abl"]
        assert one.stopped and two.stopped
        # Shutdown stops rather than drains on purpose: waiting for an agent to
        # finish is right for a settings change and would hang the process exit.
        assert one.drained is False and two.drained is False
        assert lsp._ACTIVE_SUPERVISORS == {}

    @pytest.mark.asyncio
    async def test_one_child_that_cannot_be_stopped_does_not_strand_the_others(self):
        bad = FakeSupervisor("broken", raises=True)
        good = FakeSupervisor("llama.cpp")
        lsp._ACTIVE_SUPERVISORS.update({"broken": bad, "llama.cpp": good})

        stopped = await lsp.stop_all_supervisors()

        assert stopped == ["llama.cpp"]
        assert good.stopped is True
        assert lsp._ACTIVE_SUPERVISORS == {}

    @pytest.mark.asyncio
    async def test_the_managers_shutdown_reaches_an_orphan_the_providers_dict_cannot_see(
        self, tmp_path: Path
    ):
        from dpc_client_core.llm_manager import LLMManager

        config = tmp_path / "providers.json"
        config.write_text(json.dumps({"default_provider": "", "providers": []}), encoding="utf-8")
        manager = LLMManager(config_path=config)
        assert "llama.cpp-abl" not in manager.providers

        orphan = FakeSupervisor("llama.cpp-abl")
        lsp._ACTIVE_SUPERVISORS["llama.cpp-abl"] = orphan

        await manager.shutdown()

        assert orphan.stopped is True
        assert lsp._ACTIVE_SUPERVISORS == {}

    @pytest.mark.asyncio
    async def test_a_reload_that_drops_the_alias_stops_its_child_there_and_then(
        self, tmp_path: Path
    ):
        from dpc_client_core.llm_manager import LLMManager

        config = tmp_path / "providers.json"
        config.write_text(json.dumps({"default_provider": "", "providers": []}), encoding="utf-8")
        manager = LLMManager(config_path=config)

        orphan = FakeSupervisor("llama.cpp-abl")
        lsp._ACTIVE_SUPERVISORS["llama.cpp-abl"] = orphan

        manager.save_config({"default_provider": "", "providers": []})
        await asyncio.sleep(0.05)

        assert orphan.drained is True
        assert orphan.stopped is True
        assert "llama.cpp-abl" not in lsp._ACTIVE_SUPERVISORS


class TestTheWindowBetweenTheRenameAndTheEndOfTheDrain:
    """A retired child is in neither list the shutdown walks.

    `retire_absent` takes it out of `_ACTIVE_SUPERVISORS`, and the only handle
    left is the task `_retire` returns — which this module discarded, so between
    the save that retired the alias and the end of its drain the process belonged
    to nobody. Executed against a real child on 2026-08-23: it survived the sweep
    and had to be killed by hand.

    The negative control matters as much as the fix. Three earlier runs of that
    probe reported "no orphan" and every one of them was blind — an idle child
    whose drain had nothing to wait for, a request sent around the provider so
    the supervisor never counted it, and a call shape that raised before the slot
    was taken. `slow_drain` is what those runs were missing.
    """

    @pytest.mark.asyncio
    async def test_a_child_still_draining_is_reached_by_the_shutdown(self):
        busy = FakeSupervisor("renamed-away", slow_drain=True)
        lsp._ACTIVE_SUPERVISORS["renamed-away"] = busy

        assert lsp.retire_absent(["some-other-alias"]) == ["renamed-away"]
        await asyncio.sleep(0.05)

        # The window: out of the main registry, still running, drain unfinished.
        assert "renamed-away" not in lsp._ACTIVE_SUPERVISORS
        assert busy.drained is True and busy.stopped is False
        assert "renamed-away" in lsp._RETIRING

        stopped = await lsp.stop_all_supervisors()

        assert stopped == ["renamed-away"]
        assert busy.stopped is True
        assert lsp._RETIRING == {}

    @pytest.mark.asyncio
    async def test_a_drain_that_finished_leaves_nothing_for_the_shutdown_to_do(self):
        quick = FakeSupervisor("renamed-away")
        lsp._ACTIVE_SUPERVISORS["renamed-away"] = quick

        lsp.retire_absent(["some-other-alias"])
        await asyncio.sleep(0.05)

        assert quick.stopped is True
        assert lsp._RETIRING == {}, "a finished drain must not leave a handle behind"
        assert await lsp.stop_all_supervisors() == []

    @pytest.mark.asyncio
    async def test_a_child_still_loading_its_model_is_retired_and_not_dropped(self):
        """`props` is the health payload and arrives only after /props answers, so
        a child part-way through loading a 27 GB model reads as one that never
        started. The old order popped it out of the registry and then skipped it,
        which is the one state that leaves a process reachable from nowhere at
        all — not even by the drain."""
        loading = FakeSupervisor("slow-to-load", loading=True)
        assert loading.props is None and loading._proc is not None
        lsp._ACTIVE_SUPERVISORS["slow-to-load"] = loading

        retired = lsp.retire_absent(["some-other-alias"])
        await asyncio.sleep(0.05)

        assert retired == ["slow-to-load"]
        assert loading.stopped is True

    @pytest.mark.asyncio
    async def test_a_child_that_already_exited_is_not_reported_as_retired(self):
        dead = FakeSupervisor("finished")
        dead._proc.returncode = 0
        lsp._ACTIVE_SUPERVISORS["finished"] = dead

        assert lsp.retire_absent(["some-other-alias"]) == []
        assert dead.stopped is False
        assert "finished" not in lsp._ACTIVE_SUPERVISORS


class TestTheSuccessorWaitsForTheCardAndNotForTheName:
    """The wait was keyed by alias, and a rename crosses the key.

    `_ACTIVE_SUPERVISORS.get(alias)` misses on a name that has never been in the
    registry, so `supersedes()` was never called and the successor started at
    once — beside a predecessor still draining a generation and still holding a
    card that fits one copy of the model. Both reviews reached this by reading;
    neither started two processes.
    """

    @pytest.mark.asyncio
    async def test_a_renamed_alias_still_waits_for_the_draining_child(self, tmp_path: Path):
        import types

        old = FakeSupervisor("before-the-rename", slow_drain=True)
        lsp._ACTIVE_SUPERVISORS["before-the-rename"] = old
        lsp.retire_absent(["after-the-rename"])
        await asyncio.sleep(0.05)
        assert "before-the-rename" in lsp._RETIRING, "the window must be open for this test"

        gguf = tmp_path / "m.gguf"
        gguf.write_bytes(b"GGUF")
        successor = lsp.LlamaServerProvider("after-the-rename", {"gguf_path": str(gguf)})

        assert successor.supervisor._predecessor is not None, (
            "the successor of a renamed alias must wait for the card"
        )
        assert not successor.supervisor._predecessor.done()

        successor.supervisor._predecessor.cancel()
        old._proc.returncode = 0

    @pytest.mark.asyncio
    async def test_nothing_retiring_means_nothing_to_wait_for(self, tmp_path: Path):
        gguf = tmp_path / "m.gguf"
        gguf.write_bytes(b"GGUF")
        fresh = lsp.LlamaServerProvider("first-alias-ever", {"gguf_path": str(gguf)})
        assert fresh.supervisor._predecessor is None


class TestAWaitThatIsItselfOvertaken:
    @pytest.mark.asyncio
    async def test_a_supervisor_superseded_while_waiting_refuses_to_start(self):
        """Two saves inside one turn. The second retires the supervisor the first
        one created, while that supervisor is still queued behind its own
        predecessor. `ensure_running` reads `_draining` once, before the wait, so
        without a re-check the loser wakes up and starts a child nobody holds."""
        from dpc_client_core.managers.llama_server_supervisor import (
            LlamaServerError, LlamaServerSupervisor,
        )

        sup = LlamaServerSupervisor("queued", {"gguf_path": "x.gguf"})

        async def slow():
            await asyncio.sleep(0.05)

        sup.supersedes(asyncio.ensure_future(slow()))

        async def retire_it_midway():
            await asyncio.sleep(0.01)
            sup._draining = True     # what `drain()` sets when this one is retired

        asyncio.ensure_future(retire_it_midway())

        with pytest.raises(LlamaServerError, match="itself superseded"):
            await sup._await_predecessor()


class TestTheSuccessorWaitsWithoutOwning:
    """Cancelling the successor's wait must not cancel the predecessor's drain.

    `asyncio.gather` was the first shape and the wrong one: «if the outer Future
    is cancelled, all children that have not completed yet are also cancelled».
    The successor's wait is precisely what gets cancelled — a request times out,
    a task is torn down — and the cancellation travelled into the drain, which
    then never reached `stop()` while its `_RETIRING` entry was removed by the
    done-callback anyway. A live child, held by nothing, invisible to the sweep:
    this entry's own defect, reintroduced by its fix.

    Found by the probe, not by reading: the retired child survived the shutdown
    and `stop_all_supervisors` reported only the successor.
    """

    @pytest.mark.asyncio
    async def test_cancelling_the_wait_leaves_the_drain_running(self):
        drained = asyncio.Event()

        async def slow_drain():
            await asyncio.sleep(0.2)
            drained.set()

        drain_task = asyncio.ensure_future(slow_drain())
        waiter = asyncio.ensure_future(lsp._watch_without_owning([drain_task]))
        await asyncio.sleep(0.02)

        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass

        assert not drain_task.cancelled(), "the drain belongs to the predecessor"
        await asyncio.sleep(0.3)
        assert drained.is_set(), "and it must be allowed to finish"

    @pytest.mark.asyncio
    async def test_the_wait_ends_when_every_drain_has(self):
        quick = [asyncio.ensure_future(asyncio.sleep(0.01)) for _ in range(3)]
        await asyncio.wait_for(lsp._watch_without_owning(quick), timeout=2)
        assert all(t.done() for t in quick)
