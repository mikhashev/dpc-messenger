"""Tests for P2 Evolution Verification (2A outcome tracking + 2B rolling metrics)."""

import json
import pathlib
import pytest
from unittest.mock import MagicMock

from dpc_client_core.dpc_agent.evolution import EvolutionManager


@pytest.fixture
def evo_manager(tmp_path):
    agent = MagicMock()
    agent.agent_root = tmp_path
    agent.memory = MagicMock()
    agent.memory.read_jsonl_since.return_value = [
        {"tool": "read_file", "is_error": False, "result_preview": "ok"},
        {"tool": "write_file", "is_error": True, "result_preview": "⚠️ error"},
        {"tool": "search", "is_error": False, "result_preview": "found"},
    ]
    agent.memory.read_jsonl_tail.return_value = []
    agent.skill_store = None
    (tmp_path / "state").mkdir()
    (tmp_path / "logs").mkdir()

    em = EvolutionManager.__new__(EvolutionManager)
    em.agent = agent
    em.agent_root = tmp_path
    em.auto_apply = True
    em._pending_changes = []
    return em


def test_snapshot_baseline(evo_manager):
    proposal = {"description": "test fix", "path": "skills/test/SKILL.md", "change_type": "append"}
    evo_manager._snapshot_baseline(proposal)

    outcomes_path = evo_manager.agent_root / "state" / "proposal_outcomes.jsonl"
    assert outcomes_path.exists()
    entry = json.loads(outcomes_path.read_text().strip())
    assert entry["proposal_id"] == "test fix"
    assert entry["baseline"]["tool_error_rate"] > 0
    assert entry["outcome"] is None


def test_evaluate_outcomes(evo_manager):
    outcomes_path = evo_manager.agent_root / "state" / "proposal_outcomes.jsonl"
    entry = {
        "proposal_id": "fix1",
        "path": "skills/x/SKILL.md",
        "applied_at": "2026-04-20T10:00:00Z",
        "baseline": {"tool_error_rate": 0.5, "tool_calls": 10},
        "outcome": None,
    }
    outcomes_path.write_text(json.dumps(entry) + "\n")

    results = evo_manager._evaluate_outcomes()
    assert len(results) == 1
    assert results[0]["outcome"]["verdict"] in ("improved", "neutral", "regressed")
    assert results[0]["outcome"]["current_error_rate"] is not None

    reread = json.loads(outcomes_path.read_text().strip())
    assert reread["outcome"] is not None


def test_evaluate_outcomes_already_evaluated(evo_manager):
    outcomes_path = evo_manager.agent_root / "state" / "proposal_outcomes.jsonl"
    entry = {
        "proposal_id": "fix1",
        "path": "skills/x/SKILL.md",
        "applied_at": "2026-04-20T10:00:00Z",
        "baseline": {"tool_error_rate": 0.5, "tool_calls": 10},
        "outcome": {"verdict": "improved", "delta": -0.1},
    }
    outcomes_path.write_text(json.dumps(entry) + "\n")

    results = evo_manager._evaluate_outcomes()
    assert len(results) == 0


def test_record_cycle_metrics(evo_manager):
    from dataclasses import dataclass, field

    @dataclass
    class FakeCycle:
        id: str = "evo-test"
        changes_proposed: int = 2
        changes_applied: int = 1

    cycle = FakeCycle()
    analysis = {"tool_error_rate": 0.1, "tool_calls_count": 50}
    evo_manager._record_cycle_metrics(cycle, analysis)

    metrics_path = evo_manager.agent_root / "state" / "evolution_metrics.jsonl"
    assert metrics_path.exists()
    entry = json.loads(metrics_path.read_text().strip())
    assert entry["cycle_id"] == "evo-test"
    assert entry["tool_error_rate"] == 0.1
    assert entry["proposals_generated"] == 2


def test_get_metrics_trend_empty(evo_manager):
    assert evo_manager._get_metrics_trend() == ""


def test_get_metrics_trend_with_data(evo_manager):
    metrics_path = evo_manager.agent_root / "state" / "evolution_metrics.jsonl"
    entries = [
        {"cycle_id": f"evo-{i}", "tool_error_rate": 0.1 * i, "proposals_generated": i, "proposals_applied": 0, "proposals_regressed": 0, "proposals_improved": 0}
        for i in range(3)
    ]
    metrics_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    trend = evo_manager._get_metrics_trend()
    assert "Evolution Trend" in trend
    assert "evo-0" in trend
    assert "evo-2" in trend
