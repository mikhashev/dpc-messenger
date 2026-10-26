"""
DPC Agent — Skill execution and inter-agent skill sharing tools.

Provides the `execute_skill` tool that loads a skill strategy by name
and returns its markdown instructions for the LLM to follow.

This is the Read phase of the Memento-Skills Read-Write Reflective Learning loop:
- Read:  LLM sees available skill names + descriptions in system prompt,
         calls execute_skill(name) to load the full strategy, then follows it
- Write: Phase 3 (skill_reflection.py) — agent updates SKILL.md after each task
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..tools.registry import ToolContext, ToolEntry


def _execute_skill(ctx: "ToolContext", skill_name: str, request: str = "") -> str:
    """
    Load a skill strategy and return its instructions.

    The LLM reads the returned strategy and follows it using existing tools.
    """
    if ctx.skill_store is None:
        return "⚠️ Skill store not available on this agent"

    body = ctx.skill_store.load_skill_body(skill_name)
    if body is None:
        available = ctx.skill_store.list_skill_names()
        return (
            f"⚠️ Skill '{skill_name}' not found. "
            f"Available skills: {', '.join(available) if available else 'none installed'}"
        )

    header = f"# Skill Strategy: {skill_name}\n\n"
    if request:
        header += f"**Your request:** {request}\n\n"
    header += "Follow the **Strategy** section below using your available tools:\n\n"
    return header + body


def _list_local_agents(ctx: "ToolContext") -> str:
    """List all registered agents on this device."""
    try:
        from ..utils import AgentRegistry
        registry = AgentRegistry()
        agents = registry.list_agents()
        if not agents:
            return "No agents registered."
        # Filter out self by comparing agent_root paths
        own_id = ctx.agent_root.name
        result = []
        for a in agents:
            marker = " (self)" if a.get("agent_id") == own_id else ""
            result.append(
                f"- {a.get('agent_id', '?')}{marker}: "
                f"{a.get('name', 'unnamed')} — {a.get('description', 'no description')}"
            )
        return "\n".join(result)
    except Exception as e:
        return f"⚠️ Failed to list agents: {e}"


def _list_agent_skills(ctx: "ToolContext", agent_id: str, tags: Optional[str] = None) -> str:
    """List shareable skills of another local agent."""
    try:
        from ..utils import get_agent_root, AgentRegistry
        from ..skill_store import SkillStore

        # Verify agent exists
        registry = AgentRegistry()
        if not registry.get_agent(agent_id):
            return f"⚠️ Agent '{agent_id}' not found in registry"

        tag_list: Optional[List[str]] = None
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]

        store = SkillStore(get_agent_root(agent_id))
        skills = store.list_shareable_skills(tags=tag_list)
        if not skills:
            return f"Agent '{agent_id}' has no shareable skills" + (
                f" matching tags [{tags}]" if tags else ""
            )
        lines = [f"Shareable skills from {agent_id}:"]
        for s in skills:
            tag_str = f" [{', '.join(s['tags'])}]" if s.get("tags") else ""
            lines.append(f"- {s['name']}v{s['version']}{tag_str}: {s['description'][:120]}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Failed to list agent skills: {e}"


def _list_my_tools(ctx: "ToolContext") -> str:
    """List all tools currently available to this agent (respects firewall whitelist)."""
    agent = getattr(ctx, "_agent", None)
    if agent is None or not hasattr(agent, "tools"):
        return "⚠️ Tool registry not accessible"

    own_id = ctx.agent_root.name
    # include_restricted=True so whitelisted git/shell tools appear;
    # the whitelist check inside schemas() still filters to only enabled tools.
    schemas = agent.tools.schemas(include_restricted=True)
    if not schemas:
        return f"Agent {own_id}: no tools currently available."

    lines = [f"Agent {own_id} — available tools ({len(schemas)} total):"]
    for s in schemas:
        fn = s.get("function", {})
        name = fn.get("name", "?")
        desc = fn.get("description", "No description")
        if len(desc) > 100:
            desc = desc[:97] + "..."
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def _list_my_skills(ctx: "ToolContext") -> str:
    """List all skills installed in this agent's skill store."""
    own_id = ctx.agent_root.name
    if ctx.skill_store is None:
        return f"⚠️ Skill store not available on agent {own_id}"

    skills = ctx.skill_store.list_skills()
    if not skills:
        return (
            f"Agent {own_id}: no skills installed. "
            "Skills can be written via knowledge tools or imported from other agents."
        )

    lines = [f"Agent {own_id} — installed skills ({len(skills)} total):"]
    for s in skills:
        name = s.get("name", "?")
        desc = s.get("description", "No description")
        if len(desc) > 120:
            desc = desc[:117] + "..."
        lines.append(f"- {name}: {desc}")
    lines.append("\nUse execute_skill(skill_name) to load a skill's full strategy.")
    return "\n".join(lines)


