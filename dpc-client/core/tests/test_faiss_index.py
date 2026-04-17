"""Tests for FAISS vector index (ADR-010, MEM-3.4)."""

import numpy as np
import pytest

from dpc_client_core.dpc_agent.faiss_index import FaissIndex, IndexHeader


def test_add_and_search(tmp_path):
    idx = FaissIndex(tmp_path, model_name="test", dimensions=4)
    vectors = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32)
    metas = [{"file": "a.md", "chunk": 0}, {"file": "b.md", "chunk": 0}]
    idx.add(vectors, metas)
    assert idx.total_vectors == 2

    query = np.array([1, 0, 0, 0], dtype=np.float32)
    results = idx.search(query, top_k=1)
    assert len(results) == 1
    assert results[0][0]["file"] == "a.md"


def test_save_and_load(tmp_path):
    idx = FaissIndex(tmp_path, model_name="test-model", dimensions=3)
    vectors = np.array([[1, 0, 0]], dtype=np.float32)
    idx.add(vectors, [{"file": "doc.md"}])
    idx.save()

    idx2 = FaissIndex(tmp_path)
    assert idx2.load() is True
    assert idx2.total_vectors == 1
    assert idx2._header.model_name == "test-model"


def test_needs_rebuild():
    idx = FaissIndex(None, model_name="model-a")
    assert idx.needs_rebuild("model-b") is True
    assert idx.needs_rebuild("model-a") is False


def test_search_empty(tmp_path):
    idx = FaissIndex(tmp_path, dimensions=4)
    query = np.array([1, 0, 0, 0], dtype=np.float32)
    assert idx.search(query) == []


def test_clear(tmp_path):
    idx = FaissIndex(tmp_path, dimensions=3)
    idx.add(np.array([[1, 0, 0]], dtype=np.float32), [{"file": "x.md"}])
    idx.clear()
    assert idx.total_vectors == 0


def test_load_missing(tmp_path):
    idx = FaissIndex(tmp_path)
    assert idx.load() is False
