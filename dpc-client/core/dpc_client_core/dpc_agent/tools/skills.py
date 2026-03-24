"""
DPC Agent — Skill execution tool.

Provides the `execute_skill` tool that loads a skill strategy by name
and returns its markdown instructions for the LLM to follow.

This is the Read phase of the Memento-Skills Read-Write Reflective Learning loop:
- Read:  LLM sees available skill names + descriptions in system prompt,
         calls execute_skill(name) to load the full strategy, then follows it
- Write: Phase 3 (skill_reflection.py) — agent updates SKILL.md after each task
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from ..tools.registry import ToolContext, ToolEntry


def _execute_skill(ctx: "ToolContext", skill_name: str, request: str = "") -> str:
    """
    Load a skill strategy and return its instructions.

    The LLM reads the returned strategy and follows it using existing tools.
    """
    if ctx.skill_store is None:
        return "⚠️ Skill store not available on this agent"

    body = ctx.skill_store.load_skill_body(skill_name)
    if body is None:
        available = ctx.skill_store.list_skill_names()
        return (
            f"⚠️ Skill '{skill_name}' not found. "
            f"Available skills: {', '.join(available) if available else 'none installed'}"
        )

    header = f"# Skill Strategy: {skill_name}\n\n"
    if request:
        header += f"**Your request:** {request}\n\n"
    header += "Follow the **Strategy** section below using your available tools:\n\n"
    return header + body


def get_tools() -> "List[ToolEntry]":
    from ..tools.registry import ToolEntry

    return [
        ToolEntry(
            name="execute_skill",
            schema={
                "name": "execute_skill",
                "description": (
                    "Load a skill strategy by name and return its step-by-step instructions. "
                    "Use this at the start of a complex task to get the recommended approach. "
                    "Available skills are listed in the 'Available Skills' section of your context. "
                    "After reading the skill, follow its Strategy section using your other tools."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "Name of the skill to load (kebab-case, e.g. 'code-analysis')",
                        },
                        "request": {
                            "type": "string",
                            "description": "Optional: describe your specific task so the skill strategy can be scoped",
                        },
                    },
                    "required": ["skill_name"],
                },
            },
            handler=_execute_skill,
            is_core=True,
            timeout_sec=5,
        ),
    ]
