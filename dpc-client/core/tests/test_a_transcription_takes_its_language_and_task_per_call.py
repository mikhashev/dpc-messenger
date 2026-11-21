"""`language` and `task` are arguments of one transcription, never writes onto the
shared Whisper provider: a warm model honours them, two concurrent calls each get
their own, a peer's request leaves the owner's configuration alone, and a call through
a replaced provider object forwards them.

The pipeline, mlx_whisper, librosa and torch are fakes; no model is loaded.
"""

import asyncio
import base64
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import LocalWhisperProvider


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    librosa = types.ModuleType("librosa")
    # The audio's length encodes the file name ("a.wav" -> 1 s, "b.wav" -> 2 s ...).
    librosa.load = lambda path, sr=16000: ([0.0] * (sr * (ord(path[-5]) - ord("a") + 1)), sr)
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "librosa", librosa)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", None)


def _hf_provider(**config):
    config = {"type": "local_whisper", "device": "cpu", "language": "en", "task": "translate", **config}
    p = LocalWhisperProvider("w", config)
    p.calls = []

    def pipe(audio, batch_size=None, return_timestamps=None, **kwargs):
        time.sleep(0.05)  # lets two concurrent calls overlap
        p.calls.append((len(audio) // 16000, kwargs))
        return {"text": "t", "chunks": []}

    p.pipeline = pipe
    p.model_loaded = True
    return p


@pytest.mark.asyncio
async def test_a_warm_model_honours_the_language_and_task_of_the_call():
    p = _hf_provider()
    await p.transcribe("a.wav", language="ru", task="transcribe")
    assert p.calls == [(1, {"generate_kwargs": {"language": "ru", "task": "transcribe"}})]
    assert (p.language, p.task) == ("en", "translate")


@pytest.mark.asyncio
async def test_auto_means_detect_for_this_call_only():
    p = _hf_provider()
    await p.transcribe("a.wav", language="auto")
    assert p.calls[0][1] == {"generate_kwargs": {"language": None}}
    assert p.language == "en"


@pytest.mark.asyncio
async def test_without_arguments_the_pipeline_keeps_its_configured_values():
    p = _hf_provider()
    await p.transcribe("a.wav")
    assert p.calls == [(1, {})]


@pytest.mark.asyncio
async def test_two_concurrent_calls_with_different_languages_each_get_their_own():
    p = _hf_provider()
    await asyncio.gather(p.transcribe("a.wav", language="ru"), p.transcribe("b.wav", language="de"),
                         p.transcribe("c.wav"))
    by_seconds = {seconds: kw for seconds, kw in p.calls}
    assert by_seconds[1] == {"generate_kwargs": {"language": "ru"}}
    assert by_seconds[2] == {"generate_kwargs": {"language": "de"}}
    assert by_seconds[3] == {}
    assert (p.language, p.task) == ("en", "translate")


@pytest.mark.asyncio
async def test_mlx_concurrent_calls_use_their_own_language(monkeypatch):
    seen = {}
    mlx = types.ModuleType("mlx_whisper")

    def transcribe(audio_path, path_or_hf_repo=None, language=None, task=None):
        time.sleep(0.05)
        seen[audio_path] = (language, task)
        return {"text": "t", "language": language or "xx", "segments": []}

    mlx.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", mlx)
    p = LocalWhisperProvider("w", {"type": "local_whisper", "device": "mlx", "language": "en"})
    p.pipeline, p.model_loaded = "mlx", True
    await asyncio.gather(p.transcribe("a.wav", language="ru"), p.transcribe("b.wav", language="auto"),
                         p.transcribe("c.wav", task="translate"))
    assert seen["a.wav"] == ("ru", "transcribe")
    assert seen["b.wav"] == (None, "transcribe")
    assert seen["c.wav"] == ("en", "translate")
    assert p.language == "en" and p.task == "transcribe"


@pytest.mark.asyncio
async def test_a_call_through_a_replaced_object_forwards_language_and_task():
    old = _hf_provider()
    new = _hf_provider()
    old.hand_over_to(new)
    await old.transcribe("a.wav", language="fr", task="transcribe")
    assert new.calls == [(1, {"generate_kwargs": {"language": "fr", "task": "transcribe"}})]
    assert old.calls == []


class _FakeWhisper:
    def __init__(self):
        self.config = {"type": "local_whisper"}
        self.language = "en"
        self.task = "translate"
        self.calls = []
        self._frozen = True

    def __setattr__(self, name, value):
        if self.__dict__.get("_frozen") and name in ("language", "task"):
            raise AssertionError(f"the shared provider's {name} was written")
        object.__setattr__(self, name, value)

    async def transcribe(self, path, **kwargs):
        self.calls.append(kwargs)
        return {"text": "hello", "language": "ru"}


async def _peer_request(language, task=None):
    provider = _FakeWhisper()
    service = MagicMock()
    service.p2p_manager.send_message_to_peer = AsyncMock()
    service.llm_manager.providers = {"w": provider}
    service.firewall.can_request_transcription.return_value = True
    coordinator = P2PCoordinator(service)
    audio = base64.b64encode(b"RIFFxxxx").decode()
    kwargs = {} if task is None else {"task": task}
    await coordinator.handle_transcription_request("peer-1", "req-1", audio, "audio/wav",
                                                   provider="w", language=language, **kwargs)
    return provider


@pytest.mark.asyncio
async def test_a_peer_request_passes_its_values_and_leaves_the_provider_alone():
    provider = await _peer_request("ru", "transcribe")
    assert provider.calls == [{"language": "ru", "task": "transcribe"}]
    assert (provider.language, provider.task) == ("en", "translate")


@pytest.mark.asyncio
async def test_a_peer_asking_for_auto_language_does_not_override_it():
    provider = await _peer_request("auto", "transcribe")
    assert provider.calls == [{"task": "transcribe"}]


@pytest.mark.asyncio
async def test_a_peer_that_sent_no_task_leaves_the_providers_configured_task_in_force():
    provider = await _peer_request("ru")
    assert provider.calls == [{"language": "ru"}]
    assert provider.task == "translate"


@pytest.mark.asyncio
async def test_the_message_handler_passes_none_when_the_payload_has_no_task():
    from dpc_client_core.message_handlers.transcription_handler import RemoteTranscriptionRequestHandler
    service = MagicMock()
    service._handle_transcription_request = AsyncMock()
    handler = RemoteTranscriptionRequestHandler(service)
    await handler.handle("peer-1", {"request_id": "r", "audio_base64": "QQ==", "mime_type": "audio/wav"})
    assert service._handle_transcription_request.call_args[0][-1] is None
