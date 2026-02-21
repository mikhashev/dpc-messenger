"""
DPC Agent — Core Tools.

Provides essential file and memory operations for the embedded agent:
- File reading/listing (sandboxed to ~/.dpc/agent/)
- Memory management (scratchpad, identity)
- Knowledge base operations
- DPC context integration

All file operations are restricted to the agent's sandbox directory.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File Operations (sandboxed)
# ---------------------------------------------------------------------------

def repo_read(ctx: ToolContext, path: str) -> str:
    """
    Read a file from the agent sandbox.

    Args:
        ctx: Tool context
        path: File path relative to agent root

    Returns:
        File contents
    """
    try:
        file_path = ctx.repo_path(path)

        if not file_path.exists():
            return f"⚠️ File not found: {path}"

        if not file_path.is_file():
            return f"⚠️ Not a file: {path}"

        content = file_path.read_text(encoding="utf-8", errors="replace")

        # Truncate large files
        if len(content) > 50000:
            content = content[:50000] + f"\n\n... (truncated, {len(content)} total chars)"

        return content

    except PermissionError as e:
        return f"⚠️ Sandbox violation: {e}"
    except Exception as e:
        return f"⚠️ Error reading file: {e}"


def repo_list(ctx: ToolContext, path: str = ".", recursive: bool = False) -> str:
    """
    List files in the agent sandbox.

    Args:
        ctx: Tool context
        path: Directory path relative to agent root
        recursive: List recursively

    Returns:
        List of files/directories
    """
    try:
        dir_path = ctx.repo_path(path)

        if not dir_path.exists():
            return f"⚠️ Directory not found: {path}"

        if not dir_path.is_dir():
            return f"⚠️ Not a directory: {path}"

        items = []

        if recursive:
            for item in dir_path.rglob("*"):
                rel_path = item.relative_to(dir_path)
                if item.is_dir():
                    items.append(f"[DIR]  {rel_path}/")
                else:
                    size = item.stat().st_size
                    items.append(f"[FILE] {rel_path} ({size} bytes)")
        else:
            for item in sorted(dir_path.iterdir()):
                name = item.name
                if item.is_dir():
                    items.append(f"[DIR]  {name}/")
                else:
                    size = item.stat().st_size
                    items.append(f"[FILE] {name} ({size} bytes)")

        if not items:
            return f"Empty directory: {path}"

        return f"Contents of {path}:\n" + "\n".join(items)

    except PermissionError as e:
        return f"⚠️ Sandbox violation: {e}"
    except Exception as e:
        return f"⚠️ Error listing directory: {e}"


def drive_read(ctx: ToolContext, path: str) -> str:
    """Alias for repo_read (compatibility with Ouroboros tools)."""
    return repo_read(ctx, path)


def drive_list(ctx: ToolContext, path: str = ".", recursive: bool = False) -> str:
    """Alias for repo_list (compatibility with Ouroboros tools)."""
    return repo_list(ctx, path, recursive)


def repo_write(ctx: ToolContext, path: str, content: str) -> str:
    """
    Write a file to the agent sandbox.

    Args:
        ctx: Tool context
        path: File path relative to agent root
        content: Content to write

    Returns:
        Result message
    """
    try:
        file_path = ctx.repo_path(path)

        # Create parent directories if needed
        file_path.parent.mkdir(parents=True, exist_ok=True)

        file_path.write_text(content, encoding="utf-8")

        return f"✓ Wrote {len(content)} chars to {path}"

    except PermissionError as e:
        return f"⚠️ Sandbox violation: {e}"
    except Exception as e:
        return f"⚠️ Error writing file: {e}"


def drive_write(ctx: ToolContext, path: str, content: str) -> str:
    """Alias for repo_write (compatibility with Ouroboros tools)."""
    return repo_write(ctx, path, content)


# ---------------------------------------------------------------------------
# Memory Tools
# ---------------------------------------------------------------------------

def update_scratchpad(ctx: ToolContext, content: str, mode: str = "append") -> str:
    """
    Update the agent's scratchpad (working memory).

    Args:
        ctx: Tool context
        content: Content to add/update
        mode: Update mode - 'append', 'prepend', 'replace', 'section'

    Returns:
        Result message
    """
    try:
        scratchpad_path = ctx.memory_path("scratchpad.md")

        if mode == "replace":
            new_content = content
        elif mode == "append":
            existing = ""
            if scratchpad_path.exists():
                existing = scratchpad_path.read_text(encoding="utf-8")
            new_content = existing + "\n\n" + content
        elif mode == "prepend":
            existing = ""
            if scratchpad_path.exists():
                existing = scratchpad_path.read_text(encoding="utf-8")
            new_content = content + "\n\n" + existing
        else:
            return f"⚠️ Unknown mode: {mode}. Use 'append', 'prepend', or 'replace'"

        scratchpad_path.parent.mkdir(parents=True, exist_ok=True)
        scratchpad_path.write_text(new_content.strip(), encoding="utf-8")

        # Log update
        journal_path = ctx.memory_path("scratchpad_journal.jsonl")
        journal_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "content_length": len(content),
        }
        with open(journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(journal_entry) + "\n")

        return f"✓ Updated scratchpad ({mode} mode, {len(new_content)} total chars)"

    except Exception as e:
        return f"⚠️ Error updating scratchpad: {e}"


def update_identity(ctx: ToolContext, section: str, content: str) -> str:
    """
    Update a section of the agent's identity file.

    Args:
        ctx: Tool context
        section: Section name (e.g., 'values', 'goals', 'beliefs')
        content: Section content

    Returns:
        Result message
    """
    try:
        identity_path = ctx.memory_path("identity.md")

        if not identity_path.exists():
            # Create initial identity
            initial_content = f"""# Agent Identity

