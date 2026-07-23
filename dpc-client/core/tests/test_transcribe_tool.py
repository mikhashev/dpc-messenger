"""
Tests for the agent transcription tool (dpc_agent/tools/transcribe.py).

Cover provider selection, path gating, output contract (.txt + path,
never inline text), and failure paths. The Whisper provider itself is
faked — these tests must not load a model.
"""

import json
import pathlib

import pytest

from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.dpc_agent.tools.transcribe import get_tools, transcribe_audio_file


class FakeWhisperProvider:
    def __init__(self, model_name="openai/whisper-large-v3-turbo", text="привет мир", language="ru"):
        self.config = {"type": "local_whisper"}
        self.model_name = model_name
        self.language = "auto"
        self.calls = []
        self._text = text
        self._language = language

    async def transcribe(self, audio_path):
        self.calls.append({"audio_path": audio_path, "language": self.language})
        return {"text": self._text, "language": self._language, "duration": 1816.28}


class FakeNonWhisperProvider:
    def __init__(self):
        self.config = {"type": "ollama"}
        self.model_name = "qwen3.6:27b"


class FakeLLMManager:
    def __init__(self, providers):
        self.providers = providers


class FakeService:
    def __init__(self, providers):
        self.llm_manager = FakeLLMManager(providers)


@pytest.fixture
def agent_root(tmp_path):
    root = tmp_path / "agent_001"
    root.mkdir()
    return root


@pytest.fixture
def audio_file(agent_root):
    path = agent_root / "voice.ogg"
    path.write_bytes(b"fake-ogg-bytes")
    return path


def make_ctx(agent_root, providers):
    return ToolContext(agent_root=agent_root, dpc_service=FakeService(providers))


class TestOutputContract:
    @pytest.mark.asyncio
    async def test_writes_txt_and_returns_path_not_inline_text(self, agent_root, audio_file):
        provider = FakeWhisperProvider(text="x" * 20000)
        ctx = make_ctx(agent_root, {"whisper": provider})

        raw = await transcribe_audio_file(ctx, "voice.ogg")
        result = json.loads(raw)

        written = pathlib.Path(result["output_path"])
        assert written.read_text(encoding="utf-8") == "x" * 20000
        assert result["chars"] == 20000
        assert len(raw) < 2000

    @pytest.mark.asyncio
    async def test_default_output_lands_in_sandbox_transcripts(self, agent_root, audio_file):
        ctx = make_ctx(agent_root, {"whisper": FakeWhisperProvider()})

        result = json.loads(await transcribe_audio_file(ctx, "voice.ogg"))

        assert pathlib.Path(result["output_path"]) == agent_root / "transcripts" / "voice.txt"

    @pytest.mark.asyncio
    async def test_reports_metadata(self, agent_root, audio_file):
        ctx = make_ctx(agent_root, {"whisper": FakeWhisperProvider()})

        result = json.loads(await transcribe_audio_file(ctx, "voice.ogg"))

        assert result["language"] == "ru"
        assert result["duration_seconds"] == 1816.3
        assert result["model"] == "openai/whisper-large-v3-turbo"
        assert result["preview"] == "привет мир"

    @pytest.mark.asyncio
    async def test_explicit_relative_output_path(self, agent_root, audio_file):
        ctx = make_ctx(agent_root, {"whisper": FakeWhisperProvider()})

        result = json.loads(await transcribe_audio_file(ctx, "voice.ogg", output_path="out/custom.txt"))

        assert pathlib.Path(result["output_path"]) == agent_root / "out" / "custom.txt"


