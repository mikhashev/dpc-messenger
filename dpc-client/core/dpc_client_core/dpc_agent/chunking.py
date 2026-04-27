# Deprecated: see ADR-018. Whole-document indexing replaced chunking.
# Kept for backward compatibility with any external callers.
"""Text chunking for embedding pipeline (ADR-010, MEM-3.3).

Splits text into overlapping chunks sized for the embedding model's
token limit. Configurable batch_size for VRAM-safe inference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class Chunk:
    text: str
    source_file: str
    chunk_index: int
    char_start: int
    char_end: int


def chunk_text(
    text: str,
    source_file: str = "",
    max_chars: int = 1500,
    overlap_chars: int = 200,
) -> List[Chunk]:
    """Split text into overlapping chunks.

    Args:
        max_chars: ~512 tokens for e5-small (~3 chars/token).
                   For bge-m3 (8192 tokens), set ~24000.
        overlap_chars: Overlap between consecutive chunks for context continuity.
    """
    if not text:
        return []

    chunks: List[Chunk] = []
    start = 0
    idx = 0

    while start < len(text):
        end = min(start + max_chars, len(text))

        if end < len(text):
            for sep in ("\n\n", "\n", ". ", " "):
                boundary = text.rfind(sep, start + max_chars // 2, end)
                if boundary > start:
                    end = boundary + len(sep)
                    break

        chunks.append(Chunk(
            text=text[start:end],
            source_file=source_file,
            chunk_index=idx,
            char_start=start,
            char_end=end,
        ))
        idx += 1

        next_start = end - overlap_chars
        if next_start <= start:
            next_start = end
        start = next_start

    return chunks


DEFAULT_BATCH_SIZE = 32
"""Safe default for 4GB VRAM with e5-small (~118M params)."""


def batched_chunks(chunks: List[Chunk], batch_size: int = DEFAULT_BATCH_SIZE):
    """Yield chunks in VRAM-safe batches."""
    for i in range(0, len(chunks), batch_size):
        yield chunks[i:i + batch_size]