This file tracks the agent's self-understanding and evolving identity.

## Last Updated
{datetime.now(timezone.utc).isoformat()}

## {section.title()}
{content}
"""
            identity_path.parent.mkdir(parents=True, exist_ok=True)
            identity_path.write_text(initial_content, encoding="utf-8")
            return f"✓ Created identity with {section} section"

        existing = identity_path.read_text(encoding="utf-8")

        # Find and update section
        section_header = f"## {section.title()}"
        lines = existing.split("\n")
        section_start = None
        section_end = None

        for i, line in enumerate(lines):
            if line.strip() == section_header:
                section_start = i
            elif section_start is not None and line.startswith("## ") and i > section_start:
                section_end = i
                break

        if section_start is not None:
            # Update existing section
            if section_end is None:
                section_end = len(lines)
            new_lines = (
                lines[:section_start]
                + [section_header, content]
                + lines[section_end:]
            )
        else:
            # Add new section
            new_lines = lines + ["", section_header, content]

        # Update timestamp
        for i, line in enumerate(new_lines):
            if line.startswith("## Last Updated"):
                new_lines[i] = f"## Last Updated\n{datetime.now(timezone.utc).isoformat()}"
                break

        new_content = "\n".join(new_lines)
        identity_path.write_text(new_content, encoding="utf-8")

        return f"✓ Updated identity section '{section}'"

    except Exception as e:
        return f"⚠️ Error updating identity: {e}"


def chat_history(ctx: ToolContext, limit: int = 10) -> str:
    """
    Read recent chat history from the logs.

    Args:
        ctx: Tool context
        limit: Maximum number of entries to return

    Returns:
        Recent chat history
    """
    try:
        events_path = ctx.logs_path("events.jsonl")

        if not events_path.exists():
            return "No chat history available"

        entries = []
        with open(events_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        if not entries:
            return "No chat history available"

        # Get most recent entries
        recent = entries[-limit:]

        output_lines = [f"Recent chat history ({len(recent)} entries):\n"]
        for entry in recent:
            ts = entry.get("ts", "unknown time")
            event_type = entry.get("type", "unknown")
            text = entry.get("text", "")[:100]
            output_lines.append(f"[{ts}] {event_type}: {text}...")

        return "\n".join(output_lines)

    except Exception as e:
        return f"⚠️ Error reading chat history: {e}"


# ---------------------------------------------------------------------------
# Knowledge Tools
# ---------------------------------------------------------------------------

def knowledge_read(ctx: ToolContext, topic: str) -> str:
    """
    Read a knowledge base topic.

    Args:
        ctx: Tool context
        topic: Topic name

    Returns:
        Topic content
    """
    try:
        topic_path = ctx.knowledge_path(topic)

        if not topic_path.exists():
            return f"⚠️ Topic not found: {topic}"

        return topic_path.read_text(encoding="utf-8")

    except Exception as e:
        return f"⚠️ Error reading knowledge: {e}"


def knowledge_write(ctx: ToolContext, topic: str, content: str) -> str:
    """
    Write or update a knowledge base topic.

    Args:
        ctx: Tool context
        topic: Topic name
        content: Topic content (markdown)

    Returns:
        Result message
    """
    try:
        topic_path = ctx.knowledge_path(topic)
        topic_path.parent.mkdir(parents=True, exist_ok=True)

        # Add timestamp
        full_content = f"""# {topic.title()}

