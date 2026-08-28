"""A browser session's thread must not be able to hold the process open.

Playwright's sync objects are thread-affine, so every call for one session has to
run on one thread — that is why a pinned runner exists at all. The runner used to
be a single-worker `ThreadPoolExecutor`, and that is exactly the shape that keeps
a dying process alive: `_python_exit` joins every registered pool worker, daemon
flag or not, and a worker parked in Playwright's dispatcher fiber never returns.
Observed 2026-08-12 17:32 — the process outlived its own shutdown by minutes with
`camoufox-agent_001_0` stuck inside `AuthBrowser.close()`.
"""

import concurrent.futures.thread as cf_thread
import threading
import time

import pytest

from dpc_client_core.dpc_agent.tools.browser import _PinnedThread


@pytest.fixture
def runner():
    r = _PinnedThread("test-pinned")
    yield r
    r.shutdown()


def test_every_call_lands_on_the_same_thread(runner):
    """The property the pinned runner exists for (S155 thread affinity)."""
    seen = {runner.submit(threading.get_ident).result(timeout=5) for _ in range(5)}

    assert len(seen) == 1
    assert seen != {threading.get_ident()}  # and it is not the caller's thread


def test_the_thread_is_a_daemon_and_unknown_to_the_executor_atexit_hook(runner):
    """Both halves matter, and neither is enough alone.

    `_python_exit` joins whatever sits in `_threads_queues` before the
    interpreter joins non-daemon threads, so a registered thread hangs the exit
    however it is flagged; an unregistered non-daemon thread is then joined by
    `threading._shutdown` instead.
    """
    worker = runner._thread

    assert worker.daemon is True
    assert worker not in cf_thread._threads_queues

    # Positive control: the shape we moved away from does register, so the
    # assertion above is testing something rather than passing by accident.
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        pool.submit(lambda: None).result(timeout=5)
        assert any(t in cf_thread._threads_queues for t in threading.enumerate()
                   if t.name.startswith("ThreadPoolExecutor"))
    finally:
        pool.shutdown(wait=True)


def test_a_call_that_never_returns_does_not_hold_the_next_teardown(runner):
    """The failing shape, in miniature: submit something that parks forever."""
    parked = threading.Event()
    runner.submit(lambda: (parked.set(), threading.Event().wait())[0])
    assert parked.wait(timeout=5)

    started = time.monotonic()
    runner.shutdown()  # must not wait on the parked call
    assert time.monotonic() - started < 1.0
    assert runner._thread.is_alive()  # still stuck, and that is now harmless


def test_arguments_and_results_pass_through(runner):
    assert runner.submit(lambda a, b=0: a + b, 2, b=3).result(timeout=5) == 5


def test_an_exception_reaches_the_caller_rather_than_killing_the_thread(runner):
    with pytest.raises(ValueError, match="boom"):
        runner.submit(lambda: (_ for _ in ()).throw(ValueError("boom"))).result(timeout=5)

    # the thread survived it and still serves the session
    assert runner.submit(lambda: "alive").result(timeout=5) == "alive"


def test_a_cancelled_call_is_skipped_rather_than_run(runner):
    """`asyncio.wait_for` cancels the wrapped future on timeout; the queued work
    must not run afterwards against a session the caller has given up on."""
    release = threading.Event()
    ran = []
    runner.submit(release.wait)
    queued = runner.submit(lambda: ran.append(1))

    assert queued.cancel()
    release.set()
    runner.submit(lambda: None).result(timeout=5)  # drains past the cancelled item

    assert ran == []


def test_the_session_runner_is_not_a_pool_worker():
    """A source guard, because the defect is a type choice rather than a line.

    Reintroducing `ThreadPoolExecutor` here would restore the atexit join, and
    nothing in a normal test run would notice — the failure is a process that
    does not exit.
    """
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1]
              / "dpc_client_core" / "dpc_agent" / "tools" / "browser.py").read_text(encoding="utf-8")

    # The name is welcome in prose — the docstrings explain why it is not used.
    # What must not come back is a construction or the import behind one.
    assert "ThreadPoolExecutor(" not in source, (
        "a browser session's calls must run on a daemon thread of our own — a pool "
        "worker is joined by _python_exit and can hold the process open forever"
    )
    assert "import ThreadPoolExecutor" not in source
    assert "_PinnedThread(" in source
