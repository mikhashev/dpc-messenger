"""Whole-document indexing pipeline (ADR-010 + ADR-018 + ADR-024 Phase 1.6b.1).

One embedding per file (no chunking). BGE-M3's 8192-token window covers
all DPC knowledge files (0.5-5KB each).

Triggers: write_file(knowledge/), approved commit (L6), Extended Paths mtime change.
Full rebuild if model/dimensions change (detected by backend.vector.needs_rebuild).
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import json
import re
import time
from dataclasses import dataclass, field
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


def doc_hash(doc_text: str) -> str:
    """The `file_hashes` value for a document: what the startup sync compares."""
    return hashlib.sha256(doc_text.encode()).hexdigest()[:16]


def document_meta(source_file: str, path: pathlib.Path, text: str, source_layer: str) -> "tuple[str, dict]":
    """The embedded text and the stored row for one document, as every path builds them.

    `text` in the row is a preview for readers (Active Recall prints it). It is not
    the document and nothing may rebuild an index from it.
    """
    heading, doc_text, excerpt = document_fields(source_file, text)
    return doc_text, {
        "source_file": source_file,
        "heading": heading,
        "source_layer": source_layer,
        # Where the document actually lives. The key names it; this reaches it.
        "source_path": str(path),
        "char_count": len(text),
        "text": excerpt,
        "doc_hash": doc_hash(doc_text),
    }


def index_single_file(
    path: pathlib.Path,
    embedding_provider,
    backend: RetrievalBackend,
    source_layer: str = "L5",
    source_file_key: "str | None" = None,
) -> int:
    """Extract, embed, and index a single file as one document. Returns 1 if indexed, 0 if skipped.

    This appends. A caller replacing a document goes through `replace_file_in_index`,
    which removes the key's rows first and records the hash.

    source_file_key lets the caller pin the key/display string used as
    `meta["source_file"]` (matters for `remove_by_source` lookups and for
    avoiding cross-layer basename collisions in the index). Defaults to
    `path.name` for backward compat — production callers pass the layer-
    prefixed relative posix key from `_sync_index`.
    """
    text = extract_text(path)
    if not text:
        return 0
    doc_text, meta = document_meta(source_file_key or path.name, path, text, source_layer)
    vector = np.array(embedding_provider.embed(doc_text), dtype=np.float32).reshape(1, -1)
    backend.vector.add([VectorAddItem(vector=vector, meta=meta)])
    backend.text.add([TextAddItem(text=doc_text, meta=meta)])
    return 1


def record_file_hashes(index_dir: pathlib.Path, updates: Dict[str, Optional[str]]) -> None:
    """Bring `file_hashes` in line with a live change; a None value forgets the key.

    Without this a document added by a tool is absent from the map, so the next start
    reads it as new and embeds it beside the row already there — a permanent duplicate.
    A store with no map yet is left without one: a one-entry map would tell the sync
    that every other document is new.
    """
    from .index_meta import read_meta, write_meta
    meta_path = index_dir / "index_meta.json"
    doc = read_meta(meta_path)
    hashes = doc.get("file_hashes")
    if not isinstance(hashes, dict) or not hashes:
        return
    for key, value in updates.items():
        if value is None:
            hashes.pop(key, None)
        else:
            hashes[key] = value
    write_meta(meta_path, doc)


def replace_file_in_index(
    agent_root: pathlib.Path,
    path: pathlib.Path,
    embedding_provider,
    source_layer: str,
    source_file_key: str,
) -> bool:
    """Make the index hold exactly one row per channel for this file: the current one.

    Run it on the agent's index writer. False when the index would not load, which
    leaves the document out until the next start.
    """
    from .retrieval import make_backend_for_agent
    backend = make_backend_for_agent(agent_root)
    if not backend.vector.load():
        return False
    backend.text.load()
    backend.vector.remove_by_source(source_file_key)
    backend.text.remove_by_source(source_file_key)
    text = extract_text(path) if path.exists() else ""
    new_hash: Optional[str] = None
    if text:
        doc_text, meta = document_meta(source_file_key, path, text, source_layer)
        vector = np.array(embedding_provider.embed(doc_text), dtype=np.float32).reshape(1, -1)
        backend.vector.add([VectorAddItem(vector=vector, meta=meta)])
        backend.text.add([TextAddItem(text=doc_text, meta=meta)])
        new_hash = meta["doc_hash"]
    backend.save()
    record_file_hashes(agent_root / "state" / "memory_index", {source_file_key: new_hash})
    return True


def forget_in_index(agent_root: pathlib.Path, source_files: List[str]) -> int:
    """Drop these keys from both channels and from `file_hashes`. Run on the index writer."""
    from .retrieval import make_backend_for_agent
    if not source_files:
        return 0
    backend = make_backend_for_agent(agent_root)
    removed = 0
    if backend.vector.load():
        removed = backend.vector.remove_by_sources(source_files)
        backend.vector.save()
    if backend.text.load():
        backend.text.remove_by_sources(source_files)
        backend.text.save()
    record_file_hashes(agent_root / "state" / "memory_index", {k: None for k in source_files})
    return removed


def row_is_current(row: dict, current: dict) -> bool:
    """Was this stored row embedded from the document as it is now?

    Rows written from 2026-10-05 carry the hash. Older ones are judged by what they
    do carry — preview, length and heading — which a rewrite almost always moves.
    """
    if row.get("doc_hash"):
        return row["doc_hash"] == current.get("doc_hash")
    return (row.get("text", "") == current.get("text", "")
            and int(row.get("char_count") or 0) == int(current.get("char_count") or 0)
            and row.get("heading", "") == current.get("heading", ""))


@dataclass
class IndexRepair:
    """What the startup sync has to fix beyond the documents whose hash moved."""

    ghosts: List[str] = field(default_factory=list)          # in the index, file gone
    collapse: List[str] = field(default_factory=list)        # more than one vector row
    embed_missing: List[str] = field(default_factory=list)   # collected, no vector row
    text_only: List[str] = field(default_factory=list)       # text row wrong, vector fine
    preview_rows: int = 0                                    # of text_only: old format
    strays_kept: int = 0                                     # not collected, file exists
    reembedded: int = 0                                      # of collapse: no row matched

    @property
    def needed(self) -> bool:
        return bool(self.ghosts or self.collapse or self.embed_missing or self.text_only)


def plan_index_repair(
    vector_rows: "Optional[Dict[str, List[dict]]]",
    text_rows: "Optional[Dict[str, List[dict]]]",
    preview_keys: "set",
    current: Dict[str, dict],
    reembedding: "set",
    exists=None,
) -> IndexRepair:
    """Compare what the index holds with what the sync collected. Reads, never writes.

    `current` maps each collected key to its fresh row; `reembedding` is the keys the
    pass already re-embeds because their hash moved. A key the sync does not collect
    is a ghost only when its file is gone: a file written live outside the collected
    layers (a nested knowledge directory) stays.
    """
    exists = exists or os.path.exists
    plan = IndexRepair()
    v = vector_rows or {}
    t = text_rows or {}
    for key in sorted(set(v) | set(t)):
        if key in current:
            continue
        paths = {r.get("source_path") for r in v.get(key, []) + t.get(key, [])} - {None, ""}
        if any(exists(p) for p in paths):
            plan.strays_kept += 1
        else:
            plan.ghosts.append(key)
    for key in sorted(current):
        if key in reembedding:
            continue
        n = len(v.get(key, [])) if vector_rows is not None else 1
        if n == 0:
            plan.embed_missing.append(key)
            continue
        if n > 1:
            plan.collapse.append(key)
        wrong_text = text_rows is not None and len(t.get(key, [])) != 1
        if key in preview_keys:
            plan.preview_rows += 1
        if n > 1 or wrong_text or key in preview_keys:
            plan.text_only.append(key)
    return plan


def repair_before_embedding(
    backend: RetrievalBackend,
    index_dir: pathlib.Path,
    collected: list,
    to_embed: list,
    old_hashes: dict,
    removed_files: list,
    agent_id: str = "",
    dpc_home: "pathlib.Path | None" = None,
) -> "tuple[IndexRepair, list]":
    """The startup sync's removal step, with the repair folded in. Idempotent.

    Removes every row of every key the pass is about to add — a key added live and
    missing from `file_hashes` included, which is how a tool write became a permanent
    duplicate — plus ghosts. Duplicated keys keep one vector row when one matches the
    current file and are re-embedded (appended to `to_embed`) only when none does.
    Keyword rows that are wrong or were built from previews are re-added from the
    collected text, which costs a BM25 rebuild and no embedding.

    Returns the plan and the (text, meta) pairs the caller adds to the text channel.
    """
    current = {key: meta for key, _t, meta, _l in collected}
    doc_texts = {key: text for key, text, _m, _l in collected}
    reembedding = {key for key, _t, _m in to_embed}
    vector_rows = backend.vector.source_rows()
    text_rows = backend.text.source_rows()
    plan = plan_index_repair(vector_rows, text_rows, backend.text.sources_missing_full_text(),
                             current, reembedding)
    if plan.needed:
        home = dpc_home or pathlib.Path(os.environ.get("DPC_HOME", pathlib.Path.home() / ".dpc"))
        try:
            dest = back_up_index_once(index_dir, home,
                                      with_grafeo="grafeo" in (backend.backend_id or ""))
            log.info("[%s] memory index backed up to %s before repair", agent_id, dest)
        except Exception as e:
            log.warning("[%s] memory index repair skipped — backup failed: %s", agent_id, e)
            plan = IndexRepair()

    in_index = set(vector_rows or {}) | set(text_rows or {})
    vector_drop = [k for k in reembedding if k in old_hashes or k in in_index]
    vector_drop += list(removed_files) + plan.ghosts
    if vector_drop:
        backend.vector.remove_by_sources(vector_drop)
    reembed = sorted(backend.vector.keep_one_row_per_source(
        plan.collapse, lambda row: row_is_current(row, current[row.get("source_file", "")]),
    )) if plan.collapse else []
    plan.reembedded = len(reembed)
    for key in reembed + plan.embed_missing:
        to_embed.append((key, doc_texts[key], current[key]))
    text_only = [k for k in plan.text_only if k not in set(reembed)]
    text_drop = vector_drop + reembed + plan.embed_missing + text_only
    if text_drop:
        backend.text.remove_by_sources(text_drop)
    if plan.needed:
        log.info(
            "[%s] memory index repair: %d keyword rows rebuilt from full text (%d were previews), "
            "%d ghost keys dropped, %d duplicate keys collapsed (%d re-embedded), "
            "%d missing keys re-embedded, %d uncollected keys kept",
            agent_id, len(text_only), plan.preview_rows, len(plan.ghosts), len(plan.collapse),
            plan.reembedded, len(plan.embed_missing), plan.strays_kept,
        )
    return plan, [(doc_texts[k], current[k]) for k in text_only]


def back_up_index_once(index_dir: pathlib.Path, dpc_home: pathlib.Path, today: "str | None" = None,
                       with_grafeo: bool = False) -> pathlib.Path:
    """Copy the index aside before a repair rewrites it; once per agent per day.

    The copy lives under `<dpc_home>/backups`, outside every directory an agent
    indexes. Raises when the copy fails, so the caller skips the repair instead of
    repairing without a way back. The `grafeo` directory is copied only when that
    backend is in use: a native agent never writes it (338 MB unused on agent_001).
    """
    import datetime
    import shutil
    agent = index_dir.parent.parent.name
    day = today or datetime.date.today().isoformat()
    dest = dpc_home / "backups" / f"memory-index-{day}" / agent
    if not dest.exists():
        skip = ["bm25.new", "bm25.old", "*.tmp"] + ([] if with_grafeo else ["grafeo"])
        shutil.copytree(index_dir, dest, ignore=shutil.ignore_patterns(*skip))
    return dest


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
        file_meta = read_file_meta(knowledge_dir, f.name)
        doc_text, meta = document_meta(l5_key(f, knowledge_dir), f, text, file_meta.source_layer)
        all_doc_texts.append(doc_text)
        all_metas.append(meta)

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
