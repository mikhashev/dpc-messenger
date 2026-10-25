"""The process registry: one list of the processes this backend starts (ADR-046).

The backend starts processes for several reasons and used to have no single
answer to "which are running, who asked for them, and how did each one end".
This module is that answer. Every spawn registers its child here; every end
is recorded with one word from a closed vocabulary; the list is persisted so
that the next start can find the children of a run that died without cleaning up.

**What is recorded.** For each child: `pid` and `start_time` (psutil's creation
time; pids are reused, the pair identifies a process), its parent (the backend,
with its own creation time), the `launcher` and its kind (`tool` for an agent's
tool call, `subsystem` for an engine or the browser), the spawn origin (`agent_id`,
`conversation_id`, `tool`, `round`, read from the `SPAWN_ORIGIN` context variable
the tool dispatcher sets), the command (bounded), when it started and ended, the
`exit_reason`, the stop field (`STOP_FIELD`, `stop_kind`) when the reason is `stopped`,
and the raw `exit_code` and `signal`. The resource peaks and `last_output_at` are
fields that exist and stay `None` until the sampler is written.

**Spawn origin of a subsystem.** An engine started inside an agent's tool call
carries that call's origin (`register` reads the context variable at the moment of
the spawn), while its `launcher_kind` is `subsystem`, its owner is the backend and its parent is the
backend. For a subsystem the spawn origin is the call that woke it; it says who
asked for the process, never who owns it (ADR-046 point 3).

**How an end gets its word.** The rule is one sentence: a process the registry
was told a stop was sent to ends with the reason of that stop; a process nobody
stopped ends `crashed` when it died abnormally (a signal on POSIX, an
NTSTATUS-class code on Windows) and `exited` otherwise. So a sender announces its
stop *before* it signals (`note_stop_sent`), and whoever later sees the process
gone calls `end` with the return code. Either order of two parties reporting
the same end gives the same record: the first `end` wins, and a later one may only
complete an `exit_code` the first did not have.

**Precedence.** A stop announced after the backend's orderly stop began
(`begin_shutdown`) is `backend_stop` whoever sent the signal, and so is the stop's
own kill. A stop announced *before* it keeps the word it was announced with: a
start that failed at 10:00:00 and is still being cleared away when Ctrl+C arrives
is `stopped`/`failed_start`, because the refusal stopped it and the shutdown only
found it on the way out. A timeout or a memory-ceiling kill keeps its own word.

**What a number is worth.** `exit_code` is stored as given (on Windows as the
unsigned 32-bit value); `signal` is the POSIX signal's name (`SIGKILL`, never its
number). `exit_code: None` means this backend did not see the end (a process found
gone at the next start). The return code confirms a reading and never decides one:
`crashed` also needs that nobody sent a stop. A kill nobody in DPC sent reads
differently by platform: on POSIX a foreign SIGKILL is `crashed` (signal `SIGKILL`);
on Windows a foreign `taskkill /F` leaves exit code 1, which is an ordinary code, so
it reads `exited` and cannot be told from a normal exit (measured 2026-10-25).

**Where it lives.** The default registry is in memory only. A backend that wants
the list to survive it calls `start_persistent()` (`run_service.py` does, once);
tests, probes and a module loaded on its own never touch `~/.dpc`. Two places,
both written only by a persistent registry:

- `<DPC_HOME>/process_registry.json` holds the open entries and the newest
  `KEEP_FINISHED_MAX` (200) finished ones: what the startup sweep and "what runs now"
  read. Rewritten whole through a temporary file that is fsynced and then swapped in
  with `os.replace` on every spawn and every end - never a truncating open of the
  file itself, so a crash in the middle of a write leaves the old list, and a power
  cut leaves either list, not half of one.
- `<DPC_HOME>/process_registry/YYYY-MM-DD.jsonl` receives every finished entry as one
  JSON line, in the file of the UTC date of its end (the history the graph reads).
  Appended as the node ledger appends (a `.lock` sibling, `O_APPEND`, a torn last
  line healed by the next append); files whose date is more than `HISTORY_DAYS` (7)
  days old are removed at start. A failed append is logged once and never takes a
  spawn or an end down.

**How many finished entries are kept in the state file** (ADR Q7): the newest
`KEEP_FINISHED_MAX` (200), and none older than `KEEP_FINISHED_DAYS` (7). Open
entries are never dropped. 200 would cut the graph to hours, and a whole-file
rewrite of thousands of entries on every spawn and end is too heavy, so the long
history is the daily files, which are append-only.

Imports nothing beyond the standard library (psutil lazily) so that
`run_service.py` can use it before anything else exists. The tree kills it needs
come from `dpc_agent/tools/process.py`, imported only when a kill is due.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import signal as _signal
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- the closed vocabularies (ADR-046 point 4; accepted by Mike, 2026-10-25) ---

EXIT_REASONS = (
    "exited",
    "timeout",
    "memory_ceiling",
    "backend_stop",
    "cancelled_by_agent",
    "orphan_swept",
    "crashed",
    "stopped",
)
# The field that says what kind of stop a `stopped` process got. Not `stop_reason`:
# the gateway writes that name on the wire (the Anthropic Messages API field). The
# dataclass field and this constant are the two places that name it; every log line,
# the check and the persisted key are built from them, and the module refuses to
# import if they disagree.
STOP_FIELD = "stop_kind"
_OLD_STOP_FIELD = "stop_reason"  # read from an older file, never written
# A value stays only while something produces it or a named decision will create its
# producer (ADR-046 point 4). `browser_closed` waits on browser registration;
# `idle_unload` on the owner's open choice of who unloads an idle engine.
STOP_VALUES = ("idle_unload", "ui_stop", "engine_died", "failed_start", "browser_closed")
LAUNCHER_KINDS = ("tool", "subsystem")
# A subsystem's launcher is one of these words; a tool's is the tool's name.
SUBSYSTEMS = ("strata", "llamacpp_server", "browser")

# Reasons a sender other than the backend's own stop can give; announced after the
# orderly stop began, they read `backend_stop` (the precedence rule).
_YIELDS_TO_BACKEND_STOP = ("stopped", "cancelled_by_agent")

COMMAND_MAX_CHARS = 120
KEEP_FINISHED_MAX = 200
KEEP_FINISHED_DAYS = 7
HISTORY_DAYS = 7
SCHEMA_VERSION = 1
REGISTRY_FILE = "process_registry.json"
LOCK_TIMEOUT_S = 2.0
LOCK_STALE_S = 10.0
LOCK_SLEEP_S = 0.01
# psutil's creation time is a float of the OS clock; two reads of one process agree.
START_TIME_TOLERANCE_S = 1.0


class ProcessRegistryClosed(RuntimeError):
    """The registry is refusing new entries: the backend is stopping (point 7)."""


# --- the spawn origin (ADR-046 point 3) ----------------------------------------

@dataclass(frozen=True)
class SpawnOrigin:
    """Who asked for a process: set by the tool dispatcher around a tool call.

    `conversation_id` is the code's word; the screen says "chat"."""

    agent_id: str = ""
    conversation_id: str = ""
    tool: str = ""
    round: int = 0


