"""
Free commit and free physical memory, read by one GlobalMemoryStatusEx call (ADR-044).

Strata parks conversations in host RAM and can exit at the Windows commit limit
(RAM plus page file) with an NTSTATUS code and no message. Two commit metrics
disagreed in the gate runs (`GetPerformanceInfo` and `GlobalMemoryStatusEx`);
Mike's call, 2026-10-19: the reference is free commit by `GlobalMemoryStatusEx`,
`ullAvailPageFile` of `ullTotalPageFile` (the commit limit) — the figure that
predicted the death of the 15360 MiB gate run. Free physical memory from the
same call (`ullAvailPhys` of `ullTotalPhys`) is printed after it, secondary;
each is named by its field so a reader knows which number is which.

Windows only, by the ADR's scope: on any other OS the reader returns None and
the text says "not measured on this OS". It never raises.

`read_free_commit_or_physical` is the reading `list_processes` (ADR-046 point 8)
and the local engines' status answer with: the same on Windows; elsewhere physical
memory by psutil under the same keys, the two commit fields None and `source`
naming psutil. Commit is not read off Windows (decided with the names, 2026-10-26:
null on Linux and macOS). The Strata supervisor keeps `read_free_commit`.

`read_vram_mib` is the card's side of the same answer: the first NVIDIA GPU's free and
total VRAM by nvidia-smi, cached 30 s. It lives here, with the other system-wide
readings `list_processes` answers with, because it belongs to no engine; the two
single-figure reads under it stay in `llama_server_supervisor`, where engine admission
and Strata's release check use them. Per-process VRAM is another figure from another
reader (PDH, in the registry's sampler).
"""

import ctypes
import logging
import ntpath
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SOURCE = "GlobalMemoryStatusEx"
# Off Windows, `read_free_commit_or_physical` reads physical memory only, through psutil.
PHYSICAL_SOURCE = "psutil"
NOT_MEASURED = f"free commit and free physical memory not measured on this OS ({SOURCE} is Windows only)"
UNREAD = f"free commit and free physical memory unread ({SOURCE} failed)"


class MEMORYSTATUSEX(ctypes.Structure):
    # DWORD is 32 bits on Windows; c_uint32 keeps the layout right where c_ulong is 64.
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _global_memory_status_ex(status: MEMORYSTATUSEX) -> bool:
    """The kernel32 call itself; a test replaces it."""
    return bool(ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)))


def read_free_commit() -> Optional[Dict[str, Any]]:
    """Free and total commit and physical memory in MiB, or None off Windows or on a failed call.

    `avail_page_file_mib` / `total_page_file_mib` are `ullAvailPageFile` /
    `ullTotalPageFile` (free commit and the commit limit); `avail_phys_mib` /
    `total_phys_mib` are `ullAvailPhys` / `ullTotalPhys`."""
    if sys.platform != "win32":
        return None
    try:
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not _global_memory_status_ex(status):
            logger.debug("%s returned FALSE", SOURCE)
            return None
        return {
            "avail_page_file_mib": int(status.ullAvailPageFile) >> 20,
            "total_page_file_mib": int(status.ullTotalPageFile) >> 20,
            "avail_phys_mib": int(status.ullAvailPhys) >> 20,
            "total_phys_mib": int(status.ullTotalPhys) >> 20,
            "source": SOURCE,
        }
    except Exception as e:  # a reading is advisory; it never fails the caller
        logger.debug("%s unreadable: %s", SOURCE, e)
        return None


def read_free_commit_or_physical() -> Optional[Dict[str, Any]]:
    """`read_free_commit` on Windows; elsewhere `avail_phys_mib` / `total_phys_mib`
    from `psutil.virtual_memory()` with both commit fields None, or None when
    psutil cannot be read. Never raises."""
    if sys.platform == "win32":
        return read_free_commit()
    try:
        import psutil

        memory = psutil.virtual_memory()
        return {
            "avail_page_file_mib": None,
            "total_page_file_mib": None,
            "avail_phys_mib": int(memory.available) >> 20,
            "total_phys_mib": int(memory.total) >> 20,
            "source": PHYSICAL_SOURCE,
        }
    except Exception as e:  # a reading is advisory; it never fails the caller
        logger.debug("%s unreadable: %s", PHYSICAL_SOURCE, e)
        return None


def describe_free_commit(reading: Optional[Dict[str, Any]]) -> str:
    """One reading as the log, the failure reason and the UI print it."""
    if not reading:
        return NOT_MEASURED if sys.platform != "win32" else UNREAD
    return (
        f"free commit ullAvailPageFile {reading['avail_page_file_mib']} of "
        f"ullTotalPageFile {reading['total_page_file_mib']} MiB, free physical "
        f"ullAvailPhys {reading['avail_phys_mib']} of ullTotalPhys "
        f"{reading['total_phys_mib']} MiB ({reading.get('source', SOURCE)})"
    )


# `list_processes` answers the Agent map's 5-second poll with the card's VRAM
# (ADR-046 point 8). Each nvidia-smi read is a registered `device_probe` entry,
# so the answer reuses a reading for this long; the total is the card's and is
# kept for the backend's life once read.
_VRAM_READING_TTL_S = 30.0
_vram_reading: Dict[str, Any] = {"at": None, "wall": None, "free": None, "total": None}
_vram_reading_lock = threading.Lock()


