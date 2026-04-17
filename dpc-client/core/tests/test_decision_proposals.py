"""Tests for decision proposals pipeline (ADR-010, MEM-4.2)."""

from dpc_client_core.dpc_agent.decision_proposals import (
    create_proposal, save_proposal, load_proposals, approve_proposal,
    DecisionEntry, DecisionProposal,
)


def test_create_proposal():
    entries = [{"topic": "RRF search", "decision": "Use k=60"}]
    p = create_proposal(entries, ["decision_verb"], session_id="s50")
    assert len(p.entries) == 1
    assert p.entries[0].topic == "RRF search"
    assert p.entries[0].source_session == "s50"
    assert p.status == "DRAFT"


def test_save_and_load(tmp_path):
    path = tmp_path / "proposals.jsonl"
    p = create_proposal(
        [{"topic": "FAISS", "decision": "IndexFlatIP"}],
        ["tool_call"],
    )
    save_proposal(p, path)

    loaded = load_proposals(path)
    assert len(loaded) == 1
    assert loaded[0].entries[0].topic == "FAISS"


def test_approve_moves_to_journal(tmp_path):
    journal = tmp_path / "journal.jsonl"
    p = create_proposal(
        [{"topic": "BM25", "decision": "Use bigram for CJK"}],
        ["knowledge_claim"],
    )
    count = approve_proposal(p, journal)
    assert count == 1
    assert p.status == "APPROVED"
    assert journal.exists()


def test_forward_compatible_fields():
    entry = DecisionEntry(status="active", supersedes=None, parent_id=None)
    assert entry.status == "active"
    assert entry.supersedes is None


def test_load_empty(tmp_path):
    assert load_proposals(tmp_path / "nonexistent.jsonl") == []
