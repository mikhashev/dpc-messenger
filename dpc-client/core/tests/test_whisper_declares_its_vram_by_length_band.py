"""ADR-052 L11, Whisper's declared peak by length band (Johnny #30-2, Forge #31-6, #42-6, #60; slice
S3e).

Before S3e a Whisper lease declared one flat 4447 MiB (1547 of turbo weights + 2900, the batch-8 peak)
and nothing read it. Now the peak follows the call's length band, on the batch descent's thresholds
(`_BATCH_DESCENT`): the torch reserved peak of `gpu_run1.log` plus a 485 MiB context, less the
weights, rounded up. <=60 s 1600 over the weights (ru_60, two chunks at batch 16: 2618 reserved);
<=120 s 2900 (en_120 at batch 8: 3948); <=180 s 2900, **estimated** (batch 4 never measured: four
chunks at once, as batch 8 decoded); >180 s 1600 (en_300 at batch 2: 2660). An unknown length takes
the worst band, and so does the lease's own row. L11's fit is the figure's consumer: the band decides
whether the call fits beside a busy music job. The CPU twin holds no lease and has no figure.
"""

import asyncio

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.music.yue2 import Yue2Server
from dpc_client_core.providers.whisper_provider import (
    _WHISPER_OVERHEAD_BY_BAND_MIB, _WHISPER_OVERHEAD_MIB, transcribe_with_route, whisper_vram_mib,
)

from tests._card_lease_helpers import BOUND, fast, hold_open, music, nothing_left, until, waiting  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram, lease_of

pytestmark = pytest.mark.usefixtures("fast")

TURBO = "openai/whisper-large-v3-turbo"
WEIGHTS = 1547


@pytest.mark.parametrize("audio_s, band_mib", [(10.0, 1600), (60.0, 1600), (61.0, 2900), (120.0, 2900),
                                               (150.0, 2900), (181.0, 1600), (300.0, 1600), (None, 2900)])
def test_the_peak_follows_the_calls_band(audio_s, band_mib):
    assert whisper_vram_mib(TURBO, audio_s) == WEIGHTS + band_mib


def test_the_worst_band_is_the_flat_figure_and_the_band_table_carries_it():
    assert _WHISPER_OVERHEAD_MIB == max(_WHISPER_OVERHEAD_BY_BAND_MIB.values()) == 2900
    assert set(_WHISPER_OVERHEAD_BY_BAND_MIB) == {"<=60", "<=120", "<=180", ">180"}


def test_an_unknown_model_has_no_figure():
    assert whisper_vram_mib("someone/unmeasured-whisper", 10.0) is None


@pytest.mark.asyncio
async def test_the_lease_row_keeps_the_worst_band_and_the_twin_has_no_lease(monkeypatch):
    p = gpu_whisper(monkeypatch)
    await asyncio.wait_for(transcribe_with_route(p, "a", purpose="dictation", wait_s=BOUND, audio_s=10.0), BOUND)
    assert lease_of(p).vram_mib == WEIGHTS + 2900 == 4447
    twin = cpu_twin(monkeypatch, p)
    assert twin._card_lease is None
    lease_of(p).release("test")
    await nothing_left()


async def _busy_music():
    m = music()
    over = asyncio.Event()
    task = asyncio.create_task(hold_open(m.lease, "music job J", over))
    await until(lambda: m.lease.granted)
    return m, over, task


@pytest.mark.asyncio
async def test_the_band_decides_the_fit_beside_a_busy_music_job(monkeypatch):
    """9000 MiB free beside a busy YuE2 (its declared peak): a 10 s dictation (3147) fits, a 90 s one (4447)
    does not, by the live veto `free >= peak + growth_left`."""
    monkeypatch.setattr(card_lease, "_READ_THE_CARD", lambda: (9000, 32623))
    host_ram(monkeypatch, None)  # no CPU route: a call that does not fit goes to the line
    p = gpu_whisper(monkeypatch)
    m, over, task = await _busy_music()

    short = await asyncio.wait_for(
        transcribe_with_route(p, "short", purpose="dictation", wait_s=BOUND, audio_s=10.0,
                              expected_s=p.expected_seconds(10.0)), BOUND)
    assert short == {"text": "short"}
    assert p.last_route["route"] == "gpu" and p.last_route["vram_mib"] == WEIGHTS + 1600
    assert "L11 fits it beside yue2.cpp" in p.last_route["why"]
    lease_of(p).release("test")  # let the next call ask from the start

    call = asyncio.create_task(transcribe_with_route(p, "long", purpose="dictation", wait_s=BOUND, audio_s=90.0,
                                                     expected_s=p.expected_seconds(90.0)))
    await until(lambda: waiting() == ["whisper:turbo"])
    assert p.last_route["route"] == "line" and p.last_route["l11"] == "does_not_fit"
    assert f"9000 MiB free, {4447 + Yue2Server.VRAM_ESTIMATE_MIB} MiB needed now" in p.last_route["why"]
    over.set()
    await asyncio.wait_for(task, BOUND)
    m.lease.release("test")
    assert await asyncio.wait_for(call, BOUND) == {"text": "long"}
    lease_of(p).release("test")
    await nothing_left()
