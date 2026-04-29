"""Tests for hybrid RRF search (ADR-010, MEM-3.6)."""

from dpc_client_core.dpc_agent.hybrid_search import reciprocal_rank_fusion, SearchResult, _file_key


def test_rrf_merges_both_sources():
    faiss = [({"source_file": "a.md", "chunk_index": 0}, 0.9)]
    bm25 = [({"source_file": "b.md", "chunk_index": 0}, 5.0)]
    results = reciprocal_rank_fusion(faiss, bm25)
    assert len(results) == 2
    files = {r.chunk_meta["source_file"] for r in results}
    assert files == {"a.md", "b.md"}


def test_rrf_boosts_overlap():
    shared = {"source_file": "overlap.md", "chunk_index": 0}
    faiss = [(shared, 0.9)]
    bm25 = [(shared, 5.0)]
    results = reciprocal_rank_fusion(faiss, bm25)
    assert len(results) == 1
    assert results[0].score > 1 / 61  # higher than single-source


def test_layer_weight_priority():
    l6 = {"source_file": "mike.md", "chunk_index": 0, "source_layer": "L6"}
    l5 = {"source_file": "ark.md", "chunk_index": 0, "source_layer": "L5"}
    faiss = [(l6, 0.5), (l5, 0.9)]  # l5 has higher cosine but lower layer
    results = reciprocal_rank_fusion(faiss, [])
    assert results[0].chunk_meta["source_layer"] == "L6"


def test_empty_inputs():
    assert reciprocal_rank_fusion([], []) == []


def test_file_key():
    assert _file_key({"source_file": "a.md"}) == "a.md"
