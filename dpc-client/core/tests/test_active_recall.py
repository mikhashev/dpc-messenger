"""Tests for Active Recall hint injection (ADR-010, MEM-3.8)."""

from dpc_client_core.dpc_agent.active_recall import (
    format_recall_hints, format_hints_only, should_inject, get_recall_block,
)
from dpc_client_core.dpc_agent.hybrid_search import SearchResult


def _make_result(file="doc.md", layer="L5", text="Sample content"):
    return SearchResult(
        chunk_meta={"source_file": file, "source_layer": layer, "text": text},
        score=0.9,
        source="hybrid",
    )


def test_format_recall_hints():
    results = [_make_result("a.md", "L6"), _make_result("b.md", "L5")]
    block = format_recall_hints(results)
    assert "ACTIVE RECALL" in block
    assert "[L6] a.md" in block
    assert "[L5] b.md" in block


def test_format_hints_only():
    results = [_make_result("a.md")]
    block = format_hints_only(results)
    assert "RECALL HINTS" in block
    assert "a.md" in block
    assert "Sample content" not in block


def test_should_inject_modes():
    assert should_inject(0.0) == "full"
    assert should_inject(0.3) == "full"
    assert should_inject(0.5) == "hints"
    assert should_inject(0.6) == "hints"
    assert should_inject(0.7) == "skip"
    assert should_inject(0.9) == "skip"


def test_get_recall_block_full():
    results = [_make_result()]
    block = get_recall_block(results, context_usage_ratio=0.2)
    assert "ACTIVE RECALL" in block


def test_get_recall_block_hints():
    results = [_make_result()]
    block = get_recall_block(results, context_usage_ratio=0.55)
    assert "RECALL HINTS" in block


def test_get_recall_block_skip():
    results = [_make_result()]
    block = get_recall_block(results, context_usage_ratio=0.8)
    assert block == ""


def test_empty_results():
    assert format_recall_hints([]) == ""
    assert get_recall_block([], 0.0) == ""
