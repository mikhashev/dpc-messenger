"""ADR-052 slice S3a: the producer of `expected_s`.

The local Whisper provider records its measured real-time ratio per device after every
transcription, a **cold** figure for the first call after a load and a **warm** one for the calls
after it (Johnny's bench: the first call is 3-5x slower), and the time of each load. Before it has
measured anything it starts from the ADR's figures (L12's CPU table, "GPU figures on speech"); a
device the ADR gives no figure (mps, mlx) is unknown until measured (L10). `expected_seconds` turns
an audio length into wall seconds with `card_lease.seconds_from_ratio`: the load when the model is
not loaded, plus the audio divided by the cold or the warm ratio. `card_ask` is what a door passes
to `transcribe`: its purpose, its bound by L4's `min(caller's remaining - margin, bound)` and the
estimate.

The pipeline, librosa and torch are fakes; no model is loaded.
"""

import math
import sys
import types

import pytest

from dpc_client_core.managers.card_lease import seconds_from_ratio
from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import LocalWhisperProvider, card_ask

GPU_WARM, GPU_COLD, GPU_LOAD = 15.9, 8.8, 12.8
CPU_RATIO, CPU_LOAD = 300.0 / 306.0, 9.0


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    librosa = types.ModuleType("librosa")
    # The audio's length is in the file name: "a30.wav" is 30 s.
    librosa.load = lambda path, sr=16000: ([0.0] * (sr * int(path[1:-4])), sr)
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "librosa", librosa)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", None)


def _provider(device="cuda", loaded=False, work_s=0.02):
    p = LocalWhisperProvider("w", {"type": "local_whisper", "device": device})

    def pipe(audio, batch_size=None, return_timestamps=None, **kwargs):
        import time
        time.sleep(work_s)
        return {"text": "t", "chunks": []}

    p.pipeline = pipe
    if loaded:
        p.model_loaded = True
    return p


def _close(a, b):
    return a is not None and math.isclose(a, b, rel_tol=1e-9)


class TestStartFigures:
    def test_the_gpu_starts_from_the_speech_figures(self):
        p = _provider("cuda")
        assert p.speed_figures("cuda") == {"warm": GPU_WARM, "cold": GPU_COLD, "load_s": GPU_LOAD}

    def test_the_cpu_starts_from_l12s_table(self):
        p = _provider("cpu")
        figures = p.speed_figures("cpu")
        assert _close(figures["warm"], CPU_RATIO) and _close(figures["cold"], CPU_RATIO)
        assert figures["load_s"] == CPU_LOAD

    @pytest.mark.parametrize("device", ["mps", "mlx"])
    def test_a_device_the_adr_gives_no_figure_is_unknown(self, device):
        p = _provider(device)
        assert p.speed_figures(device) == {"warm": None, "cold": None, "load_s": None}
        assert p.expected_seconds(30, device=device, loaded=False) is None
        assert p.expected_seconds(30, device=device, loaded=True) is None


class TestTheEstimate:
    def test_not_loaded_counts_the_load_and_the_cold_first_call(self):
        p = _provider("cuda")
        assert _close(p.expected_seconds(30), GPU_LOAD + seconds_from_ratio(30, GPU_COLD))

    def test_loaded_but_unused_since_the_load_is_still_cold(self):
        p = _provider("cuda", loaded=True)
        assert _close(p.expected_seconds(30), seconds_from_ratio(30, GPU_COLD))

    @pytest.mark.asyncio
    async def test_after_a_call_on_the_loaded_model_it_is_warm(self):
        p = _provider("cuda", loaded=True)
        await p.transcribe("a10.wav")
        warm = p.speed_figures("cuda")["warm"]
        assert warm == GPU_WARM, "one call was cold: the warm figure is still the start value"
        assert _close(p.expected_seconds(30), seconds_from_ratio(30, warm))

    def test_the_cpu_cold_estimate_is_l12s_cold_row(self):
        # L12's table: 10 s of audio, ≈20 s cold (load + first call ≈ 9 s on top of 11 s warm).
        p = _provider("cpu")
        assert 18.0 < p.expected_seconds(10) < 21.0

    @pytest.mark.parametrize("audio_s", [None, 0, 0.0, -5, float("nan")])
    def test_an_unknown_length_is_unknown_never_zero(self, audio_s):
        assert _provider("cuda").expected_seconds(audio_s) is None

    def test_the_device_and_the_loaded_state_can_be_given(self):
        p = _provider("cuda", loaded=True)
        assert _close(p.expected_seconds(30, device="cpu", loaded=False),
                      CPU_LOAD + seconds_from_ratio(30, CPU_RATIO))

    def test_a_configured_device_needs_no_detection(self, monkeypatch):
        p = _provider("cuda")
        monkeypatch.setattr(p, "_detect_device", lambda: pytest.fail("detected"))
        assert p.expected_seconds(30) is not None

    def test_auto_is_detected_once(self, monkeypatch):
        p = _provider("auto")
        seen = []
        monkeypatch.setattr(p, "_detect_device", lambda: seen.append(1) or "cpu")
        p.expected_seconds(30)
        p.expected_seconds(60)
        assert seen == [1]


