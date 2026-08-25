"""
DPC Agent — Shell Tool (run_shell).

Executes shell commands in a subprocess with timeout and output capture.
Cross-platform: uses cmd.exe on Windows, /bin/sh on Unix.
Restricted tool — requires explicit enable in privacy_rules.json.

Safety guardrails (ADR-030): 3-tier command classification.
  Tier 0 — auto-approve (safe read-only commands)
  Tier 1 — require approval (v2, currently blocked same as Tier 2)
  Tier 2 — hard block (catastrophic/destructive commands)
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import unicodedata
from typing import List, Optional, Tuple

from .registry import ToolEntry, ToolContext, agent_display_name, conversation_origin

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
    re.compile(r"\bdel\b.*\s/s\b", re.I),
    # Mass delete (PowerShell) — and PowerShell is not a Windows-only surface:
    # `pwsh` has shipped on Linux and macOS for years, so a rule that names only
    # `powershell` covers one platform of three. Recurse is the mass part; a
    # single-file Remove-Item stays ordinary work.
    re.compile(r"\bRemove-Item\b(?=.*\s-Recurse\b)", re.I),
    re.compile(r"\bRemove-Item\b(?=.*\s-Force\b)(?=.*[\\/]\*)", re.I),
    # Disk format / erase
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bformat\s+[A-Za-z]:", re.I),
    re.compile(r"\b(Format-Volume|Clear-Disk|Initialize-Disk|Remove-Partition)\b", re.I),
    re.compile(r"\bdiskutil\s+(eraseDisk|partitionDisk|secureErase)", re.I),
    # Raw device write
    re.compile(r"\bdd\b.*\bof=/dev/", re.I),
    re.compile(r">\s*/dev/sd", re.I),
    # Shutdown / reboot
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I),
    re.compile(r"\binit\s+[06]\b"),
    # Kill all
    re.compile(r"\bkill\b.*\s+-9\s+-1\b"),
    # WSL escape (Windows → Linux breakout)
    re.compile(r"\bwsl\b", re.I),
]

# Tier 1 / Tier 2 in v1 — dangerous patterns (blocked in v1, approval in v2)
DANGEROUS_PATTERNS: list[re.Pattern] = [
    # Privilege escalation
    re.compile(r"\b(sudo|su|runas|gsudo|pkexec)\b", re.I),
    # Subshell invocation (arbitrary code execution)
    re.compile(r"\b(bash|sh|zsh|fish)\s+-c\b", re.I),
    re.compile(r"\bcmd\s+/c\b", re.I),
    re.compile(r"\b(python|python3|py)\s+-c\b", re.I),
    re.compile(r"\bnode\s+-e\b", re.I),
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


def _split_segments(command: str) -> list[str]:
    """Split command by pipe/chain operators to check each segment.

    A **newline separates too**. It did not, so a two-line command was a
    single segment on every platform. That happened to be safer for the
    patterns we have — an unanchored HARDLINE still matches inside the whole
    blob — and it is safety by accident: any pattern anchored at the start of
    a line, and any future check that assumes a segment is one command, was
    blind to it.
    """
    parts = re.split(r"\s*(?:\|\||&&|[|;&\r\n])\s*", command)
    return [part for part in parts if part and part.strip()]


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
    """Check if command prefix matches any whitelist entry."""
    normalized = _normalize_command(command).strip().lower()
    for entry in whitelist:
        if normalized.startswith(entry.lower()):
            return True
    return False


def _validate_command(command: str, ctx: Optional["ToolContext"] = None) -> Optional[Tuple[str, str]]:
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
        for pattern in HARDLINE_PATTERNS:
            if pattern.search(segment):
                return ("tier2", f"Blocked by HARDLINE pattern: {pattern.pattern}")

    # Reasons accumulate rather than stopping at the first. The reason is
    # what a person reads in the approval dialog, and naming only the first
    # match primes them for the wrong thing — "Requires approval: sudo" for a
    # command whose second half was the part that mattered.
    dangerous: list[str] = []
    for segment in segments:
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(segment) and pattern.pattern not in dangerous:
                dangerous.append(pattern.pattern)
    if dangerous:
        whitelist = _get_tier1_whitelist(ctx)
        if _is_whitelisted(command, whitelist):
            return None
        return ("tier1", "Requires approval: " + "; ".join(dangerous))

    if ctx:
        _PATH_PATTERNS = [
            re.compile(r'\b([A-Z]:\\[^\s"\'<>|&;]+)'),
            re.compile(r'(?<!\w)(/[a-zA-Z][^\s"\'<>|&;]*)'),
        ]
        for pat in _PATH_PATTERNS:
            for match in pat.finditer(normalized):
                extracted = match.group(1)
                try:
                    ctx.validate_extended_path(extracted)
                except PermissionError:
                    whitelist = _get_tier1_whitelist(ctx)
                    if _is_whitelisted(command, whitelist):
                        break
                    return ("tier1", f"Command accesses path outside sandbox: {extracted}")

    return None


_pending_approvals: dict = {}
APPROVAL_TTL_SECONDS = 60


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
    """
    import asyncio
    import time
    import uuid
    import threading

    _cleanup_expired_approvals()

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

    dpc_service = getattr(ctx, "dpc_service", None)
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
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
            )
            return "the command and its descendants were killed"
        except Exception as exc:
            log.warning("taskkill on pid %s failed: %s", pid, exc)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return "the command and its process group were killed"
        except Exception as exc:
            log.warning("killpg on pid %s failed: %s", pid, exc)

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

    process = None
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=working_dir,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            # Nobody is sitting at this process. Without it the child inherits
            # the service's own console, so anything that asks a question —
            # PowerShell's "Do you want to continue? [Y] Yes [N] No" on
            # Invoke-WebRequest without -UseBasicParsing, an npm prompt, a
            # credential helper — prints into the operator's terminal and waits
            # there for the full timeout while the agent waits for it. Closed
            # stdin turns that into an immediate default answer, which for a
            # confirmation is "no": the command fails in a second and says so,
            # instead of hanging for minutes on a question the agent cannot see.
            stdin=subprocess.DEVNULL,
            **popen_kwargs,
        )

        try:
            stdout, stderr = process.communicate(timeout=timeout)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            # `subprocess.run` was here, and what it does on Windows is the
            # whole defect: on TimeoutExpired it calls `process.kill()` — which
            # is TerminateProcess on the direct child only — and then calls
            # `communicate()` **again with no timeout** to collect the reader
            # threads. The grandchild survives holding the inherited pipe, EOF
            # never arrives, and that second communicate waits for ever. The
            # tool's worker is not a daemon thread, so the interpreter then
            # joins it at exit and the whole process never terminates. Measured
            # 2026-08-25: nine and a half hours, 28.8 GB of VRAM held by a
            # server the hung parent could no longer reap.
            killed = _kill_process_tree(process)
            stdout, stderr = _drain_after_kill(process)
            note = (
                f"Error: command timed out after {timeout}s"
                f" — {killed}."
            )
            tail = []
            if stdout:
                tail.append(_cap_stream(stdout, "stdout"))
            if stderr:
                tail.append(f"[stderr]\n{_cap_stream(stderr, 'stderr')}")
            return "\n".join([note, *tail])

        parts = []
        if stdout:
            parts.append(_cap_stream(stdout, "stdout"))
        if stderr:
            parts.append(f"[stderr]\n{_cap_stream(stderr, 'stderr')}")
        if returncode != 0:
            parts.append(f"[exit code: {returncode}]")

        return "\n".join(parts) if parts else "(no output)"

    except Exception as e:
        log.error("run_shell failed: %s", e)
        if process is not None and process.poll() is None:
            _kill_process_tree(process)
        return f"Error: {e}"


def run_shell(ctx: ToolContext, command: str, timeout: int = 120, cwd: str = "") -> str:
    # ADR-030: validate command before execution
    violation = _validate_command(command, ctx)
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
