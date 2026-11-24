"""ADR-052 L12 point 3, Mike's decisions of 2026-11-24 (Q2; Ark #58-3, #61-2; narrowed the same day,
Zcode's review of follow-up 8, point 2): CPU contention is the small work that computes on this
machine's CPU now, not only a busy Strata.

- A resident llama-server busy by its own test (`_busy_for_the_card`: a call in flight, a turn
  standing on it, a child starting) counts, as a busy Strata does; an idle resident one does not.
- A llama-server kept off the card (`--device none`) is NOT counted, a call in flight or not: it is
  heavy, and nobody would run a transcription beside it (follow-up 9b, Mike's call).
- A Whisper transcription already running on the CPU (a CPU twin's, or any local Whisper whose model
  runs on the CPU) counts, its model load included; a twin that is loaded and idle does not.
- The embedding model and GLiNER count while they compute on the CPU (the next test file).
- The INFO line names each contention it counted, and at a factor of 1 still says "no penalty".
- The factor is applied once, whatever the number of contenders.

Real supervisors faked at their seams (`test_one_local_engine_holds_the_card.py`), a real Whisper
provider on a fake GPU and its real twin with a faked model.
"""

import asyncio
import logging

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import transcribe_with_route

from tests._card_lease_helpers import BOUND, fast, nothing_left, until  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram, l12_settings
from tests.test_music_swaps_out_an_idle_supervisor_when_nothing_waits import quick, running_strata  # noqa: F401
from tests.test_one_local_engine_holds_the_card import _llama, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick", "card")


def ask(p, audio_s, **kw):
    return {"purpose": "dictation", "wait_s": BOUND, "expected_s": p.expected_seconds(audio_s),
            "audio_s": audio_s, **kw}


async def running_llama(alias="qwen27", **cfg):
    sup = _llama(alias, **cfg)
    await sup.ensure_running()
    return sup


@pytest.mark.asyncio
async def test_a_busy_llama_server_multiplies_the_twins_estimate(monkeypatch, caplog):
    host_ram(monkeypatch, 64_000)
    l12_settings(monkeypatch, cpu_contention_factor=2.0)
    llama = await running_llama()
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    plain = twin.expected_seconds(60.0)
    llama._in_flight = 1
    try:
        with caplog.at_level(logging.INFO):
            result = await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 60.0)), BOUND)
    finally:
        llama._in_flight = 0
    assert result["device"] == "cpu"
    route = p.last_route
    assert route["factor"] == 2.0 and route["cpu_s"] == pytest.approx(2.0 * plain)
    assert route["contention"] == ["a busy llama-server 'qwen27'"]
    assert "x2 for a busy llama-server 'qwen27'" in route["why"]
    assert any("x2 for a busy llama-server 'qwen27'" in r.getMessage() for r in caplog.records)
    assert llama._proc is not None, "nothing was swapped out"
    await llama.stop()
    await nothing_left()


@pytest.mark.asyncio
async def test_an_idle_resident_llama_server_is_no_contention(monkeypatch):
    host_ram(monkeypatch, 64_000)
    l12_settings(monkeypatch, cpu_contention_factor=2.0)
    llama = await running_llama()
    p = gpu_whisper(monkeypatch)
    cpu_twin(monkeypatch, p)
    result = await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 60.0)), BOUND)
    assert result["device"] == "cpu"
    assert p.last_route["factor"] == 1.0 and p.last_route["contention"] == []
    assert "x2" not in p.last_route["why"] and "no penalty" not in p.last_route["why"]
    await llama.stop()
    await nothing_left()


@pytest.mark.asyncio
async def test_a_llama_server_off_the_card_is_no_contention_even_with_a_call_in_flight(tmp_path, monkeypatch):
    host_ram(monkeypatch, 64_000)
    l12_settings(monkeypatch, cpu_contention_factor=3.0)
    strata, _events = await running_strata(tmp_path)  # in the card's way, idle: no contention itself
    off_card = _llama("cpu-qwen", extra_args=["--device", "none"])
    await off_card.ensure_running()
    assert not off_card.on_the_card() and off_card._proc is not None
    p = gpu_whisper(monkeypatch)
    cpu_twin(monkeypatch, p)
    off_card._in_flight = 1
    try:
        result = await asyncio.wait_for(transcribe_with_route(p, "g", **ask(p, 30.0)), BOUND)
    finally:
        off_card._in_flight = 0
    assert result["device"] == "cpu"
    assert p.last_route["contention"] == [] and p.last_route["factor"] == 1.0
    assert card_lease._cpu_contention() == []
    await off_card.stop()
    await nothing_left()


@pytest.mark.asyncio
async def test_a_twin_already_transcribing_is_contention_for_the_next_call(tmp_path, monkeypatch):
    host_ram(monkeypatch, 64_000)
    l12_settings(monkeypatch, cpu_contention_factor=2.0)
    strata, events = await running_strata(tmp_path)  # idle: the card is in the way, Strata is no contention
    p = gpu_whisper(monkeypatch)
    gate = asyncio.Event()
    twin = cpu_twin(monkeypatch, p, gate=gate)
    first = asyncio.create_task(transcribe_with_route(p, "first", **ask(p, 60.0)))
    await until(lambda: twin.ran)
    assert p.last_route["contention"] == [] and p.last_route["factor"] == 1.0
    assert whisper_provider.cpu_transcriptions_running() == ["turbo@cpu"]
    plain = twin.expected_seconds(60.0)
    second = asyncio.create_task(transcribe_with_route(p, "second", **ask(p, 60.0)))
    await until(lambda: len(twin.ran) == 2)
    route = p.last_route
    assert route["route"] == "cpu" and route["factor"] == 2.0
    assert route["cpu_s"] == pytest.approx(2.0 * plain)
    assert route["contention"] == ["a CPU transcription on 'turbo@cpu'"]
    assert "x2 for a CPU transcription on 'turbo@cpu'" in route["why"]
    gate.set()
    assert (await asyncio.wait_for(first, BOUND))["device"] == "cpu"
    assert (await asyncio.wait_for(second, BOUND))["device"] == "cpu"
    assert whisper_provider.cpu_transcriptions_running() == []
    assert events == []
    await nothing_left()


