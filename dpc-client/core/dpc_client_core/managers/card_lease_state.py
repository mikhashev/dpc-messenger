"""The card lease's queue, written for CC and pushed to the Agent map.

`card_lease.queue_snapshot()` is the one plain dict of who holds the card and who waits. This module
keeps two copies of it fresh:

- a file, `<dpc home>/state/card_lease.json`, written atomically (a temp file in the same folder,
  then `os.replace`) so CC's bridge (`cc_group_chat_bridge.py --card`) reads it without asking the
  backend. `updated_at` and `backend_pid` are in it, so a file left behind by a crash is
  recognisable: its pid is not a running backend. A stopping backend writes it once more with
  `backend_stopped: true`.
- the `card_lease_updated` event to the UI, the same snapshot, for the Agent map's Card block.

Both follow the lease's own changes (`card_lease.add_change_listener`), debounced: a change is
written at most every `DEBOUNCE_S`, the write itself goes to one worker thread (in order, never on
the owner loop), and a snapshot whose content did not change is neither written nor pushed. The
supervisors' holders and card-queue places change without telling the lease module, so a light poll
(`POLL_S`) reads the snapshot too; a heartbeat rewrites the file every `HEARTBEAT_S` even when nothing
changed, which refreshes `updated_at` and the numbers that move by themselves (a holder's ETA).

Nothing here is started until the service starts it (`CardLeaseCommandsMixin`), so a test or a script
that imports the lease never writes a file. Pure Python, the same on Windows, Linux and macOS: the
write retries a few times when Windows refuses the replace because a reader has the file open.
"""

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from . import card_lease

logger = logging.getLogger(__name__)

DEBOUNCE_S = 0.5
POLL_S = 2.0
HEARTBEAT_S = 15.0
EVENT_NAME = "card_lease_updated"
FILE_NAME = "card_lease.json"

# How often the replace is tried when the platform refuses it (Windows: a reader holds the file).
_REPLACE_TRIES = 5
# The queue file is read by this user's processes only (POSIX mode bits; ignored on Windows).
STATE_FILE_MODE = 0o600


