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
import inspect
import json
import logging
import math
import pathlib
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..utils import safe_relpath, is_path_in_sandbox, get_agent_root

log = logging.getLogger(__name__)

# Names models reach for that mean a parameter we already have. Renaming beats
# failing the call: the intent is unambiguous and the alternative is a round spent
# on a TypeError the model then has to guess its way out of.
ARG_ALIASES: Dict[str, str] = {
    "file_path": "path",
    "filepath": "path",
}


def _resolve_arg_aliases(handler: Callable, args: Dict[str, Any], tool_name: str) -> Dict[str, Any]:
    """Rename an aliased argument onto the parameter the handler actually takes.

    Only when the rename is unambiguous: the handler must accept the canonical name
    and not the alias, and the caller must not have supplied both. Anything else is
    left alone so a genuine argument error still surfaces as one.
    """
    if not args or not any(alias in args for alias in ARG_ALIASES):
        return args

    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return args
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return args

    resolved = dict(args)
    for alias, canonical in ARG_ALIASES.items():
        if alias not in resolved:
            continue
        if alias in params or canonical not in params or canonical in resolved:
            continue
        resolved[canonical] = resolved.pop(alias)
        log.debug("Tool %s: argument %r accepted as %r", tool_name, alias, canonical)
    return resolved


_JSON_TYPE_OF = {bool: "boolean", int: "integer", float: "number", str: "string",
                 list: "array", dict: "object", type(None): "null"}
_INTEGER_TEXT = re.compile(r"[+-]?\d+")


def _json_type(value: Any) -> str:
    return _JSON_TYPE_OF.get(type(value), type(value).__name__)


def _declared_types(prop: Any) -> Optional[List[str]]:
    """The JSON types a schema property admits, or None when it does not say.

    `"type": "x"`, `"type": [...]` and an `anyOf`/`oneOf` whose every branch
    names a type are read; anything else (no type, a `$ref`, a branch without
    one) is unknown, and an unknown property is left to the handler.
    """
    if not isinstance(prop, dict):
        return None
    t = prop.get("type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list) and t and all(isinstance(x, str) for x in t):
        return list(t)
    for key in ("anyOf", "oneOf"):
        branches = prop.get(key)
        if isinstance(branches, list) and branches:
            out: List[str] = []
            for b in branches:
                bt = _declared_types(b)
                if bt is None:
                    return None
                out.extend(bt)
            return out
    return None


def _matches(value: Any, json_type: str) -> bool:
    if json_type == "null":
        return value is None
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "object":
        return isinstance(value, dict)
    return True  # a type word we do not know is not ours to police


_NO_COERCION = object()


def _coerce_one(value: Any, json_type: str) -> Any:
    """`value` as `json_type`, or _NO_COERCION. Never guesses from the look alone:
    the declared type picks the reading, so "1-5" for a string stays a string."""
    if json_type == "boolean":
        # "true"/"false" only. "1"/"0", "yes" and integers are refused rather than
        # read: none was seen in the logs, and a guess lands on flags such as
        # repo_delete(recursive) and git_reset(hard).
        if isinstance(value, str):
            word = value.strip().lower()
            if word == "true":
                return True
            if word == "false":
                return False
        return _NO_COERCION
    if json_type == "integer":
        if isinstance(value, bool):
            return _NO_COERCION
        if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value.strip()):
            return int(value.strip())
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return _NO_COERCION
    if json_type == "number":
        if isinstance(value, str):
            text = value.strip()
            if _INTEGER_TEXT.fullmatch(text):
                return int(text)
            try:
                f = float(text)
            except ValueError:
                return _NO_COERCION
            return f if math.isfinite(f) else _NO_COERCION
        return _NO_COERCION
    if json_type == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return _NO_COERCION
    if json_type in ("array", "object"):
        if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
            try:
                parsed = json.loads(value)
            except ValueError:
                return _NO_COERCION
            if _matches(parsed, json_type):
                return parsed
        return _NO_COERCION
    return _NO_COERCION


