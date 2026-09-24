"""
DPC Agent — Shell Tool (run_shell).

Executes shell commands in a subprocess with timeout and output capture.
Cross-platform: uses cmd.exe on Windows, /bin/sh on Unix.
Restricted tool — requires explicit enable in privacy_rules.json.

Safety guardrails (ADR-030): 3-tier command classification.
  Tier 0 — auto-approve (safe read-only commands)
  Tier 1 — goes to the approval queue: the caller is asked and the command runs
           only if a person says yes
  Tier 2 — hard block (catastrophic/destructive commands)
"""

from __future__ import annotations

import fnmatch
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
import unicodedata
import warnings
from typing import List, NamedTuple, Optional, Tuple

from .registry import ToolEntry, ToolContext, agent_display_name, conversation_origin
# Moved to `process.py` on 2026-08-26 so they cover every spawn this service
# makes, not only this file's. Imported under their old names on purpose: the
# tests monkeypatch `shell._MEMORY_CEILING_MB`, and a module global is what they
# have to be able to reach.
from .process import (  # noqa: F401  (re-exported for the tests that patch them here)
    _DRAIN_AFTER_KILL_SECONDS,
    _MEMORY_CEILING_MB,
    _MEMORY_POLL_SECONDS,
    _drain_after_kill,
    _kill_process_tree,
    _process_memory_mb,
    _read_memory_ceiling,
    _tree_memory_mb,
    _watch_memory,
    run_supervised,
)

log = logging.getLogger(__name__)

MAX_OUTPUT = 50_000  # chars

# ---------------------------------------------------------------------------
# ADR-030: Safety guardrails — hardcoded, not configurable
# ---------------------------------------------------------------------------

# Tier 2 — unconditional block (catastrophic commands)
HARDLINE_PATTERNS: list[re.Pattern] = [
    # Mass delete (Linux)
    re.compile(r"\brm\b.*\s+-[a-zA-Z]*r[a-zA-Z]*f|rm\b.*\s+-[a-zA-Z]*f[a-zA-Z]*r", re.I),
    # …and the same flags spelled apart. `rm -r -f dir` did exactly what
    # `rm -rf dir` does and classified as tier0, because the pattern above
    # wants both letters inside one token.
    re.compile(r"\brm\b(?=.*\s-[a-zA-Z]*r\b)(?=.*\s-[a-zA-Z]*f\b)", re.I),
    re.compile(r"\brm\b\s+.*(/|~|\$HOME)", re.I),
    # Mass delete (Windows). `/s` is matched anywhere in the segment, not only
    # immediately after the verb: `del foo.txt /s /q` deletes recursively and
    # `\bdel\b\s+/s` never saw it.
    re.compile(r"\b(rd|rmdir)\b.*\s/s\b", re.I),
    # `erase` is cmd.exe's own alias for `del`.
    re.compile(r"\b(del|erase)\b.*\s/s\b", re.I),
    # Mass delete (PowerShell) — and PowerShell is not a Windows-only surface:
    # `pwsh` has shipped on Linux and macOS for years, so a rule that names only
    # `powershell` covers one platform of three. Recurse is the mass part; a
    # single-file Remove-Item stays ordinary work.
    re.compile(r"\bRemove-Item\b(?=.*\s-Recurse\b)", re.I),
    re.compile(r"\bRemove-Item\b(?=.*\s-Force\b)(?=.*[\\/]\*)", re.I),
    # Disk format / erase
    re.compile(r"\bmkfs\b", re.I),
    # Switches may sit between the verb and the drive — `format /q C:`,
    # `format /fs:ntfs D:`. Only switch tokens are allowed in between, so
    # `format-json report C:\\out` stays ordinary work.
    re.compile(r"\bformat\b(?:\s+/\S+)*\s+[A-Za-z]:", re.I),
    re.compile(r"\b(Format-Volume|Clear-Disk|Initialize-Disk|Remove-Partition)\b", re.I),
    re.compile(r"\bdiskutil\s+(eraseDisk|partitionDisk|secureErase)", re.I),
    # Raw device write
    re.compile(r"\bdd\b.*\bof=/dev/", re.I),
    re.compile(r">\s*/dev/sd", re.I),
    # Shutdown / reboot
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I),
    re.compile(r"\binit\s+[06]\b"),
    # Kill all. `-1` is the *target* only when it is the last argument — as a
    # first one it is SIGHUP, and `kill -1 1234` is an ordinary signal. The
    # signal may be spelled any of the ways `kill` accepts, so it is not
    # required to be `-9`.
    re.compile(r"""\bkill\b.*\s-1\s*["']?\s*$"""),
    # WSL escape (Windows → Linux breakout)
    re.compile(r"\bwsl\b", re.I),
]

# A wrapper's own switches stand between its name and the switch that
# introduces the command it runs: `cmd /s /c`, `cmd /d /s /c`, `bash -lc`,
# `bash --norc -c`, `sh -ec`, `zsh -ic`. Every rule in this file used to want
# `-c` or `/c` to follow the name immediately, and each spelling above was
# measured tier0 — silent — with a kill of this service inside it on
# 2026-09-21. The command switch may also be the last letter of a short-option
# cluster, which is what `-lc`, `-ec`, `-ic` and `-xec` are; PowerShell's rule
# already tolerated switches in between, and is left alone.
_SHELL_NAMES = r"bash|sh|zsh|fish|dash|ksh"
_POSIX_C = r"(?:\s+--?[A-Za-z][\w-]*)*\s+-[A-Za-z]*c"
_CMD_C = r"(?:\s+/[A-Za-z](?::\S+)?)*\s+/[ck]"

# Tier 1 / Tier 2 in v1 — dangerous patterns (blocked in v1, approval in v2)
DANGEROUS_PATTERNS: list[re.Pattern] = [
    # Privilege escalation
    re.compile(r"\b(sudo|su|runas|gsudo|pkexec)\b", re.I),
    # Subshell invocation (arbitrary code execution). `dash` and `ksh` are the
    # same wrapper and were missing; so was `bash.exe`, which is how the shell
    # is spelled on the fleet's own platform.
    re.compile(rf"\b({_SHELL_NAMES})(?:\.exe)?{_POSIX_C}\b", re.I),
    # `cmd.exe` is the same wrapper, and `/k` is it with the window left open.
    re.compile(rf"\bcmd(?:\.exe)?{_CMD_C}\b", re.I),
    re.compile(r"\b(python|python3|py)\s+-c\b", re.I),
    re.compile(r"\bnode\s+-e\b", re.I),
    # PowerShell's inline-code wrapper. Every other shell's had a rule and this
    # one did not. The parameter is any unambiguous prefix of `-Command`,
    # because PowerShell resolves them: `-c`, `-co`, `-comm` all run code.
    # `-Confirm` does not match — its `n` breaks the chain before a boundary.
    re.compile(r"\b(powershell|pwsh)\b.*\s-c(?:o(?:m(?:m(?:a(?:n(?:d)?)?)?)?)?)?\b", re.I),
    # Encoded commands — both spellings of the shell, on every platform.
    re.compile(r"\b(powershell|pwsh)\b.*(-enc|-encodedcommand)", re.I),
    # Download-and-run through PowerShell, the shape `curl | sh` covers on POSIX.
    re.compile(r"\b(iex|Invoke-Expression)\b", re.I),
    # Irrecoverable overwrite. Not a hard block: shredding one file inside the
    # sandbox is legitimate, and the catastrophic form is the path, which the
    # `rm … /` rule above already covers.
    re.compile(r"\bshred\b", re.I),
    # Mass delete by search. `find / -name "*" -delete` is a whole-disk wipe
    # that no `rm` pattern sees.
    re.compile(r"\bfind\b.*\s-delete\b", re.I),
    re.compile(r"\bfind\b.*-exec\s+rm\b", re.I),
    # Download + execute — checked in CROSS_SEGMENT_PATTERNS (spans pipe boundary)
    # Registry (Windows)
    re.compile(r"\breg\s+(delete|add)\b", re.I),
    re.compile(r"\bregedit\b", re.I),
    # User management
    re.compile(r"\bnet\s+(user|localgroup)\b", re.I),
    re.compile(r"\buserdel\b", re.I),
    # Git destructive
    re.compile(r"\bgit\s+push\b.*--force\b", re.I),
    re.compile(r"\bgit\s+reset\b.*--hard\b", re.I),
    re.compile(r"\bgit\s+clean\b.*-[a-zA-Z]*f", re.I),
    re.compile(r"\bgit\s+branch\b.*\s+-D\b", re.I),
    # Service control
    re.compile(r"\b(systemctl\s+(stop|disable)|sc\s+delete|net\s+stop)\b", re.I),
    # macOS system security
    re.compile(r"\bcsrutil\s+disable\b", re.I),
    re.compile(r"\blaunchctl\s+(unload|remove)\b", re.I),
    # Docker (arbitrary code execution surface)
    re.compile(r"\bdocker\b", re.I),
]

# Fork bomb needs special detection (bash function syntax not reliably regex-matchable)
_FORK_BOMB_SIGS = [":()", ":|:", "& };"]


def _normalize_command(command: str) -> str:
    """NFKC normalize + strip ANSI escapes."""
    normalized = unicodedata.normalize("NFKC", command)
    normalized = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", normalized)
    return normalized


_SEGMENT_OPERATORS = ("||", "&&", "|", ";", "&", "\r", "\n")


def _split_segments(command: str) -> list[str]:
    """Split command by pipe/chain operators to check each segment.

    A **newline separates too**. It did not, so a two-line command was a
    single segment on every platform. That happened to be safer for the
    patterns we have — an unanchored HARDLINE still matches inside the whole
    blob — and it is safety by accident: any pattern anchored at the start of
    a line, and any future check that assumes a segment is one command, was
    blind to it.

    Quotes are honoured: an operator inside them is part of an argument, not a
    boundary. Splitting `cd "C:/R&D/tools"` at the `&` gave the cwd tracker a
    confident, wrong directory, and a wrong-but-truthy answer never trips a
    fail-closed net.

    Grouping punctuation is stripped from the edges, so `( cd sub` is still a cd.
    """
    parts, buf, quote, i = [], [], "", 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        hit = next((op for op in _SEGMENT_OPERATORS if command.startswith(op, i)), None)
        if hit:
            parts.append("".join(buf))
            buf = []
            i += len(hit)
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [cleaned for cleaned in (_strip_grouping(part) for part in parts) if cleaned]


def _strip_grouping(segment: str) -> str:
    """One command, without the shell's grouping punctuation around it."""
    return segment.strip().strip("(){}").strip()


def _is_fork_bomb(command: str) -> bool:
    """Detect fork bomb patterns."""
    return all(sig in command for sig in _FORK_BOMB_SIGS)


CROSS_SEGMENT_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(curl|wget)\b.*\|\s*(sh|bash|python)", re.I),
]


def _get_tier1_whitelist(ctx: Optional["ToolContext"] = None) -> list[str]:
    """Load per-agent Tier 1 whitelist from privacy_rules.json."""
    if not ctx or not ctx.firewall:
        log.debug("tier1_whitelist: no ctx/firewall, returning empty")
        return []
    try:
        _profile = getattr(getattr(ctx, "_agent", None), "_firewall_profile", None)
        profile_name = _profile or "default"
        wl = ctx.firewall.get_tool_setting(
            "run_shell", "tier1_whitelist", profile_name=profile_name, default=[]
        )
        if isinstance(wl, list):
            log.debug("tier1_whitelist[%s]: %s", profile_name, wl)
            return wl
    except Exception as e:
        log.debug("tier1_whitelist load error: %s", e)
    log.debug("tier1_whitelist: no whitelist found for profile")
    return []


def _is_whitelisted(command: str, whitelist: list[str]) -> bool:
    """Does this **one segment** start with a whitelisted entry?

    Callers pass the segment that triggered the finding, never the whole line.
    Passing the line is a prefix-auth bypass: an entry auto-approving `git`
    also auto-approves whatever is chained after it.
    """
    normalized = _normalize_command(command).strip().lower()
    for entry in whitelist:
        if normalized.startswith(entry.lower()):
            return True
    return False


# ---------------------------------------------------------------------------
# Killing a process, and never this one
# ---------------------------------------------------------------------------


