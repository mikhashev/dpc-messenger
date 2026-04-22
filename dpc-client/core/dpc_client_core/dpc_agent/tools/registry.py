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

import asyncio
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
    The agent cannot access files outside this directory unless
    sandbox_extensions are configured in firewall rules.
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

    # Main asyncio event loop — set by DpcAgent.process() so sync tools running in
    # executor threads can schedule async calls back via asyncio.run_coroutine_threadsafe.
    agent_event_loop: Optional[Any] = None

    # ConversationMonitor for knowledge extraction (set by agent_manager)
    conversation_monitor: Optional[Any] = None

    # Tool whitelist (if set, only these tools are allowed)
    tool_whitelist: Optional[set] = None

    # Firewall reference for extended sandbox paths (v0.16.0+)
    firewall: Optional[Any] = None

    # Reply routing for scheduled tasks: set by agent_manager when processing
    # a Telegram message so that schedule_task can propagate it into task data.
    reply_telegram_chat_id: Optional[str] = None

    # Skill store for execute_skill tool (set by DpcAgent.process())
    skill_store: Optional[Any] = None

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

    # -----------------------------------------------------------------------
    # Extended sandbox paths (v0.16.0+)
    # -----------------------------------------------------------------------

    def is_extended_path_allowed(self, path: str, require_write: bool = False) -> bool:
        """
        Check if a path is allowed via sandbox_extensions.

        Args:
            path: Path to check
            require_write: If True, check for write access

        Returns:
            True if the path is allowed for the requested access level
        """
        if not self.firewall:
            return False
        _profile = getattr(getattr(self, "_agent", None), "_firewall_profile", None)
        return self.firewall.is_extended_path_allowed(path, require_write, profile_name=_profile)

    def validate_extended_path(self, path: str, require_write: bool = False) -> pathlib.Path:
        """
        Validate and resolve an extended path.

        Args:
            path: Path to validate
            require_write: If True, require write access

        Returns:
            Resolved path if allowed

        Raises:
            PermissionError: If path is not allowed
        """
        resolved = pathlib.Path(path).expanduser().resolve()

        # First check if it's in the default sandbox
        if is_path_in_sandbox(resolved, self.agent_root):
            return resolved

        # Check extended sandbox
        if self.is_extended_path_allowed(str(resolved), require_write):
            return resolved

        access_type = "read/write" if require_write else "read"
        raise PermissionError(
            f"Sandbox violation: path '{path}' is not in agent directory or extended sandbox ({access_type})"
        )

    def list_extended_paths(self) -> Dict[str, List[str]]:
        """Get all configured extended sandbox paths."""
        if not self.firewall:
            return {'read_only': [], 'read_write': []}
        _profile = getattr(getattr(self, "_agent", None), "_firewall_profile", None)
        return self.firewall.get_extended_paths(profile_name=_profile)


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
    "repo_read", "repo_list", "repo_write_commit", "repo_delete",
    "drive_read", "drive_list", "drive_write",
    # Extended sandbox (v0.16.0+)
    "extended_path_read", "extended_path_list", "extended_path_write", "list_extended_sandbox_paths",
    # Search tools (v0.16.0+)
    "search_files", "search_in_file",
    # Memory/identity
    "update_scratchpad", "update_identity",
    "chat_history",
    # Knowledge
    "knowledge_read", "knowledge_write", "knowledge_list",
    "memory_search",
    # DPC integration
    "get_dpc_context",
    # Browser tools (safe, read-only)
    "browse_page", "fetch_json", "extract_links", "check_url", "search_web",
    # Review tools (safe, analysis only)
    "self_review", "request_critique", "compare_approaches", "quality_checklist", "consensus_check",
    # Messaging tools (agent-to-user communication)
    "send_user_message",
    # Skill router (Read phase of Memento-Skills loop)
    "execute_skill",
    # Self-introspection tools
    "list_my_tools", "list_my_skills",
    # Session archive tools (read-only access to conversation history)
    "read_session_archive", "read_session_detail",
    # Proposal review tools (ADR-013 Selection Layer)
    "list_proposals", "review_proposal",
    # Consciousness-specific
    "set_next_wakeup",
}

# Restricted tools (require explicit enable in config)
RESTRICTED_TOOL_NAMES = {
    "run_shell",           # Shell access
    "claude_code_edit",    # Code editing via Claude
    "repo_commit_push",    # Git push
    "request_restart",     # Control operations
    "promote_to_stable",
    # Git tools (can modify files / history)
    "git_add", "git_commit", "git_init",
    "git_checkout", "git_merge", "git_tag", "git_reset", "git_snapshot",
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
        self._agent_root = agent_root or get_agent_root("default")
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
        import pathlib

        # Get the path to this package (the tools directory)
        tools_path = pathlib.Path(__file__).parent

        tools_loaded = 0
        for _importer, modname, _ispkg in pkgutil.iter_modules([str(tools_path)]):
            if modname.startswith("_") or modname == "registry":
                continue
            try:
                mod = importlib.import_module(f".{modname}", package="dpc_client_core.dpc_agent.tools")
                if hasattr(mod, "get_tools"):
                    for entry in mod.get_tools():
                        self._entries[entry.name] = entry
                        tools_loaded += 1
            except Exception as e:
                log.warning(f"Failed to load tool module {modname}: {e}", exc_info=True)

        if tools_loaded > 0:
            log.info(f"Loaded {tools_loaded} agent tools from {len(self._entries)} modules")

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

    def execute(self, name: str, args: Dict[str, Any], ctx: Optional[ToolContext] = None) -> str:
        """
        Execute a tool by name with the given arguments.

        Handles both sync and async handlers. Async handlers are awaited
        using a properly managed event loop to prevent memory leaks.

        Args:
            ctx: Explicit context for this call. When provided, avoids reading
                 the shared self._ctx — prevents race conditions when multiple
                 loops (consciousness, scheduled tasks) run concurrently.

        Returns:
            Tool result as string (errors prefixed with ⚠️)
        """
        entry = self._entries.get(name)
        if entry is None:
            available = ", ".join(sorted(self._entries.keys()))
            return f"⚠️ Unknown tool: {name}. Available: {available}"

        _ctx = ctx or self._ctx

        _ctx_id = id(_ctx)
        _shared_id = id(self._ctx)
        if _ctx_id != _shared_id:
            log.debug("Tool %s: using isolated ctx %x (shared=%x)", name, _ctx_id, _shared_id)
        elif ctx is None:
            log.warning("Tool %s: using shared ctx %x (no isolation — potential race)", name, _ctx_id)

        # Check whitelist
        if _ctx.tool_whitelist and name not in _ctx.tool_whitelist:
            return f"⚠️ Tool '{name}' is not in the allowed tools list"

        try:
            result = entry.handler(_ctx, **args)
            # Handle async handlers
            if asyncio.iscoroutine(result):
                # Run the coroutine in a properly managed event loop
                # This is safe because execute() is called from a ThreadPoolExecutor
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    result = loop.run_until_complete(result)
                finally:
                    # Clean up the event loop properly to prevent memory leaks
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception:
                        pass
                    loop.close()
                    asyncio.set_event_loop(None)
            return result
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
