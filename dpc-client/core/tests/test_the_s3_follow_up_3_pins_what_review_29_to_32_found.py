"""ADR-052 S3 follow-up 3: what the reviews #29 (Zcode), #30 (Johnny), #31 (Forge) and #32 (Ark) of
the S3 branch found, pinned. (#30-4, a peer's unknown length on a free card, is pinned beside the
grant rule in test_a_peers_long_transcription_is_refused_even_on_a_free_card.py.)

- Johnny #30-7: after a card refusal an unloaded `device: auto` alias is read by its detection, so on
  a machine whose detection gives the CPU it is still tried; only a detection that fails counts as
  the card's.
- Zcode #29-3, Ark F4: with the OpenAI fallback on, the refused call's own chain has already asked
  the cloud provider and the peers; the cascade does not ask them a second time.
- Zcode #29-4, Ark (4): a card refusal is logged in its own words, not as a failure.
- Ark (6): the received-voice check is handed only to a call that asks the card.
- Forge #31-8: an LLM turn behind a warm idle Whisper gets the card within seconds of the yield,
  not after `idle_unload_s`.
- Johnny #30-6: one audio length by value: the band of the estimate is the band of the execution.
- Johnny #30-1: the queue ahead is sequential, so `_estimate` adds a going holder's stop to the work
  of the waiters ahead, and that sum is the model the line runs, not a margin.

Fake GPU, fake engines, fake inference; no torch, no model.
"""

import asyncio
import logging
import math
import sys
import time
import types
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import _CHEAP_YIELD_S
from dpc_client_core.managers.llama_server_supervisor import serving_a_peer
from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import (
    _BATCH_DESCENT,
    LocalWhisperProvider,
    asks_for_the_card,
    card_ask,
    length_band,
)
from dpc_client_core.service import _VOICE_MESSAGE_CARD_CHECK

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, swap, whisper  # noqa: F401
from tests._voice_cascade_helpers import PEER, busy_music, cloud_provider, received, speaking, voice_service  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of
from tests.test_llama_server_and_strata_wait_for_a_busy_lease_and_unload_an_idle_one import quick  # noqa: F401
from tests.test_one_local_engine_holds_the_card import _llama, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast")

REFUSAL_BOUND = 0.3


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "voice.wav"
    path.write_bytes(b"RIFF....WAVEfmt fake")
    return path


def _auto(monkeypatch, detected):
    """An unloaded `device: auto` Whisper whose detection answers `detected` (or raises it)."""
    p = LocalWhisperProvider("auto_small", {"type": "local_whisper", "device": "auto",
                                            "model": "openai/whisper-small"})

    def detect():
        if isinstance(detected, Exception):
            raise detected
        return detected

    monkeypatch.setattr(p, "_detect_device", detect)
    return p


class TestAnAutoAliasIsReadByItsDetection:
    def test_detected_cpu_does_not_ask_the_card(self, monkeypatch):
        assert asks_for_the_card(_auto(monkeypatch, "cpu")) is False

    def test_detected_cuda_asks_the_card(self, monkeypatch):
        assert asks_for_the_card(_auto(monkeypatch, "cuda")) is True

    def test_a_detection_that_fails_counts_as_the_cards(self, monkeypatch):
        assert asks_for_the_card(_auto(monkeypatch, RuntimeError("no driver answer"))) is True

    def test_the_detection_is_kept_for_the_call(self, monkeypatch):
        p = _auto(monkeypatch, "cpu")
        asks_for_the_card(p)
        assert p._estimate_device == "cpu" and not p.model_loaded, "detected, nothing loaded"

    @pytest.mark.asyncio
    async def test_after_a_refusal_an_unloaded_auto_alias_detected_on_the_cpu_is_tried(
            self, monkeypatch, audio, busy_music):
        w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
        on_auto = speaking(gpu_whisper(monkeypatch, alias="auto_small", device="auto", load_on="cpu"))
        monkeypatch.setattr(on_auto, "_detect_device", lambda: "cpu")
        svc = voice_service({"w1": w1, "auto_small": on_auto}, ["w1", "auto_small"],
                            bound=REFUSAL_BOUND, fallback=False)

        await asyncio.wait_for(received(svc, audio), BOUND)

        assert on_auto.loads == 1 and lease_of(on_auto) is None, "it ran on the CPU, with no lease"
        assert svc._voice_transcriptions["t-1"]["text"] == "heard auto_small"
        await nothing_left()


