"""The music library: watched folders, track identity, playlists (ADR-051 Phase 0).

One instance per backend, owned by `CoreService` and reached through the
`local_api` commands in `commands.py`; the `music_*` agent tools will call the same
methods (ADR-051 D11, "one service with two front doors").

**Track identity** (ADR-051 Q4, decided by Mike 2026-11-19):

1. `id` (`trk_` + 16 random hex) is stable and owns the entry: playlist positions,
   and later comments, versions and statistics, key off it. It is random, not
   derived from a hash, so it survives a re-hash.
2. Path + size + mtime (`file_stat`), compared on every scan with no ffmpeg.
3. The audio-payload hash (`identity.py`), computed only for a new path or a changed
   file, in a background worker through `asyncio.to_thread`, never on the event loop.
   A new path whose hash equals a missing entry's relinks that entry (a move or a
   rename); equal to a present entry's makes it a duplicate (`alt_paths`).
4. A Chromaprint fingerprint: later, not here.
5. A file that is gone is kept as `missing: true`, never deleted; it comes back by
   itself when the same audio reappears.

Files in watched folders are only read (`rb`); tags are never written.

**Change detection** is polling (ADR-051 N6 left `watchdog` versus polling open; polling
is portable and adds no dependency): every `poll_interval_s` each folder with
`watch: true` is walked and compared by path, size and mtime. The walk runs in a thread.
A folder that cannot be reached is marked `unavailable` and its entries are left as they
are, so an unplugged drive does not turn a library into missing tracks.

**Threading.** Every public method runs on the service's event loop. Mutations have no
`await` inside them, so they are atomic for the other coroutines; only the walk, the
hash and the file writes leave the loop, through `asyncio.to_thread`. A caller on
another loop (an agent tool's per-call loop) must forward to the service loop.

Known limits: a change to a file listed only in `alt_paths` (a duplicate) is not
noticed until it becomes the primary path; folders may not nest.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import secrets
import unicodedata
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Set, Tuple

from . import schemas
from .identity import AUDIO_EXTENSIONS, MIME_BY_EXTENSION, Identity, MediaTools, identify, probe_metadata
from .store import (
    FOLDERS_FILE, LIBRARY_FILE, PLAYLISTS_FILE, atomic_write_text, load_records, music_dir_of,
)

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 60.0
MIN_POLL_INTERVAL_S = 0.01
DEFAULT_SAVE_DELAY_S = 2.0

SMART_ALL_ID = "pl_smart_all"
SMART_GENERATED_ID = "pl_smart_generated"
_SMART_LISTS = ((SMART_ALL_ID, "All tracks", "all"), (SMART_GENERATED_ID, "Generated", "generated"))


class MusicLibraryError(Exception):
    """A request the library refuses; `code` is a short machine word."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class FolderUnavailable(OSError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mtime_text(mtime_ns: int) -> str:
    """An mtime as ISO-8601 UTC with microseconds, from the integer nanoseconds, so the
    same mtime always gives the same string."""
    seconds, rest = divmod(int(mtime_ns), 1_000_000_000)
    stamp = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=rest // 1000)
    return stamp.isoformat().replace("+00:00", "Z")


def path_key(path: str) -> str:
    """The comparison key of a path: normalised, NFC (macOS hands out NFD names), and
    case-folded where the platform's paths are case-insensitive (`os.path.normcase`
    folds on Windows only)."""
    return os.path.normcase(unicodedata.normalize("NFC", os.path.normpath(str(path))))


def _is_inside(key: str, folder_key: str) -> bool:
    return key == folder_key or key.startswith(folder_key.rstrip("\\/") + os.sep)


def _new_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(8)}"


