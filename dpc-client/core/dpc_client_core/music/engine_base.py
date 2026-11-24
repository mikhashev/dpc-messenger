"""What the DPC-supervised music engines share (ADR-051 D30, D35; SPEC §12 decisions 31, 40).

DPC runs two music engines as child servers: acestep.cpp (`music/acestep.py`) and yue2.cpp
(`music/yue2.py`). Both come from the same author and speak the same job protocol on
loopback: `GET /health` answers `{"status": "ok"}`, `GET /props` names the version and the
models, a job is a `POST` that answers `{"id": ...}`, `GET /job?id=N` polls it,
`GET /job?id=N&result=1` fetches the result, and `POST /job?id=N&cancel=1` cancels it.
`SupervisedEngine` holds everything that does not depend on which engine it is:

- **The child.** It gets a process group of its own and streams its output to a log file
  under `<DPC home>/logs/`; it is entered in the process registry at its birth (ADR-046),
  so the orphan sweep and the backend's stop see it; it is asked to stop first
  (CTRL_BREAK_EVENT on Windows, SIGTERM elsewhere) and killed when it has not gone after
  `STOP_GRACE_S`. `terminate()` is not used on Windows, for the reason
  `managers/llama_server_supervisor.py` gives (it loses the child's unflushed output).
- **States** (`EngineStatus`): `not_installed` (binary or a model file missing, named),
  `stopped`, `starting`, `running`, `error`. `error` is a start that did not reach a
  healthy server, or a server that exited on its own; it stays until a start succeeds.
  Nothing restarts the server by itself.
- **One instance.** A start of a running server returns at once: never a second child.
- **Idle stop.** After `idle_stop_s` without use the server is stopped to free the card;
  `in_use()` marks it busy for the length of a job.
- **The card lease** (ADR-052 slice S4, section 3.2). Each engine holds `CardLease("music:<KEY>")`,
  kind `music`, its declared peak `VRAM_ESTIMATE_MIB`, an `engine` yield cost, and
  `waits_for_engines` (Mike #70: an idle local LLM engine is swapped out by the owner-start rule,
  with the swap on). The lease first, then `_the_lock`: a start asks for the card before the lock
  and never waits for it (`wait_s=0`: refused at once naming the holder, `card_busy`; a start by hand
  alone lets idle holders with no queue go first, `_card_for_a_start(yields_idle=True)`); the runner's
  job has its grant already, from `in_use`, which is the job's one lease use and counts `_busy` in
  the same place (the lease's busy is the card's authority). `starting` while the child boots
  (busy: never asked to let go), `running` once it answers. The grant is let go however the child
  ends: `stop()` (`ui_stop`, `idle_unload`, `swap`), `_watch` (it exited on its own), a failed start
  and `shutdown()`; a child that ends under a job's open use lets go too (its memory is gone), and
  the job's next start asks again. A contender asks an idle engine to let go (`_yield_card`: a
  stop); a busy or booting one is waited for.
- **Identity** (PROVENANCE §2): files by name, size and sha256, cached by path, size and
  mtime in a JSON file under `<DPC home>/music/`, so a multi-GB model is hashed once.
  Records carry file names only, never local paths (PROVENANCE rule 3).

The host is always 127.0.0.1 (decision 31: loopback only). A subclass names its engine
(`KEY`, `ENGINE`, `DISPLAY`, `NAME`, `PROCESS_LAUNCHER`, `BINARY_NAME`, `LOG_NAME`,
`IDENTITY_CACHE`, `PORT_SETTING`) and supplies `build_command`, `installed_problem`,
`_check_props`, `status_models` and `identity`.

**Threading.** Every coroutine runs on the service loop. `status()` is synchronous and
cheap (a few `stat` calls), for the capability probe. `identity()` hashes files: call it
through `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from .. import process_registry as _procreg
from ..managers.card_lease import CardLease, LeaseRefusal
from .capabilities import EngineStatus
from .store import atomic_write_text, music_dir_of

logger = logging.getLogger(__name__)

# Its own process group, so CTRL_BREAK_EVENT reaches only the child (as the llama-server
# supervisor does it).
CREATION_NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

LOOPBACK = "127.0.0.1"
START_TIMEOUT_S = 60.0
STOP_GRACE_S = 15.0
LOG_ROTATE_BYTES = 10 * 1024 * 1024
LOG_KEEP = 3
_HASH_CHUNK = 4 * 1024 * 1024
BUILD_SEARCH_DEPTH = 3
BUILD_SEARCH_SKIP = {"CMakeFiles", "vendor", "tests", ".cmake", "_deps"}


# What a refusal naming a music engine as the card's holder tells the person (ADR-052 section 2.2
# item 7).
_STOP_HINT = "It lets go when idle; Stop stops it now, or cancel its job in the music player."


class MusicEngineError(RuntimeError):
    """A refusal or failure of a music engine; `code` is a short machine word."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# -- settings helpers ------------------------------------------------------------------


