"""ADR-052, Confirmation test 30 (L12, Mike #62, Ark #64; slice S3d): the GPU/CPU choice compares
times, not audio length.

Before a GPU transcription asks the card lease, L12 decides: the card free → the GPU; a file over
`cpu_max_audio_s` (or of unknown length) → the line; otherwise the CPU twin when its estimate (cold
or warm, from L12's start figures until it measures its own) beats the GPU's finish (the wait plus
the GPU's own work; an unknown wait is long) and is within `cpu_max_s`, and a cold twin fits in host
RAM; else the line. A peer takes no CPU route by default and meets L13. The decision is logged and
kept as `last_route`; the call reaches the twin through `transcribe_with_route`.

A real Whisper provider on a fake GPU, its real twin with a faked model; fake lease clients.
"""

import asyncio
import logging

import pytest

from dpc_client_core.managers.card_lease import LeaseRefusedForPeer, LeaseWaitedTooLong
from dpc_client_core.managers.llama_server_supervisor import serving_a_peer
from dpc_client_core.providers.whisper_provider import RoutedToCpu, transcribe_with_route

from tests._card_lease_helpers import BOUND, fast, hold_open, music, nothing_left, until, waiting, whisper  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram, l12_settings

pytestmark = pytest.mark.usefixtures("fast")

PLENTY = 64_000


async def busy(client, what="job"):
    """`client`'s lease granted with one open use until the returned event is set."""
    over = asyncio.Event()
    task = asyncio.create_task(hold_open(client.lease, what, over))
    await until(lambda: client.lease.granted)
    return over, task


def ask(p, audio_s, **kw):
    """A door's ask as `card_ask` builds it for a known length."""
    return {"purpose": "dictation", "wait_s": BOUND, "expected_s": p.expected_seconds(audio_s),
            "audio_s": audio_s, **kw}


@pytest.mark.asyncio
async def test_a_free_card_takes_the_gpu_and_builds_no_twin(monkeypatch):
    host_ram(monkeypatch, PLENTY)
    p = gpu_whisper(monkeypatch)
    assert await asyncio.wait_for(transcribe_with_route(p, "a", **ask(p, 120.0)), BOUND) == {"text": "a"}
    assert p.loads == 1 and p._cpu_twin is None
    assert p.last_route["route"] == "gpu" and p.last_route["free"] is True
    await nothing_left()


@pytest.mark.asyncio
async def test_a_long_gpu_wait_sends_a_short_file_to_the_cpu(monkeypatch, caplog):
    """Ark's first example: a 130 s file, the GPU 600 s away → the CPU twin."""
    host_ram(monkeypatch, PLENTY)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    cold_cpu_s = twin.expected_seconds(130.0)
    assert cold_cpu_s == pytest.approx(twin.speed_figures("cpu")["load_first_s"] + 130.0 / (300.0 / 306.0))
    over, task = await busy(whisper("other", eta=600.0), "a long transcription")
    with caplog.at_level(logging.INFO):
        result = await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 130.0)), BOUND)
    assert result == {"text": "cpu f", "device": "cpu"}
    assert twin.loads == 1 and p.loads == 0 and waiting() == []
    route = p.last_route
    assert route["route"] == "cpu" and route["cpu_s"] < route["gpu_s"] and route["cold"] is True
    assert route["cpu_s"] == pytest.approx(cold_cpu_s)
    assert any("L12 sends dictation to the CPU twin" in r.getMessage() for r in caplog.records)
    assert twin._card_lease is None and p._card_lease is not None and not p._card_lease.granted
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_card_freeing_soon_keeps_the_file_on_the_gpu(monkeypatch):
    """Ark's second example: a 90 s file with the card freeing in 10 s → the GPU (through the line)."""
    host_ram(monkeypatch, PLENTY)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    over, task = await busy(whisper("other", eta=10.0))
    call = asyncio.create_task(transcribe_with_route(p, "f", **ask(p, 90.0)))
    await until(lambda: waiting() == ["whisper:turbo"])
    assert p.last_route["route"] == "line" and "the GPU is sooner" in p.last_route["why"]
    over.set()
    assert await asyncio.wait_for(call, BOUND) == {"text": "f"}
    assert p.loads == 1 and twin.loads == 0 and twin.ran == []
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_an_unknown_gpu_wait_takes_the_cpu_only_within_cpu_max_s(monkeypatch):
    host_ram(monkeypatch, PLENTY)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    over, task = await busy(music(), "music job J")  # a running job's ETA is unknown: long
    assert (await asyncio.wait_for(transcribe_with_route(p, "short", **ask(p, 120.0)), BOUND))["device"] == "cpu"
    assert p.last_route["gpu_s"] is None

    l12_settings(monkeypatch, cpu_max_s=50)
    with pytest.raises(LeaseWaitedTooLong):
        await asyncio.wait_for(transcribe_with_route(p, "again", **ask(p, 120.0, wait_s=0.1)), BOUND)
    assert p.last_route["route"] == "line" and "over cpu_max_s 50 s" in p.last_route["why"]
    assert [path for path, *_ in twin.ran] == ["short"]
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_s, why", [(400.0, "over cpu_max_audio_s 300 s"),
                                          (None, "the length is unknown")])
