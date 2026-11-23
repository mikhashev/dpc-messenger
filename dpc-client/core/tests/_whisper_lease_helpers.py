"""A local Whisper provider on a fake GPU for the ADR-052 S3b tests: no torch, no model, no card.

`gpu_whisper` builds a real `LocalWhisperProvider` with an explicit `device` (so the lease path never
imports torch, ADR-052 Confirmation) and fakes its load, unload and inference. `load_on` is the device
the fake load reports through `_note_load`, as the real `_load_model` does: "cpu" stands for the
CUDA load that fell back to the CPU. `gate`, when given, holds every inference until it is set.
"""

import asyncio
from typing import Optional

from dpc_client_core.providers.whisper_provider import LocalWhisperProvider


def gpu_whisper(monkeypatch, alias: str = "turbo", *, device: str = "cuda", idle_s: float = 0,
                gate: Optional[asyncio.Event] = None, load_on: Optional[str] = None,
                load_fails: bool = False) -> LocalWhisperProvider:
    p = LocalWhisperProvider(alias, {"type": "local_whisper", "device": device, "idle_unload_s": idle_s,
                                     "model": "openai/whisper-large-v3-turbo"})
    p.loads = 0
    p.unloads = 0
    p.ran = []

    def load():
        p.loads += 1
        if load_fails:
            raise RuntimeError("Failed to load local Whisper model: fake")
        p.pipeline = object()
        p.model_loaded = True
        p._note_load(load_on or device, 0.1)

    def unload():
        p.unloads += 1
        p.pipeline = None
        p.model_loaded = False
        return True

    async def run(path, **kw):
        p.ran.append(path)
        if gate is not None:
            await gate.wait()
        return {"text": path}

    monkeypatch.setattr(p, "_load_model", load)
    monkeypatch.setattr(p, "_unload_model", unload)
    monkeypatch.setattr(p, "_transcribe_loaded", run)
    return p


def lease_of(p: LocalWhisperProvider):
    return p._current_owner()._card_lease


def cpu_twin(monkeypatch, p: LocalWhisperProvider, *, gate: Optional[asyncio.Event] = None,
             load_fails: bool = False, run_s: Optional[float] = None,
             idle_s: Optional[float] = 0) -> LocalWhisperProvider:
    """The CPU twin of `p` (ADR-052 L12, S3d), built as `p` builds it, its load, unload and inference
    faked like `gpu_whisper`'s. `ran` records (path, language, task, audio_s). With `run_s`, each run
    is recorded as having taken that many seconds (`_record_run`), as a real run records its speed.
    `idle_s` is its idle unload (0: off, so no timer outlives the test; None: as built)."""
    twin = p._the_cpu_twin()
    if idle_s is not None:
        twin.config["idle_unload_s"] = idle_s
    twin.loads = 0
    twin.unloads = 0
    twin.ran = []

    def load():
        twin.loads += 1
        if load_fails:
            raise RuntimeError("Failed to load local Whisper model: fake")
        twin.pipeline = object()
        twin.model_loaded = True
        twin._note_load("cpu", 0.1)

    def unload():
        twin.unloads += 1
        twin.pipeline = None
        twin.model_loaded = False
        return True

    async def run(path, language=None, task=None, audio_s=None):
        twin.ran.append((path, language, task, audio_s))
        if gate is not None:
            await gate.wait()
        if run_s is not None and audio_s is not None:
            twin._record_run(audio_s, run_s)
        return {"text": f"cpu {path}"}

    monkeypatch.setattr(twin, "_load_model", load)
    monkeypatch.setattr(twin, "_unload_model", unload)
    monkeypatch.setattr(twin, "_transcribe_loaded", run)
    return twin


def host_ram(monkeypatch, mib: Optional[float]) -> None:
    """What `psutil` would say is available, in MiB (None: unreadable, as conftest starts it)."""
    from dpc_client_core.providers import whisper_provider

    monkeypatch.setattr(whisper_provider, "available_ram_mib", lambda: mib)


def l12_settings(monkeypatch, **values) -> None:
    """L12's `[local_transcription]` numbers as the settings source would give them."""
    from dpc_client_core.providers import whisper_provider

    monkeypatch.setattr(whisper_provider, "_cpu_route_source", lambda: dict(values))
