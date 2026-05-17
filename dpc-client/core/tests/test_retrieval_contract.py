"""Contract tests for retrieval ABCs (ADR-024 Phase 1.6).

Vector/Text contract tests are parametrized over every concrete impl so
the same assertions cover Native and Grafeo with one set of expectations.
Adding a new backend = add it to the fixture params; the whole suite runs
against it for free.

Phase 1.6a (initial): Native impls.
Phase 1.6b.2 (this): Grafeo impls join the parameter list.
"""

from __future__ import annotations

import numpy as np
import pytest

from dpc_client_core.dpc_agent.retrieval import (
    GrafeoHybridFuser,
    GrafeoTextIndex,
    GrafeoVectorIndex,
    NativeHybridFuser,
    NativeTextIndex,
    NativeVectorIndex,
    TextAddItem,
    VectorAddItem,
    build_retrieval_backend,
    make_native_backend,
)


# ─────────────────────────────────────────────────────────────────────────
# Fixtures — one factory per backend kind, parametrized into every test.
# ─────────────────────────────────────────────────────────────────────────


def _make_native_vector(tmp_path, dimensions=4):
    return NativeVectorIndex(tmp_path / "vec_native", dimensions=dimensions)


def _make_grafeo_vector(tmp_path, dimensions=4):
    return GrafeoVectorIndex(tmp_path / "vec_grafeo", dimensions=dimensions)


def _make_native_text(tmp_path):
    return NativeTextIndex(tmp_path / "txt_native")


def _make_grafeo_text(tmp_path):
    return GrafeoTextIndex(tmp_path / "txt_grafeo")


@pytest.fixture(params=["native", "grafeo"])
def vector_factory(request):
    """Returns a callable (tmp_path, dimensions=...) -> VectorIndex."""
    return _make_native_vector if request.param == "native" else _make_grafeo_vector


@pytest.fixture(params=["native", "grafeo"])
def text_factory(request):
    return _make_native_text if request.param == "native" else _make_grafeo_text


# ─────────────────────────────────────────────────────────────────────────
# VectorIndex contract — runs against both impls
# ─────────────────────────────────────────────────────────────────────────


def test_vector_load_missing_returns_false(tmp_path, vector_factory):
    """Empty index contract: load() on nonexistent dir returns False."""
    idx = vector_factory(tmp_path)
    assert idx.load() is False


def test_vector_empty_search_returns_empty(tmp_path, vector_factory):
    """Empty index contract: search() on empty index returns [], does NOT raise."""
    idx = vector_factory(tmp_path)
    query = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    assert idx.search(query, top_k=5) == []


def test_vector_add_empty_is_noop(tmp_path, vector_factory):
    idx = vector_factory(tmp_path)
    idx.add([])
    assert idx.total_items == 0


def test_vector_add_and_search(tmp_path, vector_factory):
    idx = vector_factory(tmp_path)
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


def test_vector_remove_by_source(tmp_path, vector_factory):
    idx = vector_factory(tmp_path)
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


def test_vector_remove_missing_returns_zero(tmp_path, vector_factory):
    idx = vector_factory(tmp_path)
    idx.add([
        VectorAddItem(
            vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "a.md"},
        ),
    ])
    assert idx.remove_by_source("nonexistent.md") == 0


def test_vector_save_load_roundtrip(tmp_path, vector_factory):
    idx1 = vector_factory(tmp_path)
    idx1.add([
        VectorAddItem(
            vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "x.md"},
        ),
    ])
    idx1.save()

    idx2 = vector_factory(tmp_path)
    assert idx2.load() is True
    assert idx2.total_items == 1


def test_vector_clear(tmp_path, vector_factory):
    idx = vector_factory(tmp_path)
    idx.add([
        VectorAddItem(
            vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "x.md"},
        ),
    ])
    idx.clear()
    assert idx.total_items == 0


# ─────────────────────────────────────────────────────────────────────────
# Backend-specific Vector tests (don't fit the shared contract)
# ─────────────────────────────────────────────────────────────────────────


def test_native_vector_needs_rebuild_detects_model_change(tmp_path):
    """Native impl tracks embedding-model identifier via FaissIndex header."""
    idx = NativeVectorIndex(tmp_path / "vec", model_name="model-a", dimensions=4)
    assert idx.needs_rebuild("model-b") is True
    assert idx.needs_rebuild("model-a") is False


def test_grafeo_vector_needs_rebuild_no_schema_yet(tmp_path):
    """Fresh Grafeo DB has no _RetrievalSchema node — nothing to compare against."""
    idx = GrafeoVectorIndex(tmp_path / "vec", dimensions=4)
    assert idx.needs_rebuild("any-model") is False


def test_grafeo_vector_needs_rebuild_detects_model_change(tmp_path):
    """After add() with model_name set, Schema node persists model identifier."""
    idx = GrafeoVectorIndex(tmp_path / "vec", model_name="model-a", dimensions=4)
    idx.add([
        VectorAddItem(
            vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "x.md"},
        ),
    ])
    assert idx.needs_rebuild("model-a") is False
    assert idx.needs_rebuild("model-b") is True


def test_grafeo_vector_needs_rebuild_empty_model_name_arg(tmp_path):
    """Empty model_name arg means caller doesn't know — return False."""
    idx = GrafeoVectorIndex(tmp_path / "vec", model_name="model-a", dimensions=4)
    idx.add([
        VectorAddItem(
            vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            meta={"source_file": "x.md"},
        ),
    ])
    assert idx.needs_rebuild("") is False


