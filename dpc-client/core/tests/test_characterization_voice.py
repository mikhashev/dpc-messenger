"""
Characterization tests for VoiceService extraction.

These tests capture the ACTUAL behavior of voice/Whisper methods in VoiceService
after extraction from CoreService. If any test fails, we broke something.

Per refactoring guidelines: characterization tests record current behavior
(including bugs) as a safety net — not ideal behavior.

Reference: docs/decisions/001-service-split.md
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def make_voice_service():
    """Create a minimal VoiceService for testing."""
    from dpc_client_core.voice_service import VoiceService
    from unittest.mock import MagicMock, AsyncMock

    local_api = MagicMock()
    local_api.broadcast_event = AsyncMock()
    llm_manager = MagicMock()
    llm_manager.providers = {}
    settings = MagicMock()

    # Patch _load_voice_transcription_settings to avoid disk I/O
    with patch.object(VoiceService, '_load_voice_transcription_settings', lambda self: None):
        svc = VoiceService(
            llm_manager=llm_manager,
            settings=settings,
            local_api=local_api,
        )
    return svc


class TestPreloadWhisperCharacterization:
    """Characterize current behavior of preload_whisper_model."""

    @pytest.mark.asyncio
    async def test_returns_error_when_no_whisper_provider_configured(self):
        """Current behavior: returns error dict when no local_whisper provider exists."""
        svc = make_voice_service()
        svc.llm_manager.providers = {}  # no providers

        result = await svc.preload_whisper_model()

        assert result["status"] == "error"
        assert "No local Whisper provider" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_provider_alias(self):
        """Current behavior: returns error dict when alias doesn't exist."""
        svc = make_voice_service()
        svc.llm_manager.providers = {}

        result = await svc.preload_whisper_model(provider_alias="nonexistent")

        assert result["status"] == "error"
        assert "nonexistent" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_success_when_model_already_loaded(self):
        """Current behavior: returns success with already_loaded=True if model loaded."""
        svc = make_voice_service()

        mock_provider = MagicMock()
        mock_provider.config = {"type": "local_whisper"}
        mock_provider.alias = "whisper_test"
        mock_provider.is_model_loaded = MagicMock(return_value=True)
        svc.llm_manager.providers = {"whisper_test": mock_provider}

        result = await svc.preload_whisper_model()

        assert result["status"] == "success"
        assert result["already_loaded"] is True
        assert result["provider"] == "whisper_test"

    @pytest.mark.asyncio
    async def test_unloads_other_whisper_providers_before_loading(self):
        """Current behavior: unloads other loaded Whisper providers before loading new one.
        This is the VRAM conflict fix — critical behavior to preserve after extraction.
        """
        svc = make_voice_service()

        # Provider to load
        target_provider = MagicMock()
        target_provider.config = {"type": "local_whisper"}
        target_provider.alias = "whisper_new"
        target_provider.is_model_loaded = MagicMock(return_value=False)
        target_provider.ensure_model_loaded = AsyncMock()

        # Already-loaded provider (should be unloaded)
        other_provider = MagicMock()
        other_provider.config = {"type": "local_whisper"}
        other_provider.is_model_loaded = MagicMock(return_value=True)
        other_provider.unload_model_async = AsyncMock()

        svc.llm_manager.providers = {
            "whisper_new": target_provider,
            "whisper_old": other_provider,
        }

        await svc.preload_whisper_model(provider_alias="whisper_new")

        # Other provider must have been unloaded
        other_provider.unload_model_async.assert_called_once()
        # Target provider must have been loaded
        target_provider.ensure_model_loaded.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcasts_loading_started_event(self):
        """Current behavior: broadcasts whisper_model_loading_started event to UI."""
        svc = make_voice_service()

        provider = MagicMock()
        provider.config = {"type": "local_whisper"}
        provider.alias = "whisper_test"
        provider.is_model_loaded = MagicMock(return_value=False)
        provider.ensure_model_loaded = AsyncMock()
        svc.llm_manager.providers = {"whisper_test": provider}

        await svc.preload_whisper_model()

        svc.local_api.broadcast_event.assert_any_call(
            "whisper_model_loading_started",
            {"provider": "whisper_test"}
        )

    @pytest.mark.asyncio
    async def test_broadcasts_loaded_event_on_success(self):
        """Current behavior: broadcasts whisper_model_loaded event on success."""
        svc = make_voice_service()

        provider = MagicMock()
        provider.config = {"type": "local_whisper"}
        provider.alias = "whisper_test"
        provider.is_model_loaded = MagicMock(return_value=False)
        provider.ensure_model_loaded = AsyncMock()
        svc.llm_manager.providers = {"whisper_test": provider}

        result = await svc.preload_whisper_model()

        assert result["status"] == "success"
        assert result["already_loaded"] is False
        svc.local_api.broadcast_event.assert_any_call(
            "whisper_model_loaded",
            {"provider": "whisper_test"}
        )

    @pytest.mark.asyncio
    async def test_broadcasts_failed_event_on_exception(self):
        """Current behavior: broadcasts whisper_model_loading_failed on exception."""
        svc = make_voice_service()

        provider = MagicMock()
        provider.config = {"type": "local_whisper"}
        provider.alias = "whisper_test"
        provider.is_model_loaded = MagicMock(return_value=False)
        provider.ensure_model_loaded = AsyncMock(side_effect=RuntimeError("GPU OOM"))
        svc.llm_manager.providers = {"whisper_test": provider}

        result = await svc.preload_whisper_model()

        assert result["status"] == "error"
        assert "GPU OOM" in result["error"]
        svc.local_api.broadcast_event.assert_any_call(
            "whisper_model_loading_failed",
            {"provider": "whisper_test", "error": "GPU OOM"}
        )

    @pytest.mark.asyncio
    async def test_returns_error_if_provider_lacks_ensure_model_loaded(self):
        """Current behavior: returns error if provider doesn't support ensure_model_loaded."""
        svc = make_voice_service()

        provider = MagicMock()
        provider.config = {"type": "local_whisper"}
        provider.alias = "whisper_test"
        provider.is_model_loaded = MagicMock(return_value=False)
        # No ensure_model_loaded attribute
        del provider.ensure_model_loaded

        svc.llm_manager.providers = {"whisper_test": provider}

        result = await svc.preload_whisper_model()

        assert result["status"] == "error"
        assert "pre-loading" in result["error"]
