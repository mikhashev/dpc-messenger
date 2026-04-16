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


def read_session_archive(ctx: ToolContext, last_n: int = 3, offset: int = 0) -> str:
    """
    Read summaries of the last N archived sessions, optionally skipping the
    most recent `offset` sessions for navigation into older history.

    Returns structured data: session date, message count, duration,
    and first few user messages as topic hints. Does NOT return full
    message content (use read_session_detail for that).

    Args:
        ctx: Tool context
        last_n: Number of recent sessions to read (default 3, max 10)
        offset: Skip the N most recent sessions (default 0). Use for
                paging into older archives: offset=10 reads sessions 11-13
                counting from the newest.

    Returns:
        JSON string with session summaries (includes offset, total_archives)
    """
    last_n = min(max(1, last_n), 50)
    offset = max(0, offset)
    archive_dir = _get_archive_dir(ctx)

    if not archive_dir.exists():
        return json.dumps({"error": "No archive directory found", "sessions": []})

    # ADR-008: rglob to find sessions in YYYY/MM subdirs + flat (backward compat)
    archive_files = sorted(archive_dir.rglob("*_session.json"))
    if not archive_files:
        return json.dumps({"error": "No archived sessions found", "sessions": []})

    total = len(archive_files)

    # Guard: offset beyond available archives → empty result with info
    if offset >= total:
        return json.dumps({
            "total_archives": total,
            "offset": offset,
            "showing": 0,
            "sessions": [],
            "note": f"offset {offset} >= total_archives {total}",
        })

    # Take last N, skipping the most recent `offset` entries
    end = total - offset
    start = max(0, end - last_n)
    recent = archive_files[start:end]
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
        "total_archives": total,
        "offset": offset,
        "showing": len(sessions),
        "sessions": sessions,
    }, ensure_ascii=False)


def read_session_detail(
    ctx: ToolContext,
    filename: str,
    max_messages: int = 50,
    offset: int = 0,
    max_message_chars: int = 4000,
    include_thinking: bool = False,
) -> str:
    """
    Read detailed content of a specific archived session with message-level
    pagination. Use read_session_archive first to find the filename.

    Sizing hint: harness truncates tool results at ~15000 chars. Keep
    max_messages × max_message_chars ≤ 14000 to stay under the cap; when
    include_thinking=True, expect ~2-3× larger payload per message and
    reduce max_messages accordingly.

    Args:
        ctx: Tool context
        filename: Archive filename (e.g., "2026-04-07T18-08-22_reset_session.json")
        max_messages: Maximum messages to return per call (default 50, max 200)
        offset: Skip the first N messages (default 0). Use with max_messages
                to page through long sessions.
        max_message_chars: Per-message content truncation threshold in
                characters (default 4000). Larger values preserve more
                substantive content at the cost of output size.
        include_thinking: If True, include per-message `thinking` field
                (raw reasoning traces). Default False. Can significantly
                increase output size.

    Returns:
        JSON string with session messages (includes offset, total_messages)
    """
    max_messages = min(max(1, max_messages), 200)
    offset = max(0, offset)
    max_message_chars = max(100, max_message_chars)
    archive_dir = _get_archive_dir(ctx)

    # ADR-008: try direct path first (flat layout), then rglob (YYYY/MM layout)
    archive_path = archive_dir / filename
    if not archive_path.exists():
        # Search in YYYY/MM subdirectories
        matches = list(archive_dir.rglob(filename))
        if matches:
            archive_path = matches[0]
        else:
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
        total_messages = len(messages)

        # Guard: offset beyond available messages → empty result with info
        if offset >= total_messages:
            return json.dumps({
                "filename": filename,
                "archived_at": data.get("archived_at", ""),
                "total_messages": total_messages,
                "offset": offset,
                "showing": 0,
                "messages": [],
                "note": f"offset {offset} >= total_messages {total_messages}",
            }, ensure_ascii=False)

        # Paginate + per-message truncation
        truncated_messages = []
        for m in messages[offset:offset + max_messages]:
            content = m.get("content", "")
            if len(content) > max_message_chars:
                content = content[:max_message_chars] + f"... ({len(content)} chars total)"
            msg = {
                "role": m.get("role", ""),
                "sender_name": m.get("sender_name", ""),
                "content": content,
                "timestamp": m.get("timestamp", ""),
            }
            if include_thinking:
                thinking = m.get("thinking", "")
                if len(thinking) > max_message_chars:
                    thinking = thinking[:max_message_chars] + f"... ({len(thinking)} chars total)"
                msg["thinking"] = thinking
            truncated_messages.append(msg)

        return json.dumps({
            "filename": filename,
            "archived_at": data.get("archived_at", ""),
            "total_messages": total_messages,
            "offset": offset,
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
                    "Use this to understand what happened in past sessions. "
                    "Paginate through older archives with `offset`."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "last_n": {
                            "type": "integer",
                            "description": "Number of recent sessions to read (1-50, default 3)",
                            "default": 3,
                        },
                        "offset": {
                            "type": "integer",
                            "description": (
                                "Skip the N most recent sessions (default 0). "
                                "Use for paging into older archives."
                            ),
                            "default": 0,
                        },
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
                    "then use this to read the actual conversation content. "
                    "Supports message-level pagination via `offset`, per-message "
                    "content cap via `max_message_chars`, and optional `thinking` "
                    "field for self-analysis. "
                    "Sizing hint: keep max_messages × max_message_chars ≤ 14000 "
                    "to stay under the ~15000-char harness truncation cap; with "
                    "include_thinking=True expect ~2-3× larger payload."
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
                            "description": "Maximum messages to return per call (1-200, default 50)",
                            "default": 50,
                        },
                        "offset": {
                            "type": "integer",
                            "description": (
                                "Skip the first N messages (default 0). "
                                "Use with max_messages to page through long sessions."
                            ),
                            "default": 0,
                        },
                        "max_message_chars": {
                            "type": "integer",
                            "description": (
                                "Per-message content truncation threshold in characters "
                                "(default 4000, min 100). Larger values preserve more "
                                "substantive content at the cost of output size."
                            ),
                            "default": 4000,
                        },
                        "include_thinking": {
                            "type": "boolean",
                            "description": (
                                "If True, include per-message `thinking` field "
                                "(raw reasoning traces). Default False. Significantly "
                                "increases output size."
                            ),
                            "default": False,
                        },
                    },
                    "required": ["filename"]
                }
            },
            handler=read_session_detail,
            timeout_sec=30,
        ),
    ]
