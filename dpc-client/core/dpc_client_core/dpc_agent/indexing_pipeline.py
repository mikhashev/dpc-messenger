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


def _strip_front_matter(text: str) -> str:
    """Return the document body, without a leading `---` delimited block.

    Knowledge commits open with an envelope of commit id, hashes and signatures,
    and the lines inside it start with `#`. Read as markdown that made every one
    of those documents headed "Commit Identification" and excerpted as a hash —
    identical to each other and silent about their contents. The envelope is not
    the document, and it should reach neither the heading, the excerpt, nor the
    embedding.
    """
    if not text.startswith("---"):
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip() == "---":
            return "\n".join(lines[i + 1:]).lstrip("\n")
    return text


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


def document_fields(source_key: str, text: str) -> "tuple[str, str, str]":
    """How a document is read: its heading, what gets embedded, what gets shown.

    One function because there are five call sites and they must agree. They did
    not: the strip that drops the commit envelope was added to the two in this
    module, while the three in agent_manager — the path a live agent actually
    rebuilds through — kept calling the pieces directly on the raw text. The
    index came back stamped with the new format and every shared-knowledge row
    still headed by its envelope.
    """
    body = _strip_front_matter(text)
    heading = _extract_heading(body)
    return heading, _build_doc_text(source_key, heading, body), body[:500]


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

    _src = source_file_key or path.name
    heading, doc_text, excerpt = document_fields(_src, text)

    meta = {
        "source_file": _src,
        "heading": heading,
        "source_layer": source_layer,
        # Where the document actually lives. The key names it; this reaches it.
        "source_path": str(path),
        "char_count": len(text),
        "text": excerpt,
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


def rebuild_decision(index_dir: pathlib.Path, actual_model: str, backend_id: str) -> RebuildDecision:
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

    # An index built by one retrieval backend cannot be read by another, and the
    # staleness map does not say so: it describes the corpus, not who indexed it.
    # Flip retrieval_vector and the hashes still match every document, so the
    # incremental pass finds nothing to do and the new backend is left holding an
    # index it never wrote — empty, and permanently, because every later start
    # agrees with the same map.
    #
    # An absent marker is an index written before this field existed, not a
    # mismatch. Forcing a rebuild on it would re-embed every pool on the next
    # start for no reason; the sync stamps the field instead, and the comparison
    # starts protecting from then on.
    stored_backend = header.get("backend", "")
    if stored_backend and stored_backend != backend_id:
        return RebuildDecision(
            needed=True,
            message=(
                f"Memory index was built by a different retrieval backend "
                f"({stored_backend!r} -> {backend_id!r}), forcing rebuild"
            ),
        )

    return RebuildDecision(needed=False)


def keep_only_what_landed(file_hashes: dict, planned: list, embedded: int) -> dict:
    """Drop from the staleness map every document the pass did not get to.

    The map is written after the embedding loop, and the loop `break`s on shutdown. So a
    pass cut in the middle used to record a current hash for documents it never embedded,
    and the next start read those hashes, found nothing to do, and left the documents out
    of the index for good. Not the torn file `load()` refuses, nor the empty index
    `map_outlives_index` rebuilds — a short index behind a full map, which nothing else
    is looking for.

    `planned` is the list the loop walked, in order; `embedded` is how far it got. What
    remains stale is exactly the tail, and stale is the right answer: the next pass will
    see the hash missing and embed the document.
    """
    for entry in planned[embedded:]:
        file_hashes.pop(entry[0], None)
    return file_hashes


def map_outlives_index(loaded: bool, indexed_items: int, mapped_documents: int) -> bool:
    """Does the staleness map describe an index that is no longer there?

    The map and the index are two files that have to agree, and only one of them is
    consulted before the pass decides it has nothing to do. So an index that went away
    — a backend switched under it, a state directory deleted by hand, a load refused
    because its rows and its chunk list disagreed — reads as a corpus fully indexed:
    nothing is re-embedded, and the emptiness is permanent, because the next start
    finds the same agreeing pair.

    Only the empty case is treated as disagreement. A count that merely drifts is not
    evidence of the same failure and forcing a rebuild on it would re-embed the fleet
    over an off-by-one.
    """
    return bool(mapped_documents) and (not loaded or indexed_items == 0)


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
        _src = l5_key(f, knowledge_dir)
        heading, doc_text, excerpt = document_fields(_src, text)
        file_meta = read_file_meta(knowledge_dir, f.name)
        all_doc_texts.append(doc_text)
        all_metas.append({
            "source_file": _src,
            "heading": heading,
            "source_layer": file_meta.source_layer,
            "source_path": str(f),
            "char_count": len(text),
            "text": excerpt,
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