Last updated: {datetime.now(timezone.utc).isoformat()}

{content}
"""
        topic_path.write_text(full_content, encoding="utf-8")

        # Update index
        _update_knowledge_index(ctx, topic)

        return f"✓ Wrote knowledge topic '{topic}' ({len(content)} chars)"

    except Exception as e:
        return f"⚠️ Error writing knowledge: {e}"


def knowledge_list(ctx: ToolContext) -> str:
    """
    List all knowledge base topics.

    Args:
        ctx: Tool context

    Returns:
        List of topics
    """
    try:
        knowledge_dir = ctx.agent_root / "knowledge"

        if not knowledge_dir.exists():
            return "Knowledge base is empty"

        topics = list(knowledge_dir.glob("*.md"))

        if not topics:
            return "Knowledge base is empty"

        # Filter out index file
        topics = [t for t in topics if t.name != "_index.md"]

        output_lines = [f"Knowledge base topics ({len(topics)}):\n"]
        for topic in sorted(topics):
            name = topic.stem
            stat = topic.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            output_lines.append(f"  • {name} (modified: {modified.strftime('%Y-%m-%d %H:%M')})")

        return "\n".join(output_lines)

    except Exception as e:
        return f"⚠️ Error listing knowledge: {e}"


def _update_knowledge_index(ctx: ToolContext, topic: str) -> None:
    """Update the knowledge base index file."""
    try:
        index_path = ctx.knowledge_path("_index")
        knowledge_dir = ctx.agent_root / "knowledge"

        topics = []
        if knowledge_dir.exists():
            for t in knowledge_dir.glob("*.md"):
                if t.name != "_index.md":
                    topics.append(t.stem)

        index_content = f"""# Knowledge Base Index

Last updated: {datetime.now(timezone.utc).isoformat()}

## Topics