class _ServiceIdentity(NamedTuple):
    """Who «this service» is, in the terms a kill command can name it."""

    own_pid: int
    ancestors: tuple          # pids above this one, nearest first
    names: frozenset          # lowercase executable basenames, ours and theirs
    cmdline: str              # what this process was started with


_SERVICE_IDENTITY: Optional[_ServiceIdentity] = None
_ANCESTOR_LIMIT = 16
_DESCRIPTION_LIMIT = 120


def _service_identity() -> _ServiceIdentity:
    """This process, the processes above it, and the names they run under.

    The first check in this gate that asks the operating system a question
    rather than matching a string, so it carries two boundaries. Parent links
    and executable names resolve per-platform — psutil, else `/proc`, else this
    process alone. And `os.getpid()` is the *service's* pid only because agents
    run inside the service process; move them out and this stops recognising
    what it protects. `single_instance.py`, `tool_ledger.py` and
    `process.py:_kill_process_tree` are the other three answers to «this is
    me», and the invariant they share is that an agent may signal only what its
    runtime started.

    One cached provider, so a test can answer for a service it invents.
    """
    global _SERVICE_IDENTITY
    if _SERVICE_IDENTITY is None:
        ancestors = _ancestor_processes(os.getpid())
        names = {os.path.basename(sys.executable), _own_process_name()}
        # Only an ancestor of our own kind lends its name: the venv launcher
        # and the interpreter it starts are one service under one name. The
        # terminal, the editor and Explorer above them are not — their pids
        # stay protected, their names are an ordinary question.
        stems = {_name_stem(n) for n in names if n}
        names.update(name for _pid, name in ancestors if name and _name_stem(name) in stems)
        _SERVICE_IDENTITY = _ServiceIdentity(
            own_pid=os.getpid(),
            ancestors=tuple(pid for pid, _name in ancestors),
            names=frozenset(n.lower() for n in names if n),
            cmdline=" ".join(sys.argv),
        )
    return _SERVICE_IDENTITY


def _own_process_name() -> str:
    """What the OS calls this process, which is not always `sys.executable`."""
    try:
        import psutil

        return psutil.Process(os.getpid()).name()
    except Exception:
        return _proc_status(os.getpid())[1]


def _ancestor_processes(pid: int) -> list:
    """(pid, name) for every process above this one, nearest first.

    On Windows a venv launcher and the interpreter it starts are one service
    wearing two pids; on POSIX the shell above us is not an agent's to signal.
    """
    try:
        import psutil
    except ImportError:
        return _ancestors_from_proc(pid)
    found: list = []
    seen = {pid}
    try:
        proc = psutil.Process(pid)
        for _ in range(_ANCESTOR_LIMIT):
            proc = proc.parent()
            if proc is None or proc.pid in seen or proc.pid <= 0:
                break
            seen.add(proc.pid)
            try:
                name = proc.name()
            except Exception:
                name = ""
            found.append((proc.pid, name))
    except Exception as exc:
        log.debug("could not walk the parents of %s: %s", pid, exc)
        return found or _ancestors_from_proc(pid)
    return found


def _proc_status(pid: int) -> Tuple[int, str]:
    """(ppid, name) from /proc, or (0, "") where there is no /proc."""
    path = os.path.join(os.sep, "proc", str(pid), "status")
    parent, name = 0, ""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PPid:"):
                    parent = int(line.split()[1])
                elif line.startswith("Name:"):
                    fields = line.split(None, 1)
                    name = fields[1].strip() if len(fields) > 1 else ""
    except (OSError, ValueError, IndexError):
        return 0, ""
    return parent, name


def _ancestors_from_proc(pid: int) -> list:
    found: list = []
    seen = {pid}
    current = pid
    for _ in range(_ANCESTOR_LIMIT):
        parent, _name = _proc_status(current)
        if parent <= 0 or parent in seen:
            break
        seen.add(parent)
        found.append((parent, _proc_status(parent)[1]))
        current = parent
    return found


def _one_line(text: str, limit: int = _DESCRIPTION_LIMIT) -> str:
    """One line, bounded: the reason travels into a dialog and into Telegram."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _describe_pid(pid: int) -> str:
    """What this pid is, for the sentence a person reads. Never raises.

    "not found" and "unreadable" are answers: a gate that cannot see says so
    rather than failing the classification of the command around it.
    """
    try:
        import psutil
    except ImportError:
        return _describe_pid_from_proc(pid)
    try:
        proc = psutil.Process(pid)
    except Exception as exc:
        return "not found" if "NoSuchProcess" in type(exc).__name__ else "unreadable"
    try:
        name = proc.name()
    except Exception:
        name = ""
    try:
        argv = [arg for arg in proc.cmdline()[1:] if arg]
    except Exception:
        argv = []
    return _one_line(" ".join([part for part in [name, *argv] if part])) or "unreadable"


def _describe_pid_from_proc(pid: int) -> str:
    try:
        with open(os.path.join(os.sep, "proc", str(pid), "cmdline"), "rb") as fh:
            raw = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
        if raw.strip():
            return _one_line(raw)
    except OSError:
        pass
    name = _proc_status(pid)[1]
    return _one_line(name) if name else "not found"


def _name_stem(name: str) -> str:
    """`C:\\x\\Python.EXE` and `python` are the same name to a kill command."""
    bare = str(name).strip().strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return bare[:-4] if bare.endswith(".exe") else bare


# How wide a pattern reaches depends on the verb that carries it, and the verbs
# disagree: `pkill` compiles an extended regular expression, `killall` matches a
# name exactly (a regex only under `-r`), and `taskkill /IM` takes a wildcard.
# Reading all three as a substring said "Kills every process named pytho
# (0 running)" for a command that would have killed this service — the tier and
# the count both wrong, measured 2026-09-21.
_PATTERN_LENGTH_LIMIT = 200

# Python's `re` has no POSIX bracket classes: `[[:alpha:]]ython` reads as a
# nested set, matches nothing and warns, so the gate said "(0 running)" about a
# pattern pkill would have matched. Translated before compiling; a class with no
# entry here fails closed, like a pattern that does not compile.
_POSIX_CLASSES = {
    "alpha": "a-zA-Z",
    "alnum": "a-zA-Z0-9",
    "digit": "0-9",
    "lower": "a-z",
    "upper": "A-Z",
    "space": r"\s",
    "punct": r"!-/:-@\[-`{-~",
    "xdigit": "0-9A-Fa-f",
}
_POSIX_CLASS_RE = re.compile(r"\[:(\w+):\]")


def _as_python_regex(pattern: str) -> Tuple[str, str]:
    """(an expression `re` can read, why it cannot be read) — one is always empty."""
    missing: list = []

    def swap(found: "re.Match") -> str:
        body = _POSIX_CLASSES.get(found.group(1).lower())
        if body is None:
            missing.append(found.group(0))
            return found.group(0)
        return body

    expression = _POSIX_CLASS_RE.sub(swap, pattern)
    if missing:
        return "", f"it uses {missing[0]}, which python's regex reader does not know"
    return expression, ""


def _pattern_covers(pattern: str, target: str, match: str) -> bool:
    """Does this pattern reach `target`, read the way its verb reads it?

    Fail closed three times, because a pattern the gate cannot evaluate cannot
    be shown safe: one too long to be worth compiling, one carrying a POSIX
    class with no python spelling, and one that does not compile at all. No
    timeout — the length cap is what keeps a catastrophic pattern out of `re`.
    """
    if match not in ("regex", "regex-exact"):
        return False
    if len(pattern) > _PATTERN_LENGTH_LIMIT:
        return True
    expression, blind = _as_python_regex(pattern)
    if blind:
        return True
    if match == "regex-exact":
        expression = f"^(?:{expression})$"
    try:
        with warnings.catch_warnings():
            # A nested-set warning from a pattern somebody typed is noise in the
            # log a person reads to find out why a command was refused.
            warnings.simplefilter("ignore", FutureWarning)
            return bool(re.search(expression, target, re.I))
    except re.error:
        return True


def _pattern_is_unreadable(pattern: str, match: str) -> str:
    """Why this pattern could not be evaluated, or "" when it could be."""
    if match not in ("regex", "regex-exact"):
        return ""
    if len(pattern) > _PATTERN_LENGTH_LIMIT:
        return f"it is longer than the {_PATTERN_LENGTH_LIMIT}-character limit this gate reads"
    expression, blind = _as_python_regex(pattern)
    if blind:
        return blind
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            re.compile(expression)
    except re.error as exc:
        return f"it is not a valid regular expression ({exc})"
    return ""


def _name_would_be_killed(pattern: str, name: str, match: str) -> bool:
    """One process name, tested the way the verb that carries the pattern tests it."""
    if not name:
        return False
    if _pattern_covers(pattern, name, match):
        return True
    if _pattern_covers(pattern, _name_stem(name), match):
        return True
    return fnmatch.fnmatch(_name_stem(name), _name_stem(pattern))


def _count_processes_named(pattern: str, match: str = "wildcard") -> Optional[int]:
    """How wide the net is, or None when the machine will not say.

    The count goes into the sentence a person reads, so it has to use the same
    matching the verb does — a number that contradicts the verb is a false
    statement, not a rounding error.
    """
    try:
        import psutil

        return sum(
            1 for proc in psutil.process_iter(["name"])
            if _name_would_be_killed(pattern, proc.info.get("name") or "", match)
        )
    except Exception:
        return None


class _KillTargets(NamedTuple):
    """What one segment would signal, as much of it as is readable statically."""

    verb: str = ""
    pids: tuple = ()
    unresolved: tuple = ()      # arguments that are not literal numbers
    names: tuple = ()           # image names and patterns
    whole_command_line: bool = False   # pkill -f: the pattern is not a name
    match: str = "wildcard"     # wildcard | exact | regex | regex-exact
    pidfiles: tuple = ()        # pkill -F: the pid is in this file, unread here


_KILL_VERBS = {"taskkill", "tskill", "kill", "pkill", "killall", "stop-process", "spps", "wmic"}
# Words that stand in front of a verb without changing what it does. Their own
# flags are skipped, `-` and `/` alike; a flag that takes a value (`sudo -u
# root kill 1`) is a named limit — the verb is not found and the command stays
# Tier 1 on `sudo` alone.
_KILL_WRAPPERS = {
    "sudo", "doas", "nohup", "env", "command", "exec", "time", "runas",
    "start", "call", "xargs",
}
# `start` takes a window title before the command, and a title is an ordinary
# word once the quotes are off, so exactly one unrecognised token may be
# stepped over after it.
_TITLE_TAKING_WRAPPERS = {"start"}

_TASKKILL_SWITCH_RE = re.compile(r"^/(pid|im|fi)(?::(.*))?$", re.I)
_TASKKILL_FILTER_RE = re.compile(r"\s*(imagename|pid)\s+eq\s+(\S+)", re.I)
_PS_PARAM_RE = re.compile(r"^-(id|name|processname)(?::(.*))?$", re.I)
_PS_PARAM_WITH_VALUE = {"-s", "-n", "--signal", "-erroraction", "-inputobject"}
_PKILL_PARAM_WITH_VALUE = {
    "-u", "--user", "-g", "--group", "-s", "--signal", "-t", "--older",
    # `-P ppid` selects by parent, and the ppid was read as the pattern — so
    # `pkill -P 1 python` never reached `python`, the name that covers us.
    "-p", "--parent",
}
# procps reads `-F file` and `--pidfile file` as «the pid is in this file», one
# capital letter away from `-f`, «match the whole command line». The switch loop
# folds case, so the two were the same switch and the dialog described a net
# that is not one. Matched before the fold, with the case procps requires.
_PKILL_PIDFILE_RE = re.compile(r"^(?:-F|--pidfile)(?:=?(.+))?$")
_WMIC_PID_RE = re.compile(r"processid\s*=\s*['\"]?([^\s'\",]+)", re.I)
_WMIC_NAME_RE = re.compile(r"\bname\s*=\s*['\"]?([^\s'\",]+)", re.I)
# `%PID%`, `$pid`, `$(cat run.pid)`, a backtick: a pid nobody can read here.
_UNREADABLE_PID = re.compile(r"[%$`(){}*?!]")
# The same question asked of a *pattern*, which is a different question: for
# `pkill` and `killall -r` the argument is a regular expression by definition, so
# `( ) | ^ $ ? * + [ ] { } .` are its alphabet and not a sigil. The character
# class above was applied to it anyway, which sent every such regex to
# "unreadable id" before the pattern reader saw it — measured on dd1a5661, and
# four of the four spellings tried would have killed this service. Shape decides
# instead: `$` at the end of a pattern or before `)` or `|` is an anchor, while
# `$name`, `${name}`, `$(…)`, `$$`, `$_`, a backtick and cmd's `%i` / `%name%`
# are values the shell computes and this gate cannot know.
_SHELL_EXPANSION_RE = re.compile(r"`|\$\$|\$[A-Za-z_{(]|%[A-Za-z_]")


def _wmic_kills(segment: str) -> bool:
    """Is this `wmic` a kill at all? The bare word is not one.

    `wmic process get ProcessId,Name` lists, `wmic cpu get name` reads a
    datasheet; it takes the `process` class *and* a `delete` or `terminate`.
    Written once and read by both of wmic's readers — the parser's
    `_wmic_targets` and the word test below — so the two cannot drift.
    """
    if not re.search(r"\bprocess\b", segment, re.I):
        return False
    return bool(re.search(r"\b(delete|terminate)\b", segment, re.I))


# Verbs whose bare word does not mean a kill, each with the test that says it
# does. They are kept out of the plain-word regex and asked separately.
_QUALIFIED_KILL_VERBS: dict = {"wmic": _wmic_kills}
# The plain words come from `_KILL_VERBS` itself rather than a second list.
# They were two lists until 2026-09-21, the second one short by `wmic`, and a
# wmic kill the parser could not reach — inside a loop body, its target read
# from a file — therefore tripped nothing at all.
_KILL_WORD_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(
        re.escape(verb) for verb in sorted(_KILL_VERBS - set(_QUALIFIED_KILL_VERBS))
    ),
    re.I,
)


# The spellings of «end a process» that are not commands: a method on an object,
# and a cmdlet that reaches the same method by name. They sit beside
# `_KILL_VERBS` rather than in it because `_kill_verb` would read one as a verb
# and parse its switches — `Invoke-WmiMethod -Name Terminate` would come back as
# a kill of a process named Terminate. Only `_carries_a_kill_word` asks them,
# which is the question they can answer. `.Kill()` was refused and
# `.Terminate()` was not, on the accident that `\bkill\b` matches the first.
_METHOD_KILL_PATTERNS: dict = {
    # End of text counts as the parenthesis: `_strip_grouping` eats a trailing
    # `()`, so a bare `(Get-Process -Id 6520).Terminate()` arrives here without
    # the call it is. The price is a segment that ends in `.kill` or `.terminate`.
    "a .kill() or .terminate() call": re.compile(
        r"\.\s*(?:kill|terminate)(?=\s*(?:\(|$))", re.I
    ),
    "an Invoke-WmiMethod or Invoke-CimMethod that names Terminate": re.compile(
        r"\bInvoke-(?:Wmi|Cim)Method\b(?=.*\bTerminate\b)", re.I
    ),
}


def _method_kill_spelling(segment: str) -> str:
    """The method spelling of a kill in this text, or "" when it holds none.

    Qualified like `wmic`, and for the same reason: `Invoke-CimMethod
    -MethodName Create` starts a process, so the bare cmdlet is not a kill word.
    """
    for pattern in _METHOD_KILL_PATTERNS.values():
        found = pattern.search(segment)
        if found:
            return found.group(0)
    return ""


def _carries_a_kill_word(segment: str) -> bool:
    """Does this text carry a word that could signal a process?

    The one producer for the three rules that look for a kill word without
    parsing one: the unreadable-wrapper note, the unread-body rule and the
    last-resort net. Whole-word and case-insensitive, as the hand-written regex
    was. It answers «a kill cannot be ruled out here», not «this is a kill»,
    which is why a broad verb has to qualify: gating those rules on the bare
    word `wmic` would make `for %i in (1) do wmic cpu get name` a question and
    would have the net refuse `echo <our pid> & wmic os get caption`.
    """
    if _KILL_WORD_RE.search(segment):
        return True
    if _method_kill_spelling(segment):
        return True
    return any(
        re.search(rf"\b{re.escape(verb)}\b", segment, re.I) and qualifies(segment)
        for verb, qualifies in _QUALIFIED_KILL_VERBS.items()
    )


def _tokens(segment: str) -> list:
    """One segment's arguments, quotes honoured and stripped."""
    out, buf, quote = [], [], ""
    for ch in segment:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _verb_of(token: str) -> str:
    """The command a token names, without its path, its `.exe` or cmd's `@`."""
    return _name_stem(token.lstrip("@"))


