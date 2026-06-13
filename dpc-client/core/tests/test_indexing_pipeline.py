"""Tests for incremental indexing pipeline (ADR-010, MEM-3.7, ADR-024 Phase 1.6b.1)."""

import numpy as np
from unittest.mock import MagicMock

from dpc_client_core.dpc_agent.indexing_pipeline import (
    index_single_file, full_rebuild, should_index,
)


def _mock_embedding_provider(dims=4):
    provider = MagicMock()
    provider.embed_batch.return_value = [[0.1] * dims]
    return provider


def _mock_backend():
    """Mock RetrievalBackend with vector/text/fuser attributes.

    Each component is itself a MagicMock — preserves the call assertions
    used in the original tests (`vector.add.called`, `vector.clear.called`).
    """
    backend = MagicMock()
    backend.vector = MagicMock()
    backend.text = MagicMock()
    backend.fuser = MagicMock()
    return backend


def test_index_single_file(tmp_path):
    (tmp_path / "topic.md").write_text("Some knowledge content here for testing", encoding="utf-8")
    provider = _mock_embedding_provider()
    provider.embed_batch.return_value = [[0.1, 0.2, 0.3, 0.4]]
    backend = _mock_backend()

    count = index_single_file(tmp_path / "topic.md", provider, backend)
    assert count >= 1
    assert provider.embed.called or provider.embed_batch.called
    assert backend.vector.add.called
    assert backend.text.add.called


def test_index_binary_file_skipped(tmp_path):
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    count = index_single_file(
        tmp_path / "image.png", _mock_embedding_provider(), _mock_backend()
    )
    assert count == 0


def test_full_rebuild(tmp_path):
    (tmp_path / "a.md").write_text("Alpha content", encoding="utf-8")
    (tmp_path / "b.md").write_text("Beta content", encoding="utf-8")
    (tmp_path / "_meta.json").write_text("{}", encoding="utf-8")

    provider = _mock_embedding_provider()
    provider.embed_batch.return_value = [[0.1, 0.2, 0.3, 0.4]]
    backend = _mock_backend()

    count = full_rebuild(tmp_path, provider, backend)
    assert count >= 2
    assert backend.vector.clear.called
    assert backend.vector.add.called
    assert backend.text.add.called


def test_debounce():
    assert should_index("test.md") is True
    assert should_index("test.md") is False


def test_full_rebuild_empty(tmp_path):
    count = full_rebuild(
        tmp_path, _mock_embedding_provider(), _mock_backend()
    )
    assert count == 0
