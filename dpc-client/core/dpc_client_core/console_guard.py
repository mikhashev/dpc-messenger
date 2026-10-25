"""Console guard for the backend: keeps Ctrl+C reaching it on Windows.

Twice the console running run_service.py lost ENABLE_PROCESSED_INPUT (input
mode 0x01f7 -> 0x01f0, PROCESSED|LINE|ECHO cleared), so Ctrl+C was read as a
character, no CTRL_C_EVENT was raised and the graceful shutdown never started.
The mechanism is a process on the same console that switches it to raw mode
and is killed before it switches back; which process did it is not known.

The backend owns its console, so no AttachConsole is needed: open CONIN$, read
the mode about once a second, and when PROCESSED is gone log every process on
the console at that moment (the evidence that names the writer next time) and
turn PROCESSED|LINE|ECHO back on, keeping every other bit.

Windows only. Elsewhere, or with no console to open, it does nothing beyond
one DEBUG line.
"""

import logging
import os
import sys
import threading
from datetime import datetime
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

PROCESSED = 0x0001  # ENABLE_PROCESSED_INPUT: Ctrl+C becomes CTRL_C_EVENT
LINE = 0x0002  # ENABLE_LINE_INPUT
ECHO = 0x0004  # ENABLE_ECHO_INPUT
COOKED = PROCESSED | LINE | ECHO

_BIT_NAMES = (
    (0x0001, "PROCESSED"), (0x0002, "LINE"), (0x0004, "ECHO"),
    (0x0008, "WINDOW"), (0x0010, "MOUSE"), (0x0020, "INSERT"),
    (0x0040, "QUICK_EDIT"), (0x0080, "EXTENDED"), (0x0100, "AUTO_POS"),
    (0x0200, "VT_INPUT"),
)

# A loss within this many seconds of the last restore belongs to the same
# episode: a process that keeps clearing the bit gets one WARNING, not one a
# second.
EPISODE_SETTLE_SEC = 5.0
POLL_INTERVAL_SEC = 1.0


def bit_names(mode: int) -> str:
    return "|".join(name for bit, name in _BIT_NAMES if mode & bit) or "-"


