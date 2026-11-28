"""The refusals this node has made, kept where only this node reads them.

A group record refused on arrival — its signature is wrong, or the
`reject_unsigned` setting refuses a record with no signature this node can
check — is dropped: it never enters `message_history`, is never exported and
never reaches an agent. Before this store the drop left one log line, and
nobody could later ask what came, from where, when and why. Now every such
refusal is also kept here as one row, and the open chat is told (`UI_EVENT`),
so the UI can show it as a red row in the conversation at the record's own
time.

The live file is `<dpc home>/state/rejected_records.json`, NOT beside the
conversations' `history.json`: an agent sandbox can read the conversation
folders, and none can read `state/`.

Nothing is deleted. When the live file holds more than `PER_NODE_CAP` rows
from one delivering node, or more than `TOTAL_CAP` rows in all, the rows with
the oldest `last_seen` move to `rejected_records.archive.jsonl` beside it —
created 0600 like the live file, for it holds the same refused texts —
appended FIRST, and only then is the live file rewritten, so a crash between
the two loses a duplicate at worst. The archive is never read, trimmed or
deleted at runtime. A row that went to the archive and arrives again later
starts a fresh live row; the archive keeps its earlier history.

A repeat of the same refusal — the same conversation, record id, reason,
delivering node and both content hashes (`_identity_of`) — is counted on the
row it already has, never appended again.

One reason is not an accusation. `conflict` names an arriving record whose
`id` this node already holds with a different content — its text, or its
`content_hash` when both copies carry one, for a re-signed record is a new
record and not an edit (ADR-042, open question 1). The held copy is kept
exactly as it is; the row's text is the arriving copy's, the one nobody
stored, so the two can be told apart on the screen. This node can see that
the copies differ and cannot know which side moved, so the word blames
nobody — it says the id now names two things.

`arrived` says which of the three paths kept the row: `"live"` (a GROUP_TEXT
this node checked itself), `"sync"` (a record a history merge refused) or
`"repair"` (the repair that folds a split conversation folder found it on
this disk; no node handed it over, and the row names none).

This module imports nothing from `dpc_client_core` itself: both
`conversation_monitor` and the group handler import it, and a cycle would
break every import.
"""

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dpc_protocol.paths import dpc_home, resolve_module_home

logger = logging.getLogger(__name__)

REASONS = ("signer_mismatch", "content_hash_mismatch", "signature_check_failed",
           "invalid_signature", "unsigned", "unknown_preimage", "conflict")
PER_NODE_CAP = 100
TOTAL_CAP = 1000
TEXT_LIMIT = 2000        # characters of the refused record's text that are kept
SIGNATURE_LIMIT = 1024   # the signature as it arrived, cut
FIELD_LIMIT = 512        # every other string field from the record, cut
FILE_NAME = "rejected_records.json"
ARCHIVE_NAME = "rejected_records.archive.jsonl"
UI_EVENT = "group_record_rejected"

_INITIAL_DPC_HOME = dpc_home()
DPC_HOME_DIR = _INITIAL_DPC_HOME

# How often the replace is tried when the platform refuses it (Windows: a
# reader holds the file open). The card lease's writer taught the recipe; this
# is a copy of the retry, not an import, so the store pulls no other module.
_REPLACE_TRIES = 5
# The live file is this node's alone (POSIX mode bits; ignored on Windows).
_LIVE_FILE_MODE = 0o600


def _dpc_home() -> Path:
    return resolve_module_home(DPC_HOME_DIR, _INITIAL_DPC_HOME)


def default_path() -> Path:
    """`<dpc home>/state/rejected_records.json`, the home honoured as elsewhere."""
    return _dpc_home() / "state" / FILE_NAME