class TestProviderSelection:
    @pytest.mark.asyncio
    async def test_picks_whisper_and_ignores_other_provider_types(self, agent_root, audio_file):
        whisper = FakeWhisperProvider()
        ctx = make_ctx(agent_root, {"ollama": FakeNonWhisperProvider(), "whisper": whisper})

        await transcribe_audio_file(ctx, "voice.ogg")

        assert len(whisper.calls) == 1

    @pytest.mark.asyncio
    async def test_selects_by_alias(self, agent_root, audio_file):
        turbo = FakeWhisperProvider(model_name="openai/whisper-large-v3-turbo")
        large = FakeWhisperProvider(model_name="openai/whisper-large-v3")
        ctx = make_ctx(agent_root, {"turbo": turbo, "large": large})

        await transcribe_audio_file(ctx, "voice.ogg", model="large")

        assert len(large.calls) == 1
        assert turbo.calls == []

    @pytest.mark.asyncio
    async def test_selects_by_model_name(self, agent_root, audio_file):
        turbo = FakeWhisperProvider(model_name="openai/whisper-large-v3-turbo")
        large = FakeWhisperProvider(model_name="openai/whisper-large-v3")
        ctx = make_ctx(agent_root, {"turbo": turbo, "large": large})

        await transcribe_audio_file(ctx, "voice.ogg", model="openai/whisper-large-v3")

        assert len(large.calls) == 1

    @pytest.mark.asyncio
    async def test_unknown_model_lists_available(self, agent_root, audio_file):
        ctx = make_ctx(agent_root, {"turbo": FakeWhisperProvider()})

        result = await transcribe_audio_file(ctx, "voice.ogg", model="nonexistent")

        assert result.startswith("⚠️")
        assert "turbo" in result

    @pytest.mark.asyncio
    async def test_no_whisper_provider_configured(self, agent_root, audio_file):
        ctx = make_ctx(agent_root, {"ollama": FakeNonWhisperProvider()})

        result = await transcribe_audio_file(ctx, "voice.ogg")

        assert result.startswith("⚠️")
        assert "local_whisper" in result


class TestLanguageOverride:
    @pytest.mark.asyncio
    async def test_language_passed_and_restored(self, agent_root, audio_file):
        provider = FakeWhisperProvider()
        ctx = make_ctx(agent_root, {"whisper": provider})

        await transcribe_audio_file(ctx, "voice.ogg", language="en")

        assert provider.calls[0]["language"] == "en"
        assert provider.language == "auto"

    @pytest.mark.asyncio
    async def test_language_restored_after_failure(self, agent_root, audio_file):
        provider = FakeWhisperProvider()

        async def boom(_path):
            raise RuntimeError("cuda oom")

        provider.transcribe = boom
        ctx = make_ctx(agent_root, {"whisper": provider})

        result = await transcribe_audio_file(ctx, "voice.ogg", language="en")

        assert result.startswith("⚠️")
        assert provider.language == "auto"


class TestPathGating:
    @pytest.mark.asyncio
    async def test_missing_file(self, agent_root):
        ctx = make_ctx(agent_root, {"whisper": FakeWhisperProvider()})

        result = await transcribe_audio_file(ctx, "nope.ogg")

        assert "File not found" in result

    @pytest.mark.asyncio
    async def test_directory_rejected(self, agent_root):
        (agent_root / "adir").mkdir()
        ctx = make_ctx(agent_root, {"whisper": FakeWhisperProvider()})

        result = await transcribe_audio_file(ctx, "adir")

        assert "Not a file" in result

    @pytest.mark.asyncio
    async def test_absolute_path_outside_sandbox_denied(self, agent_root, tmp_path):
        outside = tmp_path / "outside.ogg"
        outside.write_bytes(b"fake")
        ctx = make_ctx(agent_root, {"whisper": FakeWhisperProvider()})

        result = await transcribe_audio_file(ctx, str(outside))

        assert "Access denied" in result


class TestFailures:
    @pytest.mark.asyncio
    async def test_transcription_error_surfaced(self, agent_root, audio_file):
        provider = FakeWhisperProvider()

        async def boom(_path):
            raise RuntimeError("model load failed")

        provider.transcribe = boom
        ctx = make_ctx(agent_root, {"whisper": provider})

        result = await transcribe_audio_file(ctx, "voice.ogg")

        assert result.startswith("⚠️")
        assert "model load failed" in result

    @pytest.mark.asyncio
    async def test_empty_transcript_reported_not_written(self, agent_root, audio_file):
        ctx = make_ctx(agent_root, {"whisper": FakeWhisperProvider(text="   ")})

        result = await transcribe_audio_file(ctx, "voice.ogg")

        assert result.startswith("⚠️")
        assert not (agent_root / "transcripts").exists()


class TestRegistration:
    def test_tool_is_opt_in_with_long_timeout(self):
        entry = get_tools()[0]

        assert entry.name == "transcribe_audio_file"
        assert entry.default_enabled is False
        assert entry.timeout_sec >= 600

    def test_schema_requires_audio_path_only(self):
        schema = get_tools()[0].schema

        assert schema["parameters"]["required"] == ["audio_path"]
        assert set(schema["parameters"]["properties"]) == {
            "audio_path", "model", "language", "output_path",
        }
