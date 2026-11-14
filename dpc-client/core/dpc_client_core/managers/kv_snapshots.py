"""KV snapshots: the index, sweep, naming, threshold and records beside the supervisors (ADR-044).

The amendment of 2026-11-13 ("KV snapshots: the live conversation is saved to disk
at a stop and restored into an empty engine") is the spec; the numbered points
below are its points. This module is pure state and arithmetic: no HTTP, no
process, nothing imported from either supervisor. The supervisors and the
providers call it; tests drive it with `tmp_path` and a clock passed in.

What lives here:
- the folder rules (point 6): validation of `slot_save_path`, the two alias
  fields, one folder per alias, and the missing-disk case at launch;
- the index `dpc-kv-snapshots.json` (point 6), the record of which files in the
  folder are DPC's. Only names with DPC's prefix (`dpc-kv-<hex>.bin`, `.staging`)
  are ever deleted, so a foreign file (Strata's image job may write session files
  there) is never counted and never deleted, whatever an index says;
- the last-call record per alias (point 2): a call is noted in `_ensure` and becomes
  the record when it goes on the wire (`mark_sent`), the latest sent wins; and the
  cleared-conversation mark (point 7);
- the bound, the size threshold and free space (point 4), and the status classes
  of a save and a restore (points 4, 5, 8);
- per-launch outcome state for the Local engines block (point 10).

The last-call record and the cleared mark are in memory only: after a backend
restart there is no record, so nothing is saved until a call is sent (a safe default).
An entry whose file the OS refused to delete stays in the index, marked, and every
sweep tries again.

Times (`now`, `at`, `saved_at`, `expires_at`) are wall-clock seconds
(`time.time()`): the index outlives the process. The order of calls is not read
off them (a 15.6 ms tick on Windows, NTP steps): it is a counter taken at `mark_sent`.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from .. import node_ledger
from ..settings import _probe_writable, _system_folders

logger = logging.getLogger(__name__)

PathLike = Union[str, "os.PathLike[str]"]

# ---------------------------------------------------------------- constants

KV_SNAPSHOT_WRITE_CEILING_S = 60.0
INDEX_NAME = "dpc-kv-snapshots.json"
INDEX_VERSION = 1
DEFAULT_TTL_S = 86_400
DEFAULT_MAX_GIB = 32
GIB = 1024 ** 3
# A write or restore counts as a rate sample only from this size and duration up: a small
# or near-instant file measures latency, not the disk. Production files start at ~236 MB.
RATE_SAMPLE_MIN_BYTES = 16 * 1024 ** 2
RATE_SAMPLE_MIN_MS = 50
# Free space a save leaves on the disk beyond its own estimate (point 4): Strata's
# own session reserve (`session_min_free_mib = 4096`, an engine default read in the
# Strata repository copy, not passed by DPC); below it the engine answers 507.
FREE_SPACE_FLOOR_BYTES = 4096 * 1024 ** 2

# Seed sizes per engine (provider type) until a save measures the alias: a floor
# per file plus a part per token, the two-point fit of 2026-11-13 (spec, "What was
# measured"): Strata ~236 MB + ~15.3 MB per 1K tokens, llama-server ~0.47 GB + ~22.6 KB per token.
SEED_SIZES: Dict[str, Dict[str, float]] = {
    "strata": {"floor_bytes": 236e6, "bytes_per_token": 15.3e3},
    "llamacpp_server": {"floor_bytes": 0.47e9, "bytes_per_token": 22.6e3},
}

# File names: opaque random hex, so a listing names no agent and no conversation.
# Strata refuses a leading dot and path characters (`slot_filename_problem`, pin v0.2.11).
# The `dpc-kv-` prefix is what makes a file DPC's: nothing else is ever deleted.
FILE_PREFIX = "dpc-kv-"
STAGING_SUFFIX = ".staging"
FINAL_SUFFIX = ".bin"
_FINAL_RE = re.compile(r"^dpc-kv-[0-9a-f]+\.bin$")
_STAGING_RE = re.compile(r"^dpc-kv-[0-9a-f]+\.staging$")
# The index's own temporary file (`write_index`), left behind only by a crash.
_INDEX_TMP_PREFIX = ".dpc-kv-snapshots-"
_INDEX_TMP_RE = re.compile(r"^\.dpc-kv-snapshots-[A-Za-z0-9_]+\.tmp$")
_CORRUPT_SUFFIX = ".corrupt-"
_REPLACE_TRIES = 5
_REPLACE_PAUSE_S = 0.05

# Where an unmounted removable disk leaves an empty directory (heuristic, `prepare_folder`).
_REMOVABLE_ROOTS = {
    "linux": ("/mnt", "/media", "/run/media"),
    "darwin": ("/Volumes",),
}

# Caller kinds of the last-call record. The ledger's words (`node_ledger.CALLER_KINDS`)
# plus one for an owner's chat, whose spelling waits on the group naming check:
# named once here so a rename touches one line.
OWNER_CHAT_KIND = "owner"
OWNER_KINDS = ("agent", OWNER_CHAT_KIND)
# Noted calls kept per alias while they wait for `mark_sent`; older ones are dropped.
PENDING_PER_ALIAS = 64
PENDING_DROPPED = "pending call dropped"          # its once-per-launch warning key
# Every word a record may carry; any other is recorded as None, so a typo never becomes an owner.
# The ledger's three words plus `owner`, which the ledger refuses.
SNAPSHOT_CALLER_KINDS = node_ledger.CALLER_KINDS + (OWNER_CHAT_KIND,)

# The off reason of a launch whose folder's disk is missing (unplugged, unmounted).
FOLDER_MISSING = "folder missing"
ENGINE_REFUSES_SAVES = "the engine refuses saves"
# Strata answers 507 when the disk lacks the file plus its reserve; DPC cannot see that reserve,
# so after this many in a row on one alias the launch stops asking.
STORAGE_REFUSALS_TO_SUSPEND = 3
STORAGE_REFUSED_SAVES = "the engine refused 3 saves in a row for lack of disk (507)"

# Sweep reasons.
EXPIRED, VERSION, MODEL, DRAFTER = "expired", "version", "model", "drafter"
OVER_CAP, STAGING_LEFT, MISSING, ORPHAN = "over_cap", "staging_left", "missing", "orphan"
REPLACED, CLEARED, REMOVED = "replaced", "cleared", "removed"
# Which sweep reason a mismatch of each expected identity key reads as.
IDENTITY_REASONS = {
    "engine_version": VERSION,
    "model": MODEL,
    "gguf_path": MODEL,
    "drafter": DRAFTER,
}

# Outcome kinds of a save and a restore.
SAVED, ENGINE_REFUSES, TRANSPORT, ENGINE_DIED, FAILED = (
    "saved", "engine_refuses", "transport", "engine_died", "failed")
NOT_LOADED = "not_loaded"
SKIPPED = "skipped"                               # a save not tried; the caller gives the reason
RESTORED, REFUSED, NO_ROOM = "restored", "refused", "no_room"
# DPC's or the configuration's fault at either call (point 8): own reason each.
TRANSPORT_REASONS = {
    415: "the request had no application/json content type",
    403: "the request carried a foreign Origin",
    501: "the engine has no folder or runs parallel requests",
    400: "the request named a slot or file the engine does not take",
}
# Strata's answer for a request the engine ended in (`EngineDied`, `EngineSilent`
# included, serve/server.py on pin v0.2.11): status 500, this text, no `error.kind`.
_DEATH_MARKER = "the next request starts the engine again"

# Cold-read buckets by prompt length (tokens), for `cold_rate_at`.
COLD_MIN_PROMPT = 5_000
COLD_BUCKET_EDGES = (5_000, 16_000, 32_000, 64_000, 128_000, 256_000)

# ---------------------------------------------------------------- module state

_STATE_LOCK = threading.Lock()
_FOLDER_LOCKS: Dict[str, threading.RLock] = {}
_FOLDERS: Dict[str, Dict[str, Any]] = {}          # alias -> {"folder": Path, "cap_bytes": int|None}
_RECORDS: Dict[str, Dict[str, Any]] = {}          # alias -> the last engine call
_SEQ = itertools.count(1)                         # sequence numbers of noted calls
_SENT = itertools.count(1)                        # order in which calls went on the wire
_PENDING: Dict[str, "OrderedDict[int, Dict[str, Any]]"] = {}   # alias -> noted, not yet sent
_CLEARED: Set[str] = set()                        # conversation ids cleared since their last call
_CLEARABLE: Set[str] = set()                      # conversation ids a history-clearing path can reach
_OUTCOMES: Dict[str, Dict[str, Any]] = {}         # alias -> per-launch and last outcomes
_IN_PROGRESS: Dict[str, Set[str]] = {}            # folder key -> names being written
_WARNED_BAD: Set[str] = set()                     # folder keys whose bad entries were logged
_WARNED_KINDS: Set[str] = set()                   # unknown caller kinds already logged


def reset_state() -> None:
    """Forget every record, mark, folder and outcome (tests; the index on disk stays)."""
    global _SEQ, _SENT
    with _STATE_LOCK:
        _FOLDER_LOCKS.clear()
        _FOLDERS.clear()
        _RECORDS.clear()
        _SEQ = itertools.count(1)
        _SENT = itertools.count(1)
        _PENDING.clear()
        _CLEARED.clear()
        _CLEARABLE.clear()
        _OUTCOMES.clear()
        _IN_PROGRESS.clear()
        _WARNED_BAD.clear()
        _WARNED_KINDS.clear()


def _path_key(path: PathLike, *, resolve: bool = True) -> str:
    """The one comparison of folders: resolved, normalised, case-folded where the
    filesystem usually ignores case (normcase on Windows, casefold on macOS)."""
    p = Path(path)
    if resolve:
        try:
            p = p.resolve()
        except (OSError, RuntimeError):
            pass
    key = os.path.normcase(os.path.normpath(str(p)))
    return key.casefold() if sys.platform == "darwin" else key


def _path_keys(path: PathLike) -> Set[str]:
    """As typed and as resolved (a link into a system folder)."""
    return {_path_key(path, resolve=False), _path_key(path)}


def _key(folder: PathLike) -> str:
    return _path_key(folder)


def _folder_lock(folder: PathLike) -> threading.RLock:
    """One lock per folder: the supervisors, the providers and the history-clearing hook
    (called inline from `conversation_monitor`) all reach the index, on no one thread."""
    key = _key(folder)
    with _STATE_LOCK:
        lock = _FOLDER_LOCKS.get(key)
        if lock is None:
            lock = _FOLDER_LOCKS[key] = threading.RLock()
        return lock


# ---------------------------------------------------------------- folder rules

def _inside(here: str, root: str) -> bool:
    return here == root or here.startswith(root.rstrip("\\/") + os.sep)


def folder_error(value: Any, *, other_folders: Iterable[Any] = (), install_dir: Any = None) -> Optional[str]:
    """Why `value` cannot be an alias's `slot_save_path`, or None (None / "" is off).

    Absolute, no control characters, not a filesystem root or system folder (the
    `model_files_dir` rule, `settings._system_folders`), not inside the Strata
    install, not another alias's folder. Reads no disk beyond resolving links:
    a missing folder or parent passes, `prepare_folder` reports it at launch."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return f"slot_save_path: expected a folder path, not {value!r}"
    if not value.strip():
        return "slot_save_path: expected a folder path, not blank text"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return "slot_save_path: the path has a control character"
    if not os.path.isabs(value):
        return f"slot_save_path: expected an absolute path, not {value!r}"
    folder = Path(os.path.normpath(value))
    if folder.parent == folder:
        return f"slot_save_path: {folder} is a filesystem root"
    candidates = _path_keys(folder)
    for here in candidates:
        for system in _system_folders():
            if _inside(here, _path_key(system, resolve=False)):
                return f"slot_save_path: {folder} is a system folder ({system})"
        if install_dir and _inside(here, _path_key(install_dir)):
            return f"slot_save_path: {folder} is inside the Strata install ({install_dir})"
    for other in other_folders:
        if other and isinstance(other, str) and _path_keys(other) & candidates:
            return f"slot_save_path: {folder} is already another alias's KV snapshots folder"
    return None