def _seconds_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
    """Seconds from one registry ISO time to another (now when `end` is None)."""
    from datetime import datetime, timezone

    try:
        began = datetime.fromisoformat(start) if start else None
        ended = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
    except ValueError:
        return None
    return None if began is None else max(0.0, (ended - began).total_seconds())


def _process_line(row: Dict[str, Any], ceiling_mb: int) -> str:
    """One registry entry as `list_my_processes` prints it."""
    took = _seconds_between(row.get("started_at"), row.get("ended_at"))
    took_text = "?" if took is None else f"{took:.1f} s"
    if row.get("ended_at") is None:
        state = f"RUNNING for {took_text}"
    else:
        reason = row.get("exit_reason") or "?"
        if row.get("stop_kind"):
            reason += f"/{row['stop_kind']}"
        state = f"ended {reason} (exit_code {row.get('exit_code')}) after {took_text}"
    who = row.get("launcher") or "?"
    if row.get("launcher_kind") == "subsystem":
        who += " subsystem, owned by the backend (your call woke it; kill_my_process refuses it)"
    used = []
    if row.get("ended_at") is None:
        commit = row.get("commit_mb")
        ceiling = f" (ceiling {ceiling_mb} MB)" if ceiling_mb else " (ceiling off)"
        used.append(f"commit now {'unmeasured' if commit is None else f'{commit} MB'}{ceiling}")
    for key, label, unit in (("peak_rss_mb", "peak RSS", "MB"), ("peak_commit_mb", "peak commit", "MB"),
                             ("peak_vram_mib", "peak VRAM", "MiB"), ("cpu_seconds", "CPU", "s"),
                             ("io_read_bytes", "read", "B"), ("io_write_bytes", "written", "B")):
        if row.get(key) is not None:
            used.append(f"{label} {row[key]} {unit}")
    if row.get("last_output_at"):
        used.append(f"last output {row['last_output_at']}")
    return (f"- pid {row.get('pid')} [{who}] {state}: {row.get('command') or ''!r}\n"
            f"  chat {row.get('conversation_id') or '-'}, tool {row.get('tool') or '-'} round "
            f"{row.get('round')}, started {row.get('started_at')}; {', '.join(used) or 'no resources read'}")


def _list_my_processes(ctx: "ToolContext") -> str:
    """This agent's own entries in the process registry, live first (ADR-046 point 8)."""
    from ...process_registry import get_registry
    from .process import _CEILING_KEY, _MEMORY_CEILING_MB, _read_memory_ceiling

    own_id = ctx.agent_root.name
    ceiling_mb = _MEMORY_CEILING_MB if _MEMORY_CEILING_MB is not None else _read_memory_ceiling()
    rows = [r for r in get_registry().list_processes() if r.get("agent_id") == own_id]
    live = [r for r in rows if r.get("ended_at") is None]
    # Newest end first, compared as times (seconds since the end), never as ISO strings.
    ended = sorted((r for r in rows if r.get("ended_at") is not None),
                   key=lambda r: _seconds_between(r.get("ended_at"), None) or 0.0)
    ceiling = (f"{ceiling_mb} MB per command tree ({_CEILING_KEY}); a tree over it is killed, memory_ceiling"
               if ceiling_mb else f"off ({_CEILING_KEY} = 0)")
    lines = [f"Agent {own_id} — your processes: {len(live)} running, {len(ended)} ended recently. "
             f"Memory ceiling: {ceiling}."]
    if not rows:
        lines.append("No process started by your tools is in the registry.")
    lines.extend(_process_line(r, ceiling_mb) for r in live + ended)
    return "\n".join(lines)