class TestRecording:
    @pytest.mark.asyncio
    async def test_the_first_call_after_a_load_is_recorded_cold_the_next_warm(self):
        p = _provider("cpu", work_s=0.05)
        p._note_load("cpu", 1.5)
        p.model_loaded = True
        await p.transcribe("a30.wav")
        figures = p.speed_figures("cpu")
        assert figures["load_s"] == 1.5
        assert figures["cold"] > 50, "30 s in about 0.05 s: the measured cold ratio replaced 0.98x"
        assert _close(figures["warm"], CPU_RATIO), "no warm call yet"
        cold = figures["cold"]

        await p.transcribe("a30.wav")
        figures = p.speed_figures("cpu")
        assert figures["warm"] > 50 and figures["cold"] == cold

    @pytest.mark.asyncio
    async def test_a_new_load_makes_the_next_call_cold_again(self):
        p = _provider("cpu", loaded=True, work_s=0.01)
        await p.transcribe("a10.wav")
        await p.transcribe("a10.wav")
        p._note_load("cpu", 0.8)
        assert _close(p.expected_seconds(30, loaded=True), seconds_from_ratio(30, p.speed_figures("cpu")["cold"]))

    @pytest.mark.asyncio
    async def test_a_ratio_measured_on_one_device_does_not_change_another(self):
        p = _provider("cpu", loaded=True, work_s=0.01)
        await p.transcribe("a30.wav")
        assert p.speed_figures("cuda") == {"warm": GPU_WARM, "cold": GPU_COLD, "load_s": GPU_LOAD}

    def test_a_load_that_fell_back_to_the_cpu_is_recorded_for_the_cpu(self):
        p = _provider("cuda")
        p._note_load("cpu", 2.0)
        p.model_loaded = True
        assert p.speed_figures("cpu")["load_s"] == 2.0
        assert p.speed_figures("cuda")["load_s"] == GPU_LOAD
        # The estimate follows the device the model is loaded on.
        assert _close(p.expected_seconds(30), seconds_from_ratio(30, CPU_RATIO))

    def test_a_zero_or_negative_figure_is_not_recorded(self):
        p = _provider("cuda")
        p._note_load("cuda", 0.0)
        p._note_run("cuda", 30.0, 0.0, cold=True)
        p._note_run("cuda", 0.0, 1.0, cold=False)
        assert p.speed_figures("cuda") == {"warm": GPU_WARM, "cold": GPU_COLD, "load_s": GPU_LOAD}

    @pytest.mark.asyncio
    async def test_the_figures_survive_a_providers_reload(self):
        old = _provider("cpu", loaded=True, work_s=0.01)
        await old.transcribe("a30.wav")
        cold = old.speed_figures("cpu")["cold"]
        new = _provider("cpu")
        old.hand_over_to(new)
        assert new.speed_figures("cpu")["cold"] == cold
        assert old.expected_seconds(30) == new.expected_seconds(30)

    def test_the_figures_live_in_memory_on_the_provider(self):
        a, b = _provider("cuda"), _provider("cuda")
        a._note_load("cuda", 1.0)
        assert b.speed_figures("cuda")["load_s"] == GPU_LOAD


