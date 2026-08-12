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

The scope is one directory. A rebuild of a 2000-document agent must not delay a write
to a 90-document one, so the registry is keyed by the resolved index path and each
agent gets its own worker.

The worker is a daemon thread, and for the same reason as the browser session's:
a mutation parked on a dead handle must not keep the process alive after shutdown.
"""

from __future__ import annotations

import logging
import pathlib
import queue
import threading
from concurrent.futures import Future
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


def writer_for(index_dir: pathlib.Path) -> _IndexWriter:
    """The one worker that owns this directory, created on first use."""
    key = str(pathlib.Path(index_dir).resolve()).lower()
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
    """
    writer = writer_for(index_dir)
    if writer.owns_current_thread:
        return fn()
    return writer.submit(fn).result()


async def write_index_async(index_dir: pathlib.Path, fn: Callable[[], Any]) -> Any:
    """Same, for callers on the event loop: yields instead of blocking."""
    import asyncio

    writer = writer_for(index_dir)
    if writer.owns_current_thread:  # not reachable today; correct if it becomes so
        return fn()
    return await asyncio.wrap_future(writer.submit(fn))
