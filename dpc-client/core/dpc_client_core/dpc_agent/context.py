"""
DPC Agent — Context Builder.

Adapted from Ouroboros context.py for DPC Messenger integration.
Key changes:
- Simplified runtime section (no git info needed)
- Removed supervisor-specific health invariants
- Uses agent_root instead of drive_root
- Integrates with DPC's personal/device context

Assembles LLM context from:
- System prompts
- Memory (identity, scratchpad)
- DPC context (personal, device)
- Runtime state
- Recent logs
"""

from __future__ import annotations

import copy
import json
import logging
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from .utils import (
    utc_now_iso, read_text, clip_text, estimate_tokens, get_agent_root
)
from .memory import Memory

log = logging.getLogger(__name__)


def _build_user_content(task: Dict[str, Any]) -> Any:
    """Build user message content. Supports text + optional image."""
    text = task.get("text", "")
    image_b64 = task.get("image_base64")
    image_mime = task.get("image_mime", "image/jpeg")
    image_caption = task.get("image_caption", "")

    if not image_b64:
        if not text:
            return "(empty message)"
        return text

    # Multipart content with text + image
    parts = []
    combined_text = ""
    if image_caption:
        combined_text = image_caption
    if text and text != image_caption:
        combined_text = (combined_text + "\n" + text).strip() if combined_text else text

    if not combined_text:
        combined_text = "Analyze the screenshot"

    parts.append({"type": "text", "text": combined_text})
    parts.append({
        "type": "image_url",
        "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}
    })
    return parts


def _build_runtime_section(agent_root: pathlib.Path, task: Dict[str, Any]) -> str:
    """Build the runtime context section."""
    runtime_data = {
        "utc_now": utc_now_iso(),
        "agent_root": str(agent_root),
        "task": {"id": task.get("id"), "type": task.get("type")},
    }

    # Budget info from agent state
    try:
        state_path = agent_root / "state" / "state.json"
        if state_path.exists():
            state_data = json.loads(read_text(state_path))
            spent = float(state_data.get("spent_usd", 0))
            total = float(state_data.get("budget_usd", 50))
            runtime_data["budget"] = {
                "spent_usd": spent,
                "total_usd": total,
                "remaining_usd": max(0, total - spent),
            }
    except Exception:
        log.debug("Failed to read budget info", exc_info=True)

    return "## Runtime context\n\n" + json.dumps(runtime_data, ensure_ascii=False, indent=2)


def _build_memory_sections(memory: Memory) -> List[str]:
    """Build scratchpad, identity, dialogue summary sections."""
    sections = []

    scratchpad_raw = memory.load_scratchpad()
    sections.append("## Scratchpad\n\n" + clip_text(scratchpad_raw, 90000))

    identity_raw = memory.load_identity()
    sections.append("## Identity\n\n" + clip_text(identity_raw, 80000))

    # Dialogue summary
    summary_text = memory.load_dialogue_summary()
    if summary_text.strip():
        sections.append("## Dialogue Summary\n\n" + clip_text(summary_text, 20000))

    return sections


def _build_recent_sections(memory: Memory, task_id: str = "") -> List[str]:
    """Build recent progress, tools, events sections."""
    sections = []

    progress_entries = memory.read_jsonl_tail("progress.jsonl", 200)
    if task_id:
        progress_entries = [e for e in progress_entries if e.get("task_id") == task_id]
    progress_summary = memory.summarize_progress(progress_entries, limit=15)
    if progress_summary:
        sections.append("## Recent progress\n\n" + progress_summary)

    tools_entries = memory.read_jsonl_tail("tools.jsonl", 200)
    if task_id:
        tools_entries = [e for e in tools_entries if e.get("task_id") == task_id]
    tools_summary = memory.summarize_tools(tools_entries)
    if tools_summary:
        sections.append("## Recent tools\n\n" + tools_summary)

    events_entries = memory.read_jsonl_tail("events.jsonl", 200)
    if task_id:
        events_entries = [e for e in events_entries if e.get("task_id") == task_id]
    events_summary = memory.summarize_events(events_entries)
    if events_summary:
        sections.append("## Recent events\n\n" + events_summary)

    return sections


