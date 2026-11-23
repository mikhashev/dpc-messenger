"""ADR-052, Confirmation test 40 (section 3.5, Ark #82 §2, Mike #11; slice S3d): an agent's
transcription inside its own turn.

A turn on a local alias stands on the card, so its tool's transcription can never get the GPU before
the turn ends. A file up to `cpu_max_audio_s` (300 s) goes to the CPU twin at once, with no lease and
no wait, whatever `cpu_contention_factor` and `cpu_max_s` say (only the RAM check stays); a longer
file, one of unknown length, or one the twin cannot take (too little RAM, its load failed) is
refused at once with `LeaseWouldWaitOnItself`, naming the alias and the reason, and no place is taken
in either line. A turn on a cloud alias stands on nothing: L12 as for any caller.

Real supervisors faked at their seams, a real Whisper provider on a fake GPU and its real twin with a
faked model; the agent tool itself for the words the model reads.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from dpc_client_core.managers.card_lease import LeaseWouldWaitOnItself
from dpc_client_core.managers.llama_server_supervisor import card_held_for
from dpc_client_core.providers.whisper_provider import transcribe_with_route

from tests._card_lease_helpers import BOUND, fast, nothing_left, waiting  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram, l12_settings
from tests.test_music_swaps_out_an_idle_supervisor_when_nothing_waits import quick, running_strata  # noqa: F401
from tests.test_one_local_engine_holds_the_card import card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick")


def ask(p, audio_s, **kw):
    the_ask = {"purpose": "agent tool", "wait_s": 1790.0, "expected_s": None}
    if audio_s is not None:
        the_ask.update(audio_s=audio_s, expected_s=p.expected_seconds(audio_s))
    the_ask.update(kw)
    return the_ask


async def in_its_own_turn(call):
    async with card_held_for("flash", "accepted_request", agent_id="agent", conversation_id="chat"):
        return await asyncio.wait_for(call(), BOUND)


@pytest.mark.asyncio
async def test_a_300_s_file_goes_to_the_twin_whatever_the_factor_and_cpu_max_s(tmp_path, monkeypatch):
    host_ram(monkeypatch, 64_000)
    l12_settings(monkeypatch, cpu_contention_factor=9.0, cpu_max_s=10)
    strata, events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    result = await in_its_own_turn(lambda: transcribe_with_route(p, "f", **ask(p, 300.0)))
    assert result == {"text": "cpu f", "device": "cpu"}
    assert twin.loads == 1 and p.loads == 0 and events == [] and waiting() == []
    assert p.last_route["route"] == "cpu" and "inside its own turn on 'flash'" in p.last_route["why"]
    assert p._card_lease is not None and not p._card_lease.granted, "no lease asked"
    await nothing_left()


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_s, words", [
    (400.0, "A file over 300 s needs the card, so nothing was queued."),
    (None, "The length of this file cannot be read, and a file over 300 s needs the card"),
])
async def test_a_longer_file_or_an_unknown_length_is_refused_at_once(tmp_path, monkeypatch, audio_s, words):
    host_ram(monkeypatch, 64_000)
    strata, events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    started = time.monotonic()
    with pytest.raises(LeaseWouldWaitOnItself) as refused:
        await in_its_own_turn(lambda: transcribe_with_route(p, "f", **ask(p, audio_s)))
    assert time.monotonic() - started < 0.5
    text = str(refused.value)
    assert text.startswith("Not transcribed: this turn runs on the local model 'flash', which holds the card.")
    assert words in text
    assert refused.value.holder_alias == "flash"
    assert p.loads == 0 and events == [] and waiting() == [] and p._cpu_twin is None
    await nothing_left()


@pytest.mark.asyncio
async def test_too_little_ram_for_the_twin_is_refused_with_the_reason(tmp_path, monkeypatch):
    host_ram(monkeypatch, 2000.0)
    await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    with pytest.raises(LeaseWouldWaitOnItself) as refused:
        await in_its_own_turn(lambda: transcribe_with_route(p, "f", **ask(p, 120.0)))
    assert str(refused.value) == (
        "Not transcribed: the CPU could not take it (2000 MiB of host RAM available, 5120 MiB needed), "
        "and this turn holds the card (the local model 'flash'), so nothing was queued.")
    assert twin.loads == 0 and waiting() == []
    await nothing_left()


@pytest.mark.asyncio
async def test_a_twin_whose_load_fails_is_refused_with_the_reason(tmp_path, monkeypatch):
    host_ram(monkeypatch, 64_000)
    await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p, load_fails=True)
    with pytest.raises(LeaseWouldWaitOnItself) as refused:
        await in_its_own_turn(lambda: transcribe_with_route(p, "f", **ask(p, 120.0)))
    assert str(refused.value).startswith("Not transcribed: the CPU could not take it (")
    assert "Failed to load local Whisper model: fake" in str(refused.value)
    assert twin.loads == 1 and p.loads == 0 and waiting() == []
    await nothing_left()


@pytest.mark.asyncio
async def test_the_model_reads_the_refusal_through_the_tool(tmp_path, monkeypatch):
    from dpc_client_core.dpc_agent.tools.registry import ToolContext
    from dpc_client_core.dpc_agent.tools.transcribe import transcribe_audio_file
    from dpc_client_core.providers import whisper_provider

    host_ram(monkeypatch, 64_000)
    await running_strata(tmp_path)
    (tmp_path / "song.mp3").write_bytes(b"ID3" + b"\x00" * 64)
    monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: 1800.0)
    p = gpu_whisper(monkeypatch, alias="w")
    ctx = ToolContext(agent_root=tmp_path, emit_progress_fn=lambda line: None,
                      dpc_service=SimpleNamespace(llm_manager=SimpleNamespace(providers={"w": p})))
    answer = await in_its_own_turn(lambda: transcribe_audio_file(ctx, "song.mp3"))
    assert answer.startswith("⚠️ Transcription failed: Not transcribed: this turn runs on the local model 'flash'")
    await nothing_left()


@pytest.mark.asyncio
async def test_the_tool_inside_its_turn_gets_the_twins_text(tmp_path, monkeypatch):
    from dpc_client_core.dpc_agent.tools.registry import ToolContext
    from dpc_client_core.dpc_agent.tools.transcribe import transcribe_audio_file
    from dpc_client_core.providers import whisper_provider

    host_ram(monkeypatch, 64_000)
    await running_strata(tmp_path)
    (tmp_path / "hum.wav").write_bytes(b"RIFF....WAVEfmt fake")
    monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: 40.0)
    p = gpu_whisper(monkeypatch, alias="w")
    twin = cpu_twin(monkeypatch, p)
    ctx = ToolContext(agent_root=tmp_path, emit_progress_fn=lambda line: None,
                      dpc_service=SimpleNamespace(llm_manager=SimpleNamespace(providers={"w": p})))
    answer = await in_its_own_turn(lambda: transcribe_audio_file(ctx, "hum.wav"))
    assert "hum" in answer and twin.loads == 1 and p.loads == 0
    await nothing_left()


@pytest.mark.asyncio
async def test_a_turn_on_a_cloud_alias_stands_on_nothing_and_takes_l12(tmp_path, monkeypatch):
    """A cloud alias's hold stands on no alias: with Strata idle and the swap on, a 400 s file takes
    the line and swaps Strata out, no refusal (L12 as for any caller)."""
    host_ram(monkeypatch, 64_000)
    strata, events = await running_strata(tmp_path)
    p = gpu_whisper(monkeypatch)
    async with card_held_for(None, "accepted_request", agent_id="agent", conversation_id="chat"):
        result = await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 400.0, wait_s=BOUND)), BOUND)
    assert result == {"text": "f"} and events == ["parked read", "live saved", "stopped"]
    await nothing_left()
