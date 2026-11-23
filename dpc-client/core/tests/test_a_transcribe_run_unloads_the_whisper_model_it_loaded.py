"""A lyrics Transcribe run unloads the local Whisper model when the run loaded it
(Mike, 2026-11-20): with success or an error. A model already loaded before the run
(voice auto-transcribe keeps it warm) stays loaded, and so does one that another
transcription is using when the run ends.

The command layer is tested against a fake provider; the provider's own use counter and
its unload paths (CUDA, CPU, MLX) against `LocalWhisperProvider` with the model load,
the inference and the `torch` / `mlx` modules mocked. No model is loaded.
"""

import asyncio
import sys
import types

import pytest

from dpc_client_core.music.commands import MusicCommandsMixin
from dpc_client_core.music.library import MusicLibraryError
from dpc_client_core.providers.whisper_provider import LocalWhisperProvider


# -- the command layer -------------------------------------------------------------------

class _Settings:
    def __init__(self, home):
        self.dpc_home_dir = home

    def get(self, section, key, fallback=None):
        return fallback

    def get_local_transcription_enabled(self):
        return True

    def get_local_transcription_max_file_size_mb(self):
        return 25


class FakeWhisper:
    """A local_whisper provider: `transcribe` loads the model when it is not loaded."""

    def __init__(self, loaded=False, error=None, busy=False):
        self.alias = "w"
        self.config = {"type": "local_whisper"}
        self.model_name = "openai/whisper-large-v3-turbo"
        self.loaded = loaded
        self.error = error
        self.busy = busy  # another transcription is using the model when the run ends
        self.unload_calls = 0

    def is_model_loaded(self):
        return self.loaded

    async def transcribe(self, path, release_if_loaded_here=False, **card_ask):
        loaded_here = not self.loaded
        self.loaded = True
        try:
            if self.error is not None:
                raise self.error
            return {"text": "a", "language": "en", "duration": 1.0, "provider": "local_whisper",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "a"}]}
        finally:
            if release_if_loaded_here and loaded_here:
                try:
                    await self.unload_if_idle()
                except Exception:
                    pass  # the real provider logs it

    async def unload_if_idle(self):
        self.unload_calls += 1
        if self.busy:
            return False
        self.loaded = False
        return True


class FakeLlm:
    def __init__(self, providers):
        self.providers = providers
        self.voice_provider = None


class _Service(MusicCommandsMixin):
    def __init__(self, home, provider):
        self.settings = _Settings(home)
        self.llm_manager = FakeLlm({"w": provider})
        self.model_download_service = None


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "song.mp3"
    path.write_bytes(b"ID3" + b"\x00" * 64)
    return path


@pytest.mark.asyncio
async def test_a_run_that_loaded_the_model_unloads_it(tmp_path, audio):
    whisper = FakeWhisper(loaded=False)
    result = await _Service(tmp_path, whisper)._music_transcribe_audio(audio)
    assert result["text"] == "a"
    assert whisper.unload_calls == 1 and whisper.loaded is False


@pytest.mark.asyncio
async def test_a_failed_run_that_loaded_the_model_unloads_it_too(tmp_path, audio):
    whisper = FakeWhisper(loaded=False, error=RuntimeError("boom"))
    with pytest.raises(MusicLibraryError) as e:
        await _Service(tmp_path, whisper)._music_transcribe_audio(audio)
    assert e.value.code == "transcription_failed"
    assert whisper.unload_calls == 1 and whisper.loaded is False


@pytest.mark.asyncio
async def test_a_model_loaded_before_the_run_stays_loaded(tmp_path, audio):
    whisper = FakeWhisper(loaded=True)
    await _Service(tmp_path, whisper)._music_transcribe_audio(audio)
    assert whisper.unload_calls == 0 and whisper.loaded is True


@pytest.mark.asyncio
async def test_a_model_in_use_by_another_transcription_stays_loaded(tmp_path, audio):
    whisper = FakeWhisper(loaded=False, busy=True)
    await _Service(tmp_path, whisper)._music_transcribe_audio(audio)
    assert whisper.unload_calls == 1 and whisper.loaded is True


@pytest.mark.asyncio
async def test_a_failing_unload_does_not_change_the_answer(tmp_path, audio):
    whisper = FakeWhisper(loaded=False)

    async def broken():
        raise RuntimeError("unload broke")
    whisper.unload_if_idle = broken
    result = await _Service(tmp_path, whisper)._music_transcribe_audio(audio)
    assert result["text"] == "a"


@pytest.mark.asyncio
async def test_the_real_provider_releases_only_a_model_its_own_call_loaded(monkeypatch):
    p = _provider(monkeypatch)
    await p.transcribe("a", release_if_loaded_here=True)
    assert p.unloads == 1 and not p.is_model_loaded()
    await p.transcribe("warm")  # loads, stays loaded
    await p.transcribe("b", release_if_loaded_here=True)  # was loaded already
    assert p.unloads == 1 and p.is_model_loaded()


# -- the provider: the use counter ---------------------------------------------------------

def _provider(monkeypatch, gate=None):
    p = LocalWhisperProvider("w", {"type": "local_whisper", "device": "cpu", "idle_unload_s": 0})
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


