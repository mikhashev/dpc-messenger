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
from typing import Any, Dict, List, Optional

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)


_NO_CONVERSATION = "No conversation context; cannot read session archives."


def _get_archive_dir(ctx: ToolContext) -> Optional[Path]:
    """The archive of the conversation this tool runs in, or None without one.

    No fallback folder: the one that stood here was agent_001's, so every agent
    without a monitor read agent_001's sessions.
    """
    monitor = ctx.conversation_monitor
    if not monitor:
        return None
    return monitor._get_history_path().parent / "archive"


def _closed_to_a_stranger(ctx: ToolContext) -> Optional[str]:
    """The refusal for a stranger's turn outside its own Telegram chat, else None.

    A Telegram turn that is not the owner's runs in `telegram-<chat id>` (step 1 of
    ADR-048), and that chat's archive is its own; any other archive is the owner's.
    """
    from .core import turn_audience
    from ..agent import STRANGER_TURN
    audience = turn_audience(ctx)
    if audience is None or audience[0] != STRANGER_TURN:
        return None
    conv = str(getattr(ctx, "conversation_id", None) or "")
    chat = getattr(ctx, "reply_telegram_chat_id", None)
    if conv.startswith("telegram-") and (not chat or conv == f"telegram-{chat}"):
        return None
    return ("⚠️ Session archives are not read on this turn: it is not the owner's, and "
            "only a Telegram chat's own archive opens to it.")


# ---------------------------------------------------------------------------
# Which session archives a turn opens (ARCHIVE-GROUP-ACCESS; Mike's call, 2026-11-09,
# review by Johnny and Zcode; ADR-048 "Session archives")
# ---------------------------------------------------------------------------
#
# Stranger: its own Telegram chat only (above). Owner: the current conversation, plus,
# with history.archive_include_groups on, every group conversation whose metadata.json
# agents map lists this agent under this node. A turn in a group with another node: that
# group's archive only, and only the sessions whose recorded `group.members` include
# every current member. Filenames repeat across conversations, so a file is always
# looked up inside one named conversation, never across folders.

_UNRECORDED = "unrecorded"
_FEWER = "fewer"
_UNKNOWN = "unknown"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _group_metadata(conversation_dir: Path) -> Optional[Dict[str, Any]]:
    """The group's metadata.json with a string group_id, else None (missing, broken)."""
    meta = _read_json(conversation_dir / "metadata.json")
    if meta is None or not isinstance(meta.get("group_id"), str) or not meta["group_id"]:
        return None
    return meta


def _this_node_id(ctx: ToolContext) -> Optional[str]:
    finder = getattr(getattr(ctx, "_agent", None), "_this_node_id", None)
    if not callable(finder):
        return None
    try:
        node_id = finder()
    except Exception:
        return None
    return node_id if isinstance(node_id, str) and node_id else None


def _profile(ctx: ToolContext) -> Optional[str]:
    return getattr(getattr(ctx, "_agent", None), "_firewall_profile", None) or ctx.agent_root.name


def _agents_map_holds_this_agent(meta: Dict[str, Any], ctx: ToolContext) -> bool:
    """The group side of the permission: this agent listed under this node. Fails closed."""
    agents = meta.get("agents")
    if not isinstance(agents, dict) or not agents:
        return False
    node_id = _this_node_id(ctx)
    if not node_id:
        return False
    ids = agents.get(node_id)
    return isinstance(ids, list) and ctx.agent_root.name in ids


def _included_groups(ctx: ToolContext, base: Path, current_dir: Path) -> List[tuple]:
    """(group id, archive dir) of every other group conversation this agent is in."""
    from dpc_client_core import conversation_paths
    found: List[tuple] = []
    try:
        dirs = sorted(d for d in base.iterdir() if d.is_dir())
    except OSError:
        return found
    current = current_dir.resolve()
    for d in dirs:
        if not d.name.lower().startswith("group-") or d.resolve() == current:
            continue
        meta = _group_metadata(d)
        if meta is None:
            continue
        group_id = meta["group_id"]
        if not conversation_paths.is_store_dir_name(d.name, group_id):
            continue
        try:
            # One folder per conversation id, the one every other reader resolves.
            if conversation_paths.resolve_store_dir(base, group_id).resolve() != d.resolve():
                continue
        except ValueError:
            continue
        if _agents_map_holds_this_agent(meta, ctx):
            found.append((group_id, d / "archive"))
    return found


