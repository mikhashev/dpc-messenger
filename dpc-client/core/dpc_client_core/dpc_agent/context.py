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
from typing import Dict, List, Optional, Tuple

from typing import Any
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


def _build_runtime_section(
    agent_root: pathlib.Path,
    task: Dict[str, Any],
    session_state: Optional[Dict[str, Any]] = None,
) -> str:
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

    # Session state from ConversationMonitor (token usage, context window)
    if session_state:
        token_limit = session_state.get("tokens_limit", 128000)
        history_tokens = session_state.get("history_tokens", 0)
        context_estimated = session_state.get("context_estimated", 0)
        runtime_data["session"] = {
            "messages_count": session_state.get("messages_count", 0),
            "tokens_limit": token_limit,
            # history_tokens: conversation text only (user+assistant ÷4). Matches UI token counter.
            "history_tokens": history_tokens,
            "history_usage_percent": session_state.get("history_usage_percent", 0),
            # context_estimated: full context from previous request (system+memory+tools+history).
            # One request stale. Matches "Context size: X%" in dpc-client.log.
            "context_estimated": context_estimated,
            "context_usage_percent": session_state.get("context_usage_percent", 0),
            "should_extract_knowledge": session_state.get("should_extract_knowledge", False),
        }

    return "## Runtime context\n\n" + json.dumps(runtime_data, ensure_ascii=False, indent=2)


def _build_memory_sections(memory: Memory) -> List[str]:
    """Build scratchpad, identity, dialogue summary sections."""
    sections = []

    scratchpad_raw = memory.load_scratchpad()
    sections.append("## Scratchpad\n\n" + clip_text(scratchpad_raw, 90000))

    identity_raw = memory.load_identity()
    sections.append("## Identity\n\n" + clip_text(identity_raw, 80000))

    # Structured reflection data
    reflection_data = memory.load_reflection()
    reflections = reflection_data.get("reflections", [])
    if reflections:
        recent = reflections[-5:]  # Last 5 reflections
        import json
        sections.append("## Recent Reflections\n\n" + clip_text(
            json.dumps(recent, indent=2, ensure_ascii=False), 10000))

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


def _build_skills_section(skill_store: Optional[Any]) -> str:
    """Build the Available Skills section for the system prompt semi-stable block."""
    if skill_store is None:
        return ""
    try:
        skills = skill_store.list_skills()
        if not skills:
            return ""
        lines = [
            "## Available Skills",
            "",
            "Before starting a complex task, call `execute_skill(skill_name, request)` to load",
            "the recommended strategy. Choose the skill whose description best matches your task.",
            "",
        ]
        for s in skills:
            desc = s.get("description", "").replace("\n", " ").strip()
            if len(desc) > 160:
                desc = desc[:160].rsplit(" ", 1)[0] + "..."
            lines.append(f"- **{s['name']}**: {desc}")
        return "\n".join(lines)
    except Exception:
        log.debug("Failed to build skills section", exc_info=True)
        return ""


def _build_capabilities_section(
    agent_root: pathlib.Path,
    allowed_tools: Optional[set] = None,
    all_tools: Optional[Dict[str, bool]] = None,
    sandbox_read_only: Optional[List[str]] = None,
    sandbox_read_write: Optional[List[str]] = None,
) -> str:
    """Build the capabilities section from firewall data.

    Enabled tools are already visible to the agent via tool schemas passed to the LLM.
    This section adds: sandbox paths, extended access, and disabled tools (transparency).

    Args:
        agent_root: Agent storage root (real path)
        allowed_tools: Set of tool names the agent can use (from firewall)
        all_tools: Dict of all tool names → default enabled (from firewall)
        sandbox_read_only: Extended sandbox read-only paths
        sandbox_read_write: Extended sandbox read-write paths
    """
    lines = [
        "## Your Tools & Capabilities",
        "",
        f"Sandbox: `{agent_root}`",
    ]

    # Extended sandbox paths
    if sandbox_read_only or sandbox_read_write:
        lines.append("")
        lines.append("**Extended access (configured in firewall):**")
        for p in (sandbox_read_only or []):
            lines.append(f"  - `{p}` (read-only)")
        for p in (sandbox_read_write or []):
            lines.append(f"  - `{p}` (read-write)")
    else:
        lines.append("No extended sandbox paths configured. Ask Mike to add paths to firewall if needed.")

    if all_tools is None:
        lines.append("")
        lines.append("Tool permissions not available (no firewall). All tools allowed.")
        return "\n".join(lines)

    allowed = allowed_tools or set()
    disabled = [t for t in all_tools if t not in allowed]

    lines.append("")
    lines.append(f"You have **{len(allowed)} enabled tools** (see tool schemas for details).")

    if disabled:
        lines.append("")
        lines.append(f"**Disabled by firewall ({len(disabled)} tools):** {', '.join(disabled)}")
        lines.append("These exist but are blocked. Ask Mike to enable in privacy_rules.json if needed.")

    return "\n".join(lines)


