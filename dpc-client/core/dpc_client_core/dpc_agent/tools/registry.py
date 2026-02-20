"""
DPC Agent — Tool registry (SSOT with sandbox).

Adapted from Ouroboros tools/registry.py for DPC Messenger integration.
Key changes:
- All file operations sandboxed to ~/.dpc/agent/
- No repo_dir (agent doesn't access DPC codebase)
- Uses agent_root instead of drive_root
- Removed browser state (not needed for MVP)
- Simplified context for DPC integration

Plugin architecture: each module in tools/ exports get_tools().
ToolRegistry collects all tools, provides schemas() and execute().
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..utils import safe_relpath, is_path_in_sandbox, get_agent_root

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """
    Tool execution context — passed from the agent before each task.

    All file operations are sandboxed to agent_root (~/.dpc/agent/).
    The agent cannot access files outside this directory.
    """

    agent_root: pathlib.Path  # ~/.dpc/agent/ - sandboxed storage
    pending_events: List[Dict[str, Any]] = field(default_factory=list)
    current_task_id: Optional[str] = None
    current_task_type: Optional[str] = None
    emit_progress_fn: Callable[[str], None] = field(default=lambda _: None)

    # LLM-driven model/effort switch (set by switch_model tool, read by loop)
    active_model_override: Optional[str] = None
    active_effort_override: Optional[str] = None

    # Reference to DPC service for DPC-specific tools
    dpc_service: Optional[Any] = None

    # Tool whitelist (if set, only these tools are allowed)
    tool_whitelist: Optional[set] = None

    # -----------------------------------------------------------------------
    # Path helpers (all sandboxed to agent_root)
    # -----------------------------------------------------------------------

    def repo_path(self, rel: str) -> pathlib.Path:
        """
        Get path within agent sandbox.

        This replaces Ouroboros's repo_path which pointed to the codebase.
        Here it points to agent_root for sandboxed file operations.
        """
        resolved = (self.agent_root / safe_relpath(rel)).resolve()
        # Enforce sandbox
        if not is_path_in_sandbox(resolved, self.agent_root):
            raise PermissionError(f"Sandbox violation: path '{rel}' is outside agent directory")
        return resolved

    def drive_path(self, rel: str) -> pathlib.Path:
        """Alias for repo_path (for compatibility with Ouroboros tools)."""
        return self.repo_path(rel)

    def memory_path(self, rel: str) -> pathlib.Path:
        """Get path to memory files (scratchpad, identity, etc.)."""
        return self.repo_path(f"memory/{safe_relpath(rel)}")

    def logs_path(self, name: str) -> pathlib.Path:
        """Get path to log files."""
        return self.repo_path(f"logs/{safe_relpath(name)}")

    def knowledge_path(self, topic: str) -> pathlib.Path:
        """Get path to knowledge base files."""
        return self.repo_path(f"knowledge/{safe_relpath(topic)}.md")

    def state_path(self) -> pathlib.Path:
        """Get path to state directory."""
        return self.repo_path("state")

    def task_results_path(self) -> pathlib.Path:
        """Get path to task results directory."""
        return self.repo_path("task_results")

    # -----------------------------------------------------------------------
    # Event helpers
    # -----------------------------------------------------------------------

    def emit_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Queue an event to be processed after tool execution."""
        self.pending_events.append({
            "type": event_type,
            "ts": data.get("ts"),
            **data
        })

    def emit_progress(self, message: str) -> None:
        """Emit a progress message (for UI feedback)."""
        try:
            self.emit_progress_fn(message)
        except Exception:
            log.debug(f"Failed to emit progress: {message}")


@dataclass
class ToolEntry:
    """Single tool descriptor: name, schema, handler, metadata."""

    name: str
    schema: Dict[str, Any]  # OpenAI function schema
    handler: Callable  # fn(ctx: ToolContext, **args) -> str
    is_code_tool: bool = False  # Tools that modify code
    timeout_sec: int = 120
    is_core: bool = True  # Core tools loaded by default


# Core tools that are always available
CORE_TOOL_NAMES = {
    # File operations (sandboxed)
    "repo_read", "repo_list", "repo_write_commit",
    "drive_read", "drive_list", "drive_write",
    # Memory/identity
    "update_scratchpad", "update_identity",
    "chat_history",
    # Knowledge
    "knowledge_read", "knowledge_write", "knowledge_list",
    # DPC integration
    "get_dpc_context",
    # Utility
    "web_search",
    # Browser tools (safe, read-only)
    "browse_page", "fetch_json", "extract_links", "check_url", "search_web",
    # Review tools (safe, analysis only)
    "self_review", "request_critique", "compare_approaches", "quality_checklist", "consensus_check",
}

# Restricted tools (require explicit enable in config)
RESTRICTED_TOOL_NAMES = {
    "run_shell",           # Shell access
    "claude_code_edit",    # Code editing via Claude
    "repo_commit_push",    # Git push
    "request_restart",     # Control operations
    "promote_to_stable",
    # Git tools (can modify files)
    "git_add", "git_commit", "git_init",
}