async def test_a_file_over_cpu_max_audio_s_or_of_unknown_length_takes_the_line(monkeypatch, audio_s, why):
    host_ram(monkeypatch, PLENTY)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    over, task = await busy(music(), "music job J")
    the_ask = {"purpose": "dictation", "wait_s": 0.1, "expected_s": None}
    if audio_s is not None:
        the_ask.update(audio_s=audio_s, expected_s=p.expected_seconds(audio_s))
    with pytest.raises(LeaseWaitedTooLong):
        await asyncio.wait_for(transcribe_with_route(p, "long", **the_ask), BOUND)
    assert p.last_route["route"] == "line" and why in p.last_route["why"]
    assert twin.ran == [] and p.loads == 0
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_twin_that_never_ran_goes_by_the_start_figures_which_its_runs_replace(monkeypatch):
    """The start figures open the route; a measured run of 120 s in 700 s closes it for that band
    (Mike #11: the timings are learned in operation)."""
    host_ram(monkeypatch, PLENTY)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p, run_s=700.0)
    over, task = await busy(music(), "music job J")
    for _ in range(2):  # the first run measures the penalty, the second the warm figure
        assert (await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 120.0)), BOUND))["device"] == "cpu"
    assert twin.speed_figures("cpu")["warm"]["<=120"] == pytest.approx(120.0 / 700.0)
    with pytest.raises(LeaseWaitedTooLong):
        await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 120.0, wait_s=0.1)), BOUND)
    assert p.last_route["route"] == "line" and p.last_route["cpu_s"] == pytest.approx(700.0)
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_too_little_ram_for_a_cold_twin_takes_the_line_and_a_warm_twin_needs_no_check(monkeypatch):
    host_ram(monkeypatch, 3000.0)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    over, task = await busy(music(), "music job J")
    with pytest.raises(LeaseWaitedTooLong):
        await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 60.0, wait_s=0.1)), BOUND)
    assert p.last_route["route"] == "line" and "3000 MiB of host RAM available, 5120 MiB needed" in p.last_route["why"]
    assert twin.loads == 0

    twin.pipeline, twin.model_loaded, twin._loaded_device = object(), True, "cpu"  # warm: already in RAM
    assert (await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 60.0)), BOUND))["device"] == "cpu"
    assert p.last_route["cold"] is False and twin.loads == 0
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_unreadable_ram_never_opens_a_cold_cpu_route(monkeypatch):
    host_ram(monkeypatch, None)
    p = gpu_whisper(monkeypatch)
    cpu_twin(monkeypatch, p)
    over, task = await busy(music(), "music job J")
    with pytest.raises(LeaseWaitedTooLong):
        await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 60.0, wait_s=0.1)), BOUND)
    assert "the host's available RAM could not be read" in p.last_route["why"]
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_peer_takes_no_cpu_route_and_is_refused_at_once(monkeypatch):
    host_ram(monkeypatch, PLENTY)
    p = gpu_whisper(monkeypatch)
    over, task = await busy(music(), "music job J")
    with serving_a_peer("node-guest"):
        with pytest.raises(LeaseRefusedForPeer):
            await asyncio.wait_for(transcribe_with_route(
                p, "f", purpose="peer node-guest", wait_s=None, expected_s=p.expected_seconds(20.0),
                audio_s=20.0), BOUND)
    assert p.last_route["route"] == "line" and "a peer's call takes no CPU route" in p.last_route["why"]
    assert p._cpu_twin is None, "no twin is even built for a peer"
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_the_gpu_alias_raises_routed_to_cpu_and_nothing_waited(monkeypatch):
    """`RoutedToCpu` is the GPU alias's answer, not a refusal: no place was taken, nothing asked."""
    host_ram(monkeypatch, PLENTY)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    over, task = await busy(music(), "music job J")
    with pytest.raises(RoutedToCpu) as routed:
        await asyncio.wait_for(p.transcribe("f", **ask(p, 30.0)), BOUND)
    assert routed.value.twin is twin and routed.value.decision["route"] == "cpu"
    assert waiting() == [] and twin.loads == 0
    over.set()
    await asyncio.wait_for(task, BOUND)
    await nothing_left()
