"""A providers reload (config save, voice-provider switch) hands every local Whisper
object over to its replacement: a transcription running across the reload still holds
off the idle timer and any deferred unload, a stale reference to the old object cannot
corrupt the count, and an unload in flight is never carried as a loaded model.

The model load, the unload and the inference are faked on the class; every wait is
bounded.
"""

import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import LocalWhisperProvider

ALIAS = "w"


class _World:
    def __init__(self):
        self.unloads = 0
        self.unload_started = threading.Event()
        self.unload_release = threading.Event()
        self.unload_release.set()
        self.unload_ok = True
        self.gate = None  # an asyncio.Event the inference waits on


@pytest.fixture
def world(monkeypatch):
    w = _World()

    def load(self):
        self.pipeline = object()
        self.model_loaded = True

    def unload(self):
        w.unloads += 1
        w.unload_started.set()
        assert w.unload_release.wait(5)
        self.pipeline = None
        self.model_loaded = False
        return w.unload_ok

    async def run(self, path):
        if w.gate is not None:
            await w.gate.wait()
        return {"text": path}

    monkeypatch.setattr(LocalWhisperProvider, "_load_model", load)
    monkeypatch.setattr(LocalWhisperProvider, "_unload_model", unload)
    monkeypatch.setattr(LocalWhisperProvider, "_transcribe_loaded", run)
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", None)
    monkeypatch.setattr(whisper_provider, "_unloaded_listener", None)
    return w


@pytest.fixture
def llm(tmp_path, world):
    path = tmp_path / "providers.json"
    config = {"providers": [{"alias": ALIAS, "type": "local_whisper", "model": "m",
                             "device": "cpu", "idle_unload_s": 0.05}],
              "voice_provider": ALIAS}
    path.write_text(json.dumps(config), encoding="utf-8")
    manager = LLMManager(path)
    manager.test_config = config
    return manager


async def _until(predicate, limit=2.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), limit)


@pytest.mark.asyncio
async def test_a_transcription_running_across_a_config_save_holds_off_unloads(llm, world):
    world.gate = asyncio.Event()
    old = llm.providers[ALIAS]
    run = asyncio.create_task(old.transcribe("x"))
    await _until(lambda: old._active_transcriptions == 1)

    llm.save_config(llm.test_config)
    new = llm.providers[ALIAS]
    assert new is not old and old._successor is new
    assert new._active_transcriptions == 1 and new.is_model_loaded()

    await asyncio.sleep(0.2)  # well past the idle timeout
    assert world.unloads == 0
    await asyncio.wait_for(new.unload_model_async(reason="requested"), 1)
    assert world.unloads == 0  # deferred, not run under the transcription
    world.gate.set()
    assert (await asyncio.wait_for(run, 1)) == {"text": "x"}
    await _until(lambda: world.unloads == 1)
    assert new._active_transcriptions == 0 and not new.is_model_loaded()


@pytest.mark.asyncio
async def test_a_deferred_unload_asked_on_the_old_object_survives_the_reload(llm, world):
    world.gate = asyncio.Event()
    old = llm.providers[ALIAS]
    run = asyncio.create_task(old.transcribe("x"))
    await _until(lambda: old._active_transcriptions == 1)
    await asyncio.wait_for(old.unload_model_async(reason="auto_transcribe_disabled"), 1)
    assert old._unload_when_idle and world.unloads == 0

    llm.reload_providers()
    new = llm.providers[ALIAS]
    assert world.unloads == 0
    world.gate.set()
    await asyncio.wait_for(run, 1)
    await _until(lambda: world.unloads == 1)
    assert not new.is_model_loaded()


@pytest.mark.asyncio
async def test_the_voice_provider_switch_goes_through_the_same_reload(llm, world):
    from dpc_client_core.voice_service import VoiceService
    world.gate = asyncio.Event()
    old = llm.providers[ALIAS]
    run = asyncio.create_task(old.transcribe("x"))
    await _until(lambda: old._active_transcriptions == 1)

    with patch.object(VoiceService, '_load_voice_transcription_settings', lambda self: None):
        svc = VoiceService(llm_manager=llm, settings=MagicMock(),
                           local_api=MagicMock(broadcast_event=AsyncMock()))
    result = await svc.set_voice_provider(ALIAS)
    assert result["status"] == "success"
    new = llm.providers[ALIAS]
    assert new is not old and new._active_transcriptions == 1 and old._successor is new
    await asyncio.sleep(0.2)
    assert world.unloads == 0
    world.gate.set()
    await asyncio.wait_for(run, 1)
    await _until(lambda: world.unloads == 1)


@pytest.mark.asyncio
async def test_a_stale_reference_counts_on_the_replacement_and_never_goes_negative(llm, world):
    old = llm.providers[ALIAS]
    await old.transcribe("warm")
    llm.reload_providers()
    new = llm.providers[ALIAS]
    world.gate = asyncio.Event()
    stale = asyncio.create_task(old.transcribe("via-old"))  # the old reference, after the reload
    await _until(lambda: new._active_transcriptions == 1)
    assert old._active_transcriptions == 0
    await asyncio.wait_for(new.unload_model_async(), 1)
    assert world.unloads == 0  # deferred under the stale-reference transcription
    world.gate.set()
    assert (await asyncio.wait_for(stale, 1)) == {"text": "via-old"}
    assert new._active_transcriptions == 0
    await _until(lambda: world.unloads == 1)