class Win32Console:
    """The four console calls the guard needs, on this process's own console."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        # A private kernel32 so the argtypes set here touch nobody else's.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetConsoleMode.restype = wintypes.BOOL
        k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetConsoleMode.restype = wintypes.BOOL
        k32.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        k32.GetConsoleProcessList.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        self._handle = None

    def last_error(self) -> int:
        return self._ctypes.get_last_error()

    def open(self) -> bool:
        generic_rw = 0x80000000 | 0x40000000
        share_rw = 0x1 | 0x2
        open_existing = 3
        handle = self._k32.CreateFileW("CONIN$", generic_rw, share_rw, None, open_existing, 0, None)
        if handle is None or handle == self._ctypes.c_void_p(-1).value:
            return False
        self._handle = handle
        return True

    def get_mode(self) -> Optional[int]:
        mode = self._wintypes.DWORD()
        if not self._k32.GetConsoleMode(self._handle, self._ctypes.byref(mode)):
            return None
        return mode.value

    def set_mode(self, mode: int) -> bool:
        return bool(self._k32.SetConsoleMode(self._handle, mode))

    def process_ids(self) -> List[int]:
        size = 64
        while True:
            buf = (self._wintypes.DWORD * size)()
            count = self._k32.GetConsoleProcessList(buf, size)
            if count <= size:
                return [buf[i] for i in range(count)]
            size = count + 16

    def close(self) -> None:
        if self._handle is not None:
            self._k32.CloseHandle(self._handle)
            self._handle = None


def describe_process(pid: int) -> str:
    """pid, parent, name, start time and command line, as far as psutil can see."""
    try:
        import psutil

        proc = psutil.Process(pid)
        started = datetime.fromtimestamp(proc.create_time()).strftime("%Y-%m-%d %H:%M:%S")
        cmdline = " ".join(proc.cmdline())[:300]
        return f"pid={pid} parent={proc.ppid()} {proc.name()} started {started} :: {cmdline}"
    except Exception as exc:  # gone, access denied, psutil missing
        return f"pid={pid} ({type(exc).__name__})"


class ConsoleGuard:
    """Polls the console input mode and restores Ctrl+C handling when it is lost."""

    def __init__(
        self,
        console,
        describe: Callable[[int], str] = describe_process,
        interval: float = POLL_INTERVAL_SEC,
        settle: float = EPISODE_SETTLE_SEC,
        clock: Callable[[], float] = None,
        own_pid: Optional[int] = None,
    ):
        import time

        self._console = console
        self._describe = describe
        self._interval = interval
        self._settle = settle
        self._clock = clock or time.monotonic
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._last: Optional[int] = None
        self._episode_open = False
        self._intact_since: Optional[float] = None
        self._restore_failure_told = False
        self._check_failure_told = False
        self._stopped = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- one poll -------------------------------------------------------
    def check(self) -> bool:
        """Read the mode once and act on it. False means: stop polling."""
        mode = self._console.get_mode()
        if mode is None:
            logger.info(
                "Console guard stopped: the console input mode can no longer be "
                "read (GetConsoleMode error %s)", self._console.last_error(),
            )
            return False

        if mode & PROCESSED:
            self._on_intact(mode)
        else:
            self._on_lost(mode)
        return True

    def _on_intact(self, mode: int) -> None:
        last = self._last
        if last is not None and last & PROCESSED and (last ^ mode) & (LINE | ECHO):
            logger.info(
                "Console input mode %#06x -> %#06x: cleared [%s] set [%s], "
                "PROCESSED intact, not restored",
                last, mode, bit_names(last & ~mode & (LINE | ECHO)),
                bit_names(mode & ~last & (LINE | ECHO)),
            )
        if self._episode_open:
            now = self._clock()
            if self._intact_since is None:
                self._intact_since = now
            elif now - self._intact_since >= self._settle:
                self._episode_open = False
                self._intact_since = None
        self._last = mode

    def _on_lost(self, mode: int) -> None:
        self._intact_since = None
        old = self._last
        if not self._episode_open:
            self._episode_open = True
            self._restore_failure_told = False
            self._warn_lost(old, mode)
        else:
            logger.debug("Console input lost PROCESSED again (%#06x), same episode", mode)

        wanted = mode | COOKED
        if self._console.set_mode(wanted):
            logger.info(
                "Console input mode restored: %#06x -> %#06x (PROCESSED|LINE|ECHO "
                "back on, other bits kept); Ctrl+C reaches the backend again",
                mode, wanted,
            )
            self._last = wanted
        else:
            if not self._restore_failure_told:
                self._restore_failure_told = True
                logger.warning(
                    "Console input mode could not be restored to %#06x "
                    "(SetConsoleMode error %s); Ctrl+C will not stop the backend. "
                    "Retrying every poll without further warnings.",
                    wanted, self._console.last_error(),
                )
            self._last = mode

    def _warn_lost(self, old: Optional[int], mode: int) -> None:
        if old is None:
            change = f"mode {mode:#06x} at guard start (inherited)"
            cleared = COOKED & ~mode
        else:
            change = f"mode {old:#06x} -> {mode:#06x}"
            cleared = old & ~mode
        try:
            pids = self._console.process_ids()
        except Exception as exc:
            pids = []
            listing = [f"  (console process list unavailable: {exc})"]
        else:
            listing = []
            for pid in pids:
                mark = " (this backend)" if pid == self._own_pid else ""
                listing.append(f"  on console: {self._describe(pid)}{mark}")
        logger.warning(
            "Console input lost ENABLE_PROCESSED_INPUT, Ctrl+C would not reach "
            "the backend: %s, cleared [%s]; %d process(es) on the console:\n%s",
            change, bit_names(cleared), len(pids), "\n".join(listing),
        )

    # -- the thread ----------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="console-guard", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    if not self.check():
                        return
                except Exception:
                    if not self._check_failure_told:
                        self._check_failure_told = True
                        logger.warning(
                            "Console guard check failed; it keeps polling and "
                            "will not report this again", exc_info=True,
                        )
                self._stopped.wait(self._interval)
        finally:
            try:
                self._console.close()
            except Exception:
                pass

    def stop(self, timeout: float = 2.0) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def thread(self) -> Optional[threading.Thread]:
        return self._thread


def start_console_guard(
    console_factory: Callable[[], object] = None,
    platform: str = None,
    **guard_kwargs,
) -> Optional[ConsoleGuard]:
    """Start the guard thread, or return None when there is nothing to guard.

    Never raises: a guard that cannot start is a DEBUG line, not a reason for
    the backend not to start.
    """
    platform = platform if platform is not None else sys.platform
    if platform != "win32":
        logger.debug("Console guard not started: not Windows (%s)", platform)
        return None
    try:
        console = (console_factory or Win32Console)()
        if not console.open():
            logger.debug(
                "Console guard not started: no console (CONIN$ could not be "
                "opened, error %s)", console.last_error(),
            )
            return None
        mode = console.get_mode()
        if mode is None:
            logger.debug(
                "Console guard not started: CONIN$ opened but its mode cannot "
                "be read (error %s)", console.last_error(),
            )
            console.close()
            return None
        guard = ConsoleGuard(console, **guard_kwargs)
        logger.info(
            "Console guard watching the console input mode (%#06x at start) every %g s",
            mode, guard._interval,
        )
        guard.start()
    except Exception as exc:
        logger.debug("Console guard not started: %s", exc)
        return None
    return guard
