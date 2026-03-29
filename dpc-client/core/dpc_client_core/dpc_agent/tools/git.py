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
import re
import subprocess
from typing import Any, Dict, List, Optional

from .registry import ToolEntry, ToolContext

# Conventional commits: type(optional-scope): description
# Types: feat fix docs style refactor perf test chore build ci revert
_CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|chore|build|ci|revert)"
    r"(\([^)]+\))?"
    r"!?:"
    r" .+",
    re.IGNORECASE,
)

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

    # If path is relative, join with agent_root before resolving
    if cwd and not work_dir.is_absolute():
        work_dir = ctx.agent_root / work_dir

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


def _run_git_external(repo_path: str, args: List[str]) -> Dict[str, Any]:
    """
    Run a read-only git command in an external (non-sandbox) repository.

    Security requirements:
    - repo_path must be an absolute path (rejects relative paths and traversal)
    - repo_path must exist and contain a .git directory
    - Only called from read-only tools (git_log, git_diff, git_branch, git_checkout)

    Args:
        repo_path: Absolute path to the external git repository
        args: Git command arguments

    Returns:
        Dict with success, output, and error fields
    """
    from pathlib import Path

    p = Path(repo_path)

    if not p.is_absolute():
        return {"success": False, "error": f"repo_path must be an absolute path, got: {repo_path!r}"}

    try:
        resolved = p.resolve()
    except Exception as e:
        return {"success": False, "error": f"Invalid repo_path: {e}"}

    if not resolved.exists():
        return {"success": False, "error": f"Path does not exist: {resolved}"}

    if not (resolved / ".git").exists():
        return {"success": False, "error": f"Not a git repository (no .git found): {resolved}"}

    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(resolved),
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


def git_diff(ctx: ToolContext, path: str = ".", staged: bool = False, repo_path: Optional[str] = None) -> str:
    """
    View git diff.

    Args:
        ctx: Tool context
        path: Directory or file path (relative, within repo)
        staged: Show staged changes
        repo_path: Optional absolute path to an external git repository.
                   When provided, runs git diff there instead of the agent sandbox.

    Returns:
        Git diff output
    """
    args = ["diff"]
    if staged:
        args.append("--staged")
    args.append(path)

    if repo_path is not None:
        result = _run_git_external(repo_path, args)
    else:
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