class _OpenArchives:
    """The archives a turn opens and, for a turn in a group with another node, the
    members every session must record."""

    def __init__(self, sources: List[tuple], filtered: bool = False,
                 group_id: Optional[str] = None, current_members: Optional[frozenset] = None):
        self.sources = sources  # [(conversation id, archive dir)], the current one first
        self.filtered = filtered
        self.group_id = group_id
        self.current_members = current_members
        self.skipped = {_UNRECORDED: 0, _FEWER: 0, _UNKNOWN: 0}

    def skip_reason(self, data: Optional[Dict[str, Any]]) -> Optional[str]:
        if not self.filtered:
            return None
        if not self.current_members:
            return _UNKNOWN
        record = data.get("group") if isinstance(data, dict) else None
        recorded = record.get("members") if isinstance(record, dict) else None
        if not isinstance(recorded, list) or record.get("group_id") != self.group_id:
            return _UNRECORDED
        if not self.current_members <= {m for m in recorded if isinstance(m, str)}:
            return _FEWER
        return None

    def any_dir(self) -> bool:
        return any(d.exists() for _, d in self.sources)

    def files(self) -> List[tuple]:
        """(conversation id, path) of every session file, oldest first by name."""
        out: List[tuple] = []
        for cid, d in self.sources:
            if d.exists():
                out.extend((cid, p) for p in d.rglob("*_session.json"))
        out.sort(key=lambda cp: (cp[1].name, cp[0]))
        return out

    def visible(self) -> List[tuple]:
        """(conversation id, path, data or None) this turn may read; counts the rest."""
        out: List[tuple] = []
        for cid, path in self.files():
            if not self.filtered:
                out.append((cid, path, None))
                continue
            data = _read_json(path)
            reason = self.skip_reason(data)
            if reason:
                self.skipped[reason] += 1
            else:
                out.append((cid, path, data))
        return out

    def skipped_note(self) -> Optional[str]:
        """One line on the sessions not shown, so the agent does not take them for absent."""
        total = sum(self.skipped.values())
        if not total:
            return None
        head = f"{total} session(s) of this group exist but are not shown on this turn"
        if self.skipped[_UNKNOWN]:
            return (f"{head}: the group's current members are not known here, so no session "
                    f"can be checked against them.")
        parts = []
        if self.skipped[_UNRECORDED]:
            parts.append(f"{self.skipped[_UNRECORDED]} record no members (archived before "
                         f"members were recorded)")
        if self.skipped[_FEWER]:
            parts.append(f"{self.skipped[_FEWER]} were held without every current member")
        return (f"{head}: " + " and ".join(parts) + "; with another node in the group only "
                f"sessions every current member witnessed open.")


def _current_conversation_id(ctx: ToolContext, conversation_dir: Path) -> str:
    cid = getattr(ctx.conversation_monitor, "conversation_id", None)
    if not isinstance(cid, str) or not cid:
        cid = getattr(ctx, "conversation_id", None)
    return str(cid or conversation_dir.name)


def _open_archives(ctx: ToolContext):
    """The refusal string for this turn, or the _OpenArchives it may read."""
    refusal = _closed_to_a_stranger(ctx)
    if refusal:
        return refusal
    archive_dir = _get_archive_dir(ctx)
    if archive_dir is None:
        return _NO_CONVERSATION
    from .core import turn_audience
    from ..agent import OTHER_NODE_TURN, OWNER_TURN
    conversation_dir = archive_dir.parent
    sources = [(_current_conversation_id(ctx, conversation_dir), archive_dir)]
    audience = turn_audience(ctx)
    cls = audience[0] if audience else None
    if cls == OTHER_NODE_TURN:
        meta = _group_metadata(conversation_dir)
        members = meta.get("members") if meta else None
        current = (frozenset(m for m in members if isinstance(m, str))
                   if isinstance(members, list) else frozenset())
        return _OpenArchives(sources, filtered=True,
                             group_id=meta["group_id"] if meta else None,
                             current_members=current or None)
    firewall = getattr(ctx, "firewall", None)
    if cls == OWNER_TURN and firewall is not None:
        try:
            include = firewall.get_archive_include_groups(_profile(ctx))
        except Exception:
            include = False
        if include is True:
            sources += _included_groups(ctx, conversation_dir.parent, conversation_dir)
    return _OpenArchives(sources)


def _not_open(conversation_id: str) -> str:
    return (f"⚠️ Conversation {conversation_id} is not open to session archive reads on "
            f"this turn.")


