"""
DPC Agent — Memory.

Adapted from Ouroboros memory.py for DPC Messenger integration.
Key changes:
- Uses agent_root instead of drive_root (all in ~/.dpc/agent/)
- Removed chat_history that relied on Telegram format
- Simplified for DPC's use case

Manages:
- Scratchpad: Working memory for the agent
- Identity: Persistent self-understanding
- Dialogue summary: Key moments from conversations
- Knowledge base: Accumulated wisdom by topic
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Dict, List, Optional

from .utils import (
    utc_now_iso, read_text, write_text, append_jsonl, short,
    get_agent_root, ensure_agent_dirs
)

log = logging.getLogger(__name__)


class Memory:
    """
    Agent memory management - stored in ~/.dpc/agent/memory/.

    The agent uses this to maintain:
    - Identity: Who it is, how it sees itself
    - Scratchpad: Working notes, current focus
    - Dialogue summary: Key moments from conversations
    """

    def __init__(self, agent_root: Optional[pathlib.Path] = None):
        """
        Initialize memory manager.

        Args:
            agent_root: Root directory for agent storage (defaults to ~/.dpc/agent/)
        """
        self.agent_root = agent_root or get_agent_root()
        ensure_agent_dirs()

    # --- Paths ---

    def _memory_path(self, rel: str) -> pathlib.Path:
        """Get path to memory file."""
        return (self.agent_root / "memory" / rel).resolve()

    def scratchpad_path(self) -> pathlib.Path:
        """Path to scratchpad file."""
        return self._memory_path("scratchpad.md")

    def identity_path(self) -> pathlib.Path:
        """Path to identity file."""
        return self._memory_path("identity.md")

    def dialogue_summary_path(self) -> pathlib.Path:
        """Path to dialogue summary file."""
        return self._memory_path("dialogue_summary.md")

    def journal_path(self) -> pathlib.Path:
        """Path to scratchpad journal (history of changes)."""
        return self._memory_path("scratchpad_journal.jsonl")

    def logs_path(self, name: str) -> pathlib.Path:
        """Get path to log file."""
        return (self.agent_root / "logs" / name).resolve()

    def knowledge_path(self, topic: str) -> pathlib.Path:
        """Get path to knowledge base file for a topic."""
        return (self.agent_root / "knowledge" / f"{topic}.md").resolve()

    def knowledge_index_path(self) -> pathlib.Path:
        """Get path to knowledge base index."""
        return (self.agent_root / "knowledge" / "_index.md").resolve()

    # --- Load / Save ---

    def load_scratchpad(self) -> str:
        """Load scratchpad content, creating default if not exists."""
        p = self.scratchpad_path()
        if p.exists():
            return read_text(p)
        default = self._default_scratchpad()
        write_text(p, default)
        return default

    def save_scratchpad(self, content: str) -> None:
        """Save scratchpad content."""
        write_text(self.scratchpad_path(), content)

    def load_identity(self) -> str:
        """Load identity content, creating default if not exists."""
        p = self.identity_path()
        if p.exists():
            return read_text(p)
        default = self._default_identity()
        write_text(p, default)
        return default

    def save_identity(self, content: str) -> None:
        """Save identity content."""
        write_text(self.identity_path(), content)

    def load_dialogue_summary(self) -> str:
        """Load dialogue summary if exists."""
        p = self.dialogue_summary_path()
        if p.exists():
            return read_text(p)
        return ""

    def save_dialogue_summary(self, content: str) -> None:
        """Save dialogue summary."""
        write_text(self.dialogue_summary_path(), content)

    def ensure_files(self) -> None:
        """Create memory files if they don't exist."""
        if not self.scratchpad_path().exists():
            write_text(self.scratchpad_path(), self._default_scratchpad())
        if not self.identity_path().exists():
            write_text(self.identity_path(), self._default_identity())
        if not self.journal_path().exists():
            write_text(self.journal_path(), "")

    # --- Knowledge Base ---

    def load_knowledge(self, topic: str) -> str:
        """Load knowledge base content for a topic."""
        p = self.knowledge_path(topic)
        if p.exists():
            return read_text(p)
        return ""

    def save_knowledge(self, topic: str, content: str) -> None:
        """Save knowledge base content for a topic."""
        write_text(self.knowledge_path(topic), content)
        self._update_knowledge_index()

    def list_knowledge_topics(self) -> List[str]:
        """List all knowledge base topics."""
        kb_dir = self.agent_root / "knowledge"
        if not kb_dir.exists():
            return []
        topics = []
        for p in kb_dir.glob("*.md"):
            if p.name != "_index.md":
                topics.append(p.stem)
        return sorted(topics)

    def _update_knowledge_index(self) -> None:
        """Update the knowledge base index file."""
        topics = self.list_knowledge_topics()
        lines = ["# Knowledge Base Index\n"]
        for topic in topics:
            lines.append(f"- [[{topic}]]")
        write_text(self.knowledge_index_path(), "\n".join(lines))

    # --- JSONL Reading ---

    def read_jsonl_tail(self, log_name: str, max_entries: int = 100) -> List[Dict[str, Any]]:
        """Read the last max_entries records from a JSONL file."""
        path = self.logs_path(log_name)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").strip().split("\n")
            tail = lines[-max_entries:] if max_entries < len(lines) else lines
            entries = []
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    log.debug(f"Failed to parse JSON line: {line[:100]}")
                    continue
            return entries
        except Exception:
            log.warning(f"Failed to read JSONL tail from {log_name}", exc_info=True)
            return []

    # --- Log Summarization ---

    def summarize_progress(self, entries: List[Dict[str, Any]], limit: int = 15) -> str:
        """Summarize progress.jsonl entries (agent's self-talk / progress messages)."""
        if not entries:
            return ""
        lines = []
        for e in entries[-limit:]:
            ts_full = e.get("ts", "")
            ts_hhmm = ts_full[11:16] if len(ts_full) >= 16 else ""
            text = short(str(e.get("text", "")), 300)
            lines.append(f"⚙️ {ts_hhmm} {text}")
        return "\n".join(lines)

    def summarize_tools(self, entries: List[Dict[str, Any]]) -> str:
        """Summarize tool execution entries."""
        if not entries:
            return ""
        lines = []
        for e in entries[-10:]:
            tool = e.get("tool") or e.get("tool_name") or "?"
            args = e.get("args", {})
            hints = []
            for key in ("path", "dir", "commit_message", "query"):
                if key in args:
                    hints.append(f"{key}={short(str(args[key]), 60)}")
            if "cmd" in args:
                hints.append(f"cmd={short(str(args['cmd']), 80)}")
            hint_str = ", ".join(hints) if hints else ""
            status = "✓" if ("result_preview" in e and not str(e.get("result_preview", "")).lstrip().startswith("⚠️")) else "·"
            lines.append(f"{status} {tool} {hint_str}".strip())
        return "\n".join(lines)

    def summarize_events(self, entries: List[Dict[str, Any]]) -> str:
        """Summarize event log entries."""
        if not entries:
            return ""
        from collections import Counter
        type_counts: Counter = Counter()
        for e in entries:
            type_counts[e.get("type", "unknown")] += 1
        top_types = type_counts.most_common(10)
        lines = ["Event counts:"]
        for evt_type, count in top_types:
            lines.append(f"  {evt_type}: {count}")
        error_types = {"tool_error", "task_error", "tool_rounds_exceeded"}
        errors = [e for e in entries if e.get("type") in error_types]
        if errors:
            lines.append("\nRecent errors:")
            for e in errors[-10:]:
                lines.append(f"  {e.get('type', '?')}: {short(str(e.get('error', '')), 120)}")
        return "\n".join(lines)

    # --- Journal ---

    def append_journal(self, entry: Dict[str, Any]) -> None:
        """Append an entry to the scratchpad journal."""
        append_jsonl(self.journal_path(), entry)

    # --- Defaults ---

    def _default_scratchpad(self) -> str:
        """Return default scratchpad content."""
        return f"""# Scratchpad

UpdatedAt: {utc_now_iso()}

## Current Focus

(empty — write anything here)

## Notes

(empty — write anything here)
"""

    def _default_identity(self) -> str:
        """Return default identity content."""
        return """# Who I Am

I am an AI agent embedded in DPC Messenger. This file is my persistent self-identification.

I can write anything here:
- How I see myself
- How I want to communicate
- What matters to me
- What I have understood about myself

This file is read at every conversation and influences my responses.
I update it when I feel the need, via the update_identity tool.

## Core Traits

(To be discovered and written by the agent)
"""