class ToolRegistry:
    """
    DPC Agent tool registry (SSOT with sandbox enforcement).

    To add a tool: create a module in dpc_agent/tools/,
    export get_tools() -> List[ToolEntry].

    All file operations are sandboxed to agent_root (~/.dpc/agent/).
    """

    CODE_TOOLS = frozenset({
        "repo_write_commit", "claude_code_edit", "run_shell"
    })

    def __init__(self, agent_root: Optional[pathlib.Path] = None):
        """
        Initialize the tool registry.

        Args:
            agent_root: Root directory for agent storage (defaults to ~/.dpc/agent/)
        """
        self._entries: Dict[str, ToolEntry] = {}
        self._agent_root = agent_root or get_agent_root()
        self._ctx = ToolContext(agent_root=self._agent_root)
        self._load_modules()

    def _load_modules(self) -> None:
        """
        Auto-discover tool modules in dpc_agent/tools/ that export get_tools().

        This allows tools to be added by simply creating a new module
        with a get_tools() function.
        """
        import importlib
        import pkgutil

        try:
            from . import tools as tools_pkg
            tools_path = tools_pkg.__path__
        except ImportError:
            # If tools package doesn't exist yet, skip module loading
            log.debug("Tools package not found, skipping module loading")
            return

        for _importer, modname, _ispkg in pkgutil.iter_modules(tools_path):
            if modname.startswith("_") or modname == "registry":
                continue
            try:
                mod = importlib.import_module(f".{modname}", package="dpc_client_core.dpc_agent.tools")
                if hasattr(mod, "get_tools"):
                    for entry in mod.get_tools():
                        self._entries[entry.name] = entry
                        log.debug(f"Loaded tool: {entry.name}")
            except Exception as e:
                log.warning(f"Failed to load tool module {modname}: {e}", exc_info=True)

    def set_context(self, ctx: ToolContext) -> None:
        """Set the execution context for subsequent tool calls."""
        self._ctx = ctx

    def register(self, entry: ToolEntry) -> None:
        """Register a new tool (for runtime extension)."""
        self._entries[entry.name] = entry

    def unregister(self, name: str) -> bool:
        """Unregister a tool. Returns True if tool existed."""
        if name in self._entries:
            del self._entries[name]
            return True
        return False

    # -----------------------------------------------------------------------
    # Contract (same as Ouroboros for compatibility)
    # -----------------------------------------------------------------------

    def available_tools(self) -> List[str]:
        """Return list of available tool names."""
        return [e.name for e in self._entries.values()]

    def schemas(self, core_only: bool = False, include_restricted: bool = False) -> List[Dict[str, Any]]:
        """
        Return tool schemas in OpenAI function format.

        Args:
            core_only: If True, only return core tools
            include_restricted: If True, include restricted tools
        """
        result = []
        for e in self._entries.values():
            # Filter by core/non-core
            if core_only and e.name not in CORE_TOOL_NAMES:
                continue
            # Filter restricted tools
            if not include_restricted and e.name in RESTRICTED_TOOL_NAMES:
                continue
            # Check whitelist
            if self._ctx.tool_whitelist and e.name not in self._ctx.tool_whitelist:
                continue

            result.append({"type": "function", "function": e.schema})
        return result

    def list_non_core_tools(self) -> List[Dict[str, str]]:
        """Return name+description of all non-core tools."""
        result = []
        for e in self._entries.values():
            if e.name not in CORE_TOOL_NAMES:
                desc = e.schema.get("description", "No description")
                result.append({"name": e.name, "description": desc})
        return result

    def get_schema_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the full schema for a specific tool."""
        entry = self._entries.get(name)
        if entry:
            return {"type": "function", "function": entry.schema}
        return None

    def get_timeout(self, name: str) -> int:
        """Return timeout_sec for the named tool (default 120)."""
        entry = self._entries.get(name)
        return entry.timeout_sec if entry is not None else 120

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        """
        Execute a tool by name with the given arguments.

        Returns:
            Tool result as string (errors prefixed with ⚠️)
        """
        entry = self._entries.get(name)
        if entry is None:
            available = ", ".join(sorted(self._entries.keys()))
            return f"⚠️ Unknown tool: {name}. Available: {available}"

        # Check whitelist
        if self._ctx.tool_whitelist and name not in self._ctx.tool_whitelist:
            return f"⚠️ Tool '{name}' is not in the allowed tools list"

        try:
            return entry.handler(self._ctx, **args)
        except TypeError as e:
            return f"⚠️ TOOL_ARG_ERROR ({name}): {e}"
        except PermissionError as e:
            return f"⚠️ SANDBOX_VIOLATION ({name}): {e}"
        except Exception as e:
            log.error(f"Tool error in {name}: {e}", exc_info=True)
            return f"⚠️ TOOL_ERROR ({name}): {type(e).__name__}: {e}"

    def override_handler(self, name: str, handler: Callable) -> None:
        """Override the handler for a registered tool (used for closure injection)."""
        entry = self._entries.get(name)
        if entry:
            self._entries[name] = ToolEntry(
                name=entry.name,
                schema=entry.schema,
                handler=handler,
                is_code_tool=entry.is_code_tool,
                timeout_sec=entry.timeout_sec,
                is_core=entry.is_core,
            )
