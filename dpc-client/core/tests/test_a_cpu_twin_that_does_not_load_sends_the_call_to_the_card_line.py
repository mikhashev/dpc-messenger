"""ADR-052 L12, "No switch in mid-flight", amended (Johnny #63-5; slice S3e): a CPU twin that fails to
load has processed no audio, so the call goes to the card's line instead of failing.

Before S3e, once L12 chose the twin the card was never asked: a twin that could not load (its own
RAM check, a load error) failed the call, even a 299 s file the GPU could have taken. Now
`transcribe_with_route` sends such a call to the line with its own arguments (`cpu_route=False`, L12
is not asked again). Inside an agent's own turn (section 3.5) it is still refused at once
(`test_an_agents_transcription_inside_its_own_turn.py`). A twin that fails while transcribing has
started the flight: that failure is still the call's.

The door's `wait_s` is a deadline, not a fresh bound for each attempt (Zcode #88-4): the line gets what
is left of it after the twin's failed load.
"""

import asyncio
import logging

import pytest

from dpc_client_core.managers.card_lease import LeaseWaitedTooLong
from dpc_client_core.providers import whisper_provider as wp
from dpc_client_core.providers.whisper_provider import RoutedToCpu, TwinDidNotLoad, transcribe_with_route

from tests._card_lease_helpers import BOUND, fast, hold_open, music, nothing_left, until, waiting  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram, lease_of

pytestmark = pytest.mark.usefixtures("fast")

PLENTY = 64_000


async def busy_music():
    m = music()
    over = asyncio.Event()
    task = asyncio.create_task(hold_open(m.lease, "music job J", over))
    await until(lambda: m.lease.granted)
    return m, over, task


def ask(p, audio_s, **kw):
    return {"purpose": "dictation", "wait_s": BOUND, "expected_s": p.expected_seconds(audio_s),
            "audio_s": audio_s, **kw}


@pytest.mark.asyncio
async def test_a_twin_whose_load_fails_sends_the_call_to_the_line(monkeypatch, caplog):
    host_ram(monkeypatch, PLENTY)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p, load_fails=True)
    m, over, task = await busy_music()  # a running job's ETA is unknown: L12 sends 60 s to the twin
    with caplog.at_level(logging.INFO, logger=wp.logger.name):
        call = asyncio.create_task(transcribe_with_route(p, "f", **ask(p, 60.0)))
        await until(lambda: waiting() == ["whisper:turbo"])
    assert twin.loads == 1 and p.last_route["route"] == "cpu"
    assert any("the CPU twin did not load" in r.getMessage() and "goes to the line for the card"
               in r.getMessage() for r in caplog.records)
    over.set()
    await asyncio.wait_for(task, BOUND)
    m.lease.release("test")
    assert await asyncio.wait_for(call, BOUND) == {"text": "f"}, "the GPU took it after the job"
    assert p.loads == 1 and twin.ran == []
    lease_of(p).release("test")
    await nothing_left()


@pytest.mark.asyncio
async def test_the_line_keeps_the_doors_bound(monkeypatch):
    host_ram(monkeypatch, PLENTY)
    p = gpu_whisper(monkeypatch)
    cpu_twin(monkeypatch, p, load_fails=True)
    m, over, task = await busy_music()
    with pytest.raises(LeaseWaitedTooLong):
        await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 60.0, wait_s=0.1)), BOUND)
    assert p.loads == 0
    over.set()
    await asyncio.wait_for(task, BOUND)
    m.lease.release("test")
    await nothing_left()


@pytest.mark.asyncio
async def test_the_twins_own_ram_check_is_a_load_failure_too(monkeypatch):
    """L12 saw the twin warm (no check at the seam); its idle timer unloaded it; its own load's RAM check
    refuses (S3 follow-up 7). That is a load failure: the call goes to the line."""
    p = gpu_whisper(monkeypatch)
    twin = p._the_cpu_twin()
    host_ram(monkeypatch, 1000)
    with pytest.raises(TwinDidNotLoad, match="1000 MiB of host RAM available, 5120 MiB needed"):
        await asyncio.wait_for(twin.transcribe("f", audio_s=30.0), BOUND)
    assert not twin.model_loaded


@pytest.mark.asyncio
async def test_a_twin_that_fails_while_transcribing_is_the_calls_failure(monkeypatch):
    host_ram(monkeypatch, PLENTY)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)

    async def broken(path, language=None, task=None, audio_s=None):
        raise RuntimeError("decode failed halfway")

    monkeypatch.setattr(twin, "_transcribe_loaded", broken)
    m, over, task = await busy_music()
    with pytest.raises(RuntimeError, match="decode failed halfway"):
        await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 60.0)), BOUND)
    assert waiting() == [] and p.loads == 0, "the card was never asked"
    over.set()
    await asyncio.wait_for(task, BOUND)
    m.lease.release("test")
    await nothing_left()


class _SlowTwin:
    """A twin whose load takes `load_s` of wall time and then fails."""

    alias = "turbo (CPU twin)"

    def __init__(self, load_s):
        self.load_s = load_s

    async def transcribe(self, audio_path, release_if_loaded_here=False, **kwargs):
        await asyncio.sleep(self.load_s)
        raise TwinDidNotLoad("the twin's load failed")


class _RoutedOnce:
    """A GPU alias whose L12 sends the first call to `twin`; the second (`cpu_route=False`) records its
    arguments."""

    def __init__(self, twin):
        self.twin, self.asks = twin, []

    async def transcribe(self, audio_path, **kwargs):
        if kwargs.get("cpu_route") is False:
            self.asks.append(kwargs)
            return {"text": audio_path}
        raise RoutedToCpu(self.twin, {"route": "cpu"})


@pytest.mark.asyncio
async def test_the_line_gets_what_is_left_of_the_doors_bound_after_the_twins_load():
    p = _RoutedOnce(_SlowTwin(0.3))
    assert await asyncio.wait_for(transcribe_with_route(p, "f", purpose="dictation", wait_s=10.0), BOUND) == {"text": "f"}
    [ask] = p.asks
    assert ask["wait_s"] <= 10.0 - 0.3, "the twin's load time is taken from the bound, not added to it"
    assert ask["wait_s"] > 10.0 - BOUND


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [None, 0.0, 0.1])
async def test_no_bound_stays_none_and_a_spent_bound_is_zero_never_negative(bound):
    p = _RoutedOnce(_SlowTwin(0.2))
    await asyncio.wait_for(transcribe_with_route(p, "f", purpose="dictation", wait_s=bound), BOUND)
    [ask] = p.asks
    assert ask["wait_s"] == (None if bound is None else 0.0)