class TestTheFallbackIsNotAskedTwice:
    @pytest.mark.asyncio
    async def test_openai_once_and_the_peer_once(self, monkeypatch, audio, busy_music):
        w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
        svc = voice_service({"w1": w1, "openai": cloud_provider()},
                            ["w1", f"remote:{PEER}:whisper", "openai"], bound=REFUSAL_BOUND, fallback=True)
        svc._transcribe_with_openai = AsyncMock(side_effect=RuntimeError("the cloud is down"))
        peer_asks = []

        async def peer_step(path, mime_type, asked=None):
            peer_asks.append("fallback")
            if asked is not None:
                asked.append(PEER)
            return None

        async def remote(**kw):
            peer_asks.append("remote alias")
            raise RuntimeError("the peer is busy")

        svc._transcribe_with_peer = peer_step
        svc._request_transcription_from_peer = remote

        await asyncio.wait_for(received(svc, audio), BOUND)

        assert svc._transcribe_with_openai.await_count == 1, "OpenAI called once, not twice"
        assert peer_asks == ["fallback"], "the peer asked once, not twice"
        assert "t-1" not in svc._voice_transcriptions
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_cloud_alias_the_fallback_did_not_ask_is_still_tried(self, monkeypatch, audio, busy_music):
        w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
        svc = voice_service({"w1": w1, "openai": cloud_provider(), "other": cloud_provider("other")},
                            ["w1", "other"], bound=REFUSAL_BOUND, fallback=True)
        calls = []

        async def cloud(path, config):
            calls.append(config is svc.llm_manager.providers["other"].config)
            if len(calls) == 1:
                raise RuntimeError("the first cloud is down")
            return {"text": "the other cloud heard it", "provider": "openai", "language": "en", "duration": 3.0}

        svc._transcribe_with_openai = cloud

        await asyncio.wait_for(received(svc, audio), BOUND)

        assert calls == [False, True], "the fallback's provider, then the other alias"
        assert svc._voice_transcriptions["t-1"]["text"] == "the other cloud heard it"
        await nothing_left()


class TestACardRefusalIsLoggedInItsOwnWords:
    @pytest.mark.asyncio
    async def test_at_the_dictation_door(self, monkeypatch, audio, busy_music, caplog):
        w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
        svc = voice_service({"w1": w1}, [], bound=REFUSAL_BOUND, fallback=False)
        with caplog.at_level(logging.INFO):
            refused = await asyncio.wait_for(svc.transcribe_audio(file_path=str(audio), provider_alias="w1"), BOUND)
        lines = [r.getMessage() for r in caplog.records]
        assert refused["card_refusal"] == "LeaseWaitedTooLong"
        assert any(line.startswith("Local transcription was refused the card (LeaseWaitedTooLong)") for line in lines)
        assert not any(line.startswith("Local transcription failed") for line in lines)
        assert not any(r.levelno >= logging.ERROR for r in caplog.records), "a refusal is not an error"
        await nothing_left()

    @pytest.mark.asyncio
    async def test_at_the_end_of_the_cascade(self, monkeypatch, audio, busy_music, caplog):
        w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
        w2 = speaking(gpu_whisper(monkeypatch, alias="w2"))
        svc = voice_service({"w1": w1, "w2": w2}, ["w1", "w2"], bound=REFUSAL_BOUND, fallback=False)
        with caplog.at_level(logging.INFO):
            await asyncio.wait_for(received(svc, audio), BOUND)
        lines = [r.getMessage() for r in caplog.records]
        assert not any("All transcription providers failed" in line for line in lines)
        assert any("the card refused it (LeaseWaitedTooLong)" in line for line in lines)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_keeps_its_words(self, monkeypatch, audio, caplog):
        broken = speaking(gpu_whisper(monkeypatch, alias="broken", load_fails=True))
        svc = voice_service({"broken": broken}, [], bound=REFUSAL_BOUND, fallback=False)
        with caplog.at_level(logging.INFO):
            await asyncio.wait_for(svc.transcribe_audio(file_path=str(audio), provider_alias="broken"), BOUND)
        lines = [r.getMessage() for r in caplog.records]
        assert any(line.startswith("Local transcription failed") for line in lines)
        assert not any("refused the card" in line for line in lines)
        await nothing_left()