def _fsync_directory(folder: Path) -> None:
    """fsync `folder` after a rename into it, on POSIX (Windows cannot open a
    directory for it). Best effort: a failure is logged, never raised."""
    if os.name == "nt":
        return
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError as exc:
        logger.debug("rejected: the folder %s could not be opened to fsync: %s", folder, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("rejected: the folder %s could not be fsynced: %s", folder, exc)
    finally:
        os.close(fd)


def _write_live_file(path: Path, records: List[Dict[str, Any]]) -> None:
    """Replace the live file in one step, so a reader sees the old rows or the
    new ones, never half of either.

    A temp file in the same folder, then `os.replace`, retried a few times when
    Windows refuses because a reader holds the file. Raises whatever the
    platform raised after the last try — the caller logs it, and any rows
    already appended to the archive stay there. The same recipe as the card
    lease's writer (`card_lease_state.write_atomically`), copied rather than
    imported: the store must stand on its own.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"version": 1, "records": records}, ensure_ascii=False, indent=2)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
                 _LIVE_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                _fsync_directory(path.parent)
                return
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


#: The sanitizer's table: every surrogate code point to one U+FFFD each.
_SURROGATES_TO_FFFD = {code: "\ufffd" for code in range(0xD800, 0xE000)}


def _clean(text: str) -> str:
    """Each surrogate replaced with U+FFFD, everything else untouched.

    On the wire a lone surrogate is the JSON escape `\\ud800`, and
    `json.loads` turns it into a lone surrogate inside the Python str — which
    utf-8 cannot encode, so one escape in a refused record's text used to make
    the live-file write raise and the red row vanish: a forger dodged the store
    with it. A Python str holds an astral character (an emoji, \\U0001F600) as
    one code point of its own, so anything outside U+D800..U+DFFF is real text
    and survives byte for byte; any surrogate in a str is unpaired by
    construction, and each becomes one U+FFFD."""
    return text.translate(_SURROGATES_TO_FFFD)


def _cut(value: Any, limit: int) -> Optional[str]:
    """A field off a refused record as a bounded string: None stays None,
    anything else becomes its `str()`, surrogates sanitised (`_clean`), cut at
    `limit`. The id fields are kept exactly as they arrived up to the cut,
    never re-formatted."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return _clean(text)[:limit]


def _identity_of(conversation_id: str, record_id: Optional[str], reason: str,
                 transport_node_id: Optional[str], claimed_content_hash: Optional[str],
                 computed_content_hash: Optional[str]) -> list:
    """What makes two refusals the same one: the room, the record's id, the
    verdict, the node that handed it over and the two hashes (stated and
    recomputed). Everything else about a repeat only moves its counters."""
    return [conversation_id, record_id or "", reason, transport_node_id or "",
            claimed_content_hash or "", computed_content_hash or ""]


def _key_of(identity: list) -> str:
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]


