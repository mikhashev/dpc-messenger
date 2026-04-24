"""
DPC Agent - Embedded autonomous AI agent.

Key Components:
- DpcAgent: Simplified agent orchestrator
- DpcLlmAdapter: Bridges to DPC's LLMManager
- ToolRegistry: Plugin architecture with sandbox constraints
- Memory: Scratchpad, identity, knowledge base
- TaskQueue: Background task scheduling
- Events: Event emission for monitoring
- Budget: Subscription-aware rate limiting
"""

from .llm_adapter import DpcLlmAdapter
from .agent import DpcAgent, AgentConfig
from .memory import Memory
from .tools import ToolRegistry, ToolContext, ToolEntry
from .task_queue import TaskQueue, Task, TaskPriority, TaskStatus
from .events import (
    AgentEventEmitter, AgentEvent, EventType,
    get_event_emitter, emit_task_completed, emit_task_failed,
    emit_code_modified,
)
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
    "emit_code_modified",
    # Budget
    "SubscriptionBudget",
    "PayPerUseBudget",
    "HybridBudget",
    "BillingModel",
    "ProviderLimits",
]
