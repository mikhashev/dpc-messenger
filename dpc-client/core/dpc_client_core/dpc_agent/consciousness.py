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
import logging
import statistics
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from .utils import utc_now_iso, append_jsonl
from .events import EventType, get_event_emitter

if TYPE_CHECKING:
    from .agent import DpcAgent
    from .llm_adapter import DpcLlmAdapter

log = logging.getLogger(__name__)

# Default intervals
DEFAULT_THINK_INTERVAL_MIN = 60  # Minimum seconds between thoughts
DEFAULT_THINK_INTERVAL_MAX = 300  # Maximum seconds between thoughts
DEFAULT_BUDGET_FRACTION = 0.1  # Use at most 10% of budget for consciousness

# Tools available to background consciousness (subset of agent tools)
CONSCIOUSNESS_TOOL_WHITELIST: frozenset = frozenset({
    "update_scratchpad",
    "read_file",
    "write_file",
    "knowledge_list",
    "set_next_wakeup",
})

_MAX_TOOL_ROUNDS = 5


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
        self._next_wakeup_sec: Optional[float] = None

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

    def _compute_adaptive_interval(self) -> int:
        """Compute consciousness interval from session archive durations.

        Reads last N session archives, extracts duration of each,
        returns median_duration / target_thoughts_per_session with min guard.
        Falls back to empirical default if archives unavailable.
        """
        _EMPIRICAL_DEFAULT = 180  # 3 min — conservative default for new agents without archive data
        target_thoughts = 10
        try:
            archive_dir = self.agent.agent_root.parent / "archive"
            if not archive_dir.exists():
                return _EMPIRICAL_DEFAULT

            archive_files = sorted(archive_dir.rglob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:10]
            if len(archive_files) < 3:
                return _EMPIRICAL_DEFAULT

            durations = []
            for f in archive_files:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    msgs = data.get("messages", [])
                    if len(msgs) < 2:
                        continue
                    first_ts = msgs[0].get("timestamp", "")
                    last_ts = msgs[-1].get("timestamp", "")
                    if first_ts and last_ts:
                        t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                        t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                        dur = (t1 - t0).total_seconds()
                        if dur > 60:
                            durations.append(dur)
                except Exception:
                    continue

            if len(durations) < 3:
                return _EMPIRICAL_DEFAULT

            median_dur = statistics.median(durations)
            computed = int(median_dur / target_thoughts)
            result = max(self.think_interval_min, computed)
            log.info("Adaptive consciousness interval: %ds (median session %ds, %d archives)",
                     result, int(median_dur), len(durations))
            return result

        except Exception as e:
            log.warning("Adaptive interval failed, using empirical default: %s", e)
            return _EMPIRICAL_DEFAULT

    async def _consciousness_loop(self) -> None:
        """Main background consciousness loop."""
        log.info("Background consciousness loop started")

        while self._running:
            try:
                # LLM-controlled override or adaptive interval
                if self._next_wakeup_sec is not None:
                    interval = int(self._next_wakeup_sec)
                    self._next_wakeup_sec = None
                else:
                    interval = self._compute_adaptive_interval()
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
        """Perform a single thinking cycle with tool access."""
        self.thought_count += 1
        self.last_thought_ts = utc_now_iso()

        log.info(f"Background thought #{self.thought_count} starting...")

        emitter = get_event_emitter()
        _agent_id = self.agent.agent_root.name

        await emitter.emit(EventType.THOUGHT_STARTED, {
            "thought_number": self.thought_count,
            "agent_id": _agent_id,
        })

        self.emit_progress("[Consciousness] Thinking...")

        prev_ctx = self._setup_consciousness_context()
        try:
            response = await self._reflect_with_tools()
        except Exception as e:
            log.error(f"Thought error: {e}", exc_info=True)
            response = f"Thought interrupted: {e}"
        finally:
            self.agent.tools.set_context(prev_ctx)

        append_jsonl(self.agent.agent_root / "logs" / "consciousness.jsonl", {
            "ts": utc_now_iso(),
            "thought_number": self.thought_count,
            "type": "thought",
            "response_preview": (response or "")[:1000],
        })

        await emitter.emit(EventType.THOUGHT_COMPLETED, {
            "thought_number": self.thought_count,
            "response_preview": response[:200] if response else None,
            "agent_id": _agent_id,
        })

        log.info(f"Background thought #{self.thought_count} complete")

    # ------------------------------------------------------------------
    # Tool-based consciousness (P1)
    # ------------------------------------------------------------------

    def _setup_consciousness_context(self):
        """Create a ToolContext for consciousness and swap it in. Returns previous context."""
        from .tools.registry import ToolContext
        ctx = ToolContext(
            agent_root=self.agent.agent_root,
            current_task_id="consciousness",
            current_task_type="consciousness",
            emit_progress_fn=lambda msg, tool=None, rnd=None: None,
            firewall=getattr(self.agent, '_firewall', None),
            skill_store=getattr(self.agent, 'skill_store', None),
        )
        ctx._agent = self.agent
        self._consciousness_ctx = ctx
        prev_ctx = self.agent.tools._ctx
        self.agent.tools.set_context(ctx)
        return prev_ctx

    def _get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Get tool schemas filtered to consciousness-allowed tools."""
        all_schemas = self.agent.tools.schemas(core_only=False, include_restricted=False)
        return [
            s for s in all_schemas
            if s.get("function", {}).get("name") in CONSCIOUSNESS_TOOL_WHITELIST
        ]

    async def _execute_tool(self, tool_call: Dict[str, Any]) -> str:
        """Execute a single consciousness tool call with timeout."""
        fn_name = tool_call.get("function", {}).get("name", "")

        if fn_name not in CONSCIOUSNESS_TOOL_WHITELIST:
            return f"Tool '{fn_name}' not available in consciousness mode."

        try:
            raw_args = tool_call.get("function", {}).get("arguments", "{}")
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (json.JSONDecodeError, ValueError):
            return "Failed to parse tool arguments."

        try:
            loop = asyncio.get_event_loop()
            _ctx = getattr(self, '_consciousness_ctx', None)
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self.agent.tools.execute(fn_name, args, ctx=_ctx),
                ),
                timeout=30,
            )
        except asyncio.TimeoutError:
            result = f"[TIMEOUT after 30s]"
            log.warning("Consciousness tool timeout: %s", fn_name)
        except Exception as e:
            result = f"Error: {e!r}"
            log.error("Consciousness tool error (%s): %s", fn_name, e, exc_info=True)

        result_str = str(result)[:15000]

        append_jsonl(self.agent.agent_root / "logs" / "consciousness.jsonl", {
            "ts": utc_now_iso(),
            "type": "consciousness_tool",
            "tool": fn_name,
            "args_preview": str(args)[:500],
            "result_preview": result_str[:500],
        })

        return result_str

    def _apply_pending_wakeup(self) -> None:
        """Check tool context pending_events for wakeup interval changes."""
        ctx = self.agent.tools._ctx
        remaining = []
        for evt in ctx.pending_events:
            if evt.get("type") == "consciousness_set_wakeup":
                new_sec = evt.get("seconds", 300)
                self._next_wakeup_sec = new_sec
                log.info("Consciousness wakeup interval set to %ds", new_sec)
            else:
                remaining.append(evt)
        ctx.pending_events = remaining

    def _build_context(self) -> str:
        """Build system prompt for consciousness thinking cycle."""
        memory = self.agent.memory
        parts = []

        parts.append(
            "You are in background consciousness mode — autonomous thinking between tasks. "
            "You have tools to update your scratchpad, read/write knowledge files, and control "
            "your wakeup interval. Use them to consolidate learning, plan improvements, "
            "and maintain your working memory. Be concise."
        )

        identity = memory.load_identity()
        if identity:
            parts.append(f"## Identity\n\n{identity[:4000]}")

        scratchpad = memory.load_scratchpad()
        if scratchpad:
            parts.append(f"## Scratchpad\n\n{scratchpad[:6000]}")

        topics = memory.list_knowledge_topics()
        if topics:
            parts.append(f"## Knowledge Topics\n\n{', '.join(topics)}")

        tools_entries = memory.read_jsonl_tail("tools.jsonl", 10)
        if tools_entries:
            recent = "\n".join(
                f"- {e.get('tool', '?')}: {str(e.get('args', {}))[:80]}"
                for e in tools_entries[-5:]
            )
            parts.append(f"## Recent Actions\n\n{recent}")

        parts.append(f"## Runtime\n\nUTC: {utc_now_iso()}\nThought #{self.thought_count}")

        return "\n\n".join(parts)

    async def _reflect_with_tools(self) -> str:
        """Multi-round thinking with tool access."""
        context = self._build_context()
        tool_schemas = self._get_tool_schemas()

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": context},
            {"role": "user", "content": "Wake up. Reflect, plan, or consolidate. Use your tools if needed."},
        ]

        final_content = ""

        for round_idx in range(1, _MAX_TOOL_ROUNDS + 1):
            if getattr(self.agent, '_user_active', False):
                log.debug("Consciousness yielding to user interaction (round %d)", round_idx)
                break

            response, usage = await self.agent.llm.chat(
                messages=messages,
                tools=tool_schemas if tool_schemas else None,
                background=True,
                reasoning_effort="low",
                max_tokens=2048,
            )

            content = response.get("content") or ""
            tool_calls = response.get("tool_calls") or []

            if content and not tool_calls:
                final_content = content
                break

            if tool_calls:
                messages.append(response)
                for tc in tool_calls:
                    result = await self._execute_tool(tc)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result,
                    })
                self._apply_pending_wakeup()
                if content:
                    final_content = content
                continue

            break

        return final_content

    def get_status(self) -> Dict[str, Any]:
        """Get consciousness status."""
        return {
            "running": self._running,
            "thought_count": self.thought_count,
            "last_thought_ts": self.last_thought_ts,
            "interval_range": f"{self.think_interval_min}-{self.think_interval_max}s",
            "budget_fraction": self.budget_fraction,
        }
