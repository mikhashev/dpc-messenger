"""
DPC Agent Task Queue - Persistent task scheduling and execution.

Adapted from Ouroboros supervisor/queue.py for single-process operation.
Key features:
- Priority-based scheduling
- Delayed execution
- Persistent storage (survives restarts)
- Retry logic with exponential backoff
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from .utils import utc_now_iso

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class TaskPriority(Enum):
    """Task priority levels."""
    CRITICAL = 0   # User-initiated, immediate
    HIGH = 1       # Scheduled tasks
    NORMAL = 2     # Background consciousness
    LOW = 3        # Cleanup, maintenance


class TaskStatus(Enum):
    """Task status values."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """A scheduled task for the agent."""
    id: str
    task_type: str
    data: Dict[str, Any]
    priority: str = "normal"  # Store as string for serialization
    status: str = "pending"
    scheduled_at: Optional[str] = None  # ISO timestamp, None = immediate
    created_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3

    def get_priority(self) -> TaskPriority:
        """Get priority enum value."""
        try:
            return TaskPriority[self.priority.upper()]
        except KeyError:
            return TaskPriority.NORMAL

    def get_status(self) -> TaskStatus:
        """Get status enum value."""
        try:
            return TaskStatus(self.status)
        except ValueError:
            return TaskStatus.PENDING


class TaskQueue:
    """
    Priority-based task queue with persistence.

    Features:
    - Priority scheduling (CRITICAL > HIGH > NORMAL > LOW)
    - Delayed execution (scheduled_at)
    - Persistent storage (survives restarts)
    - Retry logic with configurable max retries
    """

    def __init__(self, agent_root: Path):
        """
        Initialize task queue.

        Args:
            agent_root: Root directory for agent storage (~/.dpc/agent/)
        """
        self.agent_root = agent_root
        self.queue_file = agent_root / "state" / "task_queue.json"
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)

        self._queue: List[Task] = []
        self._running: bool = False
        self._processor_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # Callbacks
        self._on_task_start: Optional[Callable[[Task], Any]] = None
        self._on_task_complete: Optional[Callable[[Task], Any]] = None
        self._on_task_failed: Optional[Callable[[Task], Any]] = None

        self._load_queue()
        log.info(f"TaskQueue initialized with {len(self._queue)} pending tasks")

    def _load_queue(self) -> None:
        """Load persisted queue from disk."""
        if not self.queue_file.exists():
            return

        try:
            data = json.loads(self.queue_file.read_text(encoding="utf-8"))
            for item in data.get("tasks", []):
                try:
                    task = Task(**item)
                    # Only load pending tasks
                    if task.status == "pending":
                        self._queue.append(task)
                except Exception as e:
                    log.warning(f"Failed to load task: {e}")

            log.info(f"Loaded {len(self._queue)} pending tasks from disk")
        except Exception as e:
            log.error(f"Failed to load task queue: {e}")

    def _save_queue(self) -> None:
        """Persist queue to disk."""
        try:
            data = {
                "tasks": [asdict(t) for t in self._queue],
                "updated_at": utc_now_iso(),
            }
            self.queue_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            log.error(f"Failed to save task queue: {e}")

    def set_callbacks(
        self,
        on_task_start: Optional[Callable[[Task], Any]] = None,
        on_task_complete: Optional[Callable[[Task], Any]] = None,
        on_task_failed: Optional[Callable[[Task], Any]] = None,
    ) -> None:
        """
        Set callback functions for task lifecycle events.

        Args:
            on_task_start: Called when a task starts
            on_task_complete: Called when a task completes successfully
            on_task_failed: Called when a task fails
        """
        self._on_task_start = on_task_start
        self._on_task_complete = on_task_complete
        self._on_task_failed = on_task_failed

    def schedule(
        self,
        task_type: str,
        data: Dict[str, Any],
        priority: TaskPriority = TaskPriority.NORMAL,
        scheduled_at: Optional[datetime] = None,
        task_id: Optional[str] = None,
    ) -> Task:
        """
        Schedule a new task.

        Args:
            task_type: Type of task (e.g., 'chat', 'review', 'reminder')
            data: Task payload
            priority: Task priority
            scheduled_at: When to execute (None = immediate)
            task_id: Optional task ID (auto-generated if None)

        Returns:
            Created task
        """
        task = Task(
            id=task_id or f"task-{uuid.uuid4().hex[:8]}",
            task_type=task_type,
            data=data,
            priority=priority.name.lower(),
            scheduled_at=scheduled_at.isoformat() if scheduled_at else None,
        )

        # Insert by priority
        self._queue.append(task)
        self._queue.sort(key=lambda t: TaskPriority[t.priority.upper()].value)
        self._save_queue()

        log.info(f"Scheduled task {task.id}: {task_type} (priority={priority.name})")
        return task

    def cancel(self, task_id: str) -> bool:
        """
        Cancel a pending task.

        Args:
            task_id: ID of task to cancel

        Returns:
            True if cancelled, False if not found or already running
        """
        for i, task in enumerate(self._queue):
            if task.id == task_id and task.status == "pending":
                task.status = "cancelled"
                self._queue.pop(i)
                self._save_queue()
                log.info(f"Cancelled task {task_id}")
                return True
        return False

    def get_next(self) -> Optional[Task]:
        """
        Get next runnable task.

        Returns:
            Next task to execute, or None if no tasks are ready
        """
        now = datetime.utcnow()

        for task in self._queue:
            if task.status != "pending":
                continue

            # Check scheduled time
            if task.scheduled_at:
                try:
                    scheduled = datetime.fromisoformat(task.scheduled_at.replace("Z", "+00:00"))
                    # Remove timezone for comparison
                    if scheduled.tzinfo:
                        scheduled = scheduled.replace(tzinfo=None)
                    if scheduled > now:
                        continue  # Not yet time
                except Exception as e:
                    log.warning(f"Failed to parse scheduled_at for task {task.id}: {e}")

            return task

        return None

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get a task by ID."""
        for task in self._queue:
            if task.id == task_id:
                return task
        return None

    def mark_running(self, task: Task) -> None:
        """Mark task as running."""
        task.status = "running"
        task.started_at = utc_now_iso()
        self._save_queue()

        if self._on_task_start:
            try:
                result = self._on_task_start(task)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:
                log.error(f"on_task_start callback error: {e}")

    def mark_complete(self, task: Task, result: str) -> None:
        """Mark task as completed."""
        task.status = "completed"
        task.completed_at = utc_now_iso()
        task.result = result[:10000] if result else None  # Truncate long results

        # Remove from queue
        self._queue = [t for t in self._queue if t.id != task.id]
        self._save_queue()

        log.info(f"Task {task.id} completed successfully")

        if self._on_task_complete:
            try:
                callback_result = self._on_task_complete(task)
                if asyncio.iscoroutine(callback_result):
                    asyncio.create_task(callback_result)
            except Exception as e:
                log.error(f"on_task_complete callback error: {e}")

    def mark_failed(self, task: Task, error: str) -> None:
        """Mark task as failed, potentially retry."""
        task.retry_count += 1
        task.error = error[:2000] if error else None  # Truncate error message

        if task.retry_count < task.max_retries:
            task.status = "pending"
            log.warning(f"Task {task.id} failed, will retry ({task.retry_count}/{task.max_retries}): {error[:200]}")
        else:
            task.status = "failed"
            task.completed_at = utc_now_iso()
            # Remove from queue
            self._queue = [t for t in self._queue if t.id != task.id]
            log.error(f"Task {task.id} failed permanently: {error[:200]}")

            if self._on_task_failed:
                try:
                    callback_result = self._on_task_failed(task)
                    if asyncio.iscoroutine(callback_result):
                        asyncio.create_task(callback_result)
                except Exception as e:
                    log.error(f"on_task_failed callback error: {e}")

        self._save_queue()

    async def start_processor(
        self,
        executor: Callable[[Task], Any],
        poll_interval: float = 1.0,
    ) -> None:
        """
        Start background task processor.

        Args:
            executor: Async function to execute tasks
            poll_interval: Seconds between polls
        """
        if self._running:
            log.warning("Task processor already running")
            return

        self._running = True
        self._stop_event.clear()

        async def _processor_loop():
            log.info("Task processor started")

            while self._running:
                try:
                    task = self.get_next()
                    if task:
                        log.info(f"Executing task {task.id}: {task.task_type}")
                        self.mark_running(task)

                        try:
                            result = await executor(task)
                            self.mark_complete(task, str(result) if result else "")
                        except asyncio.CancelledError:
                            log.info(f"Task {task.id} cancelled during execution")
                            self.mark_failed(task, "Cancelled")
                            break
                        except Exception as e:
                            self.mark_failed(task, str(e))

                    # Wait for next poll or stop signal
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=poll_interval
                        )
                        # Stop event was set
                        break
                    except asyncio.TimeoutError:
                        # Poll interval elapsed
                        pass

                except Exception as e:
                    log.error(f"Processor loop error: {e}", exc_info=True)
                    await asyncio.sleep(5)

            log.info("Task processor stopped")
            self._running = False

        self._processor_task = asyncio.create_task(_processor_loop())

    def stop_processor(self) -> None:
        """Stop background task processor."""
        if not self._running:
            return

        log.info("Stopping task processor...")
        self._running = False
        self._stop_event.set()

        if self._processor_task:
            self._processor_task.cancel()
            self._processor_task = None

    def is_running(self) -> bool:
        """Check if processor is running."""
        return self._running

    def get_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        pending = [t for t in self._queue if t.status == "pending"]
        running = [t for t in self._queue if t.status == "running"]

        by_type: Dict[str, int] = {}
        for task in pending:
            by_type[task.task_type] = by_type.get(task.task_type, 0) + 1

        return {
            "total_pending": len(pending),
            "total_running": len(running),
            "processor_running": self._running,
            "by_type": by_type,
            "next_task_id": pending[0].id if pending else None,
        }

    def clear_completed(self) -> int:
        """Remove all completed/failed tasks from queue. Returns count removed."""
        initial_count = len(self._queue)
        self._queue = [t for t in self._queue if t.status in ("pending", "running")]
        removed = initial_count - len(self._queue)
        if removed > 0:
            self._save_queue()
            log.info(f"Cleared {removed} completed/failed tasks")
        return removed
