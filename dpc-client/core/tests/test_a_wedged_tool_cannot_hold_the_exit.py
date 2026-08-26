"""A tool parked on a call nobody can interrupt must not keep the process alive.

The entry this belongs to (THE-TOOL-POOL-IS-NOT-DAEMON-AND-ITS-SHUTDOWN-JOIN-HAS-NO-BOUND)
predicted the failure precisely: the bounded wait in run_service expires, the
WARNING is printed, and the process *still* does not leave, because
`_python_exit` joins pool workers after that bound has already fired.

So the assertion here is on the exit itself, not on shutdown() returning. The
`thread_pool` arm runs the identical wedge against the construction that was
replaced — if that arm ever stops hanging, this test has stopped measuring
anything and the daemon arm proves nothing by passing.
"""

import subprocess
import sys

import pytest

EXIT_BUDGET_SEC = 30

# Wedge: a socket that nobody will ever write to, read with no timeout. The
# tool layer cannot interrupt it, which is the whole premise.
_PROGRAM = """
import socket, sys, threading, time

srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
sock = socket.create_connection(srv.getsockname())
wedged = threading.Event()

def tool_that_never_returns():
    wedged.set()
    sock.recv(1)          # nothing is ever sent; no timeout

POOL = {pool}
if POOL == "thread_pool":
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dpc_agent_tool")
    pool.submit(tool_that_never_returns)
    assert wedged.wait(5)
    # Bounded exactly the way run_service bounds it.
    t = threading.Thread(target=lambda: pool.shutdown(wait=True, cancel_futures=True))
    t.daemon = True
    t.start()
    t.join(timeout=4.0)
else:
    sys.path.insert(0, {root!r})
    from dpc_client_core.dpc_agent.loop import _get_shared_executor, shutdown_shared_executor
    _get_shared_executor().submit(tool_that_never_returns)
    assert wedged.wait(5)
    shutdown_shared_executor()

print("shutdown returned", flush=True)
"""


def _run(pool: str, root: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _PROGRAM.format(pool=repr(pool), root=root)],
        capture_output=True,
        text=True,
        timeout=EXIT_BUDGET_SEC,
    )


@pytest.fixture(scope="module")
def repo_root() -> str:
    import pathlib
    return str(pathlib.Path(__file__).resolve().parents[1])


def test_the_wedge_still_hangs_a_thread_pool(repo_root):
    """Neutralise the fix: the construction that was replaced must still hang.

    Without this the daemon arm below is unfalsifiable — a wedge that no
    longer wedges would pass it for the wrong reason.
    """
    with pytest.raises(subprocess.TimeoutExpired):
        _run("thread_pool", repo_root)


def test_a_wedged_tool_does_not_hold_the_daemon_pool_open(repo_root):
    done = _run("daemon_pool", repo_root)
    assert "shutdown returned" in done.stdout, done.stderr
    assert done.returncode == 0, done.stderr
