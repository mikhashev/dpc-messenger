"""Duration-based local Whisper limit (Wave 2 Card 6).

Replaces the 25 MB OpenAI upload cap copied onto `[local_transcription] max_file_size_mb`
with `[local_transcription] max_duration_seconds`:
- Empty / unset (`""`, default until a cap is chosen): preserves legacy `max_file_size_mb`
  (25 MB) behavior on both music Transcribe and the voice local-vs-cloud switch.
- `0`: unlimited (no duration or file-size limit for local Whisper).
- Positive seconds (e.g. `600`): gates by audio duration rather than file size, so large
  lossless tracks/recordings under the duration cap run locally while compressed long
  recordings over the cap are refused for music Transcribe and routed to cloud for voice.
- Invalid/negative values: warn and fall back safely to legacy behavior without crashing.
"""

import base64
import configparser
import struct
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.music.commands import MusicCommandsMixin
from dpc_client_core.music.library import MusicLibraryError
from dpc_client_core.providers import whisper_provider as wp
from dpc_client_core.service import CoreService
from dpc_client_core.settings import _SEEDED_KEYS, Settings


def _write_pcm_wav(path: Path, *, duration_s: float, sample_rate: int = 48000, channels: int = 2) -> Path:
    """Create a valid PCM WAV whose real size and header reflect `duration_s`."""
    total_frames = int(round(duration_s * sample_rate))
    frame_bytes = channels * 2  # 16-bit PCM
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        chunk = b"\x00" * (min(total_frames, sample_rate) * frame_bytes)
        remaining = total_frames
        while remaining > 0:
            n = min(remaining, sample_rate)
            wf.writeframesraw(chunk[: n * frame_bytes])
            remaining -= n
    return path