# ─────────────────────────────────────────────────────────────────────────
# TextIndex contract — runs against both impls
# ─────────────────────────────────────────────────────────────────────────


def test_text_load_missing_returns_false(tmp_path, text_factory):
    idx = text_factory(tmp_path)
    assert idx.load() is False


def test_text_empty_search_returns_empty(tmp_path, text_factory):
    """Empty index contract: search() on empty index returns []."""
    idx = text_factory(tmp_path)
    assert idx.search("anything", top_k=5) == []


def test_text_add_empty_is_noop(tmp_path, text_factory):
    idx = text_factory(tmp_path)
    idx.add([])
    assert idx.total_items == 0


def test_text_add_and_search(tmp_path, text_factory):
    idx = text_factory(tmp_path)
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


def test_text_remove_by_source(tmp_path, text_factory):
    idx = text_factory(tmp_path)
    # Three docs — rebuild after remove leaves a healthy corpus.
    # Single-doc rebuild after remove can trip the existing bm25s edge case
    # (empty vocab when tokens are filtered to nothing) for the Native impl.
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


def test_text_remove_missing_returns_zero(tmp_path, text_factory):
    idx = text_factory(tmp_path)
    idx.add([
        TextAddItem(
            text="Python algorithm benchmark",
            meta={"source_file": "x.md"},
        ),
    ])
    assert idx.remove_by_source("nonexistent.md") == 0


def test_text_save_load_roundtrip(tmp_path, text_factory):
    idx1 = text_factory(tmp_path)
    idx1.add([
        TextAddItem(
            text="Python programming optimization tutorial",
            meta={"source_file": "x.md"},
        ),
    ])
    idx1.save()

    idx2 = text_factory(tmp_path)
    assert idx2.load() is True
    assert idx2.total_items == 1


def test_text_clear(tmp_path, text_factory):
    idx = text_factory(tmp_path)
    idx.add([
        TextAddItem(
            text="Python algorithm tutorial",
            meta={"source_file": "x.md"},
        ),
    ])
    idx.clear()
    assert idx.total_items == 0


# ─────────────────────────────────────────────────────────────────────────
# HybridFuser contract — Native + Grafeo (Grafeo currently inherits Native math)
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture(params=[NativeHybridFuser, GrafeoHybridFuser])
def fuser(request):
    return request.param()


def test_fuser_empty_inputs(fuser):
    """Empty channels return empty fused list, does NOT raise."""
    assert fuser.fuse([], [], None) == []


def test_fuser_dedup_by_source(fuser):
    """Same source_file across channels collapses to one fused result."""
    vector_results = [({"source_file": "a.md", "source_layer": "L5"}, 0.9)]
    text_results = [({"source_file": "a.md", "source_layer": "L5"}, 0.8)]
    results = fuser.fuse(vector_results, text_results)
    assert len(results) == 1
    assert results[0].chunk_meta["source_file"] == "a.md"


def test_fuser_merges_three_channels(fuser):
    vector_results = [({"source_file": "a.md", "source_layer": "L5"}, 0.9)]
    text_results = [({"source_file": "b.md", "source_layer": "L5"}, 0.8)]
    graph_results = [({"source_file": "c.md", "source_layer": "L7"}, 0.7)]
    results = fuser.fuse(vector_results, text_results, graph_results)
    sources = {r.chunk_meta["source_file"] for r in results}
    assert sources == {"a.md", "b.md", "c.md"}


def test_fuser_layer_weights_applied(fuser):
    """L6 (weight 1.5) ranks above L2 (weight 0.8) at same RRF position."""
    vector_results = [
        ({"source_file": "low.md", "source_layer": "L2"}, 0.9),
        ({"source_file": "high.md", "source_layer": "L6"}, 0.9),
    ]
    results = fuser.fuse(vector_results, [])
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


def test_factory_explicit_grafeo(tmp_path):
    """Phase 1.6b.2: grafeo branches are wired, no fallback."""
    config = {
        "retrieval_vector": "grafeo",
        "retrieval_text": "grafeo",
        "retrieval_fusion": "grafeo",
    }
    backend = build_retrieval_backend(tmp_path / "idx", config=config, dimensions=4)
    assert isinstance(backend.vector, GrafeoVectorIndex)
    assert isinstance(backend.text, GrafeoTextIndex)
    assert isinstance(backend.fuser, GrafeoHybridFuser)


def test_factory_mixed_grafeo_vector_native_text(tmp_path):
    """Mix-and-match: e.g., Grafeo vector + native BM25 while Phase B validates."""
    config = {"retrieval_vector": "grafeo", "retrieval_text": "native"}
    backend = build_retrieval_backend(tmp_path / "idx", config=config, dimensions=4)
    assert isinstance(backend.vector, GrafeoVectorIndex)
    assert isinstance(backend.text, NativeTextIndex)


def test_factory_unknown_raises_value_error(tmp_path):
    """Phase 1.6b.2 dropped the silent fallback — unknown values must raise."""
    with pytest.raises(ValueError, match="retrieval_vector"):
        build_retrieval_backend(
            tmp_path / "idx",
            config={"retrieval_vector": "lancedb"},
            dimensions=4,
        )
    with pytest.raises(ValueError, match="retrieval_text"):
        build_retrieval_backend(
            tmp_path / "idx",
            config={"retrieval_text": "elastic"},
            dimensions=4,
        )
    with pytest.raises(ValueError, match="retrieval_fusion"):
        build_retrieval_backend(
            tmp_path / "idx",
            config={"retrieval_fusion": "weighted"},
            dimensions=4,
        )


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
