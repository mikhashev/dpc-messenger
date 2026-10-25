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

import configparser
import logging
import os
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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
_DEFAULT_MEMORY_CEILING_MB = 8192
_CEILING_ENV = "DPC_SHELL_MEMORY_LIMIT_MB"
# The generic name every config.ini key accepts (`Settings.get`); the older
# name above wins over it, because it is the one already set on machines.
_CEILING_ENV_GENERIC = "DPC_SYSTEM_SHELL_MEMORY_LIMIT_MB"
_CEILING_SECTION, _CEILING_KEY = "system", "shell_memory_limit_mb"

# What a refusal tells the agent, kept in one place for every caller.
CEILING_HINT = (
    f"Raise {_CEILING_KEY} in [{_CEILING_SECTION}] of config.ini (the next "
    f"command reads it, no restart; {_CEILING_ENV} in the environment overrides it)"
)

# (path, mtime_ns, size) -> the raw value, so a command costs one stat().
_config_cache: tuple = (None, None)
_bad_ceilings_announced: set = set()


def _config_ceiling_raw() -> Optional[str]:
    """The key's raw value from config.ini, re-read only when the file changed.

    Not `Settings(home)`: its constructor rewrites a file it cannot parse with
    the defaults, and a read that lands inside `Settings.set()`'s truncate-and-
    write would hand it exactly that file. This reads with the same parser and
    never writes; an unreadable file keeps the last value it read.
    """
    global _config_cache
    path = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc")) / "config.ini"
    try:
        st = path.stat()
    except OSError:
        return None
    stamp = (str(path), st.st_mtime_ns, st.st_size)
    cached_stamp, cached_raw = _config_cache
    if stamp == cached_stamp:
        return cached_raw
    if cached_stamp is None or cached_stamp[0] != stamp[0]:
        cached_raw = None  # another file's value is not this one's last
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    try:
        parser.read(path)
        if not parser.sections():  # Settings' own test for an invalid file
            raise configparser.Error("no sections")
    except (configparser.Error, OSError, UnicodeDecodeError) as exc:
        log.warning("%s could not be read for the memory ceiling (%s); "
                    "keeping the last value read", path, exc)
        _config_cache = (stamp, cached_raw)
        return cached_raw
    raw = parser.get(_CEILING_SECTION, _CEILING_KEY, fallback=None)
    _config_cache = (stamp, raw)
    return raw


def _read_memory_ceiling() -> int:
    """The ceiling for the next command: environment, config.ini, then 8192.

    Read at every command, so a change to config.ini applies at once. It never
    raises: a bad value used to raise at module scope, which takes the whole
    tools package — every agent tool — down for a misconfigured limit.
    """
    for source, raw in (
        (_CEILING_ENV, os.environ.get(_CEILING_ENV)),
        (_CEILING_ENV_GENERIC, os.environ.get(_CEILING_ENV_GENERIC)),
    ):
        if raw is not None:
            break
    else:
        source = f"[{_CEILING_SECTION}] {_CEILING_KEY}"
        raw = _config_ceiling_raw()
        if raw is None:
            return _DEFAULT_MEMORY_CEILING_MB
    try:
        return int(raw)
    except (TypeError, ValueError):
        # Once per value: this runs at every command.
        if (source, raw) not in _bad_ceilings_announced:
            _bad_ceilings_announced.add((source, raw))
            log.warning(
                "%s is %r, which is not a number — using the default %s MB",
                source, raw, _DEFAULT_MEMORY_CEILING_MB,
            )
        return _DEFAULT_MEMORY_CEILING_MB


# A pin for tests; None in service, where the ceiling is read at each command.
_MEMORY_CEILING_MB: Optional[int] = None
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


