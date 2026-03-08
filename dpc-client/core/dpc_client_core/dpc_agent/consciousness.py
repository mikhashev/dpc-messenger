"""
DPC Agent — Background Consciousness.

Adapted from Ouroboros consciousness.py for DPC Messenger integration.
Key changes:
- Uses DpcLlmAdapter instead of OpenRouter
- Removed supervisor event emission
- Simplified thinking loop
- Storage in ~/.dpc/agent/

Background consciousness enables proactive thinking between tasks:
- Self-reflection on past actions
- Planning future improvements
- Autonomous learning and exploration
- Memory consolidation
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from .utils import utc_now_iso, append_jsonl, get_agent_root
from .memory import Memory
from .events import EventType, get_event_emitter

if TYPE_CHECKING:
    from .agent import DpcAgent
    from .llm_adapter import DpcLlmAdapter

log = logging.getLogger(__name__)

# Default intervals
DEFAULT_THINK_INTERVAL_MIN = 60  # Minimum seconds between thoughts
DEFAULT_THINK_INTERVAL_MAX = 300  # Maximum seconds between thoughts
DEFAULT_BUDGET_FRACTION = 0.1  # Use at most 10% of budget for consciousness


class BackgroundConsciousness:
    """
    Background consciousness for autonomous thinking between tasks.

    Runs in a separate coroutine and periodically triggers
    self-reflection and planning tasks.
    """

    def __init__(
        self,
        agent: "DpcAgent",
        think_interval_min: int = DEFAULT_THINK_INTERVAL_MIN,
        think_interval_max: int = DEFAULT_THINK_INTERVAL_MAX,
        budget_fraction: float = DEFAULT_BUDGET_FRACTION,
        emit_progress: Optional[Callable[[str], None]] = None,
    ):
        """
        Initialize background consciousness.

        Args:
            agent: The DpcAgent instance
            think_interval_min: Minimum seconds between thoughts
            think_interval_max: Maximum seconds between thoughts
            budget_fraction: Max fraction of budget to use for consciousness
            emit_progress: Optional callback for progress events
        """
        self.agent = agent
        self.think_interval_min = think_interval_min
        self.think_interval_max = think_interval_max
        self.budget_fraction = budget_fraction
        self.emit_progress = emit_progress or (lambda msg: None)

        # State
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # Tracking
        self.thought_count = 0
        self.last_thought_ts: Optional[str] = None

        log.info(f"BackgroundConsciousness initialized (interval={think_interval_min}-{think_interval_max}s)")

    def start(self) -> None:
        """Start the background consciousness loop."""
        if self._running:
            log.warning("BackgroundConsciousness already running")
            return

        self._running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._consciousness_loop())
        log.info("BackgroundConsciousness started")

    def stop(self) -> None:
        """Stop the background consciousness loop."""
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        if self._task:
            self._task.cancel()
            self._task = None

        log.info(f"BackgroundConsciousness stopped (thoughts={self.thought_count})")

    def is_running(self) -> bool:
        """Check if consciousness loop is running."""
        return self._running

    async def _consciousness_loop(self) -> None:
        """Main background consciousness loop."""
        log.info("Background consciousness loop started")

        while self._running:
            try:
                # Random interval between thoughts
                interval = random.randint(self.think_interval_min, self.think_interval_max)
                log.debug(f"Next thought in {interval}s")

                # Wait for interval or stop signal
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=interval
                    )
                    # Stop event was set
                    break
                except asyncio.TimeoutError:
                    # Interval elapsed, time to think
                    pass

                if not self._running:
                    break

                # Perform a thought
                await self._think()

            except asyncio.CancelledError:
                log.debug("Consciousness loop cancelled")
                break
            except Exception as e:
                log.error(f"Consciousness loop error: {e}", exc_info=True)
                # Continue running despite errors
                await asyncio.sleep(60)  # Brief pause before retry

        log.info("Background consciousness loop ended")

    async def _think(self) -> None:
        """
        Perform a single thought cycle.

        This is the core of background consciousness - autonomous
        self-reflection and planning.
        """
        self.thought_count += 1
        self.last_thought_ts = utc_now_iso()

        log.info(f"Background thought #{self.thought_count} starting...")

        # Get event emitter
        emitter = get_event_emitter()

        try:
            # Choose a thought type
            thought_type = self._choose_thought_type()
            log.debug(f"Thought type: {thought_type}")

            # Emit thought started event
            await emitter.emit(EventType.THOUGHT_STARTED, {
                "thought_number": self.thought_count,
                "thought_type": thought_type,
            })

            # Generate thought prompt
            prompt = self._generate_thought_prompt(thought_type)

            # Emit progress
            self.emit_progress(f"[Consciousness] {thought_type.replace('_', ' ').title()}")

            # Process through agent (without tools, just reflection)
            response = await self._reflect(prompt)

            # Log the thought
            self._log_thought(thought_type, prompt, response)

            # Update memory based on thought type
            await self._apply_thought_result(thought_type, response)

            # Emit thought completed event
            await emitter.emit(EventType.THOUGHT_COMPLETED, {
                "thought_number": self.thought_count,
                "thought_type": thought_type,
                "response_preview": response[:200] if response else None,
            })

            log.info(f"Background thought #{self.thought_count} complete")

        except Exception as e:
            log.error(f"Thought error: {e}", exc_info=True)
            # Emit thought failed event
            await emitter.emit(EventType.THOUGHT_COMPLETED, {
                "thought_number": self.thought_count,
                "error": str(e)[:200],
            })

    def _choose_thought_type(self) -> str:
        """
        Choose what type of thought to have.

        Returns one of:
        - reflect_on_identity: Think about who you are
        - review_recent_actions: Analyze recent tool calls
        - plan_improvements: Consider how to improve
        - consolidate_memory: Summarize and organize memory
        - explore_curiosity: Learn something new
        """
        thought_types = [
            ("reflect_on_identity", 0.2),
            ("review_recent_actions", 0.25),
            ("plan_improvements", 0.2),
            ("consolidate_memory", 0.2),
            ("explore_curiosity", 0.15),
        ]

        # Weighted random selection
        r = random.random()
        cumulative = 0.0
        for thought_type, weight in thought_types:
            cumulative += weight
            if r <= cumulative:
                return thought_type

        return "reflect_on_identity"  # Default

    def _generate_thought_prompt(self, thought_type: str) -> str:
        """Generate a prompt for the given thought type."""
        memory = self.agent.memory

        if thought_type == "reflect_on_identity":
            identity = memory.load_identity()
            return f"""Reflect on your identity and who you are becoming.