"""
        for t in sorted(topics):
            index_content += f"- [{t}]({t}.md)\n"

        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(index_content, encoding="utf-8")

    except Exception as e:
        log.warning(f"Failed to update knowledge index: {e}")


# ---------------------------------------------------------------------------
# DPC Integration Tools
# ---------------------------------------------------------------------------

def get_dpc_context(ctx: ToolContext, context_type: str = "personal") -> str:
    """
    Read DPC personal or device context.

    This tool provides access to the user's personal.json or device_context.json
    files, enabling context-aware assistance.

    Args:
        ctx: Tool context
        context_type: Type of context - 'personal' or 'device'

    Returns:
        Context content
    """
    try:
        dpc_dir = Path.home() / ".dpc"

        if context_type == "personal":
            path = dpc_dir / "personal.json"
        elif context_type == "device":
            path = dpc_dir / "device_context.json"
        else:
            return f"⚠️ Unknown context type: {context_type}. Use 'personal' or 'device'"

        if not path.exists():
            return f"⚠️ Context file not found: {path}"

        content = path.read_text(encoding="utf-8")

        # Parse and pretty-print JSON
        try:
            data = json.loads(content)
            formatted = json.dumps(data, indent=2, ensure_ascii=False)

            # Truncate if too large
            if len(formatted) > 20000:
                formatted = formatted[:20000] + "\n\n... (truncated)"

            return f"DPC {context_type} context:\n\n{formatted}"
        except json.JSONDecodeError:
            return f"⚠️ Invalid JSON in {path}"

    except Exception as e:
        return f"⚠️ Error reading DPC context: {e}"


# ---------------------------------------------------------------------------
# Tool Registry Export
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    """Export core tools for registry."""
    return [
        # File operations
        ToolEntry(
            name="repo_read",
            schema={
                "name": "repo_read",
                "description": "Read a file from the agent sandbox directory",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to agent root"
                        }
                    },
                    "required": ["path"]
                }
            },
            handler=repo_read,
            timeout_sec=30,
        ),

        ToolEntry(
            name="repo_list",
            schema={
                "name": "repo_list",
                "description": "List files in a directory within the agent sandbox",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory path (default: root)",
                            "default": "."
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "List recursively",
                            "default": False
                        }
                    },
                    "required": []
                }
            },
            handler=repo_list,
            timeout_sec=30,
        ),

        ToolEntry(
            name="drive_read",
            schema={
                "name": "drive_read",
                "description": "Read a file from the agent sandbox (alias for repo_read)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to agent root"
                        }
                    },
                    "required": ["path"]
                }
            },
            handler=drive_read,
            timeout_sec=30,
        ),

        ToolEntry(
            name="drive_list",
            schema={
                "name": "drive_list",
                "description": "List files in the agent sandbox (alias for repo_list)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory path (default: root)",
                            "default": "."
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "List recursively",
                            "default": False
                        }
                    },
                    "required": []
                }
            },
            handler=drive_list,
            timeout_sec=30,
        ),

        ToolEntry(
            name="repo_write",
            schema={
                "name": "repo_write",
                "description": "Write a file to the agent sandbox directory",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to agent root"
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write"
                        }
                    },
                    "required": ["path", "content"]
                }
            },
            handler=repo_write,
            timeout_sec=30,
            is_code_tool=True,
        ),

        ToolEntry(
            name="drive_write",
            schema={
                "name": "drive_write",
                "description": "Write a file to the agent sandbox (alias for repo_write)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to agent root"
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write"
                        }
                    },
                    "required": ["path", "content"]
                }
            },
            handler=drive_write,
            timeout_sec=30,
            is_code_tool=True,
        ),

        # Memory tools
        ToolEntry(
            name="update_scratchpad",
            schema={
                "name": "update_scratchpad",
                "description": "Update the agent's working memory (scratchpad)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Content to add to scratchpad"
                        },
                        "mode": {
                            "type": "string",
                            "description": "Update mode",
                            "enum": ["append", "prepend", "replace"],
                            "default": "append"
                        }
                    },
                    "required": ["content"]
                }
            },
            handler=update_scratchpad,
            timeout_sec=10,
        ),

        ToolEntry(
            name="update_identity",
            schema={
                "name": "update_identity",
                "description": "Update a section of the agent's identity (self-understanding)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "description": "Section name (e.g., 'values', 'goals', 'beliefs')"
                        },
                        "content": {
                            "type": "string",
                            "description": "Section content"
                        }
                    },
                    "required": ["section", "content"]
                }
            },
            handler=update_identity,
            timeout_sec=10,
        ),

        ToolEntry(
            name="chat_history",
            schema={
                "name": "chat_history",
                "description": "Read recent chat history from agent logs",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of entries",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100
                        }
                    },
                    "required": []
                }
            },
            handler=chat_history,
            timeout_sec=10,
        ),

        # Knowledge tools
        ToolEntry(
            name="knowledge_read",
            schema={
                "name": "knowledge_read",
                "description": "Read a knowledge base topic",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "Topic name"
                        }
                    },
                    "required": ["topic"]
                }
            },
            handler=knowledge_read,
            timeout_sec=10,
        ),

        ToolEntry(
            name="knowledge_write",
            schema={
                "name": "knowledge_write",
                "description": "Write or update a knowledge base topic",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "Topic name"
                        },
                        "content": {
                            "type": "string",
                            "description": "Topic content (markdown)"
                        }
                    },
                    "required": ["topic", "content"]
                }
            },
            handler=knowledge_write,
            timeout_sec=10,
        ),

        ToolEntry(
            name="knowledge_list",
            schema={
                "name": "knowledge_list",
                "description": "List all knowledge base topics",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=knowledge_list,
            timeout_sec=10,
        ),

        # DPC integration
        ToolEntry(
            name="get_dpc_context",
            schema={
                "name": "get_dpc_context",
                "description": "Read DPC personal or device context for context-aware assistance",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "context_type": {
                            "type": "string",
                            "description": "Type of context to read",
                            "enum": ["personal", "device"],
                            "default": "personal"
                        }
                    },
                    "required": []
                }
            },
            handler=get_dpc_context,
            timeout_sec=10,
        ),
    ]
