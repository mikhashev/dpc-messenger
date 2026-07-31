"""`access_count` must count reads, because that is what its consumers ask it.

It counted writes: update_access had exactly one caller and it sat inside write_file,
so a document written once and read fifty times reported one access and consolidation
offered to archive it. Confirmed on the code graph — one production import, in the
write path.
"""
import json
import pathlib

import pytest

from dpc_client_core.dpc_agent.consolidation import STALE_DAYS, tier1_consolidate, tier2_propose
from dpc_client_core.dpc_agent.memory import (
    read_file_meta,
    record_write,
    update_access,
    write_file_meta,
    FileMeta,
)
from dpc_client_core.dpc_agent.tools.core import read_file, write_file
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from datetime import datetime, timedelta, timezone


@pytest.fixture
def agent(tmp_path):
    (tmp_path / "knowledge").mkdir()
    return tmp_path


def _ctx(agent):
    return ToolContext(agent_root=agent)


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# --- the two events, kept apart ---


def test_reading_a_knowledge_file_counts_as_a_read(agent):
    ctx = _ctx(agent)
    write_file(ctx, "knowledge/topic.md", "# Topic\nbody")
    assert read_file_meta(agent / "knowledge", "topic.md").access_count == 0

    read_file(ctx, "knowledge/topic.md")
    read_file(ctx, "knowledge/topic.md")

    meta = read_file_meta(agent / "knowledge", "topic.md")
    assert meta.access_count == 2
    assert meta.last_accessed


def test_writing_counts_as_a_write_and_not_as_a_read(agent):
    ctx = _ctx(agent)
    write_file(ctx, "knowledge/topic.md", "# Topic\none")
    write_file(ctx, "knowledge/topic.md", "# Topic\ntwo")

    meta = read_file_meta(agent / "knowledge", "topic.md")
    assert meta.write_count == 2
    assert meta.last_written
    assert meta.access_count == 0


def test_reads_outside_the_knowledge_directory_are_not_recorded(agent):
    """Only that directory has a registry; the other layers are ranked elsewhere."""
    (agent / "notes.md").write_text("loose", encoding="utf-8")
    read_file(_ctx(agent), "notes.md")
    assert not (agent / "_meta.json").exists()


def test_a_failed_read_records_nothing(agent):
    ctx = _ctx(agent)
    write_file(ctx, "knowledge/topic.md", "# Topic")
    read_file(ctx, "knowledge/missing.md")
    assert read_file_meta(agent / "knowledge", "missing.md").access_count == 0


# --- what consolidation now decides on ---


def test_a_file_read_often_is_not_offered_for_archiving(agent):
    """The acceptance criterion: written once long ago, read many times since."""
    kdir = agent / "knowledge"
    (kdir / "topic.md").write_text("body", encoding="utf-8")
    write_file_meta(kdir, "topic.md", FileMeta(
        last_written=_iso(STALE_DAYS + 60), write_count=1,
        last_accessed=_iso(1), access_count=25,
    ))
    assert tier2_propose(kdir) == []


def test_a_file_just_written_is_not_stale_for_never_having_been_read(agent):
    """Moving the counter to reads must not archive everything new."""
    kdir = agent / "knowledge"
    (kdir / "fresh.md").write_text("body", encoding="utf-8")
    write_file_meta(kdir, "fresh.md", FileMeta(last_written=_iso(0), write_count=1))

    assert tier2_propose(kdir) == []
    tier1_consolidate(kdir)
    assert read_file_meta(kdir, "fresh.md").stale is False


def test_an_old_unread_file_is_still_offered(agent):
    kdir = agent / "knowledge"
    (kdir / "old.md").write_text("body", encoding="utf-8")
    write_file_meta(kdir, "old.md", FileMeta(last_written=_iso(STALE_DAYS + 5), write_count=1))

    proposals = tier2_propose(kdir)
    assert [p["file"] for p in proposals] == ["old.md"]
    assert "read 0 time(s)" in proposals[0]["reason"]


def test_a_file_with_no_history_at_all_is_offered(agent):
    kdir = agent / "knowledge"
    (kdir / "orphan.md").write_text("body", encoding="utf-8")
    write_file_meta(kdir, "orphan.md", FileMeta())
    assert tier2_propose(kdir)[0]["reason"] == "never read and never written"


# --- the history that already exists on disk ---


def test_legacy_access_numbers_move_to_the_column_they_described(agent):
    """They were all produced by writes, so this is a rename, not a guess."""
    kdir = agent / "knowledge"
    (kdir / "_meta.json").write_text(json.dumps({
        "topic.md": {"last_accessed": "2026-01-01T00:00:00Z", "access_count": 7, "summary": "s"}
    }), encoding="utf-8")

    meta = read_file_meta(kdir, "topic.md")
    assert meta.write_count == 7
    assert meta.last_written == "2026-01-01T00:00:00Z"
    assert meta.access_count == 0
    assert meta.last_accessed == ""
    assert meta.summary == "s"

    on_disk = json.loads((kdir / "_meta.json").read_text(encoding="utf-8"))
    assert on_disk["topic.md"]["write_count"] == 7


def test_the_index_sections_survive_the_migration(agent):
    """Every existing entry has only a write date after migrating, and bucketing by
    reads alone would drop the whole index into one section."""
    from dpc_client_core.dpc_agent.memory import generate_smart_index

    kdir = agent / "knowledge"
    (kdir / "_meta.json").write_text(json.dumps({
        "today.md": {"last_accessed": _iso(0), "summary": "Fresh"},
        "stale.md": {"last_accessed": _iso(45), "summary": "Old"},
    }), encoding="utf-8")

    content = generate_smart_index(kdir)
    assert "## Active (today)" in content
    assert "## Stale (30+ days)" in content


def test_migration_does_not_run_twice(agent):
    kdir = agent / "knowledge"
    (kdir / "_meta.json").write_text(json.dumps({
        "topic.md": {"last_accessed": "", "access_count": 0,
                     "last_written": "2026-01-01T00:00:00Z", "write_count": 3}
    }), encoding="utf-8")

    update_access(kdir, "topic.md")
    meta = read_file_meta(kdir, "topic.md")
    assert meta.access_count == 1
    assert meta.write_count == 3