def _whole_seconds(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    return isinstance(value, float) and math.isfinite(value) and value.is_integer() and value > 0


def alias_field_errors(cfg: Dict[str, Any]) -> List[str]:
    """The two alias fields: `kv_snapshot_ttl_s` whole seconds > 0, `kv_snapshot_max_gib` a number > 0.
    Absent or None is the default."""
    errors: List[str] = []
    ttl = cfg.get("kv_snapshot_ttl_s")
    if ttl is not None and not _whole_seconds(ttl):
        errors.append(f"kv_snapshot_ttl_s: expected whole seconds above 0, not {ttl!r}")
    cap = cfg.get("kv_snapshot_max_gib")
    if cap is not None and (
        isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap <= 0
    ):
        errors.append(f"kv_snapshot_max_gib: expected a number of GiB above 0, not {cap!r}")
    return errors


def provider_list_errors(providers: Sequence[Dict[str, Any]]) -> List[str]:
    """`folder_error` and `alias_field_errors` over a providers list, each line led by the alias;
    a folder already named by an earlier alias is refused on the later one (one index, one writer),
    compared as the folder lock compares (`_path_key`)."""
    errors: List[str] = []
    seen: Dict[str, str] = {}
    for cfg in providers or ():
        if not isinstance(cfg, dict):
            continue
        alias = cfg.get("alias", "?")
        folder = cfg.get("slot_save_path")
        problem = folder_error(folder, install_dir=cfg.get("strata_install_dir") or None)
        if problem:
            errors.append(f"{alias}: {problem}")
        elif isinstance(folder, str) and folder:
            keys = _path_keys(folder)
            earlier = next((seen[k] for k in keys if k in seen), None)
            if earlier is not None:
                errors.append(
                    f"{alias}: slot_save_path: {folder} is already the KV snapshots folder of alias {earlier!r}")
            else:
                for k in keys:
                    seen[k] = alias
        errors.extend(f"{alias}: {e}" for e in alias_field_errors(cfg))
    return errors


def ttl_s_of(cfg: Dict[str, Any]) -> int:
    """The alias's TTL in seconds, the default when absent."""
    value = cfg.get("kv_snapshot_ttl_s")
    return int(value) if _whole_seconds(value) else DEFAULT_TTL_S


def cap_bytes_of(cfg: Dict[str, Any]) -> int:
    """The alias's cap in bytes, the default when absent."""
    value = cfg.get("kv_snapshot_max_gib")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        value = DEFAULT_MAX_GIB
    return int(value * GIB)


def _looks_unmounted(parent: Path) -> bool:
    """Heuristic: an empty directory under a removable-disk root that is not a mount
    point is most likely an unmounted disk, where `mkdir` would land on the system
    disk. Linux and macOS only; on Windows a missing drive is a missing parent."""
    roots = _REMOVABLE_ROOTS.get("darwin" if sys.platform == "darwin" else
                                 "linux" if sys.platform.startswith("linux") else "", ())
    here = _path_key(parent)
    if not any(here != _path_key(r) and _inside(here, _path_key(r)) for r in roots):
        return False
    try:
        return not os.path.ismount(parent) and not any(parent.iterdir())
    except OSError:
        return False


def prepare_folder(path: PathLike) -> Optional[str]:
    """At launch: None when the folder exists or was made and takes a probe file;
    `FOLDER_MISSING` when its parent does not exist (an unplugged disk) or looks like
    an unmounted mount point, creating nothing; raises OSError when the parent exists
    but the folder cannot be made or written (read-only volume, no rights, a file in the way)."""
    folder = Path(path)
    if not folder.is_dir():
        # A parent that exists as anything (a file in the way) is a refusal, raised by mkdir.
        if not folder.parent.exists():
            return FOLDER_MISSING
        if folder.parent.is_dir() and _looks_unmounted(folder.parent):
            logger.warning(
                "KV snapshots folder %s: parent %s treated as an unmounted disk "
                "(heuristic: an empty folder under a removable-disk root)", folder, folder.parent,
            )
            return FOLDER_MISSING
        folder.mkdir(exist_ok=True)
    _probe_writable(folder)
    return None


# ---------------------------------------------------------------- names

def new_snapshot_names(folder: PathLike) -> Tuple[str, str]:
    """(staging, final) names for one save. Both are held as in progress, so no sweep
    in this process deletes them while the engine writes and the rename lands;
    `release_staging` lets both go after `add_entry` or a failure."""
    token = secrets.token_hex(16)
    staging = f"{FILE_PREFIX}{token}{STAGING_SUFFIX}"
    final = f"{FILE_PREFIX}{token}{FINAL_SUFFIX}"
    with _STATE_LOCK:
        _IN_PROGRESS.setdefault(_key(folder), set()).update((staging, final))
    return staging, final


def release_staging(folder: PathLike, staging: str) -> None:
    final = staging[: -len(STAGING_SUFFIX)] + FINAL_SUFFIX if staging.endswith(STAGING_SUFFIX) else None
    with _STATE_LOCK:
        held = _IN_PROGRESS.get(_key(folder), set())
        held.discard(staging)
        if final:
            held.discard(final)


def _dpc_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_FINAL_RE.match(name) or _STAGING_RE.match(name))


