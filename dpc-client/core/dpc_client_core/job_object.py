"""The backend puts itself in a kill-on-close Job Object (ADR-046, point 1).

Closing the console window, End Task, a crash or a stop that never reaches its
cleanup ends the backend without running any of its shutdown code, and every
process it started kept running on its own. A Job Object set to
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE ends all of its members when its last handle
closes, and the only handle is this process's, which the OS closes however the
process ends.

A process created by a member of a job is a member from birth, so putting the
backend itself in the job, before it spawns anything, leaves no window in which
a grandchild is born outside it. No JOB_OBJECT_LIMIT_BREAKAWAY_OK, on purpose:
a descendant that asks for CREATE_BREAKAWAY_FROM_JOB then fails to start instead
of escaping. (Cargo, `src/util/job.rs`, does the same and does allow breakaway.)
A job nested inside this one, such as the Strata launcher's, is allowed since
Windows 8: it becomes a child of this job and dies with it.

Failure is not fatal. If the job cannot be made or the process cannot be
assigned (an older Windows, a restricting parent job), the outcome is a WARNING
naming the step and the Windows error, and startup goes on.

Imports nothing beyond the standard library and spawns nothing, so
`run_service.py` can call it before anything else exists. Logging is not
configured yet at that point, so the call returns a `JobOutcome` and the caller
writes it with `JobOutcome.log` once the handlers are in place.

Windows only. Elsewhere it does nothing; the POSIX points of the ADR are later.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
JobObjectExtendedLimitInformation = 9

# The one handle, for the life of the process. Not inheritable: it is created
# with no security attributes, and nothing duplicates it.
_job_handle: Optional[int] = None


@dataclass
class JobOutcome:
    """What `enter_kill_on_close_job` did, to be written once logging exists."""

    level: int
    message: str
    entered: bool = False

    def log(self, target: Optional[logging.Logger] = None) -> None:
        (target or logger).log(self.level, "%s", self.message)


def _load_kernel32():
    """A private kernel32, so the argtypes set here touch nobody else's."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetCurrentProcess.argtypes = ()
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.IsProcessInJob.restype = wintypes.BOOL
    k32.IsProcessInJob.argtypes = (
        wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL),
    )
    return k32


def _extended_limit_information(flags: int):
    """A zeroed JOBOBJECT_EXTENDED_LIMIT_INFORMATION carrying `flags`."""
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = flags
    return info


class _StepFailed(Exception):
    def __init__(self, step: str, code: int):
        super().__init__(step, code)
        self.step = step
        self.code = code


def _create_kill_on_close_job(k32) -> int:
    """CreateJobObjectW + SetInformationJobObject; the handle, or _StepFailed."""
    import ctypes

    job = k32.CreateJobObjectW(None, None)  # no security attributes: not inheritable
    if not job:
        raise _StepFailed("CreateJobObjectW", ctypes.get_last_error())
    info = _extended_limit_information(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    if not k32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info),
    ):
        code = ctypes.get_last_error()
        k32.CloseHandle(job)
        raise _StepFailed("SetInformationJobObject", code)
    return job


def enter_kill_on_close_job(platform: Optional[str] = None) -> JobOutcome:
    """Put this process in a kill-on-close Job Object and keep the handle.

    Never raises. The outcome is INFO when the job is in place, WARNING when a
    step failed (named, with the Windows error), DEBUG when there is nothing to
    do (not Windows, or already in place).
    """
    global _job_handle
    platform = platform if platform is not None else sys.platform
    if platform != "win32":
        return JobOutcome(logging.DEBUG, f"No Job Object: not Windows ({platform})")
    if _job_handle is not None:
        return JobOutcome(
            logging.DEBUG, "Kill-on-close Job Object already in place", entered=True,
        )
    try:
        import ctypes

        k32 = _load_kernel32()
        job = _create_kill_on_close_job(k32)
        if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            code = ctypes.get_last_error()
            k32.CloseHandle(job)
            raise _StepFailed("AssignProcessToJobObject", code)
    except _StepFailed as exc:
        return JobOutcome(
            logging.WARNING,
            f"Kill-on-close Job Object not in place: {exc.step} failed, Windows error "
            f"{exc.code} ({_win_error_text(exc.code)}); processes this backend starts "
            "will outlive it if it dies without cleaning up",
        )
    except Exception as exc:  # no kernel32, an odd ctypes failure: still not fatal
        return JobOutcome(
            logging.WARNING,
            f"Kill-on-close Job Object not in place: {type(exc).__name__}: {exc}; "
            "processes this backend starts will outlive it if it dies without "
            "cleaning up",
        )
    _job_handle = job
    return JobOutcome(
        logging.INFO,
        f"Backend pid {os.getpid()} is in a kill-on-close Job Object (breakaway not "
        "allowed): every process it starts, and theirs, ends with it",
        entered=True,
    )


def _win_error_text(code: int) -> str:
    try:
        import ctypes

        return ctypes.FormatError(code).strip()
    except Exception:
        return "no text"


def open_url_outside_the_job(
    url: str,
    *,
    platform: Optional[str] = None,
    launch=None,
    fallback=None,
) -> str:
    """Open `url` in the user's default browser without making it the backend's descendant.

    A browser started by `webbrowser.open` is a child of this process, so with
    the job in place it would join the job and the user's whole browser would
    die when the backend stops. `explorer.exe <url>` hands the URL to the
    running shell, which starts the browser itself, outside the job (measured
    for a classic Win32 target: the target's parent was an explorer.exe and
    `IsProcessInJob` was False in three runs out of three).

    Only when the job is in place on Windows does it take that road. When the
    job is not in place, off Windows, or when the hand-off cannot be started, it
    falls back to `webbrowser.open`. Returns "explorer" or "webbrowser", the
    road taken. `launch` and `fallback` are seams for tests.
    """
    platform = platform if platform is not None else sys.platform
    if platform == "win32" and _job_handle is not None:
        try:
            (launch or _launch_explorer)(url)
            return "explorer"
        except Exception as exc:
            logger.warning(
                "Could not hand the URL to explorer.exe (%s: %s); opening it from "
                "the backend instead, so the browser may end with the backend",
                type(exc).__name__, exc,
            )
    if fallback is None:
        import webbrowser

        fallback = webbrowser.open
    fallback(url)
    return "webbrowser"


def _launch_explorer(url: str) -> None:
    """Start `explorer.exe <url>`; the launcher exits at once (code 1 is normal)."""
    import subprocess

    explorer = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
    subprocess.Popen(
        [explorer, url],
        creationflags=0x08000000,  # CREATE_NO_WINDOW
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
