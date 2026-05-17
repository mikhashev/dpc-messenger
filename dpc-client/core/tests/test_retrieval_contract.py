"""Contract tests for retrieval ABCs (ADR-024 Phase 1.6a).

Tests every Vector/Text/HybridFuser implementation must satisfy.
Phase 1.6a runs against Native impls only. Phase 1.6b will parametrize
the same tests against Grafeo impls — same assertions, both backends pass.
"""

from __future__ import annotations

import numpy as np
import pytest

from dpc_client_core.dpc_agent.retrieval import (
    NativeHybridFuser,
    NativeTextIndex,
    NativeVectorIndex,
    TextAddItem,
    VectorAddItem,
    build_retrieval_backend,
    make_native_backend,
)


# ─────────────────────────────────────────────────────────────────────────
# VectorIndex contract
# ─────────────────────────────────────────────────────────────────────────


def test_vector_load_missing_returns_false(tmp_path):
    """Empty index contract: load() on nonexistent dir returns False."""
    idx = NativeVectorIndex(tmp_path / "missing", dimensions=4)
    assert idx.load() is False


def test_vector_empty_search_returns_empty(tmp_path):
    """Empty index contract: search() on empty index returns [], does NOT raise."""
    idx = NativeVectorIndex(tmp_path / "vec", dimensions=4)
    query = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    assert idx.search(query, top_k=5) == []


def test_vector_add_empty_is_noop(tmp_path):
    idx = NativeVectorIndex(tmp_path / "vec", dimensions=4)
    idx.add([])
    assert idx.total_items == 0


def test_vector_add_and_search(tmp_path):
    idx = NativeVectorIndex(tmp_path / "vec", dimensions=4)
    items = [
        VectorAddItem(
            vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "a.md"},
        ),
        VectorAddItem(
            vector=np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "b.md"},
        ),
    ]
    idx.add(items)
    assert idx.total_items == 2

    query = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    results = idx.search(query, top_k=2)
    assert len(results) >= 1
    assert results[0][0]["source_file"] == "a.md"


def test_vector_remove_by_source(tmp_path):
    idx = NativeVectorIndex(tmp_path / "vec", dimensions=4)
    idx.add([
        VectorAddItem(
            vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "a.md"},
        ),
        VectorAddItem(
            vector=np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "b.md"},
        ),
    ])
    removed = idx.remove_by_source("a.md")
    assert removed == 1
    assert idx.total_items == 1


def test_vector_remove_missing_returns_zero(tmp_path):
    idx = NativeVectorIndex(tmp_path / "vec", dimensions=4)
    idx.add([
        VectorAddItem(
            vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "a.md"},
        ),
    ])
    assert idx.remove_by_source("nonexistent.md") == 0


def test_vector_save_load_roundtrip(tmp_path):
    idx1 = NativeVectorIndex(tmp_path / "vec", model_name="m1", dimensions=4)
    idx1.add([
        VectorAddItem(
            vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "x.md"},
        ),
    ])
    idx1.save()

    idx2 = NativeVectorIndex(tmp_path / "vec", model_name="m1", dimensions=4)
    assert idx2.load() is True
    assert idx2.total_items == 1


def test_vector_needs_rebuild_detects_model_change(tmp_path):
    """Native impl uses FaissIndex.needs_rebuild — detects different model_name."""
    idx = NativeVectorIndex(tmp_path / "vec", model_name="model-a", dimensions=4)
    assert idx.needs_rebuild("model-b") is True
    assert idx.needs_rebuild("model-a") is False


def test_vector_clear(tmp_path):
    idx = NativeVectorIndex(tmp_path / "vec", dimensions=4)
    idx.add([
        VectorAddItem(
            vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "x.md"},
        ),
    ])
    idx.clear()
    assert idx.total_items == 0


# ─────────────────────────────────────────────────────────────────────────
# TextIndex contract
# ─────────────────────────────────────────────────────────────────────────


def test_text_load_missing_returns_false(tmp_path):
    idx = NativeTextIndex(tmp_path / "missing")
    assert idx.load() is False


def test_text_empty_search_returns_empty(tmp_path):
    """Empty index contract: search() on empty index returns []."""
    idx = NativeTextIndex(tmp_path / "txt")
    assert idx.search("anything", top_k=5) == []


def test_text_add_empty_is_noop(tmp_path):
    idx = NativeTextIndex(tmp_path / "txt")
    idx.add([])
    assert idx.total_items == 0


def test_text_add_and_search(tmp_path):
    idx = NativeTextIndex(tmp_path / "txt")
    idx.add([
        TextAddItem(
            text="grafeo knowledge graph migration",
            meta={"source_file": "grafeo.md"},
        ),
        TextAddItem(
            text="bm25 retrieval text search",
            meta={"source_file": "bm25.md"},
        ),
    ])
    assert idx.total_items == 2

    results = idx.search("grafeo migration", top_k=2)
    assert len(results) >= 1
    sources = [r[0]["source_file"] for r in results]
    assert "grafeo.md" in sources


def test_text_remove_by_source(tmp_path):
    idx = NativeTextIndex(tmp_path / "txt")
    # Three docs — rebuild after remove leaves a healthy corpus.
    # Single-doc rebuild after remove can trip an existing bm25s edge case
    # (empty vocab when tokens are filtered to nothing); not this ABC layer's bug.
    idx.add([
        TextAddItem(
            text="Python algorithm optimization profiling",
            meta={"source_file": "py.md"},
        ),
        TextAddItem(
            text="JavaScript framework benchmark suite",
            meta={"source_file": "js.md"},
        ),
        TextAddItem(
            text="Rust systems programming concurrency",
            meta={"source_file": "rs.md"},
        ),
    ])
    removed = idx.remove_by_source("py.md")
    assert removed == 1
    assert idx.total_items == 2