# ---------------------------------------------------------------- the index

def _empty_index() -> Dict[str, Any]:
    return {"version": INDEX_VERSION, "entries": [], "rates": {}, "sizes": {}}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_entry(entry: Any) -> bool:
    """An entry DPC may act on: a DPC file name and well-typed numbers. Anything else is skipped."""
    if not isinstance(entry, dict) or not _dpc_name(entry.get("file")):
        return False
    nbytes = entry.get("bytes")
    if nbytes is not None and not (isinstance(nbytes, int) and not isinstance(nbytes, bool) and nbytes >= 0):
        return False
    for key in ("expires_at", "saved_at"):
        if entry.get(key) is not None and not _number(entry[key]):
            return False
    pending = entry.get("delete_pending")
    k = entry.get("prefix_messages")
    return (pending is None or isinstance(pending, str)) and (
        k is None or (isinstance(k, int) and not isinstance(k, bool)))


def _move_aside(folder: Path, why: str) -> None:
    """A corrupt or foreign index is renamed, never overwritten; raises OSError when it cannot be."""
    stamp = int(time.time())
    target = folder / f"{INDEX_NAME}{_CORRUPT_SUFFIX}{stamp}"
    n = 1
    while target.exists():
        target = folder / f"{INDEX_NAME}{_CORRUPT_SUFFIX}{stamp}-{n}"
        n += 1
    os.replace(folder / INDEX_NAME, target)
    logger.warning("KV snapshots index in %s is %s; moved aside to %s", folder, why, target.name)


def _load(folder: Path, *, move_corrupt: bool = False) -> Tuple[Dict[str, Any], List[str], bool]:
    """(index, DPC names an index of another version lists, whether an index file is there).

    Unparseable, not an object, or no integer `version`: not DPC's index, none of its
    names is touched; a writer (`move_corrupt`) moves it aside first. Entries that are
    not DPC's or are ill-typed are skipped (logged once per folder). A file the OS will
    not let us read raises OSError: read as empty, the next write would drop it."""
    path = folder / INDEX_NAME
    for attempt in range(_REPLACE_TRIES):
        try:
            data = path.read_bytes()
            break
        except FileNotFoundError:
            return _empty_index(), [], False
        except PermissionError:
            if attempt == _REPLACE_TRIES - 1:
                raise
            time.sleep(_REPLACE_PAUSE_S)
    why = None
    try:
        raw = json.loads(data.decode("utf-8"))
    except ValueError:
        raw, why = None, "unparseable"
    if why is None and not isinstance(raw, dict):
        why = "not a JSON object"
    if why is None:
        version = raw.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            why = "without an integer version"
        elif not isinstance(raw.get("entries", []), list):
            why = "without an entry list"
    if why is not None:
        if move_corrupt:
            _move_aside(folder, why)
            return _empty_index(), [], False
        return _empty_index(), [], True
    entries = raw.get("entries", [])
    if raw["version"] != INDEX_VERSION:
        stale = [e["file"] for e in entries if isinstance(e, dict) and _dpc_name(e.get("file"))]
        return _empty_index(), stale, True
    index = _empty_index()
    index["entries"] = [dict(e) for e in entries if _valid_entry(e)]
    if len(index["entries"]) != len(entries):
        key = _key(folder)
        with _STATE_LOCK:
            first = key not in _WARNED_BAD
            _WARNED_BAD.add(key)
        if first:
            logger.warning("KV snapshots index in %s: %d entries not DPC's or ill-typed, ignored",
                           folder, len(entries) - len(index["entries"]))
    for block in ("rates", "sizes"):
        if isinstance(raw.get(block), dict):
            index[block] = raw[block]
    return index, [], True


def read_index(folder: PathLike) -> Dict[str, Any]:
    """The folder's index: {version, entries, rates, sizes}; empty when absent, corrupt or of another version."""
    folder = Path(folder)
    with _folder_lock(folder):
        return _load(folder)[0]