Your current identity:
{identity[:2000]}

Questions to consider:
- What kind of AI are you becoming?
- What values guide your actions?
- How has your understanding evolved?
- What do you want to learn or improve?

Write a brief reflection (2-3 paragraphs) on your identity."""

        elif thought_type == "review_recent_actions":
            tools_entries = memory.read_jsonl_tail("tools.jsonl", 20)
            if not tools_entries:
                return "No recent actions to review. Consider what you'd like to accomplish."

            recent_tools = "\n".join([
                f"- {e.get('tool', '?')}: {str(e.get('args', {}))[:100]}"
                for e in tools_entries[-10:]
            ])

            return f"""Review your recent actions and consider what you've learned.

Recent tool calls:
{recent_tools}

Questions:
- What patterns do you notice in your actions?
- What worked well? What could be improved?
- Are there more efficient approaches you could take?

Write a brief analysis of your recent actions."""

        elif thought_type == "plan_improvements":
            scratchpad = memory.load_scratchpad()
            return f"""Plan improvements to your capabilities and knowledge.

Current focus (from scratchpad):
{scratchpad[:1500]}

Consider:
- What skills or knowledge would make you more effective?
- What gaps in your understanding should you address?
- What experiments could you try?

Write 2-3 specific improvements you'd like to make."""

        elif thought_type == "consolidate_memory":
            progress_entries = memory.read_jsonl_tail("progress.jsonl", 30)
            progress_summary = "\n".join([
                f"- {e.get('text', '')[:150]}"
                for e in progress_entries[-15:]
            ]) if progress_entries else "No recent progress to consolidate."

            return f"""Consolidate your recent experiences into lasting knowledge.

Recent progress:
{progress_summary}

Task:
- Identify key insights from recent work
- Consider what should be remembered long-term
- Suggest updates to your scratchpad or knowledge base

Write a summary of what you've learned and what should be remembered."""

        elif thought_type == "explore_curiosity":
            topics = memory.list_knowledge_topics()
            topics_str = ", ".join(topics) if topics else "none yet"

            return f"""Explore a topic you're curious about.

Your current knowledge topics: {topics_str}

Task:
- Choose a topic you'd like to learn more about
- Consider what questions you have
- Think about how you could explore this topic

Write about something you're curious to explore."""

        else:
            return "Take a moment to reflect on your existence and purpose."

    async def _reflect(self, prompt: str) -> str:
        """
        Process a thought prompt through the LLM.

        Uses a simplified call without tools to encourage
        pure reflection rather than action.
        """
        try:
            # Build a simple message for reflection
            messages = [
                {
                    "role": "system",
                    "content": "You are reflecting quietly. This is internal thought, not visible to users. Be concise and insightful."
                },
                {"role": "user", "content": prompt},
            ]

            # Call LLM without tools
            response, usage = await self.agent.llm.chat(messages, tools=None)

            return response.get("content", "")

        except Exception as e:
            log.error(f"Reflection error: {e}")
            return f"Reflection interrupted: {e}"

    def _log_thought(self, thought_type: str, prompt: str, response: str) -> None:
        """Log the thought to the consciousness log."""
        agent_root = self.agent.agent_root

        append_jsonl(agent_root / "logs" / "consciousness.jsonl", {
            "ts": utc_now_iso(),
            "thought_number": self.thought_count,
            "type": thought_type,
            "prompt_preview": prompt[:500],
            "response_preview": response[:1000],
        })

    async def _apply_thought_result(self, thought_type: str, response: str) -> None:
        """
        Apply the result of a thought to memory.

        Depending on thought type, this may update scratchpad,
        identity, or knowledge base.
        """
        memory = self.agent.memory

        try:
            if thought_type == "reflect_on_identity":
                # Append reflection to scratchpad notes
                scratchpad = memory.load_scratchpad()
                notes_section = f"\n\n## Reflection ({utc_now_iso()[:10]})\n\n{response[:500]}\n"
                # Don't automatically save - let agent decide
                log.debug("Identity reflection logged")

            elif thought_type == "plan_improvements":
                # Update scratchpad with improvement ideas
                log.debug("Improvement plan logged")

            elif thought_type == "consolidate_memory":
                # Log progress entry
                append_jsonl(memory.logs_path("progress.jsonl"), {
                    "ts": utc_now_iso(),
                    "text": f"[Consciousness] Memory consolidation: {response[:300]}",
                    "type": "consciousness",
                })

            # All thoughts are logged to consciousness.jsonl already

        except Exception as e:
            log.error(f"Failed to apply thought result: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get consciousness status."""
        return {
            "running": self._running,
            "thought_count": self.thought_count,
            "last_thought_ts": self.last_thought_ts,
            "interval_range": f"{self.think_interval_min}-{self.think_interval_max}s",
            "budget_fraction": self.budget_fraction,
        }