def _kill_my_process(ctx: "ToolContext", pid: Any) -> str:
    """End one of this agent's own running processes; the registry decides whose it is."""
    from ...process_registry import get_registry

    own_id = ctx.agent_root.name
    if isinstance(pid, str) and pid.strip().isdigit():
        pid = int(pid.strip())
    entry, said = get_registry().cancel_by_agent(pid, own_id)
    if entry is None:
        return f"⚠️ Refused: {said}. kill_my_process ends only a running process your own tools started (see list_my_processes)."
    return (f"pid {entry.pid} ({entry.launcher}: {entry.command!r}) ended {entry.exit_reason}, "
            f"exit_code {entry.exit_code}: {said}")


def _import_skill_from_agent(ctx: "ToolContext", agent_id: str, skill_name: str) -> str:
    """Copy a shareable skill from another local agent into this agent's skill store."""
    # Firewall check (per-agent profile if available)
    firewall = getattr(ctx, "firewall", None)
    _profile = getattr(getattr(ctx, "_agent", None), "_firewall_profile", None)
    if firewall and not firewall.get_agent_skill_permission("accept_peer_skills", profile_name=_profile):
        return "⚠️ Firewall blocks skill import: 'accept_peer_skills' is disabled"

    if ctx.skill_store is None:
        return "⚠️ Skill store not available"

    # Don't import from self
    own_id = ctx.agent_root.name
    if agent_id == own_id:
        return "⚠️ Cannot import a skill from yourself"

    try:
        from ..utils import get_agent_root, AgentRegistry
        from ..skill_store import SkillStore

        # Verify agent exists
        registry = AgentRegistry()
        if not registry.get_agent(agent_id):
            return f"⚠️ Agent '{agent_id}' not found in registry"

        source_store = SkillStore(get_agent_root(agent_id))

        # Check the source skill is shareable
        manifest = source_store.load_manifest(skill_name)
        if manifest is None:
            return f"⚠️ Skill '{skill_name}' not found on agent '{agent_id}'"
        if not (manifest.sharing and manifest.sharing.shareable):
            return f"⚠️ Skill '{skill_name}' is not marked as shareable by agent '{agent_id}'"

        # Don't overwrite locally-authored skills
        existing = ctx.skill_store.load_manifest(skill_name)
        if existing and existing.provenance:
            if existing.provenance.source not in ("peer", "local_agent"):
                return (
                    f"⚠️ Skill '{skill_name}' already exists locally "
                    f"(source: {existing.provenance.source}). "
                    "Delete it first if you want to replace it."
                )

        # Load content and patch provenance
        content = source_store.load_skill_content(skill_name)
        if content is None:
            return f"⚠️ Could not read skill '{skill_name}' from agent '{agent_id}'"

        # Patch provenance fields in YAML frontmatter (simple string replace)
        from ..utils import utc_now_iso
        patched = content
        patched = _patch_frontmatter_field(patched, "source", "local_agent")
        patched = _patch_frontmatter_field(patched, "origin_peer", agent_id)
        patched = _patch_frontmatter_field(patched, "created_at", utc_now_iso())
        # Received skills not shareable by default
        patched = _patch_sharing_field(patched, "shareable", "false")

        ctx.skill_store.save_skill(skill_name, patched)
        return f"✓ Imported skill '{skill_name}' from agent '{agent_id}' (source: local_agent)"

    except Exception as e:
        return f"⚠️ Failed to import skill: {e}"


def _patch_frontmatter_field(content: str, key: str, value: str) -> str:
    """Replace a key: value line inside YAML frontmatter (between --- markers)."""
    import re
    # Find frontmatter bounds
    if not content.lstrip().startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    fm = content[:end + 4]
    body = content[end + 4:]
    # Replace existing key or append to frontmatter
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*:).*$", re.MULTILINE)
    if pattern.search(fm):
        fm = pattern.sub(rf"\1 {value}", fm)
    return fm + body


def _patch_sharing_field(content: str, key: str, value: str) -> str:
    """Patch a field under the 'sharing:' block in YAML frontmatter."""
    import re
    if not content.lstrip().startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    fm = content[:end + 4]
    body = content[end + 4:]
    pattern = re.compile(rf"^(\s+{re.escape(key)}\s*:).*$", re.MULTILINE)
    if pattern.search(fm):
        fm = pattern.sub(rf"\1 {value}", fm)
    return fm + body


