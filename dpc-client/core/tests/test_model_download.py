"""Tests for first-use model download UX (ADR-010, MEM-3.9)."""

from dpc_client_core.dpc_agent.model_download import (
    download_status_message, notify_download_needed,
)


def test_notify_structure_when_needed():
    result = notify_download_needed("nonexistent/model-xyz")
    assert result["needed"] is True
    assert "model_name" in result
    assert "estimated_size_mb" in result
    assert "message" in result


def test_status_message_content():
    msg = download_status_message("nonexistent/model-xyz")
    assert msg is not None
    assert "embedding model" in msg
    assert "Active Recall" in msg


def test_notify_returns_dict():
    result = notify_download_needed()
    assert isinstance(result, dict)
    assert "needed" in result
