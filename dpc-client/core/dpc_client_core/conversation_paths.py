"""One place that decides which folder a conversation's files live in.

Three call sites used to answer that question independently — the group
manager, the conversation monitor and a helper on the service — and they did
not agree. `GroupManager` preferred `{id}-{slug}` whenever the group had a
name; `CoreService._find_group_dir` tried the bare `{id}` first and only then
looked for a prefix. With both folders on disk, different code paths read
different histories of the same conversation.

Worse than the disagreement was that the *path depended on mutable state*.
`{id}-{slug}` was returned only while the group was loaded and carried a name,
so losing the metadata, regaining it, or renaming the group moved the store,
and everything written before the move stayed where it was. That is what split
one group's history into 66 messages in one folder and 58 in another with no
overlap between them: the metadata arrived, the folder moved, and the monitor's
own migration was skipped because the manager had already created the target
one directory-creation earlier.

The rule that removes the whole class is in `resolve_store_dir`:

    an existing store always wins over a preferred-but-absent one.

Once the path stops depending on the name, no rename, no arriving metadata and
no missing group can move it again. The slug becomes decoration chosen when a
store is first created and never consulted afterwards.

Cross-platform notes, because each of these breaks a naive fix differently:

- **Never rename onto an existing path.** `Path.rename` raises `FileExistsError`
  on Windows when the target exists, and on POSIX it silently succeeds when the
  target is an empty directory. A consolidation written against either
  behaviour misbehaves on the other, so nothing here renames over anything;
  `retire_orphan` picks a free name first.
- **Detect duplicates case-insensitively.** Windows and macOS default to
  case-insensitive filesystems, Linux does not, so `group-x-Work` and
  `group-x-work` are one directory on two platforms and two on the third.
  Comparison is case-insensitive; the path used is always the one on disk.
- **macOS normalises filenames to NFD** on readdir. Nothing non-ASCII reaches a
  path here because `slugify` strips everything outside `[a-z0-9-]` — which is
  also why a group named in Cyrillic gets an empty slug and a bare folder. That
  used to mean renaming it to a Latin name moved the store; under the rule
  above it no longer does.
- **Windows reserved names** (`CON`, `NUL`, `COM1`…) and trailing dots or
  spaces cannot occur: every folder name starts with the conversation id and
  the slug is stripped to `[a-z0-9-]` with leading and trailing dashes removed.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

SLUG_MAX_LEN = 20

# A folder retired by `retire_orphan` keeps its content and stops being a store.
RETIRED_MARKER = ".merged-"

# Legacy layout: `{conversation_id}_history` directories from the pre-v0.21.0
# format. `GroupManager.load_from_disk` already skips them and so do we.
_LEGACY_SUFFIX = "_history"

# What may follow `{id}-` for the folder to be a store of that conversation
# rather than a different conversation whose id happens to start the same way.
_SLUG_TAIL = re.compile(r"^[a-z0-9-]*$")


def slugify(name: str) -> str:
    """Convert a display name to a filesystem-safe slug.

    Unchanged from the two copies this replaces, deliberately: altering the
    rules would rename every folder already on disk.
    """
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:SLUG_MAX_LEN]


# A conversation id becomes a directory name, and one of them arrives in a
# payload: `GROUP_HISTORY_RESPONSE` carries the group id it wants merged. Shown
# 2026-08-28 in a substituted HOME — `group-../../../escaped` resolved outside
# `conversations/` and `merge_history` wrote `history.json` there, parents and
# all; an absolute id discards the base outright, because that is what
# `Path.__truediv__` does. The roster gate refuses such an id before this point
# (no group has that name), but the boundary belongs where the path is built,
# not only at each of the four callers that reach it.
_UNSAFE = ("/", "\\", chr(0))


def is_safe_conversation_id(conversation_id: str) -> bool:
    """Whether this id may be used as a directory name at all."""
    if not conversation_id or not isinstance(conversation_id, str):
        return False
    if any(ch in conversation_id for ch in _UNSAFE):
        return False
    # `..` anywhere, not just at the front: Windows strips a trailing dot from a
    # path component, so a folder asked for as `group-..` is created as `group-`
    # there and as `group-..` everywhere else — the same id, two stores, which is
    # the defect this module was written to end.
    if ".." in conversation_id or conversation_id in (".",):
        return False
    if conversation_id != conversation_id.rstrip(". "):
        return False
    # `C:` and `\host` never reach the branches above on POSIX, so ask the
    # library rather than the string.
    if Path(conversation_id).is_absolute() or Path(conversation_id).drive:
        return False
    return True


def preferred_folder_name(conversation_id: str, display_name: Optional[str] = None) -> str:
    """The folder name a *new* store for this conversation would be given."""
    if display_name:
        slug = slugify(display_name)
        if slug:
            return f"{conversation_id}-{slug}"
    return conversation_id


def is_store_dir_name(name: str, conversation_id: str) -> bool:
    """Whether `name` is a folder holding this conversation's store."""
    lowered = name.lower()
    cid = conversation_id.lower()
    if lowered.endswith(_LEGACY_SUFFIX) or RETIRED_MARKER in lowered:
        return False
    if lowered == cid:
        return True
    if not lowered.startswith(cid + "-"):
        return False
    # `dpc-node-aaa` must not claim `dpc-node-aaa-bbb`'s folder if such an id
    # ever exists: only a slug may follow, and a slug is `[a-z0-9-]`.
    return bool(_SLUG_TAIL.match(lowered[len(cid) + 1:]))


