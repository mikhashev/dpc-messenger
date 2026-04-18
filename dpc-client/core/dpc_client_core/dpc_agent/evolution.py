"""
DPC Agent Evolution - Autonomous self-modification within sandbox.

Adapted from Ouroboros evolution mode with strict sandboxing.

SANDBOX BOUNDARIES:
- CAN modify: ~/.dpc/agent/ (memory, tools, config)
- CANNOT modify: DPC Messenger codebase
- CANNOT modify: ~/.dpc/personal.json, ~/.dpc/config.ini

The evolution manager runs periodic cycles where it:
1. Analyzes current state and recent performance
2. Identifies potential improvements
3. Proposes changes (requires approval if auto_apply=False)
4. Applies approved changes
5. Updates memory with learnings
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .events import EventType, get_event_emitter, emit_evolution_cycle, emit_code_modified
from .utils import (
    utc_now_iso, append_jsonl, get_agent_root, write_text, read_text
)

if TYPE_CHECKING:
    from .agent import DpcAgent
    from .memory import Memory

log = logging.getLogger(__name__)


class EvolutionStatus(Enum):
    """Status of evolution system."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"


class ChangeType(Enum):
    """Types of changes that can be made."""
    APPEND = "append"
    PREPEND = "prepend"
    REPLACE = "replace"


@dataclass
class EvolutionCycle:
    """Record of an evolution cycle."""
    id: str
    started_at: str
    completed_at: Optional[str] = None
    files_examined: int = 0
    files_modified: int = 0
    changes_proposed: int = 0
    changes_applied: int = 0
    description: str = ""
    rollback_available: bool = False


@dataclass
class ProposedChange:
    """A proposed change during evolution."""
    id: str
    path: str
    change_type: str  # 'append', 'prepend', 'replace'
    content: str
    description: str
    created_at: str = field(default_factory=utc_now_iso)
    approved: bool = False
    applied: bool = False