def _kill_verb(tokens: list) -> Tuple[str, list]:
    """(verb, its arguments), or ("", []) when this segment kills nothing.

    Verb-first on purpose: `echo taskkill`, a file called `killer.txt` and
    `git commit -m "kill the bug"` all carry the word and none of them is one.
    """
    titles = 0
    wrapped = False
    for i, token in enumerate(tokens):
        verb = _verb_of(token)
        if verb in _KILL_VERBS:
            return verb, list(tokens[i + 1:])
        if verb in _KILL_WRAPPERS:
            wrapped = True
            titles += 1 if verb in _TITLE_TAKING_WRAPPERS else 0
            continue
        if wrapped and token.startswith(("-", "/")):
            continue
        if titles > 0:
            titles -= 1
            continue
        return "", []
    return "", []


# A shell handed a command as a string. The verb-first reader sees the wrapper
# and stops, so the string is taken out and read as a command in its own right.
_WRAPPER_DEPTH = 3
# (which shell reads it, how to take the string out). The shell matters for one
# thing only: `$PID` is the host's own pid in PowerShell and an ordinary
# variable in `sh`.
_INNER_COMMAND_PATTERNS: list = [
    ("cmd", re.compile(rf"\bcmd(?:\.exe)?{_CMD_C}\s+(.+)$", re.I)),
    (
        "powershell",
        re.compile(
            r"\b(?:powershell|pwsh)(?:\.exe)?\b.*?\s-c(?:o(?:m(?:m(?:a(?:n(?:d)?)?)?)?)?)?\b\s+(.+)$",
            re.I,
        ),
    ),
    ("posix", re.compile(rf"\b(?:{_SHELL_NAMES})(?:\.exe)?{_POSIX_C}\s+(.+)$", re.I)),
]
# Shells this gate knows by name, for the case where it can see the wrapper and
# not the command inside it.
_SHELL_WRAPPERS = {"cmd", "bash", "sh", "zsh", "fish", "dash", "ksh", "powershell", "pwsh"}
_POWERSHELL_FLAVOUR_RE = re.compile(
    r"\b(?:stop-process|spps|get-process|powershell|pwsh)\b", re.I
)
_ENCODED_COMMAND_RE = re.compile(r"\b(?:powershell|pwsh)\b.*\s-e(?:nc(?:odedcommand)?)?\b", re.I)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _inner_command_strings(segment: str) -> list:
    """(shell, command string) for every command this segment hands to a shell."""
    found = []
    for shell_name, pat in _INNER_COMMAND_PATTERNS:
        match = pat.search(segment)
        if match:
            inner = _unquote(match.group(1))
            if inner:
                found.append((shell_name, inner))
    return found


class _Piece(NamedTuple):
    """One command the gate reads, and whose shell's language it is written in."""

    text: str
    shell: str = ""     # "" unknown | posix | cmd | powershell


def _shell_of(text: str, inherited: str) -> str:
    """Which shell's variables this piece uses.

    A cmdlet is the marker, not the wrapper: `Stop-Process -Id $PID` arrives
    with no wrapper at all when the gate's own shell is PowerShell.
    """
    if _POWERSHELL_FLAVOUR_RE.search(text):
        return "powershell"
    return inherited


def _shell_wrapper_named(segment: str) -> str:
    """The shell this segment's verb is, or "".

    Verb-first, like `_kill_verb`, and for the same reason: `cat kill.sh` and
    `grep -r "bash" notes.txt` carry the words without handing anything to a
    shell.
    """
    titles = 0
    for token in _tokens(segment):
        verb = _verb_of(token)
        if verb in _SHELL_WRAPPERS:
            return verb
        if verb in _KILL_WRAPPERS:
            titles += 1 if verb in _TITLE_TAKING_WRAPPERS else 0
            continue
        if token.startswith(("-", "/")):
            continue
        if titles > 0:
            titles -= 1
            continue
        return ""
    return ""


def _unreadable_wrapper_note(segment: str) -> str:
    """A shell the gate can see and a command inside it that it cannot read.

    Both halves are required. A wrapper alone is ordinary work — `bash script.sh`
    has always been allowed — and a kill word alone is a word. Together they are
    a kill that cannot be ruled out, which is Tier 1: only the rules that can
    name what dies decide Tier 2.
    """
    wrapper = _shell_wrapper_named(segment)
    if not wrapper:
        return ""
    if not _carries_a_kill_word(segment):
        return ""
    if _inner_command_strings(segment):
        return ""
    return (
        f"A kill cannot be ruled out: this segment hands work to {wrapper} and the "
        f"gate could not read the command string inside it"
    )


# What opens a command body this gate does not read, in one table so the rule
# does not depend on which construct was written. `for` and `while` open a
# header, not a body: what follows it arrives with `do` or `{`, both here.
_BODY_KEYWORDS = {
    "do", "then", "else", "if",
    "-exec", "-execdir", "-ok",
    "foreach", "foreach-object",
}


def _opens_a_body(token: str) -> Optional[str]:
    """What this token puts in front of the body it opens, or None for nothing.

    A keyword puts nothing there. `(` and `{` may put the body's first word
    there, because cmd writes `if exist x (taskkill …)` with no space. `{}` is
    `find`'s placeholder and opens nothing, so of the braces only a bare `{`
    counts — which is also the script block that `ForEach-Object`'s `%` alias
    is reached through, `%` being cmd's variable sigil as well.
    """
    if token.lower() in _BODY_KEYWORDS:
        return ""
    if token.startswith("(") or token == "{":
        return token[1:]
    return None


def _command_bodies(segment: str) -> list:
    """Every command body this segment opens, as text."""
    tokens = _tokens(segment)
    bodies = []
    for i, token in enumerate(tokens):
        head = _opens_a_body(token)
        if head is None:
            continue
        body = " ".join(([head] if head else []) + list(tokens[i + 1:])).strip()
        if body:
            bodies.append(body)
    return bodies


def _kill_inside_an_unread_body(segment: str) -> str:
    """A kill word, and a construct whose body the gate does not read.

    The second trigger of the last-resort net, and it stops at Tier 1: only the
    rules that can name what dies decide Tier 2. Both halves are required, as
    in the wrapper note above, and the keyword has to be a *token* — `_tokens`
    keeps a quoted string whole, so `echo "do not kill it"` carries neither.
    """
    if not _carries_a_kill_word(segment):
        return ""
    if not _command_bodies(segment):
        return ""
    return (
        "A kill cannot be ruled out: this segment opens a body the gate does not "
        "read — a loop, a conditional or an -exec — and the gate has not identified "
        "what the kill word inside it would signal"
    )


def _a_kill_spelled_as_a_method(segment: str) -> str:
    """A call that ends a process, with no command in it for the parser to read.

    The third trigger of the last-resort net, and it stops at Tier 1 like the
    two above: `(Get-Process -Id 4243).Terminate()` carries its target in a
    property this gate does not evaluate, so it can say a kill is here and not
    what it would signal.
    """
    spelling = _method_kill_spelling(segment)
    if not spelling:
        return ""
    return (
        f"A kill cannot be ruled out: this segment calls {_one_line(spelling, 40)}, "
        f"which ends a process, and the gate has not identified which process that is"
    )


def _kill_scan_segments(segment: str) -> list:
    """This segment and every command nested inside it, depth-bounded."""
    out, seen = [], set()
    frontier = [(segment, "", 0)]
    while frontier:
        text, inherited, depth = frontier.pop()
        out.append(_Piece(text, _shell_of(text, inherited)))
        if depth >= _WRAPPER_DEPTH:
            continue
        for inner_shell, inner in _inner_command_strings(text):
            for piece in _split_segments(inner):
                piece = piece.strip()
                if piece and piece not in seen:
                    seen.add(piece)
                    frontier.append((piece, inner_shell, depth + 1))
    return out