def _write_fake_flac_with_size(path: Path, *, size_bytes: int, duration_s: float, sample_rate: int = 44100) -> Path:
    """Create a FLAC header with STREAMINFO reporting `duration_s`, padded to `size_bytes`."""
    total_samples = int(round(duration_s * sample_rate))
    channels = 2
    bits_per_sample = 16
    bits = (
        ((sample_rate & 0xFFFFF) << 44)
        | (((channels - 1) & 0x7) << 41)
        | (((bits_per_sample - 1) & 0x1F) << 36)
        | (total_samples & 0xFFFFFFFFF)
    )
    streaminfo = struct.pack(">HH3s3sQ", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00", bits) + (b"\x00" * 16)
    block_header = bytes([0x80]) + (34).to_bytes(3, "big")
    head = b"fLaC" + block_header + streaminfo
    pad = max(0, size_bytes - len(head))
    path.write_bytes(head + b"\x00" * pad)
    return path


@pytest.fixture(scope="module")
def large_3min_wav(tmp_path_factory) -> Path:
    """Real >30 MB, 180 s PCM WAV file (exceeds 25 MB and 10 MB, well under 600 s)."""
    root = tmp_path_factory.mktemp("card6_wav")
    wav = _write_pcm_wav(root / "lossless_3min.wav", duration_s=180.0, sample_rate=48000, channels=2)
    assert wav.stat().st_size > 30 * 1024 * 1024
    return wav


class _FakeLocalWhisper:
    def __init__(self, alias: str = "w"):
        self.alias = alias
        self.config = {"type": "local_whisper"}
        self.model_name = "openai/whisper-large-v3-turbo"
        self.language = "auto"
        self.calls = []
        self.asks = []

    def is_model_loaded(self) -> bool:
        return True

    def expected_seconds(self, audio_s):
        return None if audio_s is None else float(audio_s) / 10.0

    async def transcribe(self, path, release_if_loaded_here=False, **kwargs):
        self.calls.append(path)
        self.asks.append(dict(kwargs))
        return {
            "text": "hello world",
            "language": "en",
            "duration": kwargs.get("audio_s") or 180.0,
            "provider": "local_whisper",
            "segments": [{"start": 0.0, "end": 2.0, "text": "hello world"}],
        }


class _FakeCloudProvider:
    def __init__(self, alias: str = "openai"):
        self.alias = alias
        self.config = {"type": "openai", "api_key_env": "OPENAI_API_KEY"}


def _music_service(home: Path, settings: Settings, whisper: _FakeLocalWhisper) -> MusicCommandsMixin:
    svc = MusicCommandsMixin.__new__(MusicCommandsMixin)
    svc.settings = settings
    svc.llm_manager = SimpleNamespace(providers={whisper.alias: whisper}, voice_provider=None)
    svc.model_download_service = None
    return svc


def _voice_service(settings: Settings, providers: dict) -> tuple[CoreService, AsyncMock]:
    svc = CoreService.__new__(CoreService)
    svc.settings = settings
    svc.llm_manager = SimpleNamespace(providers=providers, voice_provider=None)
    svc.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    svc._voice_transcription_settings = {}
    svc._voice_transcriptions = {}
    svc._transcription_locks = {}
    svc.p2p_manager = SimpleNamespace(node_id="dpc-node-" + "a" * 32, peers={})
    svc.model_download_service = None
    cloud_mock = AsyncMock(return_value={
        "text": "cloud transcript",
        "language": "en",
        "duration": 0,
        "provider": "openai",
    })
    svc._transcribe_with_openai = cloud_mock
    svc._transcribe_with_peer = AsyncMock(return_value=None)
    svc._peer_transcription_refusal = lambda: "no peer connected"
    svc._check_transcription_capability = AsyncMock(return_value=True)
    svc._broadcast_voice_transcription = AsyncMock()
    svc._get_or_create_conversation_monitor = lambda key: None
    return svc, cloud_mock


# -- 1. Settings --------------------------------------------------------------------------


class TestMaxDurationSecondsSettings:
    def test_fresh_config_writes_empty_max_duration_seconds(self, tmp_path):
        s = Settings(tmp_path)
        parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
        parser.read(tmp_path / "config.ini", encoding="utf-8")
        assert parser["local_transcription"]["max_duration_seconds"] == ""
        assert s.get_local_transcription_max_duration_seconds() is None
        assert ("local_transcription", "max_duration_seconds") in _SEEDED_KEYS

    def test_existing_config_seeds_empty_max_duration_seconds_and_keeps_other_keys(self, tmp_path):
        (tmp_path / "config.ini").write_text(
            "[local_transcription]\ndevice = cpu\nmax_file_size_mb = 25\n",
            encoding="utf-8",
        )
        s = Settings(tmp_path)
        parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
        parser.read(tmp_path / "config.ini", encoding="utf-8")
        assert parser["local_transcription"]["max_duration_seconds"] == ""
        assert parser["local_transcription"]["device"] == "cpu"
        assert s.get_local_transcription_max_duration_seconds() is None

    def test_positive_zero_and_env_override(self, tmp_path, monkeypatch):
        s = Settings(tmp_path)
        s._config.set("local_transcription", "max_duration_seconds", "600")
        assert s.get_local_transcription_max_duration_seconds() == 600.0

        s._config.set("local_transcription", "max_duration_seconds", "0")
        assert s.get_local_transcription_max_duration_seconds() == 0.0

        monkeypatch.setenv("DPC_LOCAL_TRANSCRIPTION_MAX_DURATION_SECONDS", "1800")
        assert s.get_local_transcription_max_duration_seconds() == 1800.0

        monkeypatch.setenv("DPC_LOCAL_TRANSCRIPTION_MAX_DURATION_SECONDS", "")
        assert s.get_local_transcription_max_duration_seconds() is None

    @pytest.mark.parametrize("raw", ["-10", "-0.5", "abc", "nan", "inf", "-inf", "none"])
    def test_invalid_values_warn_and_fall_back_safely(self, tmp_path, raw, caplog):
        s = Settings(tmp_path)
        s._config.set("local_transcription", "max_duration_seconds", raw)
        assert s.get_local_transcription_max_duration_seconds() is None
        assert any("max_duration_seconds" in r.getMessage() for r in caplog.records)


# -- 2. Music Transcribe ------------------------------------------------------------------


class TestMusicTranscribeDurationLimit:
    @pytest.mark.asyncio
    async def test_large_lossless_file_under_duration_cap_is_accepted(self, tmp_path, large_3min_wav):
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "600")
        whisper = _FakeLocalWhisper()
        svc = _music_service(tmp_path, settings, whisper)

        result = await svc._music_transcribe_audio(large_3min_wav)

        assert result["text"] == "hello world"
        assert whisper.calls == [str(large_3min_wav)]

    @pytest.mark.asyncio
    async def test_large_flac_under_duration_cap_works_even_without_ffprobe_or_soundfile(
        self, tmp_path, monkeypatch
    ):
        flac = _write_fake_flac_with_size(
            tmp_path / "song.flac", size_bytes=31 * 1024 * 1024, duration_s=180.0
        )
        monkeypatch.setattr(wp, "audio_seconds", lambda path: None)
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "600")
        whisper = _FakeLocalWhisper()
        svc = _music_service(tmp_path, settings, whisper)

        result = await svc._music_transcribe_audio(flac)

        assert result["text"] == "hello world"
        assert whisper.calls == [str(flac)]

    @pytest.mark.asyncio
    async def test_small_file_exceeding_duration_cap_is_refused_with_duration_wording(
        self, tmp_path, monkeypatch
    ):
        mp3 = tmp_path / "long_compressed.mp3"
        mp3.write_bytes(b"ID3" + b"\x00" * (10 * 1024 * 1024))
        monkeypatch.setattr(wp, "audio_seconds", lambda path: 900.0)
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "600")
        whisper = _FakeLocalWhisper()
        svc = _music_service(tmp_path, settings, whisper)

        with pytest.raises(MusicLibraryError) as exc:
            await svc._music_transcribe_audio(mp3)

        assert exc.value.code == "too_long"
        assert "900" in exc.value.message and "600" in exc.value.message
        assert "max_duration_seconds" in exc.value.message
        assert whisper.calls == []

    @pytest.mark.asyncio
    async def test_zero_max_duration_is_unlimited_for_music(self, tmp_path, large_3min_wav, monkeypatch):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: 3600.0)
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "0")
        whisper = _FakeLocalWhisper()
        svc = _music_service(tmp_path, settings, whisper)

        result = await svc._music_transcribe_audio(large_3min_wav)

        assert result["text"] == "hello world"
        assert whisper.calls == [str(large_3min_wav)]

    @pytest.mark.asyncio
    async def test_empty_max_duration_preserves_legacy_25mb_cap(self, tmp_path, large_3min_wav):
        settings = Settings(tmp_path)
        assert settings.get_local_transcription_max_duration_seconds() is None
        whisper = _FakeLocalWhisper()
        svc = _music_service(tmp_path, settings, whisper)

        with pytest.raises(MusicLibraryError) as exc:
            await svc._music_transcribe_audio(large_3min_wav)

        assert exc.value.code == "too_large"
        assert "max_file_size_mb" in exc.value.message
        assert whisper.calls == []

    @pytest.mark.asyncio
    async def test_invalid_max_duration_falls_back_to_legacy_25mb_without_crash(self, tmp_path, large_3min_wav):
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "invalid-value")
        whisper = _FakeLocalWhisper()
        svc = _music_service(tmp_path, settings, whisper)

        with pytest.raises(MusicLibraryError) as exc:
            await svc._music_transcribe_audio(large_3min_wav)

        assert exc.value.code == "too_large"
        assert whisper.calls == []

    @pytest.mark.asyncio
    async def test_duration_gate_probes_audio_seconds_at_most_once(self, tmp_path, monkeypatch):
        mp3 = tmp_path / "track.mp3"
        mp3.write_bytes(b"ID3" + b"\x00" * 64)
        probes = []
        monkeypatch.setattr(wp, "audio_seconds", lambda p: probes.append(p) or 180.0)
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "600")
        whisper = _FakeLocalWhisper()
        svc = _music_service(tmp_path, settings, whisper)

        await svc._music_transcribe_audio(mp3)

        assert len(probes) == 1
        assert whisper.asks[0]["expected_s"] == pytest.approx(18.0)

    @pytest.mark.asyncio
    async def test_unprobeable_duration_falls_back_safely_to_max_file_size_mb(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: None)
        small_mp3 = tmp_path / "small.mp3"
        small_mp3.write_bytes(b"not-a-real-mp3")
        big_mp3 = tmp_path / "big.mp3"
        big_mp3.write_bytes(b"not-a-real-mp3" + b"\x00" * (26 * 1024 * 1024))
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "600")
        whisper = _FakeLocalWhisper()
        svc = _music_service(tmp_path, settings, whisper)

        res = await svc._music_transcribe_audio(small_mp3)
        assert res["text"] == "hello world"

        with pytest.raises(MusicLibraryError) as exc:
            await svc._music_transcribe_audio(big_mp3)
        assert exc.value.code == "too_large"


