"""One place that decides what a document is called in the retrieval index.

The key is not decoration. It is the identity of a document across passes:
`file_hashes` uses it to tell changed from unchanged, `remove_by_source` deletes by
it, the fuser dedups by it, and Active Recall prints it to the agent. Two files that
share a key are one file as far as all of that is concerned — the second simply
disappears, and nothing reports it.

That is what the old EXT scheme did. It named a file by its path relative to the
matching root and dropped which root that was, so every `README.md` in every indexed
project arrived as `EXT/README.md`. Measured before this change: 43 files across two
agents were unreachable, nine of them behind that one key. Worse, an indexed path
pointing at a single *file* produced `EXT/.` — the whole key collapsed to a dot.

So a key now carries enough of its origin to be unique:

    L5   knowledge/<rel>          also exactly what read_file() takes
    L6   L6/<rel>
    EXT  EXT/<root tail>/<rel>

The root tail is the shortest tail of the root path that no other configured root
shares — usually one segment, `EXT/dpc-messenger/README.md`. Keeping it a path tail
rather than a hash means the key stays readable, survives the tree being moved to
another machine or another parent, and reads as an address in a hint. Only roots that
actually clash grow a segment, so adding a root cannot silently rename everything
under an unrelated one.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
from dataclasses import dataclass
from typing import Dict, List, Sequence

log = logging.getLogger(__name__)

# What the stored index was built by. agent_manager compares it against the marker
# in index_meta.json and rebuilds from scratch on mismatch, because these changes
# cannot be repaired incrementally: an incremental pass only touches documents whose
# hash moved, and the damage here is in documents whose hash did not.
#
# Bump it for any change to what ends up in the index, not only to how keys are
# spelled. v3 collects each file once however many roots reach it — the duplicate
# rows left by v2 all carry unchanged hashes, so nothing short of a rebuild removes
# them. v4 stores source_path on the stored node: v3 rows were written by a backend
# that dropped the field, and their hashes did not move when it started keeping it.
# v5 drops the front-matter envelope before the heading, the excerpt and the
# embedded text are taken. v4 rows carry "Commit Identification" as their heading
# and a hash as their excerpt, and their content hashes did not move when that
# stopped being how they are read.
KEY_FORMAT = "layer_addressed_v5"

L5_PREFIX = "knowledge"
L6_PREFIX = "L6"
EXT_PREFIX = "EXT"


def _norm(path) -> str:
    """Comparison key for two spellings of the same location.

    Case-folding is the platform's, not ours: `normcase` lower-cases on Windows and is
    a no-op on POSIX. So `Backlog.md` and `backlog.md` are one document on Windows and
    two on Linux — correct in both places, because that is what the filesystem says,
    and worth stating because a reader expects a comparison helper to be portable.
    """
    return os.path.normcase(os.path.normpath(str(path)))


def _segment(part: str) -> str:
    """One path component as a key segment: no separators, never empty.

    Roots reach the filesystem root as `C:\\` on Windows and `/` on POSIX; both
    would otherwise put a separator inside a segment and split it in two.
    """
    cleaned = part.strip("\\/").replace(":", "").strip()
    return cleaned or "root"


def _relative_posix(path: pathlib.Path, base: pathlib.Path) -> str:
    return path.relative_to(base).as_posix()


def l5_key(path: pathlib.Path, knowledge_dir: pathlib.Path) -> str:
    """Key for the agent's own knowledge layer — and a working read_file() argument.

    read_file resolves a relative path against the agent sandbox, and the knowledge
    directory sits directly inside it, so naming the layer after the directory it
    lives in makes the printed key followable rather than merely descriptive.
    """
    try:
        return f"{L5_PREFIX}/{_relative_posix(path, knowledge_dir)}"
    except ValueError:
        return f"{L5_PREFIX}/{path.name}"


def l6_key(path: pathlib.Path, l6_dir: pathlib.Path) -> str:
    """Key for the shared human knowledge layer under $DPC_HOME."""
    try:
        return f"{L6_PREFIX}/{_relative_posix(path, l6_dir)}"
    except ValueError:
        return f"{L6_PREFIX}/{path.name}"


@dataclass(frozen=True)
class ExtRoot:
    """A configured indexed root plus the tail that distinguishes it from the others."""

    base: pathlib.Path
    tail: str


def build_ext_roots(indexed_paths: Sequence[str]) -> List[ExtRoot]:
    """Give every indexed root the shortest tail that no other root answers to.

    A root may be configured as a file rather than a directory; its directory is used
    as the base so the file's own name still distinguishes it. Depth is chosen per
    root, not globally, so one clashing pair does not lengthen — and thereby rename —
    every other root's keys. Tails of different depths can never collide with each
    other: a depth-N tail contains exactly N-1 separators.
    """
    bases: List[pathlib.Path] = []
    seen: set = set()
    for entry in indexed_paths:
        p = pathlib.Path(entry)
        if not p.exists():
            # A root that is not there is not "a file", but `is_dir()` says False for
            # both, so its parent would become the base — and the base takes part in
            # computing everyone's tails. A path that indexes nothing could therefore
            # lengthen a live root's tail and rename every key under it, which is a
            # rebuild nobody asked for and a document that quietly changes identity.
            # Nothing reaches here today (reconcile_indexed_paths drops dead entries
            # first), and that is one caller's habit rather than a property of this
            # function.
            log.warning("indexed root %s does not exist; excluded from key naming", p)
            continue
        base = p if p.is_dir() else p.parent
        if _norm(base) not in seen:
            seen.add(_norm(base))
            bases.append(base)

    def tail_at(base: pathlib.Path, depth: int) -> str:
        parts = pathlib.PurePath(base).parts[-depth:]
        return "/".join(_segment(part) for part in parts)

    roots: List[ExtRoot] = []
    for base in bases:
        others = [b for b in bases if _norm(b) != _norm(base)]
        max_depth = len(pathlib.PurePath(base).parts)
        tail = ""
        for depth in range(1, max_depth + 1):
            tail = tail_at(base, depth)
            if all(tail_at(o, depth) != tail for o in others if len(pathlib.PurePath(o).parts) >= depth):
                break
        roots.append(ExtRoot(base=base, tail=tail))

    # Two roots that differ only above their own length — a path and its own ancestor
    # spelled the same way — would still share a tail. Fall back to marking them apart
    # rather than letting one swallow the other's files.
    by_tail: Dict[str, List[ExtRoot]] = {}
    for r in roots:
        by_tail.setdefault(r.tail, []).append(r)
    resolved: List[ExtRoot] = []
    for tail, group in by_tail.items():
        if len(group) == 1:
            resolved.append(group[0])
            continue
        for r in group:
            digest = hashlib.sha256(_norm(r.base).encode("utf-8")).hexdigest()[:6]
            log.warning("indexed roots share the tail %r; %s keyed as %s~%s", tail, r.base, tail, digest)
            resolved.append(ExtRoot(base=r.base, tail=f"{tail}~{digest}"))
    return resolved


def ext_key(path: pathlib.Path, roots: Sequence[ExtRoot]) -> str:
    """Key for an extended path, qualified by the root the file came from.

    The longest containing root wins, so a root nested inside another names its files
    by the nearer of the two. A file under no configured root cannot happen through
    collect_extended_files, but if config drifts under us the absolute path is used:
    an ugly key is recoverable, a colliding one silently loses a document.
    """
    best: ExtRoot | None = None
    for root in roots:
        try:
            path.relative_to(root.base)
        except ValueError:
            continue
        if best is None or len(root.base.parts) > len(best.base.parts):
            best = root
    if best is None:
        log.warning("extended file %s is under no indexed root; keying by absolute path", path)
        parts = "/".join(_segment(part) for part in path.parts)
        return f"{EXT_PREFIX}/{parts}"
    rel = _relative_posix(path, best.base)
    return f"{EXT_PREFIX}/{best.tail}/{rel}"