@pytest.mark.asyncio
async def test_a_loaded_idle_twin_is_no_contention(tmp_path, monkeypatch):
    host_ram(monkeypatch, 64_000)
    l12_settings(monkeypatch, cpu_contention_factor=2.0)
    await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    await asyncio.wait_for(transcribe_with_route(p, "first", **ask(p, 30.0)), BOUND)
    assert twin.model_loaded and whisper_provider.cpu_transcriptions_running() == []
    await asyncio.wait_for(transcribe_with_route(p, "second", **ask(p, 30.0)), BOUND)
    assert p.last_route["contention"] == [] and p.last_route["factor"] == 1.0
    await nothing_left()


@pytest.mark.asyncio
async def test_a_failed_cpu_transcription_leaves_no_contention_behind(monkeypatch):
    p = gpu_whisper(monkeypatch, device="cpu")

    async def fails(path, **kw):
        assert whisper_provider.cpu_transcriptions_running() == ["turbo"]
        raise RuntimeError("decode failed")

    monkeypatch.setattr(p, "_transcribe_loaded", fails)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(p.transcribe("f"), BOUND)
    assert whisper_provider.cpu_transcriptions_running() == []


@pytest.mark.asyncio
async def test_a_busy_strata_and_a_running_twin_are_both_named_at_factor_1(tmp_path, monkeypatch):
    host_ram(monkeypatch, 64_000)
    strata, _events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    gate = asyncio.Event()
    twin = cpu_twin(monkeypatch, p, gate=gate)
    first = asyncio.create_task(transcribe_with_route(p, "first", **ask(p, 60.0)))
    await until(lambda: twin.ran)
    strata._in_flight = 1
    try:
        second = asyncio.create_task(transcribe_with_route(p, "second", **ask(p, 60.0)))
        await until(lambda: len(twin.ran) == 2)
    finally:
        strata._in_flight = 0
    why = p.last_route["why"]
    assert p.last_route["contention"] == ["a busy Strata 'flash'", "a CPU transcription on 'turbo@cpu'"]
    assert "a busy Strata 'flash' and a CPU transcription on 'turbo@cpu', cpu_contention_factor 1, no penalty" in why
    assert "x1" not in why
    gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_the_factor_is_applied_once_for_two_contenders_not_once_per_contender(tmp_path, monkeypatch):
    # With a factor of 1 "once" and "per contender" read the same (Zcode #85-3): a factor of 2 and two
    # contenders (a busy Strata and a running twin) tell them apart, 2x and not 4x.
    host_ram(monkeypatch, 64_000)
    l12_settings(monkeypatch, cpu_contention_factor=2.0)
    strata, _events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    gate = asyncio.Event()
    twin = cpu_twin(monkeypatch, p, gate=gate)
    first = asyncio.create_task(transcribe_with_route(p, "first", **ask(p, 60.0)))
    await until(lambda: twin.ran)
    plain = twin.expected_seconds(60.0)  # warm: the twin is loaded now, as the second call finds it
    strata._in_flight = 1
    try:
        second = asyncio.create_task(transcribe_with_route(p, "second", **ask(p, 60.0)))
        await until(lambda: len(twin.ran) == 2)
    finally:
        strata._in_flight = 0
    route = p.last_route
    assert route["contention"] == ["a busy Strata 'flash'", "a CPU transcription on 'turbo@cpu'"]
    assert route["factor"] == 2.0, "the factor, once"
    assert route["cpu_s"] == pytest.approx(2.0 * plain) and route["cpu_s"] != pytest.approx(4.0 * plain)
    gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_model_load_on_the_cpu_counts_from_before_the_load_and_a_failed_one_leaves_nothing(monkeypatch):
    p = gpu_whisper(monkeypatch, device="cpu")
    seen = []
    load = p._load_model

    def loading():
        seen.append(whisper_provider.cpu_transcriptions_running())
        return load()

    monkeypatch.setattr(p, "_load_model", loading)
    await asyncio.wait_for(p.transcribe("f"), BOUND)
    assert seen == [["turbo"]], "counted while the model loaded, before the transcription began"
    assert whisper_provider.cpu_transcriptions_running() == []

    failing = gpu_whisper(monkeypatch, "broken", device="cpu", load_fails=True)
    with pytest.raises(Exception):
        await asyncio.wait_for(failing.transcribe("f"), BOUND)
    assert whisper_provider.cpu_transcriptions_running() == [], "a load that failed counts no more"


@pytest.mark.asyncio
async def test_a_model_load_on_the_gpu_is_no_cpu_contention(monkeypatch):
    p = gpu_whisper(monkeypatch, device="cuda")
    seen = []
    load = p._load_model

    def loading():
        seen.append(whisper_provider.cpu_transcriptions_running())
        return load()

    monkeypatch.setattr(p, "_load_model", loading)
    await asyncio.wait_for(p.transcribe("f"), BOUND)
    assert seen == [[]] and whisper_provider.cpu_transcriptions_running() == []
