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
from ..utils import auto_commit_agent_change

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File Operations (sandboxed)
# ---------------------------------------------------------------------------

def _paginate_content(content: str, path: str, offset: int | None, limit: int | None, fallback_truncate: int) -> str:
    """Apply line-based pagination or fallback truncation to file content."""
    lines = content.splitlines(keepends=True)
    total = len(lines)

    if offset is not None or limit is not None:
        start = offset or 0
        end = start + limit if limit is not None else total
        selected = lines[start:end]
        result = "".join(selected)
        shown_start = start + 1  # 1-based for display
        shown_end = min(end, total)
        return f"[Lines {shown_start}-{shown_end} of {total} total | {path}]\n{result}"

    # No pagination — apply legacy truncation
    if len(content) > fallback_truncate:
        content = content[:fallback_truncate] + f"\n\n... (truncated, {len(content)} total chars)"
    return content


def repo_read(ctx: ToolContext, path: str, offset: int | None = None, limit: int | None = None) -> str:
    """
    Read a file from the agent sandbox.

    Args:
        ctx: Tool context
        path: File path relative to agent root
        offset: Start line (0-based). If provided, enables pagination.
        limit: Number of lines to return. Used with offset for pagination.

    Returns:
        File contents (paginated or full with truncation)
    """
    try:
        file_path = ctx.repo_path(path)

        if not file_path.exists():
            return f"⚠️ File not found: {path}"

        if not file_path.is_file():
            return f"⚠️ Not a file: {path}"

        content = file_path.read_text(encoding="utf-8", errors="replace")
        return _paginate_content(content, path, offset, limit, fallback_truncate=50000)

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


def drive_read(ctx: ToolContext, path: str, offset: int | None = None, limit: int | None = None) -> str:
    """Alias for repo_read (compatibility with Ouroboros tools)."""
    return repo_read(ctx, path, offset=offset, limit=limit)


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

        # Regenerate _index.md if writing to knowledge/ dir
        if path.startswith("knowledge/") and not path.endswith("_index.md"):
            _update_knowledge_index(ctx, Path(path).stem)

        return f"✓ Wrote {len(content)} chars to {path}"

    except PermissionError as e:
        return f"⚠️ Sandbox violation: {e}"
    except Exception as e:
        return f"⚠️ Error writing file: {e}"


def drive_write(ctx: ToolContext, path: str, content: str) -> str:
    """Alias for repo_write (compatibility with Ouroboros tools)."""
    return repo_write(ctx, path, content)


def repo_delete(ctx: ToolContext, path: str, recursive: bool = False) -> str:
    """
    Delete a file or directory from the agent sandbox.

    Args:
        ctx: Tool context
        path: Path relative to agent root
        recursive: If True, delete directory and all contents

    Returns:
        Result message
    """
    try:
        target = ctx.repo_path(path)

        if not target.exists():
            return f"⚠️ Not found: {path}"

        if target.is_dir():
            if not recursive:
                return f"⚠️ '{path}' is a directory. Use recursive=true to delete it and all contents."
            import shutil
            count = sum(1 for _ in target.rglob("*") if _.is_file())
            shutil.rmtree(target)
            return f"✓ Deleted directory '{path}' ({count} files)"
        else:
            size = target.stat().st_size
            target.unlink()
            return f"✓ Deleted '{path}' ({size} bytes)"

    except PermissionError as e:
        return f"⚠️ Sandbox violation: {e}"
    except Exception as e:
        return f"⚠️ Error deleting: {e}"


# ---------------------------------------------------------------------------
# Memory Tools
# ---------------------------------------------------------------------------