def _reading_age(reading: Dict[str, Any], lock: threading.Lock, now: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    with lock:
        at, wall = reading["at"], reading["wall"]
    if at is None or wall is None:
        return None, None
    age = max(0.0, (time.monotonic() if now is None else now) - at)
    return wall, round(age, 1)


def vram_reading_age(now: Optional[float] = None) -> Tuple[Optional[float], Optional[float]]:
    """`(read_at, age_s)` of the cached VRAM reading: wall-clock epoch seconds of the nvidia-smi read
    and its age on the monotonic clock; `(None, None)` when no reading is cached. Never probes."""
    return _reading_age(_vram_reading, _vram_reading_lock, now)


def gpu_apps_reading_age(now: Optional[float] = None) -> Tuple[Optional[float], Optional[float]]:
    """Same as `vram_reading_age` for the GPU compute-apps reading."""
    return _reading_age(_gpu_apps_reading, _gpu_apps_lock, now)


def read_vram_mib(now: Optional[float] = None) -> Tuple[Optional[int], Optional[int]]:
    """`(free, total)` VRAM of the first NVIDIA GPU in MiB, either None without
    a signal. Reads through `_free_vram_mib` / `_total_vram_mib` of
    `llama_server_supervisor` at most once per `_VRAM_READING_TTL_S`; blocking, so
    callers run it off the event loop. A probe refused during the backend's stop
    answers `(None, None)` and is not cached."""
    # Imported here, not at the top: the supervisor module is heavy and no other reader in this file needs it.
    from . import llama_server_supervisor as engine

    when = time.monotonic() if now is None else now
    with _vram_reading_lock:
        at = _vram_reading["at"]
        if at is not None and 0 <= when - at < _VRAM_READING_TTL_S:
            return _vram_reading["free"], _vram_reading["total"]
        try:
            free = engine._free_vram_mib()
            total = _vram_reading["total"] if _vram_reading["total"] is not None else engine._total_vram_mib()
        except Exception as e:  # a reading is advisory: the registry closed, a probe failed
            logger.debug("VRAM reading unavailable: %s", e)
            return None, None
        _vram_reading.update(at=when, wall=time.time(), free=free, total=total)
        return free, total


# `card_status` (the agent tool) lists the processes the driver shows on the card. The read is a
# registered `device_probe` like the VRAM ones and reuses a reading for the same window. `apps` is
# the parsed list (empty when the card has no compute process) or None without a signal.
_gpu_apps_reading: Dict[str, Any] = {"at": None, "wall": None, "apps": None}
_gpu_apps_lock = threading.Lock()


def _parse_compute_apps(text: str) -> List[Dict[str, Any]]:
    """`pid, process_name, used_memory` rows of `nvidia-smi --query-compute-apps ... --format=csv,noheader`.
    `used_memory` is `[N/A]` under WDDM (Windows) and becomes None; a row that does not parse is skipped."""
    apps: List[Dict[str, Any]] = []
    for line in text.splitlines():
        pid_text, sep, rest = line.partition(",")
        name, sep2, used_text = rest.rpartition(",")
        if not sep or not sep2:
            continue
        try:
            pid = int(pid_text.strip())
        except ValueError:
            continue
        used_text = used_text.strip().split(" ")[0]
        # Only the file name: nvidia-smi gives the full path, which carries the user's home folder.
        apps.append({"pid": pid, "process_name": ntpath.basename(name.strip().replace("/", "\\")),
                     "used_memory_mib": int(used_text) if used_text.isdigit() else None})
    return apps


def read_gpu_compute_apps(now: Optional[float] = None) -> Optional[List[Dict[str, Any]]]:
    """The compute processes of the NVIDIA card as `{pid, process_name, used_memory_mib}` rows, `[]`
    when there is none and None without a signal (no nvidia-smi, or it failed). `used_memory_mib` is
    None where the driver gives no per-process figure (WDDM). One nvidia-smi probe per
    `_VRAM_READING_TTL_S`, a missing signal cached for the window too; blocking, so callers run it
    off the event loop. A probe refused during the backend's stop answers None and is not cached."""
    from . import llama_server_supervisor as engine

    when = time.monotonic() if now is None else now
    with _gpu_apps_lock:
        at = _gpu_apps_reading["at"]
        if at is not None and 0 <= when - at < _VRAM_READING_TTL_S:
            return _gpu_apps_reading["apps"]
        apps: Optional[List[Dict[str, Any]]] = None
        if shutil.which("nvidia-smi"):
            try:
                out = engine._device_probe(
                    ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"],
                    text=True, timeout=10,
                )
                apps = _parse_compute_apps(out.stdout or "")
            except (OSError, ValueError, subprocess.SubprocessError) as e:  # no signal, cached for the window
                logger.debug("GPU compute apps unreadable: %s", e)
            except Exception as e:  # the registry closed: advisory, answers None and is not cached
                logger.debug("GPU compute apps reading unavailable: %s", e)
                return None
        _gpu_apps_reading.update(at=when, wall=time.time(), apps=apps)
        return apps
