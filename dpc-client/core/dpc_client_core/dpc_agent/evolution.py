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
from datetime import datetime
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
        "memory/knowledge/",
        "config/agent.json",
    }

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
        self.agent_root = get_agent_root()
        self.enabled = enabled
        self.interval_minutes = interval_minutes
        self.auto_apply = auto_apply

        self._status = EvolutionStatus.IDLE
        self._current_cycle: Optional[EvolutionCycle] = None
        self._cycle_count = 0
        self._evolution_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # Pending changes awaiting approval
        self._pending_changes: List[ProposedChange] = []

        log.info(f"EvolutionManager initialized (enabled={enabled}, auto_apply={auto_apply})")

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
        await emitter.emit(EventType.EVOLUTION_CYCLE_STARTED, {
            "cycle_id": cycle.id,
            "cycle_number": self._cycle_count,
        })

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
                        })
                else:
                    # Queue for human approval
                    self._queue_for_approval(proposal)

            # Step 4: Update identity with learnings
            await self._update_evolution_memory(cycle)

            cycle.completed_at = utc_now_iso()
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

            await emitter.emit(EventType.EVOLUTION_CYCLE_COMPLETED, {
                "cycle_id": cycle.id,
                "files_modified": cycle.files_modified,
                "changes_applied": cycle.changes_applied,
                "description": cycle.description,
            })

        # Log cycle to file
        self._log_cycle(cycle)

        return cycle

    async def _analyze_state(self) -> Dict[str, Any]:
        """Analyze current agent state for improvement opportunities."""
        memory = self.agent.memory

        # Read recent tool usage
        tools_log = memory.read_jsonl_tail("tools.jsonl", 50)

        # Read recent consciousness thoughts
        thoughts_log = memory.read_jsonl_tail("consciousness.jsonl", 20)

        # Read current identity
        identity = memory.load_identity()
        scratchpad = memory.load_scratchpad()

        # Count knowledge topics
        knowledge_topics = memory.list_knowledge_topics()

        return {
            "files_examined": 4,
            "tool_calls_count": len(tools_log),
            "thoughts_count": len(thoughts_log),
            "identity_length": len(identity),
            "scratchpad_length": len(scratchpad),
            "knowledge_topics": len(knowledge_topics),
            "improvement_areas": [],  # Will be populated by LLM
        }

    async def _generate_proposals(self, analysis: Dict) -> List[Dict]:
        """Generate improvement proposals using LLM."""
        prompt = f"""Analyze the agent state and propose specific improvements.

Current state:
- Tool calls made recently: {analysis['tool_calls_count']}
- Background thoughts completed: {analysis['thoughts_count']}
- Identity document length: {analysis['identity_length']} chars
- Scratchpad length: {analysis['scratchpad_length']} chars
- Knowledge topics: {analysis['knowledge_topics']}

Sandbox constraints - you can ONLY modify these files under ~/.dpc/agent/:
- memory/identity.md (your self-understanding)
- memory/scratchpad.md (your working notes)
- memory/knowledge/*.md (your knowledge base)

You CANNOT access:
- DPC Messenger code
- ~/.dpc/personal.json (user's personal data)
- ~/.dpc/config.ini (user configuration)

Propose 1-3 specific, actionable improvements. Focus on:
1. Learning from recent mistakes or successes
2. Consolidating insights into knowledge
3. Updating your identity with new understanding

Respond with a JSON object in this exact format:
{{
  "proposals": [
    {{
      "path": "memory/identity.md",
      "change_type": "append",
      "content": "## Recent Learning\\n\\n...",
      "description": "Brief description of what this adds"
    }}
  ]
}}

If no improvements are needed, respond with: {{"proposals": []}}
"""

        try:
            # Call LLM without tools (just analysis)
            messages = [
                {"role": "system", "content": "You are analyzing yourself for improvement opportunities. Be concise and practical."},
                {"role": "user", "content": prompt},
            ]

            response, usage = await self.agent.llm.chat(messages, tools=None)
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

        Args:
            proposal: The change proposal

        Returns:
            Change ID
        """
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

            while not self._stop_event.is_set():
                try:
                    await self.run_evolution_cycle()

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