@pytest.mark.asyncio
async def test_the_provider_unloads_when_idle(monkeypatch):
    p = _provider(monkeypatch)
    assert await p.unload_if_idle() is False  # nothing loaded
    await p.transcribe("x")
    assert p.is_model_loaded() and p._active_transcriptions == 0
    assert await p.unload_if_idle() is True
    assert p.unloads == 1 and not p.is_model_loaded()


@pytest.mark.asyncio
async def test_the_provider_does_not_unload_during_a_transcription(monkeypatch):
    gate = asyncio.Event()
    p = _provider(monkeypatch, gate)
    voice = asyncio.create_task(p.transcribe("voice"))

    async def running():
        while p._active_transcriptions != 1:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(running(), 2)
    assert await p.unload_if_idle() is False
    assert p.unloads == 0 and p.is_model_loaded()
    gate.set()
    assert (await voice) == {"text": "voice"}
    assert p._active_transcriptions == 0
    assert await p.unload_if_idle() is True


@pytest.mark.asyncio
async def test_a_failed_transcription_releases_its_use(monkeypatch):
    p = _provider(monkeypatch)

    async def fail(path):
        raise RuntimeError("inference failed")
    monkeypatch.setattr(p, "_transcribe_loaded", fail)
    with pytest.raises(RuntimeError):
        await p.transcribe("x")
    assert p._active_transcriptions == 0
    assert await p.unload_if_idle() is True


@pytest.mark.asyncio
async def test_a_transcription_started_during_an_unload_waits_and_reloads(monkeypatch):
    p = _provider(monkeypatch)
    await p.transcribe("first")
    loads = []
    real_load = p._load_model

    def counting_load():
        loads.append(1)
        real_load()
    monkeypatch.setattr(p, "_load_model", counting_load)
    unload = asyncio.create_task(p.unload_if_idle())
    await asyncio.sleep(0)  # the unload holds the lock now
    second = asyncio.create_task(p.transcribe("second"))
    assert await unload is True
    assert (await second) == {"text": "second"}
    assert loads == [1] and p.is_model_loaded()


# -- the provider: what an unload frees on each backend ------------------------------------

def _fake_torch(cuda: bool):
    torch = types.ModuleType("torch")
    torch.emptied = 0

    def empty_cache():
        torch.emptied += 1
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda, empty_cache=empty_cache)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
    return torch


class _FakeModel:
    def __init__(self, device):
        self.device = device
        self.moved = []

    def to(self, device):
        self.moved.append(device)


def test_a_cuda_unload_moves_the_weights_off_and_empties_the_cache(monkeypatch):
    torch = _fake_torch(cuda=True)
    monkeypatch.setitem(sys.modules, "torch", torch)
    p = LocalWhisperProvider("w", {"type": "local_whisper"})
    model = _FakeModel("cuda:0")
    p.pipeline = types.SimpleNamespace(model=model)
    p.model_loaded = True
    p._unload_model()
    assert model.moved == ["cpu"] and torch.emptied == 1
    assert p.pipeline is None and p.model_loaded is False


def test_a_cpu_unload_drops_the_pipeline(monkeypatch):
    torch = _fake_torch(cuda=False)
    monkeypatch.setitem(sys.modules, "torch", torch)
    p = LocalWhisperProvider("w", {"type": "local_whisper", "device": "cpu"})
    model = _FakeModel("cpu")
    p.pipeline = types.SimpleNamespace(model=model)
    p.model_loaded = True
    p._unload_model()
    assert model.moved == [] and torch.emptied == 0
    assert p.pipeline is None and p.model_loaded is False


def test_an_mlx_unload_drops_mlx_whispers_cached_model(monkeypatch):
    holder = type("ModelHolder", (), {"model": object(), "model_path": "openai/whisper"})
    transcribe_mod = types.ModuleType("mlx_whisper.transcribe")
    transcribe_mod.ModelHolder = holder
    mlx_whisper = types.ModuleType("mlx_whisper")
    mlx_whisper.transcribe = transcribe_mod
    cleared = []
    core = types.ModuleType("mlx.core")
    core.clear_cache = lambda: cleared.append(1)
    mlx = types.ModuleType("mlx")
    mlx.core = core
    for name, mod in {"mlx_whisper": mlx_whisper, "mlx_whisper.transcribe": transcribe_mod,
                      "mlx": mlx, "mlx.core": core}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    p = LocalWhisperProvider("w", {"type": "local_whisper", "device": "mlx"})
    p.pipeline = "mlx"
    p.model_loaded = True
    p._unload_model()
    assert holder.model is None and holder.model_path is None and cleared == [1]
    assert p.pipeline is None and p.model_loaded is False


def test_an_mlx_unload_without_mlx_installed_still_drops_the_reference(monkeypatch):
    for name in ("mlx_whisper", "mlx_whisper.transcribe", "mlx", "mlx.core"):
        monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError
    p = LocalWhisperProvider("w", {"type": "local_whisper", "device": "mlx"})
    p.pipeline = "mlx"
    p.model_loaded = True
    p._unload_model()
    assert p.pipeline is None and p.model_loaded is False
