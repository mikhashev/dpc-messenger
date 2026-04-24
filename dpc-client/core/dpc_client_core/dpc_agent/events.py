"""
DPC Agent Event System - Emit events to external listeners.

Enables monitoring of agent activity via Telegram, webhooks, etc.
Events are emitted for important lifecycle changes, task completions,
and agent self-improvement activities.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .utils import utc_now_iso, append_jsonl, get_agent_root

log = logging.getLogger(__name__)


class EventType(Enum):
    """Types of events that can be emitted by the agent."""

    # Lifecycle
    AGENT_STARTED = "agent_started"
    AGENT_STOPPED = "agent_stopped"

    # Tasks
    TASK_SCHEDULED = "task_scheduled"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"

    # Streaming
    TEXT_CHUNK = "text_chunk"

    # Consciousness
    THOUGHT_STARTED = "thought_started"
    THOUGHT_COMPLETED = "thought_completed"

    # Tools
    TOOL_EXECUTED = "tool_executed"

    CODE_MODIFIED = "code_modified"

    # Memory
    IDENTITY_UPDATED = "identity_updated"
    SCRATCHPAD_UPDATED = "scratchpad_updated"
    KNOWLEDGE_UPDATED = "knowledge_updated"

    # Budget
    BUDGET_WARNING = "budget_warning"
    RATE_LIMIT_HIT = "rate_limit_hit"

    # Messaging
    AGENT_MESSAGE = "agent_message"  # Agent-initiated message to user (e.g., via Telegram)


@dataclass
class AgentEvent:
    """An event emitted by the agent."""

    type: EventType
    timestamp: str = field(default_factory=utc_now_iso)
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert event to dictionary for serialization."""
        return {
            "type": self.type.value,
            "timestamp": self.timestamp,
            "data": self.data,
        }

    def to_json(self) -> str:
        """Convert event to JSON string."""
        return json.dumps(self.to_dict())


# Event type categories for filtering
EVENT_CATEGORIES = {
    "lifecycle": [EventType.AGENT_STARTED, EventType.AGENT_STOPPED],
    "tasks": [EventType.TASK_SCHEDULED, EventType.TASK_STARTED,
              EventType.TASK_COMPLETED, EventType.TASK_FAILED],
    "streaming": [EventType.TEXT_CHUNK],
    "thoughts": [EventType.THOUGHT_STARTED, EventType.THOUGHT_COMPLETED],
    "tools": [EventType.TOOL_EXECUTED],
    "tools_extended": [EventType.CODE_MODIFIED],
    "memory": [EventType.IDENTITY_UPDATED, EventType.SCRATCHPAD_UPDATED,
               EventType.KNOWLEDGE_UPDATED],
    "budget": [EventType.BUDGET_WARNING, EventType.RATE_LIMIT_HIT],
    "messaging": [EventType.AGENT_MESSAGE],
}


