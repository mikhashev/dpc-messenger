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
and the raw `exit_code` and `signal`; then the resource peaks and `last_output_at`.

**The resource sampler** (`ResourceSampler`, started by `start_resource_sampler()`,
which `run_service.py` calls once; tests start one on demand; stopped by
`finish_shutdown()`). One daemon thread, every `SAMPLE_INTERVAL_S` (2 s), over
the open entries, each only while `probe_identity` says the pid is still the
recorded process. It walks the tree (the child and `psutil` `children(recursive=True)`,
the walk the memory ceiling makes) and keeps peaks, never a series:

- `peak_rss_mb`: the peak of the tree's summed RSS, never below the child's own
  lifetime peak where the OS keeps one (Windows `peak_wset`, Linux `VmHWM`).
- `peak_commit_mb`: the same with commit (Windows `private`, the child's own
  `peak_pagefile`). None on POSIX: it has no cheap equivalent (`vms` counts every
  reservation; `_process_memory_mb` in `process.py` says why).
- `cpu_seconds`, `io_read_bytes`, `io_write_bytes`: per process by (pid, creation
  time), the last value seen, summed over every process ever seen in the tree, so a
  total does not fall when a child exits. IO is None on macOS (psutil has none).
- `peak_vram_mib`: engines only (`ENGINES`), summed over the tree. Windows: the PDH
  counter `\\GPU Process Memory(*)\\Dedicated Usage` through ctypes, a commitment
  that can exceed what the card holds; Linux: NVML through ctypes when it loads;
  macOS: None. The sampler never spawns a process (no `nvidia-smi`).
- `last_output_at`: for a `run_supervised` child, when its readers last got bytes
  (`note_output`); for an engine, the newest mtime of the log files it was
  registered with (`log_paths`), from its start on.

A command shorter than the interval (most of what agents run) wakes the sampler
when it registers and is sampled at `EARLY_SAMPLES_S` before it joins the tick;
on Windows `end()` also reads the child's own lifetime figures through its `Popen`
handle, which still holds the exited process. So a 0.2 s command gets RSS, commit,
CPU and IO on Windows (the commit and the child's own peak whatever the timing);
RSS, CPU and IO on Linux and RSS and CPU on macOS, from whichever early sample
found it alive (a command that ends inside the first few milliseconds can get
nothing there). A failing reading leaves its field None and is logged once per
kind of failure; the thread holds the registry lock only to copy and to write.
The figures reach the state file with the spawn and end writes, and when an open
entry's peak rose by `PEAK_WRITE_RISE` (25 %) and `PEAK_WRITE_MIN_MB` (64 MB;
`PEAK_WRITE_MIN_VRAM_MIB`, 256 MiB, for VRAM) over what the file holds, at most
once per `PEAK_WRITE_MIN_INTERVAL_S` (10 s).

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
import re
import signal as _signal
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

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

# --- the resource sampler (ADR-046 point 3, "Resources are peaks, sampled") ---
SAMPLE_INTERVAL_S = 2.0
# Extra samples of a new entry, in seconds after its registration, before it joins
# the regular tick: most agent commands end well inside one interval.
EARLY_SAMPLES_S = (0.0, 0.1, 0.3, 0.7)
# The state file is rewritten by the sampler when an open entry's peak has risen
# by this fraction over the value last written, and by at least the floor, at
# most once per `PEAK_WRITE_MIN_INTERVAL_S`.
PEAK_WRITE_RISE = 0.25
PEAK_WRITE_MIN_MB = 64.0
PEAK_WRITE_MIN_VRAM_MIB = 256.0
PEAK_WRITE_MIN_INTERVAL_S = 10.0
# The subsystems whose VRAM is read: the two local engines (provider types).
ENGINES = ("strata", "llamacpp_server")
_MB = 1024 * 1024
# The peaks whose rise rewrites the state file, each with its floor.
_PEAK_FLOORS = (
    ("peak_rss_mb", PEAK_WRITE_MIN_MB),
    ("peak_commit_mb", PEAK_WRITE_MIN_MB),
    ("peak_vram_mib", PEAK_WRITE_MIN_VRAM_MIB),
)


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
    # Filled by the resource sampler where it runs (the backend; see the module
    # docstring for what each platform gives); None where nothing was read.
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
    # The files an engine writes its output to; their newest mtime is `last_output_at`.
    log_paths: Tuple[Path, ...] = ()
    # When (the sampler's monotonic clock) the early samples of this entry are due.
    early_due: List[float] = field(default_factory=list)
    # Every process ever seen in the tree, by (pid, creation time): its last
    # (cpu seconds, io read bytes, io write bytes). Summed, so a child that exits
    # does not take its share out of the totals.
    seen: Dict[Tuple[int, Optional[float]], Tuple[Optional[float], Optional[int], Optional[int]]] = field(default_factory=dict)
    # The peaks as the state file last had them (the base of the sampler's write).
    written: Dict[str, Optional[float]] = field(default_factory=dict)