SPAWN_ORIGIN: "contextvars.ContextVar[Optional[SpawnOrigin]]" = contextvars.ContextVar(
    "dpc_spawn_origin", default=None
)


@contextlib.contextmanager
def spawn_origin(origin: Optional[SpawnOrigin]) -> Iterator[None]:
    """Mark the spawns made inside as made for `origin`; restores what was there."""
    token = SPAWN_ORIGIN.set(origin)
    try:
        yield
    finally:
        SPAWN_ORIGIN.reset(token)


# --- reading an exit ------------------------------------------------------------

def _is_windows(platform: Optional[str]) -> bool:
    return (platform if platform is not None else sys.platform) == "win32"


def exit_code_of(returncode: Optional[int], platform: Optional[str] = None) -> Optional[int]:
    """The code as stored: Windows' unsigned 32-bit value, POSIX's as Python gives it
    (negative for a signal)."""
    if returncode is None:
        return None
    if _is_windows(platform):
        return returncode & 0xFFFFFFFF
    return returncode


_COMMON_SIGNALS = {
    1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 6: "SIGABRT", 8: "SIGFPE",
    9: "SIGKILL", 11: "SIGSEGV", 13: "SIGPIPE", 14: "SIGALRM", 15: "SIGTERM",
}


def signal_of(returncode: Optional[int], platform: Optional[str] = None) -> Optional[str]:
    """The POSIX signal that ended the process, by name, or None (Windows has none)."""
    if returncode is None or _is_windows(platform) or returncode >= 0:
        return None
    number = -returncode
    if sys.platform != "win32":
        try:
            return _signal.Signals(number).name
        except ValueError:
            return f"SIG{number}"
    # A POSIX code read on a Windows host (a test, a file copied over): the
    # numbers every POSIX system agrees on.
    return _COMMON_SIGNALS.get(number, f"SIG{number}")


def is_abnormal_exit(returncode: Optional[int], platform: Optional[str] = None) -> bool:
    """Whether a code is a death rather than an exit: a signal on POSIX, an
    NTSTATUS-class code on Windows.

    The Windows range is `0xC0000000` up to but not including `0xE0000000`:
    severity "error" with the customer bit clear, which is where the system's
    statuses live (`0xC0000005` access violation, `0xC0000409` stack buffer
    overrun, `0xC000013A` control-C exit). The customer bit set - `0xFFFFFFFF`
    from `exit(-1)` and every small negative exit - is an ordinary code a program
    chose, not a crash.
    """
    if returncode is None:
        return False
    if _is_windows(platform):
        return 0xC0000000 <= (returncode & 0xFFFFFFFF) < 0xE0000000
    return returncode < 0


# --- the entry ------------------------------------------------------------------