def _watch_memory(
    process: "subprocess.Popen",
    ceiling_mb: int,
    verdict: dict,
    stop: Optional[threading.Event] = None,
) -> None:
    """Kill the tree if it grows past the ceiling. Runs on a daemon thread.

    Polling rather than an OS limit on purpose: a Windows Job Object and a
    POSIX RLIMIT_AS are both better instruments and neither is the same
    instrument, so a limit expressed that way would mean two behaviours to
    reason about and one of them untested here. A poll is the same code and the
    same number on all three platforms; its cost is granularity, and against a
    process that spent nine and a half hours above the line, granularity of two
    seconds is not the weak part.

    `stop` wakes the sleep the moment the caller has its answer. Without it the
    caller's `join` waited out the rest of a poll interval after every process
    that ended sooner - measured 2026-09-29: 2.02 s added to each djvutxt and
    djvused call of `read_document`, about 4 s per DjVu page.
    """
    while process.poll() is None:
        used = _tree_memory_mb(process)
        if ceiling_mb and used > ceiling_mb:
            verdict["exceeded_mb"] = round(used)
            log.warning(
                "the command tree of pid %s reached %d MB, over the %d MB ceiling — killing it",
                process.pid, round(used), ceiling_mb,
            )
            # The real verdict: a partial kill must not be reported as a full one.
            verdict["killed"] = _kill_process_tree(process)
            return
        if stop is not None:
            if stop.wait(_MEMORY_POLL_SECONDS):
                return
        else:
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


# `subprocess.CREATE_NO_WINDOW` exists only on Windows; the value is spelled
# here so the Windows branch can be tested by injection on any platform.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _command_text(cmd) -> str:
    return cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)


# ------------------------------------------- what a timeout leaves behind
#
# A command killed at its timeout is the one whose output the agent most needs
# to see the end of: that is where it stopped. Measured 2026-10-24: Ark's
# `llama-tokenize … | findstr … | more +0` captured 1 151 755 chars of stdout
# and the agent was shown the first 50 000 — nothing that said `more` was
# sitting at its prompt waiting for a key.

TAIL_LINES = 20
TAIL_CHARS = 2000

_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PAGER_PROMPTS = (
    re.compile(r"^--\s*More\b.*--\s*$"),           # Windows more: `-- More  --`, `-- More (12%) --`
    re.compile(r"^--More--(\(\d+%\))?\s*$"),       # util-linux more
    re.compile(r"^(:|\(END\))\s*$"),               # less
)

PAGER_HINT = (
    "The output ends at a pager prompt ({prompt!r}): the command was waiting "
    "for a key, and {tool} has no keyboard - drop the `| more` (or other "
    "pager) and {then}."
)


def pager_prompt_at_end(text: Optional[str]) -> Optional[str]:
    """The pager prompt the text ends at, or None.

    Windows `more` writes its prompt to stdout even when stdout is a pipe,
    and then waits for a key on the console. A POSIX pager with a pipe for
    stdout copies instead of paging, so the other two patterns are a belt.
    """
    if not text:
        return None
    last = _ANSI_CSI.sub("", text.rstrip("\r\n").rsplit("\n", 1)[-1]).strip()
    if last and any(p.match(last) for p in _PAGER_PROMPTS):
        return last
    return None


def output_tail(text: str, lines: int = TAIL_LINES, max_chars: int = TAIL_CHARS) -> str:
    """The last `lines` lines of `text`, never longer than `max_chars`."""
    tail = "\n".join(text.rstrip("\r\n").split("\n")[-lines:])
    return tail[-max_chars:]


