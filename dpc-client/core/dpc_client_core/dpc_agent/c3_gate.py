"""C3 Gate — Session Close extension for decision review (ADR-010, MEM-4.3).

Human approval gate for episodic memory extraction. Proposals loaded
at session close, user reviews via KnowledgeCommitDialog-like UI.
Approved entries graduate to permanent journal.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import List, Optional

from .decision_proposals import (
    DecisionProposal, DecisionEntry, load_proposals,
    approve_proposal, save_proposal,
)

log = logging.getLogger(__name__)


def get_pending_proposals(agent_root: pathlib.Path) -> List[DecisionProposal]:
    """Load all DRAFT proposals for session close review."""
    proposals_path = agent_root / "state" / "decision_proposals.jsonl"
    proposals = load_proposals(proposals_path)
    return [p for p in proposals if p.status == "DRAFT"]


def review_proposal(
    agent_root: pathlib.Path,
    proposal_index: int,
    approved_entries: List[int],
    rejected_entries: List[int],
) -> dict:
    """Process user's review decision on a proposal."""
    proposals_path = agent_root / "state" / "decision_proposals.jsonl"
    journal_path = agent_root / "state" / "decision_journal.jsonl"
    rejection_log = agent_root / "state" / "rejected_proposals.jsonl"

    proposals = load_proposals(proposals_path)
    if proposal_index >= len(proposals):
        return {"error": "Invalid proposal index"}

    proposal = proposals[proposal_index]

    approved_count = 0
    rejected_count = 0

    if approved_entries:
        approved_proposal = DecisionProposal(
            entries=[proposal.entries[i] for i in approved_entries if i < len(proposal.entries)],
            created_at=proposal.created_at,
            trigger_reasons=proposal.trigger_reasons,
        )
        approved_count = approve_proposal(approved_proposal, journal_path)

    if rejected_entries:
        for i in rejected_entries:
            if i < len(proposal.entries):
                rejected_count += 1
                _log_rejection(rejection_log, proposal.entries[i], proposal.trigger_reasons)

    proposal.status = "REVIEWED"
    _rewrite_proposals(proposals_path, proposals)

    return {
        "approved": approved_count,
        "rejected": rejected_count,
        "status": "REVIEWED",
    }


def session_close_summary(agent_root: pathlib.Path) -> Optional[dict]:
    """Generate summary for session close UI. Returns None if no pending proposals."""
    pending = get_pending_proposals(agent_root)
    if not pending:
        return None

    total_entries = sum(len(p.entries) for p in pending)
    return {
        "pending_proposals": len(pending),
        "total_entries": total_entries,
        "proposals": [
            {
                "index": i,
                "entry_count": len(p.entries),
                "trigger_reasons": p.trigger_reasons,
                "topics": [e.topic for e in p.entries if e.topic],
            }
            for i, p in enumerate(pending)
        ],
    }


def _log_rejection(path: pathlib.Path, entry: DecisionEntry, reasons: List[str]) -> None:
    from dataclasses import asdict
    from .utils import utc_now_iso
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"entry": asdict(entry), "reasons": reasons, "rejected_at": utc_now_iso()}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _rewrite_proposals(path: pathlib.Path, proposals: List[DecisionProposal]) -> None:
    from dataclasses import asdict
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for p in proposals:
            f.write(json.dumps(asdict(p), ensure_ascii=False) + "\n")