def update_scratchpad(ctx: ToolContext, content: str, mode: str = "append") -> str:
    """
    Update the agent's scratchpad (working memory).

    Args:
        ctx: Tool context
        content: Content to add/update
        mode: Update mode - 'append', 'prepend', 'replace', 'deduplicate'
              - append: Add content to end
              - prepend: Add content to beginning
              - replace: Replace entire scratchpad
              - deduplicate: Remove duplicate lines/paragraphs without adding content

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
            new_content = existing + "\n\n" + content if existing.strip() else content
        elif mode == "prepend":
            existing = ""
            if scratchpad_path.exists():
                existing = scratchpad_path.read_text(encoding="utf-8")
            new_content = content + "\n\n" + existing if existing.strip() else content
        elif mode == "deduplicate":
            existing = ""
            if scratchpad_path.exists():
                existing = scratchpad_path.read_text(encoding="utf-8")
            # Remove duplicate paragraphs (blocks separated by blank lines)
            paragraphs = existing.split("\n\n")
            seen = set()
            unique_paragraphs = []
            duplicates_removed = 0
            for para in paragraphs:
                para_normalized = para.strip().lower()
                if para_normalized and para_normalized not in seen:
                    seen.add(para_normalized)
                    unique_paragraphs.append(para)
                elif para_normalized:
                    duplicates_removed += 1
            new_content = "\n\n".join(unique_paragraphs)
            if duplicates_removed > 0:
                log.info(f"Scratchpad deduplication removed {duplicates_removed} duplicate paragraphs")
        else:
            return f"⚠️ Unknown mode: {mode}. Use 'append', 'prepend', 'replace', or 'deduplicate'"

        scratchpad_path.parent.mkdir(parents=True, exist_ok=True)
        scratchpad_path.write_text(new_content.strip(), encoding="utf-8")

        # Log update
        journal_path = ctx.memory_path("scratchpad_journal.jsonl")
        journal_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "content_length": len(content),
            "total_length": len(new_content),
        }
        with open(journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(journal_entry) + "\n")

        msg = f"✓ Updated scratchpad ({mode} mode, {len(new_content)} total chars)"
        if mode == "deduplicate" and duplicates_removed > 0:
            msg += f", removed {duplicates_removed} duplicate(s)"

        return msg

    except Exception as e:
        return f"⚠️ Error updating scratchpad: {e}"


def deduplicate_identity(ctx: ToolContext) -> str:
    """
    Remove duplicate sections from the agent's identity file.

    This tool scans the identity.md file for duplicate section headers
    and removes all but the first occurrence of each section.

    Args:
        ctx: Tool context

    Returns:
        Summary of duplicates removed
    """
    try:
        identity_path = ctx.memory_path("identity.md")

        if not identity_path.exists():
            return "⚠️ Identity file does not exist"

        existing = identity_path.read_text(encoding="utf-8")
        original_len = len(existing)
        lines = existing.split("\n")

        # Find all section headers and their positions
        sections = {}  # normalized_name -> [(line_idx, original_header), ...]
        for i, line in enumerate(lines):
            if line.strip().startswith("## "):
                header = line.strip()[3:].strip()
                normalized = header.lower()
                if normalized not in sections:
                    sections[normalized] = []
                sections[normalized].append((i, header))

        # Find sections with duplicates
        duplicates = {k: v for k, v in sections.items() if len(v) > 1}

        if not duplicates:
            return "✓ No duplicate sections found in identity"

        # Get section bounds
        def get_section_bounds(start_idx):
            end_idx = len(lines)
            for i in range(start_idx + 1, len(lines)):
                if lines[i].startswith("## "):
                    end_idx = i
                    break
            return start_idx, end_idx

        # Remove duplicates from end to start to preserve indices
        total_removed = 0
        sections_cleaned = []
        for _normalized_name, occurrences in duplicates.items():
            # Keep first occurrence, remove rest
            for start_idx, _original_header in reversed(occurrences[1:]):
                _, end_idx = get_section_bounds(start_idx)
                del lines[start_idx:end_idx]
                total_removed += 1
            sections_cleaned.append(occurrences[0][1])  # Keep original header name

        # Update timestamp (replace old, don't accumulate)
        for i, line in enumerate(lines):
            if line.strip() == "## Last Updated":
                while i + 1 < len(lines) and lines[i + 1].strip() and not lines[i + 1].startswith("##"):
                    del lines[i + 1]
                lines.insert(i + 1, datetime.now(timezone.utc).isoformat())
                break

        new_content = "\n".join(lines)
        identity_path.write_text(new_content, encoding="utf-8")

        return f"✓ Deduplicated identity: removed {total_removed} duplicate section(s) from: {', '.join(sections_cleaned)}\nSize reduced: {original_len} → {len(new_content)} chars ({original_len - len(new_content)} chars saved)"

    except Exception as e:
        return f"⚠️ Error deduplicating identity: {e}"


def update_identity(
    ctx: ToolContext,
    section: str,
    content: str,
    mode: str = "replace",
    commit_message: Optional[str] = None,
) -> str:
    """
    Update a section of the agent's identity file.

    Args:
        ctx: Tool context
        section: Section name (e.g., 'values', 'goals', 'beliefs')
        content: Section content
        mode: Update mode - 'replace' (default), 'append', 'merge', 'deduplicate'
              - replace: Replace the first matching section, remove duplicates
              - append: Add content to existing section (removes duplicates first)
              - merge: Combine content with existing section (removes duplicates first)
              - deduplicate: Just remove duplicate sections without adding content
        commit_message: Conventional commit message for this change
            (e.g. 'chore(identity): refine core values after security audit').
            Defaults to 'chore(identity): update {section}'.

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
        lines = existing.split("\n")

        # Normalize section name for matching (case-insensitive)
        section_normalized = section.strip().lower()
        section_header = f"## {section.title()}"

        # Find ALL occurrences of the section (for deduplication)
        section_occurrences = []
        for i, line in enumerate(lines):
            # Match section headers case-insensitively
            if line.strip().lower().startswith("## ") and line.strip().lower()[3:] == section_normalized:
                section_occurrences.append(i)

        # Find section boundaries for each occurrence
        def get_section_bounds(start_idx):
            """Get start and end line indices for a section."""
            end_idx = len(lines)
            for i in range(start_idx + 1, len(lines)):
                if lines[i].startswith("## "):
                    end_idx = i
                    break
            return start_idx, end_idx

        # Remove duplicate sections (keep first occurrence)
        duplicates_removed = 0
        if len(section_occurrences) > 1:
            # Remove duplicates from end to start to preserve indices
            for dup_start in reversed(section_occurrences[1:]):
                _, dup_end = get_section_bounds(dup_start)
                del lines[dup_start:dup_end]
                duplicates_removed += 1

            # Recalculate first section bounds after deletions
            section_occurrences = [section_occurrences[0]]  # Keep only first

        # Now handle the content update
        if section_occurrences:
            # Section exists - update it
            section_start = section_occurrences[0]
            _, section_end = get_section_bounds(section_start)
            existing_content = "\n".join(lines[section_start + 1:section_end]).strip()

            if mode == "replace":
                new_section_content = content
            elif mode == "append":
                new_section_content = existing_content + "\n\n" + content if existing_content else content
            elif mode == "merge":
                # Merge without duplicating lines that already exist
                existing_lines = set(line.strip() for line in existing_content.split("\n") if line.strip())
                new_lines = [line for line in content.split("\n") if line.strip() and line.strip() not in existing_lines]
                new_section_content = existing_content + "\n" + "\n".join(new_lines) if new_lines else existing_content
            elif mode == "deduplicate":
                new_section_content = existing_content  # Just keep existing, duplicates already removed
            else:
                return f"⚠️ Unknown mode: {mode}. Use 'replace', 'append', 'merge', or 'deduplicate'"

            # Replace section content
            new_lines = lines[:section_start] + [section_header, new_section_content] + lines[section_end:]
        else:
            # Section doesn't exist - add it (unless just deduplicating)
            if mode == "deduplicate":
                new_lines = lines
            else:
                new_lines = lines + ["", section_header, content]

        # Update timestamp (replace old timestamp, don't accumulate)
        timestamp_updated = False
        for i, line in enumerate(new_lines):
            if line.strip() == "## Last Updated":
                # Remove old timestamp line(s) after the header
                while i + 1 < len(new_lines) and new_lines[i + 1].strip() and not new_lines[i + 1].startswith("##"):
                    del new_lines[i + 1]
                new_lines.insert(i + 1, datetime.now(timezone.utc).isoformat())
                timestamp_updated = True
                break

        if not timestamp_updated:
            # Add timestamp section at the beginning after title
            for i, line in enumerate(new_lines):
                if line.startswith("# ") and i == 0:
                    new_lines.insert(i + 2, "")
                    new_lines.insert(i + 3, f"## Last Updated")
                    new_lines.insert(i + 4, datetime.now(timezone.utc).isoformat())
                    break

        new_content = "\n".join(new_lines)
        identity_path.write_text(new_content, encoding="utf-8")

        # Auto-commit identity changes (best-effort)
        msg = commit_message or f"chore(identity): update {section}"
        auto_commit_agent_change(ctx.agent_root, msg)

        msg = f"✓ Updated identity section '{section}'"
        if duplicates_removed > 0:
            msg += f" (removed {duplicates_removed} duplicate(s))"

        return msg

    except Exception as e:
        return f"⚠️ Error updating identity: {e}"


def chat_history(ctx: ToolContext, limit: int = 0, offset: int = -1, include_internals: bool = False) -> str:
    """
    Read conversation messages (user and assistant turns).

    Use this to review the full conversation, assess context window usage,
    and decide whether to extract knowledge or suggest a session reset.

    Args:
        ctx: Tool context
        limit: Max messages to return. 0 = all messages (default).
        offset: Start position. -1 = from end/newest (default).
                0+ = from start/oldest (e.g. offset=0 returns oldest messages first).

    Examples:
        chat_history()                    → all messages (full session review)
        chat_history(limit=10)            → last 10 messages (most recent)
        chat_history(limit=1, offset=0)   → first message in session
        chat_history(limit=10, offset=0)  → first 10 messages (oldest)
        chat_history(limit=10, offset=20) → messages 20-29 from start

    Returns:
        Conversation messages with role, sender, timestamp, and content
    """
    try:
        limit = int(limit) if limit is not None else 0
        offset = int(offset) if offset is not None else -1

        monitor = getattr(ctx, 'conversation_monitor', None)
        if not monitor:
            return "No conversation monitor available for this session"

        history = monitor.message_history
        if not history:
            return "No conversation history yet"

        if offset >= 0:
            selected = history[offset:offset + limit] if limit > 0 else history[offset:]
        else:
            selected = history[-limit:] if limit > 0 else history

        output_lines = [f"Conversation ({len(selected)} of {len(history)} total messages):\n"]
        for msg in selected:
            role = msg.get("role", "unknown")
            sender = msg.get("sender_name", role)
            ts = msg.get("timestamp", "")
            content = msg.get("content", "")
            ts_str = f" [{ts[:19]}]" if ts else ""
            output_lines.append(f"{sender}{ts_str}: {content}")
            if include_internals:
                thinking = msg.get("thinking")
                streaming_raw = msg.get("streaming_raw")
                if thinking:
                    output_lines.append(f"  [THINKING]: {thinking[:500]}")
                if streaming_raw:
                    output_lines.append(f"  [RAW]: {streaming_raw[:500]}")

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


def knowledge_write(
    ctx: ToolContext,
    topic: str,
    content: str,
    commit_message: Optional[str] = None,
) -> str:
    """
    Write or update a knowledge base topic with firewall check.

    Args:
        ctx: Tool context
        topic: Topic name
        content: Topic content (markdown)
        commit_message: Conventional commit message for this change
            (e.g. 'docs(knowledge): add TurboQuant complexity analysis').
            Defaults to 'docs(knowledge): update {topic}'.

    Returns:
        Result message
    """
    try:
        # Check firewall for write access
        if ctx.dpc_service and hasattr(ctx.dpc_service, 'firewall'):
            firewall = ctx.dpc_service.firewall
            if not getattr(firewall, 'can_agent_write_knowledge', lambda: True)():
                return "⚠️ Knowledge write access is disabled via firewall rules (knowledge_access must be 'read_write')"

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

        # Auto-commit knowledge changes (best-effort)
        msg = commit_message or f"docs(knowledge): update {topic}"
        auto_commit_agent_change(ctx.agent_root, msg)

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
# Agent Progress Board Tool
# ---------------------------------------------------------------------------

