"""
DPC Agent — Shell Tool (run_shell).

Executes shell commands in a subprocess with timeout and output capture.
Cross-platform: uses cmd.exe on Windows, /bin/sh on Unix.
Restricted tool — requires explicit enable in privacy_rules.json.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
from typing import List

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

MAX_OUTPUT = 50_000  # chars


def run_shell(ctx: ToolContext, command: str, timeout: int = 120, cwd: str = "") -> str:
    working_dir: str | None = None
    if cwd:
        expanded = os.path.expanduser(cwd)
        if not os.path.isdir(expanded):
            return f"Error: cwd '{cwd}' is not a valid directory."
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
