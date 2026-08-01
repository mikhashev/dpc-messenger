"""Whole-document indexing pipeline (ADR-010 + ADR-018 + ADR-024 Phase 1.6b.1).

One embedding per file (no chunking). BGE-M3's 8192-token window covers
all DPC knowledge files (0.5-5KB each).

Triggers: write_file(knowledge/), approved commit (L6), Extended Paths mtime change.
Full rebuild if model/dimensions change (detected by backend.vector.needs_rebuild).
"""

from __future__ import annotations

import logging
import pathlib
import json
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from .index_keys import KEY_FORMAT, l5_key
from .retrieval import RetrievalBackend, TextAddItem, VectorAddItem
from .text_extract import extract_text, is_binary
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
    backend: RetrievalBackend,
    source_layer: str = "L5",
    source_file_key: "str | None" = None,
) -> int:
    """Extract, embed, and index a single file as one document. Returns 1 if indexed, 0 if skipped.

    source_file_key lets the caller pin the key/display string used as
    `meta["source_file"]` (matters for `remove_by_source` lookups and for
    avoiding cross-layer basename collisions in the index). Defaults to
    `path.name` for backward compat — production callers pass the layer-
    prefixed relative posix key from `_sync_index`.
    """
    text = extract_text(path)
    if not text:
        return 0

    heading = _extract_heading(text)
    _src = source_file_key or path.name
    doc_text = _build_doc_text(_src, heading, text)

    meta = {
        "source_file": _src,
        "heading": heading,
        "source_layer": source_layer,
        # Where the document actually lives. The key names it; this reaches it.
        "source_path": str(path),
        "char_count": len(text),
        "text": text[:500],
    }

    vector = np.array(embedding_provider.embed(doc_text), dtype=np.float32).reshape(1, -1)
    backend.vector.add([VectorAddItem(vector=vector, meta=meta)])
    backend.text.add([TextAddItem(text=doc_text, meta=meta)])

    return 1


@dataclass(frozen=True)
class RebuildDecision:
    """Whether the stored index can still be extended, and what to tell the log."""

    needed: bool
    message: str = ""


def rebuild_decision(index_dir: pathlib.Path, actual_model: str) -> RebuildDecision:
    """Can the index on disk be brought up to date incrementally, or must it be rebuilt?

    An incremental pass only touches documents whose content hash moved, so it cannot
    repair damage that lives in documents whose hash did not: a different embedding
    model, a different key spelling, a field the store used to drop. Those are exactly
    the changes that arrive as *old rows*, and the marker in the header is how a
    previous version announces itself.

    Lifted out of agent_manager unchanged so it can be run against the state earlier
    versions wrote. Inline, the one path in this system whose whole job is to recognise
    legacy state was the one path no test could reach — see tests/legacy_forms.py.
    """
    meta_path = index_dir / "index_meta.json"
    if not meta_path.exists():
        # Nothing stored yet. Not a migration, and nothing worth a line in the log.
        return RebuildDecision(needed=True)
    try:
        header = json.loads(meta_path.read_text(encoding="utf-8")).get("header", {})
    except Exception:
        # Unreadable or malformed: the safe reading is that we do not know what is in
        # there, and the cheap answer is to build it again.
        return RebuildDecision(needed=True)

    stored_model = header.get("model_name", "")
    if stored_model != actual_model:
        return RebuildDecision(
            needed=True,
            message=f"Memory index model changed ({stored_model} -> {actual_model}), forcing rebuild",
        )

    stored_key_format = header.get("key_format", "")
    if stored_key_format != KEY_FORMAT:
        return RebuildDecision(
            needed=True,
            message=f"Memory index key format outdated ({stored_key_format!r}), forcing rebuild",
        )

    return RebuildDecision(needed=False)


def full_rebuild(
    knowledge_dir: pathlib.Path,
    embedding_provider,
    backend: RetrievalBackend,
    stop_event: "threading.Event | None" = None,
) -> int:
    """Full rebuild of both indexes from all files in knowledge_dir. One vector per file."""
    backend.vector.clear()
    all_doc_texts: List[str] = []
    all_metas: List[dict] = []

    if not knowledge_dir.is_dir():
        return 0

    for f in sorted(knowledge_dir.iterdir()):
        if stop_event and stop_event.is_set():
            log.info("Indexing interrupted by shutdown during file scan")
            return 0
        if not f.is_file() or f.name in _BACKFILL_SKIP or is_binary(f):
            continue
        text = extract_text(f)
        if not text:
            continue
        heading = _extract_heading(text)
        _src = l5_key(f, knowledge_dir)
        doc_text = _build_doc_text(_src, heading, text)
        file_meta = read_file_meta(knowledge_dir, f.name)
        all_doc_texts.append(doc_text)
        all_metas.append({
            "source_file": _src,
            "heading": heading,
            "source_layer": file_meta.source_layer,
            "source_path": str(f),
            "char_count": len(text),
            "text": text[:500],
        })

    if not all_doc_texts:
        return 0

    BATCH_SIZE = 4
    indexed_count = 0
    for batch_start in range(0, len(all_doc_texts), BATCH_SIZE):
        if stop_event and stop_event.is_set():
            log.info("Indexing interrupted by shutdown at batch %d/%d", batch_start, len(all_doc_texts))
            return indexed_count
        batch_texts = all_doc_texts[batch_start:batch_start + BATCH_SIZE]
        batch_metas = all_metas[batch_start:batch_start + BATCH_SIZE]
        vectors = np.array(embedding_provider.embed_batch(batch_texts), dtype=np.float32)
        if stop_event and stop_event.is_set():
            log.info("Indexing interrupted by shutdown after embedding batch %d/%d", batch_start, len(all_doc_texts))
            return indexed_count
        backend.vector.add([
            VectorAddItem(vector=vec.reshape(1, -1), meta=meta)
            for vec, meta in zip(vectors, batch_metas)
        ])
        indexed_count += len(batch_texts)

    if stop_event and stop_event.is_set():
        log.info("Indexing interrupted by shutdown before BM25 build")
        return indexed_count

    # Rebuild text channel from scratch: clear() + add() replaces existing state,
    # matching prior bm25_index.build() semantics.
    backend.text.clear()
    backend.text.add([
        TextAddItem(text=t, meta=m)
        for t, m in zip(all_doc_texts, all_metas)
    ])

    log.info("Full rebuild: %d documents indexed (whole-document, ADR-018)",
             len(all_doc_texts))
    return len(all_doc_texts)
