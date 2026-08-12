"""Two mutations of one index must not interleave, and neither may stop the loop.

Five call sites do `load → mutate → save` on an agent's index with their own copy of
the backend: the per-file sync pass, the `write_file` and `repo_delete` tools, the L6
commit reindex, and the shared-layer purge. Three run in executor threads and two on
the event loop, and nothing serialised them — the loser's documents would simply be
absent afterwards, with no line in any log to say so.

The primitive is a queue rather than a lock because two of those callers are
coroutines: a lock acquired on the loop freezes every other coroutine for the length
of somebody else's rebuild, which is twelve seconds for the largest agent.
"""

import asyncio
import pathlib
import threading
import time

import pytest

from dpc_client_core.dpc_agent.index_writer import (
    write_index,
    write_index_async,
    writer_for,
)


def test_mutations_on_one_directory_do_not_interleave(tmp_path):
    """The property the queue exists for, stated as a trace."""
    trace = []

    def slow(tag):
        def run():
            trace.append(f"{tag}-start")
            time.sleep(0.05)
            trace.append(f"{tag}-end")
        return run

    threads = [threading.Thread(target=write_index, args=(tmp_path, slow(t)))
               for t in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(trace) == 6
    # Whatever the order between them, no mutation starts inside another.
    for i in range(0, 6, 2):
        assert trace[i].endswith("-start")
        assert trace[i + 1] == trace[i].replace("-start", "-end")


def test_two_agents_do_not_wait_for_each_other(tmp_path):
    """One worker per directory, not one for the fleet.

    A rebuild of a 2000-document agent must not delay a write to a 90-document one.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    started = threading.Event()
    release = threading.Event()

    def blocker():
        started.set()
        release.wait(timeout=10)

    t = threading.Thread(target=write_index, args=(a, blocker))
    t.start()
    assert started.wait(timeout=5)

    # The other directory answers while the first is still parked.
    assert write_index(b, lambda: "done") == "done"

    release.set()
    t.join(timeout=10)


def test_the_same_directory_is_one_worker_however_the_path_is_spelled(tmp_path):
    nested = tmp_path / "state" / "memory_index"
    nested.mkdir(parents=True)
    other_spelling = tmp_path / "state" / ".." / "state" / "memory_index"

    assert writer_for(nested) is writer_for(other_spelling)
    assert writer_for(nested) is writer_for(pathlib.Path(str(nested).upper()))


def test_a_mutation_that_reaches_for_the_same_writer_runs_inline(tmp_path):
    """Otherwise it would wait on the queue it is draining, which never ends."""
    def outer():
        return write_index(tmp_path, lambda: "inner ran")

    assert write_index(tmp_path, outer) == "inner ran"


def test_a_result_comes_back_and_an_exception_reaches_the_caller(tmp_path):
    assert write_index(tmp_path, lambda: 42) == 42

    with pytest.raises(ValueError, match="boom"):
        write_index(tmp_path, lambda: (_ for _ in ()).throw(ValueError("boom")))

    assert write_index(tmp_path, lambda: "still serving") == "still serving"


def test_the_loop_keeps_running_while_a_mutation_is_in_flight(tmp_path):
    """The reason this is a queue: a coroutine awaits, it does not block.

    A lock here would stop every other coroutine — the UI socket, the P2P reads, the
    other agents — for as long as the mutation takes.
    """
    ticks = []

    async def main():
        release = threading.Event()

        async def ticker():
            for _ in range(20):
                ticks.append(1)
                await asyncio.sleep(0.005)
            release.set()

        def parked():
            release.wait(timeout=10)
            return "unparked"

        tick_task = asyncio.create_task(ticker())
        result = await write_index_async(tmp_path, parked)
        await tick_task
        return result

    assert asyncio.run(main()) == "unparked"
    assert len(ticks) == 20  # the loop kept turning throughout


def test_a_mutation_cannot_kill_the_worker(tmp_path):
    """A dead worker is not an error anybody sees — it is a queue that never drains.

    Every later write to that agent's index would wait forever, which is worse than the
    interleaving this class exists to prevent. Reproduced with the reachable case: a
    future that refuses its own result, as a cancelled one does.
    """
    class _Hostile:
        def set_running_or_notify_cancel(self):
            return True

        def set_result(self, value):
            raise RuntimeError("nobody is waiting for this any more")

        def set_exception(self, exc):
            raise RuntimeError("nor for this")

    writer = writer_for(tmp_path)
    writer._queue.put((lambda: "ok", _Hostile()))
    writer._queue.put((lambda: (_ for _ in ()).throw(ValueError("boom")), _Hostile()))

    # Bounded, because the failure is a wait that never ends: asserting on a return
    # value would hang the suite instead of failing it.
    served = []
    t = threading.Thread(target=lambda: served.append(write_index(tmp_path, lambda: "still serving")))
    t.daemon = True
    t.start()
    t.join(timeout=5)

    assert served == ["still serving"], "the worker died and its queue will never drain again"


def test_the_writer_thread_cannot_hold_the_process_open(tmp_path):
    """Same reasoning as the browser session's thread: a parked mutation must not
    outlive its usefulness by keeping the interpreter alive."""
    import concurrent.futures.thread as cf_thread

    writer = writer_for(tmp_path)

    assert writer._thread.daemon is True
    assert writer._thread not in cf_thread._threads_queues
