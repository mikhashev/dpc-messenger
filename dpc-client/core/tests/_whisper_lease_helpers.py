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
