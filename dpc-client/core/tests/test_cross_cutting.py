"""Tests for cross-cutting tasks (ADR-010, MEM-X.1/X.2/X.3)."""

import json
from datetime import datetime, timezone, timedelta

from dpc_client_core.dpc_agent.consolidation import tier1_consolidate, tier2_propose
from dpc_client_core.dpc_agent.model_swap import detect_model_mismatch, rebuild_prompt_message


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


def test_model_swap_no_index(tmp_path):
    result = detect_model_mismatch(tmp_path, "model-a")
    assert result is None


def test_model_swap_mismatch(tmp_path):
    import numpy as np
    from dpc_client_core.dpc_agent.faiss_index import FaissIndex
    idx = FaissIndex(tmp_path, model_name="old-model", dimensions=4)
    idx.add(np.array([[1, 0, 0, 0]], dtype=np.float32), [{"file": "a.md"}])
    idx.save()

    result = detect_model_mismatch(tmp_path, "new-model")
    assert result is not None
    assert result["needs_rebuild"] is True
    assert result["saved_model"] == "old-model"


def test_model_swap_match(tmp_path):
    import numpy as np
    from dpc_client_core.dpc_agent.faiss_index import FaissIndex
    idx = FaissIndex(tmp_path, model_name="same-model", dimensions=4)
    idx.add(np.array([[1, 0, 0, 0]], dtype=np.float32), [{"file": "a.md"}])
    idx.save()

    result = detect_model_mismatch(tmp_path, "same-model")
    assert result is None


def test_rebuild_prompt():
    msg = rebuild_prompt_message({
        "saved_model": "old", "current_model": "new", "chunk_count": 100,
    })
    assert "old" in msg
    assert "new" in msg
    assert "100" in msg
