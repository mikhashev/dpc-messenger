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

**Hide, remove, delete** (Mike, 2026-11-21). `hidden: true` on an entry is a reversible
flag that leaves the file alone; a hidden entry is left out of the smart lists, and the UI
leaves it out of the queue and a playlist's play order (the playlist keeps the id, so an
unhide puts it back). It lives on the entry, so a rescan or a relink keeps it.
`remove_missing` drops entries whose file is gone (never a present one), with their
playlist positions. `delete_generated_file` deletes the audio file of a take DPC owns
(resolved path strictly inside `<DPC home>/music/generated/`, however the entry arrived:
the DPC home may itself be a watched folder) and its
`<name>.json` sidecar, then drops the entry; nothing else in that folder is touched, and
the person's own music is never deleted.

Files in watched folders are only read (`rb`); tags are never written.

**Generated takes** (ADR-051 D21) enter through `register_generated` only: once, by the
same identity (path, then audio hash), with `origin: "generated"` set there and nowhere
else; the "Generated" smart list is derived from that field on read.

**Change detection** is polling (ADR-051 N6 left `watchdog` versus polling open; polling
is portable and adds no dependency): every `poll_interval_s` each folder with
`watch: true` is walked and compared by path, size and mtime. The walk runs in a thread.
A folder that cannot be reached is marked `unavailable` and its entries are left as they
are, so an unplugged drive does not turn a library into missing tracks.

**Threading.** Every public method runs on the service's event loop. Mutations have no
`await` inside them, so they are atomic for the other coroutines; only the walk, the
hash and the file writes leave the loop, through `asyncio.to_thread`. A caller on
another loop (an agent tool's per-call loop) must forward to the service loop.

A folder whose root is reachable but where part of the walk fails (a subfolder that
cannot be listed, a file that cannot be stat'ed: a permission error, a share lost
half-way) is marked `unavailable` for that scan, with the failure in `state_reason`, and
nothing under it is marked missing: SPEC §6 makes `unavailable` a property of the
folder, and a partial listing says nothing about which files are gone.

**What may be watched.** `add_folder` refuses with `MusicLibraryError.code`:
`invalid_path`, `not_a_folder`, `already_added`, `nested_folder`, and
`network_path` (a UNC path, a mapped network drive on Windows, a network filesystem
mount on Linux), `protected_folder` (a filesystem root, the home folder itself, the DPC
home or anything inside it except its own `music/` subtree, a folder that contains the
DPC home), `too_many_folders` (more than `MAX_FOLDERS`). A network mount on macOS is not
detected: reading a mount's filesystem type there needs a subprocess or `statfs`
through ctypes. On Windows a drive whose type cannot be read is refused (`network_path`),
not let through. The same checks run again before every scan, polling included: a
folder that fails them now (its root replaced by a symlink into the DPC home, say) is
marked `unavailable` with the code in `state_reason`, and is not walked. A
`folders.json` edited by hand to hold more than `MAX_FOLDERS` folders keeps the first
`MAX_FOLDERS` by `added_at` active; the rest stay, `unavailable` and unscanned.

**DPC's own MP3 exports are never tracks** (`export.py`, Mike #210). A walked file that
`exclude_path` names (a file the export index lists) is skipped, and a file whose tags carry
`DPC_EXPORT_OF` (read from the ID3 tag before the hash, and from ffprobe's tags after it, so
any format is caught) is dropped from the scan with a log line, wherever it was copied: it
is a copy of a track the library already holds, not a second track. A skipped file is
remembered by path, size and mtime for the life of the process, so the poll does not read
it again until it changes; it is not counted in the folder's `file_count`.

Known limits: a change to a file listed only in `alt_paths` (a duplicate) is not
noticed until it becomes the primary path; folders may not nest.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import unicodedata
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

from . import schemas
from .export import read_export_marker
from .identity import AUDIO_EXTENSIONS, MIME_BY_EXTENSION, Identity, MediaTools, identify, probe_metadata
from .store import (
    FOLDERS_FILE, LIBRARY_FILE, PLAYLISTS_FILE, atomic_write_text, load_records, music_dir_of,
)

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 60.0
MIN_POLL_INTERVAL_S = 0.01
DEFAULT_SAVE_DELAY_S = 2.0
# `music_library_changed` goes out at most once per window: the first change opens it and
# every change inside it joins the one event that closes it.
DEFAULT_CHANGE_DELAY_S = 0.5
MAX_FOLDERS = 32
MAX_CHANGE_IDS = 200

# Linux filesystem types that are a network mount (`/proc/self/mounts`, third column).
# `9p` is left out: WSL mounts the Windows drives with it.
NETWORK_FS_TYPES = frozenset({
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "ncpfs", "afs", "ceph", "glusterfs", "lustre",
    "davfs", "sshfs", "fuse.sshfs", "fuse.davfs2", "fuse.rclone", "fuse.glusterfs", "afpfs",
})

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


def walk_audio_files(root: Path) -> Tuple[List[Tuple[str, int, int]], List[Tuple[str, str]]]:
    """`(found, failed)`: (path, size, mtime_ns) of every audio file under `root`,
    recursively, and (path, reason) of every subfolder or file the walk could not read.

    Directory symlinks are not followed (no loops); a file symlink is read as its
    target. A non-empty `failed` means the listing is partial, so a file not in
    `found` is not known to be gone. Raises `FolderUnavailable` when `root` itself
    cannot be listed."""
    try:
        with os.scandir(root):
            pass
    except OSError as exc:
        raise FolderUnavailable(str(exc)) from exc
    found: List[Tuple[str, int, int]] = []
    failed: List[Tuple[str, str]] = []
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            listing = os.scandir(current)
        except OSError as exc:
            logger.info("music: cannot list %s: %s", current, exc)
            failed.append((current, str(exc)))
            continue
        with listing:
            try:
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
                    except OSError as exc:
                        logger.info("music: cannot read %s: %s", item.path, exc)
                        failed.append((item.path, str(exc)))
                        continue
                    found.append((item.path, st.st_size, st.st_mtime_ns))
            except OSError as exc:  # the listing broke off half-way (a share lost mid-walk)
                logger.info("music: listing %s broke off: %s", current, exc)
                failed.append((current, str(exc)))
    return found, failed


_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def _mount_fs_type(path: str) -> Optional[str]:
    """The filesystem type of the mount holding `path`, from `/proc/self/mounts`
    (Linux); None where that file does not exist."""
    try:
        with open("/proc/self/mounts", "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    best, best_type = "", None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        # The kernel writes space, tab, newline and backslash as octal escapes.
        mount = _MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), parts[1])
        inside = path == mount or path.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) >= len(best):
            best, best_type = mount, parts[2]
    return best_type


