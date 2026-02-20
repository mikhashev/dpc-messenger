"""
DPC Agent Manager - Integration layer between DPC and embedded agent.

Manages:
- Agent lifecycle (initialization, shutdown)
- Configuration loading
- Context integration with DPC
- Event forwarding to DPC UI

This manager bridges the embedded DpcAgent with DPC Messenger's CoreService.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import TYPE_CHECKING, Any, Dict, Optional

from ..dpc_agent.agent import DpcAgent, AgentConfig
from ..dpc_agent.utils import get_agent_root, ensure_agent_dirs, utc_now_iso

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

        # Storage paths
        self.agent_root = get_agent_root()
        ensure_agent_dirs()

        # Agent instance (lazy initialization)
        self._agent: Optional[DpcAgent] = None

        log.info(f"DpcAgentManager initialized with storage at {self.agent_root}")

    @property
    def agent(self) -> DpcAgent:
        """Get the agent instance, initializing if needed."""
        if self._agent is None:
            raise RuntimeError("Agent not initialized. Call start() first.")
        return self._agent

    async def start(self) -> None:
        """Initialize the agent."""
        if self._agent is not None:
            log.warning("Agent already initialized")
            return

        # Build agent config
        agent_config = AgentConfig(
            budget_usd=self.config.get("budget_usd", 50.0),
            max_rounds=self.config.get("max_rounds", 200),
            tool_whitelist=set(self.config.get("tools", [])) if self.config.get("tools") else None,
            background_consciousness=self.config.get("background_consciousness", False),
        )

        # Get LLMManager from CoreService
        llm_manager = getattr(self.service, "llm_manager", None)
        if llm_manager is None:
            raise RuntimeError("CoreService does not have llm_manager")

        # Create agent
        self._agent = DpcAgent(
            llm_manager=llm_manager,
            config=agent_config,
            agent_root=self.agent_root,
        )

        log.info("DpcAgent started successfully")

    async def stop(self) -> None:
        """Shutdown the agent."""
        self._agent = None
        log.info("DpcAgent stopped")

    async def process_message(
        self,
        message: str,
        conversation_id: str,
        include_context: bool = True,
    ) -> str:
        """
        Process a user message through the agent.

        Args:
            message: User's message text
            conversation_id: Unique ID for this conversation
            include_context: Whether to include DPC personal/device context

        Returns:
            Agent's response text
        """
        # Get DPC context if requested
        dpc_context = None
        if include_context:
            dpc_context = self._get_dpc_context()

        # Process through agent
        response = await self.agent.process(
            message=message,
            conversation_id=conversation_id,
            dpc_context=dpc_context,
            emit_progress=self._emit_progress,
        )

        return response

    def _get_dpc_context(self) -> Dict[str, Any]:
        """Get DPC personal and device context."""
        context = {}
        dpc_dir = pathlib.Path.home() / ".dpc"

        # Load personal context
        personal_path = dpc_dir / "personal.json"
        if personal_path.exists():
            try:
                personal = json.loads(personal_path.read_text(encoding="utf-8"))
                context["personal"] = json.dumps(personal, indent=2, ensure_ascii=False)
            except Exception as e:
                log.debug(f"Failed to load personal context: {e}")

        # Load device context
        device_path = dpc_dir / "device_context.json"
        if device_path.exists():
            try:
                device = json.loads(device_path.read_text(encoding="utf-8"))
                context["device"] = json.dumps(device, indent=2, ensure_ascii=False)
            except Exception as e:
                log.debug(f"Failed to load device context: {e}")

        return context

    def _emit_progress(self, message: str) -> None:
        """Emit progress message to DPC UI (if available)."""
        try:
            # Try to broadcast via local_api if available
            local_api = getattr(self.service, "local_api", None)
            if local_api and hasattr(local_api, "broadcast_event"):
                import asyncio
                asyncio.create_task(local_api.broadcast_event("agent_progress", {
                    "message": message[:500],
                    "ts": utc_now_iso(),
                }))
        except Exception as e:
            log.debug(f"Failed to emit progress: {e}")

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
