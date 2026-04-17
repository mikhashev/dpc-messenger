"""Tests for incremental indexing pipeline (ADR-010, MEM-3.7)."""

import numpy as np
from unittest.mock import MagicMock

from dpc_client_core.dpc_agent.indexing_pipeline import (
    index_single_file, full_rebuild, should_index,
)


def _mock_embedding_provider(dims=4):
    provider = MagicMock()
    provider.embed_batch.return_value = [[0.1] * dims]
    return provider


def _mock_faiss_index():
    idx = MagicMock()
    idx.clear = MagicMock()
    return idx


def _mock_bm25_index():
    return MagicMock()


def test_index_single_file(tmp_path):
    (tmp_path / "topic.md").write_text("Some knowledge content here for testing", encoding="utf-8")
    provider = _mock_embedding_provider()
    provider.embed_batch.return_value = [[0.1, 0.2, 0.3, 0.4]]
    faiss_idx = _mock_faiss_index()
    bm25_idx = _mock_bm25_index()

    count = index_single_file(tmp_path / "topic.md", provider, faiss_idx, bm25_idx)
    assert count >= 1
    assert provider.embed_batch.called
    assert faiss_idx.add.called
    assert bm25_idx.build.called


def test_index_binary_file_skipped(tmp_path):
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    count = index_single_file(
        tmp_path / "image.png", _mock_embedding_provider(),
        _mock_faiss_index(), _mock_bm25_index()
    )
    assert count == 0


def test_full_rebuild(tmp_path):
    (tmp_path / "a.md").write_text("Alpha content", encoding="utf-8")
    (tmp_path / "b.md").write_text("Beta content", encoding="utf-8")
    (tmp_path / "_meta.json").write_text("{}", encoding="utf-8")

    provider = _mock_embedding_provider()
    provider.embed_batch.return_value = [[0.1, 0.2, 0.3, 0.4]]
    faiss_idx = _mock_faiss_index()
    bm25_idx = _mock_bm25_index()

    count = full_rebuild(tmp_path, provider, faiss_idx, bm25_idx)
    assert count >= 2
    assert faiss_idx.clear.called


def test_debounce():
    assert should_index("test.md") is True
    assert should_index("test.md") is False


def test_full_rebuild_empty(tmp_path):
    count = full_rebuild(
        tmp_path, _mock_embedding_provider(),
        _mock_faiss_index(), _mock_bm25_index()
    )
    assert count == 0
