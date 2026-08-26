"""What each indexed corpus costs and what it returns, per agent.

The question this exists for — what belongs in the index — cannot be settled by
argument, and it was nearly settled by a measurement too thin to carry it. Counting
today: only a small share of the injection log names keys that exist under the current
scheme, because the keys were respelled on 2026-08-01 and the log predates that. Reads
are unaffected, since they are recorded as absolute paths, but a decision to drop a
corpus needs both halves.

So this reports rather than recommends, and it reports the thinness alongside the
numbers: `injections_counted` against `injections_ignored_old_scheme` says how far the
evidence can be trusted. Run it again in a fortnight and the same call answers with a
sample worth deciding on.

Three columns per corpus:

    documents  what it contributes to the index    — from the index, exact
    shown      slots it has taken                  — current-scheme keys only
    opened     times the agent went there          — scheme-independent, exact
    followed   of those, times a hint sent it      — only where the read recorded it

`opened` counts any read of an indexed document, however the agent got there, and
reading it as a follow rate is what produced the one-in-four figure that started
the corpus argument. `followed` is the narrower number and the honest one, and it
is zero for everything read before the reads began saying so.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Dict, List, Optional

from .active_recall import EvidenceReadFailed, _build_access_counts
from .extended_paths_index import reconcile_indexed_paths
from .index_keys import EXT_PREFIX, build_ext_roots, ext_key, l5_key, l6_key

log = logging.getLogger(__name__)


def corpus_of(index_key: str) -> str:
    """Which corpus a document belongs to: its layer, and for EXT the root's tail.

    The tail is what a person recognises — `EXT/dpc-messenger` rather than a path or a
    hash — and it is the unit the indexed-paths setting turns on and off, so it is the
    unit a decision is made in.
    """
    if index_key.startswith(f"{EXT_PREFIX}/"):
        parts = index_key.split("/", 2)
        return f"{EXT_PREFIX}/{parts[1]}" if len(parts) > 1 else EXT_PREFIX
    return index_key.split("/", 1)[0]


def _indexed_roots(firewall, agent_id: str):
    if firewall is None:
        return []
    try:
        ext_paths = firewall.get_extended_paths(profile_name=agent_id)
        indexed = firewall._get_profile_or_global(
            agent_id, "sandbox_extensions", "indexed_paths", default=[]) or []
        indexed, _ = reconcile_indexed_paths(ext_paths, indexed)
        return build_ext_roots(indexed)
    except Exception as e:
        log.debug("corpus stats: could not resolve indexed roots for %s: %s", agent_id, e)
        return []


def _key_for_read(path: pathlib.Path, agent_root: pathlib.Path,
                  l6_dir: pathlib.Path, roots) -> Optional[str]:
    """The key the index would give a file the agent opened by absolute path."""
    try:
        if path.is_relative_to(agent_root / "knowledge"):
            return l5_key(path, agent_root / "knowledge")
        if path.is_relative_to(l6_dir):
            return l6_key(path, l6_dir)
        return ext_key(path, roots) if roots else None
    except Exception:
        return None


def corpus_stats(agent_root: pathlib.Path, firewall, agent_id: str,
                 dpc_home: Optional[pathlib.Path] = None) -> dict:
    """Per-corpus documents, slots taken and reads, for one agent."""
    home = dpc_home or agent_root.parent.parent
    index_meta = agent_root / "state" / "memory_index" / "index_meta.json"
    if not index_meta.exists():
        return {"agent_id": agent_id, "corpora": [], "documents_total": 0,
                "injections_counted": 0, "injections_ignored_old_scheme": 0}
    try:
        keys = set(json.loads(index_meta.read_text(encoding="utf-8")).get("file_hashes", {}))
    except (json.JSONDecodeError, OSError):
        keys = set()

    try:
        counts = _build_access_counts(agent_root)
    except EvidenceReadFailed as exc:
        # None, not zero: the counts were unreadable, which is not the same
        # answer as "the agent showed and opened nothing".
        log.warning("corpus stats for %s: %s", agent_id, exc)
        return {"agent_id": agent_id, "corpora": [], "documents_total": len(keys),
                "injections_counted": None, "injections_ignored_old_scheme": None,
                "evidence_read_failed": str(exc)}
    roots = _indexed_roots(firewall, agent_id)

    documents: Dict[str, int] = {}
    shown: Dict[str, int] = {}
    opened: Dict[str, int] = {}
    for key in keys:
        documents[corpus_of(key)] = documents.get(corpus_of(key), 0) + 1

    counted = ignored = 0
    for key, n in counts.injections_by_key.items():
        # A key the index no longer holds was written under the previous scheme, and
        # attributing it to a corpus would credit slots to a spelling that no document
        # answers to. Count the omission rather than hiding it.
        if key in keys:
            shown[corpus_of(key)] = shown.get(corpus_of(key), 0) + n
            counted += n
        else:
            ignored += n

    followed: Dict[str, int] = {}
    for path_str, n in counts.reads_by_path.items():
        key = _key_for_read(pathlib.Path(path_str), agent_root, home / "knowledge", roots)
        if key and key in keys:
            opened[corpus_of(key)] = opened.get(corpus_of(key), 0) + n
    for key, n in counts.reads_by_key.items():
        if key in keys:
            opened[corpus_of(key)] = opened.get(corpus_of(key), 0) + n
    for path_str, n in counts.follows_by_path.items():
        key = _key_for_read(pathlib.Path(path_str), agent_root, home / "knowledge", roots)
        if key and key in keys:
            followed[corpus_of(key)] = followed.get(corpus_of(key), 0) + n
    for key, n in counts.follows_by_key.items():
        if key in keys:
            followed[corpus_of(key)] = followed.get(corpus_of(key), 0) + n

    corpora: List[dict] = [
        {"corpus": name, "documents": documents.get(name, 0),
         "shown": shown.get(name, 0), "opened": opened.get(name, 0),
         "followed": followed.get(name, 0)}
        for name in sorted(set(documents) | set(shown) | set(opened),
                           key=lambda n: (-documents.get(n, 0), n))
    ]
    return {
        "agent_id": agent_id,
        "documents_total": len(keys),
        "injections_counted": counted,
        "injections_ignored_old_scheme": ignored,
        "corpora": corpora,
    }
