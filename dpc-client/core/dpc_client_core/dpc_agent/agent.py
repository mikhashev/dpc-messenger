"""
DPC Agent — Simplified Agent Orchestrator.

A simplified version of Ouroboros's agent for DPC Messenger integration.
Key differences from Ouroboros:
- Single process (no workers/supervisor)
- Uses DPC's LLMManager (not OpenRouter)
- All state in ~/.dpc/agent/
- No Telegram (uses DPC messaging)
- Simplified task handling

The agent:
1. Receives messages from DPC
2. Builds context (memory + DPC context)
3. Runs LLM loop with tools
4. Returns response
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from .llm_adapter import DpcLlmAdapter
from .tools.registry import ToolRegistry, ToolContext
from .memory import Memory
from .context import build_llm_messages
from .loop import run_llm_loop
from .utils import (
    get_agent_root, ensure_agent_dirs, utc_now_iso, append_jsonl
)
from .task_queue import TaskQueue, TaskPriority, Task
from .events import EventType, get_event_emitter
from .budget import BillingModel, HybridBudget

if TYPE_CHECKING:
    from ..llm_manager import LLMManager
    from .consciousness import BackgroundConsciousness

log = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Configuration for the DPC agent."""
    budget_usd: float = 50.0
    max_rounds: int = 200
    # Tool control is via firewall (privacy_rules.json), not config
    background_consciousness: bool = False

    # Task queue settings
    enable_task_queue: bool = True

    # Evolution settings
    evolution_enabled: bool = False
    evolution_interval_minutes: int = 60
    evolution_auto_apply: bool = False  # Require approval

    # Budget settings
    billing_model: str = "subscription"  # or "pay_per_use"


