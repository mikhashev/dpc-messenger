"""Tests for C3 Gate session close (ADR-010, MEM-4.3)."""

from dpc_client_core.dpc_agent.c3_gate import (
    get_pending_proposals, review_proposal, session_close_summary,
)
from dpc_client_core.dpc_agent.decision_proposals import create_proposal, save_proposal


def _setup_proposal(agent_root):
    proposals_path = agent_root / "state" / "decision_proposals.jsonl"
    p = create_proposal(
        [{"topic": "hybrid search", "decision": "Use RRF k=60"}],
        ["decision_verb"],
        session_id="s50",
    )
    save_proposal(p, proposals_path)
    return proposals_path


def test_get_pending(tmp_path):
    _setup_proposal(tmp_path)
    pending = get_pending_proposals(tmp_path)
    assert len(pending) == 1
    assert pending[0].status == "DRAFT"


def test_no_pending(tmp_path):
    assert get_pending_proposals(tmp_path) == []


def test_review_approve(tmp_path):
    _setup_proposal(tmp_path)
    result = review_proposal(tmp_path, 0, approved_entries=[0], rejected_entries=[])
    assert result["approved"] == 1
    assert result["status"] == "REVIEWED"
    assert (tmp_path / "state" / "decision_journal.jsonl").exists()


def test_review_reject(tmp_path):
    _setup_proposal(tmp_path)
    result = review_proposal(tmp_path, 0, approved_entries=[], rejected_entries=[0])
    assert result["rejected"] == 1
    assert (tmp_path / "state" / "rejected_proposals.jsonl").exists()


def test_session_close_summary(tmp_path):
    _setup_proposal(tmp_path)
    summary = session_close_summary(tmp_path)
    assert summary is not None
    assert summary["pending_proposals"] == 1
    assert summary["total_entries"] == 1


def test_session_close_no_proposals(tmp_path):
    assert session_close_summary(tmp_path) is None
