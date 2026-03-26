"""
DPC Agent — Skill Reflection (Write Phase).

Implements the Write phase of the Memento-Skills Read-Write Reflective Learning loop.

After each task where a skill was used:
1. record_outcome()  — synchronously updates _stats.json (always, fast)
2. reflect_async()   — background LLM call to assess and optionally improve the skill

Gating:
- reflection only fires when rounds >= REFLECTION_ROUNDS_THRESHOLD (default 5)
- skill writes require firewall permission: dpc_agent.skills.self_modify = true
- without permission, improvements queue in pending_improvements.jsonl (shadow mode)
- full rewrites require dpc_agent.skills.rewrite_existing = true (default false)
- appending only requires dpc_agent.skills.self_modify = true
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .utils import utc_now_iso, append_jsonl, write_text

log = logging.getLogger(__name__)

# Minimum LLM rounds before we bother reflecting on skill effectiveness.
# Below this threshold the task was simple enough that strategy gaps are unlikely.
REFLECTION_ROUNDS_THRESHOLD = 5


class SkillReflector:
    """
    Post-task Write phase — records outcomes and optionally improves skill strategies.

    Usage (in agent.py after run_llm_loop returns):
        used_skills = reflector.record_outcome(llm_trace, usage)
        if used_skills and usage.get("rounds", 0) >= REFLECTION_ROUNDS_THRESHOLD:
            asyncio.ensure_future(reflector.reflect_async(
                used_skills[0], message, llm_trace, usage
            ))
    """

    def __init__(
        self,
        skill_store: Any,
        llm: Optional[Any] = None,           # DpcLlmAdapter
        firewall: Optional[Any] = None,       # ContextFirewall
        firewall_profile: Optional[str] = None,  # Per-agent profile key
    ):
        self.skill_store = skill_store
        self.llm = llm
        self.firewall = firewall
        self.firewall_profile = firewall_profile

    # -------------------------------------------------------------------------
    # Synchronous: always called, fast
    # -------------------------------------------------------------------------

    def record_outcome(self, llm_trace: Dict[str, Any], usage: Dict[str, Any]) -> List[str]:
        """
        Record skill outcomes from a completed task into _stats.json.

        Returns list of skill names that were used (empty if none).
        """
        skill_calls = [
            tc for tc in llm_trace.get("tool_calls", [])
            if tc.get("tool") == "execute_skill" and not tc.get("is_error")
        ]
        if not skill_calls:
            return []

        rounds = usage.get("rounds", 0)

        # Success heuristic: task succeeded if the last 3 tool calls had no errors.
        # This is deliberately conservative — a task with mostly successful tools
        # but one retry at the end is still a success.
        tool_calls = llm_trace.get("tool_calls", [])
        recent_errors = sum(1 for tc in tool_calls[-3:] if tc.get("is_error"))
        success = recent_errors == 0

        used_skills: List[str] = []
        for call in skill_calls:
            skill_name = call.get("args", {}).get("skill_name", "")
            if skill_name:
                try:
                    self.skill_store.record_outcome(skill_name, success=success, rounds=rounds)
                    used_skills.append(skill_name)
                except Exception as e:
                    log.debug(f"Failed to record outcome for skill '{skill_name}': {e}")

        return used_skills

    # -------------------------------------------------------------------------
    # Async: only called when threshold exceeded, runs in background
    # -------------------------------------------------------------------------

    async def reflect_async(
        self,
        skill_name: str,
        task_text: str,
        llm_trace: Dict[str, Any],
        usage: Dict[str, Any],
    ) -> None:
        """
        Background LLM reflection — assess skill effectiveness and suggest one improvement.

        Runs as an asyncio background task (fire-and-forget from agent.py).
        Never raises — all errors are logged at DEBUG level.
        """
        if not self.llm:
            return

        try:
            await self._do_reflect(skill_name, task_text, llm_trace, usage)
        except Exception as e:
            log.debug(f"Skill reflection error for '{skill_name}': {e}", exc_info=True)

    async def _do_reflect(
        self,
        skill_name: str,
        task_text: str,
        llm_trace: Dict[str, Any],
        usage: Dict[str, Any],
    ) -> None:
        skill_body = self.skill_store.load_skill_body(skill_name)
        if not skill_body:
            return

        rounds = usage.get("rounds", 0)
        tool_calls = llm_trace.get("tool_calls", [])
        stats = self.skill_store.get_stats().get(skill_name, {})
        failure_count = stats.get("failure_count", 0)
        success_count = stats.get("success_count", 0)

        # Summarise what actually happened
        recent_tools = [tc.get("tool") for tc in tool_calls[-10:] if not tc.get("is_error")]

        reflection_prompt = f"""You are reflecting on the execution of the '{skill_name}' skill strategy.