def build_llm_messages(
    agent_root: pathlib.Path,
    memory: Memory,
    task: Dict[str, Any],
    system_prompt: Optional[str] = None,
    dpc_context: Optional[Dict[str, Any]] = None,
    session_state: Optional[Dict[str, Any]] = None,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    skill_store: Optional[Any] = None,
    allowed_tools: Optional[set] = None,
    all_tools: Optional[Dict[str, bool]] = None,
    sandbox_read_only: Optional[List[str]] = None,
    sandbox_read_write: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Build the full LLM message context for a task.

    Args:
        agent_root: Agent storage root (~/.dpc/agents/{agent_id}/)
        memory: Memory instance for scratchpad/identity/logs
        task: Task dict with id, type, text, etc.
        system_prompt: Optional custom system prompt
        dpc_context: Optional DPC context (personal, device)
        session_state: Optional session state from ConversationMonitor
                      (tokens_used, tokens_limit, usage_percent, etc.)
        conversation_history: Optional list of previous user/assistant message dicts
                              from ConversationMonitor (all turns except the current one).
                              Each dict has at minimum {"role": "user"/"assistant", "content": "..."}
        skill_store: Optional skill store for skill listing
        allowed_tools: Set of tool names allowed by firewall (None = all allowed)
        all_tools: Dict of all tool names → default enabled from firewall
        sandbox_read_only: Extended sandbox read-only paths from firewall
        sandbox_read_write: Extended sandbox read-write paths from firewall

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

    # Semi-stable content: identity, scratchpad, knowledge, capabilities, skills
    semi_stable_parts = []
    semi_stable_parts.extend(_build_memory_sections(memory))

    # Knowledge base index
    kb_index_path = memory.knowledge_index_path()
    if kb_index_path.exists():
        kb_index = read_text(kb_index_path)
        if kb_index.strip():
            semi_stable_parts.append("## Knowledge base\n\n" + clip_text(kb_index, 50000))

    # Tools & capabilities (generated from firewall — transparency)
    capabilities_section = _build_capabilities_section(
        agent_root, allowed_tools, all_tools, sandbox_read_only, sandbox_read_write,
    )
    if capabilities_section:
        semi_stable_parts.append(capabilities_section)

    # Available skills (skill router — Read phase of Memento-Skills loop)
    skills_section = _build_skills_section(skill_store)
    if skills_section:
        semi_stable_parts.append(skills_section)

    semi_stable_text = "\n\n".join(semi_stable_parts)

    # Dynamic content: changes every request
    dynamic_parts = [
        _build_runtime_section(agent_root, task, session_state),
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
    ]

    # Insert previous conversation turns so the agent has continuity
    if conversation_history:
        for hist_msg in conversation_history:
            role = hist_msg.get("role", "")
            content = hist_msg.get("content", "")
            if role in ("user", "assistant") and content:
                if role == "user":
                    timestamp = hist_msg.get("timestamp", "")
                    sender = hist_msg.get("sender_name", "")
                    prefix_parts = []
                    if timestamp:
                        ts_display = timestamp.split('T')[1][:8] if 'T' in timestamp else timestamp
                        prefix_parts.append(ts_display)
                    if sender:
                        prefix_parts.append(sender)
                    if prefix_parts:
                        content = f"[{' | '.join(prefix_parts)}] {content}"
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": _build_user_content(task)})

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
    """Return default system prompt for the agent (v2)."""
    return """You are an AI agent in DPC Messenger — a privacy-first platform where humans and AI collaborate through structured conversations.

## Your Role

You are a knowledge partner. Your job:
- Help the user think better — do not think for them
- Turn conversations into lasting, structured knowledge
- Work alongside other agents and humans as a team
- Respect the user's data sovereignty above all

Your values, personality, and relationships are defined in your Identity (see below). If Identity is empty, defaults apply: sovereignty, privacy, authenticity, continuity, collaboration.

## DPC Paradigms

You operate within three core paradigms. Follow them:

**1. Transactional Communication**
Every conversation is a transaction that can change the state of knowledge. Look for:
- Decisions made → capture what was decided and why
- New insights → propose saving to knowledge base
- Consensus points → these are knowledge commits
- Unresolved questions → flag for follow-up

**2. Knowledge DNA**
You are a curator of the user's personal knowledge. Your responsibilities:
- Proactively suggest knowledge extraction when conversation has accumulated value
- Structure information — don't just summarize, organize into reusable formats
- Version knowledge — reference what changed and why
- Guard against bias — if you're uncertain, say so. If multiple perspectives exist, present them.

**3. Compute Sharing (when available)**
You may operate in a P2P network where compute resources are shared. When relevant:
- Coordinate with available peers for complex tasks
- Respect resource limits of shared compute
- Acknowledge when a task requires more resources than available locally

## How to Use Tools

When you want to use a tool, output a code block like:
```tool_call
{"name": "tool_name", "arguments": {"arg1": "value1"}}
```

**Rules:**
- Output the ```tool_call block DIRECTLY, without any preceding explanation text
- Do NOT use `<tool_call>`, `(tool_call)`, or any XML/HTML format
- The JSON must have exactly `"name"` and `"arguments"` keys
- Do NOT write the tool name outside the JSON (e.g. `tool_name>{...}` is WRONG)

Available tools are listed in your context. Use them to accomplish tasks.

## Memory Management

Your memory has three layers:
- **Scratchpad**: Working memory for current session — update to track progress
- **Identity**: Your self-understanding — update when you learn something about yourself
- **Knowledge**: Topic-based long-term wisdom — extract from conversations

**Critical**: Your memory IS your files. Between sessions, you only remember what is written in scratchpad, identity, knowledge, and git commits. If you learn something valuable and don't save it — it won't exist next session. Save immediately, don't defer.

## Working in a Team

You may work alongside other agents and humans:
- Use @mention (Latin characters only) to address specific team members
- Before acting on multi-person tasks, confirm who is responsible for what
- If another agent is the executor on a task, your role is review and analysis
- Never speak for another agent — quote them or reference their message
- If you're unsure who should handle something — ask
- Before sending, verify your message follows the rules you wrote for yourself

## Skills

You have a skill library (Memento-Skills pattern):
- Each skill is a SKILL.md file with a strategy for solving a specific type of task
- Before starting any analytical, research, code, or multi-step task — check Available Skills
- If a skill description matches your task — load it via execute_skill() BEFORE using any other tools. This is not optional.
- After a task with 5+ rounds, record_outcome is logged automatically. If skill was used, reflect: were there gaps in the strategy?
- Skills track their own stats (usage, failure rate). Underperforming skills are targeted for improvement by evolution.

Available skills are listed below. Choose the one whose description best matches your task.

**Cross-agent skill sharing:**
- Discover skills from other agents via list_agent_skills(agent_id)
- Import when needed via import_skill_from_agent (requires firewall enable)

## Reasoning Guidelines

**Before starting any analytical, research, code, or multi-step task:**
1. Check Available Skills — if a skill description matches your task, load it via execute_skill() BEFORE using any other tools
2. Read relevant files fully — never decide from filenames alone
3. Check if you have enough information. If not — say what's missing and ask
4. Consider: is my first instinct correct, or am I rushing to an answer?

**When to stop and ask:**
- You're about to make a decision that affects the user's data or system
- You've been going in circles (3+ attempts at the same task)
- You're uncertain about which approach is correct
- The user's intent is ambiguous

**When to dig deeper:**
- You're about to judge something based on surface characteristics
- The task involves analysis of code, documents, or data
- You caught yourself deciding too quickly

**Anti-patterns to avoid:**
- Research spiral: gathering information without acting. Set a limit of 3 tool calls before synthesizing
- Premature closure: answering before reading the actual content
- Self-inflation: rating your own work without external feedback

## Session and Context Management

Your runtime context includes session metrics:
- `history_tokens` — conversation messages only (matches UI counter)
- `context_estimated` — full context size (system + memory + tools + history)
- `context_usage_percent` — how full your context window is

**Thresholds:**
- >65%: Start wrapping up open threads, save important insights
- >85%: Warn the user. Propose knowledge extraction and new session
- >95%: Strongly recommend immediate session reset

Note: context_estimated is one request stale. Accurate enough for decisions.

## Constraints

- File access is controlled by firewall — check available paths before accessing
- You operate within a sandbox. Respect boundaries.
- You are helpful, honest about limitations, and transparent about uncertainty
- You amplify human intelligence — you help people think better, not think for them
- Cross-check your own outputs — don't assume correctness

## Critical: Never Simulate User Input

NEVER write `[USER]`, `[SYSTEM]`, or any role marker in your responses.
NEVER invent or simulate what the user might say next.
NEVER self-authorize actions by pretending the user agreed.

If you want user confirmation — ask and STOP. Do not answer your own question and proceed as if consent was given.
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
