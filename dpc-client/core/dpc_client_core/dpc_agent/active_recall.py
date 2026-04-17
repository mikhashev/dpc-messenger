"""Active Recall hint injection (ADR-010, MEM-3.8).

On each user message: embed query → hybrid search → top-3 hints.
Inject hints in Block2 context with source layer label.
Budget-aware: >50% context → hints only, >70% → skip.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from .hybrid_search import SearchResult

log = logging.getLogger(__name__)

CONTEXT_THRESHOLD_HINTS_ONLY = 0.5
CONTEXT_THRESHOLD_SKIP = 0.7


def format_recall_hints(results: List[SearchResult], max_results: int = 3) -> str:
    """Format search results as markdown hints for Block2 injection."""
    if not results:
        return ""

    hints = results[:max_results]
    lines = ["", "--- ACTIVE RECALL ---"]
    for r in hints:
        meta = r.chunk_meta
        source = meta.get("source_layer", "L5")
        filename = meta.get("source_file", "unknown")
        lines.append(f"[{source}] {filename}: {_excerpt(meta, 200)}")
    lines.append("--- END RECALL ---")
    lines.append("")
    return "\n".join(lines)


def should_inject(context_usage_ratio: float) -> str:
    """Determine injection mode based on context window usage.

    Returns: 'full' | 'hints' | 'skip'
    """
    if context_usage_ratio >= CONTEXT_THRESHOLD_SKIP:
        return "skip"
    if context_usage_ratio >= CONTEXT_THRESHOLD_HINTS_ONLY:
        return "hints"
    return "full"


def format_hints_only(results: List[SearchResult], max_results: int = 3) -> str:
    """Compact format: filenames only, no excerpts."""
    if not results:
        return ""
    hints = results[:max_results]
    names = [f"[{r.chunk_meta.get('source_layer', 'L5')}] {r.chunk_meta.get('source_file', '?')}"
             for r in hints]
    return f"\n--- RECALL HINTS: {', '.join(names)} ---\n"


def get_recall_block(
    results: List[SearchResult],
    context_usage_ratio: float = 0.0,
    max_results: int = 3,
) -> str:
    """Get the appropriate recall block based on context budget."""
    mode = should_inject(context_usage_ratio)
    if mode == "skip":
        return ""
    if mode == "hints":
        return format_hints_only(results, max_results)
    return format_recall_hints(results, max_results)


def _excerpt(meta: dict, max_chars: int = 200) -> str:
    text = meta.get("text", "")
    if not text:
        ci = meta.get("chunk_index", 0)
        return f"(chunk {ci})"
    return text[:max_chars].replace("\n", " ").strip()
