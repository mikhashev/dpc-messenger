"""Tests for _meta.json Access Registry (ADR-010, MEM-1.1)."""

import json
import pathlib

from dpc_client_core.dpc_agent.memory import (
    FileMeta,
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