class AgentEventEmitter:
    """
    Event emitter for agent monitoring.

    Allows external systems to subscribe to agent events for:
    - Telegram notifications
    - Webhook integrations
    - Logging and analytics
    - UI updates

    Usage:
        emitter = AgentEventEmitter()
        emitter.add_listener(send_to_telegram)
        await emitter.emit(EventType.TASK_COMPLETED, {"task_id": "xxx", "result": "..."})
    """

    def __init__(
        self,
        agent_root: Optional[Path] = None,
        max_log_size: int = 1000,
        persist_events: bool = True,
    ):
        """
        Initialize event emitter.

        Args:
            agent_root: Root directory for agent storage (for event log)
            max_log_size: Maximum number of events to keep in memory
            persist_events: Whether to persist events to disk
        """
        # Only set agent_root if we're persisting events or if it's provided
        # This avoids creating legacy ~/.dpc/agent/ folder for global emitter
        if persist_events:
            self.agent_root = agent_root or get_agent_root("default")
        else:
            self.agent_root = None
        self._listeners: List[Callable[[AgentEvent], Any]] = []
        self._event_log: List[AgentEvent] = []
        self._max_log_size = max_log_size
        self._persist_events = persist_events
        self._lock = asyncio.Lock()

        log.info(f"AgentEventEmitter initialized (persist={persist_events})")

    def add_listener(self, callback: Callable[[AgentEvent], Any]) -> None:
        """
        Add an event listener.

        The callback can be sync or async. If async, it will be awaited.
        Exceptions in callbacks are caught and logged, not propagated.

        Args:
            callback: Function to call when an event is emitted
        """
        self._listeners.append(callback)
        log.info(f"Added event listener: {getattr(callback, '__name__', repr(callback))}")

    def remove_listener(self, callback: Callable[[AgentEvent], Any]) -> bool:
        """
        Remove an event listener.

        Args:
            callback: The callback to remove

        Returns:
            True if removed, False if not found
        """
        try:
            self._listeners.remove(callback)
            log.info(f"Removed event listener: {getattr(callback, '__name__', repr(callback))}")
            return True
        except ValueError:
            return False

    def clear_listeners(self) -> None:
        """Remove all listeners."""
        self._listeners.clear()
        log.info("Cleared all event listeners")

    async def emit(
        self,
        event_type: EventType,
        data: Optional[Dict[str, Any]] = None,
    ) -> AgentEvent:
        """
        Emit an event to all listeners.

        Args:
            event_type: Type of event
            data: Event payload

        Returns:
            The emitted event
        """
        event = AgentEvent(type=event_type, data=data or {})

        # Log event in memory
        self._event_log.append(event)
        if len(self._event_log) > self._max_log_size:
            self._event_log.pop(0)

        # Persist to disk if enabled
        if self._persist_events and self.agent_root:
            try:
                events_log = self.agent_root / "logs" / "events.jsonl"
                append_jsonl(events_log, event.to_dict())
            except Exception as e:
                log.error(f"Failed to persist event: {e}")

        # Notify listeners (non-blocking, catches exceptions)
        for listener in self._listeners:
            try:
                result = listener(event)
                if asyncio.iscoroutine(result):
                    # Create task but don't wait for it
                    asyncio.create_task(self._safe_coro(result, listener))
            except Exception as e:
                log.error(f"Event listener error in {getattr(listener, '__name__', repr(listener))}: {e}")

        log.debug(f"Emitted event: {event_type.value}")
        return event

    async def _safe_coro(self, coro, listener: Callable) -> None:
        """Safely execute a coroutine from a listener."""
        try:
            await coro
        except Exception as e:
            log.error(f"Async listener error in {getattr(listener, '__name__', repr(listener))}: {e}")

    def emit_sync(
        self,
        event_type: EventType,
        data: Optional[Dict[str, Any]] = None,
    ) -> AgentEvent:
        """
        Emit an event synchronously (for non-async contexts).

        Note: This creates a task for async listeners but doesn't wait for them.

        Args:
            event_type: Type of event
            data: Event payload

        Returns:
            The emitted event
        """
        event = AgentEvent(type=event_type, data=data or {})

        # Log event in memory
        self._event_log.append(event)
        if len(self._event_log) > self._max_log_size:
            self._event_log.pop(0)

        # Persist to disk if enabled
        if self._persist_events and self.agent_root:
            try:
                events_log = self.agent_root / "logs" / "events.jsonl"
                append_jsonl(events_log, event.to_dict())
            except Exception as e:
                log.error(f"Failed to persist event: {e}")

        # Notify sync listeners only (async ones will be missed)
        for listener in self._listeners:
            try:
                result = listener(event)
                if asyncio.iscoroutine(result):
                    log.warning(f"Async listener {getattr(listener, '__name__', repr(listener))} "
                               "called from sync context, will not be awaited")
            except Exception as e:
                log.error(f"Event listener error: {e}")

        return event

    def get_recent_events(
        self,
        count: int = 50,
        event_types: Optional[List[EventType]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get recent events.

        Args:
            count: Maximum number of events to return
            event_types: Optional filter by event types

        Returns:
            List of event dictionaries
        """
        events = self._event_log[-count:]

        if event_types:
            events = [e for e in events if e.type in event_types]

        return [e.to_dict() for e in events]

    def get_events_by_category(self, category: str, count: int = 50) -> List[Dict[str, Any]]:
        """
        Get recent events by category.

        Args:
            category: Category name (lifecycle, tasks, tools, tools_extended, memory, budget)
            count: Maximum number of events to return

        Returns:
            List of event dictionaries
        """
        event_types = EVENT_CATEGORIES.get(category, [])
        return self.get_recent_events(count, event_types)

    def get_event_count(self, event_type: Optional[EventType] = None) -> int:
        """Get count of events, optionally filtered by type."""
        if event_type:
            return sum(1 for e in self._event_log if e.type == event_type)
        return len(self._event_log)


# Global emitter instance (lazy-initialized)
_emitter: Optional[AgentEventEmitter] = None


def get_event_emitter() -> AgentEventEmitter:
    """
    Get the global event emitter instance.

    Creates a new instance on first call.

    Note: This is a fallback emitter for non-agent contexts.
    Individual agents create their own emitters with agent_root for persistence.
    Global emitter doesn't persist to disk to avoid creating legacy ~/.dpc/agent/ folder.
    """
    global _emitter
    if _emitter is None:
        _emitter = AgentEventEmitter(persist_events=False)
    return _emitter


def reset_event_emitter() -> None:
    """Reset the global event emitter (useful for testing)."""
    global _emitter
    _emitter = None


# Convenience functions for common events
async def emit_task_scheduled(task_id: str, task_type: str, **kwargs) -> AgentEvent:
    """Emit a task scheduled event."""
    return await get_event_emitter().emit(
        EventType.TASK_SCHEDULED,
        {"task_id": task_id, "task_type": task_type, **kwargs}
    )


async def emit_task_completed(task_id: str, result: Optional[str] = None, **kwargs) -> AgentEvent:
    """Emit a task completed event."""
    return await get_event_emitter().emit(
        EventType.TASK_COMPLETED,
        {"task_id": task_id, "result": result[:500] if result else None, **kwargs}
    )


async def emit_task_failed(task_id: str, error: str, **kwargs) -> AgentEvent:
    """Emit a task failed event."""
    return await get_event_emitter().emit(
        EventType.TASK_FAILED,
        {"task_id": task_id, "error": error[:500], **kwargs}
    )


async def emit_thought_completed(thought_type: str, thought_number: int, **kwargs) -> AgentEvent:
    """Emit a thought completed event."""
    return await get_event_emitter().emit(
        EventType.THOUGHT_COMPLETED,
        {"thought_type": thought_type, "thought_number": thought_number, **kwargs}
    )


async def emit_code_modified(path: str, description: str, **kwargs) -> AgentEvent:
    """Emit a code modified event."""
    return await get_event_emitter().emit(
        EventType.CODE_MODIFIED,
        {"path": path, "description": description[:200], **kwargs}
    )


async def emit_agent_message(message: str, priority: str = "normal", **kwargs) -> AgentEvent:
    """
    Emit an agent-initiated message for Telegram bridge.

    Args:
        message: The message content to send
        priority: Message priority (urgent, high, normal, low)
        **kwargs: Additional data to include

    Returns:
        The emitted event
    """
    return await get_event_emitter().emit(
        EventType.AGENT_MESSAGE,
        {
            "message": message,
            "priority": priority,  # urgent, high, normal, low
            **kwargs
        }
    )
