"""Whole-document indexing pipeline (ADR-010 + ADR-018).

One embedding per file (no chunking). BGE-M3's 8192-token window covers
all DPC knowledge files (0.5-5KB each).

Triggers: write_file(knowledge/), approved commit (L6), Extended Paths mtime change.
Full rebuild if model/dimensions change (detected by FaissIndex.needs_rebuild).
"""

from __future__ import annotations

import logging
import pathlib
import re
import time
from typing import Dict, List, Optional

import numpy as np

from .text_extract import extract_text, is_binary
from .memory import read_all_meta, write_file_meta, read_file_meta, FileMeta, _BACKFILL_SKIP

log = logging.getLogger(__name__)

_DEBOUNCE_WINDOW = 0.1
_last_index_time: Dict[str, float] = {}

DEFAULT_BATCH_SIZE = 32


def should_index(filepath: str) -> bool:
    now = time.monotonic()
    last = _last_index_time.get(filepath, 0)
    if now - last < _DEBOUNCE_WINDOW:
        return False
    _last_index_time[filepath] = now
    return True


def _extract_heading(text: str) -> str:
    """Extract first markdown heading from text."""
    match = re.search(r'^#+ (.+)$', text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _build_doc_text(filename: str, heading: str, content: str) -> str:
    """Build document text for embedding: filename + heading + content."""
    parts = [filename]
    if heading:
        parts.append(heading)
    parts.append(content)
    return " ".join(parts)


def index_single_file(
    path: pathlib.Path,
    embedding_provider,
    faiss_index,
    bm25_index,
    source_layer: str = "L5",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Extract, embed, and index a single file as one document. Returns 1 if indexed, 0 if skipped."""
    text = extract_text(path)
    if not text:
        return 0

    heading = _extract_heading(text)
    doc_text = _build_doc_text(path.name, heading, text)

    meta = {
        "source_file": path.name,
        "heading": heading,
        "source_layer": source_layer,
        "char_count": len(text),
        "text": text[:500],
    }

    vector = np.array(embedding_provider.embed(doc_text), dtype=np.float32).reshape(1, -1)
    faiss_index.add(vector, [meta])
    bm25_index.add([doc_text], [meta])

    return 1


def full_rebuild(
    knowledge_dir: pathlib.Path,
    embedding_provider,
    faiss_index,
    bm25_index,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Full rebuild of both indexes from all files in knowledge_dir. One vector per file."""
    faiss_index.clear()
    all_doc_texts: List[str] = []
    all_metas: List[dict] = []

    if not knowledge_dir.is_dir():
        return 0

    for f in sorted(knowledge_dir.iterdir()):
        if not f.is_file() or f.name in _BACKFILL_SKIP or is_binary(f):
            continue
        text = extract_text(f)
        if not text:
            continue
        heading = _extract_heading(text)
        doc_text = _build_doc_text(f.name, heading, text)
        file_meta = read_file_meta(knowledge_dir, f.name)
        all_doc_texts.append(doc_text)
        all_metas.append({
            "source_file": f.name,
            "heading": heading,
            "source_layer": file_meta.source_layer,
            "char_count": len(text),
            "text": text[:500],
        })

    if not all_doc_texts:
        return 0

    for i in range(0, len(all_doc_texts), batch_size):
        batch_texts = all_doc_texts[i:i + batch_size]
        batch_metas = all_metas[i:i + batch_size]
        vectors = np.array(embedding_provider.embed_batch(batch_texts), dtype=np.float32)
        faiss_index.add(vectors, batch_metas)

    bm25_index.build(all_doc_texts, all_metas)

    log.info("Full rebuild: %d documents indexed (whole-document, ADR-018)",
             len(all_doc_texts))
    return len(all_doc_texts)
