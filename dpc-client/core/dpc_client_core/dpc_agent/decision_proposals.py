"""Decision proposals pipeline (ADR-010, MEM-4.2).

Structured extraction of decisions/knowledge from agent responses.
Writes to decision_proposals.jsonl (DRAFT status).
Reserved fields for ARCH-20 forward-compatibility.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from .utils import utc_now_iso

log = logging.getLogger(__name__)


@dataclass
class DecisionEntry:
    ts: str = ""
    topic: str = ""
    decision: str = ""
    rationale: str = ""
    participants: List[str] = field(default_factory=list)
    source_session: str = ""
    status: str = "active"
    supersedes: Optional[str] = None
    parent_id: Optional[str] = None


@dataclass
class DecisionProposal:
    entries: List[DecisionEntry] = field(default_factory=list)
    created_at: str = ""
    trigger_reasons: List[str] = field(default_factory=list)
    status: str = "DRAFT"


def create_proposal(
    entries: List[dict],
    trigger_reasons: List[str],
    session_id: str = "",
) -> DecisionProposal:
    """Create a decision proposal from extracted entries."""
    parsed = []
    for e in entries:
        known = {k for k in DecisionEntry.__dataclass_fields__}
        entry = DecisionEntry(**{k: v for k, v in e.items() if k in known})
        if not entry.ts:
            entry.ts = utc_now_iso()
        if session_id and not entry.source_session:
            entry.source_session = session_id
        parsed.append(entry)

    return DecisionProposal(
        entries=parsed,
        created_at=utc_now_iso(),
        trigger_reasons=trigger_reasons,
    )


def save_proposal(proposal: DecisionProposal, proposals_path: pathlib.Path) -> None:
    """Append proposal to decision_proposals.jsonl (DRAFT)."""
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    with open(proposals_path, "a", encoding="utf-8") as f:
        data = asdict(proposal)
        f.write(json.dumps(data, ensure_ascii=False) + "\n")
    log.info("Saved decision proposal with %d entries", len(proposal.entries))


def load_proposals(proposals_path: pathlib.Path) -> List[DecisionProposal]:
    """Load all DRAFT proposals from decision_proposals.jsonl."""
    if not proposals_path.exists():
        return []
    proposals = []
    for line in proposals_path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            entries = [
                DecisionEntry(**{k: v for k, v in e.items() if k in DecisionEntry.__dataclass_fields__})
                for e in data.get("entries", [])
            ]
            proposals.append(DecisionProposal(
                entries=entries,
                created_at=data.get("created_at", ""),
                trigger_reasons=data.get("trigger_reasons", []),
                status=data.get("status", "DRAFT"),
            ))
        except (json.JSONDecodeError, TypeError):
            continue
    return proposals


def approve_proposal(
    proposal: DecisionProposal,
    journal_path: pathlib.Path,
) -> int:
    """Move approved entries to decision_journal.jsonl (permanent)."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(journal_path, "a", encoding="utf-8") as f:
        for entry in proposal.entries:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            count += 1
    proposal.status = "APPROVED"
    log.info("Approved %d decision entries to journal", count)
    return count