# -- 3. Voice local-vs-cloud switch -------------------------------------------------------


class TestVoiceTranscriptionDurationSwitch:
    @pytest.mark.asyncio
    async def test_large_lossless_file_under_duration_cap_runs_local_whisper(self, tmp_path, large_3min_wav):
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "600")
        whisper = _FakeLocalWhisper("w")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"w": whisper, "openai": cloud})

        result = await svc.transcribe_audio(file_path=str(large_3min_wav))

        assert result["status"] == "success"
        assert result["provider"] == "local_whisper"
        assert whisper.calls == [str(large_3min_wav)]
        cloud_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_small_compressed_file_over_duration_cap_routes_to_cloud(self, tmp_path, monkeypatch):
        mp3 = tmp_path / "long_voice.mp3"
        mp3.write_bytes(b"ID3" + b"\x00" * (10 * 1024 * 1024 - 3))
        monkeypatch.setattr(wp, "audio_seconds", lambda path: 900.0)
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "600")
        whisper = _FakeLocalWhisper("w")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"w": whisper, "openai": cloud})

        result = await svc.transcribe_audio(file_path=str(mp3))

        assert result["status"] == "success"
        assert result["provider"] == "openai"
        assert whisper.calls == []
        cloud_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_voice_metadata_duration_routes_over_cap_webm_to_cloud(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: None)
        webm = tmp_path / "voice.webm"
        webm.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 1024)
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "600")
        settings._config.set("voice_transcription", "sender_transcribes", "true")
        whisper = _FakeLocalWhisper("w")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"w": whisper, "openai": cloud})

        await svc._maybe_transcribe_voice_message(
            transfer_id="t-long",
            node_id="dpc-node-" + "b" * 32,
            file_path=webm,
            voice_metadata={"mime_type": "audio/webm", "duration_seconds": 900.0},
            is_sender=True,
        )

        assert whisper.calls == []
        cloud_mock.assert_awaited_once()
        assert svc._voice_transcriptions["t-long"]["provider"] == "openai"

    @pytest.mark.asyncio
    async def test_voice_metadata_large_lossless_under_cap_runs_local_whisper(self, tmp_path, large_3min_wav):
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "600")
        settings._config.set("voice_transcription", "recipient_delay_seconds", "0")
        whisper = _FakeLocalWhisper("w")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"w": whisper, "openai": cloud})

        await svc._maybe_transcribe_voice_message(
            transfer_id="t-wav",
            node_id="dpc-node-" + "b" * 32,
            file_path=large_3min_wav,
            voice_metadata={"mime_type": "audio/wav", "duration_seconds": 180.0},
            is_sender=False,
            allow_model_load=True,
        )

        assert len(whisper.calls) == 1
        cloud_mock.assert_not_awaited()
        assert svc._voice_transcriptions["t-wav"]["provider"] == "local_whisper"

    @pytest.mark.asyncio
    async def test_zero_max_duration_allows_large_lossless_on_local_whisper(
        self, tmp_path, large_3min_wav, monkeypatch
    ):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: 3600.0)
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "0")
        whisper = _FakeLocalWhisper("w")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"w": whisper, "openai": cloud})

        result = await svc.transcribe_audio(file_path=str(large_3min_wav))

        assert result["status"] == "success"
        assert result["provider"] == "local_whisper"
        assert whisper.calls == [str(large_3min_wav)]
        cloud_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_max_duration_preserves_legacy_25mb_switch_to_cloud(
        self, tmp_path, large_3min_wav
    ):
        settings = Settings(tmp_path)
        # Raise transport cap above 35 MB so the legacy 25 MB local_transcription switch is exercised.
        settings._config.set("voice_messages", "max_size_mb", "50")
        assert settings.get_local_transcription_max_duration_seconds() is None
        whisper = _FakeLocalWhisper("w")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"w": whisper, "openai": cloud})

        result = await svc.transcribe_audio(file_path=str(large_3min_wav))

        assert result["status"] == "success"
        assert result["provider"] == "openai"
        assert whisper.calls == []
        cloud_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_max_duration_falls_back_to_legacy_25mb_switch_without_crash(
        self, tmp_path, large_3min_wav
    ):
        settings = Settings(tmp_path)
        settings._config.set("voice_messages", "max_size_mb", "50")
        settings._config.set("local_transcription", "max_duration_seconds", "-42")
        whisper = _FakeLocalWhisper("w")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"w": whisper, "openai": cloud})

        result = await svc.transcribe_audio(file_path=str(large_3min_wav))

        assert result["status"] == "success"
        assert result["provider"] == "openai"
        assert whisper.calls == []
        cloud_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_voice_duration_gate_probes_audio_seconds_at_most_once(self, tmp_path, monkeypatch):
        mp3 = tmp_path / "voice.mp3"
        mp3.write_bytes(b"ID3" + b"\x00" * 64)
        probes = []
        monkeypatch.setattr(wp, "audio_seconds", lambda p: probes.append(p) or 120.0)
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "600")
        whisper = _FakeLocalWhisper("w")
        svc, _ = _voice_service(settings, {"w": whisper})

        result = await svc.transcribe_audio(file_path=str(mp3))

        assert result["status"] == "success"
        assert len(probes) == 1
        assert whisper.asks[0]["expected_s"] == pytest.approx(12.0)


