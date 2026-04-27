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

    def __init__(self, service: "CoreService", config: Dict[str, Any], agent_id: Optional[str] = None):
        """
        Initialize the agent manager.

        Args:
            service: DPC CoreService instance
            config: Agent configuration from settings
            agent_id: Optional agent ID for this manager (used for per-agent Telegram config)
        """
        self.service = service
        self.config = config
        self.agent_id = agent_id  # Store agent_id for per-agent configuration

        # Get firewall reference from CoreService
        self.firewall = getattr(service, "firewall", None)

        # Storage paths
        self.agent_root = get_agent_root(agent_id)
        ensure_agent_dirs(agent_id)

        # Agent instances (Phase 3: per-provider agents)
        # Key: provider_alias, Value: DpcAgent instance
        self._agents: Dict[str, DpcAgent] = {}
        # Default agent for backward compatibility
        self._agent: Optional[DpcAgent] = None

        # Telegram bridge for notifications
        self._telegram_bridge = None

        # Conversation monitors for agent sessions (reuse existing ConversationMonitor)
        # Key: conversation_id, Value: ConversationMonitor instance
        self._agent_monitors: Dict[str, ConversationMonitor] = {}

        log.info(f"DpcAgentManager initialized (agent_id={agent_id or 'singleton'}) with storage at {self.agent_root}")

    @property
    def agent(self) -> DpcAgent:
        """Get the default agent instance, initializing if needed."""
        if self._agent is None:
            raise RuntimeError("Agent not initialized. Call start() first.")
        return self._agent

    def _get_or_create_agent_for_provider(self, provider_alias: str) -> DpcAgent:
        """
        Get or create an agent configured with a specific LLM provider.

        Phase 3: Per-agent provider selection - allows different agents to use
        different underlying LLM providers.

        Args:
            provider_alias: The LLM provider alias to configure the agent with

        Returns:
            DpcAgent instance configured with the specified provider
        """
        # Check if we already have an agent for this provider
        if provider_alias in self._agents:
            return self._agents[provider_alias]

        # If no provider specified or same as default, use default agent
        if not provider_alias or (self._agent and provider_alias == "dpc_agent"):
            if self._agent is None:
                raise RuntimeError("Default agent not initialized. Call start() first.")
            return self._agent

        # Create a new agent with the specified provider
        log.info(f"Creating new agent with provider: {provider_alias}")

        # Get LLMManager from CoreService
        llm_manager = getattr(self.service, "llm_manager", None)
        if llm_manager is None:
            raise RuntimeError("CoreService does not have llm_manager")

        # Build agent config
        agent_config = AgentConfig(
            budget_usd=self.config.get("budget_usd", 50.0),
            max_rounds=self.config.get("max_rounds", 200),
            enable_task_queue=False,
            billing_model=self.config.get("billing_model", "subscription"),
        )

        # Create agent with specific provider
        new_agent = DpcAgent(
            llm_manager=llm_manager,
            config=agent_config,
            agent_root=self.agent_root,
            firewall=self.firewall,
            provider_alias=provider_alias,   # Phase 3: Use specific provider
            firewall_profile=self.agent_id,  # Per-agent profile key for per-agent permissions
            service=self.service,            # For tools that need service access
            compute_host=self.config.get("compute_host", ""),  # Remote peer for LLM inference
        )

        # Cache for reuse
        self._agents[provider_alias] = new_agent
        log.info(f"Created and cached agent for provider: {provider_alias}")

        return new_agent

    async def start(self) -> None:
        """Initialize the agent and Telegram bridge."""
        if self._agent is not None:
            log.warning("Agent already initialized")
            return

        # Check if agent is enabled: per-agent profile overrides global dpc_agent setting
        _per_agent_enabled = (
            self.firewall.get_agent_profile_settings(self.agent_id) if (self.firewall and self.agent_id) else None
        )
        _agent_enabled = (
            _per_agent_enabled.get('enabled', self.firewall.dpc_agent_enabled)
            if _per_agent_enabled is not None
            else (self.firewall.dpc_agent_enabled if self.firewall else True)
        )
        if not _agent_enabled:
            log.warning("DPC Agent is disabled (firewall profile=%s) - not starting", self.agent_id or 'global')
            return

        # Build agent config (tool control is via firewall, not config)
        # Per-agent profile overrides global dpc_agent settings
        _per_agent_profile = (
            self.firewall.get_agent_profile_settings(self.agent_id)
            if (self.firewall and self.agent_id)
            else None
        )

        agent_config = AgentConfig(
            budget_usd=self.config.get("budget_usd", 50.0),
            max_rounds=self.config.get("max_rounds", 200),
            enable_task_queue=self.config.get("enable_task_queue", True),
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
            firewall=self.firewall,           # Firewall controls tool access
            firewall_profile=self.agent_id,  # Per-agent profile key for per-agent permissions
            service=self.service,             # For tools that need firewall access
            compute_host=self.config.get("compute_host", ""),  # Remote peer for LLM inference
        )

        # Start task processor if enabled
        if agent_config.enable_task_queue:
            await self._agent.start_task_processor()
            log.info("Task processor started")

        # Initialize memory system (ADR-010)
        try:
            from dpc_client_core.dpc_agent.memory_config import get_memory_config
            from dpc_client_core.dpc_agent.model_download import notify_download_needed
            _profile = self.firewall.get_agent_profile_settings(self.agent_id) if (self.firewall and self.agent_id) else {}
            mem_cfg = get_memory_config(_profile or self.config)
            if mem_cfg.enabled:
                notification = notify_download_needed(mem_cfg.embedding_model)
                if notification.get("needed"):
                    log.info("Memory: embedding model not yet downloaded (%s)", mem_cfg.embedding_model)
                    # MEM-3.9: notify UI about pending model download
                    if hasattr(self.service, 'broadcast_event'):
                        self.service.broadcast_event("memory_model_download_needed", notification)
                # First-use full rebuild (MEM-3.7 spec)
                import asyncio
                agent_root = self._agent.agent_root if self._agent else None
                if agent_root:
                    index_dir = agent_root / "state" / "memory_index"
                    needs_full_rebuild = not (index_dir / "index_meta.json").exists()

                    def _sync_index():
                        """Runs in thread executor to avoid blocking the event loop."""
                        try:
                            import numpy as np
                            from pathlib import Path
                            import os
                            from dpc_client_core.dpc_agent.memory import EmbeddingProvider
                            from dpc_client_core.dpc_agent.faiss_index import FaissIndex
                            from dpc_client_core.dpc_agent.bm25_index import BM25Index
                            from dpc_client_core.dpc_agent.text_extract import extract_text
                            from dpc_client_core.dpc_agent.indexing_pipeline import _extract_heading, _build_doc_text
                            provider = self._agent._embedding_provider if self._agent else EmbeddingProvider(model_name=mem_cfg.embedding_model)
                            faiss_idx = FaissIndex(index_dir, model_name=mem_cfg.embedding_model, dimensions=provider.dimensions)
                            bm25_idx = BM25Index(index_dir)

                            count = 0
                            l6_count = 0
                            # Always rebuild L5 agent knowledge (not just on first init)
                            from dpc_client_core.dpc_agent.indexing_pipeline import full_rebuild
                            knowledge_dir = agent_root / "knowledge"
                            count = full_rebuild(knowledge_dir, provider, faiss_idx, bm25_idx)

                            # Collect all extra documents (L6 + EXT) then embed+index in bulk
                            extra_texts = []
                            extra_metas = []

                            # L6: human knowledge (always re-index, not just on full rebuild)
                            if self.firewall and self.firewall.can_agent_access_context('knowledge'):
                                l6_dir = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc")) / "knowledge"
                                if l6_dir.is_dir():
                                    for f in sorted(l6_dir.iterdir()):
                                        if f.suffix == ".md" and f.is_file():
                                            text = extract_text(f)
                                            if text:
                                                heading = _extract_heading(text)
                                                doc_text = _build_doc_text(f.name, heading, text)
                                                extra_texts.append(doc_text)
                                                extra_metas.append({"source_file": f.name, "heading": heading,
                                                                    "source_layer": "L6", "char_count": len(text),
                                                                    "text": text[:500]})
                                                l6_count += 1
                                    if l6_count:
                                        log.info("L6 human knowledge collected: %d documents from %s", l6_count, l6_dir)

                            # MEM-3.10: always re-index extended paths on startup
                            ext_count = 0
                            if self.firewall:
                                try:
                                    from dpc_client_core.dpc_agent.extended_paths_index import collect_extended_files, RECALL_EXTENSIONS
                                    ext_paths = self.firewall.get_extended_paths(profile_name=self.agent_id) if hasattr(self.firewall, 'get_extended_paths') else {}
                                    indexed_list = self.firewall._get_profile_or_global(self.agent_id, 'sandbox_extensions', 'indexed_paths', default=[]) if self.agent_id else []
                                    excluded_dirs = self.firewall._get_profile_or_global(self.agent_id, 'sandbox_extensions', 'excluded_dirs', default=None) if self.agent_id else None
                                    ext_files = collect_extended_files(ext_paths, indexed_paths=indexed_list, excluded_dirs=excluded_dirs, allowed_extensions=RECALL_EXTENSIONS) if indexed_list else []
                                    if ext_files:
                                        for f in ext_files:
                                            text = extract_text(f)
                                            if text:
                                                heading = _extract_heading(text)
                                                doc_text = _build_doc_text(f.name, heading, text)
                                                extra_texts.append(doc_text)
                                                extra_metas.append({"source_file": f.name, "heading": heading,
                                                                    "source_layer": "EXT", "char_count": len(text),
                                                                    "text": text[:500]})
                                                ext_count += 1
                                    elif indexed_list:
                                        log.info("Extended paths: %d paths configured but 0 text files found", len(indexed_list))
                                except Exception as e:
                                    log.debug("Extended paths indexing skipped: %s", e)

                            # Embed + index extra documents one at a time (whole-doc, no batching)
                            if extra_texts:
                                for doc_text, meta in zip(extra_texts, extra_metas):
                                    vector = np.array(provider.embed(doc_text), dtype=np.float32).reshape(1, -1)
                                    faiss_idx.add(vector, [meta])
                                bm25_idx.add(extra_texts, extra_metas)
                                log.info("Bulk indexed %d extra documents (L6: %d, EXT: %d)", len(extra_texts), l6_count, ext_count)

                            if needs_full_rebuild or extra_texts:
                                faiss_idx.save()
                                bm25_idx.save()
                            if needs_full_rebuild:
                                total = count + l6_count + ext_count
                                log.info("Memory index built: %d documents (L5: %d, L6: %d, EXT: %d)", total, count, l6_count, ext_count)
                            elif ext_count > 0:
                                log.info("Extended paths re-indexed on startup: %d documents", ext_count)
                        except Exception as e:
                            log.warning("Background memory indexing failed: %s", e)

                    if needs_full_rebuild or (self.firewall and self.firewall._get_profile_or_global(self.agent_id, 'sandbox_extensions', 'indexed_paths', default=[])):
                        loop = asyncio.get_event_loop()
                        loop.run_in_executor(None, _sync_index)
                        if needs_full_rebuild:
                            log.info("Memory: first-use index rebuild started in thread")
                        else:
                            log.info("Memory: extended paths re-index started in thread")
                log.info("Memory system initialized (model=%s, active_recall=%s)", mem_cfg.embedding_model, mem_cfg.active_recall)
        except Exception as e:
            log.warning("Memory system init skipped: %s", e)

        # Initialize Telegram bridge for agent notifications
        await self._start_telegram_bridge()

        # Wire Telegram send-back so scheduled tasks can deliver their results
        self._agent._telegram_send_fn = self._deliver_telegram_result

        # Check for unconsumed morning brief from sleep pipeline (ADR-014)
        try:
            import json as _json
            _conv_dir = pathlib.Path.home() / ".dpc" / "conversations" / self.agent_id
            _brief_path = _conv_dir / "morning_brief.json"
            if _brief_path.exists():
                _brief = _json.loads(_brief_path.read_text(encoding="utf-8"))
                if not _brief.get("consumed", False):
                    from dpc_client_core.service import CoreService
                    chat_text = CoreService._format_morning_brief(_brief)
                    monitor = self._get_or_create_agent_monitor(self.agent_id)
                    if monitor:
                        from datetime import datetime, timezone
                        monitor.add_message("assistant", chat_text, sender_name=self.agent_id, timestamp=datetime.now(timezone.utc).isoformat())
                        monitor.save_history()
                        _brief["consumed"] = True
                        _brief_path.write_text(_json.dumps(_brief, ensure_ascii=False, indent=2), encoding="utf-8")
                        log.info("Morning brief posted to chat for %s", self.agent_id)
        except Exception as e:
            log.debug("Morning brief startup check skipped: %s", e)

        log.info("DpcAgent started successfully")

    def sync_firewall_settings(self) -> None:
        """Re-read settings from firewall after UI save."""
        pass

    async def ensure_started(self) -> "DpcAgentManager":
        """
        Ensure the agent is started, starting it if necessary.

        This is a convenience method that allows lazy initialization.

        Returns:
            self (for method chaining)
        """
        if self._agent is None:
            await self.start()
        return self

    async def _start_telegram_bridge(self) -> None:
        """Initialize and start the Telegram notification bridge from per-agent configuration.

        Reads Telegram config from agent registry (_registry.json) with fallback
        to global [dpc_agent_telegram] config for backwards compatibility.
        """
        from ..dpc_agent.utils import AgentRegistry

        # Handle missing agent_id gracefully (skip per-agent config, go directly to global)
        if not self.agent_id:
            log.debug("agent_id not set in DpcAgentManager, skipping per-agent Telegram config (will use global if available)")
            # Initialize variables for global config fallback below
            bot_token = None
            chat_ids = None
            event_filter = None
            max_events_per_minute = 20
            cooldown_seconds = 3.0
            transcription_enabled = True
            unified_conversation = False
            skip_per_agent = True
        else:
            # First try to get per-agent Telegram config from registry
            skip_per_agent = False
            registry = AgentRegistry()
            agent_meta = registry.get_agent(self.agent_id)

            # Special case: local_ai should not use Telegram (built-in conversation)
            if self.agent_id == 'local_ai':
                log.debug(f"Telegram not enabled for local_ai (built-in conversation), skipping Telegram bridge")
                return

            if not agent_meta or not agent_meta.get("telegram_enabled", False):
                log.debug(f"Telegram not enabled for agent {self.agent_id}, skipping Telegram bridge")
                return

            # Read per-agent Telegram config
            bot_token = agent_meta.get("telegram_bot_token", "")
            chat_ids = agent_meta.get("telegram_allowed_chat_ids", [])
            event_filter = agent_meta.get("telegram_event_filter")
            max_events_per_minute = agent_meta.get("telegram_max_events_per_minute", 20)
            cooldown_seconds = agent_meta.get("telegram_cooldown_seconds", 3.0)
            transcription_enabled = agent_meta.get("telegram_transcription_enabled", True)
            unified_conversation = agent_meta.get("telegram_unified_conversation", False)

        # Backwards compatibility: fall back to global config if per-agent config is incomplete
        if not bot_token or not chat_ids:
            agent_desc = self.agent_id if self.agent_id else "singleton"
            if not skip_per_agent:
                log.info(f"Agent {agent_desc} has incomplete per-agent Telegram config, checking global config")

            settings = getattr(self.service, "settings", None)
            if settings is None:
                log.debug(f"No settings available, skipping Telegram bridge for agent {agent_desc}")
                return

            global_config = settings.get_dpc_agent_telegram_config()

            if not global_config.get("enabled", False):
                log.debug(f"Global Telegram config not enabled, skipping Telegram bridge for agent {agent_desc}")
                return

            # Use global config as fallback
            if not bot_token:
                bot_token = global_config.get("bot_token", "")
            if not chat_ids:
                chat_ids = global_config.get("allowed_chat_ids", [])
            if not event_filter:
                event_filter = global_config.get("event_filter")
            if transcription_enabled is True:  # Only use global default if not set
                transcription_enabled = global_config.get("transcription_enabled", True)

            # Log deprecation warning if using global config
            if not skip_per_agent:
                log.warning(
                    f"DEPRECATED: Agent {agent_desc} is using global [dpc_agent_telegram] config. "
                    f"Please migrate to per-agent Telegram configuration. "
                    f"See docs/DPC_AGENT_TELEGRAM.md for migration guide. "
                    f"Global config will be removed in v0.20.0."
                )

        # Validate required fields
        if not bot_token or not chat_ids:
            agent_desc = self.agent_id if self.agent_id else "singleton"
            log.warning(f"Agent {agent_desc} has telegram_enabled=true but missing bot_token or allowed_chat_ids")
            return

        # Check for token conflict with main TelegramBotManager
        main_telegram = getattr(self.service, "telegram_manager", None)
        if main_telegram and getattr(main_telegram, "bot_token", None) == bot_token:
            agent_desc = self.agent_id if self.agent_id else "singleton"
            log.error(
                f"Agent {agent_desc} Telegram bridge is configured with the same bot token as the main "
                f"TelegramBotManager. This causes a Conflict error (two instances polling the same bot). "
                f"Create a separate bot via @BotFather for the agent bridge. "
                f"See docs/DPC_AGENT_TELEGRAM.md for setup instructions."
            )
            return

        try:
            from .agent_telegram_bridge import AgentTelegramBridge, RateLimitConfig, create_telegram_bridge_callback

            # Create rate limit config
            rate_limit = RateLimitConfig(
                max_events_per_minute=max_events_per_minute,
                cooldown_seconds=cooldown_seconds
            )

            # Create bridge with per-agent config
            self._telegram_bridge = AgentTelegramBridge(
                bot_token=bot_token,
                allowed_chat_ids=chat_ids,
                event_filter=event_filter,
                rate_limit=rate_limit,
                transcription_enabled=transcription_enabled,
                agent_id=self.agent_id or "",
                unified_conversation=unified_conversation,
            )

            # Set message handler for two-way communication
            self._telegram_bridge.set_message_handler(
                handler=self.process_message,
                agent_manager=self,
            )

            # Start bridge
            success = await self._telegram_bridge.start()
            if not success:
                agent_desc = self.agent_id if self.agent_id else "singleton"
                log.warning(f"Failed to start Telegram bridge for agent {agent_desc}")
                self._telegram_bridge = None
                return

            # Connect to event emitter, scoped to this agent's conversation_id
            emitter = get_event_emitter()
            emitter.add_listener(create_telegram_bridge_callback(self._telegram_bridge, agent_id=self.agent_id))

            agent_desc = self.agent_id if self.agent_id else "singleton"
            log.info(
                f"Telegram bridge started for agent {agent_desc}, "
                f"connected to event emitter (filter={len(self._telegram_bridge.event_filter)} events, "
                f"chat_ids={chat_ids})"
            )

        except ImportError as e:
            log.warning(f"Telegram bridge not available: {e}")
        except Exception as e:
            agent_desc = self.agent_id if self.agent_id else "singleton"
            log.error(f"Failed to initialize Telegram bridge for agent {agent_desc}: {e}", exc_info=True)

    async def _deliver_telegram_result(self, chat_id: str, text: str) -> None:
        """Send a scheduled task result back to a Telegram chat.

        Called by _execute_task when task.data contains _reply_telegram_chat_id.
        Uses the existing _telegram_bridge._send_message so all escaping/retry
        logic is reused.
        """
        if not self._telegram_bridge or not text:
            return
        try:
            await self._telegram_bridge._send_message(chat_id, text)
            log.info("Delivered task result to Telegram chat %s", chat_id)
        except Exception as e:
            log.warning("Failed to deliver task result to Telegram chat %s: %s", chat_id, e)

    async def stop(self) -> None:
        """Shutdown the agent and Telegram bridge."""
        # Stop Telegram bridge first
        if self._telegram_bridge is not None:
            await self._telegram_bridge.stop()
            self._telegram_bridge = None

        if self._agent is not None:
            self._agent.stop_task_processor()
            self._agent = None
        log.info("DpcAgent stopped")

    async def process_message(
        self,
        message: str,
        conversation_id: str,
        include_context: bool = True,
        on_stream_chunk=None,
        # Image parameters for vision queries
        image_base64: Optional[str] = None,
        image_mime: str = "image/png",
        image_caption: Optional[str] = None,
        # Phase 3: Per-agent provider selection
        agent_llm_provider: Optional[str] = None,
        # Sender attribution (e.g. "mike (Telegram)" vs "User")
        sender_name: str = "User",
        # When set, injected into ToolContext so schedule_task auto-fills
        # _reply_telegram_chat_id in task data for later result delivery.
        telegram_chat_id: Optional[str] = None,
        # When True, don't save user message to history (already saved by caller)
        _skip_history: bool = False,
    ) -> str:
        """
        Process a user message through the agent.

        Args:
            message: User's message text
            conversation_id: Unique ID for this conversation
            include_context: Whether to include DPC personal/device context
            on_stream_chunk: Optional async callback for streaming text chunks: await on_stream_chunk(chunk, conversation_id)
            image_base64: Optional base64-encoded image data for vision queries
            image_mime: MIME type of the image (default: image/png)
            image_caption: Optional caption for the image
            agent_llm_provider: Optional underlying LLM provider for this agent (Phase 3: per-agent provider selection)

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

        # Track user message in monitor (skip when caller already saved it, e.g. CC chain trigger)
        if not _skip_history:
            node_id = getattr(self.service.p2p_manager, "node_id", "local-user")
            monitor.add_message(
                role="user",
                content=message,
                timestamp=utc_now_iso(),
                sender_node_id=node_id,
                sender_name=sender_name
            )
            monitor.save_history()  # Save to disk immediately

            # Update context_estimated immediately so UI counter reflects user message (#4)
            # Token stats will be included in Ark's response via get_session_state()
            user_tokens = len(message) // 4
            old_estimate = getattr(monitor, '_last_context_estimated', 0)
            if old_estimate:
                monitor._last_context_estimated = old_estimate + user_tokens

        # Use agent_id as sender name for better identification in chat UI
        agent_display_name = self.agent_id or "DPC Agent"

        # Get DPC context if requested
        dpc_context = None
        if include_context:
            dpc_context = self._get_dpc_context()

        try:
            # Process through agent with progress callback that includes conversation_id
            def emit_progress_with_context(msg: str, tool: str = None, round: int = None):
                self._emit_progress(msg, conversation_id, tool, round)

            # Accumulate streaming chunks for persistence to history.json
            _stream_chunks: list = []

            # Create streaming callback that broadcasts text chunks via local_api
            async def emit_stream_chunk(chunk: str, conv_id: str):
                _stream_chunks.append(chunk)
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

            # Phase 3: Get provider-specific agent if agent_llm_provider is specified
            agent = self._get_or_create_agent_for_provider(agent_llm_provider) if agent_llm_provider else self.agent

            try:
                response = await agent.process(
                    message=message,
                    conversation_id=conversation_id,
                    dpc_context=dpc_context,
                    emit_progress=emit_progress_with_context,
                    on_stream_chunk=emit_stream_chunk,
                    session_state=session_state,
                    conversation_monitor=monitor,  # For knowledge extraction tool
                    task_id=task_id,  # Unique per-message ID for event logging
                    # Pass image parameters for vision queries
                    image_base64=image_base64,
                    image_mime=image_mime,
                    image_caption=image_caption,
                    reply_telegram_chat_id=telegram_chat_id,
                )
            finally:
                pass

            # Track agent response in monitor.
            # Always save if there's content, thinking, or streaming_raw — the UI
            # already shows thinking/raw blocks, so they must be persisted to history.
            _THINKING_FALLBACK = "(thinking completed - see reasoning for details)"
            _thinking = agent._last_usage.get("thinking") if agent._last_usage else None
            _raw = "".join(_stream_chunks) if _stream_chunks else None
            _streaming_raw = _raw if _raw and _raw.strip() != (response or "").strip() else None
            _has_content = response and response.strip() != _THINKING_FALLBACK
            _has_extras = _thinking or _streaming_raw

            # Skip saving agent errors to history — they pollute context for future requests
            _is_llm_error = response and response.startswith("⚠️")

            if (_has_content or _has_extras) and not _is_llm_error:
                monitor.add_message(
                    role="assistant",
                    content=response or "",
                    timestamp=utc_now_iso(),
                    sender_node_id=conversation_id,
                    sender_name=agent_display_name,
                    thinking=_thinking,
                    streaming_raw=_streaming_raw,
                )
                monitor.save_history()  # Save to disk immediately
            elif _is_llm_error:
                log.warning(f"LLM error not saved to history: {response[:100]}")

            # Store full context estimate from this request so next request's session_state
            # can expose it. One request stale, but accurate — context grows incrementally.
            # Prefer accurate token count from LLM adapter (first_prompt_tokens = round-1 context
            # before tool results inflate it); fall back to chars/4 estimate from cap_info.
            if hasattr(agent, '_last_usage') and agent._last_usage:
                accurate = (agent._last_usage.get("first_prompt_tokens")
                            or agent._last_usage.get("prompt_tokens", 0))
                if accurate:
                    monitor._last_context_estimated = accurate
                elif hasattr(agent, '_last_cap_info') and agent._last_cap_info:
                    monitor._last_context_estimated = agent._last_cap_info.get("estimated_tokens_before", 0)
            elif hasattr(agent, '_last_cap_info') and agent._last_cap_info:
                monitor._last_context_estimated = agent._last_cap_info.get("estimated_tokens_before", 0)

            # Update token count in monitor after agent response.
            # Count tokens directly from the conversation history (user + assistant messages)
            # stored in the monitor — the same data that gets sent as input on every new request.
            # This excludes constant overhead (system prompt, tool schemas, agent memory) so the
            # counter reflects only the growing conversation portion, consistent with the intent
            # of the local AI chat token counter.
            # Use accurate tokenizer if available (via agent's LLM adapter).
            _history_text = " ".join(
                msg.get("content", "") or ""
                for msg in monitor.message_history
            )
            _token_counter = getattr(getattr(agent, 'llm_adapter', None), '_token_counter', None)
            _model_name = (getattr(agent.llm_adapter, 'default_model', lambda: None)()
                           if hasattr(agent, 'llm_adapter') else None)
            if _token_counter and _model_name and _history_text:
                conversation_tokens = _token_counter.count_tokens(_history_text, _model_name)
            else:
                conversation_tokens = len(_history_text) // 4
            if conversation_tokens:
                llm_manager = getattr(self.service, "llm_manager", None)
                if llm_manager:
                    # Use stored context_window from agent config when available
                    # (remote agents have a different context window than the local default model)
                    stored_cw = self.config.get("context_window")
                    if stored_cw:
                        context_window = int(stored_cw)
                    else:
                        # Try to resolve context window without stored value.
                        # This handles agents created before context_window was persisted.
                        provider_alias = self.config.get("provider_alias", "")
                        compute_host = self.config.get("compute_host", "")
                        context_window = None
                        if provider_alias and provider_alias in llm_manager.providers:
                            # Local provider: resolve model name then look up window
                            model = llm_manager.providers[provider_alias].model
                            context_window = llm_manager.get_context_window(model)
                        elif compute_host and provider_alias:
                            # Remote provider: check peer_metadata cache for the provider
                            peer_meta = getattr(self.service, "peer_metadata", {})
                            peer_providers = peer_meta.get(compute_host, {}).get("providers", [])
                            for p in peer_providers:
                                if p.get("alias") == provider_alias:
                                    cw = p.get("context_window")
                                    if cw:
                                        context_window = int(cw)
                                    elif p.get("model"):
                                        context_window = llm_manager.get_context_window(p["model"])
                                    break
                        if not context_window:
                            model = llm_manager.get_active_model_name()
                            context_window = llm_manager.get_context_window(model)
                    monitor.set_token_limit(context_window)
                monitor.set_token_count(conversation_tokens)

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
                    "node_id": self.agent_id or "dpc-agent",
                    "name": self.agent_id or "DPC Agent",
                    "context": "agent"
                }
            ]

            # Get settings and LLM manager from CoreService
            settings = getattr(self.service, "settings", None)
            llm_manager = getattr(self.service, "llm_manager", None)

            # Create monitor with same settings as P2P conversations
            monitor = ConversationMonitor(
                conversation_id=conversation_id,
                participants=participants,
                llm_manager=llm_manager,
                knowledge_threshold=0.7,
                settings=settings,
                ai_query_func=getattr(self.service, "send_ai_query", None),
                auto_detect=False,  # Manual extraction only (agent triggers via tool)
                instruction_set_name="general"
            )

            # Load history from disk immediately so existing messages are preserved
            # when Telegram (or any other caller) sends the first message after a restart.
            # Without this, process_message() would start with an empty monitor and
            # save_history() would overwrite the disk file with only the new messages.
            history_path = monitor._get_history_path()
            if history_path.exists():
                monitor.load_history()
                log.debug(f"Loaded {len(monitor.message_history)} messages from disk for {conversation_id}")
                # Also restore full_conversation/message_buffer for knowledge extraction.
                # load_history() only fills message_history (the dict store); without this
                # rebuild, end_session would only analyze messages from the current
                # in-memory session and miss all historical context.
                monitor.rebuild_extraction_buffers_from_history()

            self._agent_monitors[conversation_id] = monitor
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
            Dict with tokens_used, tokens_limit, usage_percent, messages_count
        """
        monitor = self._agent_monitors.get(conversation_id)
        if not monitor:
            return {
                "tokens_used": 0,
                "tokens_limit": 128000,
                "usage_percent": 0,
                "messages_count": 0,
            }

        usage = monitor.get_token_usage()
        token_limit = usage.get("token_limit", 128000)
        history_tokens = usage.get("tokens_used", 0)
        context_estimated = getattr(monitor, '_last_context_estimated', 0)
        return {
            # Conversation history only (user+assistant text ÷ 4).
            # Same basis as the token counter shown in the UI.
            "history_tokens": history_tokens,
            "history_usage_percent": round(history_tokens / token_limit, 4) if token_limit else 0,
            # Full context estimate from previous request (one request stale).
            # Includes: system prompt + scratchpad + identity + knowledge + tools + history.
            # This is what the log "Context size: X%" reports.
            "context_estimated": context_estimated,
            "context_usage_percent": round(context_estimated / token_limit, 4) if token_limit and context_estimated else 0,
            "tokens_limit": token_limit,
            "messages_count": len(monitor.message_history),
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
            preserve, max_sessions = self._get_history_settings()
            archive_count = monitor.reset_conversation(preserve=preserve, max_sessions=max_sessions)
            log.info(f"Reset conversation: {conversation_id} (preserve={preserve}, archives={archive_count})")
            # Broadcast warning if archive is approaching the retention limit (≥80%).
            # When max_sessions is 0 (unlimited), no limit to approach — skip warning.
            if preserve and max_sessions > 0 and archive_count >= int(max_sessions * 0.8):
                import asyncio
                local_api = getattr(self.service, "local_api", None)
                if local_api:
                    asyncio.ensure_future(local_api.broadcast_event("session_archive_warning", {
                        "conversation_id": conversation_id,
                        "archive_count": archive_count,
                        "max_sessions": max_sessions,
                    }))
            return True
        log.debug(f"Conversation not found for reset: {conversation_id}")
        return False

    def _get_history_settings(self) -> tuple:
        """Return (preserve_on_reset, max_archived_sessions) from firewall config.

        max_archived_sessions: 0 = unlimited (keep all archives), >0 = cap.
        """
        if not self.firewall:
            return True, 0
        preserve = getattr(self.firewall, "history_preserve_on_reset", True)
        max_sessions = getattr(self.firewall, "history_max_archived_sessions", 0)
        # Per-agent profile override
        if self.agent_id:
            profile = self.firewall.rules.get("agent_profiles", {}).get(self.agent_id, {})
            if profile:
                hist = profile.get("history", {})
                if "preserve_on_reset" in hist:
                    preserve = hist["preserve_on_reset"]
                if "max_archived_sessions" in hist:
                    max_sessions = max(0, int(hist["max_archived_sessions"]))
        return preserve, max_sessions

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