def git_log(ctx: ToolContext, path: str = ".", count: int = 10, repo_path: Optional[str] = None, branch: Optional[str] = None) -> str:
    """
    View git log.

    Args:
        ctx: Tool context
        path: File or directory path to filter history (relative, within repo)
        count: Number of commits to show
        repo_path: Optional absolute path to an external git repository.
                   When provided, queries history there instead of the agent sandbox.
        branch: Optional branch (or any ref) to query. Reads the current branch when omitted.

    Returns:
        Git log output with full commit subjects and bodies
    """
    args = ["log", f"-{count}", "--format=%C(yellow)%h%C(reset) %C(cyan)%D%C(reset)%n%s%n%b"]
    if branch:
        args.append(branch)
    args.append(path)

    if repo_path is not None:
        result = _run_git_external(repo_path, args)
    else:
        result = _run_git(ctx, args)

    if not result["success"]:
        return f"⚠️ Git log failed: {result.get('error', 'Unknown error')}"

    output = result["output"]
    if not output:
        return "No commits found"

    location = repo_path if repo_path is not None else "agent sandbox"
    ref = branch or "current branch"
    return f"Git log ({count} most recent, {ref}) [{location}]:\n{output}"


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

    Message MUST follow Conventional Commits: type(optional-scope): description
    Valid types: feat, fix, docs, style, refactor, perf, test, chore, build, ci, revert
    Examples:
        "feat(knowledge): add TurboQuant complexity analysis"
        "chore(identity): refine core values after security audit"
        "refactor: consolidate knowledge files by topic"
        "fix: correct outdated GPU model in device context"

    Args:
        ctx: Tool context
        message: Conventional Commits message (type(scope): description)
        path: Repository root

    Returns:
        Result message
    """
    if not message:
        return "⚠️ Commit message required"

    if not _CONVENTIONAL_COMMIT_RE.match(message):
        return (
            "⚠️ Commit message must follow Conventional Commits format: type(scope): description\n"
            "Valid types: feat, fix, docs, style, refactor, perf, test, chore, build, ci, revert\n"
            f"Got: {message!r}"
        )

    result = _run_git(ctx, ["commit", "-m", message], cwd=path)

    if not result["success"]:
        error = result.get("error", "")
        if "nothing to commit" in error.lower():
            return "Nothing to commit (working directory clean)"
        return f"⚠️ Git commit failed: {error}"

    return f"Committed: {result['output']}"


def git_branch(ctx: ToolContext, path: str = ".", repo_path: Optional[str] = None) -> str:
    """
    List git branches.

    Args:
        ctx: Tool context
        path: Repository root (used only for sandbox mode)
        repo_path: Optional absolute path to an external git repository.
                   When provided, lists branches there instead of the agent sandbox.

    Returns:
        Branch list
    """
    if repo_path is not None:
        result = _run_git_external(repo_path, ["branch", "-a"])
    else:
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


def git_checkout(ctx: ToolContext, branch: str, create: bool = False, path: str = ".", repo_path: Optional[str] = None) -> str:
    """
    Switch to a branch, or create and switch to a new branch.

    Args:
        ctx: Tool context
        branch: Branch name to switch to
        create: If True, create the branch before switching (-b flag)
        path: Repository root (used only for sandbox mode)
        repo_path: Optional absolute path to an external git repository.
                   When provided, switches branches there instead of the agent sandbox.
                   Note: create=True is not allowed for external repos.

    Returns:
        Result message
    """
    if repo_path is not None and create:
        return "⚠️ Cannot create new branches in external repositories (create=True not allowed with repo_path)"

    args = ["checkout"]
    if create:
        args.append("-b")
    args.append(branch)

    if repo_path is not None:
        result = _run_git_external(repo_path, args)
    else:
        result = _run_git(ctx, args, cwd=path)

    if not result["success"]:
        return f"⚠️ Git checkout failed: {result.get('error', 'Unknown error')}"

    return result["output"] or f"Switched to branch '{branch}'"


def git_merge(ctx: ToolContext, branch: str, no_ff: bool = False, path: str = ".") -> str:
    """
    Merge a branch into the current branch.

    Args:
        ctx: Tool context
        branch: Branch name to merge
        no_ff: Use --no-ff to always create a merge commit
        path: Repository root

    Returns:
        Result message (includes conflict details if merge failed)
    """
    args = ["merge"]
    if no_ff:
        args.append("--no-ff")
    args.append(branch)

    result = _run_git(ctx, args, cwd=path)

    if not result["success"]:
        output = result.get("output", "") or ""
        error = result.get("error", "") or ""
        if "CONFLICT" in output or "conflict" in error.lower():
            return (
                f"⚠️ Merge conflicts detected:\n{output}\n"
                "Resolve conflicts manually, then git_add the files and git_commit."
            )
        return f"⚠️ Git merge failed: {error}"

    return result["output"] or f"Merged branch '{branch}'"


def git_tag(ctx: ToolContext, name: str, message: Optional[str] = None, path: str = ".") -> str:
    """
    Create a git tag on the current commit.

    Args:
        ctx: Tool context
        name: Tag name (e.g. 'milestone-v1', 'before-cleanup-20260327')
        message: If provided, creates an annotated tag with this message
        path: Repository root

    Returns:
        Result message
    """
    if message:
        args = ["tag", "-a", name, "-m", message]
    else:
        args = ["tag", name]

    result = _run_git(ctx, args, cwd=path)

    if not result["success"]:
        return f"⚠️ Git tag failed: {result.get('error', 'Unknown error')}"

    return f"Created tag '{name}'"


def git_reset(
    ctx: ToolContext,
    ref: str = "HEAD",
    hard: bool = False,
    files: Optional[List[str]] = None,
    path: str = ".",
) -> str:
    """
    Reset files or commits.

    Modes (in order of safety):
    - files provided: restore specific files from ref (safe, no history change)
    - hard=False (default): soft reset — moves HEAD, keeps working tree intact
    - hard=True: hard reset — moves HEAD AND discards working tree changes

    Args:
        ctx: Tool context
        ref: Commit ref to reset to (default: HEAD)
        hard: If True, perform a hard reset (destructive — use with care)
        files: If provided, restore only these specific files from ref
        path: Repository root

    Returns:
        Result message
    """
    if files:
        # Restore specific files only — safest option
        args = ["checkout", ref, "--"] + files
        result = _run_git(ctx, args, cwd=path)
        if not result["success"]:
            return f"⚠️ Git reset (file restore) failed: {result.get('error', 'Unknown error')}"
        return f"Restored {len(files)} file(s) from {ref}"

    if hard:
        args = ["reset", "--hard", ref]
    else:
        args = ["reset", ref]

    result = _run_git(ctx, args, cwd=path)

    if not result["success"]:
        return f"⚠️ Git reset failed: {result.get('error', 'Unknown error')}"

    return result["output"] or f"Reset to {ref}"


def git_snapshot(ctx: ToolContext, path: str = ".") -> str:
    """
    Quick snapshot: stage all changes and commit with a UTC timestamp message.

    Commit message format: 'snapshot: 2026-03-27T14:30:00Z'

    Use this before experiments, restructuring, or any time you want a
    quick save point without thinking about a commit message.

    Args:
        ctx: Tool context
        path: Repository root

    Returns:
        Commit hash and message, or 'nothing to commit' if no changes
    """
    import datetime as _dt

    timestamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    message = f"snapshot: {timestamp}"

    add_result = _run_git(ctx, ["add", "-A"], cwd=path)
    if not add_result["success"]:
        return f"⚠️ Failed to stage files: {add_result.get('error', 'Unknown error')}"

    commit_result = _run_git(ctx, ["commit", "-m", message], cwd=path)
    if not commit_result["success"]:
        combined = (commit_result.get("output") or "") + (commit_result.get("error") or "")
        if "nothing to commit" in combined.lower():
            return "Nothing to commit (working directory clean)"
        return f"⚠️ Snapshot commit failed: {commit_result.get('error', 'Unknown error')}"

    return f"Snapshot created: {message}\n{commit_result['output']}"


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
            handler=git_status,
            timeout_sec=30,
        ),

        ToolEntry(
            name="git_diff",
            schema={
                "name": "git_diff",
                "description": (
                    "View git diff of changes. Defaults to agent sandbox. "
                    "Provide repo_path (absolute path) to inspect an external repository."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory or file path (relative, within repo)",
                            "default": "."
                        },
                        "staged": {
                            "type": "boolean",
                            "description": "Show staged changes instead of unstaged",
                            "default": False
                        },
                        "repo_path": {
                            "type": "string",
                            "description": (
                                "Absolute path to an external git repository "
                                "(e.g. 'C:\\\\Users\\\\mike\\\\Documents\\\\dpc-messenger'). "
                                "When provided, runs git diff there instead of the agent sandbox."
                            )
                        }
                    },
                    "required": []
                }
            },
            handler=git_diff,
            timeout_sec=30,
        ),

        ToolEntry(
            name="git_log",
            schema={
                "name": "git_log",
                "description": (
                    "View git commit history with full subject and body. "
                    "Defaults to agent sandbox. "
                    "Provide repo_path (absolute path) to inspect an external repository "
                    "such as the main dpc-messenger repo."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File or directory path to filter history (relative, within repo)",
                            "default": "."
                        },
                        "count": {
                            "type": "integer",
                            "description": "Number of commits to show",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 50
                        },
                        "repo_path": {
                            "type": "string",
                            "description": (
                                "Absolute path to an external git repository "
                                "(e.g. 'C:\\\\Users\\\\mike\\\\Documents\\\\dpc-messenger'). "
                                "When provided, queries history there instead of the agent sandbox."
                            )
                        },
                        "branch": {
                            "type": "string",
                            "description": (
                                "Branch or ref to query (e.g. 'main', 'refactor/grand', 'HEAD~5'). "
                                "Reads the currently checked-out branch when omitted. "
                                "Use this instead of git_checkout to inspect any branch without switching."
                            )
                        }
                    },
                    "required": []
                }
            },
            handler=git_log,
            timeout_sec=30,
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
            handler=git_add,
            timeout_sec=30,
        ),

        ToolEntry(
            name="git_commit",
            schema={
                "name": "git_commit",
                "description": (
                    "Create a git commit within the agent sandbox. "
                    "REQUIRED: message must follow Conventional Commits — "
                    "type(optional-scope): description — "
                    "where type is one of: feat, fix, docs, style, refactor, perf, test, chore, build, ci, revert. "
                    "Examples: 'feat(knowledge): add TurboQuant complexity analysis', "
                    "'chore(identity): refine core values after security audit', "
                    "'refactor: consolidate knowledge files by topic'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": (
                                "Conventional Commits message: type(optional-scope): description. "
                                "Write a meaningful description — you own this commit message."
                            )
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
            handler=git_commit,
            timeout_sec=30,
        ),

        ToolEntry(
            name="git_branch",
            schema={
                "name": "git_branch",
                "description": (
                    "List git branches. Defaults to agent sandbox. "
                    "Provide repo_path (absolute path) to inspect an external repository."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Repository root (used only for sandbox mode)",
                            "default": "."
                        },
                        "repo_path": {
                            "type": "string",
                            "description": (
                                "Absolute path to an external git repository "
                                "(e.g. 'C:\\\\Users\\\\mike\\\\Documents\\\\dpc-messenger'). "
                                "When provided, lists branches there instead of the agent sandbox."
                            )
                        }
                    },
                    "required": []
                }
            },
            handler=git_branch,
            timeout_sec=30,
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
            handler=git_init,
            timeout_sec=30,
        ),

        ToolEntry(
            name="git_checkout",
            schema={
                "name": "git_checkout",
                "description": (
                    "Switch to a branch. Defaults to agent sandbox. "
                    "Provide repo_path (absolute path) to switch branches in an external repository. "
                    "Note: create=True is not allowed with repo_path."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "branch": {
                            "type": "string",
                            "description": "Branch name to switch to (e.g. 'main', 'refactor/grand')"
                        },
                        "create": {
                            "type": "boolean",
                            "description": "Create the branch before switching (git checkout -b). Not allowed with repo_path.",
                            "default": False
                        },
                        "path": {
                            "type": "string",
                            "description": "Repository root (used only for sandbox mode)",
                            "default": "."
                        },
                        "repo_path": {
                            "type": "string",
                            "description": (
                                "Absolute path to an external git repository "
                                "(e.g. 'C:\\\\Users\\\\mike\\\\Documents\\\\dpc-messenger'). "
                                "When provided, switches branches there instead of the agent sandbox."
                            )
                        }
                    },
                    "required": ["branch"]
                }
            },
            handler=git_checkout,
            timeout_sec=30,
        ),

        ToolEntry(
            name="git_merge",
            schema={
                "name": "git_merge",
                "description": "Merge a branch into the current branch within the agent sandbox",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "branch": {
                            "type": "string",
                            "description": "Branch name to merge into current branch"
                        },
                        "no_ff": {
                            "type": "boolean",
                            "description": "Always create a merge commit (--no-ff), preserving branch history",
                            "default": False
                        },
                        "path": {
                            "type": "string",
                            "description": "Repository root",
                            "default": "."
                        }
                    },
                    "required": ["branch"]
                }
            },
            handler=git_merge,
            timeout_sec=30,
        ),

        ToolEntry(
            name="git_tag",
            schema={
                "name": "git_tag",
                "description": "Create a git tag on the current commit within the agent sandbox",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Tag name (e.g. 'milestone-security-audit', 'before-cleanup-20260327')"
                        },
                        "message": {
                            "type": "string",
                            "description": "Optional message for an annotated tag"
                        },
                        "path": {
                            "type": "string",
                            "description": "Repository root",
                            "default": "."
                        }
                    },
                    "required": ["name"]
                }
            },
            handler=git_tag,
            timeout_sec=30,
        ),

        ToolEntry(
            name="git_reset",
            schema={
                "name": "git_reset",
                "description": (
                    "Reset files or commits within the agent sandbox. "
                    "Safest with 'files' (restores specific files). "
                    "hard=True discards all uncommitted changes — use with care."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "string",
                            "description": "Commit ref to reset to (default: HEAD)",
                            "default": "HEAD"
                        },
                        "hard": {
                            "type": "boolean",
                            "description": "Perform a hard reset, discarding working tree changes (destructive)",
                            "default": False
                        },
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "If provided, restore only these files from ref (safest option)"
                        },
                        "path": {
                            "type": "string",
                            "description": "Repository root",
                            "default": "."
                        }
                    },
                    "required": []
                }
            },
            handler=git_reset,
            timeout_sec=30,
        ),

        ToolEntry(
            name="git_snapshot",
            schema={
                "name": "git_snapshot",
                "description": (
                    "Quick save: stage all changes and commit with a UTC timestamp message "
                    "(e.g. 'snapshot: 2026-03-27T14:30:00Z'). "
                    "Use before experiments or restructuring when you want a save point without crafting a message."
                ),
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
            handler=git_snapshot,
            timeout_sec=30,
        ),
    ]
