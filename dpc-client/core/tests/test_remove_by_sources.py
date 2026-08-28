"""Dropping N sources should rebuild the index once, not N times.

Removal is priced per call, not per item: every backend here puts its whole structure
back together afterwards. On a live incremental pass one agent removed 298 sources in
505.9 s while another embedded twice as many documents, removed none, and finished in
4.0 s. The difference was entirely in the removal column.

So these tests count rebuilds, not seconds — a timing test would pass on a fast machine
and tell us nothing about the shape of the work.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from dpc_client_core.dpc_agent.bm25_index import BM25Index
from dpc_client_core.dpc_agent.faiss_index import FaissIndex

DIM = 8


def _vec(i: int) -> np.ndarray:
    v = np.zeros((1, DIM), dtype=np.float32)
    v[0, i % DIM] = 1.0
    return v


def _faiss_with(sources, tmp_path: pathlib.Path) -> FaissIndex:
    ix = FaissIndex(tmp_path / "faiss", dimensions=DIM)
    for i, s in enumerate(sources):
        ix.add(_vec(i), [{"source_file": s, "text": f"body of {s}"}])
    return ix


def _bm25_with(sources, tmp_path: pathlib.Path) -> BM25Index:
    ix = BM25Index(tmp_path / "bm25")
    ix.add([f"body of {s}" for s in sources],
           [{"source_file": s, "text": f"body of {s}"} for s in sources])
    return ix


# --------------------------------------------------------------------------
# FAISS
# --------------------------------------------------------------------------

def test_faiss_drops_every_named_source(tmp_path):
    ix = _faiss_with([f"knowledge/f{i}.md" for i in range(6)], tmp_path)

    removed = ix.remove_by_sources([f"knowledge/f{i}.md" for i in (1, 3, 5)])

    assert removed == 3
    assert ix.total_vectors == 3
    assert {c["source_file"] for c in ix._chunks} == {
        "knowledge/f0.md", "knowledge/f2.md", "knowledge/f4.md"}


def test_faiss_rebuilds_once_for_many_sources(tmp_path, monkeypatch):
    ix = _faiss_with([f"knowledge/f{i}.md" for i in range(20)], tmp_path)
    import faiss

    builds = []
    real = faiss.IndexFlatIP
    monkeypatch.setattr(faiss, "IndexFlatIP", lambda d: (builds.append(d), real(d))[1])

    ix.remove_by_sources([f"knowledge/f{i}.md" for i in range(10)])

    assert len(builds) == 1, f"one rebuild for ten sources, got {len(builds)}"


def test_faiss_keeps_the_survivors_searchable(tmp_path):
    """A rebuild that loses the vectors would still pass a count check."""
    ix = _faiss_with([f"knowledge/f{i}.md" for i in range(6)], tmp_path)
    ix.remove_by_sources(["knowledge/f1.md", "knowledge/f3.md"])

    hits = ix.search(_vec(0), top_k=1)

    assert hits and hits[0][0]["source_file"] == "knowledge/f0.md"
    assert hits[0][1] == pytest.approx(1.0, abs=1e-5)


def test_faiss_removing_everything_leaves_an_empty_index(tmp_path):
    ix = _faiss_with(["knowledge/a.md", "knowledge/b.md"], tmp_path)

    assert ix.remove_by_sources(["knowledge/a.md", "knowledge/b.md"]) == 2
    assert ix.total_vectors == 0
    assert ix.search(_vec(0), top_k=3) == []


def test_faiss_unknown_and_repeated_sources_cost_nothing(tmp_path, monkeypatch):
    ix = _faiss_with(["knowledge/a.md"], tmp_path)
    import faiss
    builds = []
    real = faiss.IndexFlatIP
    monkeypatch.setattr(faiss, "IndexFlatIP", lambda d: (builds.append(d), real(d))[1])

    assert ix.remove_by_sources(["knowledge/missing.md"]) == 0
    assert ix.remove_by_sources([]) == 0
    assert builds == []  # nothing matched, so nothing was rebuilt
    assert ix.remove_by_sources(["knowledge/a.md", "knowledge/a.md"]) == 1


# --------------------------------------------------------------------------
# BM25
# --------------------------------------------------------------------------

def test_bm25_drops_every_named_source(tmp_path):
    ix = _bm25_with([f"knowledge/f{i}.md" for i in range(6)], tmp_path)

    assert ix.remove_by_sources([f"knowledge/f{i}.md" for i in (1, 3, 5)]) == 3
    assert {m["source_file"] for m in ix._chunk_metas} == {
        "knowledge/f0.md", "knowledge/f2.md", "knowledge/f4.md"}


def test_bm25_rebuilds_the_corpus_once(tmp_path):
    ix = _bm25_with([f"knowledge/f{i}.md" for i in range(20)], tmp_path)
    builds = []
    real_build = ix.build
    ix.build = lambda texts, metas: (builds.append(len(metas)), real_build(texts, metas))[1]

    ix.remove_by_sources([f"knowledge/f{i}.md" for i in range(10)])

    assert len(builds) == 1, f"one corpus rebuild for ten sources, got {len(builds)}"


def test_bm25_keeps_the_survivors_searchable(tmp_path):
    ix = _bm25_with(["knowledge/alpha.md", "knowledge/beta.md", "knowledge/gamma.md"], tmp_path)
    ix.remove_by_sources(["knowledge/beta.md"])

    hits = ix.search("body of knowledge/alpha.md", top_k=3)

    assert hits
    assert "knowledge/beta.md" not in {m["source_file"] for m, _ in hits}


def test_bm25_removing_everything_leaves_an_empty_index(tmp_path):
    ix = _bm25_with(["knowledge/a.md"], tmp_path)

    assert ix.remove_by_sources(["knowledge/a.md"]) == 1
    assert ix._chunk_metas == []
    assert ix.search("body", top_k=3) == []


# --------------------------------------------------------------------------
# The ABC default, which every other backend inherits
# --------------------------------------------------------------------------

def test_the_abc_default_removes_each_source_exactly_once():
    """Correct but slow, and deliberately so — a backend that does not override this
    still deletes the right rows. Duplicates must not be deleted twice."""
    from dpc_client_core.dpc_agent.retrieval.base import VectorIndex

    class Counting(VectorIndex):
        def __init__(self):
            self.calls = []

        def add(self, items): ...
        def search(self, query_vector, top_k): return []
        def remove_by_source(self, source_file):
            self.calls.append(source_file)
            return 1
        def save(self): ...
        def load(self): return False
        def clear(self): ...
        @property
        def total_items(self): return 0

    ix = Counting()

    assert ix.remove_by_sources(["a", "b", "a"]) == 2
    assert ix.calls == ["a", "b"]


# --------------------------------------------------------------------------
# Grafeo — the backend the two slow agents actually run
# --------------------------------------------------------------------------

grafeo = pytest.importorskip("grafeo")

from dpc_client_core.dpc_agent.retrieval.base import TextAddItem, VectorAddItem  # noqa: E402
from dpc_client_core.dpc_agent.retrieval.grafeo import (  # noqa: E402
    GrafeoTextIndex, GrafeoVectorIndex,
)


def _meta(s: str) -> dict:
    return {"source_file": s, "source_path": f"C:/x/{s}", "source_layer": "L5",
            "heading": s, "text": f"body of {s}"}


class _SpyDB:
    """Counts index rebuilds and passes everything else through.

    The GrafeoDB handle is a Rust extension object with read-only attributes, so the
    spy goes around it rather than on it: `_get_db` is an ordinary Python method and
    is what the index calls each time.
    """

    def __init__(self, inner, calls):
        self._inner, self._calls = inner, calls

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in ("rebuild_vector_index", "rebuild_text_index"):
            def counted(*args, **kwargs):
                self._calls.append(name)
                return attr(*args, **kwargs)
            return counted
        return attr


def _spy_on(ix, monkeypatch) -> list:
    calls: list = []
    spy = _SpyDB(ix._get_db(), calls)
    monkeypatch.setattr(ix, "_get_db", lambda: spy)
    return calls


def test_grafeo_vector_drops_every_named_source_with_one_rebuild(tmp_path, monkeypatch):
    ix = GrafeoVectorIndex(tmp_path / "gv", dimensions=DIM)
    ix.add([VectorAddItem(vector=_vec(i), meta=_meta(f"knowledge/f{i}.md")) for i in range(6)])
    rebuilds = _spy_on(ix, monkeypatch)

    removed = ix.remove_by_sources([f"knowledge/f{i}.md" for i in (1, 3, 5)])

    assert removed == 3
    assert ix.total_items == 3
    assert len(rebuilds) == 1, f"one HNSW rebuild for three sources, got {len(rebuilds)}"


def test_grafeo_vector_the_old_path_rebuilds_per_source(tmp_path, monkeypatch):
    """What the batch call replaces, so the difference is asserted and not assumed."""
    ix = GrafeoVectorIndex(tmp_path / "gv_old", dimensions=DIM)
    ix.add([VectorAddItem(vector=_vec(i), meta=_meta(f"knowledge/f{i}.md")) for i in range(6)])
    rebuilds = _spy_on(ix, monkeypatch)

    for s in (f"knowledge/f{i}.md" for i in (1, 3, 5)):
        ix.remove_by_source(s)

    assert len(rebuilds) == 3


def test_grafeo_text_drops_every_named_source_with_one_rebuild(tmp_path, monkeypatch):
    ix = GrafeoTextIndex(tmp_path / "gt")
    ix.add([TextAddItem(text=f"body of knowledge/f{i}.md", meta=_meta(f"knowledge/f{i}.md"))
            for i in range(6)])
    rebuilds = _spy_on(ix, monkeypatch)

    removed = ix.remove_by_sources([f"knowledge/f{i}.md" for i in (0, 2)])

    assert removed == 2
    assert len(rebuilds) == 1


def test_grafeo_leaves_unnamed_sources_alone(tmp_path):
    ix = GrafeoVectorIndex(tmp_path / "gv2", dimensions=DIM)
    ix.add([VectorAddItem(vector=_vec(i), meta=_meta(f"knowledge/f{i}.md")) for i in range(4)])

    ix.remove_by_sources(["knowledge/f1.md", "knowledge/absent.md"])

    hits = ix.search(_vec(0), top_k=4)
    assert {m["source_file"] for m, _ in hits} <= {
        "knowledge/f0.md", "knowledge/f2.md", "knowledge/f3.md"}
    assert ix.total_items == 3


def test_grafeo_no_sources_touches_nothing(tmp_path, monkeypatch):
    ix = GrafeoVectorIndex(tmp_path / "gv3", dimensions=DIM)
    ix.add([VectorAddItem(vector=_vec(0), meta=_meta("knowledge/a.md"))])
    rebuilds = _spy_on(ix, monkeypatch)

    assert ix.remove_by_sources([]) == 0
    assert ix.remove_by_sources(["knowledge/absent.md"]) == 0
    assert rebuilds == []
    assert ix.total_items == 1