def _refusal_pieces(segment: str) -> list:
    """What the refusal path reads: this segment, what is nested in it, and the
    bodies it opens.

    The bodies are read here and nowhere else. Reading one can add a refusal of
    ourselves and can never add noise to a dialog, while the sentence a person
    reads must keep saying that a body went unread: a verb found inside a loop
    is not the whole of what the loop does with it.
    """
    pieces = list(_kill_scan_segments(segment))
    for body in _command_bodies(segment):
        pieces.extend(_kill_scan_segments(body))
    return pieces


def _add_pid(raw: str, pids: list, unresolved: list) -> None:
    """A pid argument, split on commas for PowerShell's `-Id 1,2,3`.

    Fail closed: anything that is not a literal number cannot be shown safe,
    so it is recorded as unresolved rather than dropped.
    """
    for part in str(raw).split(","):
        part = part.strip().strip("\"'")
        if not part:
            continue
        if part.isdigit():
            pids.append(int(part))
        else:
            unresolved.append(part)


def _taskkill_targets(rest: list) -> _KillTargets:
    pids, unresolved, names = [], [], []
    i = 0
    while i < len(rest):
        token = rest[i]
        i += 1
        switch = _TASKKILL_SWITCH_RE.match(token)
        if not switch:
            continue
        key, inline = switch.group(1).lower(), switch.group(2)
        if inline:
            value = inline
        else:
            value = rest[i] if i < len(rest) else ""
            i += 1
        if not value:
            continue
        if key == "pid":
            _add_pid(value, pids, unresolved)
        elif key == "im":
            names.append(value)
        else:
            found = _TASKKILL_FILTER_RE.match(value)
            if found and found.group(1).lower() == "pid":
                _add_pid(found.group(2), pids, unresolved)
            elif found:
                names.append(found.group(2))
    return _KillTargets("taskkill", tuple(pids), tuple(unresolved), tuple(names), False)


def _signal_kill_targets(verb: str, rest: list) -> _KillTargets:
    """`kill` and `Stop-Process` at once: `kill` is both a program and
    PowerShell's alias for the cmdlet, so one segment can be either."""
    pids, unresolved, names = [], [], []
    i = 0
    while i < len(rest):
        token = rest[i]
        i += 1
        param = _PS_PARAM_RE.match(token)
        if param:
            key, inline = param.group(1).lower(), param.group(2)
            if inline:
                value = inline
            else:
                value = rest[i] if i < len(rest) else ""
                i += 1
            if key == "id":
                _add_pid(value, pids, unresolved)
            else:
                names.extend(part.strip() for part in value.split(",") if part.strip())
            continue
        if token.lower() in _PS_PARAM_WITH_VALUE:
            i += 1                      # `-s SIGKILL`, `-n 9`
            continue
        if token.startswith("-"):
            continue                    # -9, -SIGKILL, -Force, -Confirm:$false
        _add_pid(token, pids, unresolved)
    return _KillTargets(verb, tuple(pids), tuple(unresolved), tuple(names), False)


def _tskill_targets(rest: list) -> _KillTargets:
    for token in rest:
        if token.startswith(("/", "-")):
            continue
        if token.isdigit():
            return _KillTargets("tskill", (int(token),), (), (), False)
        if _UNREADABLE_PID.search(token):
            return _KillTargets("tskill", (), (token,), (), False)
        return _KillTargets("tskill", (), (), (token,), False)
    return _KillTargets("tskill")


def _pattern_match_kind(verb: str, exact: bool, regexp: bool) -> str:
    """How this verb reads its pattern.

    `pkill` compiles an extended regular expression and `-x` anchors it;
    `killall` matches a whole name and reaches for a regex only under `-r`.
    Case is ignored on both sides regardless, which over-blocks `pkill` on
    POSIX by exactly one thing: a name that differs only in case.
    """
    if verb == "pkill":
        return "regex-exact" if exact else "regex"
    if regexp:
        return "regex"
    return "exact"


def _pattern_kill_targets(verb: str, rest: list) -> _KillTargets:
    """`pkill` and `killall`: one pattern, and the switches say how it is read."""
    names, unresolved, whole = [], [], False
    pidfiles: list = []
    exact, regexp = False, False
    pattern = ""
    i = 0
    while i < len(rest):
        token = rest[i]
        i += 1
        # `killall` has no pidfile switch, so its `-F` keeps its own reading.
        found = _PKILL_PIDFILE_RE.match(token) if verb == "pkill" else None
        if found:
            attached = found.group(1)
            value = attached if attached else (rest[i] if i < len(rest) else "")
            i += 0 if attached else 1
            if value:
                pidfiles.append(value.strip("\"'"))
            continue
        low = token.lower()
        if low in ("-f", "--full"):
            whole = True
            continue
        if low in ("-x", "--exact"):
            exact = True
            continue
        if low in ("-r", "--regexp"):
            regexp = True
            continue
        if low in _PKILL_PARAM_WITH_VALUE:
            i += 1
            continue
        if token.startswith("-"):
            continue
        pattern = token
        break
    kind = _pattern_match_kind(verb, exact, regexp)
    if pattern and not _SHELL_EXPANSION_RE.search(pattern):
        names.append(pattern)
    elif pattern:
        # An expansion is an id nobody here can read, and that is the sentence it
        # gets. It is *also* handed to the pattern reader when it reads as a
        # pattern, because `_tokens` has thrown the quotes away and `'$x|python'`
        # is both — but only then: `$(cat` does not compile, and the fail-closed
        # branch for an unreadable pattern would turn a question into a refusal.
        unresolved.append(pattern)
        if not _pattern_is_unreadable(pattern, kind):
            names.append(pattern)
    return _KillTargets(verb, (), tuple(unresolved), tuple(names), whole, kind,
                        tuple(pidfiles))


def _wmic_targets(segment: str) -> _KillTargets:
    if not _wmic_kills(segment):
        return _KillTargets()
    pids, unresolved, names = [], [], []
    for found in _WMIC_PID_RE.finditer(segment):
        _add_pid(found.group(1), pids, unresolved)
    for found in _WMIC_NAME_RE.finditer(segment):
        names.append(found.group(1))
    return _KillTargets("wmic", tuple(pids), tuple(unresolved), tuple(names), False)


def _kill_targets(segment: str) -> _KillTargets:
    """Everything this segment would signal, by whichever spelling it uses."""
    verb, rest = _kill_verb(_tokens(segment))
    if not verb:
        return _KillTargets()
    if verb == "taskkill":
        return _taskkill_targets(rest)
    if verb == "wmic":
        return _wmic_targets(segment)
    if verb in ("pkill", "killall"):
        return _pattern_kill_targets(verb, rest)
    if verb == "tskill":
        return _tskill_targets(rest)
    return _signal_kill_targets(verb, rest)


def _name_matches_this_service(pattern: str, whole_command_line: bool,
                               me: "_ServiceIdentity", match: str = "wildcard") -> str:
    """What of ours this name or pattern would take down, or "" for nothing.

    The substring and wildcard tests are kept beside the regex one rather than
    replaced by it: each is a positive on its own, and the union is what fails
    closed.
    """
    pattern = str(pattern).strip().strip("\"'")
    if not pattern:
        return ""
    if whole_command_line:
        if pattern.lower() in me.cmdline.lower():
            return _one_line(me.cmdline, 80)
        if _pattern_covers(pattern, me.cmdline, match):
            return _one_line(me.cmdline, 80)
    stem = _name_stem(pattern)
    for name in me.names:
        if fnmatch.fnmatch(_name_stem(name), stem) or fnmatch.fnmatch(name.lower(), pattern.lower()):
            return name
        if _name_would_be_killed(pattern, name, match):
            return name
    return ""


# `$PPID`, `$$` and PowerShell's `$PID` are not ids the gate failed to read:
# each one names a process we already know. In the shell this tool spawns,
# `$PPID` is the process that started it — the D-PC service — and `$$` is the
# shell itself, whose death takes the command with it. Measured tier1 "could not
# identify" on 2026-09-21, which is one click from the incident.
_SELF_TARGETS: dict = {
    "$ppid": (
        "the target is $PPID — the process that started this shell, which is the D-PC "
        "service itself (pid {own}). Killing it kills the process running this command, "
        "so it could never report back. Ask the person to restart the service themselves."
    ),
    "$$": (
        "the target is $$ — this shell itself, the one the D-PC service (pid {own}) "
        "spawned for this command. Killing it takes the command with it, so it could "
        "never report back. Name the specific PIDs you mean instead."
    ),
    "$pid": (
        "the target is $PID — the PowerShell host running this command, which is the "
        "process the D-PC service (pid {own}) spawned for it, or the service itself when "
        "PowerShell is the shell it started. Killing it aborts the command before it can "
        "report back. Name the specific PIDs you mean instead."
    ),
}


# A token is one of the names above when it *begins* with it: `$PID).Terminate()`
# is the variable with a call hanging off it. Reading the name anywhere in the
# token instead would make `echo "kill $PPID"` a refusal, and that quoted string
# is one token on purpose.
_SELF_TARGET_HEAD_RE = re.compile(r"^\$\{?(?:\$|[A-Za-z_]\w*)\}?")


def _self_target_key(token: str) -> str:
    """`${PPID}` and `$ppid` are one token to this rule; `${$}` is `$$`.

    The braces are dropped rather than matched, because by the time a token
    reaches here `_strip_grouping` may already have eaten the closing one —
    `kill ${PPID}` arrives as `kill ${PPID`.

    Case is folded although a POSIX shell would not fold it: `$ppid` is an unset
    variable there and refusing it costs a command nobody writes, while reading
    `$PPID` as unknown cost the service once already.
    """
    bare = str(token).strip().strip("\"'").lstrip("(").lower()
    head = _SELF_TARGET_HEAD_RE.match(bare)
    return re.sub(r"[{}]", "", head.group(0) if head else bare)


def _self_target_reason(token: str, shell_flavour: str, me: "_ServiceIdentity") -> str:
    """Why this non-literal target is us, or "" when it is merely unreadable."""
    key = _self_target_key(token)
    if key == "$pid" and shell_flavour != "powershell":
        # In `sh` this is an ordinary variable, usually unset — not the shell's
        # own pid, which is `$$`. It stays a named question.
        return ""
    template = _SELF_TARGETS.get(key)
    return template.format(own=me.own_pid) if template else ""


def _kill_of_this_service(segment: str) -> str:
    """The reason this segment must be refused outright, or "" for none.

    The agent reads this, and the person may well have asked it to restart the
    service, so it says what to do instead. There is no restart command to
    offer: nothing in `service.py`, `local_api.py` or `run_service.py` exposes
    one.
    """
    pieces = _refusal_pieces(segment)
    for piece in pieces:
        targets = _kill_targets(piece.text)
        if not targets.verb:
            continue
        me = _service_identity()
        for pid in targets.pids:
            if pid == me.own_pid:
                return (
                    f"the target is the D-PC service this shell runs inside — pid {pid} "
                    f"({_describe_pid(pid)}). Killing it kills the process running this "
                    f"command, so it could never report back. Ask the person to restart "
                    f"the service themselves."
                )
            if pid in me.ancestors:
                return (
                    f"the target is the process the D-PC service runs inside — pid {pid} "
                    f"({_describe_pid(pid)}), an ancestor of this service (pid {me.own_pid}). "
                    f"Killing it takes this shell with it. Ask the person to restart the "
                    f"service themselves."
                )
        for token in targets.unresolved:
            mine = _self_target_reason(token, piece.shell, me)
            if mine:
                return mine
        for pattern in targets.names:
            hit = _name_matches_this_service(pattern, targets.whole_command_line,
                                            me, targets.match)
            if not hit:
                continue
            blind = _pattern_is_unreadable(pattern, targets.match)
            if blind:
                return (
                    f"\"{_one_line(pattern, 60)}\" is a pattern this gate cannot evaluate — "
                    f"{blind} — so it cannot be shown not to match the D-PC service itself "
                    f"(pid {me.own_pid}). Refusing rather than guessing. Name the specific "
                    f"PIDs you mean instead."
                )
            return (
                f"\"{_one_line(pattern, 60)}\" includes the D-PC service itself — it "
                f"matches {hit}, which is what this shell runs inside (pid {me.own_pid}). "
                f"Name the specific PIDs you mean instead; each one is identified in the "
                f"approval dialog."
            )
    # Every piece, not only the whole segment: a wrapper's quotes make its
    # command string one token, so `powershell -Command "(Get-Process -Id
    # $PID).Kill()"` offered the net no token that was the variable.
    for piece in pieces:
        mine = _kill_word_beside_a_protected_token(piece.text, piece.shell)
        if mine:
            return mine
    return ""


