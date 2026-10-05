"""A knowledge file's summary must follow its content, not its first draft.

write_file set `summary` only while it was empty, and record_write never touched
it, so a rewritten file kept the description of its first version in _meta.json
and in the _index.md that Active Recall reads.
"""
import json

import pytest

from dpc_client_core.dpc_agent.memory import (
    FileMeta,
    read_file_meta,
    record_write,
    refresh_summaries,
    write_file_meta,
)
from dpc_client_core.dpc_agent.tools.core import write_file
from dpc_client_core.dpc_agent.tools.registry import ToolContext


@pytest.fixture
def agent(tmp_path):
    (tmp_path / "knowledge").mkdir()
    return tmp_path


def _index(agent):
    return (agent / "knowledge" / "_index.md").read_text(encoding="utf-8")


def test_a_rewrite_replaces_the_summary_and_the_index_line(agent):
    ctx = ToolContext(agent_root=agent)
    write_file(ctx, "knowledge/topic.md", "# Topic\none")
    write_file(ctx, "knowledge/topic.md", "# Topic\ntwo")

    summary = read_file_meta(agent / "knowledge", "topic.md").summary
    assert "two" in summary and "one" not in summary
    index = _index(agent)
    assert "two" in index and "one" not in index


def test_a_rewrite_past_the_opening_leaves_the_index_bytes_alone(agent):
    """The summary is the first 1000 chars, so a change past them must not move
    the cached index."""
    ctx = ToolContext(agent_root=agent)
    head = "# Topic\n" + "x" * 1100
    write_file(ctx, "knowledge/topic.md", head + "first tail")
    before = _index(agent)
    write_file(ctx, "knowledge/topic.md", head + "second tail")
    assert _index(agent) == before


def test_record_write_without_content_keeps_the_summary(agent):
    kdir = agent / "knowledge"
    (kdir / "topic.md").write_text("# Topic\nnew body", encoding="utf-8")
    write_file_meta(kdir, "topic.md", FileMeta(summary="kept as it was"))
    record_write(kdir, "topic.md")
    meta = read_file_meta(kdir, "topic.md")
    assert meta.summary == "kept as it was"
    assert meta.write_count == 1


def test_refresh_summaries_fixes_stale_entries_and_nothing_else(agent):
    kdir = agent / "knowledge"
    (kdir / "stale.md").write_text("# Stale\ncurrent text", encoding="utf-8")
    (kdir / "fresh.md").write_text("# Fresh\nsame", encoding="utf-8")
    # A file with no entry: refresh_summaries must not add one.
    (kdir / "orphan.md").write_text("# Orphan\nnever registered", encoding="utf-8")
    (kdir / "_meta.json").write_text(json.dumps({
        "stale.md": {"summary": "# Stale\nfirst draft", "access_count": 7, "write_count": 3,
                     "last_accessed": "2026-09-01T00:00:00+00:00",
                     "last_written": "2026-09-02T00:00:00+00:00", "tags": ["stale"]},
        "fresh.md": {"summary": "# Fresh\nsame", "write_count": 1,
                     "last_written": "2026-09-02T00:00:00+00:00"},
        "ghost.md": {"summary": "gone", "access_count": 2, "write_count": 1, "last_written": ""},
    }), encoding="utf-8")

    counts = refresh_summaries(kdir)

    assert counts == {"changed": 1, "unchanged": 1, "missing_file": 1}
    data = json.loads((kdir / "_meta.json").read_text(encoding="utf-8"))
    assert data["stale.md"]["summary"] == "# Stale\ncurrent text"
    assert data["stale.md"]["access_count"] == 7
    assert data["stale.md"]["write_count"] == 3
    assert data["stale.md"]["last_accessed"] == "2026-09-01T00:00:00+00:00"
    assert data["stale.md"]["tags"] == ["stale"]
    assert data["ghost.md"] == {"summary": "gone", "access_count": 2, "write_count": 1,
                                "last_written": ""}
    assert set(data) == {"stale.md", "fresh.md", "ghost.md"}
    assert "current text" in _index(agent)
    assert not (kdir / "_meta.json.tmp").exists()


def test_refresh_summaries_dry_run_writes_nothing(agent):
    kdir = agent / "knowledge"
    (kdir / "stale.md").write_text("new", encoding="utf-8")
    raw = json.dumps({"stale.md": {"summary": "old"}})
    (kdir / "_meta.json").write_text(raw, encoding="utf-8")
    assert refresh_summaries(kdir, apply=False)["changed"] == 1
    assert (kdir / "_meta.json").read_text(encoding="utf-8") == raw
    assert not (kdir / "_index.md").exists()
