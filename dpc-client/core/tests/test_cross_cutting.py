"""Tests for cross-cutting tasks (ADR-010, MEM-X.1/X.3)."""

import json
from datetime import datetime, timezone, timedelta

from dpc_client_core.dpc_agent.consolidation import tier1_consolidate, tier2_propose


def test_tier1_marks_stale(tmp_path):
    now = datetime.now(timezone.utc)
    data = {
        "fresh.md": {"last_accessed": now.isoformat(), "stale": False},
        "old.md": {"last_accessed": (now - timedelta(days=45)).isoformat(), "stale": False},
    }
    (tmp_path / "_meta.json").write_text(json.dumps(data), encoding="utf-8")
    result = tier1_consolidate(tmp_path)
    assert result["stale_marked"] == 1
    assert result["total"] == 2


def test_tier1_empty(tmp_path):
    result = tier1_consolidate(tmp_path)
    assert result["total"] == 0


def test_tier2_proposes_archive(tmp_path):
    now = datetime.now(timezone.utc)
    data = {
        "stale.md": {
            "last_accessed": (now - timedelta(days=60)).isoformat(),
            "access_count": 1, "stale": True,
        },
        "active.md": {
            "last_accessed": now.isoformat(),
            "access_count": 10, "stale": False,
        },
    }
    (tmp_path / "_meta.json").write_text(json.dumps(data), encoding="utf-8")
    proposals = tier2_propose(tmp_path)
    assert len(proposals) == 1
    assert proposals[0]["action"] == "archive"
    assert proposals[0]["file"] == "stale.md"


def test_tier2_never_accessed(tmp_path):
    data = {"orphan.md": {"summary": "no access"}}
    (tmp_path / "_meta.json").write_text(json.dumps(data), encoding="utf-8")
    proposals = tier2_propose(tmp_path)
    assert len(proposals) == 1
    assert proposals[0]["reason"] == "never accessed"