@dataclass
class ProcessEntry:
    """One process. Persisted as written here; the field names are the API's."""

    pid: int
    start_time: Optional[float]
    parent_pid: Optional[int]
    parent_start_time: Optional[float]
    launcher: str
    launcher_kind: str
    agent_id: Optional[str] = None
    conversation_id: Optional[str] = None
    tool: Optional[str] = None
    round: Optional[int] = None
    command: str = ""
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    exit_reason: Optional[str] = None
    stop_kind: Optional[str] = None
    exit_code: Optional[int] = None
    signal: Optional[str] = None
    # Present, and None until the sampler exists (a later card).
    peak_rss_mb: Optional[float] = None
    peak_commit_mb: Optional[float] = None
    peak_vram_mib: Optional[float] = None
    cpu_seconds: Optional[float] = None
    io_read_bytes: Optional[int] = None
    io_write_bytes: Optional[int] = None
    last_output_at: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProcessEntry":
        known = {f.name for f in fields(cls)}
        data = dict(data)
        if _OLD_STOP_FIELD in data and STOP_FIELD not in data:
            data[STOP_FIELD] = data[_OLD_STOP_FIELD]
        entry = cls(**{k: v for k, v in data.items() if k in known})
        if not isinstance(entry.pid, int) or not entry.launcher or entry.launcher_kind not in LAUNCHER_KINDS:
            raise ValueError(f"not a process entry: {data!r}")
        return entry


assert STOP_FIELD in {f.name for f in fields(ProcessEntry)}, "STOP_FIELD names no ProcessEntry field"


@dataclass
class _Live:
    """What an open entry has that a file cannot hold."""

    handle: Any = None
    log: Optional[logging.Logger] = None
    stop_sent: Optional[Tuple[str, Optional[str]]] = None
    # Whether the backend's orderly stop had already begun when `stop_sent` was announced.
    stop_sent_while_stopping: bool = False


# --- one place to find a process by its identity ---------------------------------

def create_time_of(pid: int) -> Optional[float]:
    """psutil's creation time of `pid`, or None (gone, or psutil absent)."""
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def probe_identity(pid: int, start_time: Optional[float]) -> str:
    """Is the process an entry recorded still that process?

    "same" - alive with the recorded creation time; "other" - the pid is alive
    but belongs to a later process; "gone"; "unverifiable" - alive and the entry
    has no creation time to compare, so nothing says it is ours."""
    now = create_time_of(pid)
    if now is None:
        return "gone"
    if start_time is None:
        return "unverifiable"
    return "same" if abs(now - start_time) <= START_TIME_TOLERANCE_S else "other"


def own_and_ancestor_pids() -> set:
    """The backend's pid and every ancestor's (the `uv` launcher chain): never a target."""
    pids = {os.getpid()}
    try:
        import psutil

        pids.update(p.pid for p in psutil.Process().parents())
    except Exception:
        pids.add(os.getppid())
    return pids


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


def _epoch(iso: Optional[str]) -> Optional[float]:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except (TypeError, ValueError):
        return None


def _ends_mid_line(path: Path) -> bool:
    """True when the last byte is not a newline: the mark a killed writer leaves."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return False
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except FileNotFoundError:
        return False


@contextlib.contextmanager
def _history_lock(path: Path) -> Iterator[None]:
    """The `.lock` sibling, taken as `node_ledger._partition_lock` takes it (exclusive
    create, stale after 10 s, after 2 s of waiting the append goes ahead without it).
    Copied, not imported: the ledger's module pulls in the firewall, and this one
    imports nothing but the standard library."""
    lock_path = path.with_name(path.name + ".lock")
    fd = None
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > LOCK_STALE_S:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                logger.warning("process registry: lock %s held for over %.0fs; appending without it", lock_path, LOCK_TIMEOUT_S)
                break
            time.sleep(LOCK_SLEEP_S)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            with contextlib.suppress(OSError):
                lock_path.unlink()


def _one_line(command: Any) -> str:
    text = command if isinstance(command, str) else " ".join(str(c) for c in command)
    return " ".join(text.split())[:COMMAND_MAX_CHARS]


def _check_reasons(reason: str, stop: Optional[str]) -> None:
    if reason not in EXIT_REASONS:
        raise ValueError(f"{reason!r} is not an exit reason ({', '.join(EXIT_REASONS)})")
    if reason == "stopped":
        if stop not in STOP_VALUES:
            raise ValueError(f"a stopped process needs {STOP_FIELD} from {STOP_VALUES}, not {stop!r}")
    elif stop is not None:
        raise ValueError(f"{STOP_FIELD} goes only with 'stopped', not {reason!r}")


def describe_start(entry: ProcessEntry, detail: str = "") -> str:
    """The start line: `spawned pid N for <launcher>` plus the kind and the origin."""
    if entry.launcher_kind == "tool":
        if entry.agent_id is None:
            who = "origin lost"
        else:
            who = (f"agent_id={entry.agent_id} conversation_id={entry.conversation_id} "
                   f"tool={entry.tool} round={entry.round}")
    else:
        who = f"parent pid {entry.parent_pid}"
    extra = f"; {detail}" if detail else ""
    return f"spawned pid {entry.pid} for {entry.launcher} ({entry.launcher_kind}; {who}{extra}): {entry.command}"


def describe_end(entry: ProcessEntry) -> str:
    """The end line: pid, launcher, reason, stop kind, code, signal."""
    began, ended = _epoch(entry.started_at), _epoch(entry.ended_at)
    after = f" after {max(0.0, ended - began):.1f}s" if began is not None and ended is not None else ""
    return (
        f"process ended: pid {entry.pid} launcher {entry.launcher} reason {entry.exit_reason} "
        f"{STOP_FIELD} {getattr(entry, STOP_FIELD) or 'none'} "
        f"exit_code {'unseen' if entry.exit_code is None else entry.exit_code} "
        f"signal {entry.signal or 'none'}{after}"
    )


# --- what the startup sweep did, to be written once logging exists ----------------

@dataclass
class SweepOutcome:
    """The lines the startup sweep earned. Logging is not configured yet when the
    sweep runs (it runs before the first spawn), so `run_service.py` writes them
    with `log` once the handlers are in place, as it does the job's outcome."""

    records: List[Tuple[int, str]] = None  # type: ignore[assignment]
    killed: List[ProcessEntry] = None  # type: ignore[assignment]
    closed: List[ProcessEntry] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.records = [] if self.records is None else self.records
        self.killed = [] if self.killed is None else self.killed
        self.closed = [] if self.closed is None else self.closed

    def add(self, level: int, message: str) -> None:
        self.records.append((level, message))

    def log(self, target: Optional[logging.Logger] = None) -> None:
        for level, message in self.records:
            (target or logger).log(level, "%s", message)


