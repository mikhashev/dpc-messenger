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
    """Comparison key for two spellings of the same location.

    Case-folding is the platform's, not ours: `normcase` lower-cases on Windows and is
    a no-op on POSIX. So `Backlog.md` and `backlog.md` are one document on Windows and
    two on Linux — correct in both places, because that is what the filesystem says,
    and worth stating because a reader expects a comparison helper to be portable.
    """
    return os.path.normcase(os.path.normpath(path_str))


# The two things a repair can be. Named once so the summary counts the same wording
# the lines are written with, instead of re-guessing it from their prose.
REPAIR_REPOINTED = "re-pointed"
REPAIR_DROPPED = "dropped"


def summarise_repairs(entries: int, changes: List[str]) -> str:
    """One line for a boot log; the individual lines belong at DEBUG.

    A config that has drifted produces one repair per stale entry, and a machine that
    was renamed drifts every entry at once — fifty warnings on every start, saying the
    same thing fifty times. The count is the part worth reading at INFO.
    """
    dropped = sum(1 for c in changes if c.startswith(REPAIR_DROPPED))
    return (f"{entries} entries, {len(changes) - dropped} re-pointed, "
            f"{dropped} dropped (no reachable path)")


def reconcile_indexed_paths(
    extended_paths: Dict[str, List],
    indexed_paths: List[str],
    guess_renames: bool = False,
) -> Tuple[List[str], List[str]]:
    """Re-attach index flags to the access list they were copied from.

    `guess_renames` decides what happens to a flag whose path is gone from both the
    access list and the disk. Off — the default, and what every automatic caller
    uses — it is dropped. On, a single reachable access path with the same tail is
    treated as the same root under a new name.

    The guess is off by default because a rename and a deliberate removal look
    identical from here, and guessing wrong indexes a root the user did not tick:
    removing `…\\projA\\docs` while `…\\projB\\docs` stays moves the flag to projB.
    Verified by test. What is in the access list is what the user said, so an
    automatic pass may only subtract from it; adding is a person's call, which is
    what `repair_indexed_paths` is for — it shows the moves before making them.

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
            changes.append(
                f"{REPAIR_REPOINTED} {entry!r} -> {live_spelling!r} "
                f"(same location, other spelling)"
            )
            continue

        remapped = None
        if guess_renames and not os.path.exists(entry):
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
                        f"{REPAIR_REPOINTED} stale {entry!r} -> {remapped!r} "
                        f"(last {depth} segment(s) match, single candidate)"
                    )
                    break
        if remapped:
            repaired.append(remapped)
            continue

        changes.append(f"{REPAIR_DROPPED} {entry!r} (matches no reachable path)")

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

    A directory below a root that is a linked git worktree is pruned whole, whatever
    `excluded_dirs` says: its `.git` is a file pointing into `<main>/.git/worktrees/`,
    which part-based exclusion never matches, and a worktree is a full second checkout
    of a repository the index already has — the incident that added this re-embedded
    250 files per agent, ~56 min of CPU. A submodule, an ordinary clone and a worktree
    that is itself the indexed root are all kept (`_is_linked_git_worktree` says how
    each is told apart).

    A file is yielded once however many configured roots lead to it. Three arrangements
    reach the same file twice and all of them are ordinary: a root granted both
    read_only and read_write (the access lists are separate and a path may sit in both),
    a root nested inside another indexed root, and the same path listed twice. The
    second copy carries the same key, so it does not collide with anything — it
    duplicates, quietly doubling that document's weight in the index.
    """
    exclude = frozenset(excluded_dirs) if excluded_dirs is not None else DEFAULT_EXCLUDED_DIRS
    extensions = allowed_extensions if allowed_extensions is not None else TEXT_EXTENSIONS
    # Copied, not aliased: this grows as we collect, and the caller's set means
    # "claimed by another layer", which is not ours to add to.
    claimed = set(already_indexed or ())
    seen: set = set()
    skipped_dupes = 0
    skipped_repeats = 0
    skipped_worktrees = 0
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
                key = _norm(str(p))
                if key in claimed:
                    skipped_dupes += 1
                elif key in seen:
                    skipped_repeats += 1
                else:
                    seen.add(key)
                    files.append(p)
            elif p.is_dir():
                for dirpath, dirnames, filenames in os.walk(p):
                    # Pruned in place, not filtered after the walk: os.walk (top-down,
                    # its default) descends only into the dirnames it is left with, so
                    # an excluded subtree — or a linked worktree, which is a whole
                    # second checkout — is never walked at all.
                    kept: List[str] = []
                    for d in dirnames:
                        candidate = pathlib.Path(dirpath) / d
                        if d in exclude:
                            continue
                        if _is_linked_git_worktree(candidate):
                            skipped_worktrees += 1
                            log.debug("collect_extended_files: skipping linked git worktree %s", candidate)
                            continue
                        kept.append(d)
                    dirnames[:] = kept
                    for name in filenames:
                        f = pathlib.Path(dirpath) / name
                        if not f.is_file() or not _is_ext_match(f, extensions):
                            continue
                        # The old part filter matched the file's own name too; the
                        # prune above covers every directory part, this covers the last.
                        if name in exclude:
                            continue
                        key = _norm(str(f))
                        if key in claimed:
                            skipped_dupes += 1
                            continue
                        if key in seen:
                            skipped_repeats += 1
                            continue
                        seen.add(key)
                        files.append(f)

    if skipped_dupes:
        log.info("collect_extended_files: skipped %d file(s) already indexed by another layer", skipped_dupes)
    if skipped_repeats:
        log.info("collect_extended_files: skipped %d file(s) reachable through more than one indexed root", skipped_repeats)
    if skipped_worktrees:
        log.info("collect_extended_files: skipped %d linked git worktree(s), each a second checkout of a repository the index already has", skipped_worktrees)
    log.info("collect_extended_files: %d files (extensions: %s, excluded: %s)",
             len(files), ", ".join(sorted(extensions)), ", ".join(sorted(exclude)[:5]) + ("..." if len(exclude) > 5 else ""))
    return files


def _is_ext_match(path: pathlib.Path, extensions: frozenset) -> bool:
    return path.suffix.lower() in extensions and not is_binary(path)


def _is_linked_git_worktree(directory: pathlib.Path) -> bool:
    """True when `directory` is a linked git worktree, as git itself tells it.

    A linked worktree keeps no metadata of its own: its `.git` is a FILE reading
    `gitdir: <main>/.git/worktrees/<name>`, and everything lives in the main
    checkout that created it. The neighbours that also carry a `.git` file must
    stay indexed — a submodule's gitdir points into `.../.git/modules/<name>`,
    and a standalone worktree that IS an indexed root is the user's choice — so
    only a `worktrees` parent is a match. An unreadable, empty or garbled `.git`
    file is not a match either: a folder is skipped only on a confirmed gitdir,
    never because the check could not read one.
    """
    dot_git = directory / ".git"
    if not dot_git.is_file():
        return False
    try:
        first_line = dot_git.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0].strip()
    except OSError:
        return False
    prefix = "gitdir:"
    if not first_line.startswith(prefix):
        return False
    # Git writes forward slashes on every platform; a backslashed value has been
    # seen from tools that do not, and both spellings must read the same here.
    gitdir = first_line[len(prefix):].strip().replace("\\", "/")
    if not gitdir:
        return False
    # An absolute gitdir replaces the base, a relative one resolves against the
    # directory holding the `.git` file — one construction does both.
    return pathlib.Path(directory, gitdir).parent.name == "worktrees"


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
