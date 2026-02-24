"""
DPC Agent — Messaging Tools.

Provides tools for the agent to communicate with users:
- send_user_message: Send a message to the user via Telegram

This enables agent-initiated communication, allowing the agent
to proactively reach out to users without waiting for input.
"""

from __future__ import annotations

import logging
from typing import List

from .registry import ToolEntry, ToolContext
from ..events import emit_agent_message

log = logging.getLogger(__name__)


# Priority levels for messages
VALID_PRIORITIES = {"urgent", "high", "normal", "low"}

# Telegram message limit (leave some buffer for formatting)
MAX_MESSAGE_LENGTH = 4000


def split_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """
    Split a long message into multiple parts at natural boundaries.

    Tries to split at paragraph breaks, then sentence breaks,
    then word boundaries to avoid cutting mid-word.

    Args:
        text: The text to split
        max_length: Maximum length per part

    Returns:
        List of message parts
    """
    if len(text) <= max_length:
        return [text]

    parts = []

    while text:
        if len(text) <= max_length:
            parts.append(text)
            break

        # Find best split point (prefer paragraph, then sentence, then word)
        split_pos = max_length

        # Try to find paragraph break (double newline)
        para_break = text.rfind("\n\n", 0, max_length)
        if para_break > max_length // 2:
            split_pos = para_break + 2
        else:
            # Try to find single newline
            line_break = text.rfind("\n", 0, max_length)
            if line_break > max_length // 2:
                split_pos = line_break + 1
            else:
                # Try to find sentence end
                for end in [". ", "! ", "? ", "。", "！", "？"]:
                    sent_break = text.rfind(end, 0, max_length - 1)
                    if sent_break > max_length // 2:
                        split_pos = sent_break + len(end)
                        break
                else:
                    # Fall back to word boundary
                    word_break = text.rfind(" ", 0, max_length)
                    if word_break > max_length // 2:
                        split_pos = word_break + 1

        parts.append(text[:split_pos])
        text = text[split_pos:]

    return parts


async def send_user_message(ctx: ToolContext, message: str, priority: str = "normal") -> str:
    """
    Send a message to the user via Telegram.

    This allows the agent to proactively communicate with the user,
    for example to ask questions, provide updates, or notify about
    important findings.

    Args:
        ctx: Tool context
        message: The message to send (will be formatted as Markdown)
        priority: Message priority - one of: urgent, high, normal, low
            - urgent: Critical notifications (🔴)
            - high: Important updates (🟠)
            - normal: Standard messages (🟡)
            - low: Informational messages (🟢)

    Returns:
        Confirmation string
    """
    # Validate priority
    priority = priority.lower().strip()
    if priority not in VALID_PRIORITIES:
        priority = "normal"

    # Validate message
    if not message or not message.strip():
        return "Error: Message cannot be empty"

    # Split long messages instead of truncating
    parts = split_message(message)
    num_parts = len(parts)

    try:
        # Emit each part as a separate message
        for i, part in enumerate(parts):
            # Add part indicator for multi-part messages
            if num_parts > 1:
                part_with_indicator = f"[{i+1}/{num_parts}]\n\n{part}"
            else:
                part_with_indicator = part

            await emit_agent_message(
                message=part_with_indicator,
                priority=priority,
            )

        result = f"Message sent to user via Telegram (priority: {priority})"
        if num_parts > 1:
            result += f" [sent in {num_parts} parts]"

        log.info(f"Agent sent user message (priority={priority}, len={len(message)}, parts={num_parts})")
        return result

    except Exception as e:
        log.error(f"Failed to send user message: {e}", exc_info=True)
        return f"Error sending message: {e}"


# ---------------------------------------------------------------------------
# Tool Registry Export
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    """Export messaging tools for registry."""
    return [
        ToolEntry(
            name="send_user_message",
            schema={
                "name": "send_user_message",
                "description": (
                    "Send a message to the user via Telegram. "
                    "Use this to proactively communicate with the user, "
                    "ask questions, provide updates, or notify about important findings. "
                    "Messages are sent immediately and will appear in the user's Telegram."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": (
                                "The message to send. Supports Markdown formatting. "
                                "Long messages will be automatically split into multiple parts."
                            )
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["urgent", "high", "normal", "low"],
                            "description": (
                                "Message priority. "
                                "urgent=critical (red), high=important (orange), "
                                "normal=standard (yellow), low=info (green)"
                            ),
                            "default": "normal"
                        }
                    },
                    "required": ["message"]
                }
            },
            handler=send_user_message,
            is_code_tool=False,
            timeout_sec=30,
            is_core=True,
        ),
    ]