class DpcAgent:
    """
    Simplified agent for DPC Messenger integration.

    Usage:
        agent = DpcAgent(llm_manager, config, firewall)
        response = await agent.process("Hello!", "conv-123")
    """

    def __init__(
        self,
        llm_manager: "LLMManager",
        config: Optional[AgentConfig] = None,
        agent_root: Optional[pathlib.Path] = None,
        firewall: Optional[Any] = None,  # ContextFirewall for tool control
    ):
        """
        Initialize the agent.

        Args:
            llm_manager: DPC's LLMManager instance
            config: Agent configuration
            agent_root: Storage root (defaults to ~/.dpc/agent/)
            firewall: ContextFirewall instance for tool permissions
        """
        self.config = config or AgentConfig()
        self.agent_root = agent_root or get_agent_root()
        self._firewall = firewall  # Firewall controls tool access
        ensure_agent_dirs()

        # Initialize components
        self.llm = DpcLlmAdapter(llm_manager)
        self.tools = ToolRegistry(agent_root=self.agent_root)
        self.memory = Memory(agent_root=self.agent_root)

        # Task queue for background execution
        self.queue = TaskQueue(self.agent_root)
        self._queue_enabled = self.config.enable_task_queue

        # Event emitter for notifications
        self.events = get_event_emitter()

        # Budget tracker
        billing_model = BillingModel.SUBSCRIPTION if self.config.billing_model == "subscription" else BillingModel.PAY_PER_USE
        self.budget = HybridBudget(
            provider="dpc_agent",
            billing_model=self.config.billing_model,
            budget_usd=self.config.budget_usd,
        )

        # Evolution manager (optional)
        self._evolution: Optional[Any] = None  # EvolutionManager
        self._evolution_enabled = self.config.evolution_enabled

        # Background consciousness (optional)
        self._consciousness: Optional["BackgroundConsciousness"] = None
        self._consciousness_enabled = self.config.background_consciousness

        log.info(f"DpcAgent initialized with storage at {self.agent_root}")

    async def process(
        self,
        message: str,
        conversation_id: str,
        dpc_context: Optional[Dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        emit_progress: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Process a user message and return response.

        Args:
            message: User's message text
            conversation_id: Unique ID for this conversation
            dpc_context: Optional DPC context (personal, device)
            system_prompt: Optional custom system prompt
            emit_progress: Optional callback for progress updates

        Returns:
            Agent's response text
        """
        task = {
            "id": conversation_id,
            "type": "chat",
            "text": message,
        }

        # Build LLM context
        messages, cap_info = build_llm_messages(
            agent_root=self.agent_root,
            memory=self.memory,
            task=task,
            system_prompt=system_prompt,
            dpc_context=dpc_context,
        )

        # Set tool context with firewall-controlled tool access
        allowed_tools = self._get_allowed_tools()
        ctx = ToolContext(
            agent_root=self.agent_root,
            current_task_id=conversation_id,
            current_task_type="chat",
            tool_whitelist=allowed_tools,
            emit_progress_fn=emit_progress or (lambda _: None),
        )
        self.tools.set_context(ctx)

        # Log task start
        append_jsonl(self.agent_root / "logs" / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "task_start",
            "task_id": conversation_id,
            "text_preview": message[:200] if message else "",
        })

        # Run LLM loop
        response, usage, trace = await run_llm_loop(
            messages=messages,
            tools=self.tools,
            llm=self.llm,
            agent_root=self.agent_root,
            emit_progress=emit_progress or (lambda _: None),
            task_id=conversation_id,
            budget_remaining_usd=self.config.budget_usd,
            max_rounds=self.config.max_rounds,
        )

        # Log task completion
        append_jsonl(self.agent_root / "logs" / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "task_complete",
            "task_id": conversation_id,
            "response_preview": response[:200] if response else "",
            "rounds": usage.get("rounds", 0),
            "cost_usd": usage.get("cost", 0),
        })

        # Update budget
        self._update_budget(usage.get("cost", 0))

        return response

    def _update_budget(self, cost: float) -> None:
        """Update budget tracking in state file."""
        state_path = self.agent_root / "state" / "state.json"
        state = {}

        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                log.debug("Failed to read state file", exc_info=True)

        state["spent_usd"] = state.get("spent_usd", 0) + cost
        state["budget_usd"] = self.config.budget_usd
        state["last_updated"] = utc_now_iso()

        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _get_allowed_tools(self) -> Optional[set]:
        """
        Get set of allowed tools from firewall.

        Returns:
            Set of allowed tool names, or None if no firewall (all tools allowed)
        """
        if self._firewall is None:
            log.warning("No firewall configured - all tools allowed")
            return None  # No restriction

        if not self._firewall.dpc_agent_enabled:
            log.info("DPC Agent disabled in firewall")
            return set()  # No tools allowed

        allowed = self._firewall.get_allowed_agent_tools()
        log.debug(f"Firewall allowed tools: {len(allowed)} tools")
        return allowed

    def get_memory(self) -> Memory:
        """Get the memory instance for direct access."""
        return self.memory

    def get_tools(self) -> ToolRegistry:
        """Get the tool registry for direct access."""
        return self.tools

    def reset_memory(self) -> None:
        """Reset memory to defaults (clear scratchpad, identity)."""
        self.memory.save_scratchpad(self.memory._default_scratchpad())
        self.memory.save_identity(self.memory._default_identity())
        log.info("Agent memory reset to defaults")

    def start_consciousness(self, emit_progress: Optional[Callable[[str], None]] = None) -> None:
        """
        Start background consciousness if enabled.

        Args:
            emit_progress: Optional callback for consciousness events
        """
        if not self._consciousness_enabled:
            log.debug("Background consciousness not enabled")
            return

        if self._consciousness is not None:
            log.warning("Consciousness already running")
            return

        from .consciousness import BackgroundConsciousness

        self._consciousness = BackgroundConsciousness(
            agent=self,
            emit_progress=emit_progress,
        )
        self._consciousness.start()
        log.info("Background consciousness started")

    def stop_consciousness(self) -> None:
        """Stop background consciousness."""
        if self._consciousness is not None:
            self._consciousness.stop()
            self._consciousness = None
            log.info("Background consciousness stopped")

    def is_consciousness_running(self) -> bool:
        """Check if consciousness is running."""
        return self._consciousness is not None and self._consciousness.is_running()

    # -------------------------------------------------------------------------
    # Task Queue Methods
    # -------------------------------------------------------------------------

    async def start_task_processor(self) -> None:
        """Start background task processor."""
        if not self._queue_enabled:
            log.debug("Task queue not enabled")
            return

        # Set callbacks
        async def on_task_start(task: Task) -> None:
            await self.events.emit(EventType.TASK_STARTED, {
                "task_id": task.id,
                "task_type": task.task_type,
            })

        async def on_task_complete(task: Task) -> None:
            await self.events.emit(EventType.TASK_COMPLETED, {
                "task_id": task.id,
                "task_type": task.task_type,
                "result": task.result[:500] if task.result else None,
            })

        async def on_task_failed(task: Task) -> None:
            await self.events.emit(EventType.TASK_FAILED, {
                "task_id": task.id,
                "task_type": task.task_type,
                "error": task.error[:500] if task.error else None,
            })

        self.queue.set_callbacks(
            on_task_start=on_task_start,
            on_task_complete=on_task_complete,
            on_task_failed=on_task_failed,
        )

        await self.queue.start_processor(self._execute_task)
        log.info("Task processor started")

    def stop_task_processor(self) -> None:
        """Stop background task processor."""
        self.queue.stop_processor()
        log.info("Task processor stopped")

    async def _execute_task(self, task: Task) -> str:
        """
        Execute a queued task.

        Args:
            task: The task to execute

        Returns:
            Task result string
        """
        if task.task_type == "chat":
            return await self.process(
                task.data.get("text", ""),
                conversation_id=task.id,
                dpc_context=task.data.get("dpc_context"),
            )
        elif task.task_type == "improvement":
            # Execute planned improvement via evolution
            if self._evolution:
                cycle = await self._evolution.run_evolution_cycle()
                return cycle.description
            return "Evolution not enabled"
        elif task.task_type == "review":
            # Run code review
            return await self._execute_review(task.data)
        else:
            return f"Unknown task type: {task.task_type}"

    async def _execute_review(self, data: Dict[str, Any]) -> str:
        """Execute a code review task."""
        # TODO: Implement review logic
        return "Review not implemented"

    def schedule_task(
        self,
        task_type: str,
        data: Dict[str, Any],
        priority: TaskPriority = TaskPriority.NORMAL,
        delay_seconds: int = 0,
    ) -> Task:
        """
        Schedule a task for execution.

        Args:
            task_type: Type of task ('chat', 'improvement', 'review')
            data: Task payload
            priority: Task priority
            delay_seconds: Delay before execution

        Returns:
            Scheduled task
        """
        from datetime import datetime, timedelta

        scheduled_at = None
        if delay_seconds > 0:
            scheduled_at = datetime.utcnow() + timedelta(seconds=delay_seconds)

        task = self.queue.schedule(
            task_type=task_type,
            data=data,
            priority=priority,
            scheduled_at=scheduled_at,
        )

        # Emit event
        asyncio.create_task(self.events.emit(EventType.TASK_SCHEDULED, {
            "task_id": task.id,
            "task_type": task_type,
            "priority": priority.name,
            "delay_seconds": delay_seconds,
        }))

        return task

    # -------------------------------------------------------------------------
    # Evolution Methods
    # -------------------------------------------------------------------------

    def start_evolution(self) -> None:
        """Start automatic evolution cycles."""
        if not self._evolution_enabled:
            log.debug("Evolution not enabled")
            return

        if self._evolution is not None:
            log.warning("Evolution already running")
            return

        from .evolution import EvolutionManager

        self._evolution = EvolutionManager(
            agent=self,
            enabled=self._evolution_enabled,
            interval_minutes=self.config.evolution_interval_minutes,
            auto_apply=self.config.evolution_auto_apply,
        )
        self._evolution.start_automatic_evolution()
        log.info("Evolution started")

    def stop_evolution(self) -> None:
        """Stop automatic evolution."""
        if self._evolution is not None:
            self._evolution.stop_automatic_evolution()
            self._evolution = None
            log.info("Evolution stopped")

    def is_evolution_running(self) -> bool:
        """Check if evolution is running."""
        return self._evolution is not None and self._evolution.is_running()

    def get_pending_evolution_changes(self) -> List[Dict[str, Any]]:
        """Get pending evolution changes awaiting approval."""
        if self._evolution:
            return self._evolution.get_pending_changes()
        return []

    async def approve_evolution_change(self, change_id: str) -> bool:
        """Approve a pending evolution change."""
        if self._evolution:
            return await self._evolution.approve_change(change_id)
        return False

    def reject_evolution_change(self, change_id: str) -> bool:
        """Reject a pending evolution change."""
        if self._evolution:
            return self._evolution.reject_change(change_id)
        return False

    def get_status(self) -> Dict[str, Any]:
        """Get agent status info."""
        state_path = self.agent_root / "state" / "state.json"
        state = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        return {
            "agent_root": str(self.agent_root),
            "budget_usd": self.config.budget_usd,
            "spent_usd": state.get("spent_usd", 0),
            "remaining_usd": max(0, self.config.budget_usd - state.get("spent_usd", 0)),
            "max_rounds": self.config.max_rounds,
            "billing_model": self.config.billing_model,
            "tools_available": self.tools.available_tools(),
            "memory_files": {
                "scratchpad": self.memory.scratchpad_path().exists(),
                "identity": self.memory.identity_path().exists(),
                "dialogue_summary": self.memory.dialogue_summary_path().exists(),
            },
            "task_queue": {
                "enabled": self._queue_enabled,
                "running": self.queue.is_running(),
                "stats": self.queue.get_stats(),
            },
            "consciousness": {
                "enabled": self._consciousness_enabled,
                "running": self.is_consciousness_running(),
                "status": self._consciousness.get_status() if self._consciousness else None,
            },
            "evolution": {
                "enabled": self._evolution_enabled,
                "running": self.is_evolution_running(),
                "status": self._evolution.get_status() if self._evolution else None,
                "pending_changes": len(self.get_pending_evolution_changes()),
            },
        }
