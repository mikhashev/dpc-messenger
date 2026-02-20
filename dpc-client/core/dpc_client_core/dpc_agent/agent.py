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

if TYPE_CHECKING:
    from ..llm_manager import LLMManager

log = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Configuration for the DPC agent."""
    budget_usd: float = 50.0
    max_rounds: int = 200
    tool_whitelist: Optional[set] = None
    background_consciousness: bool = False  # Not implemented in MVP


class DpcAgent:
    """
    Simplified agent for DPC Messenger integration.

    Usage:
        agent = DpcAgent(llm_manager, config)
        response = await agent.process("Hello!", "conv-123")
    """

    def __init__(
        self,
        llm_manager: "LLMManager",
        config: Optional[AgentConfig] = None,
        agent_root: Optional[pathlib.Path] = None,
    ):
        """
        Initialize the agent.

        Args:
            llm_manager: DPC's LLMManager instance
            config: Agent configuration
            agent_root: Storage root (defaults to ~/.dpc/agent/)
        """
        self.config = config or AgentConfig()
        self.agent_root = agent_root or get_agent_root()
        ensure_agent_dirs()

        # Initialize components
        self.llm = DpcLlmAdapter(llm_manager)
        self.tools = ToolRegistry(agent_root=self.agent_root)
        self.memory = Memory(agent_root=self.agent_root)

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

        # Set tool context
        ctx = ToolContext(
            agent_root=self.agent_root,
            current_task_id=conversation_id,
            current_task_type="chat",
            tool_whitelist=self.config.tool_whitelist,
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
            "tools_available": self.tools.available_tools(),
            "memory_files": {
                "scratchpad": self.memory.scratchpad_path().exists(),
                "identity": self.memory.identity_path().exists(),
                "dialogue_summary": self.memory.dialogue_summary_path().exists(),
            },
        }
