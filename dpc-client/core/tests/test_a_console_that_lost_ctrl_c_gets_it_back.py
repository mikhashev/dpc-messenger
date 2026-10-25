"""A backend console that lost ENABLE_PROCESSED_INPUT gets Ctrl+C back.

Twice the console running run_service.py was left at 0x01f0 (PROCESSED, LINE
and ECHO cleared) and Ctrl+C never reached the backend. The guard reads the
mode about once a second, names every process on the console when PROCESSED
goes, and turns the three bits back on. The console API is injected, so no
real console is needed here.
"""

import ast
import logging
import threading
import time
from pathlib import Path

from dpc_client_core import console_guard
from dpc_client_core.console_guard import ConsoleGuard, start_console_guard

LOGGER = "dpc_client_core.console_guard"
RUN_SERVICE = Path(__file__).resolve().parents[1] / "run_service.py"


class FakeConsole:
    def __init__(self, mode=0x01F7, opens=True, set_ok=True, pids=(100, 200)):
        self.mode = mode
        self.opens = opens
        self.set_ok = set_ok
        self.pids = list(pids)
        self.set_calls = []
        self.closed = False
        self.raise_on_get = 0

    def open(self):
        return self.opens

    def last_error(self):
        return 6

    def get_mode(self):
        if self.raise_on_get:
            self.raise_on_get -= 1
            raise RuntimeError("boom")
        return self.mode

    def set_mode(self, mode):
        self.set_calls.append(mode)
        if self.set_ok:
            self.mode = mode
        return self.set_ok

    def process_ids(self):
        return list(self.pids)

    def close(self):
        self.closed = True


def _describe(pid):
    return f"pid={pid} parent=1 fake-{pid}.exe started 2026-10-25 12:00:00 :: fake {pid}"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _guard(console, clock=None):
    return ConsoleGuard(console, describe=_describe, clock=clock or FakeClock(), own_pid=100)


def _records(caplog, level=None):
    return [
        r for r in caplog.records
        if r.name == LOGGER and (level is None or r.levelno == level)
    ]


