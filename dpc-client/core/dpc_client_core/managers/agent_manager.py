"""
DPC Agent Manager - Integration layer between DPC and embedded agent.

Manages:
- Agent lifecycle (initialization, shutdown)
- Configuration loading
- Context integration with DPC
- Event forwarding to DPC UI
- Telegram notifications for agent events

This manager bridges the embedded DpcAgent with DPC Messenger's CoreService.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import TYPE_CHECKING, Any, Dict, Optional

from ..dpc_agent.agent import DpcAgent, AgentConfig
from ..dpc_agent.utils import get_agent_root, ensure_agent_dirs, utc_now_iso
from ..dpc_agent.events import get_event_emitter, EventType
from ..conversation_monitor import ConversationMonitor

if TYPE_CHECKING:
    from ..service import CoreService
    from ..llm_manager import LLMManager

log = logging.getLogger(__name__)


class DpcAgentManager:
    """
    Manages the embedded agent within DPC Messenger.

    This is the main integration point between DPC and the autonomous agent.
    It handles:
    - Agent initialization with DPC's LLMManager
    - Configuration from DPC settings
    - Context integration (personal, device context from DPC)
    - Event forwarding to DPC's UI
    - Telegram notifications for agent monitoring
    """

    def __init__(self, service: "CoreService", config: Dict[str, Any]):
        """
        Initialize the agent manager.

        Args:
            service: DPC CoreService instance
            config: Agent configuration from settings
        """
        self.service = service
        self.config = config

        # Get firewall reference from CoreService
        self.firewall = getattr(service, "firewall", None)

        # Storage paths
        self.agent_root = get_agent_root()
        ensure_agent_dirs()

        # Agent instance (lazy initialization)
        self._agent: Optional[DpcAgent] = None

        # Telegram bridge for notifications
        self._telegram_bridge = None

        # Conversation monitors for agent sessions (reuse existing ConversationMonitor)
        # Key: conversation_id, Value: ConversationMonitor instance
        self._agent_monitors: Dict[str, ConversationMonitor] = {}

        log.info(f"DpcAgentManager initialized with storage at {self.agent_root}")

    @property
    def agent(self) -> DpcAgent:
        """Get the agent instance, initializing if needed."""
        if self._agent is None:
            raise RuntimeError("Agent not initialized. Call start() first.")
        return self._agent

    async def start(self) -> None:
        """Initialize the agent and Telegram bridge."""
        if self._agent is not None:
            log.warning("Agent already initialized")
            return

        # Check if agent is enabled via firewall
        if self.firewall and not self.firewall.dpc_agent_enabled:
            log.warning("DPC Agent is disabled via firewall - not starting")
            return

        # Build agent config (tool control is via firewall, not config)
        # Evolution settings come from firewall (privacy_rules.json), not provider config
        evolution_enabled = self.firewall.evolution_enabled if self.firewall else False
        evolution_interval = self.firewall.evolution_interval_minutes if self.firewall else 60
        evolution_auto = self.firewall.evolution_auto_apply if self.firewall else False

        agent_config = AgentConfig(
            budget_usd=self.config.get("budget_usd", 50.0),
            max_rounds=self.config.get("max_rounds", 200),
            background_consciousness=self.config.get("background_consciousness", False),
            enable_task_queue=self.config.get("enable_task_queue", True),
            evolution_enabled=evolution_enabled,
            evolution_interval_minutes=evolution_interval,
            evolution_auto_apply=evolution_auto,
            billing_model=self.config.get("billing_model", "subscription"),
        )

        # Get LLMManager from CoreService
        llm_manager = getattr(self.service, "llm_manager", None)
        if llm_manager is None:
            raise RuntimeError("CoreService does not have llm_manager")

        # Create agent with firewall for tool control
        self._agent = DpcAgent(
            llm_manager=llm_manager,
            config=agent_config,
            agent_root=self.agent_root,
            firewall=self.firewall,  # Firewall controls tool access
        )

        # Start background consciousness if enabled
        if agent_config.background_consciousness:
            self._agent.start_consciousness(emit_progress=self._emit_progress)
            log.info("Background consciousness started")

        # Start evolution if enabled
        if agent_config.evolution_enabled:
            self._agent.start_evolution()
            log.info(f"Evolution started (interval={agent_config.evolution_interval_minutes}min, auto_apply={agent_config.evolution_auto_apply})")

        # Start task processor if enabled
        if agent_config.enable_task_queue:
            await self._agent.start_task_processor()
            log.info("Task processor started")

        # Initialize Telegram bridge for agent notifications
        await self._start_telegram_bridge()

        log.info("DpcAgent started successfully")

    async def _start_telegram_bridge(self) -> None:
        """Initialize and start the Telegram notification bridge."""
        # Get Telegram config from settings
        settings = getattr(self.service, "settings", None)
        if settings is None:
            log.debug("No settings available, skipping Telegram bridge")
            return

        telegram_config = settings.get_dpc_agent_telegram_config()

        if not telegram_config.get("enabled", False):
            log.debug("Telegram notifications for agent disabled")
            return

        bot_token = telegram_config.get("bot_token", "")
        chat_ids = telegram_config.get("allowed_chat_ids", [])

        if not bot_token or not chat_ids:
            log.warning("Telegram bridge enabled but missing bot_token or allowed_chat_ids")
            return

        try:
            from .agent_telegram_bridge import AgentTelegramBridge, create_telegram_bridge_callback

            # Get event filter
            event_filter = telegram_config.get("event_filter")

            # Get transcription setting
            transcription_enabled = telegram_config.get("transcription_enabled", True)

            # Create bridge
            self._telegram_bridge = AgentTelegramBridge(
                bot_token=bot_token,
                allowed_chat_ids=chat_ids,
                event_filter=event_filter,
                transcription_enabled=transcription_enabled,
            )

            # Set message handler for two-way communication
            self._telegram_bridge.set_message_handler(
                handler=self.process_message,
                agent_manager=self,
            )

            # Start bridge
            success = await self._telegram_bridge.start()
            if not success:
                log.warning("Failed to start Telegram bridge")
                self._telegram_bridge = None
                return

            # Connect to event emitter
            emitter = get_event_emitter()
            emitter.add_listener(create_telegram_bridge_callback(self._telegram_bridge))

            log.info(f"Telegram bridge started, connected to event emitter (filter={len(self._telegram_bridge.event_filter)} events)")

        except ImportError as e:
            log.warning(f"Telegram bridge not available: {e}")
        except Exception as e:
            log.error(f"Failed to initialize Telegram bridge: {e}", exc_info=True)

    async def stop(self) -> None:
        """Shutdown the agent and Telegram bridge."""
        # Stop Telegram bridge first
        if self._telegram_bridge is not None:
            await self._telegram_bridge.stop()
            self._telegram_bridge = None

        if self._agent is not None:
            # Stop consciousness first
            self._agent.stop_consciousness()
            self._agent = None
        log.info("DpcAgent stopped")

    async def process_message(
        self,
        message: str,
        conversation_id: str,
        include_context: bool = True,
        on_stream_chunk=None,
    ) -> str:
        """
        Process a user message through the agent.

        Args:
            message: User's message text
            conversation_id: Unique ID for this conversation
            include_context: Whether to include DPC personal/device context
            on_stream_chunk: Optional async callback for streaming text chunks: await on_stream_chunk(chunk, conversation_id)

        Returns:
            Agent's response text
        """
        import uuid

        # Generate task ID for this request
        task_id = f"chat-{uuid.uuid4().hex[:8]}"
        emitter = get_event_emitter()

        # Emit TASK_STARTED event
        await emitter.emit(EventType.TASK_STARTED, {
            "task_id": task_id,
            "task_type": "chat",
            "conversation_id": conversation_id,
            "message_preview": message[:100] if len(message) > 100 else message,
        })

        # Get or create ConversationMonitor for this agent conversation (reuse existing)
        monitor = self._get_or_create_agent_monitor(conversation_id)

        # Track user message in monitor (reuse existing method)
        node_id = getattr(self.service.p2p_manager, "node_id", "local-user")
        monitor.add_message(
            role="user",
            content=message,
            timestamp=utc_now_iso(),
            sender_node_id=node_id,
            sender_name="User"
        )

        # Get DPC context if requested
        dpc_context = None
        if include_context:
            dpc_context = self._get_dpc_context()

        try:
            # Process through agent with progress callback that includes conversation_id
            def emit_progress_with_context(msg: str, tool: str = None, round: int = None):
                self._emit_progress(msg, conversation_id, tool, round)

            # Create streaming callback that broadcasts text chunks via local_api
            async def emit_stream_chunk(chunk: str, conv_id: str):
                # Broadcast text_chunk event for WebSocket (UI streaming display)
                try:
                    local_api = getattr(self.service, "local_api", None)
                    if local_api and hasattr(local_api, "broadcast_event"):
                        await local_api.broadcast_event("agent_text_chunk", {
                            "conversation_id": conv_id,
                            "chunk": chunk,
                            "ts": utc_now_iso(),
                        })
                except Exception as e:
                    log.debug(f"Failed to emit text chunk: {e}")

                # Note: We do NOT call on_stream_chunk here to avoid callback chain issues
                # The broadcast above is sufficient for UI updates

            # Get session state for agent context (token usage, context window)
            session_state = self.get_session_state(conversation_id)

            response = await self.agent.process(
                message=message,
                conversation_id=conversation_id,
                dpc_context=dpc_context,
                emit_progress=emit_progress_with_context,
                on_stream_chunk=emit_stream_chunk,
                session_state=session_state,
                conversation_monitor=monitor,  # For knowledge extraction tool
            )

            # Track agent response in monitor (reuse existing method)
            monitor.add_message(
                role="assistant",
                content=response,
                timestamp=utc_now_iso(),
                sender_node_id="dpc-agent",
                sender_name="DPC Agent"
            )

            # Update token count in monitor after agent response
            # Get token usage from agent's last response
            if hasattr(self._agent, '_last_usage') and self._agent._last_usage:
                usage = self._agent._last_usage
                if usage.get("prompt_tokens"):
                    # Get context window from LLMManager (reuse existing)
                    llm_manager = getattr(self.service, "llm_manager", None)
                    if llm_manager:
                        model = llm_manager.get_active_model_name()
                        context_window = llm_manager.get_context_window(model)
                        monitor.set_token_limit(context_window)
                    monitor.set_token_count(usage["prompt_tokens"])

            # Emit TASK_COMPLETED event
            await emitter.emit(EventType.TASK_COMPLETED, {
                "task_id": task_id,
                "task_type": "chat",
                "conversation_id": conversation_id,
                "response_length": len(response),
                "result": response[:200] if len(response) > 200 else response,
            })

            # Clear progress indicator
            self._emit_progress_clear(conversation_id)

            return response

        except Exception as e:
            # Emit TASK_FAILED event
            await emitter.emit(EventType.TASK_FAILED, {
                "task_id": task_id,
                "task_type": "chat",
                "conversation_id": conversation_id,
                "error": str(e),
            })
            # Clear progress indicator on failure too
            self._emit_progress_clear(conversation_id)
            raise

    def _get_dpc_context(self) -> Dict[str, Any]:
        """Get DPC personal and device context with firewall checks."""
        context = {}
        dpc_dir = pathlib.Path.home() / ".dpc"

        # Check if agent is enabled via firewall
        if self.firewall and not self.firewall.dpc_agent_enabled:
            log.debug("DPC Agent is disabled via firewall rules")
            return context

        # Load personal context (with firewall check)
        if self.firewall is None or self.firewall.can_agent_access_context('personal'):
            personal_path = dpc_dir / "personal.json"
            if personal_path.exists():
                try:
                    personal = json.loads(personal_path.read_text(encoding="utf-8"))
                    context["personal"] = json.dumps(personal, indent=2, ensure_ascii=False)
                except Exception as e:
                    log.debug(f"Failed to load personal context: {e}")
        else:
            log.debug("Personal context access denied by firewall")

        # Load device context (with firewall check)
        if self.firewall is None or self.firewall.can_agent_access_context('device'):
            device_path = dpc_dir / "device_context.json"
            if device_path.exists():
                try:
                    device = json.loads(device_path.read_text(encoding="utf-8"))
                    context["device"] = json.dumps(device, indent=2, ensure_ascii=False)
                except Exception as e:
                    log.debug(f"Failed to load device context: {e}")
        else:
            log.debug("Device context access denied by firewall")

        return context

    def _get_or_create_agent_monitor(self, conversation_id: str) -> ConversationMonitor:
        """
        Get or create a ConversationMonitor for agent conversations.

        Reuses CoreService's existing ConversationMonitor infrastructure
        to track messages and token usage for agent sessions.

        Args:
            conversation_id: Unique ID for this conversation

        Returns:
            ConversationMonitor instance for this conversation
        """
        if conversation_id not in self._agent_monitors:
            # Create participants list for agent conversation
            node_id = getattr(self.service.p2p_manager, "node_id", "local-user")
            participants = [
                {
                    "node_id": node_id,
                    "name": "User",
                    "context": "local"
                },
                {
                    "node_id": "dpc-agent",
                    "name": "DPC Agent",
                    "context": "agent"
                }
            ]

            # Get settings and LLM manager from CoreService
            settings = getattr(self.service, "settings", None)
            llm_manager = getattr(self.service, "llm_manager", None)

            # Create monitor with same settings as P2P conversations
            self._agent_monitors[conversation_id] = ConversationMonitor(
                conversation_id=conversation_id,
                participants=participants,
                llm_manager=llm_manager,
                knowledge_threshold=0.7,
                settings=settings,
                ai_query_func=getattr(self.service, "send_ai_query", None),
                auto_detect=False,  # Manual extraction only (agent triggers via tool)
                instruction_set_name="general"
            )

            log.debug(f"Created ConversationMonitor for agent conversation: {conversation_id}")

        return self._agent_monitors[conversation_id]

    def get_session_state(self, conversation_id: str) -> Dict[str, Any]:
        """
        Get session state for an agent conversation.

        Returns token usage, context window, and other session info
        that the agent can use to make decisions about session management.

        Args:
            conversation_id: The conversation to get state for

        Returns:
            Dict with tokens_used, tokens_limit, usage_percent, messages_count,
            should_extract_knowledge
        """
        monitor = self._agent_monitors.get(conversation_id)
        if not monitor:
            return {
                "tokens_used": 0,
                "tokens_limit": 128000,
                "usage_percent": 0,
                "messages_count": 0,
                "should_extract_knowledge": False,
            }

        usage = monitor.get_token_usage()
        return {
            "tokens_used": usage.get("tokens_used", 0),
            "tokens_limit": usage.get("token_limit", 128000),
            "usage_percent": usage.get("usage_percent", 0),
            "messages_count": len(monitor.message_history),
            "should_extract_knowledge": monitor.should_suggest_extraction(),
        }

    def reset_conversation(self, conversation_id: str) -> bool:
        """
        Reset conversation history for a specific conversation.

        Args:
            conversation_id: The conversation to reset (e.g., "telegram-12345")

        Returns:
            True if reset was successful, False if conversation not found
        """
        monitor = self._agent_monitors.get(conversation_id)
        if monitor:
            monitor.reset_conversation()
            log.info(f"Reset conversation: {conversation_id}")
            return True
        log.debug(f"Conversation not found for reset: {conversation_id}")
        return False

    def _emit_progress(
        self,
        message: str,
        conversation_id: str = None,
        tool_name: str = None,
        round: int = None
    ) -> None:
        """Emit progress message to DPC UI (if available)."""
        try:
            # Try to broadcast via local_api if available
            local_api = getattr(self.service, "local_api", None)
            if local_api and hasattr(local_api, "broadcast_event"):
                import asyncio
                asyncio.create_task(local_api.broadcast_event("agent_progress", {
                    "message": message[:500],
                    "conversation_id": conversation_id,
                    "tool_name": tool_name,
                    "round": round,
                    "ts": utc_now_iso(),
                }))
        except Exception as e:
            log.debug(f"Failed to emit progress: {e}")

    def _emit_progress_clear(self, conversation_id: str) -> None:
        """Emit progress clear event to DPC UI (when task completes/fails)."""
        try:
            local_api = getattr(self.service, "local_api", None)
            if local_api and hasattr(local_api, "broadcast_event"):
                import asyncio
                asyncio.create_task(local_api.broadcast_event("agent_progress_clear", {
                    "conversation_id": conversation_id,
                }))
        except Exception as e:
            log.debug(f"Failed to emit progress clear: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get agent status."""
        status = {
            "initialized": self._agent is not None,
            "agent_root": str(self.agent_root),
            "config": {
                "budget_usd": self.config.get("budget_usd", 50.0),
                "max_rounds": self.config.get("max_rounds", 200),
                "tools": self.config.get("tools", []),
            },
        }

        if self._agent is not None:
            status["agent"] = self._agent.get_status()

        return status

    def reset_memory(self) -> None:
        """Reset agent memory to defaults."""
        if self._agent is not None:
            self._agent.reset_memory()
        else:
            # Reset directly via Memory class
            from ..dpc_agent.memory import Memory
            memory = Memory(agent_root=self.agent_root)
            memory.save_scratchpad(memory._default_scratchpad())
            memory.save_identity(memory._default_identity())

    def is_running(self) -> bool:
        """Check if agent is running."""
        return self._agent is not None

    def is_consciousness_running(self) -> bool:
        """Check if background consciousness is running."""
        return self._agent is not None and self._agent.is_consciousness_running()

    def start_consciousness(self) -> None:
        """Start background consciousness manually."""
        if self._agent is not None:
            self._agent.start_consciousness(emit_progress=self._emit_progress)

    def stop_consciousness(self) -> None:
        """Stop background consciousness."""
        if self._agent is not None:
            self._agent.stop_consciousness()