def existing_store_dirs(base: Path, conversation_id: str) -> List[Path]:
    """Every folder on disk that holds a store for this conversation.

    More than one is the defect this module exists for; the caller decides
    whether to consolidate or merely to report.
    """
    if not base.exists():
        return []
    try:
        children = list(base.iterdir())
    except OSError as exc:
        logger.warning("Could not list %s: %s", base, exc)
        return []
    found = [d for d in children if d.is_dir() and is_store_dir_name(d.name, conversation_id)]
    return sorted(found, key=lambda d: d.name.lower())


def _history_size(directory: Path) -> int:
    try:
        return (directory / "history.json").stat().st_size
    except OSError:
        return 0


def canonical_store_dir(dirs: List[Path]) -> Optional[Path]:
    """Pick one folder to be the store, deterministically.

    **The choice deliberately ignores the display name.** Letting the name
    decide is what made two resolvers disagree in the first place: the caller
    that knew the group's name picked the slugged folder and the caller that
    did not picked the bare one, and with both on disk they read two different
    histories of the same conversation. The answer has to be a property of the
    disk, so that every caller gets the same one.

    The folder holding the most history wins, because that is the copy whose
    loss would cost the most. Ties break on the shorter and then the lower
    name, so the answer never depends on directory-listing order either.
    """
    if not dirs:
        return None
    return sorted(dirs, key=lambda d: (-_history_size(d), len(d.name), d.name.lower()))[0]


def resolve_store_dir(
    base: Path, conversation_id: str, display_name: Optional[str] = None
) -> Path:
    """The folder this conversation's files belong in. Never creates anything.

    An existing store always wins over a preferred-but-absent one — that single
    rule is what keeps a display name from moving a live store.
    """
    if not is_safe_conversation_id(conversation_id):
        raise ValueError(f"unsafe conversation id for a store path: {conversation_id!r}")
    dirs = existing_store_dirs(base, conversation_id)
    chosen = canonical_store_dir(dirs)
    if chosen is not None:
        return chosen
    chosen = base / preferred_folder_name(conversation_id, display_name)
    # Belt as well as braces: whatever the id looked like, the answer stays
    # under the base this function was given.
    if base.resolve() not in chosen.resolve().parents:
        raise ValueError(f"store path for {conversation_id!r} would leave {base}")
    return chosen


def split_stores(
    base: Path, conversation_id: str, display_name: Optional[str] = None
) -> Tuple[Optional[Path], List[Path]]:
    """`(canonical, orphans)` — the folders that have to be folded into one."""
    dirs = existing_store_dirs(base, conversation_id)
    canonical = canonical_store_dir(dirs)
    if canonical is None:
        return None, []
    orphans = [d for d in dirs if d != canonical]
    return canonical, orphans


def adopt_payload(orphan: Path, canonical: Path, skip: Tuple[str, ...] = ()) -> int:
    """Move files the canonical store does not already have. Never overwrites.

    Returns the number of entries moved. Anything that would collide is left in
    the orphan, which is kept rather than deleted, so a wrong call here costs a
    duplicate rather than data.
    """
    moved = 0
    if not orphan.exists():
        return 0
    for item in sorted(orphan.iterdir()):
        if item.name in skip:
            continue
        target = canonical / item.name
        if target.exists():
            if item.is_dir():
                moved += adopt_payload(item, target, skip=skip)
            continue
        try:
            canonical.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(target))
            moved += 1
        except OSError as exc:
            logger.warning("Could not move %s into %s: %s", item, canonical, exc)
    return moved


def retire_orphan(orphan: Path, stamp: str) -> Optional[Path]:
    """Rename a folded-in folder so it stops being a store, keeping its content.

    Deliberately not a delete: the folder held the only copy of a history for
    thirteen days once. The target name is checked for freeness first because
    renaming onto an existing path raises on Windows and can quietly succeed on
    POSIX.
    """
    if not orphan.exists():
        return None
    for attempt in range(100):
        suffix = f"{RETIRED_MARKER}{stamp}" + (f"-{attempt}" if attempt else "")
        target = orphan.with_name(orphan.name + suffix)
        if target.exists():
            continue
        try:
            orphan.rename(target)
            logger.info("Retired folded-in conversation folder %s -> %s", orphan.name, target.name)
            return target
        except OSError as exc:
            logger.warning("Could not retire %s: %s", orphan, exc)
            return None
    logger.warning("Could not find a free retirement name for %s", orphan)
    return None
