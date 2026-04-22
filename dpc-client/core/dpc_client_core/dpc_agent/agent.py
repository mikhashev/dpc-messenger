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

import asyncio
import json
import logging
import pathlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from .llm_adapter import DpcLlmAdapter
from .tools.registry import ToolRegistry, ToolContext
from .memory import Memory, generate_smart_index
from .skill_store import SkillStore
from .skill_reflection import SkillReflector, REFLECTION_ROUNDS_THRESHOLD
from .context import build_llm_messages
from .loop import run_llm_loop
from .utils import (
    get_agent_root, ensure_agent_dirs, utc_now_iso, append_jsonl
)
from .task_queue import TaskQueue, TaskPriority, Task
from .task_types import TaskTypeDefinition, BUILTIN_TASK_TYPES
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
    consciousness_budget_fraction: float = 0.1

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
        provider_alias: Optional[str] = None,  # Per-agent provider override (Phase 3)
        firewall_profile: Optional[str] = None,  # Per-agent permission profile (Phase 2)
        service: Optional[Any] = None,  # CoreService reference for commit proposals
        compute_host: str = "",  # Optional remote peer node_id for LLM inference
    ):
        """
        Initialize the agent.

        Args:
            llm_manager: DPC's LLMManager instance
            config: Agent configuration
            agent_root: Storage root (defaults to ~/.dpc/agent/)
            firewall: ContextFirewall instance for tool permissions
            provider_alias: Specific LLM provider to use (overrides agent_provider)
            firewall_profile: Permission profile name from privacy_rules.json
        """
        self.config = config or AgentConfig()
        self.agent_root = agent_root or get_agent_root("default")
        self._firewall = firewall  # Firewall controls tool access
        self._provider_alias = provider_alias  # Store for LLM adapter
        self._firewall_profile = firewall_profile  # Store for tool permission lookups
        self._service = service  # CoreService — used by tools that need firewall access
        # Note: ensure_agent_dirs() is already called by DpcAgentManager, so we don't call it here

        # Initialize components
        self.llm = DpcLlmAdapter(llm_manager, provider_alias=provider_alias, compute_host=compute_host)
        self.tools = ToolRegistry(agent_root=self.agent_root)
        self.memory = Memory(agent_root=self.agent_root)
        generate_smart_index(self.agent_root / "knowledge")
        self.memory.cleanup_old_task_results(max_age_days=30)  # TTL cleanup
        self.skill_store = SkillStore(agent_root=self.agent_root)
        self.skill_store.ensure_starter_skills()  # bootstrap for existing agents on first run
        self.skill_reflector = SkillReflector(
            skill_store=self.skill_store,
            llm=self.llm,
            firewall=firewall,
            firewall_profile=firewall_profile,
        )

        from .memory import EmbeddingProvider
        self._embedding_provider = EmbeddingProvider(local_files_only=True)

        # Task queue for background execution
        self.queue = TaskQueue(self.agent_root)
        self._queue_enabled = self.config.enable_task_queue

        # Custom task handlers (extensible)
        self._task_handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}

        # Task type registry (agent-defined task types)
        self._task_type_registry: Dict[str, TaskTypeDefinition] = {}
        self._load_task_type_registry()  # Load persisted task types

        # Event emitter for notifications (per-agent instance with correct storage)
        from .events import AgentEventEmitter
        self.events = AgentEventEmitter(agent_root=self.agent_root, persist_events=True)

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

        # Callback set by agent_manager to deliver scheduled task results to Telegram
        self._telegram_send_fn: Optional[Any] = None

        # Flag: user interaction in progress — consciousness/evolution should yield
        self._user_active = False

        # Background consciousness (optional)
        self._consciousness: Optional["BackgroundConsciousness"] = None
        self._consciousness_enabled = self.config.background_consciousness

        # Track last usage for session state access by agent_manager
        self._last_usage: Optional[Dict[str, Any]] = None
        # Track last full context estimate (from build_llm_messages cap_info)
        self._last_cap_info: Optional[Dict[str, Any]] = None

        log.info(f"DpcAgent initialized with storage at {self.agent_root}")

    async def process(
        self,
        message: str,
        conversation_id: str,
        dpc_context: Optional[Dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        emit_progress: Optional[Callable[[str], None]] = None,
        on_stream_chunk: Optional[Callable[[str, str], None]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        conversation_monitor: Optional[Any] = None,
        # Image parameters for vision queries
        image_base64: Optional[str] = None,
        image_mime: str = "image/png",
        image_caption: Optional[str] = None,
        # Unique per-message task ID (distinct from conversation_id)
        task_id: Optional[str] = None,
        # Reply routing: when set, injected into ToolContext so schedule_task
        # can propagate it into task data for automatic result delivery.
        reply_telegram_chat_id: Optional[str] = None,
    ) -> str:
        """
        Process a user message and return response.

        Args:
            message: User's message text
            conversation_id: Unique ID for this conversation
            dpc_context: Optional DPC context (personal, device)
            system_prompt: Optional custom system prompt
            emit_progress: Optional callback for progress updates
            on_stream_chunk: Optional async callback for streaming: await on_stream_chunk(chunk, conversation_id)
            session_state: Optional session state from ConversationMonitor
                          (tokens_used, tokens_limit, usage_percent, etc.)
            conversation_monitor: Optional ConversationMonitor for knowledge extraction
            image_base64: Optional base64-encoded image data for vision queries
            image_mime: MIME type of the image (default: image/png)
            image_caption: Optional caption for the image

        Returns:
            Agent's response text
        """
        task = {
            "id": conversation_id,
            "type": "chat",
            "text": message,
            # Image fields for context.py:_build_user_content()
            "image_base64": image_base64,
            "image_mime": image_mime,
            "image_caption": image_caption,
        }

        # Build LLM context — pass prior conversation turns (all except current user msg,
        # which was added to the monitor just before this call, so it's the last entry)
        prior_history = None
        if conversation_monitor is not None:
            full_history = conversation_monitor.get_message_history()
            if len(full_history) > 1:
                prior_history = full_history[:-1]  # exclude the current user message

        # Get firewall-controlled tool access (needed for both context and tool registry)
        allowed_tools = self._get_allowed_tools()

        # Collect firewall metadata for capabilities section (transparency)
        all_tools_map = None
        sandbox_ro = None
        sandbox_rw = None
        if self._firewall is not None:
            all_tools_map = dict(self._firewall.dpc_agent_tools)
            sandbox_ro = list(self._firewall.sandbox_read_only_paths)
            sandbox_rw = list(self._firewall.sandbox_read_write_paths)

        messages, cap_info = build_llm_messages(
            agent_root=self.agent_root,
            memory=self.memory,
            task=task,
            system_prompt=system_prompt,
            dpc_context=dpc_context,
            session_state=session_state,
            conversation_history=prior_history,
            skill_store=self.skill_store,
            allowed_tools=allowed_tools,
            all_tools=all_tools_map,
            sandbox_read_only=sandbox_ro,
            sandbox_read_write=sandbox_rw,
            embedding_provider=self._embedding_provider,
        )

        # Store cap_info for agent_manager to include in next request's session_state
        self._last_cap_info = cap_info

        # Log context window usage — warn if approaching limit or sections were trimmed
        _estimated = cap_info.get("estimated_tokens_before", 0)
        _ctx_window = (session_state or {}).get("tokens_limit", 200000) or 200000
        _trimmed = cap_info.get("trimmed_sections", [])
        if _trimmed:
            log.warning(
                "Context trimmed (approaching limit): %s | estimated: %d / %d tokens (%.0f%%)",
                _trimmed, _estimated, _ctx_window, _estimated / _ctx_window * 100,
            )
        elif _estimated > _ctx_window * 0.8:
            log.warning(
                "Context window >80%% full: estimated %d / %d tokens (%.0f%%)",
                _estimated, _ctx_window, _estimated / _ctx_window * 100,
            )
        else:
            log.debug(
                "Context size: estimated %d / %d tokens (%.0f%%)",
                _estimated, _ctx_window, _estimated / _ctx_window * 100,
            )
        ctx = ToolContext(
            agent_root=self.agent_root,
            current_task_id=conversation_id,
            current_task_type="chat",
            tool_whitelist=allowed_tools,
            emit_progress_fn=emit_progress or (lambda msg, tool=None, rnd=None: None),
            firewall=self._firewall,  # For extended sandbox paths
            conversation_monitor=conversation_monitor,  # For knowledge extraction tool
            reply_telegram_chat_id=reply_telegram_chat_id,
            skill_store=self.skill_store,  # For execute_skill tool
            dpc_service=self._service,  # For firewall checks
        )
        # Store main event loop so sync tools running in executor threads can schedule
        # async calls back onto it via asyncio.run_coroutine_threadsafe.
        ctx.agent_event_loop = asyncio.get_event_loop()
        ctx._agent = self  # Enable schedule_task and other agent-dependent tools
        self.tools.set_context(ctx)

        # Use provided task_id or generate one; never use conversation_id as task identity
        import uuid as _uuid
        import json as _json
        event_task_id = task_id or f"chat-{_uuid.uuid4().hex[:8]}"
        started_at = utc_now_iso()

        # Log task start
        append_jsonl(self.agent_root / "logs" / "events.jsonl", {
            "ts": started_at,
            "type": "task_start",
            "task_id": event_task_id,
            "conversation_id": conversation_id,
            "text_preview": message[:200] if message else "",
        })

        # Run LLM loop
        response, usage, trace = await run_llm_loop(
            messages=messages,
            tools=self.tools,
            llm=self.llm,
            agent_root=self.agent_root,
            emit_progress=emit_progress or (lambda msg, tool=None, rnd=None: None),
            task_id=conversation_id,
            budget_remaining_usd=self.config.budget_usd,
            max_rounds=self.config.max_rounds,
            on_stream_chunk=on_stream_chunk,
            conversation_id=conversation_id,
        )

        # Store last usage for session state access by agent_manager
        self._last_usage = usage

        # Phase 4: Decision extraction (MEM-4.2) — async, non-blocking
        trigger_events = trace.get("trigger_events", [])
        if trigger_events:
            from .decision_proposals import extract_decisions
            # Use memory_provider if configured, else fall back to agent provider
            extraction_llm = self.llm
            from .memory_config import get_memory_config
            mem_cfg = get_memory_config(
                json.loads((self.agent_root / "config.json").read_text(encoding="utf-8"))
                if (self.agent_root / "config.json").exists() else {}
            )
            if mem_cfg.memory_provider:
                extraction_llm = DpcLlmAdapter(
                    self.llm._llm_manager,
                    provider_alias=mem_cfg.memory_provider,
                )
            asyncio.ensure_future(extract_decisions(
                llm=extraction_llm,
                conversation_messages=messages,
                trigger_events=trigger_events,
                agent_root=self.agent_root,
                session_id=conversation_id or "",
            ))

        # Phase 3: Skill Write phase — record outcomes, optionally reflect
        used_skills = self.skill_reflector.record_outcome(trace, usage)
        if used_skills and usage.get("rounds", 0) >= REFLECTION_ROUNDS_THRESHOLD:
            asyncio.ensure_future(
                self.skill_reflector.reflect_async(
                    skill_name=used_skills[0],
                    task_text=message,
                    llm_trace=trace,
                    usage=usage,
                )
            )

        completed_at = utc_now_iso()

        # Log task completion
        append_jsonl(self.agent_root / "logs" / "events.jsonl", {
            "ts": completed_at,
            "type": "task_complete",
            "task_id": event_task_id,
            "conversation_id": conversation_id,
            "response_preview": response[:200] if response else "",
            "rounds": usage.get("rounds", 0),
            "cost_usd": usage.get("cost", 0),
            "tokens_estimated_total": cap_info.get("estimated_tokens_before", 0),
            "tokens_context_window": _ctx_window,
            "context_trimmed": bool(_trimmed),
        })

        # Persist full task result to task_results/{task_id}.json
        try:
            results_dir = self.agent_root / "task_results"
            results_dir.mkdir(exist_ok=True)
            result_data = {
                "task_id": event_task_id,
                "conversation_id": conversation_id,
                "task_type": "chat",
                "started_at": started_at,
                "completed_at": completed_at,
                "prompt": message[:2000] if message else "",
                "response": response or "",
                "rounds": usage.get("rounds", 0),
                "cost_usd": usage.get("cost", 0),
                "tokens": usage.get("tokens", {}),
            }
            (results_dir / f"{event_task_id}.json").write_text(
                _json.dumps(result_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("Failed to save task result file: %s", e)

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

        if self._firewall_profile:
            allowed = self._firewall.get_allowed_agent_tools_for_profile(self._firewall_profile)
            log.debug(f"Firewall allowed tools (profile={self._firewall_profile}): {len(allowed)} tools")
        else:
            allowed = self._firewall.get_allowed_agent_tools()
            log.debug(f"Firewall allowed tools (global): {len(allowed)} tools")
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
            budget_fraction=self.config.consciousness_budget_fraction,
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

    def archive_old_task_results(self, max_age_hours: int = 24) -> int:
        """Archive task_results older than max_age_hours into daily JSONL files.

        Moves individual JSON files from task_results/ into
        task_results/archive/YYYY-MM-DD.jsonl (one JSON object per line).
        Returns number of files archived.
        """
        import json as _json
        from datetime import datetime, timezone, timedelta

        results_dir = self.agent_root / "task_results"
        if not results_dir.exists():
            return 0

        archive_dir = results_dir / "archive"
        archive_dir.mkdir(exist_ok=True)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        archived = 0

        for f in sorted(results_dir.glob("*.json")):
            try:
                data = _json.loads(f.read_text(encoding="utf-8"))
                completed = data.get("completed_at") or data.get("started_at", "")
                if not completed:
                    continue
                ts = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                if ts >= cutoff:
                    continue

                # Append to daily archive file
                day_str = ts.strftime("%Y-%m-%d")
                archive_file = archive_dir / f"{day_str}.jsonl"
                with open(archive_file, "a", encoding="utf-8") as af:
                    af.write(_json.dumps(data, ensure_ascii=False) + "\n")

                f.unlink()
                archived += 1
            except Exception as e:
                log.debug("Skipping task result %s: %s", f.name, e)

        if archived:
            log.info("Archived %d old task results (>%dh)", archived, max_age_hours)
        return archived

    # -------------------------------------------------------------------------
    # Task Queue Methods
    # -------------------------------------------------------------------------

    async def start_task_processor(self) -> None:
        """Start background task processor."""
        if not self._queue_enabled:
            log.debug("Task queue not enabled")
            return

        # Archive old task results on startup
        try:
            self.archive_old_task_results(max_age_hours=24)
        except Exception as e:
            log.warning("Task result archival failed: %s", e)

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
            # Persist full result (scheduled tasks use task.id directly as task_id)
            try:
                import json as _json
                results_dir = self.agent_root / "task_results"
                results_dir.mkdir(exist_ok=True)
                result_data = {
                    "task_id": task.id,
                    "task_type": task.task_type,
                    "started_at": getattr(task, "started_at", None),
                    "completed_at": utc_now_iso(),
                    "prompt": str(task.data)[:2000] if task.data else "",
                    "response": task.result or "",
                    "rounds": 0,
                    "cost_usd": 0.0,
                }
                (results_dir / f"{task.id}.json").write_text(
                    _json.dumps(result_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                log.warning("Failed to save scheduled task result: %s", e)

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

    def register_task_handler(
        self,
        task_type: str,
        handler: Callable[[Dict[str, Any]], Any],
    ) -> None:
        """
        Register a custom task handler.

        Args:
            task_type: Task type name (e.g., "weather_forecast")
            handler: Async or sync function that takes task data dict and returns result

        Example:
            async def handle_weather(data):
                location = data.get("location")
                return f"Weather for {location}: Sunny"

            agent.register_task_handler("weather_forecast", handle_weather)
        """
        self._task_handlers[task_type] = handler
        log.info(f"Registered task handler for type: {task_type}")

    def unregister_task_handler(self, task_type: str) -> bool:
        """
        Unregister a custom task handler.

        Args:
            task_type: Task type to unregister

        Returns:
            True if handler was removed, False if not found
        """
        if task_type in self._task_handlers:
            del self._task_handlers[task_type]
            log.info(f"Unregistered task handler for type: {task_type}")
            return True
        return False

    # -----------------------------------------------------------------------
    # Task Type Registry Methods
    # -----------------------------------------------------------------------

    def _load_task_type_registry(self) -> None:
        """Load task type registry from disk."""
        registry_path = self.agent_root / "state" / "task_types.json"
        if registry_path.exists():
            try:
                with open(registry_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for task_type, definition_data in data.get("task_types", {}).items():
                    self._task_type_registry[task_type] = TaskTypeDefinition.from_dict(definition_data)
                log.info(f"Loaded {len(self._task_type_registry)} task types from registry")
            except Exception as e:
                log.warning(f"Failed to load task type registry: {e}")

    def _save_task_type_registry(self) -> None:
        """Save task type registry to disk."""
        registry_path = self.agent_root / "state" / "task_types.json"
        try:
            data = {
                "task_types": {
                    tt: definition.to_dict()
                    for tt, definition in self._task_type_registry.items()
                },
                "updated_at": utc_now_iso(),
            }
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            with open(registry_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log.debug(f"Saved {len(self._task_type_registry)} task types to registry")
        except Exception as e:
            log.error(f"Failed to save task type registry: {e}")

    def register_task_type(
        self,
        task_type: str,
        description: str,
        execution_prompt: str,
        input_schema: Optional[Dict[str, Any]] = None,
        examples: Optional[List[Dict[str, Any]]] = None,
    ) -> TaskTypeDefinition:
        """
        Register a custom task type with execution instructions.

        When a task of this type executes, the agent will follow the
        execution_prompt with task.data variables substituted.

        Args:
            task_type: Unique identifier (e.g., "weather_report")
            description: What this task type does
            execution_prompt: Instructions for the agent to follow. Use {variable}
                             placeholders that will be filled from task.data
            input_schema: JSON schema for validating task.data (optional)
            examples: Example task.data payloads (optional)

        Returns:
            The created TaskTypeDefinition

        Example:
            agent.register_task_type(
                task_type="weather_report",
                description="Fetch weather for a location",
                execution_prompt="Fetch current weather for {location} and summarize it.",
                input_schema={"type": "object", "properties": {"location": {"type": "string"}}},
            )
        """
        definition = TaskTypeDefinition(
            task_type=task_type,
            description=description,
            execution_prompt=execution_prompt,
            input_schema=input_schema or {},
            examples=examples or [],
        )

        # Store in registry
        self._task_type_registry[task_type] = definition

        # Auto-register a handler that runs the agent with the formatted prompt
        self._task_handlers[task_type] = self._create_task_type_handler(definition)

        # Persist to disk
        self._save_task_type_registry()

        log.info(f"Registered task type: {task_type}")
        return definition

    def unregister_task_type(self, task_type: str) -> bool:
        """
        Unregister a custom task type.

        Args:
            task_type: Task type to unregister

        Returns:
            True if type was removed, False if not found
        """
        if task_type in self._task_type_registry:
            del self._task_type_registry[task_type]
            # Also remove the auto-registered handler
            if task_type in self._task_handlers:
                del self._task_handlers[task_type]
            self._save_task_type_registry()
            log.info(f"Unregistered task type: {task_type}")
            return True
        return False

    def get_task_type(self, task_type: str) -> Optional[TaskTypeDefinition]:
        """
        Get a task type definition.

        Args:
            task_type: Task type name

        Returns:
            TaskTypeDefinition if found, None otherwise
        """
        # Check custom registry first
        if task_type in self._task_type_registry:
            return self._task_type_registry[task_type]
        # Then check built-in types
        if task_type in BUILTIN_TASK_TYPES:
            return BUILTIN_TASK_TYPES[task_type]
        return None

    def list_task_types(self) -> Dict[str, TaskTypeDefinition]:
        """
        List all registered task types (custom + built-in).

        Returns:
            Dictionary of task_type -> TaskTypeDefinition
        """
        result = dict(BUILTIN_TASK_TYPES)
        result.update(self._task_type_registry)
        return result

    def _create_task_type_handler(
        self, definition: TaskTypeDefinition
    ) -> Callable[[Dict[str, Any]], Any]:
        """
        Create a handler function for a task type definition.

        The handler formats the execution_prompt with task data and
        runs the agent with that prompt.

        Args:
            definition: TaskTypeDefinition with execution instructions

        Returns:
            Async handler function
        """
        agent = self  # Capture reference

        async def handler(task_data: Dict[str, Any]) -> str:
            # Format the prompt with task data
            prompt = definition.format_prompt(task_data)

            # Run the agent with the formatted prompt
            result = await agent.process(
                message=prompt,
                conversation_id=f"task-{definition.task_type}",
            )
            return result

        return handler

    def _convert_task_data_to_prompt(self, task_data: Dict[str, Any]) -> str:
        """
        Convert structured task data to a readable prompt.

        This handles cases where an agent schedules a 'chat' task with
        structured data instead of plain text. The structured data is
        converted to a clear instruction for the agent to follow.

        Args:
            task_data: Structured task data dictionary

        Returns:
            A readable prompt string
        """
        # If task_data has a 'type' field, use it to construct a clear instruction
        task_type = task_data.get("type", "")
        action = task_data.get("action", "")

        if task_type == "weather_request" or "weather" in task_type.lower():
            location = task_data.get("location", "unknown location")
            return f"Execute the scheduled task: Fetch current weather for {location} and provide a summary. If Telegram bridge is available, send the result to the user."

        elif task_type == "reminder":
            message = task_data.get("message", "")
            return f"Execute the scheduled task: Remind the user: {message}"

        elif action:
            # Generic action-based prompt
            return f"Execute the scheduled task: {action}. Task data: {json.dumps(task_data, ensure_ascii=False)}"

        elif task_type:
            # Generic type-based prompt
            return f"Execute the scheduled task of type '{task_type}'. Task data: {json.dumps(task_data, ensure_ascii=False)}"

        else:
            # Fallback: convert entire dict to readable prompt
            return f"Execute the following scheduled task: {json.dumps(task_data, ensure_ascii=False)}"

    async def _execute_task(self, task: Task) -> str:
        """
        Execute a queued task.

        Args:
            task: The task to execute

        Returns:
            Task result string
        """
        # Check custom handlers first
        if task.task_type in self._task_handlers:
            handler = self._task_handlers[task.task_type]
            try:
                import asyncio
                result = handler(task.data)
                # Support both sync and async handlers
                if asyncio.iscoroutine(result):
                    result = await result
                return str(result)
            except Exception as e:
                log.error(f"Task handler error for {task.task_type}: {e}")
                return f"Handler error: {e}"

        # Built-in task types
        if task.task_type == "chat":
            # Get text from task data, or convert structured data to a prompt
            text = task.data.get("text", "")
            if not text:
                # If no 'text' field, convert structured data to a readable prompt
                # This handles cases where agent passes structured data to chat task
                text = self._convert_task_data_to_prompt(task.data)

            # Use the originating conversation_id so streaming progress and the
            # final history update appear in the correct chat (not a dead task.id).
            reply_conversation_id = task.data.get("_reply_conversation_id") or task.id
            reply_telegram_chat_id = task.data.get("_reply_telegram_chat_id")

            result = await self.process(
                text,
                conversation_id=reply_conversation_id,
                dpc_context=task.data.get("dpc_context"),
                reply_telegram_chat_id=reply_telegram_chat_id,
            )

            # Deliver result back to Telegram if the task originated from there
            if reply_telegram_chat_id:
                send_fn = getattr(self, "_telegram_send_fn", None)
                if send_fn:
                    try:
                        await send_fn(reply_telegram_chat_id, result)
                    except Exception as e:
                        log.warning("Failed to deliver task result to Telegram: %s", e)

            return result
        elif task.task_type == "reminder":
            # Deliver reminder message directly — no LLM call to prevent scheduling loops
            message = task.data.get("message", task.data.get("text", ""))
            if not message:
                return "Reminder task has no message"

            reply_telegram_chat_id = task.data.get("_reply_telegram_chat_id")
            formatted = f"⏰ Reminder: {message}"

            # Send to Telegram chat if available
            if reply_telegram_chat_id:
                send_fn = getattr(self, "_telegram_send_fn", None)
                if send_fn:
                    try:
                        await send_fn(reply_telegram_chat_id, formatted)
                    except Exception as e:
                        log.warning("Failed to deliver reminder to Telegram: %s", e)

            # Also broadcast via event system (e.g. DPC UI notifications)
            try:
                from .events import emit_agent_message
                await emit_agent_message(formatted, priority="high", agent_id=self.agent_root.name)
            except Exception as e:
                log.warning("Failed to emit reminder event: %s", e)

            log.info("Reminder delivered for task %s: %s", task.id, message[:80])
            return f"Reminder sent: {message}"
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
            return f"Unknown task type: {task.task_type}. Register a handler with register_task_handler('{task.task_type}', handler)"

    async def _execute_review(self, data: Dict[str, Any]) -> str:
        """Execute a code review task using self_review + request_critique tools."""
        target = data.get("target", data.get("text", ""))
        focus = data.get("focus", "")
        reply_conversation_id = data.get("_reply_conversation_id") or "review_task"
        reply_telegram_chat_id = data.get("_reply_telegram_chat_id")

        if not target:
            return "Review task requires a 'target' field (file path, code snippet, or description)"

        focus_clause = f" Focus on: {focus}." if focus else ""
        prompt = (
            f"Please review the following:{focus_clause}\n\n"
            f"{target}\n\n"
            f"Use your self_review and request_critique tools to produce a thorough review "
            f"covering correctness, quality, edge cases, and any improvements."
        )

        result = await self.process(
            prompt,
            conversation_id=reply_conversation_id,
        )

        if reply_telegram_chat_id:
            send_fn = getattr(self, "_telegram_send_fn", None)
            if send_fn:
                try:
                    await send_fn(reply_telegram_chat_id, result)
                except Exception as e:
                    log.warning("Failed to deliver review result to Telegram: %s", e)

        return result

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
        from datetime import datetime, timedelta, timezone

        scheduled_at = None
        if delay_seconds > 0:
            scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)

        task = self.queue.schedule(
            task_type=task_type,
            data=data,
            priority=priority,
            scheduled_at=scheduled_at,
        )

        # Emit event (only if there's a running event loop)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.events.emit(EventType.TASK_SCHEDULED, {
                "task_id": task.id,
                "task_type": task_type,
                "priority": priority.name,
                "delay_seconds": delay_seconds,
            }))
        except RuntimeError:
            # No running event loop - skip event emission
            log.debug(f"Task {task.id} scheduled (no event loop for emission)")

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
