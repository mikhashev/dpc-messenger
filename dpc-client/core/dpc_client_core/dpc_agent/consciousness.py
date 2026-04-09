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
import json
import re
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

                # Yield to user interaction — don't compete for LLM provider
                if getattr(self.agent, '_user_active', False):
                    log.debug("Skipping thought — user interaction active")
                    continue

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
        _agent_id = self.agent.agent_root.name

        try:
            # Choose a thought type
            thought_type = self._choose_thought_type()
            log.debug(f"Thought type: {thought_type}")

            # Emit thought started event
            await emitter.emit(EventType.THOUGHT_STARTED, {
                "thought_number": self.thought_count,
                "thought_type": thought_type,
                "agent_id": _agent_id,
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
                "agent_id": _agent_id,
            })

            log.info(f"Background thought #{self.thought_count} complete")

        except Exception as e:
            log.error(f"Thought error: {e}", exc_info=True)
            # Emit thought failed event
            await emitter.emit(EventType.THOUGHT_COMPLETED, {
                "thought_number": self.thought_count,
                "error": str(e)[:200],
                "agent_id": _agent_id,
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
            reflection = memory.load_reflection()
            recent = reflection.get("reflections", [])[-3:]
            recent_str = json.dumps(recent, indent=2, ensure_ascii=False) if recent else "[]"
            return f"""Reflect on your identity using structured format.

Your current identity:
{identity[:8000]}

Recent reflections:
{recent_str}

Respond ONLY with a valid JSON object (no markdown, no explanation):
{{
  "trigger": "session_review | behavioral_pattern | error_detection | user_correction",
  "trigger_details": "what prompted this reflection (max 200 chars)",
  "pattern_detected": "name of pattern or null",
  "severity": "low | medium | high",
  "action_taken": "what you will do differently (max 200 chars)",
  "identity_update": "new self-understanding to add, or null"
}}"""

        elif thought_type == "review_recent_actions":
            tools_entries = memory.read_jsonl_since("tools.jsonl", hours=6.0, max_entries=50)
            if not tools_entries:
                tools_entries = memory.read_jsonl_tail("tools.jsonl", 20)
            if not tools_entries:
                return "No recent actions to review. Consider what you'd like to accomplish."

            recent_tools = "\n".join([
                f"- {e.get('tool', '?')} ({e.get('duration_ms', '?')}ms, round {e.get('round', '?')}): {str(e.get('args', {}))[:80]}"
                for e in tools_entries[-10:]
            ])

            return f"""Review your recent actions and identify patterns.

Recent tool calls:
{recent_tools}

Respond ONLY with a valid JSON object (no markdown, no explanation):
{{
  "observation": "what you notice about your recent actions (max 300 chars)",
  "pattern_detected": "name of pattern (e.g. 'repetitive_search', 'tool_avoidance') or null",
  "severity": "low | medium | high",
  "action_suggestion": "specific change to try next time, or null",
  "confidence": 0.7
}}"""

        elif thought_type == "plan_improvements":
            scratchpad = memory.load_scratchpad()
            return f"""Plan improvements to your capabilities based on current state.

Current focus (from scratchpad):
{scratchpad[:1500]}

Respond ONLY with a valid JSON object (no markdown, no explanation):
{{
  "observation": "what capability gap or weakness you identified (max 300 chars)",
  "pattern_detected": "recurring theme or null (e.g. 'stalled_task', 'missing_skill')",
  "severity": "low | medium | high",
  "action_suggestion": "one specific, actionable improvement to make (required, max 300 chars)",
  "confidence": 0.7
}}"""

        elif thought_type == "consolidate_memory":
            progress_entries = memory.read_jsonl_tail("progress.jsonl", 30)
            progress_summary = "\n".join([
                f"- {e.get('text', '')[:150]}"
                for e in progress_entries[-15:]
            ]) if progress_entries else "No recent progress to consolidate."

            return f"""Consolidate recent experiences into lasting knowledge.

Recent progress:
{progress_summary}

Respond ONLY with a valid JSON object (no markdown, no explanation):
{{
  "observation": "key insight worth remembering long-term (max 300 chars)",
  "pattern_detected": "cross-session pattern or null (e.g. 'repeated_mistake', 'improving_efficiency')",
  "severity": "low | medium | high",
  "action_suggestion": "what to update in scratchpad or knowledge, or null",
  "confidence": 0.7
}}"""

        elif thought_type == "explore_curiosity":
            topics = memory.list_knowledge_topics()
            topics_str = ", ".join(topics) if topics else "none yet"

            return f"""Explore a topic you're curious about.

Your current knowledge topics: {topics_str}

Respond ONLY with a valid JSON object (no markdown, no explanation):
{{
  "observation": "what topic interests you and why (max 300 chars)",
  "pattern_detected": null,
  "severity": "low",
  "action_suggestion": "specific question or exploration to pursue, or null",
  "confidence": 0.5
}}"""

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

            # Call LLM without tools (use background provider to avoid rate limit competition with chat)
            response, usage = await self.agent.llm.chat(messages, tools=None, background=True)

            return response.get("content", "")

        except Exception as e:
            log.error(f"Reflection error: {e}")
            return f"Reflection interrupted: {e}"

    def _log_thought(self, thought_type: str, prompt: str, response: str) -> None:
        """Log the thought to the consciousness log.

        Attempts to parse the response as structured JSON. Falls back to
        freeform format for backward compatibility if parsing fails.
        Skips duplicate thoughts (same type + similar observation within 1h).
        """
        agent_root = self.agent.agent_root

        # Try to parse structured JSON response
        structured = self._parse_structured_response(response)

        # Dedup: skip if last thought has same type and similar observation within 1h
        if structured and self._is_duplicate_thought(agent_root, thought_type, structured):
            log.info(f"Consciousness: skipped duplicate {thought_type} thought (same observation <1h)")
            return

        if structured:
            entry = {
                "ts": utc_now_iso(),
                "thought_number": self.thought_count,
                "type": thought_type,
                "observation": str(structured.get("observation", ""))[:300],
                "pattern_detected": structured.get("pattern_detected"),
                "severity": structured.get("severity", "low"),
                "action_suggestion": structured.get("action_suggestion"),
                "confidence": min(1.0, max(0.0, float(structured.get("confidence", 0.5)))),
            }
        else:
            # Fallback: freeform response (backward compat)
            entry = {
                "ts": utc_now_iso(),
                "thought_number": self.thought_count,
                "type": thought_type,
                "prompt_preview": prompt[:500],
                "response_preview": response[:1000],
            }

        append_jsonl(agent_root / "logs" / "consciousness.jsonl", entry)

        # Act on high-severity suggestions: append to scratchpad
        if (
            structured
            and structured.get("severity") in ("medium", "high")
            and structured.get("action_suggestion")
        ):
            try:
                action = str(structured["action_suggestion"])[:300]
                severity = structured["severity"]
                scratchpad = self.agent.memory.load_scratchpad()
                note = f"\n\n## Consciousness Note ({severity})\n{action}\n"
                if note.strip() not in scratchpad:
                    self.agent.memory.save_scratchpad(scratchpad + note)
                    log.info(f"Consciousness wrote {severity} action to scratchpad: {action[:80]}")
            except Exception as e:
                log.debug(f"Failed to write consciousness action to scratchpad: {e}")

    @staticmethod
    def _is_duplicate_thought(agent_root, thought_type: str, structured: dict) -> bool:
        """Check if the last logged thought is a duplicate (same type + similar observation within 1h)."""
        try:
            log_path = agent_root / "logs" / "consciousness.jsonl"
            if not log_path.exists():
                return False
            # Read last line
            last_line = ""
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last_line = line
            if not last_line:
                return False
            import json
            from datetime import datetime, timezone, timedelta
            last = json.loads(last_line)
            # Check same type
            if last.get("type") != thought_type:
                return False
            # Check within 1 hour
            last_ts = last.get("ts", "")
            if last_ts:
                last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                if (now - last_dt) > timedelta(hours=1):
                    return False
            # Check observation similarity (simple prefix match)
            last_obs = str(last.get("observation", ""))[:100]
            new_obs = str(structured.get("observation", ""))[:100]
            if not last_obs or not new_obs:
                return False
            # Consider duplicate if first 100 chars match >80%
            common = sum(1 for a, b in zip(last_obs, new_obs) if a == b)
            max_len = max(len(last_obs), len(new_obs), 1)
            return (common / max_len) > 0.8
        except Exception:
            return False

    @staticmethod
    def _parse_structured_response(response: str) -> Optional[dict]:
        """Try to parse a structured JSON response from LLM.

        Handles common cases: raw JSON, JSON in markdown code blocks,
        JSON with surrounding text.
        """
        if not response:
            return None

        text = response.strip()

        # Structured response must be a dict with at least one known field
        _known_fields = {"observation", "trigger", "pattern_detected", "severity", "action_suggestion", "confidence"}

        def _is_structured(d: dict) -> bool:
            return isinstance(d, dict) and bool(_known_fields & d.keys())

        # Try direct parse
        try:
            result = json.loads(text)
            if _is_structured(result):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        # Try extracting JSON from markdown code block (greedy to handle nested braces)
        json_match = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.DOTALL)
        if json_match:
            try:
                result = json.loads(json_match.group(1))
                if _is_structured(result):
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

        # Try finding first { ... } block
        brace_start = text.find('{')
        brace_end = text.rfind('}')
        if brace_start >= 0 and brace_end > brace_start:
            try:
                result = json.loads(text[brace_start:brace_end + 1])
                if _is_structured(result):
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

        return None

    async def _apply_thought_result(self, thought_type: str, response: str) -> None:
        """
        Apply the result of a thought to memory.

        Depending on thought type, this may update scratchpad,
        identity, or knowledge base.
        """
        memory = self.agent.memory

        try:
            if thought_type == "reflect_on_identity":
                # Parse structured reflection and save to reflection.json
                try:
                    # Try to extract JSON from response
                    text = response.strip()
                    if text.startswith("```"):
                        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                    entry = json.loads(text)
                    entry["timestamp"] = utc_now_iso()

                    reflection_data = memory.load_reflection()
                    reflections = reflection_data.get("reflections", [])
                    max_entries = reflection_data.get("meta", {}).get("max_reflections", 50)
                    reflections.append(entry)
                    if len(reflections) > max_entries:
                        reflections = reflections[-max_entries:]
                    reflection_data["reflections"] = reflections
                    memory.save_reflection(reflection_data)
                    log.debug("Structured reflection saved to reflection.json")
                except (json.JSONDecodeError, ValueError):
                    log.debug("Identity reflection not valid JSON, logged only")


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
