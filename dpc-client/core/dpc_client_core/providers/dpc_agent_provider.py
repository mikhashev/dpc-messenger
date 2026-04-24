# dpc_client_core/providers/dpc_agent_provider.py

import logging
from typing import Dict, Any, Optional, List, TYPE_CHECKING

from .base import AIProvider

if TYPE_CHECKING:
    from dpc_client_core.service import CoreService
    from dpc_client_core.managers.agent_manager import DpcAgentManager

logger = logging.getLogger(__name__)


class DpcAgentProvider(AIProvider):
    """
    Embedded autonomous AI agent provider.

    This provider exposes the embedded DpcAgent as an AI provider option,
    enabling access to:
    - 40+ tools for file operations, web search, memory management
    - Persistent identity and scratchpad memory
    - Sleep Consolidation (session retrospective analysis)
    - Active Recall (contextual memory retrieval)

    Configuration example (~/.dpc/providers.json):
    {
        "alias": "dpc_agent",
        "type": "dpc_agent",
        "tools": ["repo_read", "repo_list", "web_search", "update_scratchpad"],
        "budget_usd": 50,
        "max_rounds": 200,
        "context_window": 200000
    }
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        # Agent configuration
        self.enabled_tools = config.get("tools", [])  # Tool whitelist
        self.background_consciousness = config.get("background_consciousness", False)
        self.budget_usd = config.get("budget_usd", 50.0)
        self.max_rounds = config.get("max_rounds", 200)

        # Remote peer inference ( v0.18.1+ KISS approach)
        # If set, agent routes inference to this peer instead of using local models
        self.peer_id = config.get("peer_id")  # Remote peer node ID
        self.remote_model = config.get("remote_model")  # Model preference on remote peer
        self.remote_provider = config.get("remote_provider")  # Provider preference on remote peer
        self.timeout = config.get("timeout", 180)  # Timeout for remote inference (default 3 minutes)


        # Set model name for token counting (uses underlying provider's model)
        self.model = "dpc_agent"  # Identifier for token counting

        # Agent managers (per-agent architecture)
        self._manager = None  # DEPRECATED: Single manager (backwards compatibility)
        self._managers: Dict[str, "DpcAgentManager"] = {}  # NEW: Multiple managers (one per agent)
        self._service = None  # Injected by LLMManager during initialization

        logger.info(f"DpcAgentProvider '{alias}' initialized (tools={len(self.enabled_tools)}, "
                   f"budget=${self.budget_usd}, consciousness={self.background_consciousness})")

    def set_service(self, service: "CoreService") -> None:
        """
        Inject CoreService reference for LLMManager access.

        Called by CoreService during initialization to enable
        the agent to use DPC's AI providers.
        """
        self._service = service
        logger.debug(f"DpcAgentProvider '{self.alias}': CoreService injected")

    def get_manager(self, agent_id: str) -> "DpcAgentManager":
        """
        Get or create a manager for a specific agent.

        Args:
            agent_id: The agent ID to get/create manager for

        Returns:
            DpcAgentManager instance for the specified agent

        Raises:
            RuntimeError: If CoreService not injected
        """
        # Check if manager already exists for this agent
        if agent_id in self._managers:
            return self._managers[agent_id]

        # Validate service reference
        if self._service is None:
            raise RuntimeError(
                f"DpcAgentProvider '{self.alias}' requires CoreService reference. "
                "Ensure the provider is properly initialized by CoreService."
            )

        # Import here to avoid circular imports
        from dpc_client_core.managers.agent_manager import DpcAgentManager
        from dpc_client_core.dpc_agent.utils import load_agent_config

        # Load per-agent config to pick up compute_host, provider_alias, context_window
        agent_file_config = load_agent_config(agent_id) or {}
        compute_host = agent_file_config.get("compute_host", "")
        provider_alias = agent_file_config.get("provider_alias", "")
        context_window = agent_file_config.get("context_window")

        # Create new manager for this agent
        logger.debug(f"DpcAgentProvider '{self.alias}': Creating new manager for agent '{agent_id}'")
        manager_config = {
            "tools": self.enabled_tools,
            "background_consciousness": self.background_consciousness,
            "budget_usd": self.budget_usd,
            "max_rounds": self.max_rounds,
            "compute_host": compute_host,
        }
        if provider_alias:
            manager_config["provider_alias"] = provider_alias
        if context_window:
            manager_config["context_window"] = int(context_window)
        manager = DpcAgentManager(self._service, manager_config, agent_id=agent_id)  # Pass agent_id for per-agent configuration

        # Cache for reuse
        self._managers[agent_id] = manager
        logger.info(f"DpcAgentProvider '{self.alias}': Created manager for agent '{agent_id}'")
        return manager

    async def _ensure_manager(self, agent_id: Optional[str] = None) -> "DpcAgentManager":
        """
        Ensure the agent manager is initialized.

        Args:
            agent_id: Optional specific agent ID to load (for per-agent managers)

        Returns:
            DpcAgentManager instance

        Raises:
            RuntimeError: If CoreService not injected or initialization fails
        """
        # NEW: If agent_id provided, use per-agent manager
        if agent_id:
            manager = self.get_manager(agent_id)
            # Ensure the manager is started (lazy initialization)
            if manager._agent is None:
                await manager.start()
                logger.info(f"DpcAgentProvider '{self.alias}': Per-agent manager started for '{agent_id}'")
            return manager

        # FALLBACK: Use legacy single manager for backwards compatibility
        if self._manager is not None:
            return self._manager

        if self._service is None:
            raise RuntimeError(
                f"DpcAgentProvider '{self.alias}' requires CoreService reference. "
                "Ensure the provider is properly initialized by CoreService."
            )

        # Import here to avoid circular imports
        from dpc_client_core.managers.agent_manager import DpcAgentManager

        # Create manager with configuration
        self._manager = DpcAgentManager(self._service, {
            "tools": self.enabled_tools,
            "background_consciousness": self.background_consciousness,
            "budget_usd": self.budget_usd,
            "max_rounds": self.max_rounds,
        }, agent_id=None)  # No agent_id for singleton manager

        # Start the agent
        await self._manager.start()
        logger.info(f"DpcAgentProvider '{self.alias}': Agent manager started (singleton mode)")

        return self._manager

    async def generate_response(self, prompt: str, conversation_id: str = None, agent_llm_provider: str = None, **kwargs) -> str:
        """
        Process a message through the autonomous agent.

        Args:
            prompt: User message text
            conversation_id: Optional conversation ID for progress tracking
            agent_llm_provider: Optional underlying LLM provider for this agent (Phase 3)
            **kwargs: Additional arguments (ignored)

        Returns:
            Agent's response text

        Raises:
            RuntimeError: If agent processing fails
        """
        try:
            # Extract agent_id from conversation_id for per-agent manager selection
            agent_id = None
            if conversation_id and conversation_id.startswith("agent_"):
                agent_id = conversation_id
                logger.debug(f"DpcAgentProvider '{self.alias}': Extracted agent_id '{agent_id}' from conversation_id")

            if not agent_id:
                raise ValueError(
                    "DpcAgentProvider requires a conversation_id starting with 'agent_' "
                    "(e.g. 'agent_001'). Got: conversation_id=%r" % conversation_id
                )

            manager = await self._ensure_manager(agent_id=agent_id)

            # Use provided conversation_id or generate one
            if not conversation_id:
                import hashlib
                conversation_id = hashlib.md5(prompt.encode()).hexdigest()[:16]

            # Process through agent with DPC context
            response = await manager.process_message(
                message=prompt,
                conversation_id=conversation_id,
                include_context=True,
                agent_llm_provider=agent_llm_provider,  # Phase 3: per-agent provider selection
            )

            logger.info(f"DpcAgentProvider '{self.alias}': Generated response ({len(response)} chars)")
            return response

        except Exception as e:
            logger.error(f"DpcAgentProvider '{self.alias}' failed: {e}", exc_info=True)
            raise RuntimeError(f"Embedded agent failed: {e}") from e

    async def generate_response_stream(
        self,
        prompt: str,
        on_chunk: callable,
        conversation_id: str = None,
        agent_llm_provider: str = None,
        **kwargs
    ) -> str:
        """
        Process a message through the autonomous agent with streaming.

        Args:
            prompt: User message text
            on_chunk: Async callback for each text chunk: await on_chunk(chunk, conversation_id)
            conversation_id: Optional conversation ID for progress tracking
            agent_llm_provider: Optional underlying LLM provider for this agent (Phase 3)
            **kwargs: Additional arguments (ignored)

        Returns:
            Agent's response text (accumulated from all chunks)
        """
        try:
            # NEW: Extract agent_id from conversation_id for per-agent manager selection
            agent_id = None
            if conversation_id and conversation_id.startswith("agent_"):
                agent_id = conversation_id
                logger.debug(f"DpcAgentProvider '{self.alias}': Extracted agent_id '{agent_id}' from conversation_id (streaming)")

            # NEW: Pass agent_id to _ensure_manager for per-agent manager selection
            manager = await self._ensure_manager(agent_id=agent_id)

            # Use provided conversation_id or generate one
            if not conversation_id:
                import hashlib
                conversation_id = hashlib.md5(prompt.encode()).hexdigest()[:16]

            # Note: We don't pass on_chunk to manager - the manager handles
            # broadcasting directly via local_api. This avoids callback chain issues.
            response = await manager.process_message(
                message=prompt,
                conversation_id=conversation_id,
                include_context=True,
                on_stream_chunk=None,  # Manager handles broadcast directly
                agent_llm_provider=agent_llm_provider,  # Phase 3: per-agent provider selection
            )

            logger.info(f"DpcAgentProvider '{self.alias}': Generated streaming response ({len(response)} chars)")
            return response

        except Exception as e:
            logger.error(f"DpcAgentProvider '{self.alias}' streaming failed: {e}", exc_info=True)
            raise RuntimeError(f"Embedded agent streaming failed: {e}") from e

    def supports_vision(self) -> bool:
        """
        The agent supports vision through VLM tools.

        Returns:
            True (agent has analyze_screenshot and vlm_query tools)
        """
        return True

    async def generate_with_vision(
        self, prompt: str, images: List[Dict[str, Any]], **kwargs
    ) -> str:
        """
        Handle vision queries by routing through the agent.

        The agent can use VLM tools (analyze_screenshot, vlm_query)
        to process images.

        Args:
            prompt: Text prompt
            images: List of image dicts with path/mime_type/base64
            **kwargs: Additional parameters (ignored by agent)

        Returns:
            Agent's response (may include image analysis)
        """
        # For now, delegate to text generation
        # The agent can use VLM tools internally if needed
        # Future: Inject image info into prompt for agent awareness
        enhanced_prompt = prompt

        if images:
            image_info = []
            for img in images:
                if "path" in img:
                    image_info.append(f"[Image: {img['path']}]")
                elif "base64" in img:
                    image_info.append("[Image: base64 data]")

            if image_info:
                enhanced_prompt = f"{prompt}\n\nAttached images:\n" + "\n".join(image_info)

        return await self.generate_response(enhanced_prompt)

    def supports_thinking(self) -> bool:
        """
        The agent supports extended thinking via background consciousness.

        Returns:
            True if background_consciousness is enabled
        """
        return self.background_consciousness

    def get_thinking_params(self) -> Dict[str, Any]:
        """
        Return agent-specific thinking parameters.

        Returns:
            Dict with consciousness configuration
        """
        return {
            "consciousness_mode": "background" if self.background_consciousness else "disabled",
            "enabled": self.background_consciousness,
        }

    def get_status(self) -> Dict[str, Any]:
        """
        Get agent status information.

        Returns:
            Dict with agent status (initialized, config, etc.)
        """
        if self._manager is None:
            return {
                "initialized": False,
                "alias": self.alias,
            }

        return {
            "initialized": True,
            "alias": self.alias,
            "manager_status": self._manager.get_status(),
        }

    async def shutdown(self) -> None:
        """
        Shutdown the agent manager gracefully.
        """
        if self._manager is not None:
            await self._manager.stop()
            self._manager = None
            logger.info(f"DpcAgentProvider '{self.alias}': Agent manager stopped")