def coerce_args_to_schema(
    schema: Dict[str, Any], args: Dict[str, Any], tool_name: str,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Bring model-supplied arguments to the types the tool's schema declares.

    Returns (args, None), or (args, error) when a value cannot be read as its
    declared type — the error is the registry's TOOL_ARG_ERROR string, and the
    handler must not run. Local models send booleans as "True"/"False"; a
    handler that tests truthiness runs a string "False" as True.

    Only declared properties with a known type are touched; None passes as
    "not given". A mismatched array or object that is not a JSON string of
    that type is left to the handler, since a handler may accept more than
    its schema says. Every coercion is logged at INFO, one line per call.
    """
    params = schema.get("parameters") if isinstance(schema, dict) else None
    props = params.get("properties") if isinstance(params, dict) else None
    if not args or not isinstance(props, dict):
        return args, None

    out = dict(args)
    changed: List[str] = []
    for pname, value in args.items():
        types = _declared_types(props.get(pname))
        if not types or value is None:
            continue
        if any(_matches(value, t) for t in types):
            continue
        for t in types:
            coerced = _coerce_one(value, t)
            if coerced is not _NO_COERCION:
                out[pname] = coerced
                changed.append(f"{pname} {value!r} ({_json_type(value)}) -> {coerced!r} ({t})")
                break
        else:
            if all(t in ("array", "object") for t in types if t != "null"):
                continue
            shown = repr(value)
            if len(shown) > 60:
                shown = shown[:57] + "..."
            expected = " or ".join(types)
            return args, (
                f"⚠️ TOOL_ARG_ERROR ({tool_name}): parameter '{pname}' expects "
                f"{expected}, got {_json_type(value)} {shown}"
            )
    if changed:
        log.info("Tool %s: arguments coerced to the schema: %s", tool_name, "; ".join(changed))
    return out, None


def agent_display_name(ctx: Any) -> str:
    """The agent's name for a person to read, or an honest identifier.

    `getattr(agent, "display_name", "Agent")` turned "we do not know who is
    asking" into a plausible signature, and it reached production twice: the
    shell dialog on 2026-08-14 and the schedule card on 2026-08-16, both
    reading «Agent wants …». An unknown agent is better shown as unknown, and
    the sandbox directory is a real identifier when the object has no name.
    """
    agent = getattr(ctx, "_agent", None)
    name = getattr(agent, "display_name", "") if agent is not None else ""
    if name:
        return name
    # `DpcAgent` carries no `display_name` at all — observed 2026-08-21, when the
    # popup read «agent_001 @ DPC Research» after the default was fixed, and
    # «Agent» before it. The name has always lived in the agent's own config,
    # which is where AgentManager._load_display_name and the service read it.
    # Read the file rather than load_agent_config(agent_id): that helper resolves
    # through get_agent_root, which creates the directory it is asked about.
    root = getattr(ctx, "agent_root", None)
    if root is not None:
        try:
            import json as _json
            cfg = _json.loads((root / "config.json").read_text(encoding="utf-8"))
            if cfg.get("name"):
                return str(cfg["name"])
        except Exception:
            pass
    return getattr(root, "name", "") or "Unknown agent"


def conversation_origin(ctx: Any) -> Tuple[str, str]:
    """(id, readable name) of the chat this run came from, or two empty strings.

    Every gate that asks a person something needs this: with four agents in
    four chats, "X wants to run Y" is unanswerable, and two agents can raise
    the same request in the same second. The title is whatever the caller
    already knew — a group run carries its name — and the rest is named by the
    service, which is the one holding groups and peers. A run with no chat
    behind it (a schedule, a sleep) stays empty rather than inventing one.
    """
    cid = getattr(ctx, "conversation_id", "") or ""
    title = getattr(ctx, "conversation_title", "") or ""
    if cid and not title:
        resolve = getattr(getattr(ctx, "dpc_service", None), "_conversation_display_name", None)
        if resolve is not None:
            try:
                title = resolve(cid) or ""
            except Exception:  # a name is never worth failing a gate over
                title = ""
    return cid, title


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

    # How many check_back wake-ups deep this turn already is. Set by the task
    # executor from the task record, never from a tool argument — a depth the
    # model could pass is a depth the model can reset, and then the cap guards
    # nothing.
    check_back_depth: int = 0

    # ConversationMonitor for knowledge extraction (set by agent_manager)
    conversation_monitor: Optional[Any] = None

    # Tool whitelist (if set, only these tools are allowed)
    tool_whitelist: Optional[set] = None

    # schedule_task is in this turn's whitelist only because a music tool is, not because
    # the firewall grants it (ADR-051 D49, music.with_check_back): schedule_task then takes
    # nothing but a check_back to a music job this agent proposed that is still queued or
    # running. Set by DpcAgent.process from the same computation as the whitelist.
    schedule_task_music_return_only: bool = False

    # Firewall reference for extended sandbox paths (v0.16.0+)
    firewall: Optional[Any] = None

    # Reply routing for scheduled tasks: set by agent_manager when processing
    # a Telegram message so that schedule_task can propagate it into task data.
    reply_telegram_chat_id: Optional[str] = None

    # Where this turn came from (DpcAgent.process's message_source). memory_search,
    # extended paths and schedule_task read it; None is nobody's turn.
    message_source: Optional[str] = None

    # Whose turn this is and which memory layers it may see (agent.turn_class,
    # agent.open_layers; ADR-048). DpcAgent.process sets both on every turn; a bare
    # context with neither and no source is a test or a probe, not a turn.
    turn_class: Optional[str] = None
    open_layers: Optional[frozenset] = None

    # Who asked for this turn (agent.Asker, ADR-053): a separate axis from the room. A
    # flagged tool (ToolEntry.owner_approval_for_others) asks the owner first when the
    # asker is not the owner. DpcAgent.process sets it on every turn; None is nobody's
    # turn and is not the owner (fail closed).
    asker: Optional[Any] = None

    # An internal call that no turn made and nobody asked for (a test, a probe). Only a
    # context with this marker and no asker, no source and no class passes the owner
    # approval gate as the owner's; the absence of the three fields alone is foreign
    # (owner_approval.asker_is_owner, Zcode #154). A real asker or source wins over it.
    # DpcAgent.process never sets it.
    internal_call: bool = False

    # Skill store for execute_skill tool (set by DpcAgent.process())
    skill_store: Optional[Any] = None

    # Which conversation this run belongs to, and what to call it in front of a
    # person. Set by DpcAgent.process(); empty for runs with no chat behind them
    # (a schedule, a sleep). The tier-1 approval prompt is what needs it: with
    # four agents working in four chats, "Johnny wants to run rm -rf" is not an
    # answerable question until it says where the request came from.
    conversation_id: Optional[str] = None
    conversation_title: Optional[str] = None

    # Where the loop keeps this call's output (ADR-045 store, loop.TOOL_OUTPUT_DIR):
    # the turn's directory and the call's name, `r<round>-<position>`. Set by the
    # loop before each call, so a tool can keep more than it returns beside its
    # own result — run_shell does after a timeout. None outside a loop.
    tool_output_turn: Optional[str] = None
    tool_output_name: Optional[str] = None

    # The attachments chat_post_file kept in this turn's chat (ADR-051 D41), in the
    # order they were posted. DpcAgent.process hands them to the agent manager, which
    # writes them on the turn's answer record. One list per turn: the context is.
    posted_attachments: List[Dict[str, Any]] = field(default_factory=list)
    # The paths of those files this turn copied anew (not reused): removed again when the
    # turn fails before its answer is written (chat_files.remove_posted_copies).
    posted_new_copies: List[str] = field(default_factory=list)
    # The sources chat_post_file posted into this turn's group (ADR-051 D50 G): each is a
    # group message of its own the moment it is sent, so it never rides on the answer and
    # is never removed; the list only counts the turn's files against MAX_FILES_PER_TURN.
    posted_group_files: List[str] = field(default_factory=list)
    # The bytes of each of those files, by its slot (`sha256:<hex>`), held to
    # chat_files.MAX_BYTES_PER_TURN (follow-up 14, Zcode #176).
    posted_group_bytes: Dict[str, int] = field(default_factory=dict)

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

        # Mike's call, 2026-11-07: a Discord turn reads nothing outside the sandbox.
        if self.message_source == "discord":
            raise PermissionError(
                f"path '{path}' is outside the agent directory, and a turn from discord "
                f"reads only inside it"
            )

        # Mike's call, 2026-11-11: what was sent into this agent's own chats is readable
        # without a profile entry (its 1:1 chat and the groups it is listed in; never a write).
        from .archive import reads_own_chat_files
        if not require_write and reads_own_chat_files(self, resolved):
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
    """Single tool descriptor: name, schema, handler, metadata.

    default_enabled is the canonical default for this tool in
    privacy_rules.json. Single source of truth — firewall.py reads
    this when seeding new privacy_rules.json files and when merging
    missing tools into existing files at startup. fail-closed (False)
    by default; flip to True explicitly for safe-by-default tools.
    """

    name: str
    schema: Dict[str, Any]  # OpenAI function schema
    handler: Callable  # fn(ctx: ToolContext, **args) -> str
    is_code_tool: bool = False  # Tools that modify code
    timeout_sec: int = 120
    is_core: bool = True  # Core tools loaded by default
    default_enabled: bool = False  # Canonical default for privacy_rules.json (fail-closed)
    # Runs only on the owner's own turns (agent.OWNER_TURN): outside them the tool is not
    # offered and a call is refused before its body runs. See owner_only_refusal.
    owner_only: bool = False
    # ADR-053: every call asked for by someone who is not the owner (ToolContext.asker) goes
    # to the owner as an approval card first. Checked in one place, ToolRegistry.execute
    # (owner_approval.open_gate). A registry test requires every tool to carry it or to be
    # named in its table of tools with no card.
    owner_approval_for_others: bool = False
    # Optional (D-d): approval_check(ctx, args) -> owner_approval.Refuse | Exempt | ToolAsks
    # | Ask, or None to ask with the arguments as "what exactly". Called only on a turn the
    # owner did not ask for.
    approval_check: Optional[Callable] = None
    # The tool's own timeout already holds a 60 s approval wait (run_shell, schedule_task,
    # music_propose_job), so the loop adds none on a foreign turn (D-c).
    budget_holds_approval: bool = False


def is_owner_turn(ctx: Optional["ToolContext"]) -> bool:
    """Is this the owner's own turn, by the class agent.turn_class gave it?

    Fails closed: a context with no class (a bare context, a probe, a run that no turn
    classified) or with any value other than agent.OWNER_TURN is not the owner's.
    """
    from ..agent import OWNER_TURN  # agent imports this module; read at call time
    return getattr(ctx, "turn_class", None) == OWNER_TURN


def owner_only_refusal(entry: "ToolEntry", ctx: Optional["ToolContext"]) -> Optional[str]:
    """The refusal of an owner_only tool on a turn that is not the owner's, else None.

    The one place the owner_only rule is decided: ToolRegistry.execute refuses with it,
    and the offered lists (ToolRegistry.schemas, DpcAgent._get_allowed_tools) leave out
    what it would refuse. A turn whose class cannot be determined is not the owner's
    (fail closed). A tool may check again inside its body, as a second line.
    """
    if not entry.owner_only or is_owner_turn(ctx):
        return None
    cls = getattr(ctx, "turn_class", None) or "unknown"
    return (f"⚠️ Tool '{entry.name}' runs only on the owner's own turns; this turn's class is "
            f"'{cls}' (a peer, a group with another node, Discord, Telegram that is not the "
            f"owner's private chat, or a turn nobody classified). Nothing was run.")


# Core tools that are always available
CORE_TOOL_NAMES = {
    # File operations (sandboxed + extended)
    "read_file", "write_file", "list_dir", "repo_delete",
    "list_extended_sandbox_paths",
    # Search tools (v0.16.0+)
    "search_files", "search_in_file",
    # Memory/identity
    "update_scratchpad", "update_identity",
    "chat_history",
    # Knowledge
    "knowledge_list",
    "memory_search",
    # DPC integration
    "get_dpc_context",
    # Browser tools (safe, read-only)
    "browse_page", "fetch_json", "check_url", "search_web",
    # Messaging tools (agent-to-user communication)
    "send_user_message",
    # Skill router (Read phase of Memento-Skills loop)
    "execute_skill",
    # Self-introspection tools
    "list_my_tools", "list_my_skills",
    # Session archive tools (read-only access to conversation history)
    "read_session_archive", "read_session_detail",
}

# Restricted tools (require explicit enable in config)
RESTRICTED_TOOL_NAMES = {
    "run_shell",           # Shell access
    "git_push",            # Git push
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
        "repo_write_commit", "run_shell"
    })

    def __init__(self, agent_root: Optional[pathlib.Path] = None):
        """
        Initialize the tool registry.

        Args:
            agent_root: Root directory for agent storage (defaults to ~/.dpc/agent/)
        """
        self._entries: Dict[str, ToolEntry] = {}
        # Modules that failed to import, name -> error text. A non-empty dict
        # means this registry is an incomplete picture of the tools that exist:
        # callers that compare it against stored config (firewall seeding and
        # pruning) must not treat "absent from the registry" as "does not exist".
        self.load_failures: Dict[str, str] = {}
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
        modules_loaded = 0
        for _importer, modname, _ispkg in pkgutil.iter_modules([str(tools_path)]):
            if modname.startswith("_") or modname == "registry":
                continue
            try:
                mod = importlib.import_module(f".{modname}", package="dpc_client_core.dpc_agent.tools")
                if hasattr(mod, "get_tools"):
                    modules_loaded += 1
                    for entry in mod.get_tools():
                        self._entries[entry.name] = entry
                        tools_loaded += 1
            except Exception as e:
                # ERROR, not WARNING: a module that does not import takes its
                # tools out of the registry, out of the permissions panel and
                # out of every agent — silently, and the only trace is here.
                self.load_failures[modname] = str(e)
                log.error(f"Failed to load tool module {modname}: {e}", exc_info=True)

        if tools_loaded > 0:
            log.info(f"Loaded {tools_loaded} agent tools from {modules_loaded} modules")
        if self.load_failures:
            log.error(
                "%d tool module(s) failed to load — their tools are missing from "
                "this registry: %s",
                len(self.load_failures), ", ".join(sorted(self.load_failures)),
            )

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
            # Check whitelist. `None` means no firewall answered, so no gate;
            # an EMPTY set means the firewall allowed nothing, which has to deny
            # everything — under truthiness those two read the same, and the
            # empty one silently offered the model every tool it had.
            if self._ctx.tool_whitelist is not None and e.name not in self._ctx.tool_whitelist:
                continue
            # An owner_only tool is not offered on a turn that is not the owner's,
            # whitelist or none (no firewall gives None, which gates nothing).
            if owner_only_refusal(e, self._ctx) is not None:
                continue

            result.append({"type": "function", "function": e.schema})
        return result

    def owner_only_names(self) -> set:
        """The names of the tools marked owner_only."""
        return {e.name for e in self._entries.values() if e.owner_only}

    def owner_approval_names(self) -> set:
        """The names of the tools marked owner_approval_for_others (ADR-053)."""
        return {e.name for e in self._entries.values() if e.owner_approval_for_others}

    def approval_wait_for(self, name: str, ctx: Optional["ToolContext"] = None) -> int:
        """Seconds the loop adds to this call's budget for the owner's card (ADR-053 D-c):
        60 for a flagged tool on a turn the owner did not ask for, unless its own timeout
        already holds the wait; else 0."""
        from ..owner_approval import approval_wait_for
        return approval_wait_for(self._entries.get(name), ctx if ctx is not None else self._ctx)

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
                 loops (sleep, scheduled tasks) run concurrently.

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

        # Check whitelist — `None` is "no gate", empty is "nothing allowed".
        if _ctx.tool_whitelist is not None and name not in _ctx.tool_whitelist:
            return f"⚠️ Tool '{name}' is not in the allowed tools list"

        # owner_only: refused before the body runs on a turn that is not the owner's,
        # an unclassified one included (fail closed).
        refusal = owner_only_refusal(entry, _ctx)
        if refusal is not None:
            log.info("Tool %s refused: owner_only, turn class %r", name,
                     getattr(_ctx, "turn_class", None))
            return refusal

        args = _resolve_arg_aliases(entry.handler, args, name)
        args, arg_error = coerce_args_to_schema(entry.schema, args, name)
        if arg_error is not None:
            return arg_error

        # ADR-053: a flagged tool asked for by someone who is not the owner goes to the owner
        # first. After the owner_only refusal and the argument check, so the card shows the
        # arguments the handler will get; the gate's mark lives until the handler returns.
        from ..owner_approval import open_gate
        gate = open_gate(entry, _ctx, args)
        if gate.refusal is not None:
            return gate.refusal
        try:
            return self._run_handler(entry, name, _ctx, args)
        finally:
            gate.close()

    def _run_handler(self, entry: "ToolEntry", name: str, _ctx: "ToolContext",
                     args: Dict[str, Any]) -> str:
        """The handler's call, sync or async, with the registry's error words."""
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
                    # A provider client opened on this loop is closed here, on it:
                    # its pooled sockets die with the loop and nobody can close them later.
                    try:
                        from ...providers.loop_clients import close_this_loop
                        loop.run_until_complete(close_this_loop())
                    except Exception:
                        pass
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
        """Override the handler for a registered tool (used for closure injection).

        `dataclasses.replace`, not a field-by-field copy: a flag the copy forgot was
        silently dropped (ADR-053 D-h)."""
        entry = self._entries.get(name)
        if entry:
            self._entries[name] = replace(entry, handler=handler)


def mark_owner_approval(entries: List["ToolEntry"],
                        checks: Dict[str, Optional[Callable]],
                        budget_holds_approval: Tuple[str, ...] = ()) -> List["ToolEntry"]:
    """Set `owner_approval_for_others` on the named tools of a module (ADR-053 step 2), with
    each one's `approval_check` (None: ask with the arguments as "what exactly").

    A name the module does not export is an error, so a renamed tool cannot fall out of
    the flag silently.
    """
    names = {e.name for e in entries}
    missing = sorted(set(checks) - names)
    if missing:
        raise ValueError(f"mark_owner_approval: no such tool in this module: {missing}")
    return [replace(e, owner_approval_for_others=True, approval_check=checks[e.name],
                    budget_holds_approval=e.name in budget_holds_approval)
            if e.name in checks else e
            for e in entries]
