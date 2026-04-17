"""Extended Paths integration for indexing (ADR-010, MEM-3.10).

Reads extended paths from firewall config, filters by text extension,
checks mtime for external file changes, triggers re-index.
"""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Dict, List, Tuple

from .text_extract import is_binary

log = logging.getLogger(__name__)

TEXT_EXTENSIONS = frozenset({
    ".md", ".txt", ".json", ".py", ".ts", ".js", ".yaml", ".yml",
    ".toml", ".ini", ".csv", ".rst", ".html", ".xml", ".cfg",
})


def collect_extended_files(
    extended_paths: Dict[str, List[str]],
) -> List[pathlib.Path]:
    """Collect all text files from extended paths."""
    files: List[pathlib.Path] = []
    for access_level in ("read_only", "read_write"):
        for path_str in extended_paths.get(access_level, []):
            p = pathlib.Path(path_str)
            if not p.exists():
                continue
            if p.is_file() and _is_text_file(p):
                files.append(p)
            elif p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file() and _is_text_file(f):
                        files.append(f)
    return files


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
    return path.suffix.lower() in TEXT_EXTENSIONS and not is_binary(path)
