"""
DPC Agent — Git Tools.

Provides git operations for the embedded agent:
- Status checking
- Diff viewing
- Commit operations (within sandbox only)

All git operations are restricted to the agent's sandbox directory.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any, Dict, List, Optional

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)


def _run_git(ctx: ToolContext, args: List[str], cwd: Optional[str] = None) -> Dict[str, Any]:
    """
    Run a git command and return result.

    Args:
        ctx: Tool context
        args: Git command arguments
        cwd: Working directory (defaults to agent_root)

    Returns:
        Dict with success, output, and error fields
    """
    import os
    from pathlib import Path

    work_dir = Path(cwd) if cwd else ctx.agent_root

    # Ensure working directory is within sandbox
    try:
        resolved = work_dir.resolve()
        if not str(resolved).startswith(str(ctx.agent_root.resolve())):
            return {
                "success": False,
                "error": f"Sandbox violation: '{cwd}' is outside agent directory",
            }
    except Exception as e:
        return {"success": False, "error": f"Invalid path: {e}"}

    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )

        return {
            "success": result.returncode == 0,
            "output": result.stdout.strip(),
            "error": result.stderr.strip() if result.stderr else None,
            "return_code": result.returncode,
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Git command timed out (30s)"}
    except FileNotFoundError:
        return {"success": False, "error": "Git not found. Please install git."}
    except Exception as e:
        return {"success": False, "error": f"Git error: {e}"}


def git_status(ctx: ToolContext, path: str = ".") -> str:
    """
    Check git status of a directory.

    Args:
        ctx: Tool context
        path: Directory path (relative to agent root)

    Returns:
        Git status output
    """
    result = _run_git(ctx, ["status", "--short", "--branch"], cwd=path)

    if not result["success"]:
        return f"⚠️ Git status failed: {result.get('error', 'Unknown error')}"

    output = result["output"]
    if not output:
        return "Working directory clean"

    return f"Git status:\n{output}"


def git_diff(ctx: ToolContext, path: str = ".", staged: bool = False) -> str:
    """
    View git diff.

    Args:
        ctx: Tool context
        path: Directory or file path
        staged: Show staged changes

    Returns:
        Git diff output
    """
    args = ["diff"]
    if staged:
        args.append("--staged")
    args.append(path)

    result = _run_git(ctx, args)

    if not result["success"]:
        return f"⚠️ Git diff failed: {result.get('error', 'Unknown error')}"

    output = result["output"]
    if not output:
        return "No changes"

    # Truncate large diffs
    if len(output) > 5000:
        output = output[:5000] + f"\n... (truncated, {len(output)} total chars)"

    return f"Git diff:\n{output}"


def git_log(ctx: ToolContext, path: str = ".", count: int = 10) -> str:
    """
    View git log.

    Args:
        ctx: Tool context
        path: Directory path
        count: Number of commits to show

    Returns:
        Git log output
    """
    result = _run_git(ctx, [
        "log",
        f"-{count}",
        "--oneline",
        "--decorate",
        path
    ])

    if not result["success"]:
        return f"⚠️ Git log failed: {result.get('error', 'Unknown error')}"

    output = result["output"]
    if not output:
        return "No commits found"

    return f"Git log ({count} most recent):\n{output}"


def git_add(ctx: ToolContext, files: List[str], path: str = ".") -> str:
    """
    Stage files for commit.

    Args:
        ctx: Tool context
        files: List of file paths to stage
        path: Repository root

    Returns:
        Result message
    """
    if not files:
        return "⚠️ No files specified"

    result = _run_git(ctx, ["add"] + files, cwd=path)

    if not result["success"]:
        return f"⚠️ Git add failed: {result.get('error', 'Unknown error')}"

    return f"Staged {len(files)} file(s)"


def git_commit(ctx: ToolContext, message: str, path: str = ".") -> str:
    """
    Create a git commit.

    Args:
        ctx: Tool context
        message: Commit message
        path: Repository root

    Returns:
        Result message
    """
    if not message:
        return "⚠️ Commit message required"

    result = _run_git(ctx, ["commit", "-m", message], cwd=path)

    if not result["success"]:
        error = result.get("error", "")
        if "nothing to commit" in error.lower():
            return "Nothing to commit (working directory clean)"
        return f"⚠️ Git commit failed: {error}"

    return f"Committed: {result['output']}"


def git_branch(ctx: ToolContext, path: str = ".") -> str:
    """
    List git branches.

    Args:
        ctx: Tool context
        path: Repository root

    Returns:
        Branch list
    """
    result = _run_git(ctx, ["branch", "-a"], cwd=path)

    if not result["success"]:
        return f"⚠️ Git branch failed: {result.get('error', 'Unknown error')}"

    output = result["output"]
    if not output:
        return "No branches found"

    return f"Git branches:\n{output}"


def git_init(ctx: ToolContext, path: str = ".") -> str:
    """
    Initialize a git repository.

    Args:
        ctx: Tool context
        path: Directory to initialize

    Returns:
        Result message
    """
    result = _run_git(ctx, ["init"], cwd=path)

    if not result["success"]:
        return f"⚠️ Git init failed: {result.get('error', 'Unknown error')}"

    return f"Initialized git repository in {path}"


def get_tools() -> List[ToolEntry]:
    """Export git tools for registry."""
    return [
        ToolEntry(
            name="git_status",
            schema={
                "name": "git_status",
                "description": "Check git status of a directory within the agent sandbox",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory path (relative to agent root)",
                            "default": "."
                        }
                    },
                    "required": []
                }
            },
            fn=git_status,
            timeout=30,
        ),

        ToolEntry(
            name="git_diff",
            schema={
                "name": "git_diff",
                "description": "View git diff of changes within the agent sandbox",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory or file path",
                            "default": "."
                        },
                        "staged": {
                            "type": "boolean",
                            "description": "Show staged changes instead of unstaged",
                            "default": False
                        }
                    },
                    "required": []
                }
            },
            fn=git_diff,
            timeout=30,
        ),

        ToolEntry(
            name="git_log",
            schema={
                "name": "git_log",
                "description": "View git commit history within the agent sandbox",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory path",
                            "default": "."
                        },
                        "count": {
                            "type": "integer",
                            "description": "Number of commits to show",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 50
                        }
                    },
                    "required": []
                }
            },
            fn=git_log,
            timeout=30,
        ),

        ToolEntry(
            name="git_add",
            schema={
                "name": "git_add",
                "description": "Stage files for commit within the agent sandbox",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of file paths to stage"
                        },
                        "path": {
                            "type": "string",
                            "description": "Repository root",
                            "default": "."
                        }
                    },
                    "required": ["files"]
                }
            },
            fn=git_add,
            timeout=30,
        ),

        ToolEntry(
            name="git_commit",
            schema={
                "name": "git_commit",
                "description": "Create a git commit within the agent sandbox",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "Commit message"
                        },
                        "path": {
                            "type": "string",
                            "description": "Repository root",
                            "default": "."
                        }
                    },
                    "required": ["message"]
                }
            },
            fn=git_commit,
            timeout=30,
        ),

        ToolEntry(
            name="git_branch",
            schema={
                "name": "git_branch",
                "description": "List git branches within the agent sandbox",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Repository root",
                            "default": "."
                        }
                    },
                    "required": []
                }
            },
            fn=git_branch,
            timeout=30,
        ),

        ToolEntry(
            name="git_init",
            schema={
                "name": "git_init",
                "description": "Initialize a git repository within the agent sandbox",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory to initialize",
                            "default": "."
                        }
                    },
                    "required": []
                }
            },
            fn=git_init,
            timeout=30,
        ),
    ]