def exe_name(stem: str) -> str:
    """The binary's file name on this platform: `stem.exe` on Windows, `stem` elsewhere."""
    return f"{stem}.exe" if sys.platform == "win32" else stem


def find_binary_in(build_dir: Path, name: str) -> Optional[Path]:
    """`name` at the top of `build_dir`, else the first one a walk of at most
    `BUILD_SEARCH_DEPTH` levels finds (shallowest first), else None. Multi-config
    generators put binaries in `bin/` or `Release/`."""
    top = build_dir / name
    if top.is_file():
        return top
    if not build_dir.is_dir():
        return None
    level = [build_dir]
    for _ in range(BUILD_SEARCH_DEPTH):
        nxt: List[Path] = []
        for folder in level:
            try:
                children = sorted(folder.iterdir())
            except OSError:
                continue
            for child in children:
                if child.name == name and child.is_file():
                    return child
                if child.is_dir() and child.name not in BUILD_SEARCH_SKIP and not child.name.startswith("."):
                    nxt.append(child)
        level = nxt
    return None


class SettingsReader:
    """`[music]` values through `get(section, key, fallback)` (a `Settings.get`), each
    replaced by its default (and logged) when it does not parse."""

    def __init__(self, get: Callable[[str, str, str], Any]):
        self._get = get

    def text(self, key: str) -> str:
        raw = self._get("music", key, "")
        return str(raw).strip() if raw is not None else ""

    def path(self, key: str) -> Optional[Path]:
        raw = self.text(key)
        return Path(os.path.expanduser(raw)) if raw else None

    def port(self, key: str, default: int) -> int:
        raw = self.text(key)
        if not raw:
            return default
        try:
            port = int(raw)
            if not 0 <= port <= 65535:
                raise ValueError(raw)
            return port
        except ValueError:
            logger.warning("[music] %s %r is not a port; using %d", key, raw, default)
            return default

    def seconds(self, key: str, default: float) -> float:
        raw = self.text(key)
        if not raw:
            return default
        try:
            value = float(raw)
            if not value >= 0:
                raise ValueError(raw)
            return value
        except ValueError:
            logger.warning("[music] %s %r is not a number of seconds; using %s", key, raw, default)
            return default

    def whole(self, key: str, default: int) -> int:
        raw = self.text(key)
        if not raw:
            return default
        try:
            value = int(raw)
            if value < 0:
                raise ValueError(raw)
            return value
        except ValueError:
            logger.warning("[music] %s %r is not a whole number >= 0; using %d", key, raw, default)
            return default


# -- small helpers -----------------------------------------------------------------


