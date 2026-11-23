"""ADR-052, Confirmation test 10 (slice S3b; section 3.1): the local Whisper model holds a card lease
while it is loaded on a GPU device, and lets go on every unload that ran, whichever trigger fired:
the idle timer, a deferred unload, `release_if_loaded_here`, a provider switch, shutdown, a yield.
The CPU device makes no lease; an `mlx` provider loaded by hand and never granted releases nothing; a
`cuda` load that falls back to the CPU lets go; a failed load lets go; one lease follows the model
across a providers reload.

Fake load, unload and inference on an explicit device (`tests/_whisper_lease_helpers.py`).
"""

import asyncio

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.providers.whisper_provider import LocalWhisperProvider

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, until  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of

pytestmark = pytest.mark.usefixtures("fast")


def _row(alias="whisper:turbo"):
    return next(row for row in base_mod.card_holders() if row["alias"] == alias)


class TestTheLeaseFollowsTheModel:
    @pytest.mark.asyncio
    async def test_a_cpu_provider_holds_no_lease(self, monkeypatch):
        p = gpu_whisper(monkeypatch, device="cpu")
        assert (await asyncio.wait_for(p.transcribe("x", purpose="dictation"), BOUND)) == {"text": "x"}
        assert p._card_lease is None and card_lease.lease_named("whisper:turbo") is None
        assert holding() == []

    @pytest.mark.asyncio
    async def test_a_cuda_call_holds_the_card_busy_while_it_runs_and_warm_after(self, monkeypatch):
        gate = asyncio.Event()
        p = gpu_whisper(monkeypatch, gate=gate)
        call = asyncio.create_task(p.transcribe("x", purpose="dictation", expected_s=4.0))
        await until(lambda: p.ran == ["x"])
        row = _row()
        assert row["busy"] is True and [use["what"] for use in row["uses"]] == ["dictation"]
        assert row["kind"] == "transcription" and row["engine"] == "whisper" and row["yield_cost"] == "cheap"
        assert row["display"] == "Whisper (whisper-large-v3-turbo)"
        assert row["vram_mib"] == 1622443339 // 2**20 + 2900, "the measured turbo row plus the overhead"
        assert row["eta_s"] is not None and 0 < row["eta_s"] <= 4.0
        gate.set()
        await asyncio.wait_for(call, BOUND)
        assert lease_of(p).granted and holding() == ["whisper:turbo"], "a warm grant: loaded, no use"
        assert _row()["busy"] is False and _row()["eta_s"] is None

    @pytest.mark.asyncio
    async def test_the_idle_timer_lets_go(self, monkeypatch):
        p = gpu_whisper(monkeypatch, idle_s=0.05)
        await asyncio.wait_for(p.transcribe("x"), BOUND)
        await until(lambda: p.unloads == 1)
        await until(lambda: not lease_of(p).granted)
        assert holding() == [] and not p.model_loaded

    @pytest.mark.asyncio
    async def test_a_deferred_unload_lets_go_when_the_transcription_ends(self, monkeypatch):
        gate = asyncio.Event()
        p = gpu_whisper(monkeypatch, gate=gate)
        call = asyncio.create_task(p.transcribe("x"))
        await until(lambda: p.ran == ["x"])
        await asyncio.wait_for(p.unload_model_async("requested"), BOUND)
        assert lease_of(p).granted and p.unloads == 0, "deferred: the call still runs"
        gate.set()
        await asyncio.wait_for(call, BOUND)
        await until(lambda: p.unloads == 1 and not lease_of(p).granted)
        assert holding() == []

    @pytest.mark.asyncio
    async def test_release_if_loaded_here_lets_go_as_the_call_ends(self, monkeypatch):
        p = gpu_whisper(monkeypatch)
        await asyncio.wait_for(p.transcribe("x", release_if_loaded_here=True, purpose="music Transcribe"), BOUND)
        await until(lambda: not lease_of(p).granted)
        assert p.unloads == 1 and holding() == []

    @pytest.mark.asyncio
    async def test_a_switch_and_shutdown_let_go(self, monkeypatch):
        p = gpu_whisper(monkeypatch)
        await asyncio.wait_for(p.transcribe("x"), BOUND)
        await asyncio.wait_for(p.unload_model_async(reason="switched_provider"), BOUND)
        assert not lease_of(p).granted and holding() == []

        await asyncio.wait_for(p.transcribe("y"), BOUND)
        assert lease_of(p).granted
        await asyncio.wait_for(p.shutdown_unload(timeout=2), BOUND)
        assert not lease_of(p).granted and holding() == [] and p.unloads == 2

    @pytest.mark.asyncio
    async def test_a_yield_unloads_and_lets_go(self, monkeypatch):
        p = gpu_whisper(monkeypatch)
        await asyncio.wait_for(p.transcribe("x"), BOUND)
        outcome = await asyncio.wait_for(lease_of(p).give_up_for("qwen"), BOUND)
        assert outcome == "let_go" and p.unloads == 1 and not p.model_loaded
        assert not lease_of(p).granted and holding() == []
        await nothing_left()

    @pytest.mark.asyncio
    async def test_an_unload_under_an_open_use_keeps_the_grant_for_it(self, monkeypatch):
        # A call granted and still waiting for `_load_lock` behind an idle unload must load under
        # the grant, never without it; the grant goes once that use closes with nothing loaded.
        p = gpu_whisper(monkeypatch)
        await asyncio.wait_for(p.transcribe("x"), BOUND)
        lease = lease_of(p)
        async with lease.use("a call waiting for the lock", wait_s=0):
            await asyncio.wait_for(p.unload_model_async("idle"), BOUND)
            await asyncio.sleep(0)
            assert p.unloads == 1 and lease.granted
        p._let_go_of_an_empty_grant("the use ended")
        await until(lambda: not lease.granted)

    @pytest.mark.asyncio
    async def test_a_failed_load_lets_go(self, monkeypatch):
        p = gpu_whisper(monkeypatch, load_fails=True)
        with pytest.raises(RuntimeError, match="fake"):
            await asyncio.wait_for(p.transcribe("x"), BOUND)
        await until(lambda: not lease_of(p).granted)
        assert holding() == []