def walk_audio_files(root: Path) -> List[Tuple[str, int, int]]:
    """(path, size, mtime_ns) of every audio file under `root`, recursively.

    Directory symlinks are not followed (no loops); a file symlink is read as its
    target. A subfolder that cannot be read is skipped with a log line. Raises
    `FolderUnavailable` when `root` itself cannot be listed."""
    try:
        with os.scandir(root):
            pass
    except OSError as exc:
        raise FolderUnavailable(str(exc)) from exc
    found: List[Tuple[str, int, int]] = []
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            listing = os.scandir(current)
        except OSError as exc:
            logger.info("music: cannot list %s: %s", current, exc)
            continue
        with listing:
            for item in listing:
                try:
                    if item.is_dir(follow_symlinks=False):
                        stack.append(item.path)
                        continue
                    if os.path.splitext(item.name)[1].lower() not in AUDIO_EXTENSIONS:
                        continue
                    if not item.is_file():
                        continue
                    st = item.stat()
                except OSError:
                    continue
                found.append((item.path, st.st_size, st.st_mtime_ns))
    return found


def _stat_paths(paths: Iterable[str]) -> Dict[str, Tuple[int, int]]:
    out: Dict[str, Tuple[int, int]] = {}
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        out[p] = (st.st_size, st.st_mtime_ns)
    return out


