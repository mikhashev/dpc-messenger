"""The local Whisper model never goes from under a running transcription, and an idle
one frees the card by itself (`[local_transcription] idle_unload_s`).

Step 1 of moving Whisper onto the GPU card queue. The model load, the unload and the
inference are faked; every wait is bounded by `_until` or `asyncio.wait_for`.
"""

import asyncio

import pytest

from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import LocalWhisperProvider
from dpc_client_core.settings import Settings


def _provider(monkeypatch, idle_s=0.05, gate=None):
    p = LocalWhisperProvider("w", {"type": "local_whisper", "device": "cpu", "idle_unload_s": idle_s})
    p.unloads = 0

    def load():
        p.pipeline = object()
        p.model_loaded = True

    def unload():
        p.unloads += 1
        p.pipeline = None
        p.model_loaded = False

    async def run(path):
        if gate is not None:
            await gate.wait()
        return {"text": path}

    monkeypatch.setattr(p, "_load_model", load)
    monkeypatch.setattr(p, "_unload_model", unload)
    monkeypatch.setattr(p, "_transcribe_loaded", run)
    return p


async def _until(predicate, limit=2.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), limit)


async def _running(p, n=1):
    await _until(lambda: p._active_transcriptions == n)


@pytest.mark.asyncio
async def test_an_unload_asked_during_a_transcription_waits_for_it_to_end(monkeypatch):
    gate = asyncio.Event()
    p = _provider(monkeypatch, idle_s=0, gate=gate)
    run = asyncio.create_task(p.transcribe("x"))
    await _running(p)
    await asyncio.wait_for(p.unload_model_async(), 1)  # returns at once: deferred
    assert p.unloads == 0 and p.is_model_loaded()
    gate.set()
    assert (await asyncio.wait_for(run, 1)) == {"text": "x"}
    await _until(lambda: p.unloads == 1)
    assert not p.is_model_loaded()


@pytest.mark.asyncio
async def test_an_unload_asked_while_idle_happens_at_once(monkeypatch):
    p = _provider(monkeypatch, idle_s=0)
    await p.transcribe("x")
    await asyncio.wait_for(p.unload_model_async(), 1)
    assert p.unloads == 1 and not p.is_model_loaded()


@pytest.mark.asyncio
async def test_an_idle_model_unloads_after_the_timeout(monkeypatch):
    p = _provider(monkeypatch, idle_s=0.05)
    await p.transcribe("x")
    assert p.is_model_loaded()
    await _until(lambda: p.unloads == 1 and p._idle_task is None)
    assert not p.is_model_loaded()


@pytest.mark.asyncio
async def test_a_new_transcription_cancels_the_pending_unload(monkeypatch):
    gate = asyncio.Event()
    p = _provider(monkeypatch, idle_s=0.15)
    await p.transcribe("first")
    first_timer = p._idle_task
    assert first_timer is not None
    # A second transcription starts and outlasts the first timer.
    async def slow(path):
        await gate.wait()
        return {"text": path}
    monkeypatch.setattr(p, "_transcribe_loaded", slow)
    second = asyncio.create_task(p.transcribe("second"))
    await _running(p)
    await asyncio.sleep(0.3)
    assert p.unloads == 0 and p.is_model_loaded()
    assert first_timer.cancelled() or first_timer.done()
    gate.set()
    await asyncio.wait_for(second, 1)
    await _until(lambda: p.unloads == 1)  # the timer restarted when it ended


@pytest.mark.asyncio
async def test_a_zero_timeout_never_unloads(monkeypatch):
    p = _provider(monkeypatch, idle_s=0)
    await p.transcribe("x")
    assert p._idle_task is None
    await asyncio.sleep(0.1)
    assert p.unloads == 0 and p.is_model_loaded()


@pytest.mark.asyncio
async def test_the_timeout_comes_from_the_settings_source(monkeypatch):
    p = LocalWhisperProvider("w", {"type": "local_whisper"})
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", lambda: 7)
    assert p._idle_unload_s() == 7.0
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", None)
    assert p._idle_unload_s() == whisper_provider.DEFAULT_IDLE_UNLOAD_S


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_running_transcription_then_unloads(monkeypatch):
    gate = asyncio.Event()
    p = _provider(monkeypatch, idle_s=0, gate=gate)
    run = asyncio.create_task(p.transcribe("x"))
    await _running(p)
    stopping = asyncio.create_task(p.shutdown_unload(timeout=2))
    await asyncio.sleep(0.05)
    assert not stopping.done() and p.unloads == 0
    gate.set()
    await asyncio.wait_for(stopping, 2)
    await asyncio.wait_for(run, 1)
    assert p.unloads == 1 and not p.is_model_loaded()


@pytest.mark.asyncio
async def test_shutdown_unloads_anyway_after_the_bound(monkeypatch):
    gate = asyncio.Event()  # never set: the transcription hangs
    p = _provider(monkeypatch, idle_s=0, gate=gate)
    run = asyncio.create_task(p.transcribe("x"))
    await _running(p)
    await asyncio.wait_for(p.shutdown_unload(timeout=0.1), 2)
    assert p.unloads == 1 and not p.is_model_loaded()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run


@pytest.mark.asyncio
async def test_a_retired_provider_keeps_no_timer_and_a_reload_gets_its_own(monkeypatch):
    old = _provider(monkeypatch, idle_s=0.05)
    await old.transcribe("x")
    assert old._idle_task is not None
    old.retire()
    assert old._idle_task is None
    await asyncio.sleep(0.15)
    assert old.unloads == 0  # the retired object never unloads the shared pipeline
    new = _provider(monkeypatch, idle_s=0.05)
    new.pipeline, new.model_loaded = old.pipeline, True
    new.schedule_idle_unload()
    await _until(lambda: new.unloads == 1)
    await old.shutdown()  # idempotent
    assert old._idle_task is None


def test_the_setting_defaults_to_300_and_reads_zero_as_never(tmp_path):
    s = Settings(tmp_path)
    assert s.get_local_transcription_idle_unload_s() == 300.0
    s._config["local_transcription"]["idle_unload_s"] = "0"
    assert s.get_local_transcription_idle_unload_s() == 0.0
    s._config["local_transcription"]["idle_unload_s"] = "nonsense"
    assert s.get_local_transcription_idle_unload_s() == 300.0