# --- the registry -------------------------------------------------------------------

class ProcessRegistry:
    """The list. Thread-safe: tool threads, supervisors and the shutdown share it."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        clock: Callable[[], float] = time.time,
        create_time: Callable[[int], Optional[float]] = create_time_of,
        platform: Optional[str] = None,
        keep_finished_max: int = KEEP_FINISHED_MAX,
        keep_finished_days: float = KEEP_FINISHED_DAYS,
        history_dir: Optional[Path] = None,
        history_days: float = HISTORY_DAYS,
    ):
        self.path = Path(path) if path is not None else None
        # The daily files sit beside the state file under its own name without the
        # suffix (`process_registry.json` -> `process_registry/`); none without a path.
        if history_dir is not None:
            self.history_dir: Optional[Path] = Path(history_dir)
        else:
            self.history_dir = None
            if self.path is not None:
                beside = self.path.with_suffix("")
                self.history_dir = beside if beside != self.path else self.path.with_name(self.path.name + ".days")
        self._history_days = history_days
        self._last_history_error: Optional[str] = None
        self._clock = clock
        self._create_time = create_time
        self._platform = platform
        self._keep_max = keep_finished_max
        self._keep_days = keep_finished_days
        self._lock = threading.RLock()
        self._entries: List[ProcessEntry] = []
        self._open: Dict[int, ProcessEntry] = {}
        self._live: Dict[int, _Live] = {}
        self._last_closed: Dict[int, ProcessEntry] = {}
        # Open entries read from the file: the children of an earlier run.
        self._inherited: List[ProcessEntry] = []
        self._stopping = False
        self._ended_in_the_stop: List[ProcessEntry] = []
        self._backend_start_time: Optional[float] = None
        self._last_write_error: Optional[str] = None
        self.load_notes: List[Tuple[int, str]] = []

    # -- state --------------------------------------------------------------------

    @property
    def stopping(self) -> bool:
        return self._stopping

    def check_open(self) -> None:
        """Raise `ProcessRegistryClosed` when the backend is stopping. Called
        before a spawn, so that nothing is born after the stop began."""
        if self._stopping:
            raise ProcessRegistryClosed("the backend is stopping; it starts no new process")

    def begin_shutdown(self) -> bool:
        """Refuse new entries from now on and turn every later stop into
        `backend_stop`. Idempotent; True the first time."""
        with self._lock:
            first = not self._stopping
            self._stopping = True
        if first:
            logger.info("process registry: the backend is stopping; no new process will be registered")
        return first

    # -- the file -----------------------------------------------------------------

    def load(self) -> None:
        """Read the persisted list. Open entries become `inherited` (the children
        of an earlier run, for `sweep_orphans`). A file that cannot be read is
        set aside as `.corrupt` and the list starts empty; never raises."""
        if self.path is None:
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.load_notes.append((logging.WARNING, f"process registry: {self.path} could not be read ({exc}); starting empty"))
            return
        try:
            data = json.loads(raw)
            loaded = [ProcessEntry.from_dict(item) for item in data["entries"]]
        except (ValueError, KeyError, TypeError) as exc:
            aside = self.path.with_name(self.path.name + ".corrupt")
            try:
                os.replace(self.path, aside)
                where = f"set aside as {aside.name}"
            except OSError:
                where = "left in place"
            self.load_notes.append((
                logging.WARNING,
                f"process registry: {self.path} is not a registry file ({type(exc).__name__}: {exc}); "
                f"{where}, starting empty",
            ))
            return
        with self._lock:
            for entry in loaded:
                self._entries.append(entry)
                if entry.is_open:
                    self._inherited.append(entry)

    def _persist_locked(self) -> None:
        self._prune_locked()  # also in memory: a registry that never writes must not grow for ever
        if self.path is None:
            return
        payload = {
            "schema": SCHEMA_VERSION,
            "backend_pid": os.getpid(),
            "backend_start_time": self._own_start_time(),
            "saved_at": _iso(self._clock()),
            "entries": [e.to_dict() for e in self._entries],
        }
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=1))
                handle.flush()
                os.fsync(handle.fileno())  # on disk before the swap, or a power cut swaps in half a list
            last: Optional[OSError] = None
            for _ in range(10):
                try:
                    os.replace(tmp, self.path)
                    last = None
                    break
                except PermissionError as exc:  # Windows: a reader has it open for a moment
                    last = exc
                    time.sleep(0.02)
            if last is not None:
                raise last
            self._last_write_error = None
        except OSError as exc:
            # Said once per distinct failure: it repeats on every spawn.
            if str(exc) != self._last_write_error:
                self._last_write_error = str(exc)
                logger.warning("process registry: could not write %s (%s); the list is kept in memory", self.path, exc)
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _prune_locked(self) -> None:
        finished = [e for e in self._entries if not e.is_open]
        drop: set = set()
        cutoff = self._clock() - self._keep_days * 86400
        for e in finished:
            ended = _epoch(e.ended_at)
            if ended is not None and ended < cutoff:
                drop.add(id(e))
        kept = [e for e in finished if id(e) not in drop]
        for e in kept[: max(0, len(kept) - self._keep_max)]:
            drop.add(id(e))
        if drop:
            self._entries = [e for e in self._entries if id(e) not in drop]
            self._last_closed = {p: e for p, e in self._last_closed.items() if id(e) not in drop}

    # -- the daily history ----------------------------------------------------------

    def _append_history_locked(self, entry: ProcessEntry) -> None:
        """One JSON line for a finished entry, in the file of the UTC date of its end.
        Never raises: a spawn or an end must not fail because the history could not
        be written. The same failure is logged once, not on every end."""
        if self.history_dir is None:
            return
        try:
            day = (entry.ended_at or _iso(self._clock()))[:10]
            path = self.history_dir / f"{day}.jsonl"
            self.history_dir.mkdir(parents=True, exist_ok=True)
            data = (json.dumps(entry.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
            with _history_lock(path):
                if _ends_mid_line(path):
                    data = b"\n" + data  # a killed writer's torn last line must not swallow this one
                # O_BINARY where it exists: the Windows CRT would turn the newline into CRLF.
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
                fd = os.open(str(path), flags, 0o644)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
            self._last_history_error = None
        except Exception as exc:
            said = f"{type(exc).__name__}: {exc}"
            if said != self._last_history_error:
                self._last_history_error = said
                logger.warning(
                    "process registry: could not append pid %s to the history under %s (%s)",
                    entry.pid, self.history_dir, said,
                )

    def prune_history(self) -> List[Path]:
        """Remove the daily files dated more than `history_days` before today (UTC).
        Called once at start; files whose names are not a date are left alone.
        Never raises; returns what it removed."""
        removed: List[Path] = []
        if self.history_dir is None:
            return removed
        try:
            today = datetime.fromtimestamp(self._clock(), timezone.utc).date()
            cutoff = today - timedelta(days=self._history_days)
            for path in sorted(self.history_dir.glob("????-??-??.jsonl")):
                try:
                    dated = date.fromisoformat(path.stem)
                    if dated < cutoff:
                        path.unlink()
                        removed.append(path)
                except (ValueError, OSError) as exc:
                    logger.warning("process registry: could not prune %s (%s)", path, exc)
        except OSError as exc:
            logger.warning("process registry: could not list %s (%s)", self.history_dir, exc)
        return removed

    def _own_start_time(self) -> Optional[float]:
        if self._backend_start_time is None:
            self._backend_start_time = self._create_time(os.getpid())
        return self._backend_start_time

    # -- spawn --------------------------------------------------------------------

    def register(
        self,
        pid: int,
        *,
        launcher: str,
        launcher_kind: str,
        command: Any,
        origin: Optional[SpawnOrigin] = None,
        handle: Any = None,
        log: Optional[logging.Logger] = None,
        detail: str = "",
        parent_pid: Optional[int] = None,
        parent_start_time: Optional[float] = None,
    ) -> ProcessEntry:
        """Record a child at its birth and log the start line.

        `handle` is the `Popen` (or anything with `pid` and `kill`) of a child
        the backend can kill itself; it is not persisted. `log` is the logger the
        start and end lines go through (the launcher's own, so a test or an
        operator filtering on it still sees them). The origin is read from
        `SPAWN_ORIGIN` when not given; a `tool` child without one is a lost
        attribution and is logged at WARNING."""
        if launcher_kind not in LAUNCHER_KINDS:
            raise ValueError(f"{launcher_kind!r} is not a launcher kind ({', '.join(LAUNCHER_KINDS)})")
        if not launcher:
            raise ValueError("a process needs a launcher")
        if launcher_kind == "subsystem" and launcher not in SUBSYSTEMS:
            raise ValueError(f"{launcher!r} is not a subsystem ({', '.join(SUBSYSTEMS)})")
        if pid == os.getpid():
            raise ValueError(f"pid {pid} is the backend's own; it is not one of its children")
        out = log or logger
        origin = origin if origin is not None else SPAWN_ORIGIN.get()
        with self._lock:
            self.check_open()
            stale = self._open.get(pid)
            if stale is not None:
                # The pid was reused, so the earlier child ended without anyone seeing it.
                logger.warning(
                    "process registry: pid %s is registered again while its earlier entry (%s) "
                    "is open; closing that one as exited with no exit code", pid, stale.launcher,
                )
                self._finish_locked(stale, "exited", None, None, announce=False)
            entry = ProcessEntry(
                pid=pid,
                start_time=self._create_time(pid),
                parent_pid=parent_pid if parent_pid is not None else os.getpid(),
                parent_start_time=parent_start_time if parent_start_time is not None else self._own_start_time(),
                launcher=launcher,
                launcher_kind=launcher_kind,
                agent_id=origin.agent_id if origin is not None else None,
                conversation_id=origin.conversation_id if origin is not None else None,
                tool=origin.tool if origin is not None else None,
                round=origin.round if origin is not None else None,
                command=_one_line(command),
                started_at=_iso(self._clock()),
            )
            self._entries.append(entry)
            self._open[pid] = entry
            self._live[pid] = _Live(handle=handle, log=out)
            self._persist_locked()
        out.info("%s", describe_start(entry, detail))
        if launcher_kind == "tool" and origin is None:
            out.warning(
                "lost attribution: pid %s for %s is a tool's child and no spawn origin was set "
                "(the dispatcher sets SPAWN_ORIGIN; a thread started by the tool does not inherit it)",
                pid, launcher,
            )
        return entry

    # -- end ----------------------------------------------------------------------

    def note_stop_sent(self, pid: int, reason: str, stop: Optional[str] = None) -> None:
        """Announce, before signalling, that a stop is being sent to `pid`.

        The first announcement stands. `end` then gives the process this reason
        instead of reading its return code as a crash. Whether the backend's own
        stop had begun at this moment is kept with it: that decides the precedence."""
        _check_reasons(reason, stop)
        with self._lock:
            live = self._live.get(pid)
            if live is not None and live.stop_sent is None:
                live.stop_sent = (reason, stop)
                live.stop_sent_while_stopping = self._stopping

    def end(
        self,
        pid: int,
        returncode: Optional[int] = None,
        *,
        reason: Optional[str] = None,
        stop: Optional[str] = None,
    ) -> Optional[ProcessEntry]:
        """Record that `pid` ended. Returns the entry (the existing one when it
        was already closed), or None when the registry never saw this pid.

        Without `reason` the word is derived: the stop that was announced (kept as
        announced if it was announced before the backend's stop began, `backend_stop`
        if after), else `crashed` for an abnormal code, else `exited`. A second `end` for the
        same process changes nothing but an `exit_code` the first lacked."""
        if reason is not None:
            _check_reasons(reason, stop)
        with self._lock:
            entry = self._open.get(pid)
            if entry is None:
                closed = self._last_closed.get(pid)
                if closed is not None and closed.exit_code is None and returncode is not None:
                    closed.exit_code = exit_code_of(returncode, self._platform)
                    closed.signal = signal_of(returncode, self._platform)
                    self._persist_locked()
                return closed
            live = self._live.get(pid)
            announced_before_the_stop = False
            if reason is None:
                sent = live.stop_sent if live is not None else None
                if sent is not None:
                    reason, stop = sent
                    announced_before_the_stop = not live.stop_sent_while_stopping
                elif is_abnormal_exit(returncode, self._platform):
                    reason = "crashed"
                else:
                    reason = "exited"
            return self._finish_locked(
                entry, reason, stop, returncode, announce=True,
                announced_before_the_stop=announced_before_the_stop,
            )

    def _finish_locked(
        self,
        entry: ProcessEntry,
        reason: str,
        stop: Optional[str],
        returncode: Optional[int],
        *,
        announce: bool,
        announced_before_the_stop: bool = False,
    ) -> ProcessEntry:
        if self._stopping and reason in _YIELDS_TO_BACKEND_STOP and not announced_before_the_stop:
            reason, stop = "backend_stop", None
        _check_reasons(reason, stop)
        entry.ended_at = _iso(self._clock())
        entry.exit_reason = reason
        setattr(entry, STOP_FIELD, stop)
        entry.exit_code = exit_code_of(returncode, self._platform)
        entry.signal = signal_of(returncode, self._platform)
        live = self._live.pop(entry.pid, None)
        if self._open.get(entry.pid) is entry:
            del self._open[entry.pid]
        self._inherited = [e for e in self._inherited if e is not entry]
        self._last_closed[entry.pid] = entry
        if self._stopping:
            self._ended_in_the_stop.append(entry)
        self._persist_locked()
        self._append_history_locked(entry)
        if announce:
            out = (live.log if live is not None and live.log is not None else logger)
            out.log(logging.WARNING if reason == "crashed" else logging.INFO, "%s", describe_end(entry))
        return entry

    # -- reading ------------------------------------------------------------------

    def open_entries(self, kind: Optional[str] = None) -> List[ProcessEntry]:
        with self._lock:
            return [e for e in self._open.values() if kind is None or e.launcher_kind == kind]

    def entries(self) -> List[ProcessEntry]:
        """Every entry kept, oldest first (copies; the registry's own are not handed out)."""
        with self._lock:
            return [ProcessEntry.from_dict(e.to_dict()) for e in self._entries]

    def handle_of(self, pid: int) -> Any:
        with self._lock:
            live = self._live.get(pid)
            return live.handle if live is not None else None

    # -- the startup sweep (point 6) ------------------------------------------------

    def sweep_orphans(
        self,
        *,
        kill: Optional[Callable[[int], str]] = None,
        probe: Callable[[int, Optional[float]], str] = probe_identity,
        protected: Optional[Callable[[], set]] = None,
    ) -> SweepOutcome:
        """Deal with the open entries an earlier run left in the file.

        An entry whose pid is alive **and** whose creation time matches is a
        process of that run still running: its tree is killed and the entry is
        closed `orphan_swept`. An entry whose process is gone (or whose pid now
        belongs to a stranger) is closed `backend_stop` with no exit code: the
        run ended without this backend seeing it, and the job or the OS took the
        process. Never the backend's own pid and never an ancestor's. Never raises."""
        if kill is None:
            kill = _kill_pid_tree
        outcome = SweepOutcome()
        for level, message in self.load_notes:
            outcome.add(level, message)
        self.load_notes = []
        with self._lock:
            inherited, self._inherited = list(self._inherited), []
        shielded = (protected or own_and_ancestor_pids)()
        for entry in inherited:
            what = f"pid {entry.pid} ({entry.launcher}, started {entry.started_at}, {entry.command[:60]!r})"
            try:
                state = probe(entry.pid, entry.start_time)
                if entry.pid in shielded:
                    outcome.add(logging.ERROR, (
                        f"orphan sweep: {what} names the backend or one of its ancestors; that is a "
                        "defect in the list. Not killed; the entry is closed backend_stop"))
                    self._close_swept(entry, "backend_stop", outcome, killed=False)
                elif state == "same":
                    verdict = kill(entry.pid)
                    if probe(entry.pid, entry.start_time) == "same" and not self._wait_gone(entry, probe):
                        outcome.add(logging.ERROR, (
                            f"orphan sweep: {what} of an earlier run is still running after the kill "
                            f"({verdict}); the entry stays open"))
                        with self._lock:
                            self._inherited.append(entry)
                        continue
                    outcome.add(logging.WARNING, (
                        f"orphan sweep: {what} of an earlier run was still running; killed it and its "
                        f"descendants ({verdict}): orphan_swept"))
                    self._close_swept(entry, "orphan_swept", outcome, killed=True)
                elif state == "unverifiable":
                    outcome.add(logging.WARNING, (
                        f"orphan sweep: {what} has no recorded creation time and its pid is alive, so "
                        "nothing says it is the same process; left alone, the entry is closed "
                        "backend_stop with no exit code"))
                    self._close_swept(entry, "backend_stop", outcome, killed=False)
                else:
                    detail = ("the pid now belongs to a process started later; left alone"
                              if state == "other" else "the process is gone")
                    outcome.add(logging.INFO, (
                        f"orphan sweep: {what} of an earlier run was found closed at start ({detail}); "
                        "recorded backend_stop with no exit code"))
                    self._close_swept(entry, "backend_stop", outcome, killed=False)
            except Exception as exc:  # the sweep must not stop the backend from starting
                outcome.add(logging.WARNING, f"orphan sweep: {what} could not be handled ({type(exc).__name__}: {exc})")
                with self._lock:
                    self._inherited.append(entry)
        return outcome

    def _wait_gone(self, entry: ProcessEntry, probe: Callable[[int, Optional[float]], str]) -> bool:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if probe(entry.pid, entry.start_time) != "same":
                return True
            time.sleep(0.05)
        return probe(entry.pid, entry.start_time) != "same"

    def _close_swept(self, entry: ProcessEntry, reason: str, outcome: SweepOutcome, *, killed: bool) -> None:
        with self._lock:
            self._finish_locked(entry, reason, None, None, announce=False)
        (outcome.killed if killed else outcome.closed).append(entry)

    # -- the orderly stop (point 7) ---------------------------------------------------

    def stop_commands(
        self,
        *,
        kill_tree: Optional[Callable[[Any], str]] = None,
        wait_s: float = 2.0,
    ) -> List[ProcessEntry]:
        """Kill every command child hard (`launcher_kind: tool`), reason `backend_stop`.

        Each is announced before the kill, so the thread that is running it, which
        sees it die a moment later, reads the same reason. A child the backend holds
        a `Popen` for is killed through it (process group and walkable descendants
        on POSIX, `taskkill /T` on Windows); the backend's own pid and its
        ancestors are skipped and logged."""
        if kill_tree is None:
            kill_tree = _kill_tree_of
        ended: List[ProcessEntry] = []
        shielded = own_and_ancestor_pids()
        for entry in self.open_entries("tool"):
            if entry.pid in shielded:
                logger.error(
                    "process registry: pid %s (%s) names the backend or an ancestor; a defect, skipped",
                    entry.pid, entry.launcher,
                )
                continue
            handle = self.handle_of(entry.pid)
            self.note_stop_sent(entry.pid, "backend_stop")
            try:
                verdict = kill_tree(handle if handle is not None else entry.pid)
            except Exception as exc:
                verdict = f"the kill failed ({type(exc).__name__}: {exc})"
                logger.warning("process registry: could not kill pid %s (%s): %s", entry.pid, entry.launcher, exc)
            returncode = self._returncode_of(handle, wait_s)
            done = self.end(entry.pid, returncode, reason="backend_stop")
            logger.debug("process registry: stopped pid %s (%s): %s", entry.pid, entry.launcher, verdict)
            if done is not None:
                ended.append(done)
        return ended

    @staticmethod
    def _returncode_of(handle: Any, wait_s: float) -> Optional[int]:
        poll = getattr(handle, "poll", None)
        if poll is None:
            return None
        deadline = time.monotonic() + wait_s
        while True:
            try:
                code = poll()
            except Exception:
                return None
            if code is not None or time.monotonic() >= deadline:
                return code
            time.sleep(0.02)

    def finish_shutdown(
        self,
        *,
        kill: Optional[Callable[[int], str]] = None,
        probe: Callable[[int, Optional[float]], str] = probe_identity,
    ) -> List[ProcessEntry]:
        """Verify, after every stop has run, that no entry is still open; write one
        log line per process the stop ended.

        An entry still open names a process nobody ended. If it is gone it is
        closed `backend_stop` with no exit code; if it is alive (and is the process
        recorded) it is killed, the failure is logged at ERROR, and it is closed
        the same way. The job is the backstop behind this, not a substitute."""
        if kill is None:
            kill = _kill_pid_tree
        shielded = own_and_ancestor_pids()
        for entry in self.open_entries():
            if entry.pid in shielded:
                logger.error("process registry: pid %s (%s) names the backend or an ancestor; skipped", entry.pid, entry.launcher)
                continue
            state = probe(entry.pid, entry.start_time)
            if state == "same":
                logger.error(
                    "process registry: pid %s (%s) is still running after the stop; killing it",
                    entry.pid, entry.launcher,
                )
                try:
                    kill(entry.pid)
                except Exception as exc:
                    logger.error("process registry: could not kill pid %s: %s", entry.pid, exc)
            self.end(entry.pid, None, reason="backend_stop")
        with self._lock:
            done = list(self._ended_in_the_stop)
        for entry in done:
            logger.info(
                "backend stop: pid %s (%s, %s) ended %s%s, exit_code %s, signal %s",
                entry.pid, entry.launcher, entry.launcher_kind, entry.exit_reason,
                f"/{getattr(entry, STOP_FIELD)}" if getattr(entry, STOP_FIELD) else "",
                "unseen" if entry.exit_code is None else entry.exit_code, entry.signal or "none",
            )
        if not done:
            logger.info("backend stop: no process was running")
        return done


def _kill_tree_of(target: Any) -> str:
    """`_kill_process_tree` from the tools package, imported only when a kill is due."""
    from .dpc_agent.tools import process as _process

    if isinstance(target, int):
        return _process.kill_pid_tree(target)
    return _process._kill_process_tree(target)


def _kill_pid_tree(pid: int) -> str:
    from .dpc_agent.tools import process as _process

    return _process.kill_pid_tree(pid)


# --- the one registry of this process ------------------------------------------------

_registry = ProcessRegistry()


def get_registry() -> ProcessRegistry:
    return _registry


def set_registry(registry: ProcessRegistry) -> ProcessRegistry:
    """Install `registry` as this process's; returns the one it replaces (tests)."""
    global _registry
    previous, _registry = _registry, registry
    return previous


def reset_registry() -> ProcessRegistry:
    """A fresh in-memory registry in place of whatever there was (tests)."""
    set_registry(ProcessRegistry())
    return _registry


def registry_path() -> Path:
    """`<DPC_HOME>/process_registry.json`; `DPC_HOME` is honoured as elsewhere."""
    return Path(os.environ.get("DPC_HOME", Path.home() / ".dpc")) / REGISTRY_FILE


def start_persistent(path: Optional[Path] = None) -> SweepOutcome:
    """Make the registry of this process the persisted one and sweep what the last
    run left. Called once by `run_service.py` after the job is entered and before
    the first spawn. Never raises; the returned outcome is logged once logging exists."""
    try:
        registry = ProcessRegistry(path if path is not None else registry_path())
        registry.load()
        registry.prune_history()
        outcome = registry.sweep_orphans()
        set_registry(registry)
        with registry._lock:
            registry._persist_locked()
        return outcome
    except Exception as exc:
        outcome = SweepOutcome()
        outcome.add(logging.WARNING, f"process registry: could not start the persisted list ({type(exc).__name__}: {exc}); it is kept in memory")
        return outcome