def _skipped_session(filename: str, reason: str) -> str:
    why = {
        _UNKNOWN: "this group's current members are not known here",
        _UNRECORDED: "it records no members (archived before members were recorded)",
        _FEWER: "it was held without every current member of this group",
    }[reason]
    return f"⚠️ Session {filename} is not open on this turn: {why}."


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
    rule = _open_archives(ctx)
    if isinstance(rule, str):
        return rule

    if not rule.any_dir():
        return json.dumps({"error": "No archive directory found", "sessions": []})

    # ADR-008: rglob to find sessions in YYYY/MM subdirs + flat (backward compat)
    if not rule.files():
        return json.dumps({"error": "No archived sessions found", "sessions": []})
    archive_files = rule.visible()
    skipped = rule.skipped_note()

    total = len(archive_files)

    # Guard: offset beyond available archives → empty result with info
    if offset >= total:
        out = {
            "total_archives": total,
            "offset": offset,
            "showing": 0,
            "sessions": [],
            "note": " ".join(n for n in (
                f"offset {offset} >= total_archives {total}" if total else None, skipped) if n),
        }
        return json.dumps(out, ensure_ascii=False)

    # Take last N, skipping the most recent `offset` entries
    end = total - offset
    start = max(0, end - last_n)
    recent = archive_files[start:end]
    sessions = []

    for conversation_id, archive_path, loaded in recent:
        try:
            if loaded is not None:
                data = loaded
            else:
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
                "conversation_id": conversation_id,
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
                "conversation_id": conversation_id,
                "file": archive_path.name,
                "error": str(e),
            })

    out = {
        "total_archives": total,
        "offset": offset,
        "showing": len(sessions),
        "sessions": sessions,
    }
    if skipped:
        out["note"] = skipped
    return json.dumps(out, ensure_ascii=False)


