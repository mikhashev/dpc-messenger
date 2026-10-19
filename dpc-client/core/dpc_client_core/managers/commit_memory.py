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
"""

import ctypes
import logging
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SOURCE = "GlobalMemoryStatusEx"
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


def describe_free_commit(reading: Optional[Dict[str, Any]]) -> str:
    """One reading as the log, the failure reason and the UI print it."""
    if not reading:
        return NOT_MEASURED if sys.platform != "win32" else UNREAD
    return (
        f"free commit ullAvailPageFile {reading['avail_page_file_mib']:,} of "
        f"ullTotalPageFile {reading['total_page_file_mib']:,} MiB, free physical "
        f"ullAvailPhys {reading['avail_phys_mib']:,} of ullTotalPhys "
        f"{reading['total_phys_mib']:,} MiB ({reading.get('source', SOURCE)})"
    )