## Skill Strategy (currently)
{skill_body[:2000]}

## What happened
Task: {task_text[:400]}
LLM rounds used: {rounds}
Tools called (last 10, successful): {recent_tools}
Cumulative stats: {success_count} successes, {failure_count} failures

## Your job
Decide if the strategy has a specific, fixable gap that caused unnecessary rounds.
Respond with JSON only — no explanation outside the JSON block:

{{
  "needs_improvement": true/false,
  "reason": "one sentence explaining the gap (only if needs_improvement)",
  "improvement_type": "append",
  "improvement_content": "markdown text to append under a new ## Lessons Learned section (only if needs_improvement)"
}}

Only set needs_improvement=true when:
- A step was clearly missing and its absence caused extra rounds
- The strategy had incorrect advice that had to be worked around
- A Common Failure was hit that isn't already documented

Do NOT suggest improvements for: normal variance, minor wording, or tasks that succeeded quickly."""

        try:
            msg, _ = await self.llm.chat(
                [{"role": "user", "content": reflection_prompt}],
                tools=None,
            )
        except Exception as e:
            log.debug(f"Reflection LLM call failed for '{skill_name}': {e}")
            return

        content = (msg or {}).get("content", "")
        if not content:
            return

        # Extract JSON — LLMs sometimes wrap it in ```json``` fences
        json_match = re.search(
            r'\{[^{}]*"needs_improvement"[^{}]*\}', content, re.DOTALL
        )
        if not json_match:
            log.debug(f"No JSON in reflection response for '{skill_name}'")
            return

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError:
            return

        if not data.get("needs_improvement"):
            return

        improvement_type = data.get("improvement_type", "none")
        improvement_content = str(data.get("improvement_content", "")).strip()
        reason = str(data.get("reason", "")).strip()

        if improvement_type != "append" or not improvement_content:
            return

        # Permission gate
        can_modify = self._get_skill_permission("self_modify")

        if can_modify:
            self._apply_append(skill_name, improvement_content, reason)
        else:
            self._log_pending(skill_name, improvement_content, reason)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _get_skill_permission(self, operation: str) -> bool:
        if not self.firewall:
            return False
        try:
            return self.firewall.get_agent_skill_permission(operation, profile_name=self.firewall_profile)
        except Exception:
            return False

    def _apply_append(self, skill_name: str, content: str, reason: str) -> None:
        """Append improvement to the skill's SKILL.md."""
        current = self.skill_store.load_skill_content(skill_name)
        if not current:
            return

        today = utc_now_iso()[:10]
        append_section = (
            f"\n\n## Lessons Learned\n\n"
            f"*Added {today}: {reason}*\n\n"
            f"{content}"
        )
        self.skill_store.save_skill(skill_name, current + append_section)

        # Record in stats improvement_log
        stats = self.skill_store.get_stats()
        entry = stats.get(skill_name, {})
        log_list = entry.get("improvement_log", [])
        manifest = self.skill_store.load_manifest(skill_name)
        log_list.append({
            "version": (manifest.version if manifest else 1) + 1,
            "date": utc_now_iso(),
            "reason": reason,
            "type": "append",
        })
        entry["improvement_log"] = log_list
        entry["last_improved"] = utc_now_iso()
        stats[skill_name] = entry
        write_text(
            self.skill_store.stats_path,
            json.dumps(stats, indent=2, ensure_ascii=False),
        )
        log.info(f"Skill '{skill_name}' improved (append): {reason}")

    def _log_pending(self, skill_name: str, content: str, reason: str) -> None:
        """Log improvement to pending_improvements.jsonl (shadow mode)."""
        pending_path = self.skill_store.skills_dir / "pending_improvements.jsonl"
        try:
            append_jsonl(pending_path, {
                "ts": utc_now_iso(),
                "skill_name": skill_name,
                "improvement_type": "append",
                "content": content,
                "reason": reason,
            })
            log.debug(f"Queued pending improvement for skill '{skill_name}': {reason}")
        except Exception as e:
            log.debug(f"Failed to log pending improvement: {e}")
