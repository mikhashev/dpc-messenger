"""The agent tool pool must not outlive the shutdown that asked it to stop.

Its workers are non-daemon, so anything left alive is joined by the
interpreter's own atexit hook — with no timeout, and after the last line of
the log. That is the silent half of a hung exit, so what is asserted here is
that the pool is actually gone, not merely that shutdown() returned.
"""

import threading

import pytest

from dpc_client_core.dpc_agent.loop import (
    _get_shared_executor,
    shutdown_shared_executor,
)

JOIN_TIMEOUT = 5


def _tool_workers():
    return [t for t in threading.enumerate() if t.name.startswith("dpc_agent_tool")]


@pytest.fixture(autouse=True)
def _leave_no_pool_behind():
    yield
    shutdown_shared_executor()


def test_shutdown_ends_the_worker_threads():
    _get_shared_executor().submit(lambda: None).result(timeout=JOIN_TIMEOUT)
    workers = _tool_workers()
    assert workers, "the pool never started a worker — the test proves nothing"

    shutdown_shared_executor()

    for t in workers:
        t.join(timeout=JOIN_TIMEOUT)
        assert not t.is_alive(), f"{t.name} survived the shutdown"


def test_pool_is_usable_again_afterwards():
    """Shutdown during service stop must not poison a later run in-process."""
    _get_shared_executor().submit(lambda: None).result(timeout=JOIN_TIMEOUT)
    shutdown_shared_executor()

    assert _get_shared_executor().submit(lambda: 7).result(timeout=JOIN_TIMEOUT) == 7


def test_shutdown_without_a_pool_is_a_no_op():
    """Shutdown runs on every exit, including runs where no agent ever ran."""
    shutdown_shared_executor()
    shutdown_shared_executor()


def test_shutdown_waits_for_a_tool_still_running():
    """The wait is real — that is why the caller has to bound it.

    A tool that outlived its own timeout is still executing in a worker;
    if shutdown returned before it finished, the bound in run_service would
    be guarding nothing.
    """
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_tool():
        started.set()
        release.wait(timeout=JOIN_TIMEOUT)
        finished.set()

    _get_shared_executor().submit(slow_tool)
    assert started.wait(timeout=JOIN_TIMEOUT)

    release.set()
    shutdown_shared_executor()

    assert finished.is_set()