_STANDALONE_NUMBER_RE = re.compile(r"(?<![\w.])(\d+)(?![\w.])")


def _kill_word_beside_a_protected_token(segment: str, shell_flavour: str = "") -> str:
    """The coarse net under the parser, because spellings are endless.

    A kill word and something that names this service in one segment is refused
    without parsing either. Two forms reach no parser we are going to write:
    `for /f %i in ('echo <pid>') do taskkill /PID %i`, where the pid is a
    literal in a loop header, and `for %i in (1) do kill $PPID`, where the
    target is the variable naming the process above this shell. The price is
    that text merely carrying both is refused too, which is the trade this file
    has already made twice.
    """
    if not _carries_a_kill_word(segment):
        return ""
    me = _service_identity()
    protected = {me.own_pid, *me.ancestors}
    for match in _STANDALONE_NUMBER_RE.finditer(segment):
        pid = int(match.group(1))
        if pid in protected:
            mine = "this service" if pid == me.own_pid else f"an ancestor of pid {me.own_pid}"
            return (
                f"this command could not be parsed, but it names a kill and the number "
                f"{pid}, which is {mine} — the D-PC service this shell runs inside. "
                f"Refusing rather than guessing. Ask the person to restart the service "
                f"themselves."
            )
    flavour = _shell_of(segment, shell_flavour)
    for token in _tokens(segment):
        mine = _self_target_reason(token, flavour, me)
        if mine:
            return mine
    return ""


def _kill_needs_a_person(segment: str) -> str:
    """Killing a process is dangerous under its own name, not under a switch's.

    What dies goes in the reason: the incident's dialog said «Command accesses
    path outside sandbox: /PID», which is false and about the wrong thing.
    """
    notes: list = []
    if _ENCODED_COMMAND_RE.search(segment):
        notes.append(
            "A kill cannot be ruled out: this command is encoded and the gate "
            "cannot read what it runs"
        )
    parsed_a_kill = False
    for piece in _kill_scan_segments(segment):
        blind_wrapper = _unreadable_wrapper_note(piece.text)
        if blind_wrapper:
            notes.append(blind_wrapper)
        targets = _kill_targets(piece.text)
        if not targets.verb:
            continue
        parsed_a_kill = True
        if targets.pids:
            described = [f"{pid}: {_describe_pid(pid)}" for pid in targets.pids[:4]]
            notes.append(
                ("Kills process " if len(described) == 1 else "Kills processes ")
                + "; ".join(described)
            )
        for pattern in targets.names[:3]:
            if pattern in targets.unresolved:
                # A value the shell computes is not a net this gate can describe;
                # the unreadable-id note below is that token's one sentence.
                continue
            if targets.whole_command_line:
                notes.append(
                    f"Kills every process whose command line matches {_one_line(pattern, 60)}"
                )
                continue
            running = _count_processes_named(pattern, targets.match)
            counted = f" ({running} running)" if running is not None else ""
            # `pkill` is handed a pattern, not a name, and saying "named" of one
            # invites the reader to check it against a name.
            reads = "matching" if targets.match.startswith("regex") else "named"
            notes.append(f"Kills every process {reads} {_one_line(pattern, 60)}{counted}")
        for token in targets.unresolved[:3]:
            notes.append(
                "Kills a process the gate could not identify: the id is "
                f"{_one_line(token, 40)}"
            )
        for path in targets.pidfiles[:3]:
            # The file is not opened: it may not exist yet, the segment before
            # may be what writes it, and it may sit outside the sandbox.
            notes.append(
                "Kills a process the gate could not identify: the id is in "
                f"{_one_line(path, 40)}, a file the gate does not read"
            )
        if not (targets.pids or targets.names or targets.unresolved or targets.pidfiles):
            notes.append(
                f"Kills a process the gate could not identify: {targets.verb} names "
                f"no literal target"
            )
    # Ahead of the rest, and not at all when the parser has already said what
    # dies in this segment: the two sentences would be one fact told twice.
    # The method note wins where both fit, because `(Get-Process -Id 4243)
    # .Terminate()` opens no body at all — it is one expression, and the body
    # sentence would name a loop that is not there.
    if not parsed_a_kill:
        blind = _a_kill_spelled_as_a_method(segment) or _kill_inside_an_unread_body(segment)
        if blind:
            notes.insert(0, blind)
    seen, unique = set(), []
    for note in notes:
        if note not in seen:
            seen.add(note)
            unique.append(note)
    return _one_line("; ".join(unique), 400)


PATH_PATTERNS: list[re.Pattern] = [
    re.compile(r'\b([A-Z]:\\[^\s"\'<>|&;]+)'),
    re.compile(r'(?<!\w)(/[a-zA-Z][^\s"\'<>|&;]*)'),
]

# Branch 2 matches a Windows switch, and the tail of a URL, exactly as it
# matches a POSIX path — THE-SANDBOX-PATH-RULE-READS-A-WINDOWS-SWITCH-AS-A-PATH
# holds the count. The two narrowings below are deliberately NOT the rule
# `_reads_as_a_filesystem_path` uses: that one reads inside a file, this one a
# command line, and one change should not quietly satisfy two falsifiers.
_SWITCH_TOKEN_RE = re.compile(r"^/[A-Za-z]{1,3}$")
_URL_PREFIX_RE = re.compile(r"https?:/$", re.I)
_DRIVE_ROOT_NAMES: dict = {}


def _on_windows() -> bool:
    """The switch exemption is Windows-only: on POSIX `/PID` can be a path."""
    return os.name == "nt"


def _drive_root_of(base: str) -> str:
    try:
        drive = os.path.splitdrive(os.path.abspath(base or os.getcwd()))[0]
    except Exception:
        drive = ""
    return (drive + os.sep) if drive else os.sep


def _names_at_drive_root(drive: str):
    """What sits at the root of this drive, lowercased; None when unreadable.

    `/Users`, `/Windows` and `/ProgramData` stay paths this way. Unreadable is
    not «nothing is there»: None keeps the old verdict.
    """
    if drive in _DRIVE_ROOT_NAMES:
        return _DRIVE_ROOT_NAMES[drive]
    try:
        names = frozenset(name.lower() for name in os.listdir(drive))
    except OSError as exc:
        log.debug("could not list %s to tell a switch from a path: %s", drive, exc)
        names = None
    _DRIVE_ROOT_NAMES[drive] = names
    return names


def _is_url_interior(segment: str, match: "re.Match") -> bool:
    """`https://host/path` leaves `/host/path`, which branch 2 read as absolute."""
    return bool(_URL_PREFIX_RE.search(segment[: match.start(1)]))


def _stands_alone(segment: str, start: int) -> bool:
    """A switch is an argument of its own: `--out=/tmp` is not one."""
    return start == 0 or segment[start - 1] in " \t\"'"


def _follows_a_redirect(segment: str, start: int) -> bool:
    i = start - 1
    while i >= 0 and segment[i] in " \t\"'":
        i -= 1
    return i >= 0 and segment[i] in "><"


def _is_windows_switch(segment: str, match: "re.Match", drive: str) -> bool:
    """One to three letters, nothing after them, and three conditions on top.

    Without the three, the same rule waves through `ls /etc`, `cat /tmp` and
    `echo x > /ev`: a false «needs approval» is a nuisance, a false «allowed»
    is a hole.
    """
    if not _on_windows():
        return False
    token = match.group(1)
    if not _SWITCH_TOKEN_RE.match(token):
        return False
    if not _stands_alone(segment, match.start(1)):
        return False
    if _follows_a_redirect(segment, match.start(1)):
        return False
    at_root = _names_at_drive_root(drive)
    if at_root is None:
        return False
    return token[1:].lower() not in at_root


def _literal_path_outside(segment: str, ctx: "ToolContext", drive: str) -> str:
    """The first literal absolute path in this segment that leaves the sandbox."""
    for pat in PATH_PATTERNS:
        for match in pat.finditer(segment):
            extracted = match.group(1)
            if _is_url_interior(segment, match):
                continue
            if _is_windows_switch(segment, match, drive):
                continue
            # `./x`, `../x`, `${VAR}/x`, `%VAR%/x`: branch 2 read the tail as
            # absolute. Those are relative or expanded, and resolved below.
            start = match.start(1)
            if extracted.startswith("/") and start > 0 and segment[start - 1] in ".}%":
                continue
            try:
                ctx.validate_extended_path(extracted)
            except PermissionError:
                return f"Command accesses path outside sandbox: {extracted}"
    return ""


def _path_outside_sandbox(
    segments: list, ctx: "ToolContext", drive: str, base_dir: str = ""
) -> str:
    """Why this command's paths need a person, or "".

    At most two `; `-joined parts: the first path that leaves the sandbox, and
    the first one the gate cannot resolve without running the shell. Per
    segment, because only the segment holding a finding may be whitelisted.
    Variables, `~` and `..` are resolved against the child's environment and
    the directory the command has moved to. Still lexical: a path built inside
    a script walks past.
    """
    whitelist = _get_tier1_whitelist(ctx)
    env = _child_environment()
    windows = _on_windows()
    walk = _Cwd(base_dir) if base_dir else None
    outside, unresolvable = "", ""
    for segment in segments:
        here = walk.here if walk else ""
        view = _expand_for_path_check(segment, env, windows, here)
        waived = bool(whitelist) and _is_whitelisted(segment, whitelist)
        if not waived:
            if not outside:
                outside = _literal_path_outside(segment, ctx, drive)
            found, blind = _expanded_path_outside(view, ctx, windows, here, drive)
            outside = outside or found
            unresolved = view.unresolved + ([blind] if blind else [])
            if unresolved and not unresolvable:
                unresolvable = (
                    "Path cannot be resolved without running the shell: "
                    f"{_one_line(unresolved[0], 80)}"
                )
        if walk is None:
            continue
        move = _cd_move(view.text)
        if move is None or (windows and _is_bare_cd(view.text)):
            # cmd's bare `cd` prints the directory; it moves nowhere.
            continue
        walk.apply(move)
        if walk.here and not waived and not outside:
            # Where a move lands: a bare POSIX `cd` goes home without naming it.
            try:
                ctx.validate_extended_path(walk.here)
            except PermissionError:
                outside = f"Command accesses path outside sandbox: {walk.here}"
            except (OSError, ValueError):
                pass
    return "; ".join(part for part in (outside, unresolvable) if part)


# --- Path spellings the shell expands before anything runs -------------------
# A view for the check only: the command the shell receives is never rewritten.

def _child_environment() -> dict:
    """The environment `run_shell` gives the child, and so the one its shell expands."""
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


class _Word(NamedTuple):
    text: str                # quotes, carets and escapes removed, variables expanded
    anchors: tuple = ()      # offsets where an expanded absolute value starts


class _Expanded(NamedTuple):
    text: str                # the segment with every resolvable reference expanded
    words: list
    unresolved: list         # spellings only the running shell could resolve


