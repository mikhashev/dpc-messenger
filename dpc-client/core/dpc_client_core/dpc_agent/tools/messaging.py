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

    # Truncate very long messages (Telegram has a 4096 char limit)
    truncated = False
    if len(message) > 3500:
        message = message[:3500] + "... (truncated)"
        truncated = True

    try:
        # Emit the AGENT_MESSAGE event
        # The AgentTelegramBridge will pick this up and forward to Telegram
        await emit_agent_message(
            message=message,
            priority=priority,
        )

        result = f"Message sent to user via Telegram (priority: {priority})"
        if truncated:
            result += " [message was truncated due to length]"

        log.info(f"Agent sent user message (priority={priority}, len={len(message)})")
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
                                "Will be truncated if longer than 3500 characters."
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