# -- 4. Voice legacy fallback & explicit local_whisper provider_alias -----------------------


class TestVoiceLegacyAndExplicitProviderDuration:
    @pytest.mark.asyncio
    async def test_unset_max_duration_rejects_12mb_voice_in_auto_mode(self, tmp_path):
        settings = Settings(tmp_path)
        assert settings.get_local_transcription_max_duration_seconds() is None
        flac_12mb = _write_fake_flac_with_size(
            tmp_path / "voice_12mb.flac", size_bytes=12 * 1024 * 1024, duration_s=60.0
        )
        whisper = _FakeLocalWhisper("local_whisper")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"local_whisper": whisper, "openai": cloud})

        with pytest.raises(ValueError, match="Audio too large"):
            await svc.transcribe_audio(file_path=str(flac_12mb))

        payload_b64 = base64.b64encode(b"\x00" * (12 * 1024 * 1024)).decode("ascii")
        with pytest.raises(ValueError, match="Audio too large"):
            await svc.transcribe_audio(audio_base64=payload_b64, mime_type="audio/webm")

        assert whisper.calls == []
        cloud_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unset_max_duration_rejects_explicit_local_whisper_over_10mb_and_25mb(
        self, tmp_path, large_3min_wav
    ):
        settings = Settings(tmp_path)
        assert settings.get_local_transcription_max_duration_seconds() is None
        flac_12mb = _write_fake_flac_with_size(
            tmp_path / "voice_12mb.flac", size_bytes=12 * 1024 * 1024, duration_s=60.0
        )
        whisper = _FakeLocalWhisper("local_whisper")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"local_whisper": whisper, "openai": cloud})

        for path in (flac_12mb, large_3min_wav):
            for alias in ("local_whisper", "local:local_whisper"):
                with pytest.raises(ValueError, match="Audio too large"):
                    await svc.transcribe_audio(file_path=str(path), provider_alias=alias)

        assert whisper.calls == []
        cloud_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_positive_max_duration_allows_15mb_short_recording_on_explicit_local_whisper(
        self, tmp_path, monkeypatch
    ):
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "120")
        flac_15mb = _write_fake_flac_with_size(
            tmp_path / "voice_15mb_60s.flac", size_bytes=15 * 1024 * 1024, duration_s=60.0
        )
        probes = []
        monkeypatch.setattr(wp, "audio_seconds", lambda p: probes.append(p) or 60.0)
        whisper = _FakeLocalWhisper("local_whisper")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"local_whisper": whisper, "openai": cloud})

        result = await svc.transcribe_audio(
            file_path=str(flac_15mb), provider_alias="local_whisper"
        )

        assert result["status"] == "success"
        assert result["provider"] == "local_whisper"
        assert whisper.calls == [str(flac_15mb)]
        assert len(probes) == 1
        assert whisper.asks[0]["expected_s"] == pytest.approx(6.0)
        cloud_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_positive_max_duration_rejects_180s_recording_on_explicit_local_whisper(
        self, tmp_path, monkeypatch
    ):
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "120")
        flac_15mb = _write_fake_flac_with_size(
            tmp_path / "voice_15mb_180s.flac", size_bytes=15 * 1024 * 1024, duration_s=180.0
        )
        monkeypatch.setattr(wp, "audio_seconds", lambda p: 180.0)
        whisper = _FakeLocalWhisper("local_whisper")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"local_whisper": whisper, "openai": cloud})

        for alias in ("local_whisper", "local:local_whisper"):
            result = await svc.transcribe_audio(
                file_path=str(flac_15mb), provider_alias=alias
            )
            assert result["status"] == "error"
            assert "180" in result["error"]
            assert "120" in result["error"]
            assert "max_duration_seconds" in result["error"]

        assert whisper.calls == []
        cloud_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_zero_max_duration_allows_explicit_local_whisper_regardless_of_size_or_duration(
        self, tmp_path, large_3min_wav, monkeypatch
    ):
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "0")
        probes = []
        monkeypatch.setattr(wp, "audio_seconds", lambda p: probes.append(p) or 3600.0)
        whisper = _FakeLocalWhisper("local_whisper")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"local_whisper": whisper, "openai": cloud})

        result = await svc.transcribe_audio(
            file_path=str(large_3min_wav), provider_alias="local_whisper"
        )

        assert result["status"] == "success"
        assert result["provider"] == "local_whisper"
        assert whisper.calls == [str(large_3min_wav)]
        cloud_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_openai_provider_still_enforces_byte_limit_when_max_duration_set(
        self, tmp_path
    ):
        flac_15mb = _write_fake_flac_with_size(
            tmp_path / "voice_15mb_60s.flac", size_bytes=15 * 1024 * 1024, duration_s=60.0
        )
        for setting in ("120", "0"):
            settings = Settings(tmp_path / setting)
            settings._config.set("local_transcription", "max_duration_seconds", setting)
            whisper = _FakeLocalWhisper("local_whisper")
            cloud = _FakeCloudProvider("openai")
            svc, cloud_mock = _voice_service(settings, {"local_whisper": whisper, "openai": cloud})

            result = await svc.transcribe_audio(
                file_path=str(flac_15mb), provider_alias="openai"
            )

            assert result["status"] == "error"
            assert "Audio too large" in result["error"]
            cloud_mock.assert_not_awaited()
            assert whisper.calls == []

    @pytest.mark.asyncio
    async def test_voice_metadata_cascade_falls_back_to_cloud_when_named_local_alias_exceeds_duration(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: None)
        webm = tmp_path / "voice.webm"
        webm.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 1024)
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "120")
        settings._config.set("voice_transcription", "sender_transcribes", "true")
        settings._config.set("voice_transcription", "provider_priority", "local_whisper,openai")
        whisper = _FakeLocalWhisper("local_whisper")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"local_whisper": whisper, "openai": cloud})

        await svc._maybe_transcribe_voice_message(
            transfer_id="t-named-long",
            node_id="dpc-node-" + "b" * 32,
            file_path=webm,
            voice_metadata={"mime_type": "audio/webm", "duration_seconds": 180.0},
            is_sender=True,
        )

        assert whisper.calls == []
        cloud_mock.assert_awaited_once()
        assert svc._voice_transcriptions["t-named-long"]["provider"] == "openai"

    @pytest.mark.asyncio
    async def test_voice_cascade_duration_fallback_emits_no_transcription_failed_toast(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: None)
        webm = tmp_path / "voice.webm"
        webm.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 1024)
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "120")
        settings._config.set("voice_transcription", "sender_transcribes", "true")
        settings._config.set("voice_transcription", "provider_priority", "local_whisper,openai")
        whisper = _FakeLocalWhisper("local_whisper")
        cloud = _FakeCloudProvider("openai")
        svc, cloud_mock = _voice_service(settings, {"local_whisper": whisper, "openai": cloud})

        await svc._maybe_transcribe_voice_message(
            transfer_id="t-cascade-no-toast",
            node_id="dpc-node-" + "b" * 32,
            file_path=webm,
            voice_metadata={"mime_type": "audio/webm", "duration_seconds": 180.0},
            is_sender=True,
        )

        assert whisper.calls == []
        cloud_mock.assert_awaited_once()
        assert svc._voice_transcriptions["t-cascade-no-toast"]["provider"] == "openai"
        error_toasts = [
            c.args[1]
            for c in svc.local_api.broadcast_event.await_args_list
            if c.args and c.args[0] == "error_toast"
        ]
        assert error_toasts == []

    @pytest.mark.asyncio
    async def test_voice_cascade_final_duration_failure_still_emits_transcription_failed_toast(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: None)
        webm = tmp_path / "voice.webm"
        webm.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 1024)
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "max_duration_seconds", "120")
        settings._config.set("voice_transcription", "sender_transcribes", "true")
        settings._config.set("voice_transcription", "provider_priority", "local_whisper")
        whisper = _FakeLocalWhisper("local_whisper")
        svc, _ = _voice_service(settings, {"local_whisper": whisper})

        await svc._maybe_transcribe_voice_message(
            transfer_id="t-cascade-final-fail",
            node_id="dpc-node-" + "b" * 32,
            file_path=webm,
            voice_metadata={"mime_type": "audio/webm", "duration_seconds": 180.0},
            is_sender=True,
        )

        assert "t-cascade-final-fail" not in svc._voice_transcriptions
        error_toasts = [
            c.args[1]
            for c in svc.local_api.broadcast_event.await_args_list
            if c.args and c.args[0] == "error_toast"
        ]
        assert len(error_toasts) == 1
        assert error_toasts[0]["title"] == "Transcription Failed"
        assert "180" in error_toasts[0]["message"]