def _spy_on_check(monkeypatch, p, seen):
    real = p.transcribe

    async def transcribe(path, *args, **kw):
        seen.append(kw.get("check"))
        return await real(path, *args, **kw)

    monkeypatch.setattr(p, "transcribe", transcribe)


class TestTheCheckGoesOnlyWithACardAsk:
    @pytest.mark.asyncio
    async def test_a_cpu_alias_after_a_refusal_is_handed_no_check_and_the_gpu_alias_was(
            self, monkeypatch, audio, busy_music):
        w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
        on_cpu = speaking(gpu_whisper(monkeypatch, alias="small_cpu", device="cpu"))
        on_gpu_checks, on_cpu_checks = [], []
        _spy_on_check(monkeypatch, w1, on_gpu_checks)
        _spy_on_check(monkeypatch, on_cpu, on_cpu_checks)
        svc = voice_service({"w1": w1, "small_cpu": on_cpu}, ["w1", "small_cpu"],
                            bound=REFUSAL_BOUND, fallback=False)

        await asyncio.wait_for(received(svc, audio), BOUND)

        assert len(on_gpu_checks) == 1 and on_gpu_checks[0] is not None
        assert on_cpu_checks == [None], "a call that holds no card is never handed the card's check"
        assert svc._voice_transcriptions["t-1"]["text"] == "heard small_cpu"
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_check_that_fires_does_not_end_a_cpu_call(self, monkeypatch, audio):
        on_cpu = speaking(gpu_whisper(monkeypatch, alias="small_cpu", device="cpu"))
        svc = voice_service({"small_cpu": on_cpu}, [], bound=REFUSAL_BOUND, fallback=False)

        async def the_transcript_is_in():
            raise AssertionError("the card's check ran on a call that holds no card")

        token = _VOICE_MESSAGE_CARD_CHECK.set(the_transcript_is_in)
        try:
            result = await asyncio.wait_for(
                svc.transcribe_audio(file_path=str(audio), provider_alias="small_cpu"), BOUND)
        finally:
            _VOICE_MESSAGE_CARD_CHECK.reset(token)
        assert result["status"] == "success" and result["text"] == "heard small_cpu"
        await nothing_left()


class TestAnLlmTurnGetsTheCardFromAWarmIdleWhisper:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("card", "quick")
    @pytest.mark.parametrize("swap_on", [True, False], ids=["swap on", "swap off"])
    async def test_within_seconds_not_after_the_idle_unload(self, monkeypatch, swap_on):
        swap(swap_on, 30)
        monkeypatch.setattr(whisper_provider, "_idle_unload_source", None)
        p = gpu_whisper(monkeypatch, idle_s=300)
        await asyncio.wait_for(p.transcribe("warm", purpose="dictation", wait_s=BOUND), BOUND)
        assert p.model_loaded and lease_of(p).granted and not lease_of(p)._uses, "warm and idle"
        assert p._idle_unload_s() == 300

        llama = _llama()
        started = time.monotonic()
        props = await asyncio.wait_for(llama.ensure_running(), BOUND)
        took = time.monotonic() - started

        assert props == {"total_slots": 1}
        assert took < 2 * _CHEAP_YIELD_S, f"the turn had the card after {took:.2f} s"
        assert p.unloads == 1 and not p.model_loaded and not lease_of(p).granted
        assert holding() == ["qwen27"]


@pytest.fixture
def decoding(monkeypatch):
    """librosa reads the length from the file name ("a59.6.wav" is 59.6 s); torch has no CUDA."""
    librosa = types.ModuleType("librosa")
    librosa.load = lambda path, sr=16000: ([0.0] * int(sr * float(path[1:-4])), sr)
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "librosa", librosa)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", None)


def _warm_provider():
    p = LocalWhisperProvider("w", {"type": "local_whisper", "device": "cuda",
                                   "model": "openai/whisper-large-v3-turbo"})
    p.batches = []

    def pipe(audio, batch_size=None, return_timestamps=None, **kwargs):
        time.sleep(0.03)  # a wall time the loop's clock can see on every platform
        p.batches.append(batch_size)
        return {"text": "t", "chunks": []}

    p.pipeline = pipe
    p.model_loaded = True
    p._calls_since_load = 1  # not the first call: the run records a warm figure
    return p