# Where `$` means a variable on a Windows fleet: only inside a shell that reads
# it. cmd leaves `$HOME` as four literal characters.
_POSIX_SHELL_NAMED = re.compile(r"(?<![\w.\\/-])(?:bash|sh|zsh|dash|ksh)(?:\.exe)?\b", re.I)
_PWSH_NAMED = re.compile(r"(?<![\w.\\/-])(?:powershell|pwsh)(?:\.exe)?\b", re.I)
_INVOKE_EXPRESSION = re.compile(r"(?<![\w-])(?:iex|invoke-expression)(?![\w-])", re.I)
_ENV_REF_RE = re.compile(r"\$(?:\{env:(?P<braced>[^}]+)\}|env:(?P<bare>\w+))", re.I)
# The closing brace may be gone: `_strip_grouping` trims it off a segment's end.
_BRACED_RE = re.compile(r"\$\{(?P<name>[A-Za-z_]\w*)(?:\}|$)")
_DOLLAR_RE = re.compile(r"\$(?P<name>[A-Za-z_]\w*)")
_PERCENT_TILDE_RE = re.compile(r"%~[A-Za-z$:]*[0-9A-Za-z*]")
_PERCENT_RE = re.compile(r"%(?P<name>[^%\s=:\"'<>|&^]+)(?P<mod>:[^%]*)?%")
_DELAYED_RE = re.compile(r"!(?P<name>[^!\s=:\"'<>|&^]+)(?P<mod>:[^!]*)?!")
_FOR_VAR_RE = re.compile(r"%%?[A-Za-z](?![\w%])")
_TILDE_RE = re.compile(r"~(?P<user>[\w.-]*)(?=[/\\\s\"']|$)")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_ROOTED_ODD_RE = re.compile(r"^/[._~]")         # `/.dpc`: branch 2 wants a letter
_REDIRECT_PREFIX_RE = re.compile(r"^\d*[<>]+&?")
_ASSIGNMENT_RE = re.compile(r"^-{0,2}[\w.-]+=")
_ECHO_VERBS = frozenset({"echo", "echo.", "printf", "write-output", "write-host"})
# Values the shell computes rather than inherits; none of them is a path.
_CMD_DYNAMIC = frozenset({
    "date", "time", "random", "errorlevel", "cmdextversion", "cmdcmdline",
    "highestnumanodenumber",
})
_POSIX_DYNAMIC = frozenset({"RANDOM", "SECONDS", "LINENO", "UID", "EUID", "PPID", "BASHPID"})
_PWSH_AUTOMATIC = frozenset({
    "_", "null", "true", "false", "psitem", "args", "input", "this",
    "lastexitcode", "matches", "error", "host", "pid",
})


def _env_lookup(name: str, env: dict, windows: bool) -> Optional[str]:
    if name in env:
        return env[name]
    if windows:
        low = name.lower()
        for key, value in env.items():
            if key.lower() == low:
                return value
    return None


def _home_of(user: str, env: dict, windows: bool) -> Optional[str]:
    """`~` or `~user`, from the child's environment rather than this process's."""
    if windows:
        home = _env_lookup("USERPROFILE", env, True) or _env_lookup("HOME", env, True)
        if not home:
            drive, rest = env.get("HOMEDRIVE"), env.get("HOMEPATH")
            home = (drive + rest) if drive and rest else None
        if not user or not home:
            return home
        return os.path.join(os.path.dirname(home.rstrip("\\/")), user)
    if not user:
        return env.get("HOME")
    try:
        import pwd
        return pwd.getpwnam(user).pw_dir
    except (ImportError, KeyError):
        return None


def _looks_absolute(path: str, windows: bool) -> bool:
    return bool(_DRIVE_RE.match(path)) or path.startswith(("/", "\\"))


def _is_path_list(value: str, windows: bool) -> bool:
    """`%PATH%` is many directories and none of them is being opened."""
    if windows:
        return ";" in value
    body = value[2:] if re.match(r"^[A-Za-z]:", value) else value
    return ":" in body


def _is_bare_cd(text: str) -> bool:
    match = _CD_RE.match(text)
    return bool(match) and not _strip_comment(match.group("target")).strip().strip("\"'")


