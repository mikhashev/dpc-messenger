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


def collect_extended_files(
    extended_paths: Dict[str, List],
    indexed_paths: Optional[List[str]] = None,
    excluded_dirs: Optional[List[str]] = None,
    allowed_extensions: Optional[frozenset] = None,
) -> List[pathlib.Path]:
    """Collect text files from extended paths, filtered by indexed flag.

    If indexed_paths is provided, only paths in that list are included.
    Default: no paths indexed (opt-in via indexed_paths).
    excluded_dirs overrides DEFAULT_EXCLUDED_DIRS when provided.
    allowed_extensions overrides TEXT_EXTENSIONS when provided (use RECALL_EXTENSIONS for Active Recall).
    """
    exclude = frozenset(excluded_dirs) if excluded_dirs is not None else DEFAULT_EXCLUDED_DIRS
    extensions = allowed_extensions if allowed_extensions is not None else TEXT_EXTENSIONS
    files: List[pathlib.Path] = []
    for access_level in ("read_only", "read_write"):
        for path_entry in extended_paths.get(access_level, []):
            path_str = path_entry if isinstance(path_entry, str) else str(path_entry)
            if indexed_paths is not None and path_str not in indexed_paths:
                continue
            p = pathlib.Path(path_str)
            if not p.exists():
                continue
            if p.is_file() and _is_ext_match(p, extensions):
                files.append(p)
            elif p.is_dir():
                for f in p.rglob("*"):
                    if not f.is_file() or not _is_ext_match(f, extensions):
                        continue
                    if any(part in exclude for part in f.relative_to(p).parts):
                        continue
                    files.append(f)
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
