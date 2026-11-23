"""ADR-052, Confirmation tests 26 and 27 (slice S3b; L6, section 3.1, Ark #60 §3 and #61 §2a).

"Nothing to give" means no model; "could not let go" means a model that is there and was not unloaded.
A Whisper yield whose unload never ran (the worker cancelled before its thread started, or the
executor refusing the work) answers `could_not_let_go`: the model stays loaded, the lease stays
granted, nothing announces an unload, and the contender keeps waiting. An unload whose thread ran
and whose release failed (`_unload_model` returns False) did free the reference: it lets go. A granted
lease with no model answers `nothing_to_give` and is released at once.
"""

import asyncio

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseWaitedTooLong
from dpc_client_core.providers import whisper_provider

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, until, waiting, whisper  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of

pytestmark = pytest.mark.usefixtures("fast")


@pytest.fixture
def announced():
    seen = []
    whisper_provider.set_unloaded_listener(lambda alias, model, reason: seen.append(reason))
    yield seen
    whisper_provider.set_unloaded_listener(None)


def _worker_task(name):
    return next(t for t in asyncio.all_tasks()
                if not t.done() and getattr(t.get_coro(), "__name__", "") == name)


async def _never_started_thread(fn, *args, **kwargs):
    await asyncio.sleep(3600)  # stands for an executor too busy to start the thread


async def _executor_refuses(fn, *args, **kwargs):
    raise RuntimeError("cannot schedule new futures after shutdown")


class TestAnUnloadThatNeverRan:
    @pytest.mark.asyncio
    async def test_a_worker_cancelled_before_its_thread_started_keeps_the_model_and_the_grant(
            self, monkeypatch, announced):
        p = gpu_whisper(monkeypatch)
        await asyncio.wait_for(p.transcribe("warm"), BOUND)
        lease = lease_of(p)
        with monkeypatch.context() as patched:
            patched.setattr(asyncio, "to_thread", _never_started_thread)
            yielding = asyncio.create_task(lease.give_up_for("qwen"))
            await asyncio.sleep(0.05)
            _worker_task("_never_started_thread").cancel()
            outcome = await asyncio.wait_for(yielding, BOUND)

        assert outcome == "could_not_let_go"
        assert p.model_loaded and lease.granted and lease.state == "running", "mark ended, grant kept"
        assert announced == [] and p.unloads == 0 and holding() == ["whisper:turbo"]
        await nothing_left()

    @pytest.mark.asyncio
    async def test_an_executor_that_refuses_the_work_keeps_them_too(self, monkeypatch, announced):
        p = gpu_whisper(monkeypatch)
        await asyncio.wait_for(p.transcribe("warm"), BOUND)
        lease = lease_of(p)
        with monkeypatch.context() as patched:
            patched.setattr(asyncio, "to_thread", _executor_refuses)
            outcome = await asyncio.wait_for(lease.give_up_for("qwen"), BOUND)
        assert outcome == "could_not_let_go" and p.model_loaded and lease.granted and announced == []
        await nothing_left()

    @pytest.mark.asyncio
    async def test_the_contender_keeps_waiting_while_the_model_stays(self, monkeypatch):
        p = gpu_whisper(monkeypatch)
        await asyncio.wait_for(p.transcribe("warm"), BOUND)
        other = whisper("other")
        with monkeypatch.context() as patched:
            patched.setattr(asyncio, "to_thread", _executor_refuses)
            with pytest.raises(LeaseWaitedTooLong) as refused:
                await asyncio.wait_for(other.lease.acquire("dictation", wait_s=0.2), BOUND)
            await until(lambda: not card_lease._YIELDS)
        assert "Whisper (whisper-large-v3-turbo)" in str(refused.value)
        assert p.model_loaded and lease_of(p).granted and not other.lease.granted
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_thread_that_ran_but_whose_release_failed_lets_go(self, monkeypatch, announced):
        p = gpu_whisper(monkeypatch)
        await asyncio.wait_for(p.transcribe("warm"), BOUND)

        def failed_release():
            p.pipeline = None
            p.model_loaded = False  # the reference is dropped anyway (`_unload_model`)
            return False

        monkeypatch.setattr(p, "_unload_model", failed_release)
        outcome = await asyncio.wait_for(lease_of(p).give_up_for("qwen"), BOUND)
        assert outcome == "let_go" and not lease_of(p).granted and announced == ["card_yield"]
        await nothing_left()


class TestNothingToGiveAndCouldNotLetGoAreToldApart:
    @pytest.mark.asyncio
    async def test_a_granted_lease_with_no_model_has_nothing_to_give_and_is_released_at_once(self, monkeypatch):
        p = gpu_whisper(monkeypatch)
        lease = p._the_card_lease()
        await asyncio.wait_for(lease.acquire("a grant whose model went", wait_s=0), BOUND)
        assert lease.granted and not p.model_loaded
        assert (await asyncio.wait_for(lease.give_up_for("qwen"), BOUND)) == "nothing_to_give"
        assert not lease.granted and holding() == []
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_busy_whisper_refuses_and_is_waited_for(self, monkeypatch):
        gate = asyncio.Event()
        p = gpu_whisper(monkeypatch, gate=gate)
        call = asyncio.create_task(p.transcribe("x"))
        await until(lambda: p.ran == ["x"])
        assert (await asyncio.wait_for(p.yield_card("qwen"), BOUND)) == "refused"
        assert p.model_loaded and lease_of(p).granted
        gate.set()
        await asyncio.wait_for(call, BOUND)
        assert base_mod.card_holders()[0]["alias"] == "whisper:turbo" and waiting() == []