def write_index(folder: PathLike, index: Dict[str, Any]) -> None:
    """Written whole: a temporary file in the same folder, then `os.replace`, retried
    a few times on PermissionError (an antivirus or indexer holding the file on Windows).
    A corrupt index in its place is moved aside first, never overwritten."""
    folder = Path(folder)
    with _folder_lock(folder):
        _load(folder, move_corrupt=True)
        data = dict(index)
        data["version"] = INDEX_VERSION
        fd, tmp = tempfile.mkstemp(prefix=_INDEX_TMP_PREFIX, suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            for attempt in range(_REPLACE_TRIES):
                try:
                    os.replace(tmp, folder / INDEX_NAME)
                    return
                except PermissionError:
                    if attempt == _REPLACE_TRIES - 1:
                        raise
                    time.sleep(_REPLACE_PAUSE_S)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _unlink(folder: Path, name: str) -> bool:
    """True when the file is gone (or was never there); False when the OS refused.
    Only DPC's names are ever unlinked."""
    if not _dpc_name(name):
        return False
    try:
        (folder / name).unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        logger.info("KV snapshot %s in %s not deleted yet (%s); the next sweep tries again", name, folder, e)
        return False


def _remove(folder: Path, entry: Dict[str, Any], reason: str, out: List[Tuple[str, str]]) -> bool:
    """Delete an entry's file; keep the entry marked when the OS refuses. True when dropped."""
    if _unlink(folder, entry["file"]):
        logger.info("KV snapshot %s deleted from %s: %s", entry["file"], folder, reason)
        out.append((entry["file"], reason))
        return True
    entry["delete_pending"] = reason
    return False


def _sweep_locked(
    folder: Path,
    index: Dict[str, Any],
    stale: List[str],
    *,
    alias: Optional[str],
    now: float,
    expected: Optional[Dict[str, Any]],
    cap_bytes: Optional[int],
    keep: Optional[str],
) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for name in stale:
        if not _unlink(folder, name):
            index["entries"].append({"file": name, "delete_pending": VERSION})
        else:
            logger.info("KV snapshot %s deleted from %s: %s", name, folder, VERSION)
            out.append((name, VERSION))
    kept: List[Dict[str, Any]] = []
    for entry in index["entries"]:
        name = entry["file"]
        if entry.get("delete_pending"):
            if not _remove(folder, entry, entry["delete_pending"], out):
                kept.append(entry)
            continue
        if not (folder / name).exists():
            logger.info("KV snapshot %s named by the index in %s is missing; entry dropped", name, folder)
            out.append((name, MISSING))
            continue
        # `keep` shields a file from the cap only, never from expiry or a changed identity.
        reason = None
        expires = entry.get("expires_at")
        if not _number(expires) or expires <= now:
            reason = EXPIRED
        elif expected and (alias is None or entry.get("alias") == alias):
            # A key absent from `expected` (drafting auto) is not compared.
            for key, value in expected.items():
                if entry.get(key) != value:
                    reason = IDENTITY_REASONS.get(key, MODEL)
                    break
        if reason is None or not _remove(folder, entry, reason, out):
            kept.append(entry)
    index["entries"] = kept
    if cap_bytes is not None:
        total = sum(e.get("bytes") or 0 for e in kept)
        for entry in sorted(kept, key=lambda e: e.get("saved_at") or 0):
            if total <= cap_bytes:
                break
            if entry["file"] == keep or entry.get("delete_pending"):
                continue
            if _remove(folder, entry, OVER_CAP, out):
                kept.remove(entry)
                total -= entry.get("bytes") or 0
    with _STATE_LOCK:
        busy = set(_IN_PROGRESS.get(_key(folder), ()))
    indexed = {e["file"] for e in index["entries"]}
    try:
        listing = [p.name for p in folder.iterdir()]
    except OSError:
        listing = []
    for name in listing:
        if name in busy or name in indexed:
            continue
        if _STAGING_RE.match(name) or _INDEX_TMP_RE.match(name):
            reason = STAGING_LEFT
        elif _FINAL_RE.match(name):
            # Unindexed but DPC's by its prefix: deleted once older than any write.
            try:
                if now - (folder / name).stat().st_mtime <= KV_SNAPSHOT_WRITE_CEILING_S:
                    continue
            except OSError:
                continue
            reason = ORPHAN
        else:
            continue
        if _INDEX_TMP_RE.match(name):
            try:
                (folder / name).unlink()
                gone = True
            except OSError:
                gone = False
        else:
            gone = _unlink(folder, name)
        if gone:
            logger.info("KV snapshot file %s deleted from %s: %s", name, folder, reason)
            out.append((name, reason))
    return out


def sweep(
    folder: PathLike,
    *,
    alias: Optional[str],
    now: float,
    expected: Optional[Dict[str, Any]] = None,
    cap_bytes: Optional[int] = None,
    keep: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """Delete what the index names and may not keep, and DPC's leftovers.
    Returns (file, reason) for each file deleted or entry dropped.

    Reasons: `expired`; `version` (an index of another version, or the engine
    version), `model`, `drafter` (a key of `expected` that differs, compared for
    `alias`'s entries; a key absent is not compared); `over_cap` (oldest
    `saved_at` first, never `keep`); `staging_left`; `missing` (named, not on
    disk); `orphan` (a `dpc-kv-<hex>.bin` no index names, older than the write
    ceiling by mtime). A file the OS refuses to unlink stays marked for the next
    sweep. Never raises: a disk or index it cannot handle is logged and skipped."""
    folder = Path(folder)
    with _folder_lock(folder):
        try:
            index, stale, exists = _load(folder, move_corrupt=True)
            before = json.dumps(index["entries"], sort_keys=True)
            out = _sweep_locked(folder, index, stale, alias=alias, now=now, expected=expected,
                                cap_bytes=cap_bytes, keep=keep)
            changed = stale or json.dumps(index["entries"], sort_keys=True) != before
            if changed and folder.is_dir() and (exists or index["entries"]):
                write_index(folder, index)
            return out
        except (OSError, ValueError, TypeError, KeyError) as e:
            logger.warning("KV snapshots sweep of %s skipped: %s", folder, e)
            return []


def new_entry(
    *,
    file: str,
    alias: str,
    engine: str,
    identity: Dict[str, Any],
    record: Dict[str, Any],
    n_saved: Any,
    nbytes: int,
    save_ms: Any,
    now: float,
    ttl_s: float = DEFAULT_TTL_S,
) -> Dict[str, Any]:
    """An index entry from the save's answer, the expected identity and the last-call record."""
    entry = {
        "file": file,
        "alias": alias,
        "engine": engine,
        "agent_id": record.get("agent_id"),
        "conversation_id": record.get("conversation_id"),
        "caller_kind": record.get("caller_kind"),
        "n_saved": n_saved,
        "bytes": int(nbytes),
        "save_ms": save_ms,
        "saved_at": now,
        "expires_at": now + ttl_s,
        "prefix_sha256": record.get("prefix_sha256"),
        # The prefix's length in messages (a count), not the messages themselves.
        "prefix_messages": record.get("prefix_messages"),
    }
    entry.update(identity or {})
    return entry


def _same_key(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in ("alias", "agent_id", "conversation_id"))


def add_entry(folder: PathLike, entry: Dict[str, Any], *, cap_bytes: int, now: Optional[float] = None) -> bool:
    """Index a file just renamed to its final name. A file already expired at `now`, or
    that alone exceeds the cap, is deleted and not added (False). An older entry of the same key is replaced; then the
    folder is swept at `now` (default: the entry's `saved_at`, else the wall clock) with
    the cap applied, never to this file. Also records the folder's write rate and the
    alias's bytes per token. Raises OSError when the index cannot be read or written."""
    folder = Path(folder)
    name = entry.get("file")
    if not _FINAL_RE.match(name or "") or not _valid_entry(entry):
        raise ValueError(f"not a DPC KV snapshot entry: {name!r}")
    if now is None:
        now = entry["saved_at"] if _number(entry.get("saved_at")) else time.time()
    with _folder_lock(folder):
        index, stale, _ = _load(folder, move_corrupt=True)
        out: List[Tuple[str, str]] = []
        nbytes = entry.get("bytes") or 0
        expires = entry.get("expires_at")
        refusal = None
        if not _number(expires) or expires <= now:
            refusal = f"{EXPIRED} at {now}"
        elif nbytes > cap_bytes:
            refusal = f"{nbytes} bytes alone exceed the cap of {cap_bytes} bytes"
        if refusal:
            _unlink(folder, name)
            logger.info("KV snapshot %s deleted from %s, not kept: %s", name, folder, refusal)
            if stale:
                _sweep_locked(folder, index, stale, alias=None, now=now, expected=None,
                              cap_bytes=None, keep=None)
                write_index(folder, index)
            return False
        kept = []
        for old in index["entries"]:
            if old.get("file") != name and not old.get("delete_pending") and _same_key(old, entry):
                if not _remove(folder, old, REPLACED, out):
                    kept.append(old)
            else:
                kept.append(old)
        index["entries"] = [e for e in kept if e.get("file") != name] + [dict(entry)]
        _note_rates_locked(index, entry)
        _sweep_locked(folder, index, stale, alias=None, now=now, expected=None,
                      cap_bytes=cap_bytes, keep=name)
        write_index(folder, index)
        return True


def remove_entry(folder: PathLike, file: str, *, reason: str = REMOVED) -> bool:
    """Delete one indexed file (a prefix mismatch, a refused restore); a 404 restore passes
    `MISSING` and the entry is dropped. False when the OS refused and the entry stays marked."""
    folder = Path(folder)
    with _folder_lock(folder):
        index, stale, _ = _load(folder, move_corrupt=True)
        out: List[Tuple[str, str]] = []
        dropped = True
        kept = []
        for entry in index["entries"]:
            if entry.get("file") == file:
                dropped = _remove(folder, entry, reason, out)
                if not dropped:
                    kept.append(entry)
            else:
                kept.append(entry)
        index["entries"] = kept
        if stale:
            _sweep_locked(folder, index, stale, alias=None, now=float("-inf"), expected=None,
                          cap_bytes=None, keep=None)
        write_index(folder, index)
        return dropped


def lookup(
    folder: PathLike, *, alias: str, agent_id: Optional[str], conversation_id: Optional[str], now: float,
) -> Optional[Dict[str, Any]]:
    """The live entry of this conversation key on this alias, after a sweep; None otherwise.
    No conversation id matches nothing, so a call without one can never reach an entry. Never raises."""
    if not conversation_id:
        return None
    sweep(folder, alias=alias, now=now)
    try:
        entries = read_index(folder)["entries"]
    except (OSError, ValueError, TypeError, KeyError) as e:
        logger.warning("KV snapshots index of %s unreadable at lookup: %s", folder, e)
        return None
    for entry in entries:
        if entry.get("delete_pending"):
            continue
        if (entry.get("alias"), entry.get("agent_id"), entry.get("conversation_id")) == (
            alias, agent_id, conversation_id
        ):
            return dict(entry)
    return None


# ---------------------------------------------------------------- folders and the cleared mark

def register_folder(alias: str, folder: PathLike, *, cap_bytes: Optional[int] = None) -> None:
    """The alias's folder for this launch: where `forget_conversation` and `status_of` look."""
    with _STATE_LOCK:
        _FOLDERS[alias] = {"folder": Path(folder), "cap_bytes": cap_bytes}


def unregister_folder(alias: str) -> None:
    with _STATE_LOCK:
        _FOLDERS.pop(alias, None)


def folder_of(alias: str) -> Optional[Path]:
    with _STATE_LOCK:
        held = _FOLDERS.get(alias)
    return held["folder"] if held else None


def forget_conversation(conversation_id: Optional[str]) -> List[Tuple[str, str]]:
    """A cleared history (point 7): delete every entry of this conversation in every
    registered folder, whatever the alias or agent, and mark it so the next save of
    it is skipped until `note_engine_call` names it again. Never raises."""
    if not conversation_id:
        return []
    with _STATE_LOCK:
        _CLEARED.add(conversation_id)
        folders = {_key(h["folder"]): h["folder"] for h in _FOLDERS.values()}
    out: List[Tuple[str, str]] = []
    for folder in folders.values():
        try:
            with _folder_lock(folder):
                index, stale, exists = _load(folder, move_corrupt=True)
                if not exists:
                    continue
                kept = []
                for entry in index["entries"]:
                    if entry.get("conversation_id") == conversation_id and not entry.get("delete_pending"):
                        if not _remove(folder, entry, CLEARED, out):
                            kept.append(entry)
                    else:
                        kept.append(entry)
                if len(kept) != len(index["entries"]) or stale or any(
                    e.get("delete_pending") == CLEARED for e in kept
                ):
                    index["entries"] = kept
                    if stale:
                        _sweep_locked(folder, index, stale, alias=None, now=float("-inf"),
                                      expected=None, cap_bytes=None, keep=None)
                    write_index(folder, index)
        except Exception as e:  # an unplugged disk: the mark still holds, the prefix check does the rest
            logger.warning("KV snapshots of a cleared conversation not deleted in %s: %s", folder, e)
    return out


def is_cleared(conversation_id: Optional[str]) -> bool:
    with _STATE_LOCK:
        return bool(conversation_id) and conversation_id in _CLEARED


def note_clearable(conversation_id: Optional[str]) -> None:
    """A `ConversationMonitor` exists for this id, so a history clear of it reaches
    `forget_conversation`. Only such conversations are saved (team consensus, 2026-11-13).
    In memory: a restart re-creates monitors as conversations load."""
    if conversation_id:
        with _STATE_LOCK:
            _CLEARABLE.add(conversation_id)


def is_clearable(conversation_id: Optional[str]) -> bool:
    with _STATE_LOCK:
        return bool(conversation_id) and conversation_id in _CLEARABLE


# ---------------------------------------------------------------- the prefix

def prefix_digest(messages: Sequence[Any], k: Optional[int] = None) -> Tuple[str, int]:
    """(sha256 hex, k) of the first k wire messages (all when k is None), over canonical JSON."""
    messages = list(messages or [])
    k = len(messages) if k is None else max(0, min(int(k), len(messages)))
    text = json.dumps(messages[:k], sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), k


def matches(entry: Dict[str, Any], messages: Sequence[Any]) -> bool:
    """Whether the request's first `prefix_messages` hash to the saved value."""
    k = entry.get("prefix_messages")
    saved = entry.get("prefix_sha256")
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0 or not saved:
        return False
    messages = list(messages or [])
    if len(messages) < k:
        return False
    return prefix_digest(messages, k)[0] == saved


# ---------------------------------------------------------------- arithmetic

def estimate_bytes(tokens: Any, floor: float, per_token: float) -> int:
    """A file's size: the floor plus the part per token."""
    return int(round(float(floor) + float(per_token) * max(0, int(tokens or 0))))


def save_bound(
    now: float, stop_bound: Optional[float], expected_stop_s: float,
    ceiling: float = KV_SNAPSHOT_WRITE_CEILING_S,
) -> float:
    """Seconds a save may take: the ceiling, or less when the waiting request's stop bound,
    less the stop still to come, leaves less. Never below 0; no stop bound, the ceiling."""
    if stop_bound is None:
        return max(0.0, float(ceiling))
    return max(0.0, min(float(ceiling), float(stop_bound) - float(now) - float(expected_stop_s)))


def _rate(rates: Optional[Dict[str, Any]], key: str) -> Optional[float]:
    value = (rates or {}).get(key)
    return float(value) if _number(value) and value > 0 else None


def estimate_save_s(nbytes: int, folder_rates: Optional[Dict[str, Any]]) -> Optional[float]:
    """Seconds to write `nbytes` at the folder's last measured rate; None while unmeasured."""
    rate = _rate(folder_rates, "write_bytes_per_s")
    return None if rate is None else nbytes / rate


def worth_saving(
    tokens: Any,
    folder_rates: Optional[Dict[str, Any]],
    alias_sizes: Dict[str, Any],
    cold_rate: Optional[float],
    *,
    at_stop: bool = True,
) -> Tuple[bool, str]:
    """Whether estimated save plus restore beats the cold read at this length (point 4).

    Tokens unknown: skip, "size unknown". No write rate yet: tried once, which
    measures it. No cold rows: "threshold unknown", tried. No read rate yet: the
    write rate stands in. `at_stop` False (slice 3's idle save) counts the restore only."""
    if tokens is None or isinstance(tokens, bool) or int(tokens) <= 0:
        return False, "size unknown"
    tokens = int(tokens)
    nbytes = estimate_bytes(tokens, alias_sizes["floor_bytes"], alias_sizes["bytes_per_token"])
    write = _rate(folder_rates, "write_bytes_per_s")
    if write is None:
        return True, "write rate unknown, tried once"
    if not cold_rate or cold_rate <= 0:
        return True, "threshold unknown"
    read = _rate(folder_rates, "read_bytes_per_s") or write
    cost = nbytes / read + (nbytes / write if at_stop else 0.0)
    cold = tokens / float(cold_rate)
    words = f"{'save and restore' if at_stop else 'restore'} ~{cost:.1f} s against a cold read ~{cold:.1f} s"
    if cost < cold:
        return True, words
    return False, f"below the size threshold: {words}"


def cold_rate_at(rows: Iterable[Dict[str, Any]], alias: str, n_tokens: int) -> Optional[float]:
    """The alias's cold prefill rate (tok/s) at this length; see `cold_rate_with_rows`."""
    return cold_rate_with_rows(rows, alias, n_tokens)[0]


def cold_rate_with_rows(rows: Iterable[Dict[str, Any]], alias: str, n_tokens: int) -> Tuple[Optional[float], int]:
    """(rate, rows it rests on): the alias's cold prefill rate (tok/s) at this length, from
    usage ledger rows with `cached_tokens` 0, prompt at least 5K and `counts_source` engine,
    the median of the nearest length bucket that has rows, and how many rows that bucket
    holds, so a log line can show how thin the evidence is. (None, 0) when there is none."""
    buckets: Dict[int, List[float]] = {}
    for row in rows or ():
        try:
            if row.get("alias") != alias or row.get("counts_source") != "engine":
                continue
            if row.get("cached_tokens") != 0:
                continue
            prompt = int(row.get("prompt_tokens") or 0)
            ms = float(row.get("prompt_ms") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if prompt < COLD_MIN_PROMPT or ms <= 0:
            continue
        buckets.setdefault(_bucket(prompt), []).append(prompt / (ms / 1000.0))
    if not buckets:
        return None, 0
    want = _bucket(max(int(n_tokens or 0), 0))
    nearest = min(buckets, key=lambda b: (abs(b - want), b))
    return float(median(buckets[nearest])), len(buckets[nearest])


def cold_rate_detail(rows: Iterable[Dict[str, Any]], alias: str, n_tokens: int) -> Dict[str, Any]:
    """`cold_rate_with_rows` plus where the rate came from: {rate, rows, bucket, extrapolated}.
    `bucket` is the ledger length bucket the rate rests on (`bucket_label`), None when there
    are no rows; `extrapolated` is True when the conversation's own bucket has no rows and a
    neighbour's rate stands in (a 256K conversation with none measured at 256K)."""
    rows = list(rows or ())
    rate, count = cold_rate_with_rows(rows, alias, n_tokens)
    if rate is None:
        return {"rate": None, "rows": 0, "bucket": None, "extrapolated": False}
    want = _bucket(max(int(n_tokens or 0), 0))
    have = {b for b in (_cold_row_bucket(r, alias) for r in rows) if b is not None}
    # The nearest bucket with rows, by the rule `cold_rate_with_rows` applies.
    used = min(have, key=lambda b: (abs(b - want), b))
    return {"rate": rate, "rows": count, "bucket": bucket_label(used), "extrapolated": used != want}


def _cold_row_bucket(row: Dict[str, Any], alias: str) -> Optional[int]:
    """The length bucket of a usable cold row of this alias, else None (the rate's own filter)."""
    try:
        if row.get("alias") != alias or row.get("counts_source") != "engine":
            return None
        if row.get("cached_tokens") != 0:
            return None
        prompt = int(row.get("prompt_tokens") or 0)
        ms = float(row.get("prompt_ms") or 0)
    except (TypeError, ValueError, AttributeError):
        return None
    if prompt < COLD_MIN_PROMPT or ms <= 0:
        return None
    return _bucket(prompt)


def bucket_label(bucket: int) -> str:
    """A ledger length bucket in words: `5K-16K`, ..., `256K+`."""
    edges = COLD_BUCKET_EDGES
    if bucket <= 0:
        return f"<{edges[0] // 1000}K"
    if bucket >= len(edges):
        return f"{edges[-1] // 1000}K+"
    return f"{edges[bucket - 1] // 1000}K-{edges[bucket] // 1000}K"


def _bucket(tokens: int) -> int:
    return sum(1 for edge in COLD_BUCKET_EDGES if tokens >= edge)


def disk_short(folder: PathLike, nbytes: int) -> bool:
    """Whether the disk lacks `nbytes` plus the free-space floor; an unreadable disk is short."""
    try:
        free = shutil.disk_usage(str(folder)).free
    except OSError:
        return True
    return free < int(nbytes) + FREE_SPACE_FLOOR_BYTES


def seed_sizes(engine: str) -> Dict[str, float]:
    return dict(SEED_SIZES.get(engine, SEED_SIZES["llamacpp_server"]))


def alias_sizes(folder: PathLike, alias: str, engine: str) -> Dict[str, float]:
    """The alias's floor and bytes per token: measured in the index, else the engine's seed."""
    try:
        measured = read_index(folder)["sizes"].get(alias)
    except (OSError, ValueError, TypeError, KeyError):
        measured = None
    sizes = seed_sizes(engine)
    if isinstance(measured, dict):
        for key in ("floor_bytes", "bytes_per_token"):
            if _number(measured.get(key)) and measured[key] > 0:
                sizes[key] = float(measured[key])
    return sizes


def folder_rates(folder: PathLike) -> Dict[str, float]:
    """The folder's last measured write and read rates (bytes/s); keys absent while unmeasured."""
    try:
        rates = read_index(folder)["rates"]
    except (OSError, ValueError, TypeError, KeyError):
        return {}
    return {k: float(v) for k, v in rates.items() if _number(v) and v > 0}


def _rate_sample(nbytes: Any, ms: Any) -> bool:
    """Whether one write or restore is big and slow enough to stand for the disk's rate."""
    return (_number(nbytes) and nbytes >= RATE_SAMPLE_MIN_BYTES
            and _number(ms) and ms >= RATE_SAMPLE_MIN_MS)


def _note_rates_locked(index: Dict[str, Any], entry: Dict[str, Any]) -> None:
    nbytes = entry.get("bytes") or 0
    ms = entry.get("save_ms")
    if not _rate_sample(nbytes, ms):
        return
    index["rates"]["write_bytes_per_s"] = nbytes / (ms / 1000.0)
    alias, tokens = entry.get("alias"), entry.get("n_saved")
    if alias and isinstance(tokens, int) and tokens > 0:
        held = index["sizes"].get(alias)
        floor = held.get("floor_bytes") if isinstance(held, dict) else None
        if not _number(floor):
            floor = seed_sizes(entry.get("engine") or "")["floor_bytes"]
        # One measurement fits one unknown: the floor stays, the part per token follows the file.
        if nbytes > floor:
            index["sizes"][alias] = {"floor_bytes": float(floor), "bytes_per_token": (nbytes - floor) / tokens}


def note_restore_rate(folder: PathLike, nbytes: int, ms: float) -> None:
    """Record the folder's read rate from one restore (a sample too small to measure is ignored)."""
    if not _rate_sample(nbytes, ms):
        return
    folder = Path(folder)
    with _folder_lock(folder):
        try:
            index, stale, exists = _load(folder)
            if not exists or stale:
                return
            index["rates"]["read_bytes_per_s"] = nbytes / (ms / 1000.0)
            write_index(folder, index)
        except OSError as e:  # a rate is advisory
            logger.info("KV snapshots read rate of %s not recorded: %s", folder, e)


# ---------------------------------------------------------------- status classes

def _parse(body: Any) -> Tuple[str, Optional[str]]:
    """(text, error.kind) of a response body given as text, a dict or None."""
    if body is None:
        return "", None
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    parsed = body
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except ValueError:
            return body, None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        kind = error.get("kind") if isinstance(error, dict) else None
        return json.dumps(parsed, ensure_ascii=False), kind if isinstance(kind, str) else None
    return str(body), None


def classify_save(code: Optional[int], body_text: Any = None, *, engine: str = "strata") -> str:
    """A save's outcome kind from the HTTP status (None: no answer) and the body.

    200 `saved`. Strata's 500 naming the engine's restart `engine_died`, never a
    refusal. Any other 500 on Strata `engine_refuses` (suspends saving for the
    launch); on llama-server ("Unable to save slot": a file, disk or rights
    problem) `failed`. 415, 403, 501, 400 `transport`, saving stays on. 503
    without a kind `not_loaded`. No answer, 507 storage, 503 memory: `failed`."""
    text, kind = _parse(body_text)
    if code == 200:
        return SAVED
    if code is None:
        return FAILED
    if _DEATH_MARKER in text:
        return ENGINE_DIED
    if code == 500:
        return FAILED if engine == "llamacpp_server" else ENGINE_REFUSES
    if code in TRANSPORT_REASONS:
        return TRANSPORT
    if code == 503 and kind is None:
        return NOT_LOADED
    return FAILED


def classify_restore(code: Optional[int], body: Any = None) -> str:
    """A restore's outcome kind (point 5): what to do with the file follows from it.

    200 `restored`. 404 `missing`: the file is gone, the entry is dropped.
    Strata's `invalid` refusal ("saved with another model") `refused`: deleted.
    llama-server's 400 "No available space in KV cache or invalid slot save file"
    (one text for both causes on b12110) or Strata's `memory` kind `no_room`:
    kept. 503 without a kind `not_loaded`: kept. 415, 403, 501 and any other 400
    `transport`: kept. Strata's restart text `engine_died`; no answer and anything
    else `failed`; both keep the file."""
    text, kind = _parse(body)
    low = text.lower()
    if code == 200:
        return RESTORED
    if code is None:
        return FAILED
    if _DEATH_MARKER in text:
        return ENGINE_DIED
    if code == 404:
        return MISSING
    if kind == "invalid" or "another model" in low:
        return REFUSED
    if kind == "memory" or "no available space" in low:
        return NO_ROOM
    if code == 503 and kind is None:
        return NOT_LOADED
    if code in TRANSPORT_REASONS:
        return TRANSPORT
    return FAILED


def describe_outcome(kind: str, code: Optional[int] = None, body: Any = None, *,
                     reason: Optional[str] = None) -> str:
    """The words a log line and the Local engines block use for an outcome;
    `reason` is the caller's own words for a `SKIPPED` save."""
    if kind == SKIPPED:
        return f"not saved: {reason}" if reason else "not saved"
    if kind == TRANSPORT and code == 400 and _parse(body)[1] == "invalid":
        return "the engine refused the request's content (400)"
    if kind == TRANSPORT and code in TRANSPORT_REASONS:
        return f"refused by the request ({code}): {TRANSPORT_REASONS[code]}"
    if kind == FAILED and code == 507:
        # The engine's own disk reserve (Strata `storage`), not DPC's `disk_short` before the call.
        return "the engine refused: not enough disk for the file plus its reserve (507)"
    with_code = f" ({code})" if code is not None else ""
    return {
        SAVED: "saved",
        RESTORED: "restored",
        ENGINE_REFUSES: f"the engine refused the save{with_code}",
        ENGINE_DIED: "the engine ended during the call",
        NOT_LOADED: "the model was not loaded",
        REFUSED: "the engine refused the snapshot; deleted",
        NO_ROOM: "no room in the engine; kept",
        MISSING: "the file was missing",
        FAILED: f"failed{with_code}" if code is not None else "failed (no answer)",
    }.get(kind, kind)


# ---------------------------------------------------------------- the last-call record

def note_engine_call(
    alias: str,
    *,
    caller_kind: Optional[str],
    agent_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    peer_id: Optional[str] = None,
    prefix: Optional[Tuple[str, int]] = None,
    at: float,
) -> int:
    """Note a call about to reach the engine (point 2) and return its sequence number.
    The call is pending: it becomes the alias's record only at `mark_sent`, so a call
    delayed by an await, cancelled or refused never stands for what the slot holds.
    `at` is wall time for display only. A `caller_kind` outside `SNAPSHOT_CALLER_KINDS` is logged
    once and noted as None with no conversation, so a typo never becomes an owner."""
    sha, k = prefix if prefix else (None, None)
    with _STATE_LOCK:
        if caller_kind is not None and caller_kind not in SNAPSHOT_CALLER_KINDS:
            if caller_kind not in _WARNED_KINDS:
                _WARNED_KINDS.add(caller_kind)
                logger.warning("KV snapshots: unknown caller kind %r on %s recorded as none (no conversation)",
                               caller_kind, alias)
            caller_kind, agent_id, conversation_id = None, None, None
        seq = next(_SEQ)
        pending = _PENDING.setdefault(alias, OrderedDict())
        pending[seq] = {
            "alias": alias,
            "caller_kind": caller_kind,
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "peer_id": peer_id,
            "prefix_sha256": sha,
            "prefix_messages": k,
            "seq": seq,
            "sent": None,
            "at": at,
            "prompt_tokens": None,
            "reply_tokens": None,
        }
        dropped = 0
        while len(pending) > PENDING_PER_ALIAS:
            pending.popitem(last=False)
            dropped += 1
        # Once per launch per alias (the `first_this_launch` set, read here under the same lock).
        warned = _outcomes(alias)["warned"] if dropped else None
        if dropped and PENDING_DROPPED not in warned:
            warned.add(PENDING_DROPPED)
            logger.warning("KV snapshots: %s dropped a pending call never marked sent (more than %d "
                           "waiting); further drops this launch are not logged", alias, PENDING_PER_ALIAS)
        return seq


def mark_sent(alias: str, seq: int) -> bool:
    """The noted call goes on the wire now (the last synchronous step in `call_slot`): it
    becomes the alias's record, ordered by a counter taken here, and its conversation loses
    its cleared mark. False for a sequence number not pending (unknown, dropped, already sent)."""
    with _STATE_LOCK:
        record = _PENDING.get(alias, {}).pop(seq, None)
        if record is None:
            return False
        record["sent"] = next(_SENT)
        # When it went on the wire, so a supervisor can tell an engine (re)loaded after it.
        record["sent_mono"] = time.monotonic()
        _RECORDS[alias] = record
        if record["conversation_id"]:
            _CLEARED.discard(record["conversation_id"])
        return True


def note_engine_tokens(alias: str, call_seq: int, prompt: Any, reply: Any) -> bool:
    """The call's token counts, applied only while that call (by its sequence number) is the sent record."""
    with _STATE_LOCK:
        current = _RECORDS.get(alias)
        if current is None or current["seq"] != call_seq:
            return False
        current["prompt_tokens"] = prompt
        current["reply_tokens"] = reply
        return True


def engine_call_of(alias: str) -> Optional[Dict[str, Any]]:
    """The latest call marked sent on this alias; a pending call never shows here."""
    with _STATE_LOCK:
        current = _RECORDS.get(alias)
        return dict(current) if current else None


def is_owner_kind(caller_kind: Optional[str]) -> bool:
    """Slice 1 saves an agent's turn or an owner's chat only."""
    return caller_kind in OWNER_KINDS


# ---------------------------------------------------------------- per-launch outcomes

def _outcomes(alias: str) -> Dict[str, Any]:
    return _OUTCOMES.setdefault(
        alias, {"off_reason": None, "warned": set(), "last_save": None, "last_restore": None,
                "storage_streak": 0})


def launch_began(alias: str) -> None:
    """A new launch: suspension and once-per-launch warnings clear; the last outcomes stay."""
    with _STATE_LOCK:
        state = _outcomes(alias)
        state["off_reason"] = None
        state["warned"] = set()
        state["storage_streak"] = 0


def suspend(alias: str, reason: str) -> None:
    """Saving off for the rest of this launch (`FOLDER_MISSING`, `ENGINE_REFUSES_SAVES`)."""
    with _STATE_LOCK:
        _outcomes(alias)["off_reason"] = reason


def note_storage_refusal(alias: str) -> int:
    """A save answered 507 (storage): the alias's streak of them this launch, after this one."""
    with _STATE_LOCK:
        state = _outcomes(alias)
        state["storage_streak"] = state.get("storage_streak", 0) + 1
        return state["storage_streak"]


def reset_storage_streak(alias: str) -> None:
    """Any save outcome other than 507 ends the streak."""
    with _STATE_LOCK:
        _outcomes(alias)["storage_streak"] = 0


def off_reason(alias: str) -> Optional[str]:
    with _STATE_LOCK:
        state = _OUTCOMES.get(alias)
        return state["off_reason"] if state else None


def first_this_launch(alias: str, what: str) -> bool:
    """True the first time `what` is asked this launch: a WARNING once per launch per reason."""
    with _STATE_LOCK:
        warned = _outcomes(alias)["warned"]
        if what in warned:
            return False
        warned.add(what)
        return True


def note_save(alias: str, outcome: Optional[Dict[str, Any]]) -> None:
    with _STATE_LOCK:
        _outcomes(alias)["last_save"] = dict(outcome) if outcome is not None else None


def note_restore(alias: str, outcome: Optional[Dict[str, Any]]) -> None:
    with _STATE_LOCK:
        _outcomes(alias)["last_restore"] = dict(outcome) if outcome is not None else None


def status_of(alias: str, now: Optional[float] = None) -> Dict[str, Any]:
    """The Local engines block's row. `files`, `bytes` and `oldest_expiry` count only this
    alias's unexpired indexed entries (not marked for deletion; unindexed files never);
    then the cap, the last save and restore outcomes, and the off reason. Never raises."""
    now = time.time() if now is None else now
    with _STATE_LOCK:
        held = dict(_FOLDERS.get(alias) or {})
        state = _OUTCOMES.get(alias) or {}
        last_save, last_restore = state.get("last_save"), state.get("last_restore")
        reason = state.get("off_reason")
    files, nbytes, oldest = 0, 0, None
    folder = held.get("folder")
    if folder is not None:
        try:
            entries = [
                e for e in read_index(folder)["entries"]
                if not e.get("delete_pending") and e.get("alias") == alias
                and _number(e.get("expires_at")) and e["expires_at"] > now
            ]
        except (OSError, ValueError, TypeError, KeyError):
            entries = []
        files = len(entries)
        nbytes = sum(e.get("bytes") or 0 for e in entries)
        oldest = min((e["expires_at"] for e in entries), default=None)
    return {
        "folder": str(folder) if folder is not None else None,
        "files": files,
        "bytes": nbytes,
        "cap_bytes": held.get("cap_bytes"),
        "oldest_expiry": oldest,
        "last_save": dict(last_save) if last_save else None,
        "last_restore": dict(last_restore) if last_restore else None,
        "off_reason": reason,
    }