@pytest.mark.asyncio
async def test_a_counter_that_would_go_below_zero_is_clamped_and_logged(llm, caplog):
    p = llm.providers[ALIAS]
    await p.transcribe("x")
    p._active_transcriptions = 0

    async def corrupt(path):
        p._active_transcriptions = 0
        return {"text": path}
    p._transcribe_loaded = corrupt
    with caplog.at_level("ERROR"):
        await p.transcribe("y")
    assert p._active_transcriptions == 0
    assert "no use counted" in caplog.text


@pytest.mark.asyncio
async def test_an_alias_removed_by_a_reload_refuses_to_transcribe(llm, world, tmp_path):
    old = llm.providers[ALIAS]
    await old.transcribe("warm")
    llm.config_path.write_text(json.dumps({"providers": []}), encoding="utf-8")
    llm.reload_providers()
    assert ALIAS not in llm.providers and old._successor is None
    with pytest.raises(RuntimeError, match="removed or shut down"):
        await old.transcribe("late")
    with pytest.raises(RuntimeError, match="removed or shut down"):
        await old.ensure_model_loaded()


@pytest.mark.asyncio
async def test_an_unloaded_alias_is_handed_over_too_so_a_stale_reference_forwards(llm, world):
    old = llm.providers[ALIAS]
    llm.reload_providers()
    new = llm.providers[ALIAS]
    assert old._successor is new
    assert (await old.transcribe("x")) == {"text": "x"}  # forwarded: loads and counts on `new`
    assert new.is_model_loaded() and not old.is_model_loaded()


@pytest.mark.asyncio
async def test_a_reload_during_an_unload_does_not_carry_a_loaded_model(llm, world):
    old = llm.providers[ALIAS]
    await old.transcribe("warm")
    world.unload_release.clear()
    unloading = asyncio.create_task(old.unload_model_async())
    await asyncio.wait_for(asyncio.to_thread(world.unload_started.wait, 2), 3)
    assert old._unloading and old.model_loaded  # the unload thread is mid-flight

    llm.reload_providers()
    new = llm.providers[ALIAS]
    assert not new.is_model_loaded() and new.pipeline is None
    assert new._load_lock is old._load_lock

    loading = asyncio.create_task(new.transcribe("after"))
    await asyncio.sleep(0.05)
    assert not loading.done()  # waits for the unload on the shared lock
    world.unload_release.set()
    await asyncio.wait_for(unloading, 2)
    assert (await asyncio.wait_for(loading, 2)) == {"text": "after"}
    assert new.is_model_loaded() and new.pipeline is not None


@pytest.mark.asyncio
async def test_the_deferred_flag_is_carried_by_the_hand_over(world):
    old = LocalWhisperProvider("w", {"type": "local_whisper", "device": "cpu"})
    new = LocalWhisperProvider("w", {"type": "local_whisper", "device": "cpu"})
    old._unload_when_idle = True
    old._unload_reason = "switched_provider"
    old.hand_over_to(new)
    assert new._unload_when_idle is True and new._unload_reason == "switched_provider"


@pytest.mark.asyncio
async def test_every_unload_path_tells_the_listener(llm, world):
    seen = []
    whisper_provider.set_unloaded_listener(lambda alias, model, reason: seen.append((alias, model, reason)))
    p = llm.providers[ALIAS]
    await p.transcribe("x")
    await _until(lambda: [r for _, _, r in seen] == ["idle"])
    await p.transcribe("y", release_if_loaded_here=True)  # loaded here -> released
    assert seen[-1] == (ALIAS, "m", "released")
    await p.transcribe("z")
    await p.unload_model_async(reason="switched_provider")
    assert seen[-1][2] == "switched_provider"
    await p.transcribe("w")
    await p.shutdown_unload(timeout=1)
    assert seen[-1][2] == "shutdown"


@pytest.mark.asyncio
async def test_a_failed_unload_is_logged_as_a_failure_not_as_success(llm, world, caplog):
    world.unload_ok = False
    p = llm.providers[ALIAS]
    await p.transcribe("x")
    with caplog.at_level("INFO"):
        await p.unload_model_async()
    assert "the unload failed" in caplog.text and "unloaded: " not in caplog.text


@pytest.mark.asyncio
async def test_a_cancelled_release_still_rearms_the_idle_timer(llm, world):
    p = llm.providers[ALIAS]
    await p.transcribe("warm")
    await _until(lambda: world.unloads == 1)  # idle timer ran once
    world.unload_release.clear()  # the release below blocks inside its thread
    world.unload_started.clear()
    run = asyncio.create_task(p.transcribe("x", release_if_loaded_here=True))
    await asyncio.wait_for(asyncio.to_thread(world.unload_started.wait, 2), 3)
    run.cancel()
    world.unload_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 2)
    assert p._active_transcriptions == 0


def test_a_garbage_provider_level_timeout_warns_and_uses_the_setting(monkeypatch, caplog):
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", lambda: 9)
    p = LocalWhisperProvider("w", {"type": "local_whisper", "idle_unload_s": "soon"})
    with caplog.at_level("WARNING"):
        assert p._idle_unload_s() == 9.0
    assert "not a number" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_uses_one_total_bound_and_says_so_when_the_lock_is_stuck(llm, world, caplog):
    p = llm.providers[ALIAS]
    await p.transcribe("x")
    await p._load_lock.acquire()  # a load that never ends
    started = time.monotonic()
    with caplog.at_level("ERROR"):
        await asyncio.wait_for(p.shutdown_unload(timeout=0.3), 3)
    assert time.monotonic() - started < 1.5
    assert "left loaded at exit" in caplog.text
    p._load_lock.release()
