"""ADR-052 slice S3d, the parts of L12 its Confirmation tests do not name.

- L12's three numbers (`cpu_max_s` 600 and `cpu_contention_factor` 1.0, item 9; `cpu_max_audio_s`
  300, Mike #71) have a code default and a getter that reads `config.ini` first, and are seeded into
  an existing file by item 7's carrier, as `idle_unload_s` and `queue_wait_s` are.
- A peer reaches the twin only when the owner lets it (`let_peers_use_cpu_twin`, Mike #71, 6C; the UI
  is S5's), and then only when the twin's estimate fits the guest's 120 s (Mike #17, L13).
- The twin is never the agent tool's default and never preloaded as if it were the main alias
  (Zcode).
- The cascade's question `asks_for_the_card` writes nothing on the provider (Ark #44-2); the call
  keeps its own detection.
- A GPU load that falls back to the CPU passes L12's RAM check, or is refused with the reason
  (3.1, Ark #30).
"""

import asyncio
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.managers.card_lease import LeaseRefusedForPeer
from dpc_client_core.managers.llama_server_supervisor import serving_a_peer
from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import (
    LocalWhisperProvider,
    asks_for_the_card,
    cpu_ram_check,
    cpu_route_settings,
    transcribe_with_route,
)
from dpc_client_core.settings import _SEEDED_KEYS, Settings

from tests._card_lease_helpers import BOUND, fast, hold_open, music, nothing_left, until  # noqa: F401
from tests._voice_cascade_helpers import busy_music  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram, l12_settings

KEYS = {"cpu_max_s": 600.0, "cpu_max_audio_s": 300.0, "cpu_contention_factor": 1.0}


class TestTheSettings:
    def test_a_fresh_config_writes_the_decided_values(self, tmp_path):
        Settings(tmp_path)
        text = (tmp_path / "config.ini").read_text()
        for key, value in KEYS.items():
            assert _SEEDED_KEYS[("local_transcription", key)][0] in (str(value), f"{value:g}")
            assert f"{key} = " in text

    def test_the_getters_read_the_defaults(self, tmp_path):
        assert Settings(tmp_path).get_local_transcription_cpu_route() == KEYS

    def test_the_file_wins_and_a_bad_value_reads_as_the_default(self, tmp_path):
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "cpu_max_s", "450")
        settings._config.set("local_transcription", "cpu_contention_factor", "-2")
        settings._config.set("local_transcription", "cpu_max_audio_s", "soon")
        assert settings.get_local_transcription_cpu_route() == {
            "cpu_max_s": 450.0, "cpu_max_audio_s": 300.0, "cpu_contention_factor": 1.0}

    def test_an_old_config_gets_the_keys_and_keeps_its_own(self, tmp_path):
        (tmp_path / "config.ini").write_text("[local_transcription]\ncpu_max_s = 120\n")
        settings = Settings(tmp_path)
        text = (tmp_path / "config.ini").read_text()
        assert "cpu_max_s = 120" in text and "cpu_max_audio_s = 300" in text
        assert "cpu_contention_factor = 1.0" in text
        assert settings.get_local_transcription_cpu_max_s() == 120.0

    def test_the_provider_reads_the_registered_source_and_falls_back(self, monkeypatch):
        assert cpu_route_settings() == KEYS, "no source: the code defaults"
        l12_settings(monkeypatch, cpu_max_s=90, cpu_max_audio_s=-1, cpu_contention_factor="x")
        assert cpu_route_settings() == {**KEYS, "cpu_max_s": 90.0}
        # 0 is the CPU route off, not the default (Mike, 2026-11-24; `test_zero_turns_the_cpu_route_off.py`).
        l12_settings(monkeypatch, cpu_max_s=90, cpu_max_audio_s=0)
        assert cpu_route_settings() == {**KEYS, "cpu_max_s": 90.0, "cpu_max_audio_s": 0.0}


@pytest.mark.usefixtures("fast")
class TestAPeer:
    @pytest.mark.asyncio
    async def test_with_the_owners_setting_a_short_file_goes_to_the_twin(self, monkeypatch, busy_music):
        host_ram(monkeypatch, 64_000)
        p = gpu_whisper(monkeypatch)
        p.config["let_peers_use_cpu_twin"] = True
        twin = cpu_twin(monkeypatch, p)
        with serving_a_peer("node-guest"):
            result = await asyncio.wait_for(transcribe_with_route(
                p, "f", purpose="peer node-guest", wait_s=None, expected_s=p.expected_seconds(20.0),
                audio_s=20.0), BOUND)
        assert result["device"] == "cpu" and twin.loads == 1
        busy_music.set()
        await nothing_left()

    @pytest.mark.asyncio
    async def test_with_the_setting_a_twin_over_120_s_is_still_refused(self, monkeypatch, busy_music):
        """Mike #17: a peer's long audio on the CPU twin is refused (300 s at ≈0.98x)."""
        host_ram(monkeypatch, 64_000)
        p = gpu_whisper(monkeypatch)
        p.config["let_peers_use_cpu_twin"] = "true"
        twin = cpu_twin(monkeypatch, p)
        with serving_a_peer("node-guest"):
            with pytest.raises(LeaseRefusedForPeer):
                await asyncio.wait_for(transcribe_with_route(
                    p, "f", purpose="peer node-guest", wait_s=None,
                    expected_s=p.expected_seconds(250.0), audio_s=250.0), BOUND)
        assert "over the guest's 120 s" in p.last_route["why"] and twin.ran == []
        busy_music.set()
        await nothing_left()