def get_tools() -> "List[ToolEntry]":
    from ..tools.registry import ToolEntry

    return [
        ToolEntry(
            name="list_my_tools",
            schema={
                "name": "list_my_tools",
                "description": (
                    "List all tools currently available to you (respects your firewall permissions). "
                    "Use this to discover what you can do, plan a task, or explain your capabilities."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_list_my_tools,
            is_core=True,
            timeout_sec=5,
            default_enabled=True,
        ),
        ToolEntry(
            name="list_my_skills",
            schema={
                "name": "list_my_skills",
                "description": (
                    "List all skills installed in your own skill store. "
                    "Shows skill names and descriptions. "
                    "Use execute_skill(skill_name) to load a skill's full strategy."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_list_my_skills,
            is_core=True,
            timeout_sec=5,
            default_enabled=True,
        ),
        ToolEntry(
            name="list_my_processes",
            schema={
                "name": "list_my_processes",
                "description": (
                    "List the processes your own tools started, running ones first: pid, command, "
                    "chat, when it started and for how long, what it used, how it ended, and the "
                    "memory ceiling beside your live usage. Shows only your own processes."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_list_my_processes,
            is_core=True,
            timeout_sec=10,
            default_enabled=True,
        ),
        ToolEntry(
            name="kill_my_process",
            schema={
                "name": "kill_my_process",
                "description": (
                    "End one of your own running processes and everything it started. Only a pid "
                    "that list_my_processes shows as running and started by your tools is killed; "
                    "any other pid is refused with the reason."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pid": {"type": "integer", "description": "The pid, as list_my_processes shows it"},
                    },
                    "required": ["pid"],
                },
            },
            handler=_kill_my_process,
            is_core=False,  # opt-in: it ends a process
            timeout_sec=20,
            default_enabled=False,
        ),
        ToolEntry(
            name="list_local_agents",
            schema={
                "name": "list_local_agents",
                "description": (
                    "List all DPC agents registered on this device. "
                    "Use to discover other local agents before sharing or importing skills."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_list_local_agents,
            is_core=True,
            timeout_sec=5,
            default_enabled=True,
        ),
        ToolEntry(
            name="list_agent_skills",
            schema={
                "name": "list_agent_skills",
                "description": (
                    "List the shareable skills of another local agent. "
                    "Use before importing to see what skills are available."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "Agent ID to list skills for (e.g. 'agent_001')",
                        },
                        "tags": {
                            "type": "string",
                            "description": "Optional comma-separated tag filter (e.g. 'code,analysis')",
                        },
                    },
                    "required": ["agent_id"],
                },
            },
            handler=_list_agent_skills,
            is_core=True,
            timeout_sec=5,
            default_enabled=True,
        ),
        ToolEntry(
            name="import_skill_from_agent",
            schema={
                "name": "import_skill_from_agent",
                "description": (
                    "Copy a shareable skill from another local agent into your own skill store. "
                    "Requires accept_peer_skills to be enabled in firewall rules. "
                    "The source skill must be marked shareable. "
                    "Will not overwrite locally-authored skills."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "Agent ID to import from (e.g. 'agent_001')",
                        },
                        "skill_name": {
                            "type": "string",
                            "description": "Name of the skill to import (e.g. 'code-analysis')",
                        },
                    },
                    "required": ["agent_id", "skill_name"],
                },
            },
            handler=_import_skill_from_agent,
            is_core=False,  # Restricted by default — needs accept_peer_skills
            timeout_sec=10,
            default_enabled=False,
        ),
        ToolEntry(
            name="execute_skill",
            schema={
                "name": "execute_skill",
                "description": (
                    "Load a skill strategy by name and return its step-by-step instructions. "
                    "Use this at the start of a complex task to get the recommended approach. "
                    "Available skills are listed in the 'Available Skills' section of your context. "
                    "After reading the skill, follow its Strategy section using your other tools."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "Name of the skill to load (kebab-case, e.g. 'code-analysis')",
                        },
                        "request": {
                            "type": "string",
                            "description": "Optional: describe your specific task so the skill strategy can be scoped",
                        },
                    },
                    "required": ["skill_name"],
                },
            },
            handler=_execute_skill,
            is_core=True,
            timeout_sec=5,
            default_enabled=True,
        ),
    ]
