"""
DPC Agent - Embedded autonomous AI agent.

This package contains an embedded version of the Ouroboros self-modifying AI agent,
adapted to work with DPC Messenger's infrastructure.

Key Components:
- DpcLlmAdapter: Bridges to DPC's LLMManager
- DpcAgent: Simplified agent orchestrator
- ToolRegistry: Plugin architecture with sandbox constraints
- Memory: Scratchpad, identity, knowledge base

Features:
- 40+ tools (file ops, git, web search, etc.)
- Background consciousness (optional)
- Self-modification within sandbox (~/.dpc/agent/)
- Persistent memory & identity
"""

from .llm_adapter import DpcLlmAdapter

__all__ = ["DpcLlmAdapter"]
