"""ADR-052, Confirmation test 32 (Mike #71, Zcode #69; slice S3d), with item 1a (decided 2026-11-23).

"Busy" is read from Strata's own test (a live `accepted_request` hold in `_CARD_HOLDS`, or a call in
flight), not guessed from timing. A busy Strata and a 120 s file → the CPU twin, its estimate times
`cpu_contention_factor`; a factor that pushes it past `cpu_max_s` → the line; a 400 s file → the line
whatever the estimate (L12's order: the audio cap before the factor). A call started on the CPU stays
there when the card frees mid-run. Item 1a: an idle Strata in front of a dictation goes through L12
first, so a file up to 300 s takes the twin and nothing is swapped out; a longer one, with the swap
on, swaps it out by the owner-start rule.

Real supervisors faked at their seams (`test_one_local_engine_holds_the_card.py`), a real Whisper
provider on a fake GPU and its real twin with a faked model.
"""

import asyncio

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseWaitedTooLong
from dpc_client_core.managers.llama_server_supervisor import card_held_for
from dpc_client_core.providers.whisper_provider import transcribe_with_route

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, until, waiting  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram, l12_settings
from tests.test_music_swaps_out_an_idle_supervisor_when_nothing_waits import quick, running_strata  # noqa: F401
from tests.test_one_local_engine_holds_the_card import card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick")


def ask(p, audio_s, **kw):
    return {"purpose": "dictation", "wait_s": BOUND, "expected_s": p.expected_seconds(audio_s),
            "audio_s": audio_s, **kw}


async def a_turn_on(alias: str):
    """Another conversation's turn standing on `alias` until the returned event is set."""
    over = asyncio.Event()

    async def turn():
        async with card_held_for(alias, "accepted_request", agent_id="agent", conversation_id="chat"):
            await over.wait()

    task = asyncio.create_task(turn())
    await until(lambda: base_mod._CARD_HOLDS.get(alias))
    return over, task


@pytest.mark.asyncio
async def test_a_busy_strata_lets_a_120_s_file_on_the_cpu_times_the_factor(tmp_path, monkeypatch):
    host_ram(monkeypatch, 64_000)
    l12_settings(monkeypatch, cpu_contention_factor=2.0)
    strata, events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    plain = twin.expected_seconds(120.0)
    over, turn = await a_turn_on("flash")
    result = await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 120.0)), BOUND)
    assert result["device"] == "cpu"
    route = p.last_route
    assert route["factor"] == 2.0 and route["cpu_s"] == pytest.approx(2.0 * plain)
    assert "x2 for a busy Strata" in route["why"] and "behind a local engine" in route["why"]
    assert events == [] and holding() == ["flash"] and p.loads == 0
    over.set()
    await asyncio.wait_for(turn, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_call_in_flight_is_busy_too(tmp_path, monkeypatch):
    host_ram(monkeypatch, 64_000)
    l12_settings(monkeypatch, cpu_contention_factor=1.5)
    strata, events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    cpu_twin(monkeypatch, p)
    strata._in_flight = 1
    try:
        assert (await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 60.0)), BOUND))["device"] == "cpu"
        assert p.last_route["factor"] == 1.5
    finally:
        strata._in_flight = 0
    assert events == []
    await nothing_left()


@pytest.mark.asyncio
async def test_a_factor_past_cpu_max_s_sends_the_file_to_the_line(tmp_path, monkeypatch):
    host_ram(monkeypatch, 64_000)
    l12_settings(monkeypatch, cpu_contention_factor=6.0)
    await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    over, turn = await a_turn_on("flash")
    with pytest.raises(LeaseWaitedTooLong):
        await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 120.0, wait_s=0.1)), BOUND)
    assert p.last_route["route"] == "line" and "over cpu_max_s 600 s" in p.last_route["why"]
    assert twin.ran == []
    over.set()
    await asyncio.wait_for(turn, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_400_s_file_takes_the_line_before_the_factor_is_read(tmp_path, monkeypatch):
    host_ram(monkeypatch, 64_000)
    await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    over, turn = await a_turn_on("flash")
    with pytest.raises(LeaseWaitedTooLong):
        await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 400.0, wait_s=0.1)), BOUND)
    assert "over cpu_max_audio_s" in p.last_route["why"] and "factor" not in p.last_route
    assert twin.ran == []
    over.set()
    await asyncio.wait_for(turn, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_call_started_on_the_cpu_stays_there_when_the_card_frees(tmp_path, monkeypatch):
    host_ram(monkeypatch, 64_000)
    await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    gate = asyncio.Event()
    twin = cpu_twin(monkeypatch, p, gate=gate)
    over, turn = await a_turn_on("flash")
    call = asyncio.create_task(transcribe_with_route(p, "f", **ask(p, 60.0)))
    await until(lambda: twin.ran)
    over.set()
    await asyncio.wait_for(turn, BOUND)
    await asyncio.sleep(0.05)
    gate.set()
    assert (await asyncio.wait_for(call, BOUND))["device"] == "cpu"
    assert p.loads == 0 and waiting() == [], "no switch in mid-flight"
    await nothing_left()


@pytest.mark.asyncio
async def test_an_idle_strata_and_a_short_file_take_the_twin_and_nothing_is_swapped(tmp_path, monkeypatch):
    """Item 1a: L12 first, so a dictation never costs Strata its parked conversations."""
    host_ram(monkeypatch, 64_000)
    strata, events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    cpu_twin(monkeypatch, p)
    result = await asyncio.wait_for(transcribe_with_route(p, "d", **ask(p, 120.0)), BOUND)
    assert result["device"] == "cpu" and p.last_route["gpu_s"] is None
    assert events == [] and strata._proc is not None and holding() == ["flash"]
    await nothing_left()


@pytest.mark.asyncio
async def test_an_idle_strata_and_a_long_file_swap_it_out_with_the_swap_on(tmp_path, monkeypatch):
    """Item 1a: a file too long for the twin takes the line and swaps the idle supervisor out."""
    host_ram(monkeypatch, 64_000)
    strata, events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    assert await asyncio.wait_for(transcribe_with_route(p, "d", **ask(p, 400.0)), BOUND) == {"text": "d"}
    assert events == ["parked read", "live saved", "stopped"] and holding() == ["whisper:turbo"]
    assert twin.ran == [] and p.loads == 1
    await nothing_left()
