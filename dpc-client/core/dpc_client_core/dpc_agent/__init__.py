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
- TaskQueue: Background task scheduling
- Events: Event emission for monitoring
- Evolution: Self-modification within sandbox
- Budget: Subscription-aware rate limiting

Features:
- 40+ tools (file ops, git, web search, etc.)
- Background consciousness (optional)
- Self-modification within sandbox (~/.dpc/agent/)
- Persistent memory & identity
- Task scheduling and background execution
- Telegram notifications for monitoring
"""

from .llm_adapter import DpcLlmAdapter
from .agent import DpcAgent, AgentConfig
from .memory import Memory
from .tools import ToolRegistry, ToolContext, ToolEntry
from .task_queue import TaskQueue, Task, TaskPriority, TaskStatus
from .events import (
    AgentEventEmitter, AgentEvent, EventType,
    get_event_emitter, emit_task_completed, emit_task_failed,
    emit_evolution_cycle, emit_code_modified,
)
from .evolution import EvolutionManager, EvolutionStatus
from .budget import (
    SubscriptionBudget, PayPerUseBudget, HybridBudget,
    BillingModel, ProviderLimits,
)

__all__ = [
    # Core
    "DpcAgent",
    "AgentConfig",
    "DpcLlmAdapter",
    "Memory",
    "ToolRegistry",
    "ToolContext",
    "ToolEntry",
    "BackgroundConsciousness",
    # Task Queue
    "TaskQueue",
    "Task",
    "TaskPriority",
    "TaskStatus",
    # Events
    "AgentEventEmitter",
    "AgentEvent",
    "EventType",
    "get_event_emitter",
    "emit_task_completed",
    "emit_task_failed",
    "emit_evolution_cycle",
    "emit_code_modified",
    # Evolution
    "EvolutionManager",
    "EvolutionStatus",
    # Budget
    "SubscriptionBudget",
    "PayPerUseBudget",
    "HybridBudget",
    "BillingModel",
    "ProviderLimits",
]
