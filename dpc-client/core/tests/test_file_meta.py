"""Tests for _meta.json Access Registry (ADR-010, MEM-1.1 + MEM-1.2 + MEM-2.1)."""

import json
import pathlib
from datetime import datetime, timezone, timedelta

from dpc_client_core.dpc_agent.memory import (
    FileMeta,
    backfill_meta,
    generate_smart_index,
    read_all_meta,
    write_all_meta,
    read_file_meta,
    write_file_meta,
    update_access,
)


def test_file_meta_defaults():
    m = FileMeta()
    assert m.access_count == 0
    assert m.tags == []
    assert m.source_layer == "L5"
    assert m.stale is False


def test_read_all_meta_empty(tmp_path):
    assert read_all_meta(tmp_path) == {}


def test_write_and_read_roundtrip(tmp_path):
    meta = FileMeta(summary="test topic", tags=["arch", "p2p"], access_count=3)
    write_file_meta(tmp_path, "topic.md", meta)
    loaded = read_file_meta(tmp_path, "topic.md")
    assert loaded.summary == "test topic"
    assert loaded.tags == ["arch", "p2p"]
    assert loaded.access_count == 3


def test_update_access_increments(tmp_path):
    update_access(tmp_path, "topic.md")
    m = read_file_meta(tmp_path, "topic.md")
    assert m.access_count == 1
    assert m.last_accessed != ""

    update_access(tmp_path, "topic.md")
    m2 = read_file_meta(tmp_path, "topic.md")
    assert m2.access_count == 2


def test_corrupt_meta_returns_empty(tmp_path):
    (tmp_path / "_meta.json").write_text("not json", encoding="utf-8")
    assert read_all_meta(tmp_path) == {}


def test_unknown_fields_ignored(tmp_path):
    data = {"topic.md": {"summary": "ok", "future_field": 42}}
    (tmp_path / "_meta.json").write_text(json.dumps(data), encoding="utf-8")
    m = read_file_meta(tmp_path, "topic.md")
    assert m.summary == "ok"


# --- MEM-1.2: backfill tests ---


def test_backfill_creates_entries(tmp_path):
    (tmp_path / "topic-a.md").write_text("Alpha content here", encoding="utf-8")
    (tmp_path / "topic-b.md").write_text("Beta content here", encoding="utf-8")
    data = backfill_meta(tmp_path)
    assert "topic-a.md" in data
    assert "topic-b.md" in data
    assert (tmp_path / "_meta.json").exists()


def test_backfill_skips_meta_and_index(tmp_path):
    (tmp_path / "_meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "_index.md").write_text("index", encoding="utf-8")
    (tmp_path / "real-topic.md").write_text("content", encoding="utf-8")
    # Remove _meta.json so backfill triggers
    (tmp_path / "_meta.json").unlink()
    data = backfill_meta(tmp_path)
    assert "real-topic.md" in data
    assert "_meta.json" not in data
    assert "_index.md" not in data


def test_backfill_extracts_tags_from_filename(tmp_path):
    (tmp_path / "agent-memory-architecture.md").write_text("x", encoding="utf-8")
    data = backfill_meta(tmp_path)
    entry = data["agent-memory-architecture.md"]
    assert "agent" in entry["tags"]
    assert "memory" in entry["tags"]
    assert "architecture" in entry["tags"]


def test_backfill_summary_truncates(tmp_path):
    (tmp_path / "long.md").write_text("A" * 500, encoding="utf-8")
    data = backfill_meta(tmp_path)
    assert len(data["long.md"]["summary"]) == 200


def test_read_all_meta_triggers_backfill(tmp_path):
    (tmp_path / "topic.md").write_text("content", encoding="utf-8")
    data = read_all_meta(tmp_path)
    assert "topic.md" in data
    assert (tmp_path / "_meta.json").exists()


# --- MEM-2.1: smart _index.md tests ---


def test_generate_smart_index_sections(tmp_path):
    now = datetime.now(timezone.utc)
    data = {
        "today.md": {"last_accessed": now.isoformat(), "summary": "Fresh topic"},
        "recent.md": {"last_accessed": (now - timedelta(days=3)).isoformat(), "summary": "Recent topic"},
        "old.md": {"last_accessed": (now - timedelta(days=15)).isoformat(), "summary": "Old topic"},
        "stale.md": {"last_accessed": (now - timedelta(days=45)).isoformat(), "summary": "Stale topic"},
    }
    (tmp_path / "_meta.json").write_text(json.dumps(data), encoding="utf-8")
    content = generate_smart_index(tmp_path)
    assert "## Active (today)" in content
    assert "## Recent (7 days)" in content
    assert "## Reference" in content
    assert "## Stale (30+ days)" in content
    assert "Fresh topic" in content
    assert "stale, last: 45 days" in content
    assert (tmp_path / "_index.md").exists()


def test_generate_smart_index_empty(tmp_path):
    content = generate_smart_index(tmp_path)
    assert content == ""


def test_update_access_triggers_index_refresh(tmp_path):
    (tmp_path / "topic.md").write_text("content", encoding="utf-8")
    write_file_meta(tmp_path, "topic.md", FileMeta(summary="test"))
    update_access(tmp_path, "topic.md")
    assert (tmp_path / "_index.md").exists()
    index_text = (tmp_path / "_index.md").read_text(encoding="utf-8")
    assert "Topic" in index_text
