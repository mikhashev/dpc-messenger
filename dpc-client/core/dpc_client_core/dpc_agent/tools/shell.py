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

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

MAX_OUTPUT = 50_000  # chars

# ---------------------------------------------------------------------------
# ADR-030: Safety guardrails — hardcoded, not configurable
# ---------------------------------------------------------------------------

# Tier 2 — unconditional block (catastrophic commands)
HARDLINE_PATTERNS: list[re.Pattern] = [
    # Mass delete (Linux)
    re.compile(r"\brm\b.*\s+-[a-zA-Z]*r[a-zA-Z]*f|rm\b.*\s+-[a-zA-Z]*f[a-zA-Z]*r", re.I),
    re.compile(r"\brm\b\s+.*(/|~|\$HOME)", re.I),
    # Mass delete (Windows)
    re.compile(r"\b(rd|rmdir)\b.*\s+/s", re.I),
    re.compile(r"\bdel\b\s+/s", re.I),
    # Disk format / erase
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bformat\s+[A-Za-z]:", re.I),
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
    # Encoded commands
    re.compile(r"\bpowershell\b.*(-enc|-encodedcommand)", re.I),
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
    """Split command by pipe/chain operators to check each segment."""
    return re.split(r"\s*[|;&]\s*|\s*&&\s*|\s*\|\|\s*", command)


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
        rules = ctx.firewall.rules
        profiles = rules.get("agent_profiles", {})
        profile = profiles.get(profile_name, {})
        tools_block = profile.get("tools", {})
        shell_block = tools_block.get("run_shell", {})
        if isinstance(shell_block, dict):
            wl = shell_block.get("tier1_whitelist", [])
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

    segments = _split_segments(normalized)
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        for pattern in HARDLINE_PATTERNS:
            if pattern.search(segment):
                return ("tier2", f"Blocked by HARDLINE pattern: {pattern.pattern}")
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(segment):
                whitelist = _get_tier1_whitelist(ctx)
                if _is_whitelisted(command, whitelist):
                    return None
                return ("tier1", f"Requires approval: {pattern.pattern}")

    return None


_pending_approvals: dict = {}


def _request_approval(ctx: ToolContext, command: str, reason: str, cwd: str, timeout: int) -> str:
    """Request user approval for a Tier 1 command (Variant B).

    Returns a pending message to the agent. The actual execution happens
    asynchronously when the user approves via shell_approve_command WS.
    Result is broadcast to chat — agent sees it on the next turn.
    """
    import uuid

    request_id = str(uuid.uuid4())[:8]
    agent_name = getattr(getattr(ctx, "_agent", None), "display_name", "Agent")

    _pending_approvals[request_id] = {
        "command": command,
        "cwd": cwd or str(ctx.agent_root),
        "timeout": timeout,
        "agent_name": agent_name,
        "ctx": ctx,
    }

    ctx.pending_events.append({
        "type": "shell_approval_request",
        "request_id": request_id,
        "command": command,
        "reason": reason,
        "agent_name": agent_name,
    })

    log.info("run_shell TIER1 approval requested: %r (id=%s)", command, request_id)
    return f"⏳ Command requires approval: `{command}`\nWaiting for user decision. Result will appear in chat after approval."


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

    is_windows = platform.system() == "Windows"
    timeout = min(max(timeout, 5), 300)

    log.info("run_shell: %s (cwd=%s, timeout=%ds)", command, working_dir, timeout)

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=working_dir,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

        parts = []
        if result.stdout:
            stdout = result.stdout
            if len(stdout) > MAX_OUTPUT:
                stdout = stdout[:MAX_OUTPUT] + f"\n... (truncated, {len(result.stdout)} total chars)"
            parts.append(stdout)
        if result.stderr:
            stderr = result.stderr
            if len(stderr) > MAX_OUTPUT:
                stderr = stderr[:MAX_OUTPUT] + f"\n... (truncated, {len(result.stderr)} total chars)"
            parts.append(f"[stderr]\n{stderr}")
        if result.returncode != 0:
            parts.append(f"[exit code: {result.returncode}]")

        output = "\n".join(parts) if parts else "(no output)"
        return output

    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s."
    except Exception as e:
        log.error("run_shell failed: %s", e)
        return f"Error: {e}"


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