class RejectedRecordStore:
    """One node's refused records: a live file of bounded rows and an archive
    beside it that only grows. Nothing is created on disk until the first
    record is kept."""

    def __init__(self, file_path, *, per_node_cap: int = PER_NODE_CAP,
                 total_cap: int = TOTAL_CAP, text_limit: int = TEXT_LIMIT):
        self.file_path = Path(file_path)
        self.per_node_cap = per_node_cap
        self.total_cap = total_cap
        self.text_limit = text_limit
        # One lock around every read-modify-write, the archive append included.
        self._lock = threading.RLock()
        self._records: Optional[List[Dict[str, Any]]] = None  # loaded on first use

    # -- reading --

    def _load(self) -> List[Dict[str, Any]]:
        """The live rows, oldest `last_seen` first; a missing file is empty.

        A file that cannot be read or parsed is moved aside rather than
        overwritten: it holds refusals this node made, and losing them to a
        torn write is exactly the silence the store exists to end.
        """
        if self._records is not None:
            return self._records
        records: List[Dict[str, Any]] = []
        try:
            try:
                raw = self.file_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return records  # nothing kept yet: empty, and nothing to move aside
            data = json.loads(raw) if raw.strip() else {}
            rows = data.get("records") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError("the live file does not hold {version, records}")
            records = [row for row in rows if isinstance(row, dict) and row.get("key")]
        except (OSError, ValueError) as exc:
            self._move_unreadable_aside(exc)
            records = []
        records.sort(key=lambda row: str(row.get("last_seen", "")))
        self._records = records
        return records

    def _move_unreadable_aside(self, exc: Exception) -> None:
        """Rename the unreadable live file in place, named with the moment, and
        say so: the rows are not lost, only out of the live list. A rename that
        itself fails is raised — overwriting the file silently is the one thing
        forbidden here."""
        logger.warning("The rejected-records file %s could not be read (%s); "
                       "it is moved aside and the store starts empty", self.file_path, exc)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        os.replace(self.file_path,
                   self.file_path.with_name(f"rejected_records.unreadable-{stamp}.json"))

    def for_conversation(self, conversation_id: str) -> List[Dict[str, Any]]:
        """The live rows of one conversation, as copies. The archive is never
        read at runtime; what moved there is history, not the red rows."""
        with self._lock:
            return [dict(row) for row in self._load()
                    if row.get("conversation_id") == conversation_id]

    # -- writing --

    def _archive_path(self) -> Path:
        return self.file_path.with_name(ARCHIVE_NAME)

    def _append_archive(self, row: Dict[str, Any]) -> None:
        """One JSON line, appended; the file is created 0600 like the live file,
        because it holds the same refused texts (a plain `open` would take the
        umask's mode)."""
        path = self._archive_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_BINARY where it exists: without it the Windows CRT turns the
        # newline into CRLF on the way out (`node_ledger.NodeLedger.append`
        # measured it; the live file's writer does the same).
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, _LIVE_FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _row_past_the_caps(self, records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The row the caps move next, or None. The per-node cap first: while
        one delivering node holds more than its share, that node's oldest row
        goes. Then the total cap: the oldest row overall, across nodes. `None`
        as a transport id is a bucket of its own."""
        counts: Dict[Any, int] = {}
        for row in records:
            bucket = row.get("transport_node_id")
            counts[bucket] = counts.get(bucket, 0) + 1
        for bucket, held in counts.items():
            if held > self.per_node_cap:
                # The list is kept sorted by `last_seen`, so the first row of
                # the bucket is that node's oldest.
                return next(row for row in records if row.get("transport_node_id") == bucket)
        if len(records) > self.total_cap:
            return records[0]
        return None

    def record(
        self,
        *,
        conversation_id: str,
        reason: str,
        arrived: str,
        transport_node_id: Optional[str] = None,
        record_id: Optional[str] = None,
        claimed_content_hash: Optional[str] = None,
        computed_content_hash: Optional[str] = None,
        record_timestamp: Optional[str] = None,
        claimed_sender_node_id: Optional[str] = None,
        claimed_sender_name: Optional[str] = None,
        claimed_sender_type: Optional[str] = None,
        claimed_agent_owner: Optional[str] = None,
        signer_node_id: Optional[str] = None,
        signature: Optional[str] = None,
        preimage_version: Optional[str] = None,
        text: Optional[str] = None,
        now: Optional[Any] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        """Keep one refusal, or count it on the row it already made.

        Returns `(entry, is_new)`. `arrived` is `"live"` (a GROUP_TEXT this
        node checked itself), `"sync"` (a record a history merge refused) or
        `"repair"` (the repair that folds a split conversation folder found
        the record on this disk). The reason `conflict` is not an accusation:
        the record's `id` was already held with a different content, the held
        copy is kept exactly as it is, and the row's `text` is the arriving
        copy's — the one nobody stored. `now` is an injectable clock returning
        an aware UTC datetime. A `reason` outside `REASONS` or an `arrived`
        outside the three words raises `ValueError` before anything is
        written.
        """
        if reason not in REASONS:
            raise ValueError(f"reason={reason!r} is not one of {REASONS}")
        if arrived not in ("live", "sync", "repair"):
            raise ValueError(f"arrived={arrived!r} is neither 'live', 'sync' nor 'repair'")
        moment = now() if now is not None else datetime.now(timezone.utc)
        seen = moment.astimezone(timezone.utc).isoformat()
        raw_text = "" if text is None else _clean(
            text if isinstance(text, str) else str(text))
        kept_text = raw_text[:self.text_limit]

        room = _cut(conversation_id, FIELD_LIMIT)
        entry_id = _cut(record_id, FIELD_LIMIT)
        identity = _identity_of(room, entry_id, reason, _cut(transport_node_id, FIELD_LIMIT),
                                _cut(claimed_content_hash, FIELD_LIMIT),
                                _cut(computed_content_hash, FIELD_LIMIT))
        key = _key_of(identity)

        with self._lock:
            records = self._load()
            entry = next((row for row in records if row.get("key") == key), None)
            if entry is None:
                entry = {
                    "key": key,
                    "conversation_id": room,
                    "record_id": entry_id,
                    "reason": reason,
                    "transport_node_id": _cut(transport_node_id, FIELD_LIMIT),
                    "claimed_sender_node_id": _cut(claimed_sender_node_id, FIELD_LIMIT),
                    "claimed_sender_name": _cut(claimed_sender_name, FIELD_LIMIT),
                    "claimed_sender_type": _cut(claimed_sender_type, FIELD_LIMIT),
                    "claimed_agent_owner": _cut(claimed_agent_owner, FIELD_LIMIT),
                    "signer_node_id": _cut(signer_node_id, FIELD_LIMIT),
                    "claimed_content_hash": _cut(claimed_content_hash, FIELD_LIMIT),
                    "computed_content_hash": _cut(computed_content_hash, FIELD_LIMIT),
                    "signature": _cut(signature, SIGNATURE_LIMIT),
                    "preimage_version": _cut(preimage_version, FIELD_LIMIT),
                    "record_timestamp": _cut(record_timestamp, FIELD_LIMIT),
                    "first_seen": seen,
                    "last_seen": seen,
                    "repeat_count": 1,
                    "live_count": 1 if arrived == "live" else 0,
                    "sync_count": 1 if arrived == "sync" else 0,
                    "repair_count": 1 if arrived == "repair" else 0,
                    "text": kept_text,
                    "text_truncated": len(raw_text) > len(kept_text),
                }
                records.append(entry)
                is_new = True
            else:
                # A repeat: counted, never a second row, and the text and the
                # first sighting are the ones the row was born with.
                entry["repeat_count"] += 1
                entry[f"{arrived}_count"] += 1
                entry["last_seen"] = seen
                is_new = False
            records.sort(key=lambda row: str(row.get("last_seen", "")))
            while True:
                victim = self._row_past_the_caps(records)
                if victim is None:
                    break
                records.remove(victim)
                self._append_archive(victim)
            _write_live_file(self.file_path, records)
            return dict(entry), is_new


_store: Optional[RejectedRecordStore] = None
_store_home: Optional[Path] = None
# Guards the two globals above: two threads building the store at once would
# give two in-memory copies of one file, and one of their writes is lost.
_store_lock = threading.Lock()


def get_store() -> RejectedRecordStore:
    """The process-wide store, built on `default_path()` for the home in force.

    Rebuilt whenever the module home moved: the test suite re-points
    `DPC_HOME_DIR` per test, and two tests must never share a store.
    """
    global _store, _store_home
    home = _dpc_home()
    with _store_lock:
        if _store is None or _store_home != home:
            _store = RejectedRecordStore(default_path())
            _store_home = home
        return _store


def set_store(store: Optional[RejectedRecordStore]) -> None:
    """Install an explicit store (tests). None resets to build-on-next-use."""
    global _store, _store_home
    with _store_lock:
        _store = store
        _store_home = None if store is None else _dpc_home()