def test_lost_processed_is_named_with_the_console_and_restored_keeping_other_bits(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    console = FakeConsole(0x01F7)
    guard = _guard(console)
    assert guard.check()
    console.mode = 0x01F0
    assert guard.check()

    assert console.set_calls == [0x01F7]
    assert console.mode == 0x01F7
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "lost ENABLE_PROCESSED_INPUT" in text
    assert "0x01f7 -> 0x01f0" in text
    assert "cleared [PROCESSED|LINE|ECHO]" in text
    assert "2 process(es) on the console" in text
    assert "fake-100.exe" in text and "(this backend)" in text
    assert "fake-200.exe" in text
    infos = [r.getMessage() for r in _records(caplog, logging.INFO)]
    assert any("restored: 0x01f0 -> 0x01f7" in m for m in infos)


def test_a_mode_inherited_raw_at_start_is_restored_too(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    console = FakeConsole(0x01E0)
    guard = _guard(console)
    assert guard.check()

    assert console.set_calls == [0x01E7]
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "0x01e0 at guard start" in warnings[0].getMessage()
    assert "cleared [PROCESSED|LINE|ECHO]" in warnings[0].getMessage()


def test_a_healthy_console_is_left_alone_and_silent(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    console = FakeConsole(0x01F7)
    guard = _guard(console)
    for _ in range(5):
        assert guard.check()
    assert console.set_calls == []
    assert _records(caplog) == []


def test_line_or_echo_alone_is_reported_once_and_not_restored(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    console = FakeConsole(0x01F7)
    guard = _guard(console)
    guard.check()
    console.mode = 0x01F1  # llama.cpp console.cpp: LINE and ECHO off, PROCESSED kept
    for _ in range(3):
        guard.check()

    assert console.set_calls == []
    assert _records(caplog, logging.WARNING) == []
    infos = [r.getMessage() for r in _records(caplog, logging.INFO)]
    assert len(infos) == 1
    assert "0x01f7 -> 0x01f1" in infos[0]
    assert "cleared [LINE|ECHO]" in infos[0] and "not restored" in infos[0]


def test_a_failing_restore_warns_once_and_never_raises(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    console = FakeConsole(0x01F7, set_ok=False)
    clock = FakeClock()
    guard = _guard(console, clock)
    guard.check()
    console.mode = 0x01F0
    for _ in range(30):
        clock.now += 1
        assert guard.check()

    assert len(console.set_calls) == 30  # still trying, quietly
    warnings = [r.getMessage() for r in _records(caplog, logging.WARNING)]
    assert len(warnings) == 2, warnings
    assert "lost ENABLE_PROCESSED_INPUT" in warnings[0]
    assert "could not be restored to 0x01f7" in warnings[1]
    assert "error 6" in warnings[1]
    assert not any("restored:" in r.getMessage() for r in _records(caplog, logging.INFO))


def test_a_process_that_keeps_clearing_it_gets_one_warning_per_episode(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    console = FakeConsole(0x01F7)
    clock = FakeClock()
    guard = _guard(console, clock)
    guard.check()
    for _ in range(10):  # cleared again a second after every restore
        clock.now += 1
        console.mode = 0x01F0
        guard.check()
        clock.now += 1
        guard.check()
    assert len(_records(caplog, logging.WARNING)) == 1
    assert len(console.set_calls) == 10

    clock.now += console_guard.EPISODE_SETTLE_SEC + 1
    guard.check()  # intact long enough: the episode is over
    clock.now += 1
    console.mode = 0x01F0
    guard.check()
    assert len(_records(caplog, logging.WARNING)) == 2


def test_no_console_means_no_thread_and_no_error(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    before = {t.ident for t in threading.enumerate()}
    guard = start_console_guard(lambda: FakeConsole(opens=False), platform="win32")

    assert guard is None
    new = [t for t in threading.enumerate() if t.ident not in before]
    assert not any(t.name == "console-guard" for t in new)
    records = _records(caplog)
    assert len(records) == 1 and records[0].levelno == logging.DEBUG
    assert "no console" in records[0].getMessage()


def test_not_windows_is_a_no_op(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    made = []
    guard = start_console_guard(lambda: made.append(1) or FakeConsole(), platform="linux")
    assert guard is None
    assert made == []
    records = _records(caplog)
    assert len(records) == 1 and records[0].levelno == logging.DEBUG


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_thread_restores_a_lost_mode_and_stops_on_request(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    console = FakeConsole(0x01F7)
    guard = start_console_guard(
        lambda: console, platform="win32", describe=_describe, interval=0.01,
    )
    try:
        assert guard is not None and guard.thread.daemon
        console.mode = 0x01F0
        assert _wait_for(lambda: console.mode == 0x01F7)
    finally:
        guard.stop()
    assert not guard.thread.is_alive()
    assert console.closed


def test_a_failing_check_is_told_once_and_the_thread_keeps_polling(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    console = FakeConsole(0x01F7)
    guard = start_console_guard(
        lambda: console, platform="win32", describe=_describe, interval=0.01,
    )
    try:
        console.raise_on_get = 5
        assert _wait_for(lambda: console.raise_on_get == 0)
        console.mode = 0x01F0
        assert _wait_for(lambda: console.mode == 0x01F7)
        assert guard.thread.is_alive()
    finally:
        guard.stop()
    failures = [r for r in _records(caplog, logging.WARNING) if "check failed" in r.getMessage()]
    assert len(failures) == 1


def test_run_service_starts_the_guard_after_logging_and_stops_it_on_shutdown():
    tree = ast.parse(RUN_SERVICE.read_text(encoding="utf-8"))
    main = next(
        n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main"
    )
    calls = [
        (n.lineno, n.func.id if isinstance(n.func, ast.Name) else n.func.attr)
        for n in ast.walk(main)
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
    ]
    lines = {name: line for line, name in sorted(calls, reverse=True)}
    assert "start_console_guard" in lines, "main() never starts the console guard"
    assert lines["setup_logging"] < lines["start_console_guard"]
    stops = [
        n for n in ast.walk(main)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "stop" and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "console_guard"
    ]
    assert stops, "the shutdown path never stops the console guard"