def _fsync_directory(folder: Path) -> None:
    """fsync `folder` after a rename into it, on POSIX (Windows cannot open a directory for
    it, and NTFS journals the rename). Best effort: a failure is logged, never raised."""
    if os.name == "nt":
        return
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError as exc:
        logger.debug("card: the folder %s could not be opened to fsync: %s", folder, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("card: the folder %s could not be fsynced: %s", folder, exc)
    finally:
        os.close(fd)


def state_file_path(dpc_home: Path) -> Path:
    """`<dpc home>/state/card_lease.json`: the home the service uses (`settings.dpc_home_dir`)."""
    return Path(dpc_home) / "state" / FILE_NAME


def write_atomically(path: Path, snapshot: Dict[str, Any]) -> bool:
    """Write `snapshot` as JSON to `path` so that a reader sees the old file or the new one, never
    half of one. False (and a log line) when it could not be written; never raises.

    The file is the owner's alone (follow-up 14, Zcode #176): created 0600 on Linux and macOS
    (Windows has no such mode bits; the file lives in the user's profile), and after the
    replace the directory is fsynced there too, so the new name survives a power cut."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
                     STATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                _fsync_directory(path.parent)
                return True
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except Exception as exc:
        logger.warning("card: the queue file %s could not be written: %s", path, exc)
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def content_key(snapshot: Dict[str, Any]) -> str:
    """What makes two snapshots the same news: who holds, who waits and for what, the record's length.
    Times and estimates move by themselves; the heartbeat carries them."""
    holders = [(h["client"], h["state"], h["busy"], h["purpose"], h["agent_id"], h["conversation_id"],
                h["beside"]) for h in snapshot.get("holders", [])]
    waiters = [(w["client"], w["purpose"], w["where"], tuple(w["waits_for"]), w["wait_known"])
               for w in snapshot.get("waiters", [])]
    recent = snapshot.get("recent", [])
    last = (recent[-1]["at"], recent[-1]["event"]) if recent else None
    return json.dumps([holders, waiters, len(recent), last, snapshot.get("closed")], default=str)


class CardLeaseStatePublisher:
    """Keeps the file and the UI event current. Built on the owner loop; `start()` / `stop()` there."""

    def __init__(self, loop: asyncio.AbstractEventLoop, path: Path,
                 broadcast: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None, *,
                 debounce_s: float = DEBOUNCE_S, poll_s: float = POLL_S,
                 heartbeat_s: float = HEARTBEAT_S):
        self._loop = loop
        self.path = Path(path)
        self._broadcast = broadcast
        self.debounce_s = debounce_s
        self.poll_s = poll_s
        self.heartbeat_s = heartbeat_s
        # One worker: two writes never overtake each other, and none runs on the owner loop.
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="card-lease-file")
        self._timer: Optional[asyncio.TimerHandle] = None
        self._poll: Optional[asyncio.TimerHandle] = None
        self._last_fire = -float("inf")
        self._last_written = -float("inf")
        self._last_key: Optional[str] = None
        self._tasks: "set[asyncio.Future]" = set()
        self._running = False
        self.writes = 0

    # -- lifecycle --

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        card_lease.add_change_listener(self.poke)
        self._schedule()
        self._poll = self._loop.call_later(self.poll_s, self._tick)

    def stop(self) -> None:
        """Stop following the lease, then write the file one last time as a stopped backend's."""
        if not self._running:
            return
        self._running = False
        card_lease.remove_change_listener(self.poke)
        for handle in (self._timer, self._poll):
            if handle is not None:
                handle.cancel()
        self._timer = self._poll = None
        try:
            snapshot = card_lease.queue_snapshot()
            snapshot["backend_stopped"] = True
            self._writer.submit(self._write, snapshot).result(timeout=5)
        except Exception as exc:
            logger.debug("card: the final queue file was not written: %s", exc)
        self._writer.shutdown(wait=False)

    # -- the change --

    def poke(self) -> None:
        """A change happened, on any thread: write soon, not now."""
        if not self._running:
            return
        try:
            running_here = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            running_here = False
        if running_here:
            self._schedule()
        else:
            try:
                self._loop.call_soon_threadsafe(self._schedule)
            except RuntimeError:  # the loop closed
                pass

    def _schedule(self) -> None:
        if not self._running or self._timer is not None:
            return
        delay = max(0.0, self._last_fire + self.debounce_s - time.monotonic())
        self._timer = self._loop.call_later(delay, self._fire)

    def _tick(self) -> None:
        if not self._running:
            return
        self._poll = self._loop.call_later(self.poll_s, self._tick)
        self._schedule()

    def _fire(self) -> None:
        self._timer = None
        if not self._running:
            return
        try:
            snapshot = card_lease.queue_snapshot()
        except Exception:
            logger.debug("card: the queue could not be read", exc_info=True)
            return
        key = content_key(snapshot)
        changed = key != self._last_key
        now = time.monotonic()
        if not changed and now - self._last_written < self.heartbeat_s:
            return
        self._last_key = key
        self._last_fire = now
        self._last_written = now
        self._keep(self._loop.run_in_executor(self._writer, self._write, snapshot))
        if changed and self._broadcast is not None:
            self._keep(self._loop.create_task(self._push(snapshot)))

    def _write(self, snapshot: Dict[str, Any]) -> bool:
        self.writes += 1
        try:
            return write_atomically(self.path, snapshot)
        except Exception:
            logger.debug("card: the queue file write failed", exc_info=True)
            return False

    async def _push(self, snapshot: Dict[str, Any]) -> None:
        try:
            await self._broadcast(EVENT_NAME, snapshot)  # type: ignore[misc]
        except Exception:
            logger.debug("card: the queue was not pushed to the UI", exc_info=True)

    def _keep(self, future: "asyncio.Future") -> None:
        self._tasks.add(future)
        future.add_done_callback(self._tasks.discard)

    async def flush(self) -> None:
        """Wait for the writes and pushes under way (tests, and a stop that must not lose one)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