class TestTheDoorsAsk:
    def test_a_persons_door_waits_its_bound_when_it_has_no_deadline_of_its_own(self):
        ask = card_ask(_provider("cuda", loaded=True), "dictation", "x.webm", bound_s=600, audio_s=30)
        assert ask["purpose"] == "dictation" and ask["wait_s"] == 600
        assert _close(ask["expected_s"], seconds_from_ratio(30, GPU_COLD))

    def test_the_queues_deadline_is_never_longer_than_the_callers(self):
        # L4: wait_s = min(caller's remaining - margin, bound); margin = max(10 s, estimate).
        p = _provider("cpu", loaded=True)
        ask = card_ask(p, "agent tool", "x.wav", caller_left_s=1800, audio_s=120)
        assert _close(ask["wait_s"], 1800 - ask["expected_s"])
        assert ask["expected_s"] > 10
        ask = card_ask(p, "agent tool", "x.wav", caller_left_s=1800, bound_s=200, audio_s=120)
        assert ask["wait_s"] == 200

    def test_a_short_estimate_keeps_the_ten_second_margin(self):
        ask = card_ask(_provider("cuda", loaded=True), "agent tool", "x.wav", caller_left_s=100, audio_s=10)
        assert ask["expected_s"] < 10 and _close(ask["wait_s"], 90.0)

    def test_an_unknown_estimate_keeps_the_ten_second_margin(self):
        ask = card_ask(_provider("mps"), "agent tool", "x.wav", caller_left_s=100, audio_s=10)
        assert ask["expected_s"] is None and _close(ask["wait_s"], 90.0)

    def test_no_bound_and_no_deadline_is_until_cancelled(self):
        ask = card_ask(_provider("cuda"), "music Transcribe", "x.flac", audio_s=200)
        assert ask["wait_s"] is None and ask["expected_s"] is not None

    def test_a_provider_without_figures_gives_an_unknown_estimate(self):
        ask = card_ask(object(), "peer node-1", "x.wav", audio_s=30)
        assert ask == {"purpose": "peer node-1", "wait_s": None, "expected_s": None}

    def test_an_unreadable_file_has_an_unknown_length(self, tmp_path):
        broken = tmp_path / "not-audio.webm"
        broken.write_bytes(b"not audio at all")
        assert whisper_provider.audio_seconds(str(broken)) is None
        assert card_ask(_provider("cuda"), "dictation", str(broken), bound_s=600)["expected_s"] is None

    def test_the_length_is_read_from_the_file_when_not_given(self, tmp_path):
        import wave
        path = tmp_path / "two.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 32000)
        assert math.isclose(whisper_provider.audio_seconds(str(path)), 2.0, rel_tol=0.01)


class TestTranscribeTakesTheAsk:
    @pytest.mark.asyncio
    async def test_purpose_bound_and_estimate_are_arguments_of_one_call(self):
        p = _provider("cpu", loaded=True)
        result = await p.transcribe("a10.wav", purpose="dictation", wait_s=600, expected_s=12.0)
        assert result["text"] == "t"

    @pytest.mark.asyncio
    async def test_a_call_through_a_replaced_object_forwards_the_ask(self, monkeypatch):
        old, new = _provider("cpu", loaded=True), _provider("cpu", loaded=True)
        old.hand_over_to(new)
        seen = {}
        original = LocalWhisperProvider.transcribe

        async def spy(self, audio_path, *args, **kw):
            if self is new:
                seen["ask"] = {k: kw.get(k) for k in ("purpose", "wait_s", "expected_s")}
            return await original(self, audio_path, *args, **kw)

        monkeypatch.setattr(LocalWhisperProvider, "transcribe", spy)
        await old.transcribe("a10.wav", purpose="peer n", wait_s=None, expected_s=3.0)
        assert seen["ask"] == {"purpose": "peer n", "wait_s": None, "expected_s": 3.0}
