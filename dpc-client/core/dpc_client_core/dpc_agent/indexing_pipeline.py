"""Incremental indexing pipeline (ADR-010, MEM-3.7).

Triggers: write_file(knowledge/), approved commit (L6), Extended Paths mtime change.
Embed + index one file per event. Full rebuild if cache empty/corrupted.
"""

from __future__ import annotations

import logging
import pathlib
import time
from typing import Dict, List, Optional

import numpy as np

from .text_extract import extract_text, is_binary
from .chunking import chunk_text, batched_chunks, Chunk, DEFAULT_BATCH_SIZE
from .memory import read_all_meta, write_file_meta, read_file_meta, FileMeta, _BACKFILL_SKIP

log = logging.getLogger(__name__)

_DEBOUNCE_WINDOW = 0.1
_last_index_time: Dict[str, float] = {}


def should_index(filepath: str) -> bool:
    now = time.monotonic()
    last = _last_index_time.get(filepath, 0)
    if now - last < _DEBOUNCE_WINDOW:
        return False
    _last_index_time[filepath] = now
    return True


def index_single_file(
    path: pathlib.Path,
    embedding_provider,
    faiss_index,
    bm25_index,
    source_layer: str = "L5",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Extract, chunk, embed, and index a single file. Returns chunk count."""
    text = extract_text(path)
    if not text:
        return 0

    chunks = chunk_text(text, source_file=path.name)
    if not chunks:
        return 0

    chunk_metas = [
        {"source_file": c.source_file, "chunk_index": c.chunk_index,
         "char_start": c.char_start, "char_end": c.char_end,
         "source_layer": source_layer,
         "text": c.text[:200]}
        for c in chunks
    ]

    for batch in batched_chunks(chunks, batch_size):
        texts = [c.text for c in batch]
        vectors = np.array(embedding_provider.embed_batch(texts), dtype=np.float32)
        batch_metas = chunk_metas[batch[0].chunk_index:batch[0].chunk_index + len(batch)]
        faiss_index.add(vectors, batch_metas)

    bm25_index.add(
        [c.text for c in chunks],
        chunk_metas,
    )

    return len(chunks)


def full_rebuild(
    knowledge_dir: pathlib.Path,
    embedding_provider,
    faiss_index,
    bm25_index,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Full rebuild of both indexes from all files in knowledge_dir."""
    faiss_index.clear()
    all_chunks: List[Chunk] = []
    all_metas: List[dict] = []

    if not knowledge_dir.is_dir():
        return 0

    for f in sorted(knowledge_dir.iterdir()):
        if not f.is_file() or f.name in _BACKFILL_SKIP or is_binary(f):
            continue
        text = extract_text(f)
        if not text:
            continue
        chunks = chunk_text(text, source_file=f.name)
        meta = read_file_meta(knowledge_dir, f.name)
        for c in chunks:
            all_chunks.append(c)
            all_metas.append({
                "source_file": c.source_file, "chunk_index": c.chunk_index,
                "char_start": c.char_start, "char_end": c.char_end,
                "source_layer": meta.source_layer,
                "text": c.text[:200],
            })

    if not all_chunks:
        return 0

    for batch in batched_chunks(all_chunks, batch_size):
        texts = [c.text for c in batch]
        vectors = np.array(embedding_provider.embed_batch(texts), dtype=np.float32)
        start_idx = batch[0].chunk_index
        faiss_index.add(vectors, all_metas[start_idx:start_idx + len(batch)])

    bm25_index.build([c.text for c in all_chunks], all_metas)

    log.info("Full rebuild: %d chunks from %d files",
             len(all_chunks), len({c.source_file for c in all_chunks}))
    return len(all_chunks)
