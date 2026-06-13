"""Single-instance guard for the backend.

Two concurrent run_service.py processes fight over the Telegram bot
(getUpdates Conflict) and clash on the API/listen ports. This guard writes a
pid file on startup and refuses to start when a live backend already holds it.
A stale lock (dead pid, or a pid that is no longer a run_service process) is
treated as free.
"""

import os
import sys
from pathlib import Path
from typing import Callable, Optional

DEFAULT_LOCK = Path.home() / ".dpc" / "run_service.pid"


def is_dpc_backend_pid(pid: int) -> bool:
    """True if pid is a live process whose command line runs run_service."""
    try:
        import psutil

        proc = psutil.Process(pid)
        return "run_service" in " ".join(proc.cmdline()).lower()
    except Exception:
        # psutil missing, process gone, or access denied — treat as not-a-backend
        # (fail open so an unreadable pid never blocks a legitimate start).
        return False


def find_running_instance(
    lock_path: Path,
    is_backend: Callable[[int], bool],
    own_pid: Optional[int] = None,
) -> Optional[int]:
    """Return the pid of another live backend holding the lock, else None."""
    own = own_pid if own_pid is not None else os.getpid()
    try:
        pid = int(lock_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid == own:
        return None
    return pid if is_backend(pid) else None


def acquire(lock_path: Path = DEFAULT_LOCK) -> None:
    """Refuse to start if another backend is running; otherwise claim the lock."""
    existing = find_running_instance(lock_path, is_dpc_backend_pid)
    if existing is not None:
        print(f"[startup] Another D-PC backend is already running (PID {existing}).")
        print(
            "[startup] Refusing to start a second instance — two backends fight over "
            "the Telegram bot (getUpdates Conflict) and the API/listen ports."
        )
        print(f"[startup] Stop it first, then retry. If it is stale, delete {lock_path}.")
        sys.exit(1)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(os.getpid()), encoding="utf-8")


def release(lock_path: Path = DEFAULT_LOCK) -> None:
    """Remove the lock if it is ours (no-op for a stale/foreign lock)."""
    try:
        if lock_path.exists() and lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_path.unlink()
    except OSError:
        pass
