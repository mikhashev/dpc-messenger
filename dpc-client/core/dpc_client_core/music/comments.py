"""Timecoded listening notes on a library track: `mp.comment/1` (SPEC §4), stored per track.

```
<DPC home>/music/comments/<track id>.json     [mp.comment/1], oldest first
```

The key is the library entry id (`trk_` + 16 hex). It persists across a rescan and a
relink (the known entry takes the new path), so the comments follow the track; when the
library folds one entry into another (a duplicate, a missing twin) it calls `merge`, which
moves the comments to the surviving entry. Each file is one JSON array replaced whole in
one step (`store.atomic_write_json`), read with `store.load_records` so a file that does
not parse is moved aside and a record that fails its schema is kept aside, never lost.

**When an entry is removed** (`remove_missing_music_tracks`, a folder removal, deleting a
generated file) its comments file is **kept** on disk: it is a few KB of the person's own
words, and nothing reads it once the id is gone. It is not cleaned up; a later change can
sweep files whose id the library no longer knows.

The core reads and writes the fields SPEC §4 names. `action` is `note` or one of the built
region edits (`regenerate_region`, `fix_seam`, `remove_region`, `region_edit.py`), which need a
region; `extend_ending` is refused as `action_unsupported` (the capabilities report's "planned").
`version_id` is the constant `ORIGINAL_VERSION_ID` because the core has no versions yet: an edit's
takes are sibling files with their own library entries, the base is never changed. `author` is the
local person, `{kind: "human", name: "Me"}` (the prototype's own value), or the agent that proposed
an edit (`add(..., author=)`).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import schemas
from .library import MusicLibrary, MusicLibraryError
from .region_edit import BUILT_ACTIONS
from .store import atomic_write_json, load_records, music_dir_of

logger = logging.getLogger(__name__)

COMMENTS_DIR = "comments"
MAX_TEXT_CHARS = 2000
# The most comments one track holds. The `add_music_comment` command is reachable by an agent, and
# every add rewrites the whole file, so the count is bounded (error `too_many_comments`).
MAX_COMMENTS_PER_TRACK = 500
# SPEC §4 requires a version id; versions do not exist in the core yet, so every comment is
# on "the original": `ver_` + 16 zeros.
ORIGINAL_VERSION_ID = "ver_" + "0" * 16
LOCAL_AUTHOR = {"kind": "human", "name": "Me"}
SUPPORTED_ACTIONS = ("note", *BUILT_ACTIONS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        raise MusicLibraryError("invalid_comment", f"{field} must be a number")
    return float(value)


def check_action(action: Any) -> str:
    if action is None:
        return "note"
    if action not in schemas.COMMENT_ACTIONS:
        raise MusicLibraryError("invalid_comment", f"action {action!r} is not one of {schemas.COMMENT_ACTIONS}")
    if action not in SUPPORTED_ACTIONS:
        raise MusicLibraryError(
            "action_unsupported", f"the action {action!r} is planned and not supported yet; these work now: "
                                  f"{', '.join(SUPPORTED_ACTIONS)}")
    return action


def check_action_range(action: str, t_end: Optional[float]) -> None:
    """A region edit needs a region: a point comment carries only a note."""
    if action in BUILT_ACTIONS and t_end is None:
        raise MusicLibraryError("invalid_comment", f"the {action} action needs a region (an end time)")


def check_author(author: Any) -> dict:
    if author is None:
        return dict(LOCAL_AUTHOR)
    if (not isinstance(author, dict) or set(author) != {"kind", "name"} or author["kind"] not in ("human", "agent")
            or not isinstance(author["name"], str) or not author["name"].strip()):
        raise MusicLibraryError("invalid_comment", "author must be {kind: human|agent, name}")
    return {"kind": author["kind"], "name": author["name"].strip()[:80]}


def check_text(text: Any) -> str:
    if not isinstance(text, str) or not text.strip():
        raise MusicLibraryError("invalid_comment", "text is required")
    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise MusicLibraryError("invalid_comment", f"text is {len(text)} characters; the limit is {MAX_TEXT_CHARS}")
    return text


def check_range(start_s: Any, end_s: Any, duration_s: Optional[float]) -> tuple:
    """`(t_start, t_end)`: `t_start` >= 0 and within the duration when it is known, `t_end`
    null for a point, else greater than `t_start` and within the duration."""
    start = _number(start_s, "start_s")
    if start < 0:
        raise MusicLibraryError("invalid_comment", "start_s must be >= 0")
    if duration_s is not None and start > duration_s:
        raise MusicLibraryError("invalid_comment", f"start_s {start:g} is past the end of the track ({duration_s:g} s)")
    if end_s is None:
        return start, None
    end = _number(end_s, "end_s")
    if end <= start:
        raise MusicLibraryError("invalid_comment", "end_s must be greater than start_s (leave it empty for a point)")
    if duration_s is not None and end > duration_s:
        raise MusicLibraryError("invalid_comment", f"end_s {end:g} is past the end of the track ({duration_s:g} s)")
    return start, end


def _sort_key(c: dict) -> tuple:
    return (c["t_start"], c["created_at"], c["id"])


class MusicComments:
    def __init__(self, dpc_home_dir: Path, library: MusicLibrary):
        self.library = library
        self.root = music_dir_of(dpc_home_dir) / COMMENTS_DIR
        # Orders the coroutines of one loop. It cannot guard `merge`, which the library calls
        # synchronously (from the loop or a worker thread), so every read-modify-write of a comments
        # file also runs under `_file_lock`, a thread lock `merge` takes too.
        self._lock = asyncio.Lock()
        self._file_lock = threading.RLock()

    def comments_path(self, track_id: str) -> Path:
        if not isinstance(track_id, str) or not schemas.TRACK_ID_RE.match(track_id):
            raise MusicLibraryError("invalid_track_id", f"not a track id: {track_id!r}")
        return self.root / f"{track_id}.json"

    def _read(self, track_id: str) -> List[dict]:
        return load_records(self.comments_path(track_id), schemas.validate_comment)

    def _write(self, track_id: str, comments: List[dict]) -> None:
        atomic_write_json(self.comments_path(track_id), comments)

    def _duration(self, track_id: str) -> Optional[float]:
        """The entry's duration when known; refuses an id the library does not hold."""
        d = self.library.get_entry(track_id).get("duration_s")
        return float(d) if isinstance(d, (int, float)) and not isinstance(d, bool) and d > 0 else None

    async def list(self, track_id: str) -> List[dict]:
        self._duration(track_id)
        comments = await asyncio.to_thread(self._read, track_id)
        return sorted(copy.deepcopy(comments), key=_sort_key)

    async def add(self, track_id: str, start_s: Any, end_s: Any = None, text: Any = None,
                  action: Any = None, *, author: Any = None) -> dict:
        duration = self._duration(track_id)
        t_start, t_end = check_range(start_s, end_s, duration)
        body = check_text(text)
        act = check_action(action)
        check_action_range(act, t_end)
        who = check_author(author)
        now = _now()
        comment = {
            "schema": schemas.COMMENT,
            "id": f"cmt_{secrets.token_hex(8)}",
            "track_id": track_id,
            "version_id": ORIGINAL_VERSION_ID,
            "kind": "point" if t_end is None else "region",
            "t_start": t_start,
            "t_end": t_end,
            "text": body,
            "action": act,
            "author": who,
            "created_at": now,
            "updated_at": now,
        }
        schemas.validate_comment(comment)
        def append() -> None:
            with self._file_lock:
                self._duration(track_id)   # still in the library: a merge may have folded it away
                comments = self._read(track_id)
                if len(comments) >= MAX_COMMENTS_PER_TRACK:
                    raise MusicLibraryError(
                        "too_many_comments", f"this track already has {len(comments)} comments; the limit is "
                                             f"{MAX_COMMENTS_PER_TRACK}. Delete some before adding more")
                comments.append(comment)
                self._write(track_id, comments)
        async with self._lock:
            await asyncio.to_thread(append)
        return copy.deepcopy(comment)

    async def update(self, track_id: str, comment_id: Any, **fields: Any) -> dict:
        """Change `start_s`, `end_s`, `text` and/or `action` of one comment; a field that is
        not passed stays. `end_s=None` passed explicitly makes the comment a point."""
        def change() -> dict:
            with self._file_lock:
                duration = self._duration(track_id)   # still in the library: a merge may have folded it away
                comments = self._read(track_id)
                comment = next((c for c in comments if c["id"] == comment_id), None)
                if comment is None:
                    raise MusicLibraryError("unknown_comment", f"no comment {comment_id!r} on {track_id}")
                start = fields["start_s"] if "start_s" in fields else comment["t_start"]
                end = fields["end_s"] if "end_s" in fields else comment["t_end"]
                t_start, t_end = check_range(start, end, duration)
                updated = dict(comment)
                updated.update(t_start=t_start, t_end=t_end, kind="point" if t_end is None else "region")
                if "text" in fields:
                    updated["text"] = check_text(fields["text"])
                if "action" in fields:
                    updated["action"] = check_action(fields["action"])
                check_action_range(updated["action"], t_end)
                updated["updated_at"] = _now()
                schemas.validate_comment(updated)
                comments[comments.index(comment)] = updated
                self._write(track_id, comments)
                return updated
        async with self._lock:
            updated = await asyncio.to_thread(change)
        return copy.deepcopy(updated)

    async def delete(self, track_id: str, comment_id: Any) -> None:
        self._duration(track_id)

        def remove() -> None:
            with self._file_lock:
                self._duration(track_id)
                comments = self._read(track_id)
                kept = [c for c in comments if c["id"] != comment_id]
                if len(kept) == len(comments):
                    raise MusicLibraryError("unknown_comment", f"no comment {comment_id!r} on {track_id}")
                self._write(track_id, kept)
        async with self._lock:
            await asyncio.to_thread(remove)

    def merge(self, from_id: str, to_id: str) -> None:
        """The library folded entry `from_id` into `to_id`: its comments move to `to_id`
        (re-keyed) and its file goes. Synchronous: the library calls it from `_delete_entry`,
        on the loop or in a worker thread, so it cannot wait on the asyncio lock; it takes the
        thread lock every comment write holds, so an add that is writing `from_id` finishes
        first and its comment moves with the rest. The moved comments are capped like an add
        (the surplus stays in `from_id`'s file, which is then kept)."""
        if from_id == to_id:
            return
        src = self.comments_path(from_id)
        with self._file_lock:
            if not src.exists():
                return
            moved = self._read(from_id)
            if moved:
                existing = self._read(to_id)
                have = {c["id"] for c in existing}
                fresh = [c for c in moved if c["id"] not in have]
                room = max(0, MAX_COMMENTS_PER_TRACK - len(existing))
                take, left = fresh[:room], fresh[room:]
                existing.extend({**c, "track_id": to_id} for c in take)
                self._write(to_id, existing)
                if left:
                    # Never lose a comment to the cap: the surplus stays in the file it came from.
                    logger.warning("music: %s is full, %d comment(s) of %s stay in its file", to_id, len(left), from_id)
                    self._write(from_id, left)
                    return
            try:
                src.unlink()
            except OSError:
                logger.warning("music: could not remove %s after moving its comments to %s", src.name, to_id)