class TestWhereNoLeaseIsHeld:
    @pytest.mark.asyncio
    async def test_a_cuda_load_that_falls_back_to_the_cpu_lets_go_and_the_next_call_takes_none(self, monkeypatch):
        gate = asyncio.Event()
        p = gpu_whisper(monkeypatch, gate=gate, load_on="cpu")
        call = asyncio.create_task(p.transcribe("x"))
        await until(lambda: p.ran == ["x"])
        assert not lease_of(p).granted and holding() == [], "the model is in host RAM: no card hold"
        gate.set()
        await asyncio.wait_for(call, BOUND)

        second = asyncio.create_task(p.transcribe("y"))
        await until(lambda: p.ran == ["x", "y"])
        assert not lease_of(p).granted and holding() == []
        await asyncio.wait_for(second, BOUND)

    @pytest.mark.asyncio
    async def test_an_mlx_provider_loaded_by_hand_and_never_granted_releases_nothing(self, monkeypatch):
        p = gpu_whisper(monkeypatch, alias="m", device="mlx")
        p.pipeline = "mlx"
        p.model_loaded = True
        await asyncio.wait_for(p.unload_model_async("requested"), BOUND)
        assert not p.model_loaded and holding() == []
        assert p._card_lease is None or not p._card_lease.granted


class TestOneLeaseAcrossAReload:
    @pytest.mark.asyncio
    async def test_the_successor_takes_the_lease_and_answers_its_yield(self, monkeypatch):
        old = gpu_whisper(monkeypatch)
        await asyncio.wait_for(old.transcribe("x"), BOUND)
        lease = lease_of(old)
        assert lease.granted

        new = gpu_whisper(monkeypatch)  # the providers reload builds a new object for the alias
        new.pipeline, new.model_loaded = old.pipeline, True
        old.hand_over_to(new)

        assert new._card_lease is lease and card_lease.lease_named("whisper:turbo") is lease
        assert lease.granted and holding() == ["whisper:turbo"], "a hand-over never releases"
        outcome = await asyncio.wait_for(lease.give_up_for("qwen"), BOUND)
        assert outcome == "let_go" and new.unloads == 1 and old.unloads == 0
        assert not lease.granted and holding() == []
        await nothing_left()

    def test_a_new_object_for_the_alias_builds_no_second_lease_while_one_stands(self, monkeypatch):
        first = LocalWhisperProvider("turbo", {"type": "local_whisper", "device": "cuda"})
        lease = first._the_card_lease()
        lease._granted = True  # standing on the card, as a retired object can leave it
        second = LocalWhisperProvider("turbo", {"type": "local_whisper", "device": "cuda"})
        assert second._the_card_lease() is lease
        lease._granted = False