def read_session_detail(
    ctx: ToolContext,
    filename: str,
    max_messages: int = 50,
    offset: int = 0,
    max_message_chars: int = 4000,
    include_thinking: bool = False,
    conversation_id: Optional[str] = None,
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
        conversation_id: The conversation the file belongs to, as the listing and
                search results name it. Default: the current conversation. A
                conversation this turn may not read is refused.

    Returns:
        JSON string with session messages (includes offset, total_messages)
    """
    max_messages = min(max(1, max_messages), 200)
    offset = max(0, offset)
    max_message_chars = max(100, max_message_chars)
    rule = _open_archives(ctx)
    if isinstance(rule, str):
        return rule
    if conversation_id:
        opened = [d for cid, d in rule.sources if cid == conversation_id]
        if not opened:
            return _not_open(conversation_id)
        archive_dir = opened[0]
    else:
        conversation_id, archive_dir = rule.sources[0]

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

        reason = rule.skip_reason(data)
        if reason:
            return _skipped_session(filename, reason)

        messages = data.get("messages", [])
        total_messages = len(messages)

        # Guard: offset beyond available messages → empty result with info
        if offset >= total_messages:
            return json.dumps({
                "conversation_id": conversation_id,
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
            "conversation_id": conversation_id,
            "filename": filename,
            "archived_at": data.get("archived_at", ""),
            "total_messages": total_messages,
            "offset": offset,
            "showing": len(truncated_messages),
            "messages": truncated_messages,
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"Failed to read archive: {e}"})


def search_session_archives(
    ctx: ToolContext,
    query: str,
    scope: str = "content",
    last_n: int = 20,
    offset: int = 0,
    snippet_chars: int = 100,
) -> str:
    """
    Search across all archived sessions for messages matching a substring query.

    Case-insensitive substring match on `content` (default), `thinking`, or
    `both` scope. Returns matched messages with filename, message_index,
    snippet (±snippet_chars around first match), timestamp, and which scope
    field matched, each with its conversation_id. Use read_session_detail(filename,
    offset=message_index, conversation_id=...) to follow up on specific matches.

    Args:
        ctx: Tool context
        query: Substring to search for (case-insensitive, min 2 chars)
        scope: "content" (default), "thinking", or "both"
        last_n: Maximum matches to return per call (1-50, default 20)
        offset: Skip first N matches across all archives (default 0)
        snippet_chars: Context window around match (default 100, min 20)

    Returns:
        JSON string with matches list + total_matches + offset
    """
    query = (query or "").strip()
    if len(query) < 2:
        return json.dumps({
            "error": "Query must be at least 2 characters",
            "matches": [],
        })

    scope = scope if scope in ("content", "thinking", "both") else "content"
    last_n = min(max(1, last_n), 50)
    offset = max(0, offset)
    snippet_chars = max(20, snippet_chars)

    rule = _open_archives(ctx)
    if isinstance(rule, str):
        return rule
    if not rule.any_dir():
        return json.dumps({"error": "No archive directory found", "matches": []})

    if not rule.files():
        return json.dumps({"error": "No archived sessions found", "matches": []})
    archive_files = rule.visible()

    query_lower = query.lower()
    fields = ("content", "thinking") if scope == "both" else (scope,)
    all_matches: List[Dict[str, Any]] = []

    for conversation_id, archive_path, loaded in archive_files:
        data = loaded
        if data is None:
            try:
                with open(archive_path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

        messages = data.get("messages", [])
        for msg_idx, m in enumerate(messages):
            for field in fields:
                text = m.get(field, "") or ""
                if not text:
                    continue
                hit = text.lower().find(query_lower)
                if hit < 0:
                    continue
                start = max(0, hit - snippet_chars)
                end = min(len(text), hit + len(query) + snippet_chars)
                snippet = text[start:end]
                if start > 0:
                    snippet = "..." + snippet
                if end < len(text):
                    snippet = snippet + "..."
                all_matches.append({
                    "conversation_id": conversation_id,
                    "filename": archive_path.name,
                    "message_index": msg_idx,
                    "scope_matched": field,
                    "role": m.get("role", ""),
                    "sender_name": m.get("sender_name", ""),
                    "timestamp": m.get("timestamp", ""),
                    "snippet": snippet,
                })
                break  # one hit per message is enough

    total_matches = len(all_matches)
    page = all_matches[offset:offset + last_n]

    out = {
        "query": query,
        "scope": scope,
        "total_matches": total_matches,
        "offset": offset,
        "showing": len(page),
        "matches": page,
    }
    skipped = rule.skipped_note()
    if skipped:
        out["note"] = skipped
    return json.dumps(out, ensure_ascii=False)


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
                    "Paginate through older archives with `offset`. "
                    "Each session names its `conversation_id`: on the owner's turns, with "
                    "'Include group sessions' on, group chats this agent is in are listed "
                    "too; in a group with another node only sessions every current member "
                    "witnessed are listed, and a `note` counts the rest."
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
            default_enabled=True,
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
                    "include_thinking=True expect ~2-3× larger payload. "
                    "Pass `conversation_id` from the listing to open a file of another "
                    "conversation; filenames repeat across conversations, and one this "
                    "turn may not read is refused."
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
                        "conversation_id": {
                            "type": "string",
                            "description": (
                                "Conversation the file belongs to, as read_session_archive "
                                "or search_session_archives names it (default: the current "
                                "conversation)."
                            ),
                        },
                    },
                    "required": ["filename"]
                }
            },
            handler=read_session_detail,
            timeout_sec=30,
            default_enabled=True,
        ),
        ToolEntry(
            name="search_session_archives",
            schema={
                "name": "search_session_archives",
                "description": (
                    "Search across all archived sessions for messages matching a "
                    "substring query. Case-insensitive. Returns snippets with "
                    "conversation_id + filename + message_index so you can follow up via "
                    "read_session_detail(filename, offset=message_index, conversation_id=...). "
                    "Use this when looking for when/where a topic was discussed "
                    "across many sessions. It searches the same archives "
                    "read_session_archive lists on this turn."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Substring to search for (case-insensitive, min 2 chars)",
                        },
                        "scope": {
                            "type": "string",
                            "enum": ["content", "thinking", "both"],
                            "description": (
                                "Which message field to search. 'content' (default) "
                                "is the visible message text; 'thinking' is the "
                                "reasoning trace; 'both' searches both fields."
                            ),
                            "default": "content",
                        },
                        "last_n": {
                            "type": "integer",
                            "description": "Maximum matches to return per call (1-50, default 20)",
                            "default": 20,
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Skip first N matches across all archives (default 0, for pagination)",
                            "default": 0,
                        },
                        "snippet_chars": {
                            "type": "integer",
                            "description": "Context window around each match in chars (default 100, min 20)",
                            "default": 100,
                        },
                    },
                    "required": ["query"]
                }
            },
            handler=search_session_archives,
            timeout_sec=60,
            default_enabled=True,
        ),
    ]
