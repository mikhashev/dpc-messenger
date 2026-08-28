"""Supervision every process this service starts is entitled to.

Until 2026-08-26 the memory ceiling, the tree kill and the bounded drain lived
inside `shell.py`, which meant they protected exactly one call path. On that day
a tree of two python processes reached 50 753 MB and ran for fourteen minutes on
this machine without any of them firing, because it never went through
`run_shell` - and nothing in the logs could even name what started it. A
guardrail that describes itself as protecting the machine has to be reachable
from every place the machine can be loaded from, so it lives here now and
`shell.py` imports it.

What this module does NOT claim: it can only supervise processes *we* spawn. A
tree started from a terminal, a scheduler or another application is invisible to
it, and no amount of logging inside this repository would name that one.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

log = logging.getLogger(__name__)

# A command may run for its timeout; nothing said how much memory it may take
# while doing so. On 2026-08-25 an agent wrote `step1.py`, ran it twice, and the
# two copies took 84.4 and 80.9 GB of committed memory — the machine reached 0.0
# GB free and a 118.6 GB page file. Every gate we had looked at the *verb*, and
# the verb was `python step1.py`: ordinary, correctly ungated, and ruinous.
#
# The ceiling is deliberately generous. A guardrail that stops a real build is
# switched off within a week, and the failure it exists for is two orders of
# magnitude above ordinary work, not two times.
def _read_memory_ceiling() -> int:
    """Parse the ceiling, and never fail the import over it.

    A bad value used to raise at module scope, which takes the whole tools
    package — every agent tool — down for a misconfigured limit.
    """
    raw = os.environ.get("DPC_SHELL_MEMORY_LIMIT_MB", "8192")
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning(
            "DPC_SHELL_MEMORY_LIMIT_MB is %r, which is not a number — "
            "using the default 8192 MB", raw,
        )
        return 8192


_MEMORY_CEILING_MB = _read_memory_ceiling()
_MEMORY_POLL_SECONDS = 2.0
_PSUTIL_MISSING_ANNOUNCED = False


def _process_memory_mb(proc) -> float:
    """One process's footprint, by the larger of the two things that can hurt.

    RSS and commit are different quantities and neither dominates: RSS counts
    shared pages the process never committed, commit counts pages it reserved
    and never touched. On Windows `private` is the commit charge; POSIX has no
    cheap equivalent (`vms` counts every reservation, so a ceiling on it would
    fire on ordinary work) and RSS stands alone there.
    """
    info = proc.memory_info()
    used = info.rss
    committed = getattr(info, "private", 0)  # Windows only
    return max(used, committed) / (1024 * 1024)


def _tree_memory_mb(process: "subprocess.Popen") -> float:
    """Memory held by the command and everything it started, in MB.

    The whole tree, because the runaway was a grandchild: a parent that reads
    only its own usage watches an innocent shell while the machine fills up.
    """
    try:
        import psutil
    except ImportError:
        # A guard that is silently absent is worse than no guard: the operator
        # believes there is a ceiling. Say it once, then behave as if switched off.
        global _PSUTIL_MISSING_ANNOUNCED
        if not _PSUTIL_MISSING_ANNOUNCED:
            _PSUTIL_MISSING_ANNOUNCED = True
            log.warning(
                "psutil is not installed, so run_shell has NO memory ceiling; "
                "only the timeout limits a command"
            )
        return 0.0
    try:
        root = psutil.Process(process.pid)
        procs = [root] + root.children(recursive=True)
    except Exception:
        return 0.0
    total = 0.0
    for proc in procs:
        try:
            total += _process_memory_mb(proc)
        except Exception:
            continue  # it exited between listing and reading; not an error
    return total


def _watch_memory(process: "subprocess.Popen", ceiling_mb: int, verdict: dict) -> None:
    """Kill the tree if it grows past the ceiling. Runs on a daemon thread.

    Polling rather than an OS limit on purpose: a Windows Job Object and a
    POSIX RLIMIT_AS are both better instruments and neither is the same
    instrument, so a limit expressed that way would mean two behaviours to
    reason about and one of them untested here. A poll is the same code and the
    same number on all three platforms; its cost is granularity, and against a
    process that spent nine and a half hours above the line, granularity of two
    seconds is not the weak part.
    """
    while process.poll() is None:
        used = _tree_memory_mb(process)
        if ceiling_mb and used > ceiling_mb:
            verdict["exceeded_mb"] = round(used)
            log.warning(
                "the command tree of pid %s reached %d MB, over the %d MB ceiling — killing it",
                process.pid, round(used), ceiling_mb,
            )
            _kill_process_tree(process)
            return
        time.sleep(_MEMORY_POLL_SECONDS)


def _parent_map_from_proc() -> dict:
    """child pids by parent pid, read from /proc. Empty where there is none."""
    proc = os.path.join(os.sep, "proc")
    if not os.path.isdir(proc):
        return {}
    children: dict = {}
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "status"), encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("PPid:"):
                        children.setdefault(int(line.split()[1]), []).append(int(name))
                        break
        except (OSError, ValueError):
            continue  # the process ended while we were reading it
    return children


def _parent_map_from_ps(pid: int) -> dict:
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.warning("could not list processes to find the children of %s: %s", pid, exc)
        return {}
    children: dict = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return children


def _descendants(pid: int) -> list:
    """Every descendant of `pid`, by parent link, collected before any kill.

    `setsid` changes a process's session and group and leaves its parent link
    alone, so this walk finds exactly what `killpg` cannot reach. It has to run
    **first**: once the parent dies the survivors are reparented to init and
    there is nothing left to walk from.

    `/proc` before `ps`, and the order is not a preference. A slim container is
    where an agent's work actually runs and `python:3.12-slim` carries no `ps`
    at all — measured 2026-08-27, where the first version of this walk degraded
    to a warning and the escaped grandchild lived. `/proc` is part of Linux;
    `ps` is the fallback for macOS, which has no `/proc` and always has `ps`.
    """
    children = _parent_map_from_proc() or _parent_map_from_ps(pid)
    if not children:
        return []
    found, stack = [], [pid]
    while stack:
        for child in children.get(stack.pop(), ()):
            found.append(child)
            stack.append(child)
    return found


def _kill_process_tree(process: "subprocess.Popen") -> str:
    """Kill the command and everything it started. Returns what happened, for the agent.

    Killing the direct child is not enough and never was: the child is a shell,
    and what the agent actually launched is the shell's child. `taskkill /T`
    walks the Windows tree; on POSIX the process got its own session at spawn,
    so one `killpg` reaches every descendant that did not deliberately break
    away.
    """
    import signal

    pid = process.pid
    if platform.system() == "Windows":
        try:
            done = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
            )
            # Read the exit code before claiming the tree died: taskkill is
            # denied access to some children, and the sentence was asserted
            # anyway — false in exactly the partial-kill case the bounded
            # drain exists for.
            if done.returncode == 0:
                return "the command and its descendants were killed"
            log.warning(
                "taskkill on pid %s exited %s: %s", pid, done.returncode,
                (done.stderr or b"").decode("utf-8", "replace").strip()[:200],
            )
            return (
                "the kill did not reach the whole tree "
                f"(taskkill exited {done.returncode}); a descendant may survive"
            )
        except Exception as exc:
            log.warning("taskkill on pid %s failed: %s", pid, exc)
    else:
        # Measured 2026-08-27 against this very function in a Linux container:
        # a grandchild started with `start_new_session=True` outlived the killpg
        # and held the pipes for the whole bounded drain, which is why the list
        # is taken before anything dies rather than after.
        strays = _descendants(pid)
        killed_group = False
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            killed_group = True
        except Exception as exc:
            log.warning("killpg on pid %s failed: %s", pid, exc)
        signalled = []
        for stray in strays:
            try:
                os.kill(stray, signal.SIGKILL)
                signalled.append(stray)
            except ProcessLookupError:
                pass  # the group kill already reached it
            except Exception as exc:
                log.warning("could not kill descendant %s of %s: %s", stray, pid, exc)
        if killed_group:
            if signalled:
                # Counted, not characterised. A signal reaches a process the
                # group kill has already killed but not yet reaped, so «this one
                # had left the group» is not establishable from here and the
                # sentence the agent reads must not claim it.
                log.info(
                    "signalled %d descendant(s) of %s directly after the group kill: %s",
                    len(signalled), pid, signalled,
                )
            return "the command and its process group were killed"

    # Last resort: at least the direct child, so we do not leave it running
    # while telling the agent we cleaned up.
    try:
        process.kill()
        return "only the command itself could be killed; a descendant may survive"
    except Exception:
        return "the command could not be killed"


_DRAIN_AFTER_KILL_SECONDS = 5


def _drain_after_kill(process: "subprocess.Popen") -> tuple:
    """Collect whatever output exists, and never wait on it for ever.

    After a successful tree kill every handle-holder is gone and EOF arrives at
    once. After a partial one it does not, and this is the exact place the old
    code waited for it with no bound. A few seconds is enough to pick up what a
    dead process already wrote; past that the output is not worth a hung agent.
    """
    try:
        return process.communicate(timeout=_DRAIN_AFTER_KILL_SECONDS)
    except subprocess.TimeoutExpired:
        log.warning(
            "pipes still open %ss after killing pid %s — a descendant escaped the kill; "
            "abandoning the output rather than waiting",
            _DRAIN_AFTER_KILL_SECONDS, process.pid,
        )
        return ("", "")
    except Exception as exc:
        log.warning("draining pid %s after kill failed: %s", process.pid, exc)
        return ("", "")

@dataclass
class SupervisedRun:
    """What happened, in the terms a caller has to report to an agent."""

    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    exceeded_mb: Optional[int] = None
    killed: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.exceeded_mb is None


def run_supervised(
    cmd,
    *,
    launcher: str,
    timeout: int,
    cwd: Optional[str] = None,
    shell: bool = False,
    env: Optional[dict] = None,
    ceiling_mb: Optional[int] = None,
    popen_kwargs: Optional[dict] = None,
) -> SupervisedRun:
    """Spawn, watch, and never wait unbounded on anything.

    `subprocess.run(timeout=…)` is what this replaces, and on Windows that call
    is the defect itself: on TimeoutExpired it kills the direct child only and
    then calls `communicate()` **again with no timeout**. A surviving grandchild
    holds the inherited pipe, EOF never arrives, and the caller waits for ever.

    `launcher` is not decoration. A spawn that no log names cannot be attributed
    once the process is gone, which is exactly the position this machine was in
    on 2026-08-26, so every process started here announces who asked for it.
    """
    ceiling = _MEMORY_CEILING_MB if ceiling_mb is None else ceiling_mb
    kwargs = dict(popen_kwargs or {})
    if platform.system() != "Windows":
        # A session of its own, so one killpg reaches the whole descendant set.
        kwargs.setdefault("start_new_session", True)

    process = subprocess.Popen(
        cmd,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
        # Nobody is sitting at this process; a prompt would be asked of the
        # operator's console and waited for until the timeout ran out.
        stdin=subprocess.DEVNULL,
        **kwargs,
    )
    log.info(
        "spawned pid %s for %s (timeout %ss, ceiling %s MB): %.120s",
        process.pid, launcher, timeout, ceiling or "off",
        cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd),
    )

    verdict: dict = {}
    if ceiling and ceiling > 0:
        threading.Thread(
            target=_watch_memory,
            args=(process, ceiling, verdict),
            name=f"dpc-memory-watch-{process.pid}",
            daemon=True,
        ).start()

    run = SupervisedRun()
    try:
        run.stdout, run.stderr = process.communicate(timeout=timeout)
        run.returncode = process.returncode
    except subprocess.TimeoutExpired:
        run.timed_out = True
        run.killed = _kill_process_tree(process)
        run.stdout, run.stderr = _drain_after_kill(process)
        run.returncode = process.returncode
    if verdict.get("exceeded_mb"):
        run.exceeded_mb = verdict["exceeded_mb"]
        if not run.killed:
            run.killed = "the command and its descendants were killed"
    return run