def _batch_of(p, band):
    divisor = dict(zip((">180", "<=180", "<=120"), (d for _, d in _BATCH_DESCENT))).get(band, 1)
    return max(1, p.batch_size // divisor)


@pytest.mark.usefixtures("decoding")
class TestOneAudioLengthByValue:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("probed, decoded_file", [(60.4, "a59.6.wav"), (59.6, "a60.4.wav"),
                                                      (120.3, "a119.8.wav"), (180.2, "a179.9.wav")])
    async def test_the_band_of_the_estimate_is_the_band_of_the_execution(self, monkeypatch, probed, decoded_file):
        monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: probed)
        p = _warm_provider()
        decoded_band = length_band(float(decoded_file[1:-4]))
        estimated_band = length_band(probed)
        assert decoded_band != estimated_band, "probe and decode disagree across a band boundary"

        ask = card_ask(p, "dictation", decoded_file, bound_s=BOUND)
        await asyncio.wait_for(p.transcribe(decoded_file, **ask), BOUND)

        assert ask["audio_s"] == probed
        assert p.batches == [_batch_of(p, estimated_band)], "the batch of the estimate's band"
        assert set(p._measured["cuda"]["warm"]) == {estimated_band}, "recorded in the estimate's band"
        await nothing_left()

    @pytest.mark.asyncio
    async def test_with_no_length_read_the_decoded_one_runs_and_is_recorded(self, monkeypatch):
        monkeypatch.setattr(whisper_provider, "audio_seconds", lambda path: None)
        p = _warm_provider()
        ask = card_ask(p, "dictation", "a60.4.wav", bound_s=BOUND)
        await asyncio.wait_for(p.transcribe("a60.4.wav", **ask), BOUND)
        assert "audio_s" not in ask
        assert p.batches == [_batch_of(p, "<=120")]
        assert set(p._measured["cuda"]["warm"]) == {"<=120"}
        await nothing_left()


class _GoingSupervisor:
    """An idle supervisor being swapped out (draining), whose stop takes `stop_s`."""

    ENGINE, port, alias = "strata", None, "flash"

    def __init__(self, stop_s):
        self._proc = type("Proc", (), {"returncode": None})()
        self.props = {"total_slots": 1}
        self._draining = True
        self._expected_stop_s = lambda: stop_s

    def _busy_for_the_card(self, state, but_for=None, before=None):
        return False


class TestTheQueueAheadIsSequential:
    def test_a_going_holders_stop_and_the_waiter_ahead_add_up(self):
        """Johnny #30-1's case: a holder whose stop takes 20 s, an owner's waiter ahead whose own work
        is 60 s, then a peer. The waiter ahead has the holder among its own blockers, and `_settle`
        grants it only when nothing blocks it, so it starts after the stop and the peer after it:
        20 + 60 = 80 s (the sequential model). The parallel model, max(20, 60) = 60 s, is not the one
        the line runs."""
        sup = _GoingSupervisor(stop_s=20.0)
        base_mod._CARD_CLAIMS[sup] = "claimed"
        ahead_client, peer_client = whisper("ahead", eta=60.0), whisper("small")
        ahead = card_lease._LeaseWaiter(ahead_client.lease, "dictation", None, True, 60.0)
        card_lease._LEASE_LINE.append(ahead)
        with serving_a_peer("node-guest"):
            peer = card_lease._LeaseWaiter(peer_client.lease, "peer node-guest", None, True, 10.0)

        assert ("holder", sup, "draining") in card_lease._blockers(ahead), "the holder blocks the waiter ahead"
        estimate = card_lease._estimate(peer)
        assert estimate["known"] and math.isclose(estimate["wait_s"], 20.0 + 60.0)

        # The stop ends: the line grants the waiter ahead alone, and the peer now waits for its work.
        card_lease._LEASE_LINE.append(peer)
        del base_mod._CARD_CLAIMS[sup]
        card_lease._settle()
        assert ahead.granted and not peer.granted
        assert [kind for kind, what, _ in card_lease._blockers(peer)] == ["holder"]
        assert math.isclose(card_lease._estimate(peer)["wait_s"], 60.0)
