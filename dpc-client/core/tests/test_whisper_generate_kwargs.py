"""
Tests for LocalWhisperProvider generate_kwargs and timestamp handling.

These guards are what keep Whisper from inventing subtitle credits, applause
and looped tokens on non-speech audio. Pure assembly — no model is loaded.
"""

import pytest

from dpc_client_core.providers.whisper_provider import LocalWhisperProvider


def make(**config):
    config.setdefault("type", "local_whisper")
    return LocalWhisperProvider("whisper_test", config)


class TestDefaults:
    def test_guards_present_by_default(self):
        gk = make()._build_generate_kwargs()

        assert gk["compression_ratio_threshold"] == 1.35
        assert gk["logprob_threshold"] == -1.0
        assert gk["no_speech_threshold"] == 0.6

    def test_temperature_ladder_by_default(self):
        gk = make()._build_generate_kwargs()

        assert gk["temperature"] == (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

    def test_condition_on_prev_tokens_false_is_not_dropped(self):
        gk = make()._build_generate_kwargs()

        assert gk["condition_on_prev_tokens"] is False

    def test_language_auto_becomes_none(self):
        gk = make(language="auto")._build_generate_kwargs()

        assert gk["language"] is None
        assert gk["task"] == "transcribe"

    def test_explicit_language_passes_through(self):
        gk = make(language="ru")._build_generate_kwargs()

        assert gk["language"] == "ru"


class TestTemperatureSentinel:
    def test_sentinel_keeps_ladder(self):
        gk = make(temperature=0.7)._build_generate_kwargs()

        assert gk["temperature"] == (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

    def test_explicit_temperature_pins_single_value(self):
        gk = make(temperature=0.0)._build_generate_kwargs()

        assert gk["temperature"] == 0.0

    def test_empty_ladder_omits_temperature(self):
        gk = make(temperature_fallback=[])._build_generate_kwargs()

        assert "temperature" not in gk


class TestConfigOverrides:
    def test_thresholds_are_configurable(self):
        gk = make(
            compression_ratio_threshold=2.0,
            logprob_threshold=-0.5,
            no_speech_threshold=0.9,
            condition_on_prev_tokens=True,
        )._build_generate_kwargs()

        assert gk["compression_ratio_threshold"] == 2.0
        assert gk["logprob_threshold"] == -0.5
        assert gk["no_speech_threshold"] == 0.9
        assert gk["condition_on_prev_tokens"] is True

    def test_custom_ladder(self):
        gk = make(temperature_fallback=[0.0, 0.5])._build_generate_kwargs()

        assert gk["temperature"] == (0.0, 0.5)

    @pytest.mark.parametrize("key", [
        "compression_ratio_threshold", "logprob_threshold", "no_speech_threshold",
    ])
    def test_explicit_none_disables_a_guard(self, key):
        gk = make(**{key: None})._build_generate_kwargs()

        assert key not in gk


class TestSegmentsFromChunks:
    """Chunk -> segment mapping. Pure assembly, no model and no torch."""

    def test_chunks_become_segments(self):
        chunks = [
            {"timestamp": (0.0, 2.5), "text": " hello"},
            {"timestamp": (2.5, 6.0), "text": " world"},
        ]

        segments = LocalWhisperProvider._segments_from_chunks(chunks, 6.0)

        assert segments == [
            {"start": 0.0, "end": 2.5, "text": "hello"},
            {"start": 2.5, "end": 6.0, "text": "world"},
        ]

    def test_open_ended_last_chunk_gets_the_duration(self):
        chunks = [{"timestamp": (12.0, None), "text": "tail"}]

        segments = LocalWhisperProvider._segments_from_chunks(chunks, 19.5)

        assert segments == [{"start": 12.0, "end": 19.5, "text": "tail"}]

    def test_empty_and_malformed_chunks_are_dropped(self):
        chunks = [{"timestamp": (0.0, 1.0), "text": "  "}, "not-a-dict", {"text": "no stamp"}]

        segments = LocalWhisperProvider._segments_from_chunks(chunks, 4.0)

        assert segments == [{"start": 0.0, "end": 4.0, "text": "no stamp"}]

    def test_no_chunks_gives_no_segments(self):
        assert LocalWhisperProvider._segments_from_chunks(None, 3.0) == []

    def test_mlx_segments_are_mapped_to_the_same_shape(self):
        raw = [{"start": 0.0, "end": 1.25, "text": " mlx "}, {"start": 1.25, "end": None, "text": "end"}]

        segments = LocalWhisperProvider._segments_from_mlx(raw, 9.0)

        assert segments == [
            {"start": 0.0, "end": 1.25, "text": "mlx"},
            {"start": 1.25, "end": 9.0, "text": "end"},
        ]


class TestPipelineIsAskedForTimestamps:
    """The PyTorch path must call the pipeline with return_timestamps=True."""

    @pytest.mark.asyncio
    async def test_call_passes_return_timestamps_and_returns_segments(self, monkeypatch):
        import sys
        import time
        import types

        calls = {}

        class FakePipeline:
            def __call__(self, audio_array, **kwargs):
                # A measurable elapsed time: the provider logs a real-time ratio.
                time.sleep(0.01)
                calls.update(kwargs)
                return {
                    "text": " hello tail",
                    "chunks": [
                        {"timestamp": (0.0, 2.0), "text": " hello"},
                        {"timestamp": (12.0, None), "text": " tail"},
                    ],
                }

        fake_torch = types.ModuleType("torch")
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
        fake_librosa = types.ModuleType("librosa")
        fake_librosa.load = lambda path, sr=16000: ([0.0] * (16000 * 20), 16000)
        monkeypatch.setitem(sys.modules, "torch", fake_torch)
        monkeypatch.setitem(sys.modules, "librosa", fake_librosa)

        provider = make()
        provider.model_loaded = True
        provider.pipeline = FakePipeline()

        result = await provider.transcribe("any.ogg")

        assert calls["return_timestamps"] is True
        assert result["text"] == "hello tail"
        assert result["segments"] == [
            {"start": 0.0, "end": 2.0, "text": "hello"},
            {"start": 12.0, "end": 20.0, "text": "tail"},
        ]
        assert result["duration"] == 20.0
        assert result["provider"] == "local_whisper"
