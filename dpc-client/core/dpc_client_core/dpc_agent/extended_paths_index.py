"""Extended Paths integration for indexing (ADR-010, MEM-3.10).

Reads extended paths from firewall config, filters by text extension,
checks mtime for external file changes, triggers re-index.
"""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Dict, List, Optional, Tuple

from .text_extract import is_binary

log = logging.getLogger(__name__)

TEXT_EXTENSIONS = frozenset({
    ".md", ".txt", ".json", ".py", ".ts", ".js", ".yaml", ".yml",
    ".toml", ".ini", ".csv", ".rst", ".html", ".xml", ".cfg",
})

RECALL_EXTENSIONS = frozenset({".md"})

DEFAULT_EXCLUDED_DIRS = frozenset({
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "target", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".next", ".nuxt", ".svelte-kit", "coverage",
    "htmlcov", ".eggs", "*.egg-info", "bower_components",
    ".gradle", ".idea", ".vs", "bin", "obj",
})


def _norm(path_str: str) -> str:
    """Comparison key for two spellings of the same location."""
    return os.path.normcase(os.path.normpath(path_str))


def reconcile_indexed_paths(
    extended_paths: Dict[str, List],
    indexed_paths: List[str],
) -> Tuple[List[str], List[str]]:
    """Re-attach index flags to the access list they were copied from.

    The UI stores an index flag as a *copy* of the access-list string, so editing a
    path in `read_only` leaves the old spelling stranded in `indexed_paths`, where it
    matches nothing and the root silently stops being indexed. This repairs that:

    - exact match kept as-is;
    - different spelling of a live entry re-pointed at the live spelling;
    - a stranded entry whose location is gone re-pointed at the single reachable
      entry ending in the same path tail (this is what a directory rename leaves
      behind). Two tails are tried, longest first, and each must match exactly one
      candidate — a rename that also moves the parent, `…\\mike\\ai-studio` becoming
      `…\\mikha\\Documents\\ai-studio`, only agrees on the final segment;
    - anything still unmatched dropped, because it can only ever be dead weight.

    Only reachable paths are accepted as re-point targets: silently moving a flag
    onto another dead path would hide the problem instead of fixing it.

    Returns the repaired list plus one human-readable line per change.
    """
    live: List[str] = []
    for access_level in ("read_only", "read_write"):
        for entry in extended_paths.get(access_level, []):
            live.append(entry if isinstance(entry, str) else str(entry))

    reachable = [p for p in live if os.path.exists(p)]
    by_norm = {_norm(p): p for p in live}
    repaired: List[str] = []
    changes: List[str] = []

    for raw in indexed_paths:
        entry = raw if isinstance(raw, str) else str(raw)
        if entry in live:
            repaired.append(entry)
            continue

        live_spelling = by_norm.get(_norm(entry))
        if live_spelling:
            repaired.append(live_spelling)
            changes.append(f"re-pointed {entry!r} -> {live_spelling!r} (same location, other spelling)")
            continue

        remapped = None
        if not os.path.exists(entry):
            entry_parts = pathlib.PurePath(entry).parts
            for depth in (2, 1):
                if len(entry_parts) < depth:
                    continue
                tail = entry_parts[-depth:]
                # Distinct locations, not distinct entries: the same path may be listed
                # under both access levels, and that is one candidate, not two.
                candidates: Dict[str, str] = {}
                for p in reachable:
                    if pathlib.PurePath(p).parts[-depth:] == tail:
                        candidates.setdefault(_norm(p), p)
                if len(candidates) == 1:
                    remapped = next(iter(candidates.values()))
                    changes.append(
                        f"re-pointed stale {entry!r} -> {remapped!r} "
                        f"(last {depth} segment(s) match, single candidate)"
                    )
                    break
        if remapped:
            repaired.append(remapped)
            continue

        changes.append(f"dropped {entry!r} (matches no reachable path)")

    deduped: List[str] = []
    for p in repaired:
        if p not in deduped:
            deduped.append(p)
    return deduped, changes


def collect_extended_files(
    extended_paths: Dict[str, List],
    indexed_paths: Optional[List[str]] = None,
    excluded_dirs: Optional[List[str]] = None,
    allowed_extensions: Optional[frozenset] = None,
    already_indexed: Optional[set] = None,
) -> List[pathlib.Path]:
    """Collect text files from extended paths, filtered by indexed flag.

    If indexed_paths is provided, only paths in that list are included.
    Default: no paths indexed (opt-in via indexed_paths).
    excluded_dirs overrides DEFAULT_EXCLUDED_DIRS when provided.
    allowed_extensions overrides TEXT_EXTENSIONS when provided (use RECALL_EXTENSIONS for Active Recall).
    already_indexed holds normalised paths another layer has claimed; a directory can
    legitimately appear both as an implicit layer source and in the extended paths, and
    indexing it twice puts the same file in the index under two keys.
    """
    exclude = frozenset(excluded_dirs) if excluded_dirs is not None else DEFAULT_EXCLUDED_DIRS
    extensions = allowed_extensions if allowed_extensions is not None else TEXT_EXTENSIONS
    claimed = already_indexed or set()
    skipped_dupes = 0
    files: List[pathlib.Path] = []

    if indexed_paths is not None:
        indexed_paths, repairs = reconcile_indexed_paths(extended_paths, indexed_paths)
        for line in repairs:
            log.warning("indexed_paths repair: %s", line)

    for access_level in ("read_only", "read_write"):
        for path_entry in extended_paths.get(access_level, []):
            path_str = path_entry if isinstance(path_entry, str) else str(path_entry)
            if indexed_paths is not None and path_str not in indexed_paths:
                continue
            p = pathlib.Path(path_str)
            if not p.exists():
                log.warning("indexed path does not exist, nothing will be indexed from it: %s", path_str)
                continue
            if p.is_file() and _is_ext_match(p, extensions):
                if _norm(str(p)) in claimed:
                    skipped_dupes += 1
                else:
                    files.append(p)
            elif p.is_dir():
                for f in p.rglob("*"):
                    if not f.is_file() or not _is_ext_match(f, extensions):
                        continue
                    if any(part in exclude for part in f.relative_to(p).parts):
                        continue
                    if _norm(str(f)) in claimed:
                        skipped_dupes += 1
                        continue
                    files.append(f)

    if skipped_dupes:
        log.info("collect_extended_files: skipped %d file(s) already indexed by another layer", skipped_dupes)
    log.info("collect_extended_files: %d files (extensions: %s, excluded: %s)",
             len(files), ", ".join(sorted(extensions)), ", ".join(sorted(exclude)[:5]) + ("..." if len(exclude) > 5 else ""))
    return files


def _is_ext_match(path: pathlib.Path, extensions: frozenset) -> bool:
    return path.suffix.lower() in extensions and not is_binary(path)


def check_mtime_changes(
    files: List[pathlib.Path],
    mtime_cache: Dict[str, float],
) -> Tuple[List[pathlib.Path], Dict[str, float]]:
    """Compare file mtimes against cache, return changed files + updated cache."""
    changed: List[pathlib.Path] = []
    new_cache: Dict[str, float] = {}

    for f in files:
        key = str(f)
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        new_cache[key] = mtime
        if key not in mtime_cache or mtime_cache[key] != mtime:
            changed.append(f)

    return changed, new_cache


def _is_text_file(path: pathlib.Path) -> bool:
    return _is_ext_match(path, TEXT_EXTENSIONS)