def port_free(port: int) -> bool:
    """Whether 127.0.0.1:`port` can be bound now. SO_REUSEADDR on POSIX, as the servers set
    it, so a port in TIME_WAIT after our own last run is not reported taken; never on
    Windows, where it would let a second listener share the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((LOOPBACK, port))
        except OSError:
            return False
    return True


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOOPBACK, 0))
        return s.getsockname()[1]


def rotate_log(path: Path) -> None:
    """Keep `LOG_KEEP` older logs: `x.log.1` is the newest of them."""
    try:
        if not path.is_file() or path.stat().st_size < LOG_ROTATE_BYTES:
            return
        for i in range(LOG_KEEP, 0, -1):
            older = path.with_name(f"{path.name}.{i}")
            newer = path.with_name(f"{path.name}.{i - 1}") if i > 1 else path
            if newer.exists():
                os.replace(newer, older)
    except OSError:
        logger.warning("music engine: could not rotate %s", path, exc_info=True)


class HashCache:
    """sha256 of files, kept by path with size and mtime, in one JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: Optional[Dict[str, Dict[str, Any]]] = None

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self._data is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def lookup(self, path: Path, size: int, mtime_ns: int) -> Optional[str]:
        with self._lock:
            rec = self._load().get(str(path))
            if isinstance(rec, dict) and rec.get("size") == size and rec.get("mtime_ns") == mtime_ns:
                sha = rec.get("sha256")
                return sha if isinstance(sha, str) else None
            return None

    def store(self, path: Path, size: int, mtime_ns: int, sha: str) -> None:
        with self._lock:
            self._load()[str(path)] = {"size": size, "mtime_ns": mtime_ns, "sha256": sha}
            try:
                atomic_write_text(self.path, json.dumps(self._data, indent=1, sort_keys=True))
            except OSError:
                logger.warning("music engine: could not write the identity cache %s", self.path, exc_info=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return "sha256:" + h.hexdigest()


# -- the supervisor ----------------------------------------------------------------


class SupervisedEngine:
    """A DPC-owned engine server child. One instance per engine per backend."""

    KEY = "engine"                # the job-param word: "yue2", "acestep"
    ENGINE = "engine"             # the engine id in records: "yue2.cpp", "acestep.cpp"
    DISPLAY = "the engine"        # in messages
    NAME = "the engine"           # the long name in the capability report
    PROCESS_LAUNCHER = "engine"
    BINARY_NAME = "server"
    LOG_NAME = "engine.log"
    IDENTITY_CACHE = "engine_identity_cache.json"
    PORT_SETTING = "port"
    # The engine's declared peak on the card in MiB (ADR-052 L11: a music holder's peak is its declared
    # `vram_mib`), None for unknown; each engine carries its trial's figure.
    VRAM_ESTIMATE_MIB: Optional[int] = None

    def __init__(self, config: Any, *, dpc_home: Path,
                 config_source: Optional[Callable[[], Any]] = None,
                 on_change: Optional[Callable[[EngineStatus], None]] = None):
        self.config = config
        self.dpc_home = Path(dpc_home)
        self._config_source = config_source
        self.on_change = on_change
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._log_fh = None
        self._port: Optional[int] = None
        self._state = "stopped"
        self._reason: Optional[str] = None
        self._last_exit: Optional[Dict[str, Any]] = None
        self._props: Optional[Dict[str, Any]] = None
        self._version: Optional[str] = None
        self._lock: Optional[asyncio.Lock] = None
        self._stopping = False
        self._busy = 0
        self._last_used = time.monotonic()
        self._watch_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._hashes = HashCache(music_dir_of(self.dpc_home) / self.IDENTITY_CACHE)
        # One standing on the card per engine (ADR-052 section 3.2): a job, a start by hand and a
        # contender's yield all go through it.
        self.card_lease = CardLease(
            f"music:{self.KEY}", kind="music", engine=self.ENGINE, display=self.DISPLAY,
            vram_mib=self.VRAM_ESTIMATE_MIB, yield_cost="engine", on_yield=self._yield_card,
            waits_for_engines=True, stop_hint=_STOP_HINT,
        )

    # -- what a subclass supplies ----------------------------------------------------

    def build_command(self, port: int) -> List[str]:
        raise NotImplementedError

    def installed_problem(self) -> Optional[str]:
        """Why the engine cannot be started, naming what is missing; None when it can."""
        raise NotImplementedError

    def _check_props(self, props: Any) -> None:
        """Raise `MusicEngineError("start_failed", ...)` when `/props` shows the wrong models."""
        if not isinstance(props, dict):
            raise MusicEngineError("start_failed", "/props is not a JSON object")

    def status_models(self) -> Dict[str, str]:
        """The model file names by role, for the status."""
        return {}

    def server_args(self) -> Dict[str, Any]:
        return {}

    def _read_version_file(self) -> Optional[str]:
        return None

    def identity(self, *, model_sha256: bool = True) -> Dict[str, Any]:
        raise NotImplementedError

    # -- configuration -------------------------------------------------------------

    def reload_config(self) -> None:
        """Re-read the settings (before a start); a running child keeps its own."""
        if self._config_source is not None and not self.alive():
            try:
                self.config = self._config_source()
            except Exception:
                logger.exception("%s: reading the [music] settings failed; keeping the last ones", self.DISPLAY)

    @property
    def log_path(self) -> Path:
        return self.config.log_path or (self.dpc_home / "logs" / self.LOG_NAME)

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def base_url(self) -> str:
        if self._port is None:
            raise MusicEngineError("not_running", f"{self.DISPLAY} is not running")
        return f"http://{LOOPBACK}:{self._port}"

    # -- status --------------------------------------------------------------------

    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def state(self) -> str:
        return self._state

    @property
    def busy(self) -> bool:
        """A job holds the server (`in_use`)."""
        return self._busy > 0

    @property
    def version(self) -> Optional[str]:
        if self._version:
            return self._version
        return self._read_version_file()

    def _status(self, state: str, reason: str) -> EngineStatus:
        return EngineStatus(state=state, reason=reason, engine_id=self.ENGINE, name=self.NAME, key=self.KEY,
                            supervised=True, version=self.version, models=self.status_models())

    def status(self) -> EngineStatus:
        """The capability probe's answer. Synchronous and cheap."""
        if not self.alive():
            self.reload_config()
        if self._state in ("starting", "running"):
            where = f"{LOOPBACK}:{self._port}" if self._port else LOOPBACK
            pid = getattr(self._proc, "pid", None)
            word = "starting" if self._state == "starting" else "running"
            return self._status(self._state, f"{word} on {where} (pid {pid}), started by DPC")
        problem = self.installed_problem()
        if problem:
            return self._status("not_installed", problem)
        if self._state == "error":
            return self._status("error", self._reason or "the engine failed")
        reason = "installed, not running; DPC starts it when a job runs"
        if self._reason:
            reason += f" (last: {self._reason})"
        return self._status("stopped", reason)

    def details(self) -> Dict[str, Any]:
        """For `get_music_engine_status`: the status and where things are on this machine."""
        status = self.status()
        return {
            **status.as_dict(),
            "pid": getattr(self._proc, "pid", None) if self.alive() else None,
            "host": LOOPBACK,
            "port": self._port if self.alive() else (self.config.port or None),
            "binary": str(self.config.binary),
            "models_dir": str(self.config.models_dir),
            "log_path": str(self.log_path),
            "idle_stop_s": self.config.idle_stop_s,
            "busy": self.busy,
            "last_exit": dict(self._last_exit) if self._last_exit else None,
            "server_args": self.server_args(),
        }

    def _set_state(self, state: str, reason: Optional[str] = None) -> None:
        changed = (state, reason) != (self._state, self._reason)
        self._state, self._reason = state, reason
        if changed and self.on_change is not None:
            try:
                self.on_change(self.status())
            except Exception:
                logger.exception("%s: the state listener failed", self.DISPLAY)

    def tail_log(self, max_bytes: int = 2000) -> str:
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                return f.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def with_log_tail(self, message: str) -> str:
        tail = self.tail_log(600)
        last = [ln for ln in tail.splitlines() if ln.strip()][-3:]
        return f"{message}; log {self.log_path.name}: {' | '.join(last)}" if last else message

    # -- lifecycle -----------------------------------------------------------------

    def _the_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _running_as_wanted(self) -> bool:
        """A running child that needs no restart (a subclass adds its own boot options)."""
        return self.alive() and self._state == "running"

    async def start(self) -> None:
        """Start the server and wait until it answers `/health` and `/props`, or raise
        `MusicEngineError` (`card_busy`, `not_installed`, `port_taken`, `backend_stopping`,
        `start_failed`). A running server is left as it is: never a second child.

        The card first (ADR-052 section 3.2, Ark #44): without a grant the start asks for one at
        once, before `_the_lock`, and never waits for it (`card_busy` names the holder); a job's
        start has its grant already. Nothing is acquired under the lock."""
        await self._card_for_a_start()
        await self._start_child()

    async def _card_for_a_start(self, what: Optional[str] = None, *, yields_idle: bool = False) -> None:
        """The grant a start needs, asked for with `wait_s=0` when this engine holds none: granted at
        once on a free card, else `MusicEngineError("card_busy")` naming what holds it. With
        `yields_idle` (a start by hand) idle holders with no queue behind them are let go of first,
        on the terms a queued job gets, and waited for (`CardLease.acquire`). Never under a client
        lock: callers run it before `_the_lock` (yue2.cpp before its own start lock too)."""
        if self.card_lease.granted:
            return
        try:
            await self.card_lease.acquire(what or f"start of {self.DISPLAY}", wait_s=0, yields_idle=yields_idle)
        except LeaseRefusal as exc:
            raise MusicEngineError("card_busy", f"{self.DISPLAY} cannot start now: {exc}") from None

    def _let_go_of_the_card(self, why: str) -> None:
        """The child is gone, or never came: nothing of it is on the card (ADR-052 L8)."""
        self.card_lease.release(why)

    async def _start_child(self) -> None:
        """The start itself, under `_the_lock`, on a grant this engine already holds. A start that
        ends with no child alive lets the card go. A failed start never leaves its child counted as
        alive, a second cancel during its stop included (`_stop_proc`, Zcode #87-4), so the release
        below runs on every failure that spawned a child."""
        async with self._the_lock():
            if self._running_as_wanted():
                return
            if not self.card_lease.granted:
                # Let go between the card step and the lock (a person's Stop): never load without it.
                raise MusicEngineError("card_busy", f"{self.DISPLAY} lost the card before its start; "
                                                    "start it again")
            try:
                await self._start_locked()
            except BaseException:
                if not self.alive():
                    self._let_go_of_the_card("failed_start")
                raise

    async def _start_locked(self) -> None:
        self.reload_config()
        problem = self.installed_problem()
        if problem:
            raise MusicEngineError("not_installed", problem)
        port = self.config.port or free_port()
        if not port_free(port):
            reason = (f"{LOOPBACK}:{port} is taken by another process (a {self.BINARY_NAME} left from an "
                      f"earlier run, or another program); free it or set [music] {self.PORT_SETTING}")
            self._set_state("error", reason)
            raise MusicEngineError("port_taken", reason)
        try:
            _procreg.get_registry().check_open()
        except _procreg.ProcessRegistryClosed as exc:
            raise MusicEngineError("backend_stopping", str(exc)) from None
        self._port = port
        cmd = self.build_command(port)
        self._open_log(cmd)
        # Busy for the card while it boots (L1): no contender marks a booting engine.
        self.card_lease.mark_starting()
        self._set_state("starting")
        try:
            proc = await self._spawn(cmd)
        except OSError as exc:
            self._close_log()
            reason = f"{self.DISPLAY} did not start: {exc}"
            self._set_state("error", reason)
            raise MusicEngineError("start_failed", reason) from exc
        self._proc = proc
        try:
            await self._wait_healthy(proc, port)
            props = await self._get_json("/props")
            self._answered_by(proc, "/props")
            self._check_props(props)
        except BaseException as exc:
            await self._stop_proc(proc, "failed_start")
            message = exc.message if isinstance(exc, MusicEngineError) else f"{type(exc).__name__}: {exc}"
            reason = self.with_log_tail(f"{self.DISPLAY} did not start: {message}")
            self._set_state("error", reason)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MusicEngineError("start_failed", reason) from exc
        self._props = props
        self._version = props.get("version") if isinstance(props.get("version"), str) else self._version
        self._last_used = time.monotonic()
        self.card_lease.mark_running()
        self._set_state("running")
        logger.info("%s: running on %s:%s (pid %s, %s)", self.DISPLAY, LOOPBACK, port, proc.pid, self._version)
        self._watch_task = asyncio.ensure_future(self._watch(proc))
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.ensure_future(self._idle_loop())

    async def _spawn(self, cmd: List[str]) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=self._log_fh,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.config.binary.parent),
            creationflags=CREATION_NEW_PROCESS_GROUP,
        )
        self._register_child(proc, cmd)
        return proc

    def _register_child(self, proc: Any, cmd: List[str]) -> None:
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int):
            return
        try:
            _procreg.get_registry().register(
                pid, launcher=self.PROCESS_LAUNCHER, launcher_kind="subsystem", command=cmd, log=logger,
                detail=f"port {self._port}", log_paths=[self.log_path])
        except _procreg.ProcessRegistryClosed as exc:
            try:
                proc.kill()
            except Exception:
                pass
            raise OSError(f"the backend is stopping: {exc}") from None

    def _open_log(self, cmd: List[str]) -> None:
        path = self.log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_log(path)
        self._log_fh = open(path, "ab")
        self._log_fh.write(f"\n--- {now_text()} DPC starts: {' '.join(cmd)}\n".encode("utf-8"))
        self._log_fh.flush()

    def _close_log(self) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    def _answered_by(self, proc: Any, path: str) -> None:
        """Refuse an answer the child cannot have given: it is dead, so whatever holds the
        port answered (as `LlamaServerSupervisor._answered_by`)."""
        if self._proc is proc and proc.returncode is None:
            return
        raise MusicEngineError("start_failed", f"the child exited with {proc.returncode} by the time {path} "
                                               f"answered on :{self._port}, so another process answered it")

    async def _wait_healthy(self, proc: Any, port: int) -> None:
        deadline = time.monotonic() + float(self.config.start_timeout_s)
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                raise MusicEngineError("start_failed", f"exited with {proc.returncode} before answering /health")
            if await self._health_ok(port):
                self._answered_by(proc, "/health")
                return
            await asyncio.sleep(0.25)
        raise MusicEngineError("start_failed", f"no healthy /health within {self.config.start_timeout_s:g} s")

    async def _health_ok(self, port: int) -> bool:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
                resp = await client.get(f"http://{LOOPBACK}:{port}/health")
            return resp.status_code == 200 and (resp.json() or {}).get("status") == "ok"
        except Exception:
            return False

    async def _watch(self, proc: Any) -> None:
        """Notice a child that exits on its own: an `error`, recorded; nothing restarts it."""
        try:
            code = await proc.wait()
        except asyncio.CancelledError:
            return
        if self._proc is not proc or self._stopping:
            return
        self._proc = None
        self._end_child(proc)
        self._close_log()
        # Its memory is gone, under a job's open use too: the job's next start asks again (L8).
        self._let_go_of_the_card("exited on its own")
        self._last_exit = {"code": code, "at": now_text(), "why": "exited on its own"}
        reason = self.with_log_tail(f"{self.DISPLAY} exited on its own with code {code}")
        logger.warning("%s: %s", self.DISPLAY, reason)
        self._set_state("error", reason)

    def _end_child(self, proc: Any) -> None:
        pid, code = getattr(proc, "pid", None), getattr(proc, "returncode", None)
        if isinstance(pid, int) and code is not None:
            _procreg.get_registry().end(pid, code)

    async def stop(self, why: str = "ui_stop", *, keep_the_card: bool = False) -> bool:
        """Ask the child to stop, then kill it after `STOP_GRACE_S`. True when a running
        child was stopped. `why` is the registry's stop word (`ui_stop`, `idle_unload`,
        `swap` when another client needs the card). The card is let go with the child
        (ADR-052 L8), unless `keep_the_card`: a restart for other boot options (yue2.cpp) is
        one start, and the card stays with the engine across it."""
        async with self._the_lock():
            proc = self._proc
            if proc is None or proc.returncode is not None:
                if proc is not None:
                    self._proc = None
                    self._end_child(proc)
                    self._close_log()
                if not keep_the_card:
                    self._let_go_of_the_card(why)
                return False
            self._stopping = True
            try:
                await self._stop_proc(proc, why)
            finally:
                self._stopping = False
                if not keep_the_card and not self.alive():
                    self._let_go_of_the_card(why)
            self._last_exit = {"code": proc.returncode, "at": now_text(), "why": why}
            self._set_state("stopped", f"stopped by DPC ({why}) at {self._last_exit['at']}")
            logger.info("%s: stopped (%s, rc=%s)", self.DISPLAY, why, proc.returncode)
            return True

    async def _stop_proc(self, proc: Any, why: str) -> None:
        """Signal `proc`, wait `STOP_GRACE_S`, then kill it and wait as long again. However this
        ends (a second cancel while it waits, a failed wait), the child is killed if it has not exited
        and `self._proc` lets go of it, so every caller's `not self.alive()` holds and its release of
        the card runs (ADR-052 L8; Zcode #87-4); the exception goes on. A child still there after the
        kill and the second wait is logged and no longer counted: holding the card for it forever
        would stop every other client."""
        pid = getattr(proc, "pid", None)
        try:
            if proc.returncode is None:
                if isinstance(pid, int):
                    _procreg.get_registry().note_stop_sent(pid, "stopped", why)
                try:
                    try:
                        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
                    except ProcessLookupError:
                        raise
                    except OSError as exc:
                        logger.warning("%s: could not signal pid %s to stop (%s); killing it", self.DISPLAY, pid, exc)
                        self._kill(proc)
                    await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE_S)
                except (ProcessLookupError, asyncio.TimeoutError):
                    self._kill(proc)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE_S)
                    except asyncio.TimeoutError:
                        logger.error("%s: pid %s has not exited %.0f s after it was killed; DPC no longer "
                                     "counts it as its child", self.DISPLAY, pid, STOP_GRACE_S)
        except BaseException:
            if proc.returncode is None:
                self._kill(proc)
            raise
        finally:
            if self._proc is proc:
                self._proc = None
            self._end_child(proc)
            self._close_log()

    @staticmethod
    def _kill(proc: Any) -> None:
        """Kill `proc`; never raises. A child already gone is `ProcessLookupError`; any other `OSError`
        (access denied, as Windows answers for a process that is exiting) is logged, so the stop that
        called this goes on to let the card go (Zcode #87-4)."""
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.warning("music engine: could not kill pid %s (%s)", getattr(proc, "pid", None), exc)

    async def shutdown(self) -> None:
        """The backend's stop: stop the child and the idle timer, and let the card go however the stop
        ended (Zcode #87-4). A stop that failed, or was cancelled before it could run (waiting for
        `_the_lock`), leaves the child killed and the card let go. Never raises, except a
        cancellation, which goes on after the release."""
        task, self._idle_task = self._idle_task, None
        if task is not None:
            task.cancel()
        try:
            await self.stop("ui_stop")
        except Exception:
            logger.exception("%s: the server did not stop cleanly", self.DISPLAY)
        finally:
            watch, self._watch_task = self._watch_task, None
            if watch is not None and not watch.done():
                watch.cancel()
            proc = self._proc
            if proc is not None and proc.returncode is None:
                logger.warning("%s: the stop did not finish; killing pid %s", self.DISPLAY, getattr(proc, "pid", None))
                self._kill(proc)
            self._let_go_of_the_card("ui_stop")

    # -- use and idle stop ----------------------------------------------------------

    @contextlib.asynccontextmanager
    async def in_use(self, what: str = "a job", **acquire_kw: Any) -> AsyncIterator[None]:
        """Hold the card for a job and mark the server busy, so the idle stop and the other
        engine wait: one lease use (`card_lease.use`, ADR-052 L1), waiting in the lease line as
        the arguments say (`wait_s` None by default: until `check` raises or the caller is
        cancelled), and `_busy` counted in the same place. The runner takes it before
        `start_job`; one use per job, so no second use of the job waits behind a contender that
        waits for the first (L5)."""
        acquire_kw.setdefault("wait_s", None)
        async with self.card_lease.use(what, **acquire_kw):
            self._busy += 1
            try:
                yield
            finally:
                self._busy -= 1
                self._last_used = time.monotonic()

    async def _yield_card(self, why: str) -> str:
        """The lease's `on_yield` (ADR-052 L6, section 3.2): a child that is not alive has nothing
        to give; a start or a stop under way (`_the_lock` held) or a job (busy) refuses without
        calling `stop()`; else the idle child is stopped (`swap`), which lets the card go."""
        if not self.alive():
            return "nothing_to_give"
        if self._the_lock().locked() or self.busy:
            return "refused"
        await self.stop("swap")
        return "could_not_let_go" if self.alive() else "let_go"

    async def _idle_loop(self) -> None:
        while True:
            idle_s = float(self.config.idle_stop_s)
            await asyncio.sleep(max(0.05, min(5.0, idle_s / 4 if idle_s else 0.05)))
            if self._state != "running" or not self.alive():
                if not self.alive() and self._state != "starting":
                    return
                continue
            if self._busy:
                continue
            if time.monotonic() - self._last_used >= idle_s:
                logger.info("%s: idle for %.0f s; stopping it to free the card", self.DISPLAY, idle_s)
                await self.stop("idle_unload")
                return

    # -- HTTP ----------------------------------------------------------------------

    def _client(self, timeout: float):
        import httpx
        return httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def _get_json(self, path: str) -> Any:
        async with self._client(10.0) as client:
            resp = await client.get(self.base_url + path)
            resp.raise_for_status()
            return resp.json()

    def _unreachable(self, exc: Exception, what: str) -> MusicEngineError:
        state = "" if self.alive() else f" ({self._reason or 'the server is not running'})"
        return MusicEngineError("engine_unreachable", f"{self.DISPLAY} did not answer {what}: {exc}{state}")

    async def submit(self, path: str, body: bytes) -> str:
        """POST a job body (`/lm`, `/synth`); the engine's job id."""
        import httpx
        self._last_used = time.monotonic()
        try:
            async with self._client(30.0) as client:
                resp = await client.post(self.base_url + path, content=body,
                                         headers={"Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            raise self._unreachable(exc, f"POST {path}") from exc
        if resp.status_code != 200:
            raise MusicEngineError("engine_refused", f"POST {path} answered {resp.status_code}: {resp.text[:300]}")
        try:
            job_id = resp.json()["id"]
        except (ValueError, KeyError, TypeError):
            raise MusicEngineError("engine_refused", f"POST {path} answered without a job id: {resp.text[:200]}")
        return str(job_id)

    async def job_status(self, engine_job_id: str) -> str:
        """`running`, `done`, `failed` or `cancelled`."""
        import httpx
        self._last_used = time.monotonic()
        try:
            async with self._client(10.0) as client:
                resp = await client.get(self.base_url + "/job", params={"id": engine_job_id})
        except httpx.HTTPError as exc:
            raise self._unreachable(exc, "GET /job") from exc
        if resp.status_code != 200:
            raise MusicEngineError("engine_refused", f"GET /job?id={engine_job_id} answered {resp.status_code}: "
                                                     f"{resp.text[:200]}")
        try:
            return str(resp.json()["status"])
        except (ValueError, KeyError, TypeError):
            raise MusicEngineError("engine_refused", f"GET /job answered no status: {resp.text[:200]}")

    async def fetch_result(self, engine_job_id: str, dest: Optional[Path] = None) -> Tuple[str, Optional[bytes]]:
        """The result of a done job: `(content type, body)`, or `(content type, None)` with
        the body streamed into `dest` (a synth result can be hundreds of MB)."""
        import httpx
        self._last_used = time.monotonic()
        try:
            async with self._client(600.0) as client:
                async with client.stream("GET", self.base_url + "/job",
                                         params={"id": engine_job_id, "result": "1"}) as resp:
                    if resp.status_code != 200:
                        text = (await resp.aread())[:300].decode("utf-8", errors="replace")
                        raise MusicEngineError("engine_refused",
                                               f"GET /job result answered {resp.status_code}: {text}")
                    ctype = resp.headers.get("content-type", "")
                    if dest is None:
                        return ctype, await resp.aread()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest, "wb") as f:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            f.write(chunk)
                    return ctype, None
        except httpx.HTTPError as exc:
            raise self._unreachable(exc, "GET /job result") from exc

    async def cancel(self, engine_job_id: str) -> None:
        """Ask the engine to cancel a job (`POST /job?id=N&cancel=1`); never raises."""
        try:
            async with self._client(10.0) as client:
                await client.post(self.base_url + "/job", params={"id": engine_job_id, "cancel": "1"})
        except Exception as exc:
            logger.info("%s: cancelling engine job %s failed: %s", self.DISPLAY, engine_job_id, exc)

    # -- identity (blocking) ---------------------------------------------------------

    def _file_identity(self, path: Path, *, with_sha: bool) -> Dict[str, Any]:
        try:
            st = path.stat()
        except OSError:
            return {"file": path.name, "bytes": None, "sha256": None}
        sha = self._hashes.lookup(path, st.st_size, st.st_mtime_ns)
        if sha is None and with_sha:
            sha = sha256_file(path)
            self._hashes.store(path, st.st_size, st.st_mtime_ns, sha)
        return {"file": path.name, "bytes": st.st_size, "sha256": sha}

    def _engine_libraries(self) -> List[Path]:
        folder = self.config.binary.parent
        try:
            names = sorted(p for p in folder.iterdir() if p.is_file())
        except OSError:
            return []
        out = []
        for p in names:
            n = p.name.lower()
            if (n.startswith(("ggml", "libggml")) and (n.endswith((".dll", ".dylib")) or ".so" in n)):
                out.append(p)
        return out

    def _binaries_identity(self) -> List[Dict[str, Any]]:
        binaries = [self._file_identity(self.config.binary, with_sha=True)]
        binaries += [self._file_identity(p, with_sha=True) for p in self._engine_libraries()]
        return binaries