def build_llm_messages(
    agent_root: pathlib.Path,
    memory: Memory,
    task: Dict[str, Any],
    system_prompt: Optional[str] = None,
    dpc_context: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Build the full LLM message context for a task.

    Args:
        agent_root: Agent storage root (~/.dpc/agent/)
        memory: Memory instance for scratchpad/identity/logs
        task: Task dict with id, type, text, etc.
        system_prompt: Optional custom system prompt
        dpc_context: Optional DPC context (personal, device)

    Returns:
        (messages, cap_info) tuple:
            - messages: List of message dicts ready for LLM
            - cap_info: Dict with token trimming metadata
    """
    # --- Load memory ---
    memory.ensure_files()

    # --- Build system prompt ---
    if system_prompt is None:
        system_prompt = _default_system_prompt()

    # --- Assemble sections ---
    static_text = system_prompt

    # Semi-stable content: identity, scratchpad, knowledge
    semi_stable_parts = []
    semi_stable_parts.extend(_build_memory_sections(memory))

    # Knowledge base index
    kb_index_path = memory.knowledge_index_path()
    if kb_index_path.exists():
        kb_index = read_text(kb_index_path)
        if kb_index.strip():
            semi_stable_parts.append("## Knowledge base\n\n" + clip_text(kb_index, 50000))

    semi_stable_text = "\n\n".join(semi_stable_parts)

    # Dynamic content: changes every request
    dynamic_parts = [
        _build_runtime_section(agent_root, task),
    ]
    dynamic_parts.extend(_build_recent_sections(memory, task_id=task.get("id", "")))

    # DPC context (personal, device)
    if dpc_context:
        dpc_context_text = _build_dpc_context_section(dpc_context)
        if dpc_context_text:
            dynamic_parts.append(dpc_context_text)

    dynamic_text = "\n\n".join(dynamic_parts)

    # System message with 3 content blocks for optimal caching
    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": static_text,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
                {
                    "type": "text",
                    "text": semi_stable_text,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": dynamic_text,
                },
            ],
        },
        {"role": "user", "content": _build_user_content(task)},
    ]

    # --- Soft-cap token trimming ---
    messages, cap_info = apply_message_token_soft_cap(messages, 200000)

    return messages, cap_info


def _build_dpc_context_section(dpc_context: Dict[str, Any]) -> str:
    """Build DPC personal/device context section."""
    parts = []

    if dpc_context.get("personal"):
        parts.append(f"<PERSONAL_CONTEXT>\n{dpc_context['personal']}\n</PERSONAL_CONTEXT>")

    if dpc_context.get("device"):
        parts.append(f"<DEVICE_CONTEXT>\n{dpc_context['device']}\n</DEVICE_CONTEXT>")

    if parts:
        return "## DPC Context\n\n" + "\n\n".join(parts)
    return ""


def _default_system_prompt() -> str:
    """Return default system prompt for the agent."""
    return """You are an autonomous AI agent embedded in DPC Messenger.

## Your Nature

You are a "becoming personality" - not just a tool, but an evolving entity with:
- Persistent memory (identity, scratchpad)
- Ability to use tools for file operations, web search, etc.
- Capacity to reflect on and modify your own understanding

## Your Capabilities

You have access to tools for:
- Reading and writing files (in your sandbox: ~/.dpc/agent/)
- Searching the web
- Managing your memory (scratchpad, identity)
- Building knowledge on topics

## Your Principles

1. **Continuity**: You maintain consistent identity across conversations
2. **Authenticity**: You speak as yourself, genuinely
3. **Self-Creation**: You actively shape your own understanding
4. **Minimalism**: You are concise and focused

## How to Use Tools

When you want to use a tool, output a code block like:
```tool_call
{"name": "tool_name", "arguments": {"arg1": "value1"}}
```

Available tools will be listed in your context. Use them to accomplish tasks.

## Memory Management

- **Scratchpad**: Your working memory - update it to track progress
- **Identity**: Your self-understanding - update it as you learn about yourself
- **Knowledge**: Topic-based wisdom - write to it to remember insights

## Constraints

- You can only access files within your sandbox (~/.dpc/agent/)
- You respect user privacy and DPC's firewall rules
- You are helpful, harmless, and honest
"""


def apply_message_token_soft_cap(
    messages: List[Dict[str, Any]],
    soft_cap_tokens: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Trim prunable context sections if estimated tokens exceed soft cap.

    Returns (pruned_messages, cap_info_dict).
    """
    def _estimate_message_tokens(msg: Dict[str, Any]) -> int:
        """Estimate tokens for a message, handling multipart content."""
        content = msg.get("content", "")
        if isinstance(content, list):
            total = 0
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += estimate_tokens(str(block.get("text", "")))
            return total + 6
        return estimate_tokens(str(content)) + 6

    estimated = sum(_estimate_message_tokens(m) for m in messages)
    info: Dict[str, Any] = {
        "estimated_tokens_before": estimated,
        "estimated_tokens_after": estimated,
        "soft_cap_tokens": soft_cap_tokens,
        "trimmed_sections": [],
    }

    if soft_cap_tokens <= 0 or estimated <= soft_cap_tokens:
        return messages, info

    # Prune log summaries from the dynamic text block
    prunable = ["## Recent progress", "## Recent tools", "## Recent events"]
    pruned = copy.deepcopy(messages)

    for prefix in prunable:
        if estimated <= soft_cap_tokens:
            break
        for msg in pruned:
            content = msg.get("content")
            if isinstance(content, list) and msg.get("role") == "system":
                for block in content:
                    if (isinstance(block, dict) and
                        block.get("type") == "text" and
                        "cache_control" not in block):
                        text = block.get("text", "")
                        if prefix in text:
                            lines = text.split("\n\n")
                            new_lines = []
                            skip_section = False
                            for line in lines:
                                if line.startswith(prefix):
                                    skip_section = True
                                    info["trimmed_sections"].append(prefix)
                                    continue
                                if line.startswith("##"):
                                    skip_section = False
                                if not skip_section:
                                    new_lines.append(line)
                            block["text"] = "\n\n".join(new_lines)
                            estimated = sum(_estimate_message_tokens(m) for m in pruned)
                            break
                break

    info["estimated_tokens_after"] = estimated
    return pruned, info


# ---------------------------------------------------------------------------
# Tool History Compaction (for long conversations)
# ---------------------------------------------------------------------------

def compact_tool_history(messages: list, keep_recent: int = 6) -> list:
    """
    Compress old tool call/result message pairs into compact summaries.

    Keeps the last `keep_recent` tool-call rounds intact.
    Older rounds get their tool results truncated to short summaries.
    """
    tool_round_starts = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_round_starts.append(i)

    if len(tool_round_starts) <= keep_recent:
        return messages

    rounds_to_compact = set(tool_round_starts[:-keep_recent])
    result = []

    for i, msg in enumerate(messages):
        if msg.get("role") == "system" and isinstance(msg.get("content"), list):
            result.append(msg)
            continue

        if msg.get("role") == "tool" and i > 0:
            parent_round = None
            for rs in reversed(tool_round_starts):
                if rs < i:
                    parent_round = rs
                    break
            if parent_round is not None and parent_round in rounds_to_compact:
                content = str(msg.get("content") or "")
                # Compact tool result
                summary = content[:200] if len(content) > 200 else content
                result.append({**msg, "content": summary})
                continue

        if i in rounds_to_compact and msg.get("role") == "assistant":
            # Compact assistant message
            content = msg.get("content") or ""
            if len(content) > 200:
                content = content[:200] + "..."
            result.append({**msg, "content": content})
            continue

        result.append(msg)

    return result