def _is_network_drive(path: Path) -> bool:
    """A mapped network drive on Windows, a network filesystem mount on Linux. Reads
    only local tables, never the share itself."""
    if os.name == "nt":
        drive = path.drive
        if len(drive) != 2 or drive[1] != ":":
            return False
        try:
            import ctypes

            drive_remote = 4  # GetDriveTypeW: DRIVE_REMOTE
            return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == drive_remote
        except Exception as exc:
            # Fail closed: a drive whose type cannot be read is refused, not watched.
            raise MusicLibraryError(
                "network_path", f"cannot tell whether {drive} is a network drive ({exc}), so it is not watched",
            ) from exc
    fs_type = _mount_fs_type(str(path))
    return fs_type is not None and fs_type.lower() in NETWORK_FS_TYPES


def _is_unc_text(text: str) -> bool:
    r"""A UNC or Windows device path by its spelling: `\\server\share` or `\\?\...` on
    any OS, and `//server/share` on Windows."""
    head = text[:2]
    if head == "\\\\":
        return True
    return os.name == "nt" and len(head) == 2 and all(c in "\\/" for c in head)


def _realpath(path: Path) -> Path:
    try:
        return Path(os.path.realpath(path))
    except (OSError, ValueError):
        return path


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
        on_added: Optional[Callable[[dict], None]] = None,
        on_merged: Optional[Callable[[str, str], None]] = None,
        on_changed: Optional[Callable[[dict], None]] = None,
        change_delay_s: float = DEFAULT_CHANGE_DELAY_S,
    ):
        self.dpc_home = Path(dpc_home_dir)
        self.root = music_dir_of(dpc_home_dir)
        self._tools = tools
        self.poll_interval_s = max(float(poll_interval_s), MIN_POLL_INTERVAL_S)
        self.save_delay_s = save_delay_s
        # The one "added" event producer (ADR-051 D21): called with a copy of the entry,
        # synchronously, once per generated take that enters the library.
        self.on_added = on_added
        # Called as `on_merged(from_id, to_id)`, synchronously, when an entry is folded into
        # another (a duplicate or a missing twin): what is keyed by the entry id (the
        # comments) moves with the playlist positions. A failure is logged, never raised.
        self.on_merged = on_merged
        # Called as `on_changed({version, reasons, track_ids})` on the loop, once per
        # `change_delay_s` window however many changes it held; a failure is logged.
        self.on_changed = on_changed
        self.change_delay_s = max(float(change_delay_s), 0.0)
        self.change_version = 0
        self._change_handle: Optional[asyncio.TimerHandle] = None
        self._change_reasons: Set[str] = set()
        self._change_ids: Set[str] = set()
        self._entries: Dict[str, dict] = {}
        self._folders: Dict[str, dict] = {}
        self._playlists: Dict[str, dict] = {}
        self._by_hash: Dict[Tuple[str, str], List[str]] = {}
        self._loaded = False
        self._queue: Deque[str] = deque()
        self._queued: Set[str] = set()
        self._verify: Dict[str, Tuple[str, str]] = {}
        # Hashed entries with no duration (scanned before ffprobe was there): a rescan
        # that finds ffprobe now re-probes each of them once per process, with no re-hash.
        self._reprobe: Set[str] = set()
        self._reprobed: Set[str] = set()
        self._wake: Optional[asyncio.Event] = None
        self._idle: Optional[asyncio.Event] = None
        self._scan_lock: Optional[asyncio.Lock] = None
        self._worker: Optional[asyncio.Task] = None
        self._poller: Optional[asyncio.Task] = None
        self._save_task: Optional[asyncio.Task] = None
        self._write_lock: Optional[asyncio.Lock] = None
        # The disk side of a save. `flush()` writes in a thread and `save_now()` on the
        # caller's thread; both write under this lock, and a snapshot older than the
        # one already on disk is not written, so the newest text always lands last.
        self._disk_lock = threading.Lock()
        self._snapshot_seq = 0
        self._written_seq = 0
        self._over_limit: Set[str] = set()
        self._background: Set[asyncio.Task] = set()
        self._dirty = False
        # Set by the service to the exporter's index check: `exclude_path(path)` answers why a
        # walked file is not a track (an MP3 export it listed), or None.
        self.exclude_path: Optional[Callable[[str], Optional[str]]] = None
        # Walked files left out as exports: path key -> (size, mtime_ns) when skipped.
        self._ignored: Dict[str, Tuple[int, int]] = {}

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
        anchored = self._anchor_relative_paths()
        self._by_hash = {}
        for e in self._entries.values():
            self._index_add(e)
        merged = 0
        for key in list(self._by_hash):
            merged += self._merge_missing_twins(key)
        limited = self._apply_folder_limit()
        if merged or limited or anchored:
            self._dirty = True  # `open()` schedules the save
        self._loaded = True

    def _anchor_relative_paths(self) -> bool:
        """Make every entry's `path` absolute. DPC writes only absolute paths; a relative one comes
        from a hand-edited `library.json`, and left alone it would resolve from the process's
        working directory, wherever the backend was started (and could land inside `generated/`,
        which DPC may delete from). It is resolved against the library's own folder
        (`<DPC home>/music/`) instead. A path that leaves that folder, or that falls inside
        `generated/` on an entry that is not a generated take, is refused: the entry stays, marked
        missing, and its file is never served or deleted. Returns whether a record changed."""
        changed = False
        base = _realpath(self.root)
        generated = _realpath(self.root / "generated")
        for e in self._entries.values():
            raw = e.get("path")
            if not isinstance(raw, str) or not raw or Path(raw).is_absolute() or _is_unc_text(raw):
                continue
            target = _realpath(base / raw)
            inside = target != base and target.is_relative_to(base)
            in_generated = target.is_relative_to(generated)
            refuse = None
            if not inside:
                refuse = "leaves the library folder"
            elif in_generated and e.get("origin") != "generated":
                refuse = "points into the folder DPC generates into, which only a generated take may do"
            e["path"] = str(target)
            if refuse and not e.get("missing"):
                e["missing"] = True
            if refuse:
                logger.warning("music: the relative path %r of %s %s; the entry is kept as missing",
                               raw, e.get("id"), refuse)
            else:
                logger.info("music: the relative path %r of %s is taken from the library folder: %s",
                            raw, e.get("id"), target)
            changed = True
        return changed

    def _create_root_and_load(self) -> None:
        """Make `<DPC home>/music/` if it is missing, then `load()`. Blocking; `open()`
        calls it in a thread. Only the root is made: `lyrics/` and `files/` stay lazy.
        A home that cannot be written is logged and the library still loads."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("music: could not create %s", self.root, exc_info=True)
        self.load()

    def _apply_folder_limit(self) -> bool:
        """Keep at most `MAX_FOLDERS` folders active: the first by `added_at`. The rest
        (a hand-edited `folders.json`) stay in the file, `unavailable` with a reason,
        and are not scanned; removing an active folder lets the next one in. Returns
        whether a folder record changed."""
        ordered = sorted(self._folders.values(), key=lambda f: (f.get("added_at") or "", f["id"]))
        over = {f["id"] for f in ordered[MAX_FOLDERS:]}
        changed = False
        for f in ordered:
            if f["id"] in over:
                reason = (f"over the limit of {MAX_FOLDERS} watched folders, so it is not scanned; "
                          f"remove another folder to let it in")
                if f.get("watch_state") != "unavailable" or f.get("state_reason") != reason:
                    f["watch_state"], f["state_reason"] = "unavailable", reason
                    changed = True
            elif f["id"] in self._over_limit:
                # Let back in: scanned on the next poll or rescan.
                f["watch_state"] = "polling" if f["watch"] else "off"
                f["state_reason"] = None
                changed = True
        self._over_limit = over
        return changed

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

    def _take_snapshot(self) -> Tuple[int, List[Tuple[Path, str]]]:
        texts = self._snapshot_texts()
        self._snapshot_seq += 1
        return self._snapshot_seq, texts

    def _write_snapshot(self, seq: int, texts: List[Tuple[Path, str]]) -> None:
        """Write one snapshot under the disk lock; skipped when a newer one is already
        on disk (a `save_now()` that ran while a flush's thread waited)."""
        with self._disk_lock:
            if seq <= self._written_seq:
                return
            for path, text in texts:
                atomic_write_text(path, text)
            self._written_seq = seq

    def save_now(self) -> None:
        """Write the three files now. Blocking; for tests and shutdown.

        Takes the write lock: a flush whose thread is writing finishes first, and its
        older snapshot can no longer land after this one."""
        self._dirty = False
        try:
            self._write_snapshot(*self._take_snapshot())
        except BaseException:
            self._dirty = True
            raise

    async def flush(self) -> None:
        """Write the three files now, off the loop.

        Writes are serialised: the snapshot is taken inside the lock, so a later flush
        never finishes before an earlier one and leaves the older text on disk. A
        failed write leaves the library dirty, so the next save tries again."""
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        async with self._write_lock:
            self._dirty = False
            try:
                seq, texts = self._take_snapshot()
                write = asyncio.ensure_future(asyncio.to_thread(self._write_snapshot, seq, texts))
            except BaseException:
                self._dirty = True
                raise
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                # `stop()` cancels a pending save: keep the lock until the thread is done,
                # so the final flush's newer text is the one that lands last.
                self._dirty = True
                await asyncio.wait({write})
                raise
            except BaseException:
                self._dirty = True
                raise

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
        """Save until nothing is pending. A mutation while a flush is in its thread
        sets `_dirty` again but starts no task (this one is still running), so the
        loop, not the mutation, picks it up. A failed save stops the loop with the
        library still dirty: the next mutation or `stop()` retries."""
        while self._dirty:
            await asyncio.sleep(self.save_delay_s)
            if not self._dirty:
                break
            try:
                await self.flush()
            except Exception:
                logger.exception("music: saving the library failed")
                break

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
            await asyncio.to_thread(self._create_root_and_load)
            for e in self._entries.values():
                if e.get("hash_state") == "pending" and not e.get("missing"):
                    self._enqueue(e["id"])
            if self._dirty:
                self._mark_dirty()
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
        if self._change_handle is not None:
            self._change_handle.cancel()
            self._change_handle = None
        if self._loaded and self._dirty:
            try:
                await self.flush()
            except Exception:
                # Shutdown goes on; what was not written is lost, and the log says so.
                logger.exception("music: the final save at shutdown failed")

    async def _poll_loop(self) -> None:
        while True:
            for folder_id in [f["id"] for f in self._folders.values()
                              if f["watch"] and f["id"] not in self._over_limit]:
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

    # -- change events -----------------------------------------------------------

    def notify_changed(self, reason: str, track_ids: Iterable[str] = ()) -> None:
        """Record that what the library lists changed (`reason`: scan, hash, folder, track,
        playlist, take, export). Changes inside one window share the `on_changed` event that
        closes it; with no running loop the event goes out at once."""
        self._change_reasons.add(reason)
        self._change_ids.update(i for i in track_ids if isinstance(i, str))
        if self.on_changed is None or self._change_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._emit_change()
            return
        self._change_handle = loop.call_later(self.change_delay_s, self._emit_change)

    def _emit_change(self) -> None:
        self._change_handle = None
        reasons, ids = sorted(self._change_reasons), sorted(self._change_ids)
        self._change_reasons, self._change_ids = set(), set()
        if self.on_changed is None or not reasons:
            return
        self.change_version += 1
        payload = {"version": self.change_version, "reasons": reasons,
                   "track_ids": ids[:MAX_CHANGE_IDS], "track_ids_truncated": len(ids) > MAX_CHANGE_IDS}
        try:
            self.on_changed(payload)
        except Exception:
            logger.exception("music: the on_changed listener failed")

    def _signature(self) -> str:
        """A digest of what the library lists, to tell a poll that found nothing from one
        that changed something; `last_scan_at` moves on every poll and is left out."""
        h = hashlib.blake2b(digest_size=16)
        h.update(json.dumps(list(self._entries.values()), default=str).encode("utf-8"))
        h.update(json.dumps([{**f, "last_scan_at": None} for f in self._folders.values()],
                            default=str).encode("utf-8"))
        h.update(json.dumps(list(self._playlists.values()), default=str).encode("utf-8"))
        return h.hexdigest()

    # -- the hash index ----------------------------------------------------------

    @staticmethod
    def _hash_key(e: dict) -> Optional[Tuple[str, str]]:
        if e.get("hash_state") != "done" or not e.get("content_hash") or not e.get("hash_kind"):
            return None
        return (e["hash_kind"], e["content_hash"])

    def _index_add(self, e: dict) -> None:
        """Every entry with a hash is indexed, not only the first: two entries can share
        one (a library written before collapsing, or loaded from an import)."""
        key = self._hash_key(e)
        if key is not None:
            ids = self._by_hash.setdefault(key, [])
            if e["id"] not in ids:
                ids.append(e["id"])

    def _index_drop(self, e: dict) -> None:
        key = self._hash_key(e)
        ids = self._by_hash.get(key) if key is not None else None
        if ids and e["id"] in ids:
            ids.remove(e["id"])
            if not ids:
                del self._by_hash[key]

    def _delete_entry(self, track_id: str, *, replaced_by: Optional[str] = None) -> None:
        """Drop an entry. Its playlist positions move to `replaced_by` when given (a
        merge), else they are removed. A merge into a track the playlist already holds
        drops the merged positions instead, so the collapse adds no duplicate; a
        repeat the person put there (the same id twice) is left alone."""
        e = self._entries.pop(track_id, None)
        if e is None:
            return
        self._index_drop(e)
        self._verify.pop(track_id, None)
        if replaced_by and replaced_by != track_id and self.on_merged is not None:
            try:
                self.on_merged(track_id, replaced_by)
            except Exception:
                logger.exception("music: the on_merged listener failed for %s -> %s", track_id, replaced_by)
        for pl in self._playlists.values():
            if track_id in pl["track_ids"]:
                if replaced_by and replaced_by not in pl["track_ids"]:
                    pl["track_ids"] = [replaced_by if t == track_id else t for t in pl["track_ids"]]
                else:
                    pl["track_ids"] = [t for t in pl["track_ids"] if t != track_id]
                pl["updated_at"] = _now()

    def _merge_missing_twins(self, key: Optional[Tuple[str, str]]) -> int:
        """When a present entry holds this hash, merge every missing entry with the same
        hash into it (the oldest present one): their playlist positions move to it, or
        are dropped where the playlist already holds it. One audio, one entry. Returns
        how many were merged."""
        if key is None:
            return 0
        twins = [self._entries[i] for i in self._by_hash.get(key, []) if i in self._entries]
        present = sorted((t for t in twins if not t.get("missing")),
                         key=lambda t: (t.get("added_at") or "", t["id"]))
        if not present:
            return 0
        keeper = present[0]
        merged = 0
        for t in twins:
            if t.get("missing"):
                logger.info("music: merged missing %s into %s (same audio hash)", t["id"], keeper["id"])
                self._delete_entry(t["id"], replaced_by=keeper["id"])
                merged += 1
        return merged

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
        # The summary of the file's own provenance record (engine, seed, has_codes); never a path.
        if ident.provenance:
            e["provenance"] = copy.deepcopy(ident.provenance)
        else:
            e.pop("provenance", None)
        # Why the hash is the kind it is ("ffmpeg absent"); empty for a normal hash.
        e["hash_notes"] = [str(n) for n in ident.notes]

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
                if track_id in self._reprobe:
                    self._reprobe.discard(track_id)
                    if e.get("hash_state") == "done":
                        meta = await asyncio.to_thread(probe_metadata, Path(path), tools)
                        current = self._entries.get(track_id)
                        if current is not None and path_key(current.get("path") or "") == path_key(path):
                            self._apply_probe(current, meta)
                        continue
                    # Not hashed (changed meanwhile): the full pass below reads it.
                marker = await asyncio.to_thread(read_export_marker, Path(path))
                if marker is not None:
                    self._ignore_export(track_id, path, marker[0])
                    continue
                try:
                    ident = await asyncio.to_thread(identify, Path(path), tools)
                except OSError as exc:
                    logger.info("music: cannot hash %s: %s", path, exc)
                    current = self._entries.get(track_id)
                    if current is not None and current.get("hash_state") == "pending":
                        current["hash_state"] = "error"
                        self._mark_dirty()
                        self.notify_changed("hash", [track_id])
                    self._verify.pop(track_id, None)
                    continue
                except Exception:
                    logger.exception("music: hashing %s failed", path)
                    self._verify.pop(track_id, None)
                    continue
                if ident.export_of:
                    self._ignore_export(track_id, path, ident.export_of)
                    continue
                self._apply_identity(track_id, path, ident)
            self._wake.clear()
            self._idle.set()

    def _ignore_export(self, track_id: str, path: str, source_hash: str) -> None:
        """Drop the entry of a walked file that turned out to be a DPC export, and remember
        the file so the next poll skips it (see the module docstring)."""
        e = self._entries.get(track_id)
        if e is None or path_key(e.get("path") or "") != path_key(path):
            return
        try:
            st = os.stat(path)
            self._ignored[path_key(path)] = (st.st_size, st.st_mtime_ns)
        except OSError:
            pass
        logger.info("music: %s is a DPC export of %s, not a new track; it is left out of the library",
                    path, source_hash)
        self._verify.pop(track_id, None)
        self._delete_entry(track_id)
        self._mark_dirty()
        self.notify_changed("scan", [track_id])

    def _without_exports(self, found: List[Tuple[str, int, int]]) -> List[Tuple[str, int, int]]:
        """The walked files minus the ones already known as exports and the ones the export
        index lists."""
        kept: List[Tuple[str, int, int]] = []
        for path, size, mtime in found:
            key = path_key(path)
            if self._ignored.get(key) == (size, mtime):
                continue
            reason = None
            if self.exclude_path is not None:
                try:
                    reason = self.exclude_path(path)
                except Exception:
                    logger.exception("music: the export check failed for %s", path)
            if reason:
                logger.info("music: %s is left out of the library: %s", path, reason)
                self._ignored[key] = (size, mtime)
                continue
            kept.append((path, size, mtime))
        return kept

    def _apply_probe(self, e: dict, meta: Dict[str, Any]) -> None:
        """Fill what a late ffprobe found on a hashed entry: the duration, and the tags
        the scan could not read (the title only while it is still the file name)."""
        changed = False
        if e.get("duration_s") is None and meta.get("duration_s") is not None:
            e["duration_s"] = round(float(meta["duration_s"]), 3)
            changed = True
        if meta.get("provenance") and not e.get("provenance"):
            e["provenance"] = copy.deepcopy(meta["provenance"])
            changed = True
        for key in ("artist", "album"):
            if not e.get(key) and meta.get(key):
                e[key] = meta[key]
                changed = True
        if meta.get("title") and e.get("title") == Path(e["path"]).stem:
            e["title"] = meta["title"]
            changed = True
        if changed:
            self._mark_dirty()
            self.notify_changed("hash", [e["id"]])

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
                self.notify_changed("hash", [track_id])
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
        self.notify_changed("hash", [track_id, e["id"]])

    def _same_audio(self, e: dict, key: Tuple[str, str]) -> Tuple[List[dict], List[dict]]:
        """The other entries with this hash, (present, missing), each in a fixed order.
        Present: oldest first. Missing: the likeliest source of `e` first: the same
        file name (a move), then the same folder (a rename), then the oldest."""
        others = [self._entries[i] for i in self._by_hash.get(key, []) if i != e["id"] and i in self._entries]
        name = Path(e.get("path") or "").name.casefold()
        present = sorted((o for o in others if not o.get("missing")),
                         key=lambda o: (o.get("added_at") or "", o["id"]))
        missing = sorted((o for o in others if o.get("missing")), key=lambda o: (
            Path(o.get("path") or "").name.casefold() != name,
            o.get("folder_id") != e.get("folder_id"),
            o.get("added_at") or "",
            o["id"],
        ))
        return present, missing

    def _resolve(self, e: dict, ident: Identity, stat: Tuple[int, int]) -> None:
        """Give a freshly hashed entry its identity: relink, collapse, or keep it.

        When several missing entries share the hash, the first by `_same_audio` takes
        the new path and the others are merged into it (their playlist positions move
        to it): one audio, one entry, the same rule as a duplicate."""
        key = (ident.hash_kind, ident.content_hash)
        present, missing = self._same_audio(e, key)
        other = present[0] if present else (missing[0] if missing else None)
        if other is not None and other.get("missing"):
            # A move or a rename: the known track takes the new path.
            logger.info("music: relinked %s to %s (same audio hash)", other["id"], e["path"])
            self._set_path(other, e["path"], e.get("folder_id"), stat)
            self._apply_metadata(other, ident)
            self._delete_entry(e["id"], replaced_by=other["id"])
            for extra in missing[1:]:
                logger.info("music: merged missing %s into %s (same audio hash)", extra["id"], other["id"])
                self._delete_entry(extra["id"], replaced_by=other["id"])
            return
        if other is not None:
            # The same audio at a second path: one entry, the path in alt_paths.
            if path_key(e["path"]) not in {path_key(p) for p in [other["path"], *other.get("alt_paths", [])]}:
                other.setdefault("alt_paths", []).append(e["path"])
            self._delete_entry(e["id"], replaced_by=other["id"])
            self._merge_missing_twins(key)
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

    def _check_watchable(self, chosen: Path) -> None:
        """Raise `MusicLibraryError` unless `chosen` (absolute, normalised) may be
        watched. Blocking: `add_folder` calls it in a thread. The checks run on the
        path as given and on its real path, so a symlink or a `subst` drive does not
        get past them."""
        if _is_network_drive(chosen):
            raise MusicLibraryError("network_path", f"a network drive cannot be watched: {chosen}")
        if not chosen.is_dir():
            raise MusicLibraryError("not_a_folder", f"not a folder, or not reachable: {chosen}")
        real = _realpath(chosen)
        if _is_unc_text(str(real)) or (real != chosen and _is_network_drive(real)):
            raise MusicLibraryError("network_path", f"a network path cannot be watched: {chosen} is {real}")

        def keys(p: Path) -> Set[str]:
            return {path_key(str(p)), path_key(str(_realpath(p)))}

        try:
            home_keys = keys(Path.home())
        except (RuntimeError, KeyError, OSError):  # no home folder known to the OS
            home_keys = set()
        dpc_keys = keys(self.dpc_home)
        music_keys = keys(self.root)
        for candidate in (chosen, real):
            if candidate.parent == candidate or str(candidate) == candidate.anchor:
                raise MusicLibraryError("protected_folder", f"a filesystem root cannot be watched: {chosen}")
            key = path_key(str(candidate))
            if key in home_keys:
                raise MusicLibraryError("protected_folder", f"the home folder itself cannot be watched: {chosen}")
            if any(_is_inside(key, m) for m in music_keys):
                continue  # the DPC home's own music/ subtree is allowed
            if any(_is_inside(key, d) for d in dpc_keys):
                raise MusicLibraryError("protected_folder", f"inside the DPC home, which cannot be watched: {chosen}")
            if any(_is_inside(d, key) for d in dpc_keys):
                raise MusicLibraryError("protected_folder", f"contains the DPC home, which cannot be watched: {chosen}")

    async def add_folder(self, path: str, *, watch: bool = True, scan: bool = True) -> dict:
        """Add a watched folder (only the person does this; SPEC §6). The first scan
        starts in the background when `scan`."""
        if not isinstance(path, str) or not path.strip():
            raise MusicLibraryError("invalid_path", "a folder path is required")
        if len(self._folders) >= MAX_FOLDERS:
            raise MusicLibraryError("too_many_folders", f"at most {MAX_FOLDERS} watched folders")
        text = path.strip()
        # Before any filesystem call: a check of an unreachable share can hang for seconds.
        if _is_unc_text(text):
            raise MusicLibraryError("network_path", f"a network (UNC) path cannot be watched: {text}")
        chosen = Path(text).expanduser()
        if not chosen.is_absolute():
            raise MusicLibraryError("invalid_path", f"not an absolute path: {path}")
        chosen = Path(os.path.normpath(chosen))
        await asyncio.to_thread(self._check_watchable, chosen)
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
        self.notify_changed("folder")
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
        self._apply_folder_limit()
        self.notify_changed("folder")
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
            if folder_id in self._over_limit:
                return copy.deepcopy(folder)  # over MAX_FOLDERS: kept, not scanned
            root = folder["path"]
            fkey = path_key(root)
            outside_alts = [
                a for e in self._entries.values()
                if e.get("folder_id") == folder_id and not e.get("missing")
                for a in e.get("alt_paths") or [] if not _is_inside(path_key(a), fkey)
            ]

            def check_and_walk():
                # The add-time checks again, right before the walk: the root may have
                # been replaced since (a symlink into the DPC home, a network drive).
                self._check_watchable(Path(root))
                return walk_audio_files(Path(root)), _stat_paths(outside_alts)

            try:
                (found, failed), alt_stats = await asyncio.to_thread(check_and_walk)
            except MusicLibraryError as exc:
                if folder_id in self._folders:
                    was = (folder.get("watch_state"), folder.get("state_reason"))
                    folder["watch_state"] = "unavailable"
                    folder["state_reason"] = (
                        f"refused on rescan ({exc.code}), so it was not scanned and no track "
                        f"was marked missing: {exc.message}"
                    )
                    self._mark_dirty()
                    if was != (folder["watch_state"], folder["state_reason"]):
                        self.notify_changed("folder")
                return copy.deepcopy(folder)
            except FolderUnavailable as exc:
                if folder_id in self._folders:
                    was = (folder.get("watch_state"), folder.get("state_reason"))
                    folder["watch_state"] = "unavailable"
                    folder["state_reason"] = f"cannot read the folder: {exc}"
                    self._mark_dirty()
                    if was != (folder["watch_state"], folder["state_reason"]):
                        self.notify_changed("folder")
                return copy.deepcopy(folder)
            if folder_id not in self._folders:
                raise MusicLibraryError("unknown_folder", f"no watched folder {folder_id}")
            reprobe = False
            if any(self._wants_reprobe(e) for e in self._entries.values() if e.get("folder_id") == folder_id):
                tools = self._tools or await asyncio.to_thread(MediaTools.discover)
                reprobe = bool(tools.ffprobe)  # only when ffprobe can answer now
            before = self._signature() if self.on_changed is not None else None
            self._apply_scan(folder, found, alt_stats, failed=failed, reprobe=reprobe)
            self._mark_dirty()
            if before is not None and before != self._signature():
                self.notify_changed("scan")
            return copy.deepcopy(folder)

    async def rescan_all(self) -> List[dict]:
        return [await self.rescan_folder(fid) for fid in list(self._folders)]

    def _wants_reprobe(self, e: dict) -> bool:
        """A present, hashed entry with no duration that was not re-probed in this process."""
        return (not e.get("missing") and e.get("hash_state") == "done" and e.get("duration_s") is None
                and bool(e.get("path")) and e["id"] not in self._reprobed)

    def _apply_scan(self, folder: dict, found: List[Tuple[str, int, int]], alt_stats: Dict[str, Tuple[int, int]],
                    *, failed: Optional[List[Tuple[str, str]]] = None, reprobe: bool = False) -> None:
        """Reconcile one walk. With `failed` non-empty the walk was partial: new and
        changed files are still taken in, but nothing is marked missing or dropped
        from `alt_paths`, the counts and `last_scan_at` keep the last full scan, and
        the folder is `unavailable` until a walk completes. With `reprobe` (ffprobe is
        available now) an unchanged entry that has no duration is probed again, once per
        process and without hashing: its mtime did not change, so nothing else would."""
        folder_id = folder["id"]
        fkey = path_key(folder["path"])
        found = self._without_exports(found)
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
                    if reprobe and self._wants_reprobe(e):
                        self._reprobed.add(e["id"])
                        self._reprobe.add(e["id"])
                        self._enqueue(e["id"])
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

        if failed:
            first_path, first_reason = failed[0]
            more = f" (and {len(failed) - 1} more)" if len(failed) > 1 else ""
            folder["watch_state"] = "unavailable"
            folder["state_reason"] = (
                f"part of the folder could not be read, so no track was marked missing: "
                f"{first_path}: {first_reason}{more}"
            )
            return

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
                # A present twin elsewhere (another entry with this hash) takes it in.
                self._merge_missing_twins(self._hash_key(e))
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

    # -- generated takes ---------------------------------------------------------

    def _find_by_path(self, key: str) -> Optional[dict]:
        """The present entry that holds this path, as its path or one of its `alt_paths`
        (the same lookup `_apply_scan` makes), whatever its origin."""
        for e in self._entries.values():
            if e.get("missing") or not e.get("path"):
                continue
            if path_key(e["path"]) == key or any(path_key(a) == key for a in e.get("alt_paths") or []):
                return e
        return None

    def _new_generated_entry(self, path: str, job_id: str, lineage: Optional[dict], size: int, mtime_ns: int) -> dict:
        p = Path(path)
        entry = {
            "schema": schemas.LIBRARY_ENTRY,
            "id": _new_id("trk_"),
            "title": p.stem,
            "source": {
                "kind": "generated", "name": p.name, "uri": p.as_uri(),
                "size_bytes": size, "mime": MIME_BY_EXTENSION.get(p.suffix.lower()),
                "lineage": {**copy.deepcopy(lineage or {}), "job_id": job_id},
            },
            "source_hash": f"path:{path_key(path)}",
            "content_hash": None,
            "duration_s": None,
            "added_at": _now(),
            "persist": "full",
            "placeholder": None,
            "origin": "generated",
            "path": path,
            "folder_id": None,
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

    async def register_generated(self, path: str, *, job_id: str, lineage: Optional[dict] = None) -> dict:
        """Put a generated take's file into the library, once (ADR-051 D21), and return
        its entry. This is the only way a take enters the library: `origin: "generated"`
        is set here and nowhere else, and the "Generated" smart list is derived from it.

        Idempotent, by the identity the library already uses: a path the library holds
        (as a path or an `alt_paths` entry, any origin) returns that entry; a new path
        with the audio hash of a known entry is that entry (a missing one is relinked, a
        present one gets the path in `alt_paths`), as `_resolve` does for a scan. An
        existing entry is returned as it is: its `origin` is never changed, so a file a
        folder scan took in first stays `origin: "folder"`, and a later scan of a
        watched folder that holds a generated file keeps the one entry and its origin
        (`_apply_scan` matches it by path, and `file_stat` and the hash are set here).
        `on_added` is called only for a new entry. Raises `MusicLibraryError`
        (`invalid_path`, `invalid_job`, `not_a_file`, `not_audio`, `unreadable`)."""
        if not isinstance(path, str) or not path.strip() or not Path(path).is_absolute():
            raise MusicLibraryError("invalid_path", f"an absolute file path is required: {path!r}")
        if not isinstance(job_id, str) or not job_id.strip():
            raise MusicLibraryError("invalid_job", "a job id is required")
        if lineage is not None and not isinstance(lineage, dict):
            raise MusicLibraryError("invalid_lineage", "lineage must be an object")
        p = Path(os.path.normpath(path))
        if p.suffix.lower() not in AUDIO_EXTENSIONS:
            raise MusicLibraryError("not_audio", f"not an audio file: {p}")
        key = path_key(str(p))
        existing = self._find_by_path(key)
        if existing is not None:
            return copy.deepcopy(existing)
        tools = self._tools or await asyncio.to_thread(MediaTools.discover)
        try:
            ident = await asyncio.to_thread(identify, p, tools)
        except OSError as exc:
            raise MusicLibraryError("unreadable", f"cannot read {p}: {exc}")
        # From here to the insert there is no await: a second call, or a scan, that took
        # the path while this one hashed is found now, and nothing is added twice.
        existing = self._find_by_path(key)
        if existing is not None:
            return copy.deepcopy(existing)
        stat = (ident.size_bytes, ident.mtime_ns)
        e = self._new_generated_entry(str(p), job_id.strip(), lineage, *stat)
        present, missing = self._same_audio(e, (ident.hash_kind, ident.content_hash))
        other = present[0] if present else (missing[0] if missing else None)
        self._resolve(e, ident, stat)
        if other is None:
            result = e
            if self.on_added is not None:
                try:
                    self.on_added(copy.deepcopy(result))
                except Exception:
                    logger.exception("music: the on_added listener failed for %s", result["id"])
        else:
            result = self._entries[other["id"]]
        snapshot = copy.deepcopy(result)
        self.notify_changed("take", [result["id"]])
        await self.flush()
        return snapshot

    # -- library -----------------------------------------------------------------

    def list_entries(self, *, folder_id: Optional[str] = None, include_missing: bool = True) -> List[dict]:
        out = []
        for e in self._entries.values():
            if folder_id is not None and e.get("folder_id") != folder_id:
                continue
            if not include_missing and e.get("missing"):
                continue
            out.append({**copy.deepcopy(e), "deletable": self._is_deletable(e)})
        return out

    def get_entry(self, track_id: str) -> dict:
        e = self._entries.get(track_id)
        if e is None:
            raise MusicLibraryError("unknown_track", f"no track {track_id}")
        return {**copy.deepcopy(e), "deletable": self._is_deletable(e)}

    async def content_hash_of(self, path: Path) -> str:
        """The content hash of the file at `path` now, by the same identity function and media tools the
        library hashes its entries with (`identify`): a region edit re-hashes its base at run start and
        refuses a file that changed since the proposal (SPEC §6 `hash_mismatch`). Raises `OSError` when the
        file cannot be read."""
        tools = self._tools or await asyncio.to_thread(MediaTools.discover)
        ident = await asyncio.to_thread(identify, Path(path), tools)
        return ident.content_hash

    def get_track_path(self, track_id: str) -> Optional[Path]:
        """Where the track's bytes are now, or None when it is missing or has no file.
        The track stream serves these bytes by `track_id` (`music/stream.py`, ADR-051 N8)."""
        e = self._entries.get(track_id)
        if e is None or e.get("missing") or not e.get("path"):
            return None
        path = Path(e["path"])
        # `load()` anchors a hand-edited relative path; one that is still relative is not served
        # from whatever the working directory is.
        return path if path.is_absolute() else None

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

    # -- hide, remove, delete ----------------------------------------------------

    async def set_hidden(self, track_id: str, hidden: bool) -> dict:
        """Hide or unhide a track. The file is untouched. Returns the entry."""
        e = self._entries.get(track_id)
        if e is None:
            raise MusicLibraryError("unknown_track", f"no track {track_id}")
        if bool(e.get("hidden")) != hidden:
            e["hidden"] = hidden
            self.notify_changed("track", [track_id])
            await self.flush()
        return copy.deepcopy(e)

    async def remove_missing(self, track_id: Optional[str] = None) -> List[str]:
        """Drop one missing entry, or every missing entry, from the library and from
        the playlists. A present entry is refused (`not_missing`). Returns the ids."""
        if track_id is not None:
            e = self._entries.get(track_id)
            if e is None:
                raise MusicLibraryError("unknown_track", f"no track {track_id}")
            if not e.get("missing"):
                raise MusicLibraryError(
                    "not_missing", f"{e.get('title')!r} is not missing: Hide it, or delete its file if DPC made it")
            ids = [track_id]
        else:
            ids = [i for i, e in self._entries.items() if e.get("missing")]
        for i in ids:
            self._delete_entry(i)
        if ids:
            self.notify_changed("track", ids)
            await self.flush()
        return ids

    def _generated_root(self) -> Path:
        return (self.root / "generated").resolve()

    def _is_deletable(self, e: dict) -> bool:
        """The `deletable` flag the list responses carry: present, owned by path, file there."""
        try:
            return self._owned_file(e).is_file()
        except (MusicLibraryError, OSError):
            return False

    def _owned_file(self, e: dict) -> Path:
        """The resolved path of an entry's file when DPC owns it, else a refusal."""
        raw = e.get("path")
        if not raw or e.get("missing"):
            raise MusicLibraryError("file_missing", "the file is already gone: use Remove from library")
        if not Path(raw).is_absolute():
            raise MusicLibraryError("invalid_path", "the path is not absolute, so DPC does not own the file")
        try:
            target = Path(raw).resolve()
            owned = self._generated_root()
        except (OSError, RuntimeError, ValueError) as exc:
            raise MusicLibraryError("invalid_path", f"cannot resolve the path: {exc}")
        if target == owned or not target.is_relative_to(owned):
            raise MusicLibraryError(
                "not_owned", f"this file is outside {owned}, so DPC does not own it and will not delete it. Hide it instead")
        return target

    async def delete_generated_file(self, track_id: str) -> dict:
        """Delete the audio file of a DPC-owned generated take, and its `<name>.json`
        sidecar when there is one, then drop the entry. Returns `{id, title, deleted}`
        (the paths removed)."""
        e = self._entries.get(track_id)
        if e is None:
            raise MusicLibraryError("unknown_track", f"no track {track_id}")
        target = self._owned_file(e)
        sidecar = target.with_suffix(".json")
        try:
            if not target.is_file():
                raise MusicLibraryError("file_missing", "the file is not there: use Remove from library")
            target.unlink()
            deleted = [str(target)]
            if sidecar.is_file() and sidecar.resolve().is_relative_to(self._generated_root()):
                sidecar.unlink()
                deleted.append(str(sidecar))
        except OSError as exc:
            raise MusicLibraryError("delete_failed", f"could not delete {target.name}: {exc}")
        title = e.get("title")
        self._delete_entry(track_id)
        self.notify_changed("track", [track_id])
        await self.flush()
        return {"id": track_id, "title": title, "deleted": deleted}

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
                   if not e.get("hidden") and (rule == "all" or e.get("origin") == "generated")]
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
        self.notify_changed("playlist")
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
        self.notify_changed("playlist")
        await self.flush()
        return copy.deepcopy(pl)

    async def delete_playlist(self, playlist_id: str) -> None:
        """Delete a playlist; its tracks stay in the library."""
        self._user_playlist(playlist_id)
        del self._playlists[playlist_id]
        self.notify_changed("playlist")
        await self.flush()
