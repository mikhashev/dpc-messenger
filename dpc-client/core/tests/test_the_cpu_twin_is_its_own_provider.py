"""ADR-052, Confirmation test 33 (Ark #64 §4; slice S3d), with section 3.1 condition 2 and S3c.

The CPU twin is a separate `local_whisper` object with `device: "cpu"`, alias `<alias>@cpu`, no
lease, its own `model_loaded`, load lock and idle timer. A providers reload carries the GPU alias's
model state to the new GPU object and the twin's to the new twin, never one into the other.
`RoutedToCpu` sends the call to the twin with the same `language`, `task` and length. In the
received-voice cascade the twin is reached through its GPU alias when L12 routes there, and a card
refusal (the call went to the line) is not followed by the twin: no switch in mid-flight, so it is
never tried twice; a configured `device: cpu` alias later in the list still is.

A real Whisper provider on a fake GPU and its real twin with a faked model; fake lease clients; the
real received-voice cascade over a service double.
"""

import asyncio
import json

import pytest

from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import (
    CPU_TWIN_SUFFIX,
    LocalWhisperProvider,
    asks_for_the_card,
    is_cpu_twin,
    transcribe_with_route,
)

from tests._card_lease_helpers import BOUND, fast, hold_open, music, nothing_left, until  # noqa: F401
from tests._voice_cascade_helpers import busy_music, received, speaking, voice_service  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram, lease_of

pytestmark = pytest.mark.usefixtures("fast")


def ask(p, audio_s, **kw):
    return {"purpose": "dictation", "wait_s": BOUND, "expected_s": p.expected_seconds(audio_s),
            "audio_s": audio_s, **kw}


def test_the_twin_is_a_separate_cpu_provider_with_no_lease(monkeypatch):
    p = gpu_whisper(monkeypatch)
    p.config.update(language="ru", task="translate", let_peers_use_cpu_twin=True)
    twin = p._the_cpu_twin()
    assert isinstance(twin, LocalWhisperProvider) and twin is not p and p._the_cpu_twin() is twin
    assert twin.alias == "turbo" + CPU_TWIN_SUFFIX == "turbo@cpu"
    assert twin.device == "cpu" and twin.model_name == p.model_name and twin.config["type"] == "local_whisper"
    assert (twin.language, twin.task) == ("ru", "translate")
    assert is_cpu_twin(twin) and not is_cpu_twin(p)
    assert "let_peers_use_cpu_twin" not in twin.config
    assert twin._load_lock is not p._load_lock and twin._card_lease is None
    assert asks_for_the_card(twin) is False and asks_for_the_card(p) is True


@pytest.mark.asyncio
async def test_routed_to_cpu_carries_language_task_and_length_and_the_states_stay_apart(monkeypatch, busy_music):
    host_ram(monkeypatch, 64_000)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    result = await asyncio.wait_for(
        transcribe_with_route(p, "f", language="de", task="translate", **ask(p, 45.0)), BOUND)
    assert result == {"text": "cpu f", "device": "cpu"}
    assert twin.ran == [("f", "de", "translate", 45.0)]
    assert twin.model_loaded and not p.model_loaded and lease_of(p) is not None and not lease_of(p).granted
    busy_music.set()
    await nothing_left()


@pytest.mark.asyncio
async def test_a_providers_reload_carries_each_state_to_its_own_successor(tmp_path, monkeypatch):
    path = tmp_path / "providers.json"
    config = {"providers": [{"alias": "w", "type": "local_whisper", "model": "openai/whisper-large-v3-turbo",
                             "device": "cuda", "idle_unload_s": 0, "cpu_idle_unload_s": 0}],
              "voice_provider": "w"}
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", None)
    manager = LLMManager(path)
    old = manager.providers["w"]
    old_twin = old._the_cpu_twin()
    pipeline = object()
    old_twin.pipeline, old_twin.model_loaded, old_twin._loaded_device = pipeline, True, "cpu"
    old_twin._measured["cpu"] = {"warm": {"<=60": 0.5}}

    manager.save_config(config)
    new = manager.providers["w"]
    assert new is not old and old._successor is new
    assert not new.model_loaded, "the GPU object gets no CPU pipeline"
    new_twin = new._cpu_twin
    assert new_twin is not None and new_twin is not old_twin and old_twin._successor is new_twin
    assert new_twin.model_loaded and new_twin.pipeline is pipeline and not old_twin.model_loaded
    assert new_twin._load_lock is old_twin._load_lock
    assert new_twin.speed_figures("cpu")["warm"]["<=60"] == 0.5, "its measured figures follow it"
    assert "w@cpu" not in manager.providers, "never in the providers manager's list"


def test_a_retired_gpu_alias_retires_its_twin(monkeypatch):
    p = gpu_whisper(monkeypatch)
    twin = p._the_cpu_twin()
    p.retire()
    assert p._cpu_twin is None and twin._retired


@pytest.mark.asyncio
async def test_the_cascade_reaches_the_twin_through_its_gpu_alias(monkeypatch, tmp_path, busy_music):
    host_ram(monkeypatch, 64_000)
    monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: 20.0)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF....WAVEfmt fake")
    w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
    twin = speaking(cpu_twin(monkeypatch, w1))
    svc = voice_service({"w1": w1}, ["w1"], bound=BOUND)
    await asyncio.wait_for(received(svc, str(audio)), BOUND)
    assert svc._voice_transcriptions["t-1"]["text"] == "heard w1@cpu"
    assert twin.loads == 1 and w1.loads == 0
    busy_music.set()
    await nothing_left()


@pytest.mark.asyncio
async def test_after_a_card_refusal_the_twin_is_not_tried_and_a_configured_cpu_alias_is(
        monkeypatch, tmp_path, busy_music):
    """The 400 s file went to the line (over the twin's cap) and was refused at the bound: the walk
    skips the next GPU alias and its twin, and reaches the configured `device: cpu` alias once."""
    host_ram(monkeypatch, 64_000)
    monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: 400.0)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF....WAVEfmt fake")
    w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
    w2 = speaking(gpu_whisper(monkeypatch, alias="w2"))
    small = speaking(gpu_whisper(monkeypatch, alias="small_cpu", device="cpu"))
    twin1, twin2 = cpu_twin(monkeypatch, w1), cpu_twin(monkeypatch, w2)
    svc = voice_service({"w1": w1, "w2": w2, "small_cpu": small}, ["w1", "w2", "small_cpu"], bound=0.2)
    await asyncio.wait_for(received(svc, str(audio)), BOUND)
    assert svc._voice_transcriptions["t-1"]["text"] == "heard small_cpu"
    assert w1.last_route["route"] == "line" and w2.loads == 0 and w2.last_route is None
    assert twin1.ran == [] and twin2.ran == [] and small.loads == 1
    busy_music.set()
    await nothing_left()
