"""A Windows loop close must end even when a completion packet never arrives.

Observed 2026-08-24: every component reported clean stop in 4.2 seconds, and
the process then sat in `IocpProactor.close()` for the six minutes until it
was ended by hand. `py-spy` put the MainThread at
`windows_events.py:864 close -> :774 _poll`, and the three cached operations
printed at loop close were two futures already `finished result=548` / `549`
plus one cancelled, all on `socket [closed] fd=-1`.

The stdlib explains both halves. `_register` (`:723-738`) caches an operation
that completed synchronously *together with* its finished future, because
«Even if GetOverlappedResult() was called, we have to wait for the
notification of the completion in GetQueuedCompletionStatus()». And
`close()` (`:857`) drains with `while self._cache: self._poll(1)` and no
timeout, «don't exit with running overlapped to prevent a crash». One packet
that never comes is therefore a process that never exits.

These tests pin the escape: the watchdog posts the missing packet, the
drain finishes through CPython's own `_poll`, and a future that is still
waiting on a real answer is left alone until the give-up pass.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="IocpProactor is Windows-only"
)

import run_service  # noqa: E402


class _SettledFuture:
    """What `close()` and `_poll` actually ask of a cached future.

    `set_result` and `set_exception` are here because the give-up pass does
    reach them: `_poll` runs the callback for a future that is not done and
    then sets the result. That is the price of the second pass, and pinning
    it here is how it stays visible.
    """

    def __init__(self, done: bool = True) -> None:
        self._done = done
        self.result_set = None
        self.exception_set = None

    def cancelled(self) -> bool:
        return False

    def cancel(self, msg=None) -> bool:
        return False

    def done(self) -> bool:
        return self._done

    def set_result(self, value) -> None:
        self.result_set = value
        self._done = True

    def set_exception(self, exc) -> None:
        self.exception_set = exc
        self._done = True


def _proactor_with_stuck_op(done: bool = True):
    """A real IocpProactor holding one entry whose packet will never come."""
    import _overlapped
    from asyncio.windows_events import IocpProactor

    proactor = IocpProactor()
    ov = _overlapped.Overlapped(0)
    obj = object()  # never registered with the port, so nothing will complete
    proactor._cache[ov.address] = (_SettledFuture(done), ov, obj, lambda *a: None)
    return proactor


def _close_in_thread(proactor, timeout: float):
    """Returns (returned_in_time, thread, exception_or_None).

    The exception matters: an early version of this test read a crash inside
    the drain as a clean return, because the thread's `finally` fired either
    way.
    """
    done = threading.Event()
    box: dict[str, BaseException] = {}

    def run():
        try:
            proactor.close()
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            box["exc"] = exc
        finally:
            done.set()

    t = threading.Thread(target=run, name="close-under-test", daemon=True)
    t.start()
    return done.wait(timeout), t, box.get("exc")


@pytest.fixture(autouse=True)
def _fast_and_shutting_down(monkeypatch):
    """Real deadlines are 5s and 20s; the mechanism is the same at 0.4s."""
    monkeypatch.setattr(run_service, "_DRAIN_NUDGE_SECONDS", 0.4, raising=False)
    monkeypatch.setattr(run_service, "_DRAIN_GIVE_UP_SECONDS", 1.5, raising=False)
    monkeypatch.setattr(run_service, "_shutting_down", True, raising=False)


def test_close_returns_when_a_finished_op_never_gets_its_packet():
    """The load-bearing one: without the nudge this hangs forever."""
    run_service._install_shutdown_diagnostics()
    proactor = _proactor_with_stuck_op(done=True)

    returned, thread, exc = _close_in_thread(proactor, timeout=10.0)

    assert returned, (
        "IocpProactor.close() never returned — the drain is still waiting for "
        "a completion packet that will never arrive"
    )
    assert exc is None, f"close() raised instead of finishing: {exc!r}"
    assert proactor._iocp is None
    assert not proactor._cache
    thread.join(timeout=1.0)


def test_a_still_running_operation_is_not_faked_on_the_first_pass():
    """`done_only` is the whole safety of the nudge.

    Posting a packet for a future that still expects a real answer would let
    `_poll` run its callback with transferred=0 and set a wrong result. The
    first pass must skip it; only the give-up pass may take it.
    """
    proactor = _proactor_with_stuck_op(done=False)
    try:
        assert run_service._post_missing_completions(proactor, done_only=True) == 0
        assert run_service._post_missing_completions(proactor, done_only=False) == 1
    finally:
        proactor._cache.clear()
        proactor.close()


def test_the_give_up_pass_lets_an_unfinished_operation_go():
    """A run where nothing is settled still ends — later, and it says so."""
    run_service._install_shutdown_diagnostics()
    proactor = _proactor_with_stuck_op(done=False)

    started = time.monotonic()
    returned, thread, exc = _close_in_thread(proactor, timeout=10.0)
    waited = time.monotonic() - started

    assert returned, "the give-up pass did not release the drain"
    assert exc is None, f"close() raised instead of finishing: {exc!r}"
    # It must not have taken the short path — that would mean an unfinished
    # future was faked at the first tick.
    assert waited >= 1.0, f"released after {waited:.2f}s, before the give-up deadline"
    thread.join(timeout=1.0)


def test_a_clean_close_arms_nothing():
    """An empty cache must not cost a thread, and must not wait."""
    from asyncio.windows_events import IocpProactor

    run_service._install_shutdown_diagnostics()
    before = {t.name for t in threading.enumerate()}
    proactor = IocpProactor()

    started = time.monotonic()
    proactor.close()
    waited = time.monotonic() - started

    assert waited < 1.0
    new = {t.name for t in threading.enumerate()} - before
    assert "shutdown-drain-watchdog" not in new


def test_the_nudge_posts_only_for_what_is_cached():
    """Nothing cached, nothing posted — and a closed port is not touched."""
    from asyncio.windows_events import IocpProactor

    proactor = IocpProactor()
    assert run_service._post_missing_completions(proactor, done_only=True) == 0
    proactor.close()
    assert proactor._iocp is None
    assert run_service._post_missing_completions(proactor, done_only=False) == 0