def _closing(text: str, start: int, opening: str = "(", closing: str = ")") -> int:
    """The index after the bracket that closes the one at `start`."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opening:
            depth += 1
        elif text[i] == closing:
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def _expand_for_path_check(segment: str, env: dict, windows: bool, cwd: str) -> _Expanded:
    """The segment's words as its shell would expand them, for the check only.

    cmd expands `%VAR%` and drops `^`; sh expands `$VAR`, `${VAR}` and a
    leading `~`, and honours single quotes and backslashes; `$env:VAR` is
    expanded everywhere. What only the running shell knows — an unset
    variable, a substitution, a `%~` modifier, a loop variable, iex — is
    listed as unresolved. An unset `%VAR%` stays literal in cmd, so it counts
    only in a word that carries a separator.
    """
    sh = (not windows) or bool(_POSIX_SHELL_NAMED.search(segment))
    pwsh = bool(_PWSH_NAMED.search(segment))
    delayed = windows and bool(re.search(r"/v:on\b", segment, re.I))
    for_loop = windows and bool(re.match(r"for\b", segment, re.I))
    out: list = []
    words: list = []
    unresolved: list = []
    buf: list = []
    anchors: list = []
    soft: list = []
    if _INVOKE_EXPRESSION.search(segment):
        unresolved.append("Invoke-Expression")

    def flush() -> None:
        if buf:
            text = "".join(buf)
            if soft and re.search(r"[\\/]", text):
                unresolved.extend(soft)
            words.append(_Word(text, tuple(anchors)))
        buf.clear()
        anchors.clear()
        soft.clear()

    def emit(raw: str, value: Optional[str], hard: bool = True) -> None:
        if value is None:
            (unresolved if hard else soft).append(raw)
            value = raw
        elif _is_path_list(value, windows):
            value = raw
        elif _looks_absolute(value, windows):
            anchors.append(sum(len(part) for part in buf))
        buf.append(value)
        out.append(value)

    def resolve(name: str, flavor: str) -> Optional[str]:
        if flavor == "cmd":
            if name.lower() == "cd":
                return cwd or None
            if name.lower() in _CMD_DYNAMIC:
                return "0"
            return _env_lookup(name, env, True)
        if name == "PWD" or (windows and name.lower() == "pwd"):
            return cwd or None
        if pwsh and name.lower() in _PWSH_AUTOMATIC:
            return "0"
        if name in _POSIX_DYNAMIC:
            return "0"
        value = _env_lookup(name, env, windows)
        if value is None and name.lower() == "home":
            value = _home_of("", env, windows)      # PowerShell's automatic $HOME
        return value

    quote = ""
    i, n = 0, len(segment)
    while i < n:
        ch = segment[i]
        if quote == "'":
            if ch == "'":
                quote = ""
            else:
                buf.append(ch)
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            quote = "" if quote == '"' else '"'
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            if not windows:
                quote = "'"
            out.append(ch)
            i += 1
            continue
        if not quote and ch in " \t":
            flush()
            out.append(ch)
            i += 1
            continue
        if windows and ch == "^" and not quote and i + 1 < n:
            buf.append(segment[i + 1])
            out.append(segment[i + 1])
            i += 2
            continue
        if not windows and ch == "\\" and i + 1 < n and (not quote or segment[i + 1] in '$`"\\'):
            buf.append(segment[i + 1])
            out.append(segment[i:i + 2])
            i += 2
            continue
        match = _ENV_REF_RE.match(segment, i)
        if match:
            name = match.group("braced") or match.group("bare")
            emit(match.group(0), _env_lookup(name, env, windows))
            i = match.end()
            continue
        if (sh or pwsh) and segment.startswith("$(", i):
            end = _closing(segment, i + 1)
            emit(segment[i:end], None)
            i = end
            continue
        if sh and ch == "`":
            end = segment.find("`", i + 1)
            end = n if end < 0 else end + 1
            emit(segment[i:end], None)
            i = end
            continue
        if (sh or pwsh) and segment.startswith("${", i):
            match = _BRACED_RE.match(segment, i)
            if match:
                emit(match.group(0), resolve(match.group("name"), "sh"))
                i = match.end()
            else:
                end = _closing(segment, i + 1, "{", "}")
                emit(segment[i:end], None)          # ${VAR:-x}, ${VAR%/*}, …
                i = end
            continue
        if sh or pwsh:
            match = _DOLLAR_RE.match(segment, i)
            if match:
                emit(match.group(0), resolve(match.group("name"), "sh"))
                i = match.end()
                continue
        if windows and ch == "%":
            match = _PERCENT_TILDE_RE.match(segment, i)
            if match:
                emit(match.group(0), None)
                i = match.end()
                continue
            match = _PERCENT_RE.match(segment, i)
            if match:
                if match.group("mod"):
                    emit(match.group(0), None)      # %VAR:~0,3%, %VAR:a=b%
                else:
                    emit(match.group(0), resolve(match.group("name"), "cmd"), hard=False)
                i = match.end()
                continue
            match = _FOR_VAR_RE.match(segment, i) if for_loop else None
            if match:
                emit(match.group(0), None)
                i = match.end()
                continue
        if delayed and ch == "!":
            match = _DELAYED_RE.match(segment, i)
            if match:
                if match.group("mod"):
                    emit(match.group(0), None)
                else:
                    emit(match.group(0), resolve(match.group("name"), "cmd"), hard=False)
                i = match.end()
                continue
        if ch == "~" and not quote and not buf:
            match = _TILDE_RE.match(segment, i)
            if match:
                emit(match.group(0), _home_of(match.group("user"), env, windows))
                i = match.end()
                continue
        buf.append(ch)
        out.append(ch)
        i += 1
    flush()
    return _Expanded("".join(out), words, unresolved)


def _word_spellings(word: _Word, windows: bool, drive: str = "") -> list:
    """The spellings in one word worth resolving: an expanded absolute value,
    an absolute form the two literal patterns miss, or a `..` traversal."""
    text = word.text
    found = [text[at:] for at in word.anchors]
    body = _REDIRECT_PREFIX_RE.sub("", text, count=1)
    if _ASSIGNMENT_RE.match(body):
        body = body.split("=", 1)[1]
    if windows and (_DRIVE_RE.match(body) or body.startswith("\\\\")):
        found.append(body)          # `c:\…`, `C:/…`, `"C:"\…`, `\\host\share`
    elif windows and _is_drive_rooted(body, drive):
        found.append(body)          # `\Users\…` is `C:\Users\…` to cmd
    elif _ROOTED_ODD_RE.match(body):
        found.append(body)
    if ".." in re.split(r"[\\/]" if windows else "/", body):
        found.append(body)
    return found


def _is_drive_rooted(body: str, drive: str) -> bool:
    """`\\Users\\x`, but not a regex like `\\bword\\b`: the first name must
    exist at the drive root, and an unreadable root counts as a path."""
    match = re.match(r'^\\([^\\/:*?"<>|\s]+)[\\/]', body)
    if not match:
        return False
    at_root = _names_at_drive_root(drive or _drive_root_of(""))
    return at_root is None or match.group(1).lower() in at_root


def _resolve_spelling(spelling: str, cwd: str, windows: bool) -> Optional[str]:
    if windows and os.sep == "/":
        spelling = spelling.replace("\\", "/")
    if _looks_absolute(spelling, windows):
        return os.path.normpath(spelling)
    if not cwd:
        return None
    return os.path.normpath(os.path.join(cwd, spelling))


def _expanded_path_outside(
    view: _Expanded, ctx: "ToolContext", windows: bool, cwd: str, drive: str = ""
) -> Tuple[str, str]:
    """(outside reason, unresolvable spelling) for the words of one segment.

    `echo %USERPROFILE%` prints a path and opens nothing, so an echo's
    arguments are skipped — its redirect target is not.
    """
    words = view.words
    echo = bool(words) and words[0].text.lower() in _ECHO_VERBS
    after_redirect = False
    for word in words[1:] if echo else words:
        redirect = bool(_REDIRECT_PREFIX_RE.match(word.text))
        if echo and not (redirect or after_redirect):
            continue
        after_redirect = redirect and not _REDIRECT_PREFIX_RE.sub("", word.text, count=1)
        for spelling in _word_spellings(word, windows, drive):
            if windows and spelling.startswith(("\\\\", "//")):
                # Resolving a share contacts its host: the gate must not.
                return f"Command accesses path outside sandbox: {spelling}", ""
            resolved = _resolve_spelling(spelling, cwd, windows)
            if resolved is None:
                return "", spelling         # a `..` after a move the gate lost
            try:
                ctx.validate_extended_path(resolved)
            except PermissionError:
                shown = resolved if resolved == spelling else f"{resolved} (spelled {spelling})"
                return f"Command accesses path outside sandbox: {shown}", ""
            except (OSError, ValueError):
                return "", spelling
    return "", ""

# An interpreter invoked on a script file. `-c` and `-e` have their own Tier 1
# rules; running a file had none.
#
# `bat` and `cmd` were absent until 2026-08-30, on a Windows fleet. Two
# independent reviews of the eval traces found the agent write `dl.bat` around a
# refused `curl` — it never ran it, but nothing here would have seen it if it
# had. Measured on this tree the same day: `dl.cmd` containing a drive-letter
# read of the answer archive classified Tier 0, and so did a bare `grab.py`,
# because the shebang pattern demanded a separator in the name.
_SCRIPT_EXT = r"py|pyw|js|mjs|cjs|ts|rb|pl|sh|bash|zsh|bat|cmd"
_SCRIPT_LAUNCH_PATTERNS: list[re.Pattern] = [
    # `python3.12 x.py` and `py -3 x.py` as well as the bare name.
    re.compile(
        r"\b(?:python(?:3(?:\.\d+)?)?|py|node|deno|ruby|perl|bash|sh|zsh)\s+"
        r"(?:-[^\s]+\s+)*"
        rf"([^\s\"'<>|&;]+\.(?:{_SCRIPT_EXT}))\b",
        re.I,
    ),
    re.compile(r"\b(?:powershell|pwsh)\b.*?-f(?:i(?:l(?:e)?)?)?\s+([^\s\"'<>|&;]+\.ps1)\b", re.I),
    # The cmd wrappers, which are how a batch file is usually reached.
    re.compile(
        r"\b(?:call|start|cmd(?:\.exe)?\s+/c|cmd(?:\.exe)?\s+/k)\s+"
        r"(?:/[^\s]+\s+)*"
        rf"([^\s\"'<>|&;]+\.(?:{_SCRIPT_EXT}|ps1))\b",
        re.I,
    ),
    # A script run by its own name: `./x.py`, `.\x.bat`, or plain `x.py`, which
    # Windows runs by file association and POSIX by the executable bit.
    re.compile(rf"^\s*([^\s\"'<>|&;]+\.(?:{_SCRIPT_EXT}|ps1))(?:\s|$)", re.I),
]

_SCRIPT_READ_LIMIT = 256 * 1024

_CD_RE = re.compile(r"^\s*(?:cd|chdir|pushd)\b(?:\s+/d\b)?\s*(?P<target>.*?)\s*$", re.I)
_POPD_RE = re.compile(r"^\s*popd\b", re.I)
_PUSHD_RE = re.compile(r"^\s*pushd\b", re.I)
# A target we cannot evaluate without running the shell: a variable, a
# substitution, or `cd -`. Resolving one of these by guessing is worse than
# saying the directory is unknown.
#
# Bare `(` and `%` are NOT that. The first spelling of this rule listed them as
# plain characters, so `cd "C:/Program Files (x86)/tool"` — the most ordinary
# path on the fleet's own platform — read as unresolvable and every script after
# it went to the approval queue. That is the same false-positive shape as the
# four XPath firings of 2026-08-30, introduced by the fix for them.
_UNRESOLVABLE_CD = re.compile(r"\$[\w{(]|\$$|`|%\w+%")


class _Move(NamedTuple):
    """A directory change, as much of it as the gate can know statically."""

    kind: str          # move | push | pop | previous | unknown
    target: str = ""


def _cd_move(segment: str) -> Optional[_Move]:
    """The directory change this segment makes, or None if it makes none.

    `popd` and `cd -` are *knowable* moves, not unknown ones — the first
    returns to what `pushd` recorded, the second to where we just were. Calling
    them unknown made every later script in the command unreadable, which
    refused ordinary work on a directory the gate had already cleared.
    """
    if _POPD_RE.match(segment):
        return _Move("pop")
    match = _CD_RE.match(segment)
    if not match:
        return None
    kind = "push" if _PUSHD_RE.match(segment) else "move"
    target = _strip_comment(match.group("target")).strip().strip("\"'")
    if target == "-":
        return _Move("previous")
    if not target:
        return _Move("move", os.path.expanduser("~"))
    if _UNRESOLVABLE_CD.search(target):
        return _Move("unknown")
    return _Move(kind, os.path.expanduser(target))


def _strip_comment(text: str) -> str:
    """Everything before an unquoted `#` that starts a word."""
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1].isspace()):
            return text[:i]
    return text


class _Cwd:
    """Where each segment of one command runs, tracked as the shell would.

    `here` is "" once a move cannot be evaluated. `candidates` always offers the
    starting directory as well, so a move can only add places the gate looks.
    """

    def __init__(self, base_dir: str):
        self.base = os.path.normpath(base_dir)
        self.here = self.base
        self._stack: list[str] = []
        self._previous = self.base

    def apply(self, move: _Move) -> None:
        if move.kind == "pop":
            self.here = self._stack.pop() if self._stack else ""
            return
        if move.kind == "previous":
            self.here, self._previous = self._previous, self.here
            return
        if move.kind == "push":
            self._stack.append(self.here)
        was = self.here
        if move.kind == "unknown":
            self.here = ""
        elif os.path.isabs(move.target):
            self.here = os.path.normpath(move.target)
        else:
            self.here = os.path.normpath(os.path.join(was, move.target)) if was else ""
        self._previous = was

    @property
    def candidates(self) -> list[str]:
        return list(dict.fromkeys(d for d in (self.here, self.base) if d))


def _script_named_in(segment: str) -> str:
    """The first script this segment launches, or "" if it launches none."""
    for pat in _SCRIPT_LAUNCH_PATTERNS:
        match = pat.search(segment)
        if match:
            return match.group(1).strip("\"'")
    return ""


def _script_paths_out_of_sandbox(
    segment: str, ctx: "ToolContext", base_dir: str
) -> Tuple[Optional[Tuple[str, str]], bool]:
    """A path that leaves the sandbox, found inside a script this segment runs.

    A speed bump rather than a boundary: it catches the naive form and an
    import, a computed name or base64 walks past it. The class is closed only
    by isolation — see WRITE-A-SCRIPT-AND-RUN-IT-AND-THE-PATH-GATE-NEVER-SEES-THE-PATH.

    Returns ((script, offending path), resolved); ("", script) when the script
    is present and unreadable, because a gate that cannot see must say so.

    `resolved` says whether a launched script was actually located here and
    read. Without it the caller cannot tell «nothing to look at» from «looked,
    and it was clean», and treating the second as the first refuses work the
    gate has already cleared.
    """
    resolved = False
    for pat in _SCRIPT_LAUNCH_PATTERNS:
        for match in pat.finditer(segment):
            raw = match.group(1).strip("\"'")
            candidate = raw if os.path.isabs(raw) else os.path.join(base_dir, raw)
            candidate = os.path.normpath(os.path.expanduser(candidate))
            if not os.path.isfile(candidate):
                continue
            try:
                # Never open a file outside the sandbox to decide: that is the
                # hole this check exists for.
                ctx.validate_extended_path(candidate)
            except PermissionError:
                continue
            try:
                # Too big to read is a kind of cannot read, and the branch three
                # lines down already answers that with a refusal. This used to
                # `continue`, so a script over the limit ran unexamined.
                size = os.path.getsize(candidate)
                if size > _SCRIPT_READ_LIMIT:
                    log.warning(
                        "script gate: %s is %d bytes, over the %d-byte read limit",
                        candidate, size, _SCRIPT_READ_LIMIT,
                    )
                    return ("", raw), True
                with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
                resolved = True
            except OSError as e:
                log.warning("script gate could not read %s: %s", candidate, e)
                return ("", raw), True
            for path_pat in PATH_PATTERNS:
                for hit in path_pat.finditer(body):
                    if not _reads_as_a_filesystem_path(body, hit):
                        continue
                    try:
                        ctx.validate_extended_path(hit.group(1))
                    except PermissionError:
                        return (raw, hit.group(1)), True
    return None, resolved


def _reads_as_a_filesystem_path(body: str, hit: "re.Match") -> bool:
    """Inside a file, most slashes are not paths.

    Measured on the campaign of 2026-08-30: all four firings of this gate were
    XPath prefixes — `//w:t`, `//a:t` — from scripts parsing docx and pptx, and
    every one refused a legitimate read inside the sandbox. URLs are the same
    shape. Both are rejected here rather than in PATH_PATTERNS, which the
    command-line scan shares and which has its own, larger version of this
    problem.
    """
    text = hit.group(1)
    start = hit.start(1)
    if start > 0 and body[start - 1] in "/:":
        return False
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    return bool(re.match(r"^/[^/:\s]+/", text))


def _validate_command(
    command: str, ctx: Optional["ToolContext"] = None, cwd: str = ""
) -> Optional[Tuple[str, str]]:
    """Validate command against safety tiers. Returns (tier, reason) or None if allowed."""
    normalized = _normalize_command(command)

    if _is_fork_bomb(normalized):
        return ("tier2", "Fork bomb detected")

    for pattern in CROSS_SEGMENT_PATTERNS:
        if pattern.search(normalized):
            return ("tier2", f"Blocked by cross-segment pattern: {pattern.pattern}")

    # Tier-major, not segment-major. This loop used to walk segments and
    # return on the first pattern of any tier that matched, so a Tier-1 hit
    # in an early segment ended the scan before a later segment was examined
    # for a hard block at all: `sudo ls && rm -rf /` came back as "ask the
    # human" because `sudo` matched first, and `rm -rf /` was never seen.
    # Every segment is now checked for a hard block before any of them is
    # checked for a soft one.
    segments = [segment.strip() for segment in _split_segments(normalized)]

    for segment in segments:
        # Before the pattern list, because this one can name what it refuses.
        refusal = _kill_of_this_service(segment)
        if refusal:
            return ("tier2", refusal)
        for pattern in HARDLINE_PATTERNS:
            if pattern.search(segment):
                return ("tier2", f"Blocked by HARDLINE pattern: {pattern.pattern}")

    # Reasons accumulate rather than stopping at the first. The reason is
    # what a person reads in the approval dialog, and naming only the first
    # match primes them for the wrong thing — "Requires approval: sudo" for a
    # command whose second half was the part that mattered.
    dangerous: list[str] = []
    flagged: list[str] = []
    kills: list[str] = []
    for segment in segments:
        note = _kill_needs_a_person(segment)
        if note:
            if note not in kills:
                kills.append(note)
            if segment not in flagged:
                flagged.append(segment)
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(segment):
                if pattern.pattern not in dangerous:
                    dangerous.append(pattern.pattern)
                if segment not in flagged:
                    flagged.append(segment)
    base_dir = os.path.expanduser(cwd) if cwd else str(getattr(ctx, "agent_root", "") or "")
    if dangerous or kills:
        whitelist = _get_tier1_whitelist(ctx)
        # Every flagged segment must be whitelisted on its own.
        if whitelist and all(_is_whitelisted(seg, whitelist) for seg in flagged):
            return None
        # What dies comes first: a person reads the head of this sentence.
        reasons = list(kills)
        if dangerous:
            reasons.append("Requires approval: " + "; ".join(dangerous))
        if ctx:
            outside = _path_outside_sandbox(segments, ctx, _drive_root_of(base_dir), base_dir)
            if outside:
                reasons.append(outside)
        return ("tier1", "; ".join(reasons))

    if ctx:
        whitelist = _get_tier1_whitelist(ctx)
        outside = _path_outside_sandbox(segments, ctx, _drive_root_of(base_dir), base_dir)
        if outside:
            return ("tier1", outside)

        if base_dir:
            # The cwd travels with the command. It used to be fixed once here and
            # applied to every segment, so `cd sub && python steal.py` resolved the
            # script against the directory above the one it runs in, os.path.isfile
            # missed, and the gate skipped it — one cd was the whole bypass.
            walk = _Cwd(base_dir)
            for segment in segments:
                move = _cd_move(segment)
                if move is not None:
                    walk.apply(move)
                    continue
                found, resolved = None, False
                for candidate_dir in walk.candidates:
                    found, seen = _script_paths_out_of_sandbox(segment, ctx, candidate_dir)
                    resolved = resolved or seen
                    if found:
                        break
                if not found and not resolved and not walk.here:
                    # The gate cannot say where this file is: the move before it was
                    # a variable or a substitution, and no directory it does know
                    # holds the script. That is the case the unreadable branch
                    # already answers with a refusal.
                    named = _script_named_in(segment)
                    if named:
                        found = ("", named)
                if not found:
                    continue
                if whitelist and _is_whitelisted(segment, whitelist):
                    continue
                script, path = found
                if not script:
                    return ("tier1", f"Command runs a script the gate could not read: {path}")
                return (
                    "tier1",
                    f"Script {script} accesses path outside sandbox: {path}",
                )

    return None


_pending_approvals: dict = {}
APPROVAL_TTL_SECONDS = 60

# Answerers that are not transports the service knows about. Nothing in
# production registers one; the eval harness does, so a headless benchmark is
# not told there is nobody to ask while its own approver watches the queue. A
# registered watcher can only restore the wait — it approves nothing itself.
_approval_watchers: set = set()


def register_approval_watcher(name: str) -> None:
    """Declare a non-transport answerer, so the gate asks instead of refusing."""
    _approval_watchers.add(name)


def unregister_approval_watcher(name: str) -> None:
    _approval_watchers.discard(name)


def _cleanup_expired_approvals() -> None:
    """Remove pending approvals older than TTL."""
    import time
    now = time.time()
    expired = [k for k, v in _pending_approvals.items()
               if now - v.get("created_at", 0) > APPROVAL_TTL_SECONDS]
    for k in expired:
        entry = _pending_approvals.pop(k, None)
        if entry:
            log.info("Shell approval expired (TTL %ds): %s — %r",
                     APPROVAL_TTL_SECONDS, k, entry.get("command"))


def _request_approval(ctx: ToolContext, command: str, reason: str, cwd: str, timeout: int) -> str:
    """Request user approval for a Tier 1 command (blocking).

    Blocks the executor thread until user approves/rejects via UI.
    On approval, service.py executes the command and stores the result.
    Returns the actual command output to the agent — approval is transparent.

    LIMITS: the check below establishes that no surface could carry the question,
    not that no person is present. An interface left open all night looks exactly
    like somebody sitting at it, and for that case the second line is still the
    TTL: the card is shown, nobody presses it, and sixty seconds later the
    command does not run.
    """
    import asyncio
    import time
    import uuid
    import threading

    _cleanup_expired_approvals()

    # Learned twice already — in the scheduler (tools/core.py:1097) and in the
    # headless web-auth gate (browser.py:2835): broadcast_event drops the request
    # when nobody is listening, so the wait below could only end in a timeout that
    # reads like a person's refusal. Telegram is the other surface that can answer,
    # and it answers with no interface client attached. A registered watcher is
    # the third — the eval harness declares one — so the refusal is for the case
    # where none of them can carry the question. Before the queue entry exists,
    # not after: an unanswerable request should not be sitting anywhere.
    dpc_service = getattr(ctx, "dpc_service", None)
    local_api = getattr(dpc_service, "local_api", None) if dpc_service else None
    telegram_chat_id = getattr(ctx, "reply_telegram_chat_id", "") or ""
    ui_attached = bool(getattr(local_api, "has_clients", False)) if local_api else False
    if not ui_attached and not telegram_chat_id and not _approval_watchers:
        log.warning("run_shell TIER1 not asked, no surface could answer: %r", command)
        return (
            f"🚫 Approval required and nobody could be asked: no interface client is "
            f"connected and this run did not come from Telegram. Not run: `{command}`"
        )

    request_id = str(uuid.uuid4())[:8]
    agent_obj = getattr(ctx, "_agent", None)
    # Never the bare word "Agent": an unknown agent has to look unknown, and the
    # sandbox directory is its real id. The firewall profile inherits the same
    # fallback, and that is an improvement — profiles are keyed by agent id, so
    # the old default could only ever miss.
    agent_name = agent_display_name(ctx)
    agent_profile = getattr(agent_obj, "_firewall_profile", None) or agent_name
    event = threading.Event()

    agent_id = getattr(getattr(ctx, "agent_root", None), "name", "") or ""

    _pending_approvals[request_id] = {
        "command": command,
        # Why this is Tier 1, kept where the queue can be read. Without it an
        # approver cannot tell "needs a judgement" from "leaves the sandbox"
        # and can only answer both the same way.
        "reason": reason,
        "cwd": cwd or str(ctx.agent_root),
        "timeout": timeout,
        "agent_name": agent_name,
        "agent_profile": agent_profile,
        "agent_id": agent_id,
        "ctx": ctx,
        "created_at": time.time(),
        "event": event,
        "result": None,
    }

    main_loop = getattr(ctx, "_event_loop", None)
    _origin_id, _origin_title = conversation_origin(ctx)

    def _on_main_loop(coro) -> bool:
        """Hand a coroutine to the service loop from this executor thread."""
        if main_loop is None or not main_loop.is_running():
            coro.close()
            return False
        asyncio.run_coroutine_threadsafe(coro, main_loop)
        return True

    if dpc_service is not None:
        try:
            offered = _on_main_loop(dpc_service.announce_shell_approval_request(
                request_id=request_id,
                command=command,
                reason=reason,
                agent_id=agent_id,
                agent_name=agent_name,
                timeout_seconds=APPROVAL_TTL_SECONDS,
                # Empty unless this run came from Telegram. The same field that
                # decides where the agent's answer goes decides where the
                # approval is offered — otherwise the button appears in chats
                # nobody is talking in, and anyone there can press it.
                telegram_chat_id=getattr(ctx, "reply_telegram_chat_id", "") or "",
                # Which chat the agent was working in. The tool knows the id and
                # sometimes the name; naming the conversation is the service's
                # job, because it is the one holding groups and peers.
                conversation_id=_origin_id,
                conversation_title=_origin_title,
            ))
            if not offered:
                log.warning("No main event loop available to announce shell_approval_request")
        except Exception as e:
            log.warning("Failed to announce shell_approval_request: %s", e)
    else:
        log.warning("No service available to announce shell_approval_request")

    log.info("run_shell TIER1 approval requested: %r (id=%s), blocking executor thread", command, request_id)

    # Wait for the user's decision only; the command runs below on this
    # thread, not in the approve handler (S197 timeout-race fix).
    signaled = event.wait(timeout=APPROVAL_TTL_SECONDS)
    entry = _pending_approvals.pop(request_id, None)

    if not signaled or entry is None:
        log.info("Shell approval timed out (%ds): %s — %r", APPROVAL_TTL_SECONDS, request_id, command)
        if dpc_service is not None:
            try:
                _on_main_loop(dpc_service.announce_shell_approval_closed(
                    request_id=request_id,
                    agent_id=agent_id,
                    outcome="⌛ Expired — the agent stopped waiting for this one.",
                    resolution="expired",
                ))
            except Exception as e:
                log.warning("Failed to announce shell_approval_closed: %s", e)
        return f"⏳ Command approval timed out after {APPROVAL_TTL_SECONDS}s: `{command}`"

    if entry.get("decision") == "rejected":
        return entry.get("result") or "❌ Command rejected by user."

    log.info("Shell approval granted: %s — executing %r", request_id, command)
    return _execute_shell_command(command, entry.get("cwd"), entry.get("timeout", 120))


def _cap_stream(text: str, stream: str) -> str:
    """Cap one output stream and say so *before* the content.

    The notice used to sit after the content, and the second cap in
    `loop.py:_truncate_tool_result` (15 000 chars) cut this string again,
    taking the notice with it. The agent was then left with the harness
    marker alone, which reports the size of what it received (50 036) as
    if it were the size of the command's output (699 370): a confident
    wrong number rather than silence. A prefix survives that second cut.

    It also names the way to read the rest, because this tool has no
    offset of its own, and the price of that way — the command runs
    again, which for a slow or non-deterministic command is a real cost
    the agent must see before paying it. The redirect form is one static
    string on all three platforms: `>` and `2>` are valid in both
    `cmd.exe` and `/bin/sh`, and a bare relative name lands in the agent
    sandbox, which is exactly where `read_file` resolves relative paths.
    """
    if len(text) <= MAX_OUTPUT:
        return text
    return (
        f"[{stream}: {len(text)} chars, kept first {MAX_OUTPUT} of them"
        f" | to read the rest, re-run the command as"
        f' `<command> > out.txt 2> err.txt`, then read_file("out.txt", offset=…)'
        f' or read_file("err.txt") — the files land in the working directory of'
        f" the run, which is the agent sandbox unless you passed cwd, and"
        f" re-running EXECUTES THE COMMAND AGAIN]\n"
        + text[:MAX_OUTPUT]
    )




def _execute_shell_command(command: str, working_dir: str | None, timeout: int) -> str:
    """Run a shell command, format stdout/stderr/exit. Executor-thread only."""
    is_windows = platform.system() == "Windows"
    timeout = min(max(timeout, 5), 300)
    if is_windows:
        command = f"chcp 65001 >nul && {command}"

    popen_kwargs: dict = {}
    if not is_windows:
        # A session of its own, so the whole descendant set can be signalled
        # with one killpg. Without it only the shell dies and its children are
        # reparented to init, still running and still holding the pipes.
        popen_kwargs["start_new_session"] = True

    try:
        # The spawn, the watcher, the tree kill and the bounded drain used to be
        # written out here. They are the same four things `git.py` needs, and a
        # process-kill routine kept in two copies is how one of them ends up a
        # version behind - so they live in `process.py` now and this is one of
        # its two callers.
        run = run_supervised(
            command,
            launcher="run_shell",
            timeout=timeout,
            cwd=working_dir,
            shell=True,
            env=_child_environment(),
            ceiling_mb=_MEMORY_CEILING_MB,
            popen_kwargs=popen_kwargs,
        )

        note = ""
        if run.exceeded_mb:
            # Name the ceiling the run was actually held to.
            note = (
                f"Error: the command and its children reached "
                f"{run.exceeded_mb} MB, over the {run.ceiling_mb} MB "
                f"ceiling, and were killed"
            )
            # Naming the clock when the ceiling is what fired tells the agent
            # the wrong cause, so the timeout adds only what it knows.
            if run.killed:
                note += f" - {run.killed}"
            note += (
                ". Raise DPC_SHELL_MEMORY_LIMIT_MB (a restart applies it) "
                "if this work genuinely needs more."
            )
        elif run.timed_out:
            note = f"Error: command timed out after {timeout}s - {run.killed}."

        parts = []
        if run.stdout:
            parts.append(_cap_stream(run.stdout, "stdout"))
        if run.stderr:
            parts.append(f"[stderr]\n{_cap_stream(run.stderr, 'stderr')}")
        if note:
            return "\n".join([note, *parts])
        if run.returncode != 0:
            parts.append(f"[exit code: {run.returncode}]")

        return "\n".join(parts) if parts else "(no output)"

    except Exception as e:
        log.error("run_shell failed: %s", e)
        return f"Error: {e}"


def run_shell(ctx: ToolContext, command: str, timeout: int = 120, cwd: str = "") -> str:
    # ADR-030: validate command before execution
    violation = _validate_command(command, ctx, cwd)
    if violation:
        tier, reason = violation
        if tier == "tier2":
            log.warning("run_shell BLOCKED (tier2): %r — %s", command, reason)
            return f"⛔ Command blocked by safety guardrails: {reason}"
        elif tier == "tier1":
            log.warning("run_shell TIER1 (approval needed): %r — %s", command, reason)
            return _request_approval(ctx, command, reason, cwd, timeout)

    working_dir: str | None = None
    if cwd:
        expanded = os.path.expanduser(cwd)
        if not os.path.isdir(expanded):
            return f"Error: cwd '{cwd}' is not a valid directory."
        try:
            ctx.validate_extended_path(expanded)
        except PermissionError:
            log.warning("run_shell cwd BLOCKED: %r outside sandbox", cwd)
            return f"⛔ cwd '{cwd}' is outside allowed sandbox paths."
        working_dir = expanded
    else:
        working_dir = str(ctx.agent_root)

    log.info("run_shell: %s (cwd=%s, timeout=%ds)", command, working_dir, min(max(timeout, 5), 300))
    return _execute_shell_command(command, working_dir, timeout)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="run_shell",
            schema={
                "name": "run_shell",
                "description": (
                    "Execute a shell command and return stdout/stderr. "
                    "Uses cmd.exe on Windows, /bin/sh on Unix. "
                    "Default working directory is the agent sandbox. "
                    "Max timeout 300s. Output truncated at 50K chars."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command to execute.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds (5-300, default 120).",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Working directory (absolute path). Default: agent sandbox.",
                        },
                    },
                    "required": ["command"],
                },
            },
            handler=run_shell,
            is_code_tool=True,
            timeout_sec=300,
            is_core=False,
            default_enabled=False,
        ),
    ]
