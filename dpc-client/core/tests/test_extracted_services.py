"""Tests for KnowledgeService and AgentService extracted methods (Phase C).

Covers: get_personal_context, save_personal_context, reload_personal_context,
get_session_archive_info, clear_session_archives.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dpc_client_core.agent_service import AgentService


def make_agent_service(*, max_sessions: int = 0, preserve_on_reset: bool = True):
    """Create an AgentService with mocked dependencies.

    `max_sessions` / `preserve_on_reset` configure the value returned by
    `firewall.get_history_settings(...)`. Tests that override the profile
    via `svc.firewall.rules` should also call
    `svc.firewall.get_history_settings.return_value = (preserve, max)`
    to match the firewall helper they expect to be invoked.
    """
    llm_manager = MagicMock()
    local_api = MagicMock()
    local_api.broadcast_event = AsyncMock()
    firewall = MagicMock()
    firewall.rules = {}
    firewall.get_history_settings.return_value = (preserve_on_reset, max_sessions)
    peer_metadata = {}

    svc = AgentService(llm_manager, local_api, firewall, peer_metadata)
    return svc


# ─────────────────────────────────────────────────────────────
# get_session_archive_info
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_archive_info_no_dir(tmp_path):
    svc = make_agent_service()

    with patch("pathlib.Path.home", return_value=tmp_path):
        result = await svc.get_session_archive_info("agent_001")

    assert result["status"] == "success"
    assert result["count"] == 0
    assert result["sessions"] == []


@pytest.mark.asyncio
async def test_archive_info_with_sessions(tmp_path):
    svc = make_agent_service()
    archive_dir = tmp_path / ".dpc" / "conversations" / "agent_001" / "archive"
    archive_dir.mkdir(parents=True)

    session_data = {
        "archived_at": "2026-05-01T10:00:00",
        "session_reason": "reset",
        "message_count": 42,
    }
    (archive_dir / "2026-05-01T10-00-00_reset_session.json").write_text(
        json.dumps(session_data), encoding="utf-8"
    )

    with patch("pathlib.Path.home", return_value=tmp_path):
        result = await svc.get_session_archive_info("agent_001")

    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["sessions"][0]["message_count"] == 42


@pytest.mark.asyncio
async def test_archive_info_per_agent_max_sessions():
    """Per-agent profile override is read via firewall.get_history_settings."""
    svc = make_agent_service(max_sessions=50)
    svc.firewall.rules = {
        "agent_profiles": {
            "agent_001": {
                "history": {"max_archived_sessions": 50}
            }
        }
    }

    with patch("pathlib.Path.home", return_value=Path("/fake")):
        result = await svc.get_session_archive_info("agent_001")

    assert result["max_sessions"] == 50
    # Sanity: agent_service delegated the per-agent lookup to the firewall helper.
    svc.firewall.get_history_settings.assert_called_with("agent_001")


# ───────���─────────────────────────────��───────────────────────
# clear_session_archives
# ─────────────────────────────────────��───────────────────────

@pytest.mark.asyncio
async def test_clear_archives_empty_dir(tmp_path):
    svc = make_agent_service()

    with patch("pathlib.Path.home", return_value=tmp_path):
        result = await svc.clear_session_archives("agent_001")

    assert result["status"] == "success"
    assert result["deleted_count"] == 0


@pytest.mark.asyncio
async def test_clear_archives_keeps_latest(tmp_path):
    svc = make_agent_service()
    archive_dir = tmp_path / ".dpc" / "conversations" / "agent_001" / "archive"
    archive_dir.mkdir(parents=True)

    for i in range(5):
        (archive_dir / f"2026-05-0{i+1}T10-00-00_reset_session.json").write_text(
            json.dumps({"archived_at": f"2026-05-0{i+1}"}), encoding="utf-8"
        )

    with patch("pathlib.Path.home", return_value=tmp_path):
        result = await svc.clear_session_archives("agent_001", keep_latest=2)

    assert result["status"] == "success"
    assert result["deleted_count"] == 3
    assert result["remaining"] == 2
