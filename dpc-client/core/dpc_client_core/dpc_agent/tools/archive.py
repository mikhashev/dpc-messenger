"""
DPC Agent — Session Archive Tools.

Provides read-only access to session archives for self-analysis.
Archives are stored outside the agent sandbox in ~/.dpc/conversations/{id}/archive/.
This tool bridges that gap, giving the agent structured access to its own history.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)


def _get_archive_dir(ctx: ToolContext) -> Path:
    """Get the archive directory for the agent's conversation."""
    # conversation_monitor knows the conversation_id and history path
    monitor = ctx.conversation_monitor
    if monitor:
        history_path = monitor._get_history_path()
        return history_path.parent / "archive"
    # Fallback: derive from dpc_service
    dpc_dir = Path.home() / ".dpc"
    return dpc_dir / "conversations" / "agent_001" / "archive"


def read_session_archive(ctx: ToolContext, last_n: int = 3) -> str:
    """
    Read summaries of the last N archived sessions.

    Returns structured data: session date, message count, duration,
    and first few user messages as topic hints. Does NOT return full
    message content (use read_session_detail for that).

    Args:
        ctx: Tool context
        last_n: Number of recent sessions to read (default 3, max 10)

    Returns:
        JSON string with session summaries
    """
    last_n = min(max(1, last_n), 10)
    archive_dir = _get_archive_dir(ctx)

    if not archive_dir.exists():
        return json.dumps({"error": "No archive directory found", "sessions": []})

    archive_files = sorted(archive_dir.glob("*_session.json"))
    if not archive_files:
        return json.dumps({"error": "No archived sessions found", "sessions": []})

    # Take last N
    recent = archive_files[-last_n:]
    sessions = []

    for archive_path in recent:
        try:
            with open(archive_path, encoding="utf-8") as f:
                data = json.load(f)

            messages = data.get("messages", [])

            # Calculate duration from timestamps
            timestamps = [m.get("timestamp", "") for m in messages if m.get("timestamp")]
            duration_mins = 0.0
            if len(timestamps) >= 2:
                try:
                    first = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
                    last = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
                    duration_mins = round((last - first).total_seconds() / 60, 1)
                except (ValueError, TypeError):
                    pass

            # Extract user message previews as topic hints
            user_previews = [
                m.get("content", "")[:150]
                for m in messages
                if m.get("role") == "user" and m.get("content")
            ][:5]

            # Count participants
            participants = set()
            for m in messages:
                name = m.get("sender_name", m.get("role", "unknown"))
                if name:
                    participants.add(name)

            sessions.append({
                "file": archive_path.name,
                "archived_at": data.get("archived_at", ""),
                "session_reason": data.get("session_reason", ""),
                "message_count": len(messages),
                "duration_mins": duration_mins,
                "participants": sorted(participants),
                "user_message_previews": user_previews,
            })

        except Exception as e:
            sessions.append({
                "file": archive_path.name,
                "error": str(e),
            })

    return json.dumps({
        "total_archives": len(archive_files),
        "showing": len(sessions),
        "sessions": sessions,
    }, ensure_ascii=False)


def read_session_detail(ctx: ToolContext, filename: str, max_messages: int = 50) -> str:
    """
    Read detailed content of a specific archived session.

    Returns the actual messages from the session, truncated to max_messages.
    Use read_session_archive first to find the filename.

    Args:
        ctx: Tool context
        filename: Archive filename (e.g., "2026-04-07T18-08-22_reset_session.json")
        max_messages: Maximum messages to return (default 50)

    Returns:
        JSON string with session messages
    """
    max_messages = min(max(1, max_messages), 200)
    archive_dir = _get_archive_dir(ctx)
    archive_path = archive_dir / filename

    if not archive_path.exists():
        return json.dumps({"error": f"Archive not found: {filename}"})

    # Security: ensure the file is within archive directory
    try:
        archive_path.resolve().relative_to(archive_dir.resolve())
    except ValueError:
        return json.dumps({"error": "Path traversal not allowed"})

    try:
        with open(archive_path, encoding="utf-8") as f:
            data = json.load(f)

        messages = data.get("messages", [])

        # Truncate messages and content
        truncated_messages = []
        for m in messages[:max_messages]:
            content = m.get("content", "")
            if len(content) > 500:
                content = content[:500] + f"... ({len(content)} chars total)"
            truncated_messages.append({
                "role": m.get("role", ""),
                "sender_name": m.get("sender_name", ""),
                "content": content,
                "timestamp": m.get("timestamp", ""),
            })

        return json.dumps({
            "filename": filename,
            "archived_at": data.get("archived_at", ""),
            "total_messages": len(messages),
            "showing": len(truncated_messages),
            "messages": truncated_messages,
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"Failed to read archive: {e}"})


# ---------------------------------------------------------------------------
# Tool Registry Export
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    """Export archive tools for registry."""
    return [
        ToolEntry(
            name="read_session_archive",
            schema={
                "name": "read_session_archive",
                "description": (
                    "Read summaries of recent archived sessions. Returns session dates, "
                    "message counts, durations, participants, and topic hints. "
                    "Use this to understand what happened in past sessions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "last_n": {
                            "type": "integer",
                            "description": "Number of recent sessions to read (1-10, default 3)",
                            "default": 3,
                        }
                    },
                    "required": []
                }
            },
            handler=read_session_archive,
            timeout_sec=30,
        ),
        ToolEntry(
            name="read_session_detail",
            schema={
                "name": "read_session_detail",
                "description": (
                    "Read detailed messages from a specific archived session. "
                    "Use read_session_archive first to find the filename, "
                    "then use this to read the actual conversation content."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "Archive filename from read_session_archive results",
                        },
                        "max_messages": {
                            "type": "integer",
                            "description": "Maximum messages to return (1-200, default 50)",
                            "default": 50,
                        }
                    },
                    "required": ["filename"]
                }
            },
            handler=read_session_detail,
            timeout_sec=30,
        ),
    ]
