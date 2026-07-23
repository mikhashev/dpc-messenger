"""
Tests for LocalWhisperProvider._build_generate_kwargs.

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