class EvolutionManager:
    """
    Manages autonomous self-modification within sandbox.

    The evolution system allows the agent to improve itself over time by:
    - Learning from mistakes and successes
    - Updating its identity and knowledge
    - Improving efficiency through self-analysis

    All modifications are strictly sandboxed to ~/.dpc/agent/
    """

    # Files that CAN be modified during evolution (relative to agent_root)
    ALLOWED_PATHS = {
        "memory/identity.md",
        "memory/scratchpad.md",
        "knowledge/",
        "config/agent.json",
        "skills/",  # Skill strategies (SKILL.md files under skills/*/SKILL.md)
    }

    # Skill appends are auto-approved even when auto_apply=False.
    # Only memory/identity changes require manual approval.
    SKILL_AUTO_APPROVE_PATH_PREFIX = "skills/"

    # Patterns that are NEVER allowed in paths
    FORBIDDEN_PATTERNS = [
        "personal.json",
        "config.ini",
        "privacy_rules.json",
        "providers.json",
        "../",  # No parent directory access
        "..\\",  # Windows parent directory
        "~",  # No home directory expansion
        "/etc/",
        "/var/",
        "C:\\",
    ]

    def __init__(
        self,
        agent: "DpcAgent",
        enabled: bool = False,
        interval_minutes: int = 60,
        auto_apply: bool = False,
    ):
        """
        Initialize evolution manager.

        Args:
            agent: The DpcAgent instance
            enabled: Whether evolution is enabled
            interval_minutes: Minutes between evolution cycles
            auto_apply: If True, auto-apply changes; if False, require approval
        """
        self.agent = agent
        self.agent_root = agent.agent_root  # Use agent's agent_root instead of calling get_agent_root()
        self.enabled = enabled
        self.interval_minutes = interval_minutes
        self.auto_apply = auto_apply

        self._status = EvolutionStatus.IDLE
        self._current_cycle: Optional[EvolutionCycle] = None
        self._cycle_count = 0
        self._evolution_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # Pending changes awaiting approval — load from disk to survive restarts
        self._pending_changes: List[ProposedChange] = self._load_pending_changes()

        log.info(
            "EvolutionManager initialized (enabled=%s, auto_apply=%s, pending=%d)",
            enabled, auto_apply, len(self._pending_changes),
        )

    def is_path_allowed(self, path: str) -> bool:
        """
        Check if a path is within allowed evolution scope.

        Args:
            path: Relative path to check

        Returns:
            True if path is allowed, False otherwise
        """
        # Normalize path
        path = path.replace("\\", "/").lstrip("/")

        # Check forbidden patterns
        for pattern in self.FORBIDDEN_PATTERNS:
            if pattern in path:
                log.warning(f"Path contains forbidden pattern: {pattern}")
                return False

        # Must be under agent root
        try:
            full_path = (self.agent_root / path).resolve()
            if not str(full_path).startswith(str(self.agent_root.resolve())):
                log.warning(f"Path escapes agent root: {path}")
                return False
        except Exception as e:
            log.warning(f"Path resolution failed: {e}")
            return False

        # Check allowed paths
        for allowed in self.ALLOWED_PATHS:
            if path.startswith(allowed):
                return True

        log.warning(f"Path not in allowed list: {path}")
        return False

    def _get_skill_existing_sections(self, skill_name: str) -> Dict[str, Any]:
        """
        Read existing SKILL.md for a skill and return section headers + size info.

        Used by _generate_proposals to prevent duplicate content.
        """
        skill_path = self.agent_root / "skills" / skill_name / "SKILL.md"
        result: Dict[str, Any] = {"sections": [], "size": 0, "over_limit": False}
        try:
            if not skill_path.exists():
                return result
            content = skill_path.read_text(encoding="utf-8")
            result["size"] = len(content)
            result["over_limit"] = len(content) > self.MAX_SKILL_FILE_SIZE
            # Extract markdown ## headers as section names
            result["sections"] = [
                line.lstrip("#").strip()
                for line in content.splitlines()
                if line.startswith("## ")
            ]
        except Exception as e:
            log.debug(f"Failed to read skill sections for {skill_name}: {e}")
        return result

    async def run_evolution_cycle(self) -> EvolutionCycle:
        """
        Run one evolution cycle.

        Process:
        1. Analyze current state and recent performance
        2. Identify potential improvements
        3. Propose changes
        4. Apply changes (if auto_apply) or queue for approval
        """
        self._status = EvolutionStatus.RUNNING
        self._cycle_count += 1

        cycle = EvolutionCycle(
            id=f"evo-{uuid.uuid4().hex[:8]}",
            started_at=utc_now_iso(),
        )
        self._current_cycle = cycle

        emitter = get_event_emitter()
        _agent_id = self.agent_root.name
        await emitter.emit(EventType.EVOLUTION_CYCLE_STARTED, {
            "cycle_id": cycle.id,
            "cycle_number": self._cycle_count,
            "agent_id": _agent_id,
        })

        proposals: List[Dict[str, Any]] = []
        try:
            # Step 1: Analyze state
            log.info(f"Starting evolution cycle {self._cycle_count}")
            analysis = await self._analyze_state()
            cycle.files_examined = analysis.get("files_examined", 0)

            # Step 2: Generate improvement proposals
            proposals = await self._generate_proposals(analysis)
            cycle.changes_proposed = len(proposals)

            # Step 3: Validate and apply proposals
            for proposal in proposals:
                if not self.is_path_allowed(proposal["path"]):
                    log.warning(f"Skipping proposal with forbidden path: {proposal['path']}")
                    continue

                if self.auto_apply:
                    success = await self._apply_change(proposal)
                    if success:
                        cycle.changes_applied += 1
                        cycle.files_modified += 1

                        await emitter.emit(EventType.CODE_MODIFIED, {
                            "path": proposal["path"],
                            "description": proposal["description"][:200],
                            "agent_id": _agent_id,
                        })
                else:
                    # Queue for human approval (memory/identity changes)
                    self._queue_for_approval(proposal)

            # Step 4: Update identity with learnings
            cycle.completed_at = utc_now_iso()
            await self._update_evolution_memory(cycle)
            cycle.rollback_available = True
            cycle.description = f"Proposed {cycle.changes_proposed} changes, applied {cycle.changes_applied}"

            log.info(f"Evolution cycle {self._cycle_count} complete: {cycle.description}")

        except Exception as e:
            log.error(f"Evolution cycle failed: {e}", exc_info=True)
            cycle.description = f"Failed: {e}"
            cycle.completed_at = utc_now_iso()

        finally:
            self._status = EvolutionStatus.IDLE
            self._current_cycle = None

            # Brief summary of each proposal for downstream notifiers (Telegram bridge etc.)
            proposals_summary = [
                {
                    "path": str(p.get("path", "?")),
                    "change_type": str(p.get("change_type", "")),
                    "description": str(p.get("description", ""))[:200],
                }
                for p in proposals
            ]

            await emitter.emit(EventType.EVOLUTION_CYCLE_COMPLETED, {
                "cycle_id": cycle.id,
                "files_modified": cycle.files_modified,
                "changes_proposed": cycle.changes_proposed,
                "changes_applied": cycle.changes_applied,
                "proposals_summary": proposals_summary,
                "description": cycle.description,
                "agent_id": _agent_id,
            })

        # Log cycle to file
        self._log_cycle(cycle)

        return cycle

    async def _analyze_state(self) -> Dict[str, Any]:
        """Analyze current agent state for improvement opportunities."""
        memory = self.agent.memory

        # Read recent tool usage (temporal window: last 24h instead of fixed tail)
        tools_log = memory.read_jsonl_since("tools.jsonl", hours=24.0, max_entries=500)
        if not tools_log:
            # Fallback to tail if no recent entries (e.g., first run after upgrade)
            tools_log = memory.read_jsonl_tail("tools.jsonl", 50)

        # Read recent consciousness thoughts (temporal window: last 24h)
        thoughts_log = memory.read_jsonl_since("consciousness.jsonl", hours=24.0, max_entries=100)
        if not thoughts_log:
            thoughts_log = memory.read_jsonl_tail("consciousness.jsonl", 20)

        # Read current identity
        identity = memory.load_identity()
        scratchpad = memory.load_scratchpad()

        # Count knowledge topics
        knowledge_topics = memory.list_knowledge_topics()

        # Read skill performance data (_stats.json) — primary signal for improvement
        skill_stats: Dict[str, Any] = {}
        underperforming_skills: List[Dict[str, Any]] = []
        try:
            skill_store = getattr(self.agent, "skill_store", None)
            if skill_store is not None:
                skill_stats = skill_store.get_stats()
                available_skills = skill_store.list_skill_names()
                for skill_name in available_skills:
                    stats = skill_stats.get(skill_name, {})
                    total = stats.get("success_count", 0) + stats.get("failure_count", 0)
                    if total < 3:
                        continue  # Not enough data yet
                    failure_rate = stats.get("failure_count", 0) / total
                    avg_rounds = stats.get("avg_rounds", 0)
                    # Flag as underperforming if >30% failure rate or avg rounds > 10
                    if failure_rate > 0.30 or avg_rounds > 10:
                        underperforming_skills.append({
                            "name": skill_name,
                            "failure_rate": round(failure_rate, 2),
                            "avg_rounds": avg_rounds,
                            "total_uses": total,
                            "improvement_count": len(stats.get("improvement_log", [])),
                        })
        except Exception as e:
            log.debug(f"Failed to read skill stats: {e}")

        # Analyze tool usage quality from tools.jsonl (error rate + diversity + categories + duration)
        tool_error_count = 0
        tool_frequency: Dict[str, int] = {}
        tool_durations: Dict[str, list] = {}
        error_categories: Dict[str, int] = {}
        for entry in tools_log:
            tool_name = entry.get("tool", "unknown")
            tool_frequency[tool_name] = tool_frequency.get(tool_name, 0) + 1
            dur = entry.get("duration_ms")
            if dur is not None:
                tool_durations.setdefault(tool_name, []).append(dur)
            is_err = entry.get("is_error", False)
            if not is_err:
                result = str(entry.get("result_preview", ""))
                is_err = result.startswith("⚠️") or "error" in result.lower()[:50]
            if is_err:
                tool_error_count += 1
                cat = entry.get("error_category", "unknown")
                error_categories[cat] = error_categories.get(cat, 0) + 1

        tool_error_rate = round(tool_error_count / len(tools_log), 2) if tools_log else 0.0
        # Top 5 most used tools
        top_tools = sorted(tool_frequency.items(), key=lambda x: -x[1])[:5]
        # Slow tools (avg duration > 1000ms, at least 3 calls)
        slow_tools = []
        for name, durations in tool_durations.items():
            if len(durations) >= 3:
                avg = round(sum(durations) / len(durations))
                if avg > 1000:
                    slow_tools.append({"tool": name, "avg_ms": avg, "calls": len(durations)})
        slow_tools.sort(key=lambda x: -x["avg_ms"])

        # Read cross-session trends from digest.jsonl
        digest_trends: List[Dict[str, Any]] = []
        try:
            from pathlib import Path
            digest_path = Path.home() / ".dpc" / "conversations" / self.agent_root.name / "digest.jsonl"
            if digest_path.exists():
                with open(digest_path, encoding="utf-8") as df:
                    for line in df:
                        line = line.strip()
                        if line:
                            try:
                                digest_trends.append(json.loads(line))
                            except Exception:
                                continue
        except Exception:
            log.debug("Failed to read digest.jsonl for evolution trends", exc_info=True)

        # Summarize recent consciousness thoughts for Evolution prompt
        # Supports both structured (v2) and freeform (v1) formats
        recent_thoughts: List[str] = []
        for entry in thoughts_log[-5:]:
            thought_type = entry.get("type", "unknown")
            # Structured format (v2): has "observation" field
            if "observation" in entry:
                obs = str(entry.get("observation", ""))[:200]
                pattern = entry.get("pattern_detected")
                severity = entry.get("severity", "low")
                action = entry.get("action_suggestion")
                parts = [f"[{thought_type}] {obs}"]
                if pattern:
                    parts.append(f"pattern={pattern}")
                if severity in ("medium", "high"):
                    parts.append(f"severity={severity}")
                if action:
                    parts.append(f"action={action[:100]}")
                recent_thoughts.append(" | ".join(parts))
            else:
                # Freeform format (v1): has "response_preview" field
                preview = str(entry.get("response_preview", ""))[:200]
                if preview.strip():
                    recent_thoughts.append(f"[{thought_type}] {preview}")

        return {
            "files_examined": 5,
            "tool_calls_count": len(tools_log),
            "tool_error_rate": tool_error_rate,
            "tool_error_count": tool_error_count,
            "error_categories": error_categories,
            "top_tools": top_tools,
            "slow_tools": slow_tools[:5],
            "thoughts_count": len(thoughts_log),
            "identity_length": len(identity),
            "scratchpad_length": len(scratchpad),
            "knowledge_topics": len(knowledge_topics),
            "underperforming_skills": underperforming_skills,
            "total_skill_stats": len(skill_stats),
            "improvement_areas": [],  # Will be populated by LLM
            "recent_thoughts": recent_thoughts,
            "digest_sessions": len(digest_trends),
            "digest_latest": digest_trends[-3:] if digest_trends else [],
        }

    # Maximum size for skill files — beyond this, append proposals are skipped
    MAX_SKILL_FILE_SIZE = 10_000  # characters

    async def _generate_proposals(self, analysis: Dict) -> List[Dict]:
        """Generate improvement proposals using LLM."""
        underperforming = analysis.get("underperforming_skills", [])

        # Build the skill-focused section of the prompt, including existing content
        # to prevent duplicate proposals
        if underperforming:
            skill_section = "## Underperforming Skills (primary improvement target)\n"
            for s in underperforming[:3]:
                skill_section += (
                    f"\n- **{s['name']}**: failure_rate={s['failure_rate']:.0%}, "
                    f"avg_rounds={s['avg_rounds']:.1f}, uses={s['total_uses']}, "
                    f"prior_improvements={s['improvement_count']}"
                )

                # Read existing SKILL.md to prevent duplicate proposals
                existing_info = self._get_skill_existing_sections(s['name'])
                if existing_info["over_limit"]:
                    skill_section += (
                        f"\n  ⚠ SKILL.md is {existing_info['size']} chars "
                        f"(limit {self.MAX_SKILL_FILE_SIZE}). "
                        f"DO NOT propose appends for this skill — file is too large."
                    )
                elif existing_info["sections"]:
                    skill_section += (
                        f"\n  Existing sections in SKILL.md: "
                        f"{', '.join(existing_info['sections'])}"
                        f"\n  DO NOT propose content that duplicates these sections."
                    )

            skill_section += (
                "\n\nFor each underperforming skill (that is NOT over the size limit), "
                "propose an APPEND to skills/{skill-name}/SKILL.md that adds a "
                "GENUINELY NEW step or common-failure entry not already covered. "
                "Path format: skills/{name}/SKILL.md"
            )
        else:
            skill_section = (
                "No skills are underperforming right now. "
                "Consider memory/identity.md or knowledge/ improvements only."
            )

        # Build consciousness insights section
        recent_thoughts = analysis.get("recent_thoughts", [])
        if recent_thoughts:
            thoughts_section = (
                "## Recent Agent Self-Reflections (from Consciousness)\n"
                "Use these insights to inform your proposals — "
                "the agent has already been thinking about these topics:\n\n"
                + "\n".join(f"- {t}" for t in recent_thoughts)
            )
        else:
            thoughts_section = ""

        # Build tool usage quality section
        tool_error_rate = analysis.get("tool_error_rate", 0.0)
        top_tools = analysis.get("top_tools", [])
        error_cats = analysis.get("error_categories", {})
        if tool_error_rate > 0.1 or top_tools:
            tool_quality_section = "## Tool Usage Quality\n"
            tool_quality_section += f"- Error rate: {tool_error_rate:.0%} ({analysis.get('tool_error_count', 0)} errors in {analysis['tool_calls_count']} calls)\n"
            if top_tools:
                tool_quality_section += "- Most used: " + ", ".join(f"{name}({count})" for name, count in top_tools) + "\n"
            if error_cats:
                tool_quality_section += "- Error breakdown: " + ", ".join(f"{cat}={cnt}" for cat, cnt in sorted(error_cats.items(), key=lambda x: -x[1])) + "\n"
                if error_cats.get("firewall_blocked", 0) > 0:
                    tool_quality_section += "  - firewall_blocked: tool is disabled by user — stop calling it\n"
            if tool_error_rate > 0.15:
                tool_quality_section += "- ⚠️ HIGH error rate — consider if search patterns or file paths need improvement\n"
        else:
            tool_quality_section = ""

        prompt = f"""You are an evolution cycle for an AI agent. Propose targeted improvements based on performance data and the agent's own self-reflections.

## Performance Data
- Recent tool calls: {analysis['tool_calls_count']}
- Knowledge topics: {analysis['knowledge_topics']}
- Skills tracked: {analysis['total_skill_stats']}

{tool_quality_section}

{thoughts_section}

{skill_section}

## Sandbox — ONLY these paths are allowed:
- skills/{{name}}/SKILL.md  (append only — fix underperforming skills)
- memory/identity.md        (append only — add new self-understanding)
- knowledge/{{topic}}.md  (append — add new knowledge)

## Rules
1. Prefer skill improvements when underperforming skills exist — they have measurable impact
2. Skill changes MUST be "append" only — never replace full SKILL.md
3. Content for skill appends should go under a new markdown section heading
4. Only propose changes that address a specific observed gap, not hypothetical improvements
5. Propose at most 2 changes total

Respond with JSON only:
{{
  "proposals": [
    {{
      "path": "skills/code-analysis/SKILL.md",
      "change_type": "append",
      "content": "## Additional Tips\\n\\n- Always check X before Y...",
      "description": "Add missing step for handling monorepos"
    }}
  ]
}}

If no improvements are warranted: {{"proposals": []}}
"""

        try:
            # Call LLM without tools (just analysis)
            messages = [
                {"role": "system", "content": "You are analyzing yourself for improvement opportunities. Be concise and practical."},
                {"role": "user", "content": prompt},
            ]

            response, usage = await self.agent.llm.chat(messages, tools=None, background=True)
            content = response.get("content", "")

            # Parse JSON from response
            proposals = self._parse_proposals(content)

            log.info(f"Generated {len(proposals)} improvement proposals")
            return proposals

        except Exception as e:
            log.error(f"Failed to generate proposals: {e}")
            return []

    def _parse_proposals(self, content: str) -> List[Dict]:
        """Parse proposals from LLM response."""
        # Try to extract JSON from response
        json_match = re.search(r'\{[\s\S]*"proposals"[\s\S]*\}', content)
        if not json_match:
            log.debug(f"No JSON found in response: {content[:200]}...")
            return []

        try:
            data = json.loads(json_match.group())
            proposals = data.get("proposals", [])

            # Validate each proposal
            valid_proposals = []
            for p in proposals:
                if not isinstance(p, dict):
                    continue
                if "path" not in p or "content" not in p:
                    continue
                if p.get("change_type") not in ("append", "prepend", "replace"):
                    p["change_type"] = "append"
                p["description"] = p.get("description", "No description")
                valid_proposals.append(p)

            return valid_proposals

        except json.JSONDecodeError as e:
            log.warning(f"Failed to parse proposals JSON: {e}")
            return []

    async def _apply_change(self, proposal: Dict) -> bool:
        """
        Apply a proposed change.

        Args:
            proposal: The change proposal

        Returns:
            True if successful, False otherwise
        """
        path = proposal["path"]
        change_type = proposal["change_type"]
        content = proposal["content"]
        description = proposal.get("description", "")

        # Final safety check
        if not self.is_path_allowed(path):
            log.error(f"Blocked attempt to modify forbidden path: {path}")
            return False

        full_path = self.agent_root / path

        try:
            # Backup current state
            if full_path.exists():
                backup_path = full_path.with_suffix(full_path.suffix + ".bak")
                backup_path.write_text(full_path.read_text(encoding="utf-8"), encoding="utf-8")

            # Ensure parent directory exists
            full_path.parent.mkdir(parents=True, exist_ok=True)

            # Apply change based on type
            if change_type == "append":
                current = full_path.read_text(encoding="utf-8") if full_path.exists() else ""
                if len(current) > self.MAX_SKILL_FILE_SIZE and path.startswith("skills/"):
                    log.warning(
                        "Blocked append to %s — file size %d exceeds limit %d",
                        path, len(current), self.MAX_SKILL_FILE_SIZE,
                    )
                    return False
                new_content = current + "\n\n" + content
                full_path.write_text(new_content, encoding="utf-8")

            elif change_type == "prepend":
                current = full_path.read_text(encoding="utf-8") if full_path.exists() else ""
                new_content = content + "\n\n" + current
                full_path.write_text(new_content, encoding="utf-8")

            elif change_type == "replace":
                full_path.write_text(content, encoding="utf-8")

            else:
                log.error(f"Unknown change type: {change_type}")
                return False

            log.info(f"Applied change to {path}: {description}")
            return True

        except Exception as e:
            log.error(f"Failed to apply change to {path}: {e}")
            return False

    def _queue_for_approval(self, proposal: Dict) -> str:
        """
        Queue a change for human approval.

        Skips if a pending change already targets the same path (dedup).

        Args:
            proposal: The change proposal

        Returns:
            Change ID, or empty string if skipped as duplicate
        """
        # Dedup: skip if we already have a pending change for the same path
        for existing in self._pending_changes:
            if existing.path == proposal["path"]:
                log.info(
                    "Skipping duplicate proposal for %s — pending change %s already exists",
                    proposal["path"], existing.id,
                )
                return ""

        change_id = f"change-{uuid.uuid4().hex[:8]}"

        pending_change = ProposedChange(
            id=change_id,
            path=proposal["path"],
            change_type=proposal["change_type"],
            content=proposal["content"],
            description=proposal.get("description", ""),
        )

        self._pending_changes.append(pending_change)

        # Persist to disk
        self._save_pending_changes()

        log.info(f"Queued change {change_id} for approval: {pending_change.description}")
        return change_id

    def _load_pending_changes(self) -> List[ProposedChange]:
        """Load pending changes from disk (survives restarts)."""
        pending_file = self.agent_root / "state" / "pending_changes.json"
        if not pending_file.exists():
            return []
        try:
            data = json.loads(pending_file.read_text(encoding="utf-8"))
            changes = []
            for c in data.get("changes", []):
                changes.append(ProposedChange(
                    id=c["id"],
                    path=c["path"],
                    change_type=c["change_type"],
                    content=c["content"],
                    description=c["description"],
                    created_at=c.get("created_at", ""),
                ))
            if changes:
                log.info("Loaded %d pending evolution changes from disk", len(changes))
            return changes
        except Exception as e:
            log.warning("Failed to load pending changes: %s", e)
            return []

    def _save_pending_changes(self) -> None:
        """Save pending changes to disk."""
        pending_file = self.agent_root / "state" / "pending_changes.json"

        try:
            data = {
                "changes": [
                    {
                        "id": c.id,
                        "path": c.path,
                        "change_type": c.change_type,
                        "content": c.content,
                        "description": c.description,
                        "created_at": c.created_at,
                    }
                    for c in self._pending_changes
                ],
                "updated_at": utc_now_iso(),
            }
            pending_file.parent.mkdir(parents=True, exist_ok=True)
            pending_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            log.error(f"Failed to save pending changes: {e}")

    def get_pending_changes(self) -> List[Dict[str, Any]]:
        """Get list of pending changes awaiting approval."""
        return [
            {
                "id": c.id,
                "path": c.path,
                "change_type": c.change_type,
                "description": c.description,
                "created_at": c.created_at,
                "content_preview": c.content[:200] + "..." if len(c.content) > 200 else c.content,
            }
            for c in self._pending_changes
        ]

    async def approve_change(self, change_id: str) -> bool:
        """
        Approve and apply a pending change.

        Args:
            change_id: ID of the change to approve

        Returns:
            True if successful, False otherwise
        """
        for i, change in enumerate(self._pending_changes):
            if change.id == change_id:
                success = await self._apply_change({
                    "path": change.path,
                    "change_type": change.change_type,
                    "content": change.content,
                    "description": change.description,
                })

                if success:
                    self._pending_changes.pop(i)
                    self._save_pending_changes()
                    log.info(f"Approved and applied change {change_id}")

                    # Emit event
                    emitter = get_event_emitter()
                    await emitter.emit(EventType.CODE_MODIFIED, {
                        "path": change.path,
                        "description": change.description,
                        "approved": True,
                        "agent_id": self.agent_root.name,
                    })

                return success

        log.warning(f"Change not found: {change_id}")
        return False

    def reject_change(self, change_id: str) -> bool:
        """
        Reject and remove a pending change.

        Args:
            change_id: ID of the change to reject

        Returns:
            True if found and removed, False otherwise
        """
        for i, change in enumerate(self._pending_changes):
            if change.id == change_id:
                self._pending_changes.pop(i)
                self._save_pending_changes()
                log.info(f"Rejected change {change_id}")
                return True

        return False

    async def _update_evolution_memory(self, cycle: EvolutionCycle) -> None:
        """Update memory with evolution learnings."""
        summary = f"""

## Evolution Cycle {self._cycle_count}

**Date:** {cycle.completed_at[:10] if cycle.completed_at else "N/A"}

- Files examined: {cycle.files_examined}
- Changes proposed: {cycle.changes_proposed}
- Changes applied: {cycle.changes_applied}

**Summary:** {cycle.description}

"""
        # Append to scratchpad
        try:
            scratchpad = self.agent.memory.load_scratchpad()
            self.agent.memory.save_scratchpad(scratchpad + summary)
        except Exception as e:
            log.error(f"Failed to update evolution memory: {e}")

    def _log_cycle(self, cycle: EvolutionCycle) -> None:
        """Log cycle to evolution log file."""
        try:
            log_file = self.agent_root / "logs" / "evolution.jsonl"
            append_jsonl(log_file, {
                "cycle_id": cycle.id,
                "started_at": cycle.started_at,
                "completed_at": cycle.completed_at,
                "stats": {
                    "files_examined": cycle.files_examined,
                    "files_modified": cycle.files_modified,
                    "changes_proposed": cycle.changes_proposed,
                    "changes_applied": cycle.changes_applied,
                },
                "description": cycle.description,
            })
        except Exception as e:
            log.error(f"Failed to log evolution cycle: {e}")

    def start_automatic_evolution(self) -> None:
        """Start automatic evolution cycles at configured interval."""
        if not self.enabled:
            log.warning("Evolution not enabled, not starting automatic cycles")
            return

        if self._evolution_task is not None:
            log.warning("Automatic evolution already running")
            return

        self._stop_event.clear()

        async def _evolution_loop():
            log.info(f"Starting automatic evolution (interval={self.interval_minutes}min)")

            # Skip first cycle if last one was recent (prevents evolution-on-every-restart)
            try:
                log_file = self.agent_root / "logs" / "evolution.jsonl"
                if log_file.exists():
                    # Read only the last line efficiently
                    with open(log_file, "rb") as f:
                        f.seek(0, 2)  # Seek to end
                        size = f.tell()
                        # Read last 4KB (more than enough for one JSONL entry)
                        f.seek(max(0, size - 4096))
                        last_line = f.read().decode("utf-8").strip().rsplit("\n", 1)[-1]
                    last_cycle = json.loads(last_line)
                    completed = datetime.fromisoformat(last_cycle["completed_at"])
                    if completed.tzinfo is None:
                        completed = completed.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - completed).total_seconds()
                    remaining = self.interval_minutes * 60 - elapsed
                    if remaining > 0:
                        log.info(f"Last evolution cycle was {elapsed:.0f}s ago, waiting {remaining:.0f}s before first cycle")
                        try:
                            await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
                            log.info("Automatic evolution stopped during initial wait")
                            self._evolution_task = None
                            return
                        except asyncio.TimeoutError:
                            pass  # Time to run
            except Exception as e:
                log.debug(f"Could not read last evolution timestamp, running immediately: {e}")

            while not self._stop_event.is_set():
                try:
                    # Yield to user interaction — don't compete for LLM provider
                    if getattr(self.agent, '_user_active', False):
                        log.debug("Skipping evolution cycle — user interaction active")
                        await asyncio.sleep(10)  # Check again shortly
                        continue

                    await self.run_evolution_cycle()

                    # Memory consolidation Tier 1 (ADR-010, WIRE-5)
                    try:
                        from .consolidation import tier1_consolidate
                        knowledge_dir = self.agent_root / "knowledge"
                        if knowledge_dir.is_dir():
                            tier1_consolidate(knowledge_dir)
                    except Exception as e:
                        log.debug("Memory consolidation skipped: %s", e)

                    # Wait for next cycle
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=self.interval_minutes * 60
                        )
                        break  # Stop event was set
                    except asyncio.TimeoutError:
                        pass  # Continue to next cycle

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.error(f"Evolution loop error: {e}", exc_info=True)
                    await asyncio.sleep(60)  # Brief pause before retry

            log.info("Automatic evolution stopped")
            self._evolution_task = None

        self._evolution_task = asyncio.create_task(_evolution_loop())
        log.info("Automatic evolution started")

    def stop_automatic_evolution(self) -> None:
        """Stop automatic evolution cycles."""
        self._stop_event.set()

        if self._evolution_task:
            self._evolution_task.cancel()
            self._evolution_task = None

        log.info("Automatic evolution stopped")

    def pause(self) -> None:
        """Pause evolution (for use during manual review)."""
        self._status = EvolutionStatus.PAUSED
        log.info("Evolution paused")

    def resume(self) -> None:
        """Resume evolution from paused state."""
        if self._status == EvolutionStatus.PAUSED:
            self._status = EvolutionStatus.IDLE
            log.info("Evolution resumed")

    def is_running(self) -> bool:
        """Check if evolution is currently running."""
        return self._status == EvolutionStatus.RUNNING

    def get_status(self) -> Dict[str, Any]:
        """Get evolution status."""
        return {
            "status": self._status.value,
            "enabled": self.enabled,
            "auto_apply": self.auto_apply,
            "interval_minutes": self.interval_minutes,
            "cycle_count": self._cycle_count,
            "current_cycle": self._current_cycle.id if self._current_cycle else None,
            "pending_changes": len(self._pending_changes),
        }