def get_task_board(ctx: ToolContext) -> str:
    """
    Read your Agent Progress Board — the shared task and learning workspace
    visible to both you and the user in the DPC desktop UI.

    Returns two sections:

    TASK HISTORY — your last 20 completed/failed tasks from logs/events.jsonl.
    This is populated automatically whenever you run tasks; no action needed.

    LEARNING PROGRESS — your current status parsed from the
    ## Progress Tracking section of knowledge/llm_learning_schedule.md.
    You are responsible for keeping this section current.

    IMPORTANT — after every learning session, update llm_learning_schedule.md
    via knowledge_write using this exact format inside ## Progress Tracking:

        ### Task 1.1: Title
        Status: completed | in_progress | pending
        Started: YYYY-MM-DD
        Last Activity: YYYY-MM-DD      (update this every session)
        Session Summary: One sentence.
        Next Step: Specific actionable next step.

    The backend auto-computes 'stalled' when Last Activity is more than 3 days
    old and status is in_progress — do not write 'stalled' yourself.

    Use this tool at the start of a learning session to review where you left off,
    or before scheduling a task to understand what is already queued.
    """
    import re
    from datetime import datetime, timezone

    output_parts: list[str] = []

    # --- Task History ---
    try:
        events_path = ctx.logs_path("events.jsonl")
        task_events: list[dict] = []
        if events_path.exists():
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("type") in ("task_start", "task_complete", "task_error"):
                            task_events.append(entry)
                    except json.JSONDecodeError:
                        continue

        recent = task_events[-40:]  # up to 40 events = up to 20 task pairs
        if recent:
            lines = ["=== Task History (recent) ==="]
            for ev in recent:
                ts = ev.get("ts", "")[:10]
                etype = ev.get("type", "")
                preview = ev.get("text_preview") or ev.get("response_preview") or ""
                preview = preview[:80].replace("\n", " ")
                lines.append(f"[{ts}] {etype}: {preview}")
            output_parts.append("\n".join(lines))
        else:
            output_parts.append("=== Task History ===\nNo task events found.")
    except Exception as e:
        output_parts.append(f"=== Task History ===\nError reading task history: {e}")

    # --- Learning Progress ---
    try:
        knowledge_dir = ctx.agent_root / "knowledge"
        schedule_path = knowledge_dir / "llm_learning_schedule.md"
        if not schedule_path.exists():
            output_parts.append(
                "=== Learning Progress ===\n"
                "knowledge/llm_learning_schedule.md not found.\n"
                "Create it with a ## Progress Tracking section to use this feature."
            )
        else:
            content = schedule_path.read_text(encoding="utf-8", errors="replace")
            # Extract only the ## Progress Tracking section
            match = re.search(r"##\s+Progress Tracking\s*\n(.*?)(?=\n##\s|\Z)", content, re.DOTALL)
            if not match:
                output_parts.append(
                    "=== Learning Progress ===\n"
                    "No ## Progress Tracking section found in llm_learning_schedule.md."
                )
            else:
                tracking_text = match.group(1).strip()
                now = datetime.now(timezone.utc)
                lines = ["=== Learning Progress ==="]
                for task_block in re.split(r"(?=###\s+Task)", tracking_text):
                    task_block = task_block.strip()
                    if not task_block:
                        continue
                    header_match = re.match(r"###\s+(Task\s+[\d.]+):\s+(.+)", task_block)
                    if not header_match:
                        continue
                    task_id = header_match.group(1)
                    title = header_match.group(2).strip()
                    status_m = re.search(r"^Status:\s*(.+)$", task_block, re.MULTILINE)
                    last_act_m = re.search(r"^Last Activity:\s*(.+)$", task_block, re.MULTILINE)
                    next_step_m = re.search(r"^Next Step:\s*(.+)$", task_block, re.MULTILINE)
                    status = status_m.group(1).strip() if status_m else "unknown"
                    last_activity = last_act_m.group(1).strip() if last_act_m else None
                    next_step = next_step_m.group(1).strip() if next_step_m else None
                    # Stalled detection
                    if status == "in_progress" and last_activity:
                        try:
                            la_date = datetime.strptime(last_activity, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            days_ago = (now - la_date).days
                            if days_ago > 3:
                                status = f"stalled ({days_ago} days)"
                        except ValueError:
                            pass
                    line = f"  {task_id}: {title}  [{status}]"
                    if next_step and "in_progress" in status or "stalled" in status:
                        line += f"\n    Next: {next_step}"
                    lines.append(line)
                output_parts.append("\n".join(lines))
    except Exception as e:
        output_parts.append(f"=== Learning Progress ===\nError reading learning data: {e}")

    return "\n\n".join(output_parts)


# ---------------------------------------------------------------------------
# DPC Integration Tools
# ---------------------------------------------------------------------------

def get_dpc_context(ctx: ToolContext, context_type: str = "personal") -> str:
    """
    Read DPC personal or device context with firewall checks.

    This tool provides access to the user's personal.json or device_context.json
    files, enabling context-aware assistance.

    Args:
        ctx: Tool context
        context_type: Type of context - 'personal' or 'device'

    Returns:
        Context content
    """
    try:
        # Check firewall if available via DPC service (per-agent profile if set)
        if ctx.dpc_service and hasattr(ctx.dpc_service, 'firewall'):
            firewall = ctx.dpc_service.firewall
            _profile = getattr(getattr(ctx, "_agent", None), "_firewall_profile", None)

            if not getattr(firewall, 'dpc_agent_enabled', True):
                return "⚠️ DPC Agent is disabled via firewall rules"

            if context_type == "personal" and not firewall.can_agent_access_context("personal", profile_name=_profile):
                return "⚠️ Personal context access is disabled via firewall rules"

            if context_type == "device" and not firewall.can_agent_access_context("device", profile_name=_profile):
                return "⚠️ Device context access is disabled via firewall rules"

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
# Task Queue Tools
# ---------------------------------------------------------------------------

def schedule_task(
    ctx: ToolContext,
    task_type: str,
    task_data: str,
    delay_seconds: int = 0,
    priority: str = "normal",
) -> str:
    """
    Schedule a task for future execution.

    Args:
        ctx: Tool context
        task_type: Type of task ('chat', 'improvement', 'review', or custom registered type)
        task_data: JSON string with task payload
        delay_seconds: Delay before execution (0 = immediate)
        priority: 'critical', 'high', 'normal', or 'low'

    Returns:
        Task ID and confirmation
    """
    try:
        # Check if agent has task queue
        if not hasattr(ctx, '_agent') or not hasattr(ctx._agent, 'queue'):
            return "⚠️ Task queue not available"

        # Check if task type can be handled
        builtin_types = {"chat", "improvement", "review", "reminder"}
        custom_handlers = getattr(ctx._agent, '_task_handlers', {})
        if task_type not in builtin_types and task_type not in custom_handlers:
            available = list(builtin_types) + list(custom_handlers.keys())
            return f"⚠️ Unknown task type '{task_type}'. Available types: {available}. Register a handler with agent.register_task_handler('{task_type}', handler)"

        # Parse task data
        try:
            data = json.loads(task_data)
        except json.JSONDecodeError:
            return "⚠️ task_data must be valid JSON"

        # Map priority string
        from ..task_queue import TaskPriority
        priority_map = {
            "critical": TaskPriority.CRITICAL,
            "high": TaskPriority.HIGH,
            "normal": TaskPriority.NORMAL,
            "low": TaskPriority.LOW,
        }
        task_priority = priority_map.get(priority.lower(), TaskPriority.NORMAL)

        # Inject reply routing so the executor knows where to send the result.
        # _reply_conversation_id: the conversation that triggered this schedule call,
        #   so streaming progress and the final result appear in the right chat.
        # _reply_telegram_chat_id: set when the trigger came from Telegram,
        #   so the result is sent back to that Telegram chat automatically.
        if "_reply_conversation_id" not in data and ctx.current_task_id:
            data["_reply_conversation_id"] = ctx.current_task_id
        if "_reply_telegram_chat_id" not in data and getattr(ctx, "reply_telegram_chat_id", None):
            data["_reply_telegram_chat_id"] = ctx.reply_telegram_chat_id

        # Schedule task
        task = ctx._agent.schedule_task(
            task_type=task_type,
            data=data,
            priority=task_priority,
            delay_seconds=delay_seconds,
        )

        return f"✓ Scheduled task {task.id} (type={task_type}, priority={priority}, delay={delay_seconds}s)"

    except Exception as e:
        return f"⚠️ Error scheduling task: {e}"


def get_task_status(ctx: ToolContext, task_id: str) -> str:
    """
    Get status of a scheduled task.

    Args:
        ctx: Tool context
        task_id: Task ID to check

    Returns:
        Task status information
    """
    try:
        if not hasattr(ctx, '_agent') or not hasattr(ctx._agent, 'queue'):
            return "⚠️ Task queue not available"

        task = ctx._agent.queue.get_task(task_id)
        if not task:
            return f"⚠️ Task not found: {task_id}"

        return json.dumps({
            "id": task.id,
            "type": task.task_type,
            "status": task.status,
            "created_at": task.created_at,
            "scheduled_at": task.scheduled_at,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
            "result": task.result[:200] if task.result else None,
            "error": task.error[:200] if task.error else None,
            "retry_count": task.retry_count,
        }, indent=2)

    except Exception as e:
        return f"⚠️ Error getting task status: {e}"


def register_task_type(
    ctx: ToolContext,
    task_type: str,
    description: str,
    execution_prompt: str,
    input_schema: str = "{}",
) -> str:
    """
    Register a custom task type with execution instructions.

    The agent will follow execution_prompt when the task runs,
    with task.data variables substituted into the prompt.

    IMPORTANT: Always register the task type BEFORE scheduling tasks of that type.

    Args:
        ctx: Tool context
        task_type: Unique identifier (e.g., "weather_report"). Use snake_case.
        description: What this task type does (human-readable)
        execution_prompt: Instructions for the agent to follow when executing.
                         Use {variable} placeholders that will be filled from task.data.
        input_schema: JSON schema for validating task.data (optional, as JSON string)

    Returns:
        Confirmation message with task type details

    Example:
        register_task_type(
            task_type="weather_report",
            description="Fetch and summarize weather for a location",
            execution_prompt="Fetch current weather for {location} and provide a summary.
                             Use web search if needed. Include temperature and conditions.",
            input_schema='{"type": "object", "properties": {"location": {"type": "string"}}}'
        )

        Then schedule: schedule_task("weather_report", '{"location": "New York"}', delay_seconds=60)
    """
    try:
        # Check if agent is available
        if not hasattr(ctx, '_agent'):
            return "⚠️ Agent not available in context"

        # Parse input schema
        try:
            schema = json.loads(input_schema) if input_schema else {}
        except json.JSONDecodeError:
            return "⚠️ input_schema must be valid JSON"

        # Validate task_type format
        if not task_type or not task_type.replace("_", "").isalnum():
            return "⚠️ task_type must be alphanumeric with underscores (e.g., 'weather_report')"

        # Check if it conflicts with built-in types
        from ..task_types import BUILTIN_TASK_TYPES
        if task_type in BUILTIN_TASK_TYPES:
            return f"⚠️ Cannot override built-in task type: {task_type}"

        # Register the task type
        definition = ctx._agent.register_task_type(
            task_type=task_type,
            description=description,
            execution_prompt=execution_prompt,
            input_schema=schema,
        )

        return json.dumps({
            "status": "registered",
            "task_type": definition.task_type,
            "description": definition.description,
            "input_schema": definition.input_schema,
            "message": f"✓ Task type '{task_type}' registered. You can now schedule tasks of this type."
        }, indent=2)

    except Exception as e:
        return f"⚠️ Error registering task type: {e}"


def list_task_types(ctx: ToolContext) -> str:
    """
    List all registered task types (custom and built-in).

    Args:
        ctx: Tool context

    Returns:
        JSON list of available task types with their descriptions
    """
    try:
        if not hasattr(ctx, '_agent'):
            return "⚠️ Agent not available in context"

        task_types = ctx._agent.list_task_types()

        result = {
            "task_types": [
                {
                    "task_type": tt,
                    "description": defn.description,
                    "input_schema": defn.input_schema,
                    "is_builtin": tt in ["chat", "improvement", "review"],
                }
                for tt, defn in task_types.items()
            ],
            "total": len(task_types),
        }

        return json.dumps(result, indent=2)

    except Exception as e:
        return f"⚠️ Error listing task types: {e}"


def unregister_task_type(ctx: ToolContext, task_type: str) -> str:
    """
    Unregister a custom task type.

    Args:
        ctx: Tool context
        task_type: Task type to unregister

    Returns:
        Confirmation message
    """
    try:
        if not hasattr(ctx, '_agent'):
            return "⚠️ Agent not available in context"

        # Check if it's a built-in type
        from ..task_types import BUILTIN_TASK_TYPES
        if task_type in BUILTIN_TASK_TYPES:
            return f"⚠️ Cannot unregister built-in task type: {task_type}"

        if ctx._agent.unregister_task_type(task_type):
            return f"✓ Task type '{task_type}' unregistered"
        else:
            return f"⚠️ Task type '{task_type}' not found"

    except Exception as e:
        return f"⚠️ Error unregistering task type: {e}"


# ---------------------------------------------------------------------------
# Evolution Tools
# ---------------------------------------------------------------------------

def pause_evolution(ctx: ToolContext) -> str:
    """
    Pause automatic evolution cycles.

    Args:
        ctx: Tool context

    Returns:
        Confirmation message
    """
    try:
        if not hasattr(ctx, '_agent') or not hasattr(ctx._agent, '_evolution'):
            return "⚠️ Evolution not enabled"

        if ctx._agent._evolution:
            ctx._agent._evolution.pause()
            return "✓ Evolution paused"
        return "⚠️ Evolution not running"

    except Exception as e:
        return f"⚠️ Error pausing evolution: {e}"


def resume_evolution(ctx: ToolContext) -> str:
    """
    Resume automatic evolution cycles.

    Args:
        ctx: Tool context

    Returns:
        Confirmation message
    """
    try:
        if not hasattr(ctx, '_agent') or not hasattr(ctx._agent, '_evolution'):
            return "⚠️ Evolution not enabled"

        if ctx._agent._evolution:
            ctx._agent._evolution.resume()
            return "✓ Evolution resumed"
        return "⚠️ Evolution not initialized"

    except Exception as e:
        return f"⚠️ Error resuming evolution: {e}"


def get_evolution_stats(ctx: ToolContext) -> str:
    """
    Get evolution statistics and pending changes.

    Args:
        ctx: Tool context

    Returns:
        Evolution status information
    """
    try:
        if not hasattr(ctx, '_agent'):
            return "⚠️ Agent not available"

        if ctx._agent._evolution:
            status = ctx._agent._evolution.get_status()
            pending = ctx._agent.get_pending_evolution_changes()
            status["pending_changes_detail"] = pending
            return json.dumps(status, indent=2)

        return json.dumps({
            "enabled": False,
            "message": "Evolution not enabled. Enable in config with evolution_enabled=true"
        }, indent=2)

    except Exception as e:
        return f"⚠️ Error getting evolution stats: {e}"


async def approve_evolution_change(ctx: ToolContext, change_id: str) -> str:
    """
    Approve a pending evolution change.

    Args:
        ctx: Tool context
        change_id: ID of the change to approve

    Returns:
        Confirmation message
    """
    try:
        if not hasattr(ctx, '_agent'):
            return "⚠️ Agent not available"

        if ctx._agent._evolution:
            success = await ctx._agent.approve_evolution_change(change_id)
            if success:
                return f"✓ Change {change_id} approved and applied"
            return f"⚠️ Failed to approve change {change_id} — not found or already applied"
        return "⚠️ Evolution not enabled"

    except Exception as e:
        return f"⚠️ Error approving change: {e}"


def reject_evolution_change(ctx: ToolContext, change_id: str) -> str:
    """
    Reject a pending evolution change.

    Args:
        ctx: Tool context
        change_id: ID of the change to reject

    Returns:
        Confirmation message
    """
    try:
        if not hasattr(ctx, '_agent'):
            return "⚠️ Agent not available"

        if ctx._agent._evolution:
            success = ctx._agent.reject_evolution_change(change_id)
            if success:
                return f"✓ Change {change_id} rejected"
            return f"⚠️ Change {change_id} not found"
        return "⚠️ Evolution not enabled"

    except Exception as e:
        return f"⚠️ Error rejecting change: {e}"


# ---------------------------------------------------------------------------
# Extended Sandbox Tools (v0.16.0+)
# ---------------------------------------------------------------------------

def extended_path_read(ctx: ToolContext, path: str, offset: int | None = None, limit: int | None = None) -> str:
    """
    Read a file from an extended sandbox path (outside ~/.dpc/agent/).

    Requires the path to be configured in privacy_rules.json under
    dpc_agent.sandbox_extensions.read_only or read_write.

    Args:
        ctx: Tool context
        path: Absolute file path (must be in extended sandbox)
        offset: Start line (0-based). If provided, enables pagination.
        limit: Number of lines to return. Used with offset for pagination.

    Returns:
        File contents (paginated or full with truncation)
    """
    try:
        file_path = ctx.validate_extended_path(path, require_write=False)

        if not file_path.exists():
            return f"⚠️ File not found: {path}"

        if not file_path.is_file():
            return f"⚠️ Not a file: {path}"

        content = file_path.read_text(encoding="utf-8", errors="replace")
        return _paginate_content(content, path, offset, limit, fallback_truncate=100000)

    except PermissionError as e:
        return f"⚠️ Access denied: {e}"
    except Exception as e:
        return f"⚠️ Error reading file: {e}"


def extended_path_list(ctx: ToolContext, path: str, recursive: bool = False) -> str:
    """
    List files in an extended sandbox directory.

    Args:
        ctx: Tool context
        path: Absolute directory path (must be in extended sandbox)
        recursive: If True, list recursively

    Returns:
        Directory listing
    """
    try:
        dir_path = ctx.validate_extended_path(path, require_write=False)

        if not dir_path.exists():
            return f"⚠️ Directory not found: {path}"

        if not dir_path.is_dir():
            return f"⚠️ Not a directory: {path}"

        items = []
        if recursive:
            for item in dir_path.rglob("*"):
                rel = item.relative_to(dir_path)
                if item.is_dir():
                    items.append(f"📁 {rel}/")
                else:
                    size = item.stat().st_size
                    items.append(f"📄 {rel} ({size:,} bytes)")
        else:
            for item in sorted(dir_path.iterdir()):
                if item.is_dir():
                    items.append(f"📁 {item.name}/")
                else:
                    size = item.stat().st_size
                    items.append(f"📄 {item.name} ({size:,} bytes)")

        if not items:
            return f"Directory '{path}' is empty"

        return "\n".join(items[:200]) + (f"\n\n... ({len(items)} total items)" if len(items) > 200 else "")

    except PermissionError as e:
        return f"⚠️ Access denied: {e}"
    except Exception as e:
        return f"⚠️ Error listing directory: {e}"


def extended_path_write(ctx: ToolContext, path: str, content: str) -> str:
    """
    Write a file to an extended sandbox path (outside ~/.dpc/agent/).

    Requires the path to be configured in privacy_rules.json under
    dpc_agent.sandbox_extensions.read_write.

    Args:
        ctx: Tool context
        path: Absolute file path (must be in extended sandbox with write access)
        content: Content to write

    Returns:
        Confirmation message
    """
    try:
        file_path = ctx.validate_extended_path(path, require_write=True)

        # Create parent directories if needed
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Check content size
        if len(content) > 500000:
            return f"⚠️ Content too large ({len(content):,} chars). Maximum is 500,000 chars."

        file_path.write_text(content, encoding="utf-8")

        return f"✓ Wrote {len(content):,} chars to {path}"

    except PermissionError as e:
        return f"⚠️ Access denied: {e}"
    except Exception as e:
        return f"⚠️ Error writing file: {e}"


def list_extended_sandbox_paths(ctx: ToolContext) -> str:
    """
    List all configured extended sandbox paths.

    Returns:
        Formatted list of allowed paths
    """
    try:
        paths = ctx.list_extended_paths()

        lines = ["## Extended Sandbox Paths\n"]

        if paths['read_only']:
            lines.append("### Read-Only Access:")
            for p in paths['read_only']:
                lines.append(f"  📖 {p}")
        else:
            lines.append("### Read-Only Access: (none configured)")

        if paths['read_write']:
            lines.append("\n### Read-Write Access:")
            for p in paths['read_write']:
                lines.append(f"  ✏️ {p}")
        else:
            lines.append("\n### Read-Write Access: (none configured)")

        if not paths['read_only'] and not paths['read_write']:
            lines.append("\n_To add extended paths, edit ~/.dpc/privacy_rules.json:_")
            lines.append("```json")
            lines.append('"dpc_agent": {')
            lines.append('  "sandbox_extensions": {')
            lines.append('    "read_only": ["C:\\\\Users\\\\you\\\\Documents\\\\notes"],')
            lines.append('    "read_write": ["C:\\\\Users\\\\you\\\\projects\\\\myapp"]')
            lines.append('  }')
            lines.append('}')
            lines.append("```")

        return "\n".join(lines)

    except Exception as e:
        return f"⚠️ Error listing paths: {e}"


# ---------------------------------------------------------------------------
# Search Tools (v0.16.0+)
# ---------------------------------------------------------------------------

import re
import subprocess


def search_files(ctx: ToolContext, pattern: str, path: str = "", max_results: int = 50, include_pattern: str = "*") -> str:
    """
    Search for a text pattern in files (grep-like functionality).

    Searches within the agent sandbox by default, or in extended sandbox paths
    if configured and allowed.

    Args:
        ctx: Tool context
        pattern: Regex pattern to search for
        path: Directory path to search (relative to sandbox, or absolute for extended)
        max_results: Maximum number of matches to return (default 50)
        include_pattern: Glob pattern for files to include (default "*" for all files)

    Returns:
        Search results with file:line:content format
    """
    # Memory limits to prevent OOM issues
    MAX_FILE_SIZE = 1024 * 1024  # 1MB per file
    MAX_TOTAL_BYTES = 10 * 1024 * 1024  # 10MB total

    try:
        # Determine search directory
        if path and (path.startswith("/") or path.startswith("C:") or path.startswith("~")):
            # Absolute path - check extended sandbox
            search_dir = ctx.validate_extended_path(path, require_write=False)
        else:
            # Relative path - use sandbox
            search_dir = ctx.repo_path(path) if path else ctx.agent_root

        if not search_dir.exists():
            return f"⚠️ Directory not found: {path or 'sandbox root'}"

        if not search_dir.is_dir():
            return f"⚠️ Not a directory: {path}"

        # Compile regex pattern
        try:
            regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            return f"⚠️ Invalid regex pattern: {e}"

        matches = []
        files_searched = 0
        files_with_matches = 0
        total_bytes_read = 0
        skipped_large_files = 0

        # Search files
        for file_path in search_dir.rglob(include_pattern):
            if not file_path.is_file():
                continue

            # Skip binary files and common non-text files
            if file_path.suffix.lower() in {'.pyc', '.pyo', '.exe', '.dll', '.so', '.dylib',
                                              '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico',
                                              '.mp3', '.mp4', '.wav', '.avi', '.mkv', '.webm',
                                              '.zip', '.tar', '.gz', '.rar', '.7z', '.pdf',
                                              '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                                              '.db', '.sqlite', '.sqlite3'}:
                continue

            # Check file size to avoid reading huge files
            try:
                file_size = file_path.stat().st_size
                if file_size > MAX_FILE_SIZE:
                    skipped_large_files += 1
                    continue
                if total_bytes_read + file_size > MAX_TOTAL_BYTES:
                    # Stop if we've read too much data
                    break
            except OSError:
                continue

            files_searched += 1

            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                total_bytes_read += len(content)
                rel_path = file_path.relative_to(search_dir)

                for line_num, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        # Truncate long lines
                        display_line = line.strip()[:300]
                        if len(line.strip()) > 300:
                            display_line += "..."

                        matches.append(f"{rel_path}:{line_num}: {display_line}")
                        files_with_matches = files_with_matches if matches else 1
                        if len(matches) >= max_results:
                            break

                # Clear content from memory after processing each file
                del content

                if len(matches) >= max_results:
                    break

            except Exception:
                continue

        if not matches:
            msg = f"No matches found for pattern '{pattern}' in {files_searched} files"
            if skipped_large_files > 0:
                msg += f" ({skipped_large_files} large files skipped)"
            return msg

        result = [f"## Search Results for '{pattern}'"]
        result.append(f"Found {len(matches)} matches in {files_with_matches} files (searched {files_searched} files)")
        if skipped_large_files > 0:
            result.append(f"_Note: {skipped_large_files} files >1MB were skipped to conserve memory_")
        result.append("")

        # Group by file for better readability
        current_file = None
        for match in matches:
            file_name = match.split(":")[0]
            if file_name != current_file:
                result.append(f"\n### {file_name}")
                current_file = file_name
            result.append(f"  {match.split(':', 1)[1]}")

        if len(matches) >= max_results:
            result.append(f"\n_... truncated at {max_results} results_")

        return "\n".join(result)

    except PermissionError as e:
        return f"⚠️ Access denied: {e}"
    except Exception as e:
        return f"⚠️ Search error: {e}"


def search_in_file(ctx: ToolContext, pattern: str, file_path: str, context_lines: int = 2) -> str:
    """
    Search for a pattern in a specific file with context.

    Args:
        ctx: Tool context
        pattern: Regex pattern to search for
        file_path: Path to the file (relative to sandbox, or absolute for extended)
        context_lines: Number of lines to show before/after each match

    Returns:
        Matches with surrounding context
    """
    try:
        # Determine file path
        if file_path.startswith("/") or file_path.startswith("C:") or file_path.startswith("~"):
            # Absolute path - check extended sandbox
            full_path = ctx.validate_extended_path(file_path, require_write=False)
        else:
            # Relative path - use sandbox
            full_path = ctx.repo_path(file_path)

        if not full_path.exists():
            return f"⚠️ File not found: {file_path}"

        if not full_path.is_file():
            return f"⚠️ Not a file: {file_path}"

        # Compile regex
        try:
            regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            return f"⚠️ Invalid regex pattern: {e}"

        content = full_path.read_text(encoding="utf-8", errors="ignore")
        lines = content.splitlines()

        matches = []
        for i, line in enumerate(lines):
            if regex.search(line):
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)

                match_block = [f"\n### Line {i + 1}"]
                for j in range(start, end):
                    prefix = ">>>" if j == i else "   "
                    line_text = lines[j][:300]
                    if len(lines[j]) > 300:
                        line_text += "..."
                    matches.append(f"{prefix} {j + 1:4d}: {line_text}")

        if not matches:
            return f"No matches found for pattern '{pattern}' in {file_path}"

        return f"## Search in {file_path}\n" + "\n".join(matches)

    except PermissionError as e:
        return f"⚠️ Access denied: {e}"
    except Exception as e:
        return f"⚠️ Search error: {e}"


# ---------------------------------------------------------------------------
# Knowledge Extraction (Session Management)
# ---------------------------------------------------------------------------

def extract_knowledge(ctx: ToolContext, topic: Optional[str] = None, force: bool = False) -> str:
    """
    Extract knowledge from the current conversation and save to knowledge base.

    This tool uses the existing ConversationMonitor's generate_commit_proposal()
    method to extract structured knowledge from the conversation history.

    Args:
        ctx: Tool context
        topic: Optional topic to focus extraction on
        force: Extract even if knowledge threshold not met

    Returns:
        Summary of extracted knowledge or error message
    """
    try:
        # Get ConversationMonitor from context
        monitor = getattr(ctx, 'conversation_monitor', None)
        if not monitor:
            return "⚠️ No conversation monitor available for this session"

        # Check message count
        if len(monitor.message_history) < 2:
            return "⚠️ Not enough messages in conversation to extract knowledge (need at least 2)"

        # Run async extraction in sync context
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're in an async context, but this is a sync tool
            # Use asyncio.run_coroutine_threadsafe or return a message
            return "⚠️ Knowledge extraction requires async context. Use force=True to attempt extraction."

        # Run extraction on the main event loop via run_coroutine_threadsafe so that:
        # - asyncio objects (HTTP clients, etc.) bound to the main loop work correctly
        # - we avoid creating a second event loop in the thread (asyncio.run would
        #   do that and cause cross-loop issues with ai_query_func)
        # - the timeout below propagates a clear error instead of a generic TOOL_TIMEOUT
        main_loop = getattr(ctx, 'agent_event_loop', None)
        if main_loop and main_loop.is_running():
            try:
                agent_id = getattr(ctx, 'firewall_profile', None) or "agent_tool"
                initiated_by = "telegram" if getattr(ctx, 'reply_telegram_chat_id', None) else "agent_tool"
                future = asyncio.run_coroutine_threadsafe(
                    monitor.generate_commit_proposal(
                        force=force,
                        proposed_by=agent_id,
                        initiated_by=initiated_by,
                    ),
                    main_loop,
                )
                # Use 290s so we can return a meaningful error before the 300s TOOL_TIMEOUT fires
                proposal = future.result(timeout=290)
            except TimeoutError:
                return (
                    "⚠️ Knowledge extraction timed out (>290s). The conversation history is very long — "
                    "try calling end_conversation_session instead, or split into shorter sessions."
                )
        else:
            # Fallback when event loop reference is unavailable (should not happen normally)
            agent_id = getattr(ctx, 'firewall_profile', None) or "agent_tool"
            initiated_by = "telegram" if getattr(ctx, 'reply_telegram_chat_id', None) else "agent_tool"
            proposal = asyncio.run(monitor.generate_commit_proposal(
                force=force, proposed_by=agent_id, initiated_by=initiated_by
            ))

        if proposal == "EXTRACTION_IN_PROGRESS":
            return "⚠️ Knowledge extraction already in progress — wait for it to finish and retry."
        if not proposal:
            return "⚠️ No knowledge extracted — conversation buffer is empty or below threshold. Try force=True."

        if not proposal.entries:
            return (
                "⚠️ Extraction ran but the LLM returned 0 knowledge entries. "
                "The conversation may not contain extractable facts, or the model needs more context. "
                f"Topic detected: {proposal.topic!r}. Summary: {proposal.summary[:200]}"
            )

        # Save to agent's local knowledge directory for reference
        knowledge_dir = ctx.agent_root / "knowledge"
        knowledge_dir.mkdir(parents=True, exist_ok=True)

        safe_topic = "".join(c if c.isalnum() or c in " _-" else "_" for c in (topic or proposal.topic or "knowledge"))
        safe_topic = safe_topic[:50].strip() or "knowledge"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{safe_topic}_{timestamp}.json"
        filepath = knowledge_dir / filename

        knowledge_entry = {
            "topic": proposal.topic,
            "summary": proposal.summary,
            "entries": [
                {
                    "content": entry.content if hasattr(entry, 'content') else str(entry),
                    "tags": entry.tags if hasattr(entry, 'tags') else [],
                    "confidence": entry.confidence if hasattr(entry, 'confidence') else 1.0,
                }
                for entry in (proposal.entries or [])
            ],
            "conversation_id": proposal.conversation_id,
            "participants": proposal.participants if hasattr(proposal, 'participants') else [],
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "extraction_method": "agent_tool",
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(knowledge_entry, f, indent=2, ensure_ascii=False)

        log.info(f"Extracted knowledge saved to {filepath}")

        # Agent's extract_knowledge saves to agent's OWN knowledge directory only.
        # It does NOT trigger Human's voting/consensus workflow — Human owns their
        # knowledge store (~/.dpc/knowledge/) and extraction is Human-initiated only
        # (End Session button). Agent extraction previously consumed the conversation
        # monitor's message_buffer, causing Human's End Session to find 0 entries.
        return (
            f"✅ Knowledge extracted and saved to agent knowledge store.\n\n"
            f"**Topic:** {proposal.topic}\n"
            f"**Summary:** {proposal.summary[:200]}...\n"
            f"**Entries:** {len(proposal.entries)}\n"
            f"**Saved to:** knowledge/{filename}\n"
            f"**Proposal ID:** {proposal.proposal_id}\n\n"
            f"Call `get_proposal_result(\"{proposal.proposal_id}\")` after voting completes to get the commit_id for git linking."
        )

    except Exception as e:
        log.error(f"Knowledge extraction error: {e}", exc_info=True)
        return f"⚠️ Knowledge extraction error: {e}"


# ---------------------------------------------------------------------------
# Knowledge Proposal Result (store-and-poll)
# ---------------------------------------------------------------------------

def get_proposal_result(ctx: ToolContext, proposal_id: str) -> str:
    """
    Poll for the result of a knowledge commit proposal.

    Returns the commit_id once voting is complete (approved or rejected).
    Call this after extract_knowledge to retrieve the commit_id for git linking.

    Args:
        ctx: Tool context
        proposal_id: The proposal_id returned by extract_knowledge

    Returns:
        JSON string with status, commit_id (if approved), topic, and markdown_file.
    """
    service = getattr(ctx, 'dpc_service', None)
    if not service:
        return json.dumps({"status": "error", "message": "DPC service unavailable"})

    knowledge_service = getattr(service, 'knowledge_service', None)
    if not knowledge_service:
        return json.dumps({"status": "error", "message": "Knowledge service unavailable"})

    result = knowledge_service.pending_results.get(proposal_id)
    if result is None:
        return json.dumps({"status": "pending", "proposal_id": proposal_id,
                           "message": "Voting in progress — call again after participants vote"})

    return json.dumps(result)


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
                "description": "Read a file from the agent sandbox directory. Supports line-based pagination via offset/limit for large files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to agent root"
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Start line (0-based). Enables pagination when provided."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of lines to return. Use with offset for pagination."
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
                "description": "Read a file from the agent sandbox (alias for repo_read). Supports line-based pagination via offset/limit.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to agent root"
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Start line (0-based). Enables pagination when provided."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of lines to return. Use with offset for pagination."
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
            name="repo_delete",
            schema={
                "name": "repo_delete",
                "description": "Delete a file or directory from the agent sandbox. Use recursive=true for directories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to agent root to delete"
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "Delete directory and all contents (required for directories)",
                            "default": False
                        }
                    },
                    "required": ["path"]
                }
            },
            handler=repo_delete,
            timeout_sec=30,
        ),

        ToolEntry(
            name="repo_write_commit",
            schema={
                "name": "repo_write_commit",
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
                "description": "Update the agent's working memory (scratchpad). Use mode='deduplicate' to clean up duplicate content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Content to add to scratchpad (not required for deduplicate mode)"
                        },
                        "mode": {
                            "type": "string",
                            "description": "Update mode",
                            "enum": ["append", "prepend", "replace", "deduplicate"],
                            "default": "append"
                        }
                    },
                    "required": []
                }
            },
            handler=update_scratchpad,
            timeout_sec=10,
        ),

        ToolEntry(
            name="update_identity",
            schema={
                "name": "update_identity",
                "description": (
                    "Update a section of the agent's identity (self-understanding). "
                    "Automatically removes duplicate sections. "
                    "Use mode='deduplicate' to just clean up duplicates without adding content. "
                    "Provide commit_message in Conventional Commits format to describe why you made this change "
                    "(e.g. 'chore(identity): refine core values after security audit'). "
                    "You own this commit message — write it to reflect what actually changed and why."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "description": "Section name (e.g., 'values', 'goals', 'beliefs', 'Operational Protocols')"
                        },
                        "content": {
                            "type": "string",
                            "description": "Section content"
                        },
                        "mode": {
                            "type": "string",
                            "description": "Update mode",
                            "enum": ["replace", "append", "merge", "deduplicate"],
                            "default": "replace"
                        },
                        "commit_message": {
                            "type": "string",
                            "description": (
                                "Conventional Commits message for this change: type(scope): description. "
                                "E.g. 'chore(identity): refine core values after coevolution task'. "
                                "Omit to use the default."
                            )
                        }
                    },
                    "required": ["section"]
                }
            },
            handler=update_identity,
            timeout_sec=10,
        ),

        ToolEntry(
            name="deduplicate_identity",
            schema={
                "name": "deduplicate_identity",
                "description": "Remove duplicate sections from the agent's identity file. Use this to clean up when identity.md has grown too large with repeated content.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=deduplicate_identity,
            timeout_sec=10,
        ),

        ToolEntry(
            name="chat_history",
            schema={
                "name": "chat_history",
                "description": "Read recent conversation messages (user and assistant turns) from the current session's history",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of entries",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100
                        },
                        "include_internals": {
                            "type": "boolean",
                            "description": "Include thinking and streaming_raw blocks in output",
                            "default": False
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
                "description": (
                    "Write or update a knowledge base topic. "
                    "Provide commit_message in Conventional Commits format to describe what you learned "
                    "(e.g. 'docs(knowledge): add TurboQuant complexity analysis with benchmarks'). "
                    "You own this commit message — make it meaningful."
                ),
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
                        },
                        "commit_message": {
                            "type": "string",
                            "description": (
                                "Conventional Commits message for this change: type(scope): description. "
                                "E.g. 'docs(knowledge): add TurboQuant complexity analysis'. "
                                "Omit to use the default."
                            )
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

        ToolEntry(
            name="get_task_board",
            schema={
                "name": "get_task_board",
                "description": (
                    "Read your Agent Progress Board — task history and learning progress "
                    "visible to both you and the user in the DPC desktop UI. "
                    "Use at the start of a learning session to review where you left off, "
                    "or before scheduling a task to see what is already queued. "
                    "Also documents the required format for updating your learning progress "
                    "in knowledge/llm_learning_schedule.md."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=get_task_board,
            timeout_sec=15,
        ),

        # Knowledge extraction and result polling
        ToolEntry(
            name="get_proposal_result",
            schema={
                "name": "get_proposal_result",
                "description": "Poll for the result of a knowledge commit proposal. Call this after extract_knowledge to get the commit_id once voting completes. Returns status (pending/approved/rejected) and commit_id for git linking.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "proposal_id": {
                            "type": "string",
                            "description": "The proposal_id returned by extract_knowledge (format: proposal-XXXXXXXX)"
                        }
                    },
                    "required": ["proposal_id"]
                }
            },
            handler=get_proposal_result,
            timeout_sec=10,
        ),

        ToolEntry(
            name="extract_knowledge",
            schema={
                "name": "extract_knowledge",
                "description": "Extract knowledge from the current conversation and save to knowledge base. Use this when the conversation contains valuable insights worth preserving.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "Optional topic to focus extraction on"
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Extract even if knowledge threshold not met",
                            "default": False
                        }
                    },
                    "required": []
                }
            },
            handler=extract_knowledge,
            timeout_sec=300,  # GLM-4.7 extended thinking can take 2-5 min
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

        # Task queue tools
        ToolEntry(
            name="schedule_task",
            schema={
                "name": "schedule_task",
                "description": "Schedule a task for future or background execution. For custom tasks: 1) First call register_task_type to define execution instructions, 2) Then call schedule_task. For 'chat' tasks: task_data must include 'text' field with the message to process. For reminders: use task_type='reminder' with task_data={\"message\": \"...\"} — this sends the message directly WITHOUT going through the LLM, preventing accidental re-scheduling loops.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_type": {
                            "type": "string",
                            "description": "Type of task. Built-in: 'chat' (LLM conversation), 'reminder' (direct message delivery, no LLM — use for notifications/alerts), 'improvement' (self-improvement), 'review' (code review). Custom: any type registered via register_task_type."
                        },
                        "task_data": {
                            "type": "string",
                            "description": "JSON string with task payload. For 'chat' tasks: {\"text\": \"your message here\"}. For 'reminder' tasks: {\"message\": \"reminder text\"}. For custom types: match the input_schema defined in register_task_type."
                        },
                        "delay_seconds": {
                            "type": "integer",
                            "description": "Delay before execution in seconds",
                            "default": 0,
                            "minimum": 0
                        },
                        "priority": {
                            "type": "string",
                            "description": "Task priority",
                            "enum": ["critical", "high", "normal", "low"],
                            "default": "normal"
                        }
                    },
                    "required": ["task_type", "task_data"]
                }
            },
            handler=schedule_task,
            timeout_sec=10,
        ),

        ToolEntry(
            name="get_task_status",
            schema={
                "name": "get_task_status",
                "description": "Get status of a scheduled task",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "Task ID to check"
                        }
                    },
                    "required": ["task_id"]
                }
            },
            handler=get_task_status,
            timeout_sec=10,
        ),

        # Task type management tools
        ToolEntry(
            name="register_task_type",
            schema={
                "name": "register_task_type",
                "description": "Register a custom task type with execution instructions. The agent will follow the execution_prompt when tasks of this type run. MUST call this BEFORE scheduling tasks of a custom type.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_type": {
                            "type": "string",
                            "description": "Unique identifier (e.g., 'weather_report'). Use snake_case."
                        },
                        "description": {
                            "type": "string",
                            "description": "What this task type does (human-readable)"
                        },
                        "execution_prompt": {
                            "type": "string",
                            "description": "Instructions for the agent to follow. Use {variable} placeholders that will be filled from task.data"
                        },
                        "input_schema": {
                            "type": "string",
                            "description": "JSON schema for validating task.data (optional, as JSON string)",
                            "default": "{}"
                        }
                    },
                    "required": ["task_type", "description", "execution_prompt"]
                }
            },
            handler=register_task_type,
            timeout_sec=10,
        ),

        ToolEntry(
            name="list_task_types",
            schema={
                "name": "list_task_types",
                "description": "List all registered task types (custom and built-in)",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=list_task_types,
            timeout_sec=5,
        ),

        ToolEntry(
            name="unregister_task_type",
            schema={
                "name": "unregister_task_type",
                "description": "Unregister a custom task type",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_type": {
                            "type": "string",
                            "description": "Task type to unregister"
                        }
                    },
                    "required": ["task_type"]
                }
            },
            handler=unregister_task_type,
            timeout_sec=5,
        ),

        # Evolution tools
        ToolEntry(
            name="pause_evolution",
            schema={
                "name": "pause_evolution",
                "description": "Pause automatic evolution cycles",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=pause_evolution,
            timeout_sec=5,
        ),

        ToolEntry(
            name="resume_evolution",
            schema={
                "name": "resume_evolution",
                "description": "Resume automatic evolution cycles",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=resume_evolution,
            timeout_sec=5,
        ),

        ToolEntry(
            name="get_evolution_stats",
            schema={
                "name": "get_evolution_stats",
                "description": "Get evolution statistics and pending changes",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=get_evolution_stats,
            timeout_sec=10,
        ),

        ToolEntry(
            name="approve_evolution_change",
            schema={
                "name": "approve_evolution_change",
                "description": "Approve a pending evolution change",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "change_id": {
                            "type": "string",
                            "description": "ID of the change to approve"
                        }
                    },
                    "required": ["change_id"]
                }
            },
            handler=approve_evolution_change,
            timeout_sec=10,
            is_code_tool=True,
        ),

        ToolEntry(
            name="reject_evolution_change",
            schema={
                "name": "reject_evolution_change",
                "description": "Reject a pending evolution change",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "change_id": {
                            "type": "string",
                            "description": "ID of the change to reject"
                        }
                    },
                    "required": ["change_id"]
                }
            },
            handler=reject_evolution_change,
            timeout_sec=10,
        ),

        # Extended sandbox tools (v0.16.0+)
        ToolEntry(
            name="extended_path_read",
            schema={
                "name": "extended_path_read",
                "description": "Read a file from an extended sandbox path (outside ~/.dpc/agent/). Requires sandbox_extensions configuration. Supports line-based pagination via offset/limit for large files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute file path (must be in configured extended sandbox)"
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Start line (0-based). Enables pagination when provided."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of lines to return. Use with offset for pagination."
                        }
                    },
                    "required": ["path"]
                }
            },
            handler=extended_path_read,
            timeout_sec=30,
        ),

        ToolEntry(
            name="extended_path_list",
            schema={
                "name": "extended_path_list",
                "description": "List files in an extended sandbox directory",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute directory path"
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "List recursively",
                            "default": False
                        }
                    },
                    "required": ["path"]
                }
            },
            handler=extended_path_list,
            timeout_sec=30,
        ),

        ToolEntry(
            name="extended_path_write",
            schema={
                "name": "extended_path_write",
                "description": "Write a file to an extended sandbox path. Requires read_write access in sandbox_extensions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute file path"
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write"
                        }
                    },
                    "required": ["path", "content"]
                }
            },
            handler=extended_path_write,
            timeout_sec=30,
            is_code_tool=True,
        ),

        ToolEntry(
            name="list_extended_sandbox_paths",
            schema={
                "name": "list_extended_sandbox_paths",
                "description": "List all configured extended sandbox paths (paths outside ~/.dpc/agent/ that the agent can access)",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=list_extended_sandbox_paths,
            timeout_sec=5,
        ),

        # Search tools (v0.16.0+)
        ToolEntry(
            name="search_files",
            schema={
                "name": "search_files",
                "description": "Search for a text/regex pattern in files (grep-like). Searches sandbox by default, or extended paths if absolute path given.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Regex pattern to search for"
                        },
                        "path": {
                            "type": "string",
                            "description": "Directory to search (relative to sandbox, or absolute for extended)",
                            "default": ""
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum matches to return",
                            "default": 50,
                            "minimum": 1,
                            "maximum": 200
                        },
                        "include_pattern": {
                            "type": "string",
                            "description": "Glob pattern for files (e.g., '*.py', '*.md')",
                            "default": "*"
                        }
                    },
                    "required": ["pattern"]
                }
            },
            handler=search_files,
            timeout_sec=60,
        ),

        ToolEntry(
            name="search_in_file",
            schema={
                "name": "search_in_file",
                "description": "Search for a pattern in a specific file with surrounding context",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Regex pattern to search for"
                        },
                        "file_path": {
                            "type": "string",
                            "description": "File path (relative to sandbox, or absolute for extended)"
                        },
                        "context_lines": {
                            "type": "integer",
                            "description": "Lines to show before/after match",
                            "default": 2,
                            "minimum": 0,
                            "maximum": 10
                        }
                    },
                    "required": ["pattern", "file_path"]
                }
            },
            handler=search_in_file,
            timeout_sec=30,
        ),
    ]