# --- one place to find a process by its identity ---------------------------------

def create_time_of(pid: int) -> Optional[float]:
    """psutil's creation time of `pid`, or None (gone, or psutil absent)."""
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _is_zombie(pid: int) -> bool:
    """A POSIX process that has died and waits for its parent to reap it."""
    try:
        import psutil

        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except Exception:
        return False


def probe_identity(pid: int, start_time: Optional[float]) -> str:
    """Is the process an entry recorded still that process?

    "same" - alive with the recorded creation time; "other" - the pid is alive
    but belongs to a later process; "gone"; "unverifiable" - alive and the entry
    has no creation time to compare, so nothing says it is ours. A zombie is
    gone: it has died and keeps only its creation time until its parent reaps
    it, which on Linux is what a killed child of this very process looks like."""
    now = create_time_of(pid)
    if now is None or _is_zombie(pid):
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
        # The resource sampler, when one was started (the backend; tests on demand).
        self._sampler: Optional["ResourceSampler"] = None

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
            self._note_written_locked()
        except OSError as exc:
            # Said once per distinct failure: it repeats on every spawn.
            if str(exc) != self._last_write_error:
                self._last_write_error = str(exc)
                logger.warning("process registry: could not write %s (%s); the list is kept in memory", self.path, exc)
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _note_written_locked(self) -> None:
        """The peaks of every open entry are now on disk: the base the sampler's
        next write is measured against."""
        for pid, entry in self._open.items():
            live = self._live.get(pid)
            if live is not None:
                live.written = {name: getattr(entry, name) for name, _ in _PEAK_FLOORS}

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
        log_paths: Sequence[Any] = (),
    ) -> ProcessEntry:
        """Record a child at its birth and log the start line.

        `handle` is the `Popen` (or anything with `pid` and `kill`) of a child
        the backend can kill itself; it is not persisted. `log` is the logger the
        start and end lines go through (the launcher's own, so a test or an
        operator filtering on it still sees them). The origin is read from
        `SPAWN_ORIGIN` when not given; a `tool` child without one is a lost
        attribution and is logged at WARNING. `log_paths` are the files the
        child writes its output to (an engine's log); the sampler reads their
        newest modification time as `last_output_at`. Not persisted either."""
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
            live = self._live[pid] = _Live(handle=handle, log=out, log_paths=tuple(Path(p) for p in log_paths))
            sampler = self._sampler
            if sampler is not None:
                live.early_due = sampler.early_schedule()
            self._persist_locked()
        if sampler is not None:
            sampler.wake()  # the first sample now, not at the next tick
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
        same process changes nothing but an `exit_code` the first lacked.

        With a sampler running, the child's own lifetime figures are read once
        more first, while its `Popen` still holds the process object (Windows):
        a command shorter than the sampler's interval gets its peaks from that."""
        if reason is not None:
            _check_reasons(reason, stop)
        sampler = self._sampler
        if sampler is not None:
            sampler.read_at_end(pid)
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

    def note_output(self, pid: int) -> None:
        """`pid` wrote to its stdout or stderr just now (`run_supervised`'s readers).

        Kept in memory only: the next write of the state file (a spawn, an end,
        the sampler's) carries it, so a chatty command costs no disk."""
        at = _iso(self._clock())
        with self._lock:
            entry = self._open.get(pid)
            if entry is not None:
                entry.last_output_at = at

    # -- the resource sampler ---------------------------------------------------------

    def start_sampler(self, **options: Any) -> "ResourceSampler":
        """Start the resource sampler over this registry (once; a second call returns
        the running one). `options` go to `ResourceSampler` (tests inject readers)."""
        with self._lock:
            if self._sampler is None:
                self._sampler = ResourceSampler(self, **options)
            sampler = self._sampler
        sampler.start()
        return sampler

    def stop_sampler(self) -> None:
        """Stop the sampler, if one runs. Harmless when none was ever started."""
        with self._lock:
            sampler, self._sampler = self._sampler, None
        if sampler is not None:
            sampler.stop()

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
        the same way. The job is the backstop behind this, not a substitute.
        The resource sampler stops first: nothing it could read now is wanted."""
        self.stop_sampler()
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


# --- the resource sampler (ADR-046 point 3) ---------------------------------------------

@dataclass
class ProcessSample:
    """One process of a tree at one moment; None where a figure was not read."""

    pid: int
    create_time: Optional[float]
    rss_mb: Optional[float] = None
    commit_mb: Optional[float] = None
    cpu_seconds: Optional[float] = None
    io_read_bytes: Optional[int] = None
    io_write_bytes: Optional[int] = None


@dataclass
class TreeSample:
    """A child and its descendants at one moment, plus the OS's lifetime peaks of
    the child itself where the OS keeps them (the tree's peak is never below them)."""

    root_create_time: Optional[float]
    processes: List[ProcessSample]
    root_peak_rss_mb: Optional[float] = None
    root_peak_commit_mb: Optional[float] = None


def _key_time(create_time: Optional[float]) -> Optional[float]:
    return None if create_time is None else round(create_time, 3)


def _higher(old: Optional[float], new: Optional[float]) -> Optional[float]:
    if new is None:
        return old
    return new if old is None or new > old else old


def _vm_hwm_mb(pid: int) -> Optional[float]:
    """Linux: the process's lifetime peak RSS (`VmHWM` in `/proc/<pid>/status`)."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024.0  # kB
    except FileNotFoundError:
        return None
    return None


# "Dedicated Usage" is what the driver has committed to the process in the card's
# dedicated memory: a commitment, which can exceed what the card holds at that
# moment, so these figures are never summed into a card total. Instances are
# named `pid_<N>_luid_..._phys_<k>`, one per adapter.
_PDH_GPU_COUNTER = r"\GPU Process Memory(*)\Dedicated Usage"
_PDH_MORE_DATA = 0x800007D2
_PDH_NO_DATA = 0x800007D5
# The counter object or the counter does not exist here (no WDDM GPU counters).
_PDH_ABSENT = (0xC0000BB8, 0xC0000BB9, 0x800007D0, 0xC0000BC4)
_PDH_FMT_LARGE = 0x00000400
_PDH_INSTANCE_PID = re.compile(r"^pid_(\d+)_")


def read_vram_windows() -> Optional[Dict[int, float]]:
    """MiB of dedicated GPU memory per pid, from the PDH counter, through ctypes on
    `pdh.dll` (no subprocess, no dependency). None where the counter does not
    exist; raises OSError on any other failure (the sampler says it once)."""
    import ctypes
    from ctypes import wintypes

    class _Value(ctypes.Union):
        _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double),
                    ("largeValue", ctypes.c_longlong), ("AnsiStringValue", ctypes.c_char_p),
                    ("WideStringValue", ctypes.c_wchar_p)]

    class _CounterValue(ctypes.Structure):
        _fields_ = [("CStatus", wintypes.DWORD), ("value", _Value)]

    class _Item(ctypes.Structure):
        _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _CounterValue)]

    pdh = ctypes.WinDLL("pdh.dll")
    query, counter = ctypes.c_void_p(), ctypes.c_void_p()
    status = pdh.PdhOpenQueryW(None, None, ctypes.byref(query)) & 0xFFFFFFFF
    if status:
        raise OSError(f"PdhOpenQueryW returned 0x{status:08X}")
    try:
        status = pdh.PdhAddEnglishCounterW(query, _PDH_GPU_COUNTER, None, ctypes.byref(counter)) & 0xFFFFFFFF
        if status in _PDH_ABSENT:
            return None
        if status:
            raise OSError(f"PdhAddEnglishCounterW returned 0x{status:08X}")
        status = pdh.PdhCollectQueryData(query) & 0xFFFFFFFF
        if status == _PDH_NO_DATA:
            return {}
        if status:
            raise OSError(f"PdhCollectQueryData returned 0x{status:08X}")
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        status = pdh.PdhGetFormattedCounterArrayW(counter, _PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), None) & 0xFFFFFFFF
        if status in (0, _PDH_NO_DATA) and size.value == 0:
            return {}
        if status != _PDH_MORE_DATA:
            raise OSError(f"PdhGetFormattedCounterArrayW returned 0x{status:08X}")
        buffer = (ctypes.c_byte * size.value)()
        items = ctypes.cast(buffer, ctypes.POINTER(_Item))
        status = pdh.PdhGetFormattedCounterArrayW(counter, _PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), items) & 0xFFFFFFFF
        if status:
            raise OSError(f"PdhGetFormattedCounterArrayW returned 0x{status:08X}")
        per_pid: Dict[int, float] = {}
        for i in range(count.value):
            item = items[i]
            if item.FmtValue.CStatus not in (0, 1):  # PDH_CSTATUS_VALID_DATA, _NEW_DATA
                continue
            match = _PDH_INSTANCE_PID.match(item.szName or "")
            if match:
                pid = int(match.group(1))
                per_pid[pid] = per_pid.get(pid, 0.0) + item.FmtValue.value.largeValue / _MB
        return per_pid
    finally:
        pdh.PdhCloseQuery(query)


_NVML_SUCCESS = 0
_NVML_ERROR_INSUFFICIENT_SIZE = 7
_NVML_VALUE_NOT_AVAILABLE = 0xFFFFFFFFFFFFFFFF
_nvml_libraries: Dict[str, Any] = {}


def _nvml(library: str) -> Any:
    """The initialised NVML library, or None where it does not load or start
    (no NVIDIA driver). Tried once per name."""
    if library in _nvml_libraries:
        return _nvml_libraries[library]
    lib = None
    try:
        import ctypes

        candidate = ctypes.CDLL(library)
        init = getattr(candidate, "nvmlInit_v2", None) or candidate.nvmlInit
        if init() == _NVML_SUCCESS:
            lib = candidate
    except (OSError, AttributeError):
        lib = None
    _nvml_libraries[library] = lib
    return lib


def read_vram_nvml(library: str = "libnvidia-ml.so.1") -> Optional[Dict[int, float]]:
    """Linux: MiB of GPU memory per pid for the compute processes NVML lists, through
    ctypes (no `nvidia-smi`). None when the library is absent or does not start."""
    import ctypes

    lib = _nvml(library)
    if lib is None:
        return None

    class _InfoV2(ctypes.Structure):  # nvmlProcessInfo_t of the _v2 and _v3 calls
        _fields_ = [("pid", ctypes.c_uint), ("usedGpuMemory", ctypes.c_ulonglong),
                    ("gpuInstanceId", ctypes.c_uint), ("computeInstanceId", ctypes.c_uint)]

    class _InfoV1(ctypes.Structure):
        _fields_ = [("pid", ctypes.c_uint), ("usedGpuMemory", ctypes.c_ulonglong)]

    for name, info in (("nvmlDeviceGetComputeRunningProcesses_v3", _InfoV2),
                       ("nvmlDeviceGetComputeRunningProcesses_v2", _InfoV2),
                       ("nvmlDeviceGetComputeRunningProcesses", _InfoV1)):
        listing = getattr(lib, name, None)
        if listing is not None:
            break
    else:
        raise OSError("NVML has no nvmlDeviceGetComputeRunningProcesses")
    count = ctypes.c_uint(0)
    get_count = getattr(lib, "nvmlDeviceGetCount_v2", None) or lib.nvmlDeviceGetCount
    status = get_count(ctypes.byref(count))
    if status != _NVML_SUCCESS:
        raise OSError(f"nvmlDeviceGetCount returned {status}")
    get_handle = getattr(lib, "nvmlDeviceGetHandleByIndex_v2", None) or lib.nvmlDeviceGetHandleByIndex
    per_pid: Dict[int, float] = {}
    for index in range(count.value):
        device = ctypes.c_void_p()
        status = get_handle(ctypes.c_uint(index), ctypes.byref(device))
        if status != _NVML_SUCCESS:
            raise OSError(f"nvmlDeviceGetHandleByIndex({index}) returned {status}")
        wanted = ctypes.c_uint(0)
        status = listing(device, ctypes.byref(wanted), None)
        if status == _NVML_SUCCESS or wanted.value == 0:
            continue
        if status != _NVML_ERROR_INSUFFICIENT_SIZE:
            raise OSError(f"{name} returned {status}")
        rows = (info * (wanted.value + 8))()
        have = ctypes.c_uint(len(rows))
        status = listing(device, ctypes.byref(have), rows)
        if status != _NVML_SUCCESS:
            raise OSError(f"{name} returned {status}")
        for row in rows[: have.value]:
            if row.usedGpuMemory != _NVML_VALUE_NOT_AVAILABLE:
                per_pid[int(row.pid)] = per_pid.get(int(row.pid), 0.0) + row.usedGpuMemory / _MB
    return per_pid


def _default_vram_reader(platform: str) -> Callable[[], Optional[Dict[int, float]]]:
    if platform == "win32":
        return read_vram_windows
    if platform.startswith("linux"):
        return read_vram_nvml
    return lambda: None  # macOS: no per-process VRAM reading


def read_exited_root_windows(handle: Any) -> Optional[TreeSample]:
    """The lifetime figures of a child through its `Popen`'s process handle, which
    keeps the process object readable after the child has exited (Windows only):
    peak working set, peak commit (`PeakPagefileUsage`), CPU and IO. Descendants
    that have already exited are not in it; nothing else can name them now."""
    import ctypes
    from ctypes import wintypes

    raw = getattr(handle, "_handle", None)
    if raw is None:
        return None

    class _Memory(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    class _Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process = wintypes.HANDLE(int(raw))
    memory = _Memory()
    memory.cb = ctypes.sizeof(memory)
    get_memory = kernel32.K32GetProcessMemoryInfo
    get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Memory), wintypes.DWORD]
    if not get_memory(process, ctypes.byref(memory), memory.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    times = [wintypes.FILETIME() for _ in range(4)]
    cpu: Optional[float] = None
    if kernel32.GetProcessTimes(process, *(ctypes.byref(t) for t in times)):
        def _seconds(ft: Any) -> float:
            return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7
        cpu = _seconds(times[2]) + _seconds(times[3])  # kernel + user
    io = _Io()
    read_bytes = write_bytes = None
    if kernel32.GetProcessIoCounters(process, ctypes.byref(io)):
        read_bytes, write_bytes = int(io.ReadTransferCount), int(io.WriteTransferCount)
    return TreeSample(
        root_create_time=None,
        processes=[ProcessSample(pid=-1, create_time=None, cpu_seconds=cpu,
                                 io_read_bytes=read_bytes, io_write_bytes=write_bytes)],
        root_peak_rss_mb=memory.PeakWorkingSetSize / _MB,
        root_peak_commit_mb=memory.PeakPagefileUsage / _MB,
    )


class ResourceSampler:
    """One daemon thread over the registry's open entries: what each field gets,
    per platform, is in the module docstring.

    It never spawns a process, never raises into the backend (a failed reading
    leaves its field None and is said once per kind of failure), never samples a
    pid unless `probe_identity` says it is still the recorded process, and never
    holds the registry lock while it walks: it copies what it needs, samples, and
    writes the results after checking each entry is still the same open entry."""

    def __init__(
        self,
        registry: ProcessRegistry,
        *,
        interval_s: float = SAMPLE_INTERVAL_S,
        early_samples_s: Sequence[float] = EARLY_SAMPLES_S,
        probe: Callable[[int, Optional[float]], str] = probe_identity,
        read_tree: Optional[Callable[[int], Optional[TreeSample]]] = None,
        read_vram: Optional[Callable[[], Optional[Dict[int, float]]]] = None,
        read_mtime: Optional[Callable[[Path], Optional[float]]] = None,
        read_exited_root: Optional[Callable[[Any], Optional[TreeSample]]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        platform: Optional[str] = None,
    ):
        self._registry = registry
        self.interval_s = interval_s
        self._early = tuple(early_samples_s)
        self._probe = probe
        self._platform = platform if platform is not None else sys.platform
        self._read_tree = read_tree or self._read_tree_psutil
        self._read_vram = read_vram or _default_vram_reader(self._platform)
        self._read_mtime = read_mtime or self._mtime_of
        if read_exited_root is not None:
            self._read_exited_root = read_exited_root
        elif self._platform == "win32":
            self._read_exited_root = read_exited_root_windows
        else:
            self._read_exited_root = lambda handle: None
        self._monotonic = monotonic
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._warned: set = set()
        self._last_write: Optional[float] = None
        self._psutil_checked = False

    # -- the thread ---------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="dpc-process-sampler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopped.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wake(self) -> None:
        self._wake.set()

    def early_schedule(self) -> List[float]:
        now = self._monotonic()
        return [now + offset for offset in self._early]

    def _run(self) -> None:
        next_full = self._monotonic()
        while not self._stopped.is_set():
            now = self._monotonic()
            try:
                if now >= next_full:
                    self.tick()
                    next_full = now + self.interval_s
                else:
                    due = self._due_early(now)
                    if due:
                        self.tick(due)
            except Exception as exc:  # the thread outlives any one bad tick
                self._failed("tick", exc)
            self._drop_early_due(now)  # done, or failed: either way not due again
            wait = min(next_full, self._next_early()) - self._monotonic()
            self._wake.wait(max(0.0, wait))
            self._wake.clear()

    def _due_early(self, now: float) -> List[int]:
        with self._registry._lock:
            return [pid for pid, live in self._registry._live.items()
                    if live.early_due and live.early_due[0] <= now]

    def _drop_early_due(self, now: float) -> None:
        with self._registry._lock:
            for live in self._registry._live.values():
                if live.early_due and live.early_due[0] <= now:
                    live.early_due = [due for due in live.early_due if due > now]

    def _next_early(self) -> float:
        with self._registry._lock:
            dues = [live.early_due[0] for live in self._registry._live.values() if live.early_due]
        return min(dues) if dues else float("inf")

    # -- one tick -------------------------------------------------------------------

    def tick(self, pids: Optional[Iterable[int]] = None) -> None:
        """Sample the open entries (or only `pids`) once. Never raises."""
        registry = self._registry
        only = set(pids) if pids is not None else None
        with registry._lock:
            targets = [
                (entry, registry._live[pid].log_paths)
                for pid, entry in registry._open.items()
                if pid in registry._live and (only is None or pid in only)
            ]
        if not targets:
            return
        self._check_psutil()
        vram: Optional[Dict[int, float]] = None
        if any(_is_engine(entry) for entry, _ in targets):
            vram = self._safely("vram", self._read_vram)
        results = []
        for entry, log_paths in targets:
            tree = None
            if self._identity_holds(entry.pid, entry.start_time):
                tree = self._safely("tree", lambda: self._read_tree(entry.pid))
                if tree is not None and not _same_start(tree.root_create_time, entry.start_time):
                    tree = None  # the pid changed hands between the probe and the walk
            vram_mib = None
            if tree is not None and vram is not None and _is_engine(entry):
                vram_mib = sum(vram.get(p.pid, 0.0) for p in tree.processes)
            mtime = self._safely("log_mtime", lambda: self._newest_mtime(log_paths)) if log_paths else None
            results.append((entry, tree, vram_mib, mtime))
        with registry._lock:
            for entry, tree, vram_mib, mtime in results:
                live = registry._live.get(entry.pid)
                if registry._open.get(entry.pid) is not entry or live is None:
                    continue  # ended (or replaced) while it was being read
                self._apply_locked(entry, live, tree, vram_mib, mtime)
            self._write_if_risen_locked(self._monotonic())

    def _identity_holds(self, pid: int, start_time: Optional[float]) -> bool:
        """The recorded process and only it: never a stranger that reused the pid."""
        return self._probe(pid, start_time) == "same"

    def read_at_end(self, pid: int) -> None:
        """Before `end()` closes `pid`: its own lifetime figures through the handle
        the registry holds (Windows), applied to the entry. Never raises."""
        registry = self._registry
        with registry._lock:
            entry = registry._open.get(pid)
            live = registry._live.get(pid)
            handle = live.handle if live is not None else None
        if entry is None or handle is None:
            return
        tree = self._safely("end_reading", lambda: self._read_exited_root(handle))
        if tree is None:
            return
        for sample in tree.processes:  # the root, keyed as the walk keys it
            sample.pid, sample.create_time = entry.pid, entry.start_time
        with registry._lock:
            if registry._open.get(pid) is entry and registry._live.get(pid) is live:
                self._apply_locked(entry, live, tree, None, None)

    def _apply_locked(
        self,
        entry: ProcessEntry,
        live: _Live,
        tree: Optional[TreeSample],
        vram_mib: Optional[float],
        mtime: Optional[float],
    ) -> None:
        if tree is not None:
            rss = [p.rss_mb for p in tree.processes if p.rss_mb is not None]
            if rss or tree.root_peak_rss_mb is not None:
                value = max(sum(rss), tree.root_peak_rss_mb or 0.0)
                entry.peak_rss_mb = _higher(entry.peak_rss_mb, round(value, 1))
            commit = [p.commit_mb for p in tree.processes if p.commit_mb is not None]
            if commit or tree.root_peak_commit_mb is not None:
                value = max(sum(commit), tree.root_peak_commit_mb or 0.0)
                entry.peak_commit_mb = _higher(entry.peak_commit_mb, round(value, 1))
            for p in tree.processes:
                key = (p.pid, _key_time(p.create_time))
                old_cpu, old_read, old_write = live.seen.get(key, (None, None, None))
                live.seen[key] = (_higher(old_cpu, p.cpu_seconds),
                                  _higher(old_read, p.io_read_bytes),
                                  _higher(old_write, p.io_write_bytes))
            cpu = [v[0] for v in live.seen.values() if v[0] is not None]
            if cpu:
                entry.cpu_seconds = round(sum(cpu), 3)
            reads = [v[1] for v in live.seen.values() if v[1] is not None]
            if reads:
                entry.io_read_bytes = int(sum(reads))
            writes = [v[2] for v in live.seen.values() if v[2] is not None]
            if writes:
                entry.io_write_bytes = int(sum(writes))
        if vram_mib is not None:
            entry.peak_vram_mib = _higher(entry.peak_vram_mib, round(vram_mib, 1))
        if mtime is not None:
            began = _epoch(entry.started_at)
            # A log reopened for append keeps the last run's mtime until the engine writes.
            if began is None or mtime >= began:
                at = _iso(mtime)
                if entry.last_output_at is None or at > entry.last_output_at:
                    entry.last_output_at = at

    def _write_if_risen_locked(self, now: float) -> None:
        """Rewrite the state file when a peak rose enough, at most once per
        `PEAK_WRITE_MIN_INTERVAL_S`; a rise inside the throttle is written by the
        first tick after it, since the base stays what the file holds."""
        registry = self._registry
        if registry.path is None:
            return
        if self._last_write is not None and now - self._last_write < PEAK_WRITE_MIN_INTERVAL_S:
            return
        for pid, entry in registry._open.items():
            live = registry._live.get(pid)
            if live is not None and _peak_rose(entry, live.written):
                registry._persist_locked()
                self._last_write = now
                return

    # -- the default readers ---------------------------------------------------------

    def _check_psutil(self) -> None:
        if self._psutil_checked:
            return
        self._psutil_checked = True
        try:
            import psutil  # noqa: F401
        except ImportError as exc:
            self._failed("psutil", exc, "psutil is not installed, so no process is sampled")

    def _read_tree_psutil(self, pid: int) -> Optional[TreeSample]:
        try:
            import psutil
        except ImportError:
            return None
        try:
            root = psutil.Process(pid)
            processes = [root] + root.children(recursive=True)
        except psutil.NoSuchProcess:
            return None
        samples: List[ProcessSample] = []
        root_create_time = root_peak_rss = root_peak_commit = None
        for index, proc in enumerate(processes):
            try:
                with proc.oneshot():
                    sample = ProcessSample(pid=proc.pid, create_time=proc.create_time())
                    info = self._read_one(proc, "memory", proc.memory_info)
                    if info is not None:
                        sample.rss_mb = info.rss / _MB
                        private = getattr(info, "private", None)  # Windows only
                        sample.commit_mb = private / _MB if private is not None else None
                        if index == 0:
                            peak = getattr(info, "peak_wset", None)
                            root_peak_rss = peak / _MB if peak is not None else None
                            peak = getattr(info, "peak_pagefile", None)
                            root_peak_commit = peak / _MB if peak is not None else None
                    times = self._read_one(proc, "cpu", proc.cpu_times)
                    if times is not None:
                        sample.cpu_seconds = times.user + times.system
                    if hasattr(proc, "io_counters"):  # absent on macOS
                        io = self._read_one(proc, "io", proc.io_counters)
                        if io is not None:
                            sample.io_read_bytes, sample.io_write_bytes = int(io.read_bytes), int(io.write_bytes)
            except psutil.NoSuchProcess:
                continue  # it ended between the listing and the reading; not an error
            if index == 0:
                root_create_time = sample.create_time
            samples.append(sample)
        if root_create_time is None:
            return None
        if self._platform.startswith("linux"):
            root_peak_rss = self._safely("root_peak", lambda: _vm_hwm_mb(pid))
        return TreeSample(root_create_time, samples, root_peak_rss, root_peak_commit)

    def _read_one(self, proc: Any, kind: str, read: Callable[[], Any]) -> Any:
        """One psutil reading; a process that ended propagates, anything else is
        said once and gives None."""
        import psutil

        try:
            return read()
        except psutil.NoSuchProcess:
            raise
        except Exception as exc:
            self._failed(kind, exc)
            return None

    def _newest_mtime(self, paths: Sequence[Path]) -> Optional[float]:
        times = [t for t in (self._read_mtime(p) for p in paths) if t is not None]
        return max(times) if times else None

    @staticmethod
    def _mtime_of(path: Path) -> Optional[float]:
        try:
            return os.stat(path).st_mtime
        except FileNotFoundError:
            return None

    # -- failures, said once -----------------------------------------------------------

    def _safely(self, kind: str, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except Exception as exc:
            self._failed(kind, exc)
            return None

    def _failed(self, kind: str, exc: BaseException, message: str = "") -> None:
        key = (kind, type(exc).__name__)
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(
            "process registry: the resource sampler could not read %s (%s: %s)%s; "
            "the field stays None where this fails, and this is said once",
            kind, type(exc).__name__, exc, f": {message}" if message else "",
        )


def _same_start(create_time: Optional[float], start_time: Optional[float]) -> bool:
    return (create_time is not None and start_time is not None
            and abs(create_time - start_time) <= START_TIME_TOLERANCE_S)


def _is_engine(entry: ProcessEntry) -> bool:
    return entry.launcher_kind == "subsystem" and entry.launcher in ENGINES


def _peak_rose(entry: ProcessEntry, written: Dict[str, Optional[float]]) -> bool:
    for name, floor in _PEAK_FLOORS:
        new = getattr(entry, name)
        if new is None:
            continue
        old = written.get(name) or 0.0
        if new - old >= floor and new >= old * (1.0 + PEAK_WRITE_RISE):
            return True
    return False


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


def start_resource_sampler() -> Optional[ResourceSampler]:
    """Start the resource sampler over this process's registry. Called once by
    `run_service.py`, after logging exists so that the sampler's one-time warnings
    reach the log file; `start_persistent()` stays a load and a sweep that starts
    no thread. Stopped by `finish_shutdown()`. Never raises."""
    try:
        return get_registry().start_sampler()
    except Exception as exc:
        logger.warning("process registry: could not start the resource sampler (%s: %s); "
                       "no peaks will be recorded", type(exc).__name__, exc)
        return None
