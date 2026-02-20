"""
DPC Agent - Embedded autonomous AI agent.

This package contains an embedded version of the Ouroboros self-modifying AI agent,
adapted to work with DPC Messenger's infrastructure.

Key Components:
- DpcAgent: Simplified agent orchestrator
- DpcLlmAdapter: Bridges to DPC's LLMManager
- ToolRegistry: Plugin architecture with sandbox constraints
- Memory: Scratchpad, identity, knowledge base
- BackgroundConsciousness: Proactive thinking between tasks

Features:
- 40+ tools (file ops, git, web search, etc.)
- Background consciousness (optional)
- Self-modification within sandbox (~/.dpc/agent/)
- Persistent memory & identity
"""

from .llm_adapter import DpcLlmAdapter
from .agent import DpcAgent, AgentConfig
from .memory import Memory
from .tools import ToolRegistry, ToolContext, ToolEntry
from .consciousness import BackgroundConsciousness

__all__ = [
    "DpcAgent",
    "AgentConfig",
    "DpcLlmAdapter",
    "Memory",
    "ToolRegistry",
    "ToolContext",
    "ToolEntry",
    "BackgroundConsciousness",
]