class MusicLibrary:
    """The library over `<DPC home>/music/`."""

    def __init__(
        self,
        dpc_home_dir: Path,
        *,
        tools: Optional[MediaTools] = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        save_delay_s: float = DEFAULT_SAVE_DELAY_S,
    ):
        self.root = music_dir_of(dpc_home_dir)
        self._tools = tools
        self.poll_interval_s = max(float(poll_interval_s), MIN_POLL_INTERVAL_S)
        self.save_delay_s = save_delay_s
        self._entries: Dict[str, dict] = {}
        self._folders: Dict[str, dict] = {}
        self._playlists: Dict[str, dict] = {}
        self._by_hash: Dict[Tuple[str, str], str] = {}
        self._loaded = False
        self._queue: Deque[str] = deque()
        self._queued: Set[str] = set()
        self._verify: Dict[str, Tuple[str, str]] = {}
        self._wake: Optional[asyncio.Event] = None
        self._idle: Optional[asyncio.Event] = None
        self._scan_lock: Optional[asyncio.Lock] = None
        self._worker: Optional[asyncio.Task] = None
        self._poller: Optional[asyncio.Task] = None
        self._save_task: Optional[asyncio.Task] = None
        self._background: Set[asyncio.Task] = set()
        self._dirty = False

    # -- storage -----------------------------------------------------------------

    @property
    def library_path(self) -> Path:
        return self.root / LIBRARY_FILE

    @property
    def folders_path(self) -> Path:
        return self.root / FOLDERS_FILE

    @property
    def playlists_path(self) -> Path:
        return self.root / PLAYLISTS_FILE

    def load(self) -> None:
        """Read the three files. Blocking; `open()` calls it in a thread."""
        entries = load_records(self.library_path, schemas.validate_library_entry)
        folders = load_records(self.folders_path, schemas.validate_music_folder)
        playlists = load_records(self.playlists_path, schemas.validate_playlist)
        self._entries = {e["id"]: e for e in entries}
        self._folders = {f["id"]: f for f in folders}
        self._playlists = {p["id"]: p for p in playlists if p.get("kind") == "user"}
        self._by_hash = {}
        for e in self._entries.values():
            self._index_add(e)
        self._loaded = True

    def _snapshot_texts(self) -> List[Tuple[Path, str]]:
        """The three files as JSON text, made on the loop so no coroutine changes a
        record mid-dump; each record is checked against its schema first."""
        out = []
        for path, records, validate in (
            (self.library_path, self._entries.values(), schemas.validate_library_entry),
            (self.folders_path, self._folders.values(), schemas.validate_music_folder),
            (self.playlists_path, self._playlists.values(), schemas.validate_playlist),
        ):
            records = list(records)
            for r in records:
                validate(r)
            out.append((path, json.dumps(records, ensure_ascii=False, indent=1)))
        return out

    def save_now(self) -> None:
        """Write the three files now. Blocking; for tests and shutdown."""
        self._dirty = False
        for path, text in self._snapshot_texts():
            atomic_write_text(path, text)

    async def flush(self) -> None:
        """Write the three files now, off the loop."""
        self._dirty = False
        texts = self._snapshot_texts()
        await asyncio.to_thread(lambda: [atomic_write_text(p, t) for p, t in texts])

    def _mark_dirty(self) -> None:
        """Save soon: hashing a large folder changes one entry at a time, and writing
        the whole library after each would be quadratic."""
        self._dirty = True
        if self._save_task is None or self._save_task.done():
            try:
                self._save_task = asyncio.get_running_loop().create_task(self._save_later())
            except RuntimeError:  # no loop: a synchronous caller saves itself
                pass

    async def _save_later(self) -> None:
        await asyncio.sleep(self.save_delay_s)
        if self._dirty:
            try:
                await self.flush()
            except Exception:
                logger.exception("music: saving the library failed")

    # -- lifecycle ---------------------------------------------------------------

    def _ensure_primitives(self) -> None:
        if self._wake is None:
            self._wake = asyncio.Event()
            self._idle = asyncio.Event()
            self._idle.set()
            self._scan_lock = asyncio.Lock()

    async def open(self) -> None:
        """Load the files and start the hash worker (no polling)."""
        self._ensure_primitives()
        if not self._loaded:
            await asyncio.to_thread(self.load)
            for e in self._entries.values():
                if e.get("hash_state") == "pending" and not e.get("missing"):
                    self._enqueue(e["id"])
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._hash_worker())

    async def start(self, *, poll: bool = True) -> None:
        """`open()`, then poll the watched folders (one scan of each right away)."""
        await self.open()
        if poll and (self._poller is None or self._poller.done()):
            self._poller = asyncio.get_running_loop().create_task(self._poll_loop())

    async def stop(self) -> None:
        tasks = [t for t in (self._poller, self._worker, self._save_task, *self._background) if t is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._poller = self._worker = self._save_task = None
        self._background.clear()
        if self._loaded and self._dirty:
            await self.flush()

    async def _poll_loop(self) -> None:
        while True:
            for folder_id in [f["id"] for f in self._folders.values() if f["watch"]]:
                try:
                    await self.rescan_folder(folder_id)
                except MusicLibraryError:
                    pass  # removed while the loop ran
                except Exception:
                    logger.exception("music: polling folder %s failed", folder_id)
            await asyncio.sleep(self.poll_interval_s)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- the hash index ----------------------------------------------------------

    @staticmethod
    def _hash_key(e: dict) -> Optional[Tuple[str, str]]:
        if e.get("hash_state") != "done" or not e.get("content_hash") or not e.get("hash_kind"):
            return None
        return (e["hash_kind"], e["content_hash"])

    def _index_add(self, e: dict) -> None:
        key = self._hash_key(e)
        if key is not None:
            self._by_hash.setdefault(key, e["id"])

    def _index_drop(self, e: dict) -> None:
        key = self._hash_key(e)
        if key is not None and self._by_hash.get(key) == e["id"]:
            del self._by_hash[key]

    def _delete_entry(self, track_id: str, *, replaced_by: Optional[str] = None) -> None:
        """Drop an entry. Its playlist positions move to `replaced_by` when given (a
        merge), else they are removed."""
        e = self._entries.pop(track_id, None)
        if e is None:
            return
        self._index_drop(e)
        self._verify.pop(track_id, None)
        for pl in self._playlists.values():
            if track_id in pl["track_ids"]:
                if replaced_by:
                    pl["track_ids"] = [replaced_by if t == track_id else t for t in pl["track_ids"]]
                else:
                    pl["track_ids"] = [t for t in pl["track_ids"] if t != track_id]
                pl["updated_at"] = _now()

    # -- entries -----------------------------------------------------------------

    def _new_folder_entry(self, path: str, folder_id: str, size: int, mtime_ns: int) -> dict:
        p = Path(path)
        entry = {
            "schema": schemas.LIBRARY_ENTRY,
            "id": _new_id("trk_"),
            "title": p.stem,
            "source": {
                "kind": "local", "name": p.name, "uri": p.as_uri(),
                "size_bytes": size, "mime": MIME_BY_EXTENSION.get(p.suffix.lower()),
            },
            # Provisional until hashed (SPEC flow 22): a key from the path.
            "source_hash": f"path:{path_key(path)}",
            "content_hash": None,
            "duration_s": None,
            "added_at": _now(),
            "persist": "full",
            "placeholder": None,
            "origin": "folder",
            "path": path,
            "folder_id": folder_id,
            "alt_paths": [],
            "file_stat": {"size_bytes": size, "mtime": _mtime_text(mtime_ns)},
            "hash_state": "pending",
            "missing": False,
            "hash_kind": None,
            "artist": None,
            "album": None,
        }
        self._entries[entry["id"]] = entry
        return entry

    def _set_path(self, e: dict, path: str, folder_id: Optional[str], stat: Optional[Tuple[int, int]]) -> None:
        p = Path(path)
        e["path"] = path
        e["source"]["name"] = p.name
        e["source"]["uri"] = p.as_uri()
        e["source"]["mime"] = MIME_BY_EXTENSION.get(p.suffix.lower())
        if folder_id:
            e["folder_id"] = folder_id
        if stat is None:
            e.pop("file_stat", None)  # unknown: the next scan of its folder re-checks it
        else:
            e["file_stat"] = {"size_bytes": stat[0], "mtime": _mtime_text(stat[1])}
            e["source"]["size_bytes"] = stat[0]
        e["missing"] = False

    def _apply_metadata(self, e: dict, ident: Identity) -> None:
        e["title"] = ident.title or Path(e["path"]).stem
        e["artist"] = ident.artist
        e["album"] = ident.album
        if ident.duration_s is not None:
            e["duration_s"] = round(float(ident.duration_s), 3)
        e["source"]["size_bytes"] = ident.size_bytes

    def _enqueue(self, track_id: str, *, first: bool = False) -> None:
        self._ensure_primitives()
        if track_id in self._queued:
            if first:
                self._queue.remove(track_id)
                self._queue.appendleft(track_id)
            return
        self._queued.add(track_id)
        (self._queue.appendleft if first else self._queue.append)(track_id)
        self._idle.clear()
        self._wake.set()

    def _folder_of(self, path: str) -> Optional[str]:
        key = path_key(path)
        for f in self._folders.values():
            if _is_inside(key, path_key(f["path"])):
                return f["id"]
        return None

    # -- hashing -----------------------------------------------------------------

    async def _hash_worker(self) -> None:
        while True:
            await self._wake.wait()
            tools: Optional[MediaTools] = None
            while self._queue:
                track_id = self._queue.popleft()
                self._queued.discard(track_id)
                e = self._entries.get(track_id)
                if e is None or e.get("missing") or not e.get("path"):
                    self._verify.pop(track_id, None)
                    continue
                if tools is None:
                    tools = self._tools or await asyncio.to_thread(MediaTools.discover)
                path = e["path"]
                try:
                    ident = await asyncio.to_thread(identify, Path(path), tools)
                except OSError as exc:
                    logger.info("music: cannot hash %s: %s", path, exc)
                    current = self._entries.get(track_id)
                    if current is not None and current.get("hash_state") == "pending":
                        current["hash_state"] = "error"
                        self._mark_dirty()
                    self._verify.pop(track_id, None)
                    continue
                except Exception:
                    logger.exception("music: hashing %s failed", path)
                    self._verify.pop(track_id, None)
                    continue
                self._apply_identity(track_id, path, ident)
            self._wake.clear()
            self._idle.set()

    async def wait_hashed(self, timeout: float = 60.0) -> None:
        """Wait until the hash queue is empty (tests, and a caller that needs ids final)."""
        self._ensure_primitives()
        await asyncio.wait_for(self._idle.wait(), timeout)

    def _apply_identity(self, track_id: str, hashed_path: str, ident: Identity) -> None:
        e = self._entries.get(track_id)
        if e is None:
            return
        if e.get("missing") or path_key(e.get("path") or "") != path_key(hashed_path):
            self._verify.pop(track_id, None)
            if not e.get("missing"):
                self._enqueue(track_id)  # its path moved while it was hashed
            return
        stat = (ident.size_bytes, ident.mtime_ns)
        key = (ident.hash_kind, ident.content_hash)
        previous = self._verify.pop(track_id, None)
        if previous is not None:
            if previous == key:
                # Same audio after a change (a retag, a touch): the same track.
                self._set_path(e, hashed_path, None, stat)
                self._apply_metadata(e, ident)
                self._mark_dirty()
                return
            # New audio at this path: the old track's bytes are no longer here.
            logger.info("music: %s holds new audio; %s keeps its state and the path becomes a new entry",
                        hashed_path, track_id)
            if e.get("alt_paths"):
                alt = e["alt_paths"].pop(0)
                self._set_path(e, alt, self._folder_of(alt), None)
            else:
                e["missing"] = True
            folder_id = e.get("folder_id") or self._folder_of(hashed_path)
            e = self._new_folder_entry(hashed_path, folder_id, *stat)
        self._resolve(e, ident, stat)
        self._mark_dirty()

    def _resolve(self, e: dict, ident: Identity, stat: Tuple[int, int]) -> None:
        """Give a freshly hashed entry its identity: relink, collapse, or keep it."""
        key = (ident.hash_kind, ident.content_hash)
        other_id = self._by_hash.get(key)
        other = self._entries.get(other_id) if other_id and other_id != e["id"] else None
        if other is not None and other.get("missing"):
            # A move or a rename: the known track takes the new path.
            logger.info("music: relinked %s to %s (same audio hash)", other["id"], e["path"])
            self._set_path(other, e["path"], e.get("folder_id"), stat)
            self._apply_metadata(other, ident)
            self._delete_entry(e["id"], replaced_by=other["id"])
            return
        if other is not None:
            # The same audio at a second path: one entry, the path in alt_paths.
            if path_key(e["path"]) not in {path_key(p) for p in [other["path"], *other.get("alt_paths", [])]}:
                other.setdefault("alt_paths", []).append(e["path"])
            self._delete_entry(e["id"], replaced_by=other["id"])
            return
        e["hash_kind"], e["content_hash"] = key
        e["source_hash"] = ident.content_hash
        e["hash_state"] = "done"
        e["file_stat"] = {"size_bytes": stat[0], "mtime": _mtime_text(stat[1])}
        self._apply_metadata(e, ident)
        self._index_add(e)

    # -- folders -----------------------------------------------------------------

    def list_folders(self) -> List[dict]:
        return [copy.deepcopy(f) for f in self._folders.values()]

    async def add_folder(self, path: str, *, watch: bool = True, scan: bool = True) -> dict:
        """Add a watched folder (only the person does this; SPEC §6). The first scan
        starts in the background when `scan`."""
        if not isinstance(path, str) or not path.strip():
            raise MusicLibraryError("invalid_path", "a folder path is required")
        chosen = Path(path.strip()).expanduser()
        if not chosen.is_absolute():
            raise MusicLibraryError("invalid_path", f"not an absolute path: {path}")
        chosen = Path(os.path.normpath(chosen))
        if not await asyncio.to_thread(chosen.is_dir):
            raise MusicLibraryError("not_a_folder", f"not a folder, or not reachable: {chosen}")
        key = path_key(str(chosen))
        for f in self._folders.values():
            other = path_key(f["path"])
            if other == key:
                raise MusicLibraryError("already_added", f"already a watched folder: {f['path']}")
            if _is_inside(key, other) or _is_inside(other, key):
                raise MusicLibraryError("nested_folder", f"overlaps the watched folder {f['path']}")
        folder = {
            "schema": schemas.MUSIC_FOLDER,
            "id": _new_id("mf_"),
            "path": str(chosen),
            "added_at": _now(),
            "last_scan_at": None,
            "watch": bool(watch),
            "file_count": 0,
            "hashed_count": 0,
            "watch_state": "polling" if watch else "off",
            "state_reason": None,
        }
        schemas.validate_music_folder(folder)
        self._folders[folder["id"]] = folder
        await self.flush()
        if scan:
            self._spawn(self.rescan_folder(folder["id"]))
        return copy.deepcopy(folder)

    async def remove_folder(self, folder_id: str) -> int:
        """Remove a folder and its entries from the library; files are never touched.
        An entry that also lives in another watched folder (an `alt_paths` there)
        stays, with that path. Returns how many entries were removed."""
        folder = self._folders.get(folder_id)
        if folder is None:
            raise MusicLibraryError("unknown_folder", f"no watched folder {folder_id}")
        fkey = path_key(folder["path"])
        del self._folders[folder_id]
        removed = 0
        for e in list(self._entries.values()):
            alts = e.get("alt_paths") or []
            kept_alts = [a for a in alts if not _is_inside(path_key(a), fkey)]
            if len(kept_alts) != len(alts):
                e["alt_paths"] = kept_alts
            if e.get("origin") != "folder" or e.get("folder_id") != folder_id:
                continue
            home = next(((a, self._folder_of(a)) for a in kept_alts if self._folder_of(a)), None)
            if home is not None:
                alt, other_folder = home
                e["alt_paths"] = [a for a in kept_alts if a != alt]
                self._set_path(e, alt, other_folder, None)
                continue
            self._delete_entry(e["id"])
            removed += 1
        await self.flush()
        return removed

    async def rescan_folder(self, folder_id: str) -> dict:
        """Walk one folder and reconcile it (layer 2); new and changed files are queued
        for hashing (layer 3). Returns the folder record."""
        self._ensure_primitives()
        async with self._scan_lock:
            folder = self._folders.get(folder_id)
            if folder is None:
                raise MusicLibraryError("unknown_folder", f"no watched folder {folder_id}")
            root = folder["path"]
            fkey = path_key(root)
            outside_alts = [
                a for e in self._entries.values()
                if e.get("folder_id") == folder_id and not e.get("missing")
                for a in e.get("alt_paths") or [] if not _is_inside(path_key(a), fkey)
            ]
            try:
                found, alt_stats = await asyncio.to_thread(
                    lambda: (walk_audio_files(Path(root)), _stat_paths(outside_alts)))
            except FolderUnavailable as exc:
                if folder_id in self._folders:
                    folder["watch_state"] = "unavailable"
                    folder["state_reason"] = f"cannot read the folder: {exc}"
                    self._mark_dirty()
                return copy.deepcopy(folder)
            if folder_id not in self._folders:
                raise MusicLibraryError("unknown_folder", f"no watched folder {folder_id}")
            self._apply_scan(folder, found, alt_stats)
            self._mark_dirty()
            return copy.deepcopy(folder)

    async def rescan_all(self) -> List[dict]:
        return [await self.rescan_folder(fid) for fid in list(self._folders)]

    def _apply_scan(self, folder: dict, found: List[Tuple[str, int, int]], alt_stats: Dict[str, Tuple[int, int]]) -> None:
        folder_id = folder["id"]
        fkey = path_key(folder["path"])
        found_by_key = {path_key(p): (p, size, mtime) for p, size, mtime in found}
        primary: Dict[str, dict] = {}
        alternate: Dict[str, dict] = {}
        for e in self._entries.values():
            if e.get("missing") or not e.get("path"):
                continue
            primary[path_key(e["path"])] = e
            for a in e.get("alt_paths") or []:
                alternate[path_key(a)] = e

        # Files on disk: unchanged, changed, or new.
        for key, (path, size, mtime) in found_by_key.items():
            e = primary.get(key)
            if e is not None:
                stat_text = {"size_bytes": size, "mtime": _mtime_text(mtime)}
                if e.get("file_stat") == stat_text and e.get("hash_state") != "error":
                    continue
                if e.get("hash_state") == "done":
                    if e["id"] not in self._verify:
                        self._verify[e["id"]] = (e["hash_kind"], e["content_hash"])
                else:
                    e["file_stat"] = stat_text
                    e["hash_state"] = "pending"
                self._enqueue(e["id"])
                continue
            if key in alternate:
                continue
            new = self._new_folder_entry(path, folder_id, size, mtime)
            self._enqueue(new["id"])

        # Entries of this folder whose files are gone.
        for e in list(self._entries.values()):
            if e.get("missing") or not e.get("path"):
                continue
            alts = e.get("alt_paths") or []
            kept = [a for a in alts if not _is_inside(path_key(a), fkey) or path_key(a) in found_by_key]
            if len(kept) != len(alts):
                e["alt_paths"] = kept
            key = path_key(e["path"])
            if not _is_inside(key, fkey) or key in found_by_key:
                continue
            promoted = False
            while e["alt_paths"]:
                alt = e["alt_paths"].pop(0)
                akey = path_key(alt)
                if akey in found_by_key:
                    _, size, mtime = found_by_key[akey]
                    self._set_path(e, alt, folder_id, (size, mtime))
                    promoted = True
                    break
                if alt in alt_stats:
                    self._set_path(e, alt, self._folder_of(alt), alt_stats[alt])
                    promoted = True
                    break
            if promoted:
                continue
            if e.get("hash_state") == "done":
                e["missing"] = True
                self._verify.pop(e["id"], None)
            else:
                # Never hashed, so nothing can relink to it: no state to keep.
                self._delete_entry(e["id"])

        hashed = 0
        for key in found_by_key:
            e = primary.get(key) or alternate.get(key)
            if e is not None and e["id"] in self._entries and e.get("hash_state") == "done":
                hashed += 1
        folder["file_count"] = len(found_by_key)
        folder["hashed_count"] = hashed
        folder["last_scan_at"] = _now()
        folder["watch_state"] = "polling" if folder["watch"] else "off"
        folder["state_reason"] = (
            f"no file-event watcher; polled every {self.poll_interval_s:g} s" if folder["watch"] else None
        )

    # -- library -----------------------------------------------------------------

    def list_entries(self, *, folder_id: Optional[str] = None, include_missing: bool = True) -> List[dict]:
        out = []
        for e in self._entries.values():
            if folder_id is not None and e.get("folder_id") != folder_id:
                continue
            if not include_missing and e.get("missing"):
                continue
            out.append(copy.deepcopy(e))
        return out

    def get_entry(self, track_id: str) -> dict:
        e = self._entries.get(track_id)
        if e is None:
            raise MusicLibraryError("unknown_track", f"no track {track_id}")
        return copy.deepcopy(e)

    def get_track_path(self, track_id: str) -> Optional[Path]:
        """Where the track's bytes are now, or None when it is missing or has no file.
        For byte serving later (ADR-051 N8 is open)."""
        e = self._entries.get(track_id)
        if e is None or e.get("missing") or not e.get("path"):
            return None
        return Path(e["path"])

    async def get_track(self, track_id: str) -> dict:
        """The entry and, when its duration is known, its `mp.track/1` (native rate
        and channels from ffprobe when present). An unhashed entry is moved to the
        front of the hash queue (SPEC flow 22: the opened track first)."""
        e = self.get_entry(track_id)
        if e.get("hash_state") == "pending" and not e.get("missing"):
            self._enqueue(track_id, first=True)
        meta: Dict[str, Any] = {}
        path = self.get_track_path(track_id)
        if path is not None:
            tools = self._tools or await asyncio.to_thread(MediaTools.discover)
            meta = await asyncio.to_thread(probe_metadata, path, tools)
        duration = e.get("duration_s") if e.get("duration_s") is not None else meta.get("duration_s")
        track = None
        if duration is not None:
            track = {
                "schema": schemas.TRACK,
                "id": e["id"],
                "source": copy.deepcopy(e["source"]),
                "content_hash": e["content_hash"],
                "source_hash": e["source_hash"],
                "editable": e["source"]["kind"] != "web",
                "duration_s": float(duration),
                "sample_rate": meta.get("sample_rate"),
                "sample_rate_basis": "ffprobe" if meta.get("sample_rate") else "unknown: ffprobe absent or the file is missing",
                "channels": meta.get("channels"),
                "loaded_at": _now(),
            }
            schemas.validate_track(track)
        return {"entry": e, "track": track}

    # -- playlists ---------------------------------------------------------------

    def _check_track_ids(self, track_ids: Any) -> List[str]:
        if not isinstance(track_ids, list) or not all(isinstance(t, str) for t in track_ids):
            raise MusicLibraryError("invalid_track_ids", "track_ids must be a list of track ids")
        unknown = [t for t in track_ids if t not in self._entries]
        if unknown:
            raise MusicLibraryError("unknown_track", f"not in the library: {', '.join(unknown[:5])}")
        return list(track_ids)

    @staticmethod
    def _check_name(name: Any) -> str:
        if not isinstance(name, str) or not name.strip():
            raise MusicLibraryError("invalid_name", "a playlist needs a name")
        return name.strip()

    def list_playlists(self) -> List[dict]:
        """The smart lists (computed now), then the person's playlists."""
        now = _now()
        out = []
        for pl_id, name, rule in _SMART_LISTS:
            ids = [e["id"] for e in self._entries.values()
                   if rule == "all" or e.get("origin") == "generated"]
            out.append({"schema": schemas.PLAYLIST, "id": pl_id, "name": name, "track_ids": ids,
                        "created_at": now, "updated_at": now, "kind": "smart", "rule": rule})
        out.extend(copy.deepcopy(p) for p in self._playlists.values())
        return out

    async def create_playlist(self, name: str, track_ids: Optional[List[str]] = None) -> dict:
        now = _now()
        playlist = {
            "schema": schemas.PLAYLIST, "id": _new_id("pl_"), "name": self._check_name(name),
            "track_ids": self._check_track_ids(track_ids or []),
            "created_at": now, "updated_at": now, "kind": "user",
        }
        schemas.validate_playlist(playlist)
        self._playlists[playlist["id"]] = playlist
        await self.flush()
        return copy.deepcopy(playlist)

    def _user_playlist(self, playlist_id: str) -> dict:
        if playlist_id in (SMART_ALL_ID, SMART_GENERATED_ID):
            raise MusicLibraryError("smart_list", "a smart list is computed and cannot be changed")
        pl = self._playlists.get(playlist_id)
        if pl is None:
            raise MusicLibraryError("unknown_playlist", f"no playlist {playlist_id}")
        return pl

    async def update_playlist(self, playlist_id: str, *, name: Optional[str] = None,
                              track_ids: Optional[List[str]] = None) -> dict:
        """Rename, and/or set the full ordered list (so a reorder is one call, SPEC §6)."""
        pl = self._user_playlist(playlist_id)
        if name is None and track_ids is None:
            raise MusicLibraryError("nothing_to_update", "give name and/or track_ids")
        new_name = self._check_name(name) if name is not None else pl["name"]
        new_ids = self._check_track_ids(track_ids) if track_ids is not None else pl["track_ids"]
        pl["name"], pl["track_ids"], pl["updated_at"] = new_name, new_ids, _now()
        await self.flush()
        return copy.deepcopy(pl)

    async def delete_playlist(self, playlist_id: str) -> None:
        """Delete a playlist; its tracks stay in the library."""
        self._user_playlist(playlist_id)
        del self._playlists[playlist_id]
        await self.flush()