def _lines(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") else 1) if text else 0


def captured_sizes(run: "SupervisedRun") -> str:
    """One sentence: how much of each stream was captured before the kill."""
    def one(name: str, text: str) -> str:
        if not text:
            return f"{name} empty"
        return f"{name} {len(text):,} chars in {_lines(text):,} lines"
    return f"Captured before the kill: {one('stdout', run.stdout or '')}, {one('stderr', run.stderr or '')}."


def timeout_details(
    run: "SupervisedRun", *, tool: str, stored: Optional[dict] = None,
) -> list:
    """What a timed-out run tells the agent after its first sentence.

    The sizes; the pager sentence when the output ends at a pager prompt;
    where the full captured streams are kept, when the caller stored them
    (`stored` maps "stdout"/"stderr" to an address read_file resolves); and
    the last lines of each stream that is longer than its tail.
    """
    out = [captured_sizes(run)]
    prompt = pager_prompt_at_end(run.stdout) or pager_prompt_at_end(run.stderr)
    if prompt:
        out.append(PAGER_HINT.format(
            prompt=prompt, tool=tool,
            then="read the stored output instead" if stored else "run it again without it",
        ))
    if stored:
        where = ", ".join(f"{name} at {addr}" for name, addr in stored.items())
        out.append(f"Full captured output: {where} (read_file reads it).")
    for name in ("stdout", "stderr"):
        text = getattr(run, name) or ""
        tail = output_tail(text)
        if text and len(tail) < len(text.rstrip("\r\n")):
            out.append(f"[last {tail.count(chr(10)) + 1} lines of {name}]\n{tail}")
    return out


@dataclass
class SupervisedRun:
    """What happened, in the terms a caller has to report to an agent."""

    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    exceeded_mb: Optional[int] = None
    # The ceiling this run was held to, so a report names the one applied.
    # 0 = the ceiling was off; None = not set (built outside run_supervised).
    ceiling_mb: Optional[int] = None
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
    log_as: Optional[str] = None,
) -> SupervisedRun:
    """Spawn, watch, and never wait unbounded on anything.

    `subprocess.run(timeout=…)` is what this replaces, and on Windows that call
    is the defect itself: on TimeoutExpired it kills the direct child only and
    then calls `communicate()` **again with no timeout**. A surviving grandchild
    holds the inherited pipe, EOF never arrives, and the caller waits for ever.

    `launcher` is not decoration. A spawn that no log names cannot be attributed
    once the process is gone, which is exactly the position this machine was in
    on 2026-08-26, so every process started here announces who asked for it.
    `log_as` is the command the log names when `cmd` is a wrapper around it.
    """
    ceiling = ceiling_mb if ceiling_mb is not None else _MEMORY_CEILING_MB
    if ceiling is None:
        ceiling = _read_memory_ceiling()
    kwargs = dict(popen_kwargs or {})
    if platform.system() == "Windows":
        # A hidden console of its own, not the service's. A closed stdin is not
        # enough: `more` at its `-- More --` prompt opens CONIN$ itself, turns
        # PROCESSED|LINE|ECHO off to read a key, and when the timeout tree-kills
        # it the console it shared stays raw — on 2026-10-22 and 2026-10-24 that
        # was the service's, and Ctrl+C stopped reaching the backend. A caller's
        # own creationflags are kept as given.
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    else:
        # A session of its own, so one killpg reaches the whole descendant set.
        # It is also why a pager cannot reach the operator's terminal here: a
        # new session has no controlling terminal, so opening /dev/tty fails
        # (ENXIO), and a pager whose stdout is a pipe pages nothing anyway.
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
        process.pid, launcher, timeout, ceiling or "off", log_as or _command_text(cmd),
    )

    verdict: dict = {}
    watch_thread: Optional[threading.Thread] = None
    watch_stop = threading.Event()
    if ceiling and ceiling > 0:
        watch_thread = threading.Thread(
            target=_watch_memory,
            args=(process, ceiling, verdict, watch_stop),
            name=f"dpc-memory-watch-{process.pid}",
            daemon=True,
        )
        watch_thread.start()

    run = SupervisedRun(ceiling_mb=ceiling)
    try:
        run.stdout, run.stderr = process.communicate(timeout=timeout)
        run.returncode = process.returncode
    except subprocess.TimeoutExpired:
        run.timed_out = True
        run.killed = _kill_process_tree(process)
        run.stdout, run.stderr = _drain_after_kill(process)
        run.returncode = process.returncode
        # The ceiling kill always said so here; the timeout kill did not, and
        # three agent commands that left the console raw were visible only in
        # the agents' tools.jsonl.
        prompt = pager_prompt_at_end(run.stdout) or pager_prompt_at_end(run.stderr)
        log.warning(
            "the command tree of pid %s for %s ran past its %ss timeout - %s; "
            "%d chars of stdout and %d of stderr captured%s: %.200s",
            process.pid, launcher, timeout, run.killed,
            len(run.stdout or ""), len(run.stderr or ""),
            f"; the output ends at a pager prompt {prompt!r}" if prompt else "",
            (log_as or _command_text(cmd)).replace("\r", " ").replace("\n", " "),
        )
    if watch_thread is not None:
        # communicate() can return before the watcher has written its verdict.
        # A watcher mid-kill finishes it (it is past its sleep); one asleep is
        # woken and leaves without a poll, so a short command costs no interval.
        watch_stop.set()
        watch_thread.join(timeout=15)
    if verdict.get("exceeded_mb"):
        run.exceeded_mb = verdict["exceeded_mb"]
        if not run.killed:
            run.killed = verdict.get("killed") or "the command and its descendants were killed"
    return run