def test_text_remove_missing_returns_zero(tmp_path):
    idx = NativeTextIndex(tmp_path / "txt")
    idx.add([
        TextAddItem(
            text="Python algorithm benchmark",
            meta={"source_file": "x.md"},
        ),
    ])
    assert idx.remove_by_source("nonexistent.md") == 0


def test_text_save_load_roundtrip(tmp_path):
    idx1 = NativeTextIndex(tmp_path / "txt")
    idx1.add([
        TextAddItem(
            text="Python programming optimization tutorial",
            meta={"source_file": "x.md"},
        ),
    ])
    idx1.save()

    idx2 = NativeTextIndex(tmp_path / "txt")
    assert idx2.load() is True
    assert idx2.total_items == 1


def test_text_clear(tmp_path):
    idx = NativeTextIndex(tmp_path / "txt")
    idx.add([
        TextAddItem(
            text="Python algorithm tutorial",
            meta={"source_file": "x.md"},
        ),
    ])
    idx.clear()
    assert idx.total_items == 0


# ─────────────────────────────────────────────────────────────────────────
# HybridFuser contract
# ─────────────────────────────────────────────────────────────────────────


def test_fuser_empty_inputs():
    """Empty channels return empty fused list, does NOT raise."""
    fuser = NativeHybridFuser()
    assert fuser.fuse([], [], None) == []


def test_fuser_dedup_by_source():
    """Same source_file across channels collapses to one fused result."""
    fuser = NativeHybridFuser()
    vector_results = [({"source_file": "a.md", "source_layer": "L5"}, 0.9)]
    text_results = [({"source_file": "a.md", "source_layer": "L5"}, 0.8)]
    results = fuser.fuse(vector_results, text_results)
    assert len(results) == 1
    assert results[0].chunk_meta["source_file"] == "a.md"


def test_fuser_merges_three_channels():
    fuser = NativeHybridFuser()
    vector_results = [({"source_file": "a.md", "source_layer": "L5"}, 0.9)]
    text_results = [({"source_file": "b.md", "source_layer": "L5"}, 0.8)]
    graph_results = [({"source_file": "c.md", "source_layer": "L7"}, 0.7)]
    results = fuser.fuse(vector_results, text_results, graph_results)
    sources = {r.chunk_meta["source_file"] for r in results}
    assert sources == {"a.md", "b.md", "c.md"}


def test_fuser_layer_weights_applied():
    """L6 (weight 1.5) ranks above L2 (weight 0.8) at same RRF position."""
    fuser = NativeHybridFuser()
    vector_results = [
        ({"source_file": "low.md", "source_layer": "L2"}, 0.9),
        ({"source_file": "high.md", "source_layer": "L6"}, 0.9),
    ]
    results = fuser.fuse(vector_results, [])
    # high.md (L6 weight 1.5) ranked higher despite same vector score.
    # Position depends on RRF rank, but L6 weight beats L2 weight.
    sources = [r.chunk_meta["source_file"] for r in results]
    assert sources.index("high.md") < sources.index("low.md")


# ─────────────────────────────────────────────────────────────────────────
# RetrievalBackend composite + factory
# ─────────────────────────────────────────────────────────────────────────


def test_make_native_backend_returns_native_components(tmp_path):
    backend = make_native_backend(tmp_path / "idx", dimensions=4)
    assert isinstance(backend.vector, NativeVectorIndex)
    assert isinstance(backend.text, NativeTextIndex)
    assert isinstance(backend.fuser, NativeHybridFuser)


def test_factory_default_config_native(tmp_path):
    backend = build_retrieval_backend(tmp_path / "idx", config=None, dimensions=4)
    assert isinstance(backend.vector, NativeVectorIndex)
    assert isinstance(backend.text, NativeTextIndex)
    assert isinstance(backend.fuser, NativeHybridFuser)


def test_factory_explicit_native(tmp_path):
    config = {
        "retrieval_vector": "native",
        "retrieval_text": "native",
        "retrieval_fusion": "custom",
    }
    backend = build_retrieval_backend(tmp_path / "idx", config=config, dimensions=4)
    assert isinstance(backend.vector, NativeVectorIndex)


def test_factory_unknown_falls_back_to_native(tmp_path, caplog):
    """Phase 1.6a contract: unknown values log warning, return native."""
    config = {"retrieval_vector": "grafeo"}  # Grafeo not yet wired
    with caplog.at_level("WARNING"):
        backend = build_retrieval_backend(tmp_path / "idx", config=config, dimensions=4)
    assert isinstance(backend.vector, NativeVectorIndex)
    assert any("not implemented in Phase 1.6a" in rec.message for rec in caplog.records)


def test_backend_composite_load_returns_false_when_both_missing(tmp_path):
    backend = make_native_backend(tmp_path / "missing", dimensions=4)
    assert backend.load() is False


def test_backend_composite_save_load_roundtrip(tmp_path):
    backend = make_native_backend(tmp_path / "idx", dimensions=4)
    backend.vector.add([
        VectorAddItem(
            vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "x.md"},
        ),
    ])
    backend.text.add([
        TextAddItem(
            text="Python algorithm benchmark tutorial",
            meta={"source_file": "x.md"},
        ),
    ])
    backend.save()

    backend2 = make_native_backend(tmp_path / "idx", dimensions=4)
    assert backend2.load() is True
    assert backend2.vector.total_items == 1
    assert backend2.text.total_items == 1
