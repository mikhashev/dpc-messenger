"""ADR-052, Confirmation test 31 (Ark #64 §2, Mike #71; slice S3d): a cold CPU twin counts its load and
a warm one does not.

With a 20 s file and a GPU wait chosen between the two, the cold twin (its load and first call
added) loses to the GPU and the warm one wins. The twin unloads after its own short `idle_unload_s`
(60 s by default, `TWIN_IDLE_UNLOAD_S`; L12, "Residency") whatever holds the card, and it is never
loaded because the card became busy: only a call L12 sends there loads it.

A real Whisper provider on a fake GPU, its real twin with a faked model; fake lease clients.
"""

import asyncio

import pytest

from dpc_client_core.providers.whisper_provider import TWIN_IDLE_UNLOAD_S, transcribe_with_route

from tests._card_lease_helpers import BOUND, fast, hold_open, music, nothing_left, until, waiting, whisper  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram

pytestmark = pytest.mark.usefixtures("fast")

AUDIO_S = 20.0


async def busy(client, what="job"):
    over = asyncio.Event()
    task = asyncio.create_task(hold_open(client.lease, what, over))
    await until(lambda: client.lease.granted)
    return over, task


def ask(p, audio_s=AUDIO_S, **kw):
    return {"purpose": "dictation", "wait_s": BOUND, "expected_s": p.expected_seconds(audio_s),
            "audio_s": audio_s, **kw}


def a_wait_between(p, twin):
    """A GPU wait with which the GPU finishes after a warm twin and before a cold one."""
    cold = twin.expected_seconds(AUDIO_S)
    warm = twin.expected_seconds(AUDIO_S, loaded=True)
    assert warm + 5 < cold, "the cold twin pays its load"
    return (cold + warm) / 2 - p.expected_seconds(AUDIO_S)


@pytest.mark.asyncio
async def test_a_cold_twin_loses_to_a_short_gpu_wait(monkeypatch):
    host_ram(monkeypatch, 64_000)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    over, task = await busy(whisper("other", eta=a_wait_between(p, twin)))
    call = asyncio.create_task(transcribe_with_route(p, "f", **ask(p)))
    await until(lambda: waiting() == ["whisper:turbo"])
    assert p.last_route["route"] == "line" and p.last_route["cold"] is True
    over.set()
    assert await asyncio.wait_for(call, BOUND) == {"text": "f"}
    assert twin.loads == 0 and p.loads == 1
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_warm_twin_wins_the_same_wait(monkeypatch):
    host_ram(monkeypatch, 64_000)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    eta = a_wait_between(p, twin)
    twin.pipeline, twin.model_loaded, twin._loaded_device, twin._calls_since_load = object(), True, "cpu", 1
    over, task = await busy(whisper("other", eta=eta))
    result = await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p)), BOUND)
    assert result == {"text": "cpu f", "device": "cpu"}
    assert p.last_route["cold"] is False and twin.loads == 0 and p.loads == 0
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


def test_the_twin_has_its_own_short_idle_unload(monkeypatch):
    p = gpu_whisper(monkeypatch)
    assert TWIN_IDLE_UNLOAD_S == 60.0
    assert p._the_cpu_twin()._idle_unload_s() == TWIN_IDLE_UNLOAD_S
    q = gpu_whisper(monkeypatch, alias="tuned")
    q.config["cpu_idle_unload_s"] = 15
    assert q._the_cpu_twin()._idle_unload_s() == 15
    assert q._idle_unload_s() == 0, "the GPU alias keeps its own"


@pytest.mark.asyncio
async def test_the_twin_unloads_after_its_idle_timer_whatever_holds_the_card(monkeypatch):
    host_ram(monkeypatch, 64_000)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p, idle_s=0.05)
    over, task = await busy(music(), "music job J")
    assert (await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p)), BOUND))["device"] == "cpu"
    await until(lambda: twin.unloads == 1 and not twin.model_loaded)
    assert music_still_holds(task)
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


def music_still_holds(task):
    return not task.done()


@pytest.mark.asyncio
async def test_the_twin_is_never_loaded_because_the_card_became_busy(monkeypatch):
    host_ram(monkeypatch, 64_000)
    p = gpu_whisper(monkeypatch)
    over, task = await busy(music(), "music job J")
    await asyncio.sleep(0.05)
    assert p._cpu_twin is None, "no twin is built, let alone loaded, until a call is routed there"
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()