class TestTheTwinIsNeverTheDefault:
    def test_the_agent_tool_never_picks_a_twin(self, monkeypatch):
        from dpc_client_core.dpc_agent.tools.transcribe import _pick_provider

        p = gpu_whisper(monkeypatch, alias="whisper")
        twin = p._the_cpu_twin()
        # A twin under a name that sorts first: `sorted(candidates)[0]` alone would pick it.
        ctx = SimpleNamespace(dpc_service=SimpleNamespace(llm_manager=SimpleNamespace(
            providers={"a@cpu": twin, "whisper": p})))
        assert _pick_provider(ctx, None) == (p, None)
        assert _pick_provider(ctx, "a@cpu")[0] is None

    @pytest.mark.asyncio
    async def test_the_preload_never_takes_a_twin_for_the_main_alias(self, monkeypatch):
        from dpc_client_core.voice_service import VoiceService

        p = gpu_whisper(monkeypatch, alias="whisper")
        twin = p._the_cpu_twin()
        p.ensure_model_loaded = AsyncMock()
        twin.ensure_model_loaded = AsyncMock()
        svc = SimpleNamespace(llm_manager=SimpleNamespace(providers={"a@cpu": twin, "whisper": p}),
                              local_api=SimpleNamespace(broadcast_event=AsyncMock()))
        answer = await VoiceService.preload_whisper_model(svc)
        assert answer["provider"] == "whisper"
        p.ensure_model_loaded.assert_awaited_once()
        twin.ensure_model_loaded.assert_not_awaited()


class TestTheQuestionWritesNothing:
    def _auto(self, monkeypatch, detected="cuda"):
        p = LocalWhisperProvider("auto", {"type": "local_whisper", "device": "auto",
                                          "model": "openai/whisper-small"})
        seen = []
        monkeypatch.setattr(p, "_detect_device", lambda: seen.append(1) or detected)
        return p, seen

    def test_asks_for_the_card_keeps_no_detection(self, monkeypatch):
        p, seen = self._auto(monkeypatch)
        assert asks_for_the_card(p) is True
        assert p._estimate_device is None and p._detected_device is None
        assert asks_for_the_card(p) is True and seen == [1, 1], "asked again, detected again"

    def test_the_call_keeps_its_detection_and_the_question_reads_it(self, monkeypatch):
        p, seen = self._auto(monkeypatch)
        assert p.expected_seconds(30) is not None  # the door's estimate, on the call's path
        assert p._estimate_device == "cuda" and seen == [1]
        assert asks_for_the_card(p) is True and seen == [1]


class TestACpuFallbackLoad:
    def _fallback_world(self, monkeypatch):
        """`_load_model` up to `model.to(device)`, which fails as an uninitialised CUDA does."""
        import sys

        model = SimpleNamespace(to=lambda device: (_ for _ in ()).throw(RuntimeError("CUDA error: no device"))
                                if device == "cuda" else None)
        transformers = types.ModuleType("transformers")
        transformers.AutoModelForSpeechSeq2Seq = SimpleNamespace(from_pretrained=lambda *a, **k: model)
        transformers.AutoProcessor = SimpleNamespace(from_pretrained=lambda *a, **k: SimpleNamespace(
            tokenizer=None, feature_extractor=None))
        transformers.pipeline = lambda *a, **k: object()
        torch = types.ModuleType("torch")
        torch.float16, torch.float32 = "f16", "f32"
        monkeypatch.setitem(sys.modules, "transformers", transformers)
        monkeypatch.setitem(sys.modules, "torch", torch)
        from dpc_client_core.providers import model_sizes
        monkeypatch.setattr(model_sizes, "hf_cache_path", lambda: str(self.cache))
        return LocalWhisperProvider("w", {"type": "local_whisper", "device": "cuda",
                                          "model": "openai/whisper-large-v3-turbo"})

    def test_too_little_ram_refuses_the_fallback_with_the_reason(self, monkeypatch, tmp_path):
        self.cache = tmp_path
        p = self._fallback_world(monkeypatch)
        host_ram(monkeypatch, 1000.0)
        with pytest.raises(RuntimeError) as failed:
            p._load_model()
        assert "the CPU cannot take the model instead (1000 MiB of host RAM available, 5120 MiB needed)" \
            in str(failed.value)
        assert not p.model_loaded

    def test_enough_ram_lets_the_fallback_load_on_the_cpu(self, monkeypatch, tmp_path):
        self.cache = tmp_path
        p = self._fallback_world(monkeypatch)
        host_ram(monkeypatch, 64_000)
        p._load_model()
        assert p.model_loaded and p._loaded_device == "cpu"

    def test_the_check_needs_the_twin_plus_the_margin(self, monkeypatch):
        monkeypatch.setattr(whisper_provider, "available_ram_mib", lambda: 5120.0)
        assert cpu_ram_check()[0] is True
        monkeypatch.setattr(whisper_provider, "available_ram_mib", lambda: 5119.0)
        assert cpu_ram_check()[0] is False
