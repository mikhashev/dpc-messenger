"""One writer per memory index, so five call sites cannot interleave on one directory.

Five places mutate an agent's index — the per-file sync pass, the `write_file` and
`repo_delete` tools, the L6 commit reindex, and the shared-layer purge — and each
does `load → mutate → save` with its own copy of the backend. Two of them run on the
event loop and three in executor threads, so the interleaving is possible today and
nothing reports it: the loser's documents are simply absent afterwards, with no line
in any log.

Why a queue rather than a lock. A `threading.Lock` held across a pass is acquired by
callers on the event loop, and blocking there freezes everything for the length of
someone else's rebuild — 12 s for the largest agent. A queue lets a loop caller await
instead of wait, and lets two approvals in quick succession serialise inside the
worker without either voter's turn stopping.

The scope is one directory **within one process**. A rebuild of a 2000-document agent
must not delay a write to a 90-document one, so each agent gets its own worker. Nothing
here coordinates across processes: a second D-PC instance, or a script pointed at the
same `~/.dpc`, gets its own registry and can still interleave read-modify-write on
`index_meta.json`. No production code does that today and this is an accepted risk
rather than an oversight — written down because an external reviewer had to ask.

The worker is a daemon thread, and for the same reason as the browser session's:
a mutation parked on a dead handle must not keep the process alive after shutdown.
"""

from __future__ import annotations

import logging
import os
import pathlib
import queue
import threading
from concurrent.futures import Future, TimeoutError as FuturesTimeout
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)


def _settle(future: Future, *, value: Any = None, exc: "BaseException | None" = None) -> None:
    """Hand the result back, and do not care if nobody is waiting for it any more.

    `set_result` on a future the caller has already cancelled raises, and raising here
    would kill the worker — see the loop below for why that is the expensive failure.
    """
    try:
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(value)
    except Exception:
        log.debug("index writer result dropped — caller is gone", exc_info=True)


class _IndexWriter:
    """A serial worker for one index directory."""

    def __init__(self, name: str):
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name=f"index-writer-{name}", daemon=True,
        )
        self._thread.start()

    @property
    def owns_current_thread(self) -> bool:
        return threading.get_ident() == self._thread.ident

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get()
                if item is None:
                    return
                fn, future = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = fn()
                except BaseException as exc:  # noqa: BLE001 — the caller's future owns it
                    _settle(future, exc=exc)
                else:
                    _settle(future, value=result)
            except BaseException:  # noqa: BLE001
                # The worker has to outlive anything one mutation can do to it. A dead
                # worker is not an error anybody sees — it is a queue that never drains
                # again, so every later write to that agent's index waits forever. The
                # reachable case is small (a future cancelled between the check above
                # and the settle below) and the consequence is not.
                log.exception("index writer loop error — worker continues")

    def submit(self, fn: Callable[[], Any]) -> Future:
        future: Future = Future()
        self._queue.put((fn, future))
        return future


_writers: Dict[str, _IndexWriter] = {}
_registry_lock = threading.Lock()

# The longest a caller will wait for its turn before treating the queue as broken.
# Chosen from the slowest legitimate pass anybody has measured — a full rebuild on the
# node without a GPU took 2256 s — not from taste. It is a deadlock backstop, not a
# latency promise: a wait this long is already a defect, and the point is that it ends.
_QUEUE_CEILING = 3600.0
_WAIT_SLICE = 0.5

_shutting_down = threading.Event()


class IndexWriterUnavailable(RuntimeError):
    """Raised instead of waiting forever for a queue that will not drain."""


def begin_shutdown() -> None:
    """Stop new callers from waiting on the queue, because the process is leaving.

    A tool runs on the shared agent pool, whose workers the interpreter joins at exit.
    So a tool parked on `Future.result()` holds the whole process — the same shape as
    the browser session's parked call, one level removed. Once shutdown starts, waiting
    for an index write is pointless: whatever it was going to write, the next start
    rebuilds. Named by an external reviewer, 2026-08-12.
    """
    _shutting_down.set()


def writer_for(index_dir: pathlib.Path) -> _IndexWriter:
    """The one worker that owns this directory, created on first use.

    Keyed by the directory's identity — `(st_dev, st_ino)` — rather than by how its path
    is spelled. Spelling cannot answer this question: `normcase` follows the operating
    system's convention, and the filesystem is what decides. They agree on Windows and on
    Linux and disagree on macOS, whose default APFS is case-insensitive while
    `posixpath.normcase` is the identity — so `.../Agent` and `.../agent`, one directory,
    would get two workers and the interleaving would be back on the one platform nobody
    watches. Both external reviewers found this independently. `realpath` does not help;
    it does not fold case on a case-insensitive volume.

    A directory that does not exist yet has no identity, so those fall back to the
    normalised path — a first write creates it and every later call keys by inode.
    """
    path = pathlib.Path(index_dir)
    try:
        st = path.stat()
        key = "%s:%s" % (st.st_dev, st.st_ino)
    except OSError:
        key = os.path.normcase(os.path.realpath(path))
    with _registry_lock:
        writer = _writers.get(key)
        if writer is None:
            writer = _IndexWriter(pathlib.Path(index_dir).parent.parent.name or "index")
            _writers[key] = writer
        return writer


def write_index(index_dir: pathlib.Path, fn: Callable[[], Any]) -> Any:
    """Run a mutation on the directory's writer and wait for it. For thread callers.

    A caller already running *on* that writer — a mutation that reaches for another —
    runs inline instead, because waiting on the queue it is draining is a deadlock and
    the ordering it wants is already guaranteed.

    Never call this from the event loop: use `write_index_async`, or the wait becomes
    a freeze for the length of whatever the worker is doing for that agent.

    The wait is bounded twice over. It ends when shutdown starts, because this runs on a
    pool worker the interpreter joins at exit and an unbounded wait here would hold the
    process; and it ends at `_QUEUE_CEILING` regardless, because a queue that has not
    drained in an hour is not going to. Both raise rather than return, since a caller
    that believes its document was indexed is worse than one that is told it was not.
    """
    writer = writer_for(index_dir)
    if writer.owns_current_thread:
        return fn()
    future = writer.submit(fn)
    waited = 0.0
    while True:
        try:
            return future.result(timeout=_WAIT_SLICE)
        except FuturesTimeout:
            waited += _WAIT_SLICE
            if _shutting_down.is_set():
                future.cancel()
                raise IndexWriterUnavailable(
                    "index write abandoned: the process is shutting down")
            if waited >= _QUEUE_CEILING:
                future.cancel()
                raise IndexWriterUnavailable(
                    "index write gave up after %.0fs — the writer for %s is not draining"
                    % (waited, index_dir))


async def write_index_async(index_dir: pathlib.Path, fn: Callable[[], Any]) -> Any:
    """Same, for callers on the event loop: yields instead of blocking."""
    import asyncio

    writer = writer_for(index_dir)
    if writer.owns_current_thread:  # not reachable today; correct if it becomes so
        return fn()
    return await asyncio.wrap_future(writer.submit(fn))
