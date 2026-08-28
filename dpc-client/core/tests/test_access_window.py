"""One window for both halves of the access counter, and a log that stops growing.

The counter divides what was shown by what was read. The two numbers came from files
with different lifetimes — the injection log has never rotated, the read log rotates at
5 MB and keeps exactly one old file — so on agent_001 it was comparing 103 days of
showing against 11 days of reading, and half of the reading was sitting unread in the
file next to it.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from dpc_client_core.dpc_agent.active_recall import (
    ACCESS_LOG_ARCHIVE,
    _build_access_counts,
    compact_access_log,
)


def _agent(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "agent"
    (root / "state").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    return root


def _injections(root: pathlib.Path, entries) -> None:
    (root / "state" / "knowledge_access.jsonl").write_text(
        "".join(json.dumps({"ts": ts, "mode": "full", "files": files, "useful": None}) + "\n"
                for ts, files in entries),
        encoding="utf-8",
    )


def _reads(path: pathlib.Path, entries) -> None:
    path.write_text(
        "".join(json.dumps({"ts": ts, "tool": "read_file", "args": {"path": p}}) + "\n"
                for ts, p in entries),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# The rotated half of the read log
# --------------------------------------------------------------------------

def test_reads_in_the_rotated_log_are_counted(tmp_path):
    root = _agent(tmp_path)
    _reads(root / "logs" / "tools.jsonl.1", [("2026-07-08T00:00:00+00:00", "knowledge/alpha.md")])
    _reads(root / "logs" / "tools.jsonl", [("2026-07-30T00:00:00+00:00", "knowledge/alpha.md")])

    counts = _build_access_counts(root)

    assert counts.reads_by_key["knowledge/alpha.md"] == 2


def test_a_read_only_in_the_rotated_log_is_not_lost(tmp_path):
    """The case that was actually happening: 13 days of reads on disk, none counted."""
    root = _agent(tmp_path)
    _reads(root / "logs" / "tools.jsonl.1", [("2026-07-08T00:00:00+00:00", "knowledge/beta.md")])
    _reads(root / "logs" / "tools.jsonl", [("2026-07-30T00:00:00+00:00", "knowledge/alpha.md")])

    counts = _build_access_counts(root)

    assert counts.reads_by_key["knowledge/beta.md"] == 1


# --------------------------------------------------------------------------
# One window
# --------------------------------------------------------------------------

def test_injections_from_before_the_oldest_read_are_not_counted(tmp_path):
    """Credit earned in a period whose reads have expired cannot be checked against
    anything, and counting it is what made a long-shown document look established."""
    root = _agent(tmp_path)
    _injections(root, [
        ("2026-04-20T00:00:00+00:00", ["knowledge/old.md"]),
        ("2026-07-30T00:00:00+00:00", ["knowledge/current.md"]),
    ])
    _reads(root / "logs" / "tools.jsonl", [("2026-07-21T00:00:00+00:00", "knowledge/other.md")])

    counts = _build_access_counts(root)

    assert "knowledge/old.md" not in counts.injections_by_key
    assert counts.injections_by_key["knowledge/current.md"] == 1


def test_the_window_opens_at_the_oldest_read_including_the_rotated_log(tmp_path):
    """Reading the rotated log widens the window, so it must widen what is counted too
    — otherwise the fix to one half silently discards more of the other."""
    root = _agent(tmp_path)
    _injections(root, [("2026-07-10T00:00:00+00:00", ["knowledge/mid.md"])])
    _reads(root / "logs" / "tools.jsonl.1", [("2026-07-08T00:00:00+00:00", "knowledge/x.md")])
    _reads(root / "logs" / "tools.jsonl", [("2026-07-21T00:00:00+00:00", "knowledge/x.md")])

    assert _build_access_counts(root).injections_by_key["knowledge/mid.md"] == 1


def test_a_log_with_no_reads_at_all_is_counted_whole(tmp_path):
    """No reads is an absence, not a mismatch. An agent that has never opened anything
    still gets the weak ordering injections provide."""
    root = _agent(tmp_path)
    _injections(root, [("2026-04-20T00:00:00+00:00", ["knowledge/old.md"])])

    assert _build_access_counts(root).injections_by_key["knowledge/old.md"] == 1


# --------------------------------------------------------------------------
# Compaction
# --------------------------------------------------------------------------

def test_compaction_archives_what_the_window_excludes_and_keeps_the_rest(tmp_path):
    root = _agent(tmp_path)
    _injections(root, [
        ("2026-04-20T00:00:00+00:00", ["knowledge/old.md"]),
        ("2026-04-21T00:00:00+00:00", ["knowledge/old.md"]),
        ("2026-07-30T00:00:00+00:00", ["knowledge/current.md"]),
    ])
    _reads(root / "logs" / "tools.jsonl", [("2026-07-21T00:00:00+00:00", "knowledge/x.md")])

    assert compact_access_log(root) == 2

    live = (root / "state" / "knowledge_access.jsonl").read_text(encoding="utf-8").strip().splitlines()
    archive = (root / "state" / ACCESS_LOG_ARCHIVE).read_text(encoding="utf-8").strip().splitlines()
    assert len(live) == 1 and "current.md" in live[0]
    assert len(archive) == 2 and all("old.md" in line for line in archive)


def test_compaction_loses_no_line(tmp_path):
    """The archive exists so that bounding the runtime cost is not a deletion."""
    root = _agent(tmp_path)
    entries = [(f"2026-04-{d:02d}T00:00:00+00:00", ["knowledge/old.md"]) for d in range(1, 20)]
    entries.append(("2026-07-30T00:00:00+00:00", ["knowledge/current.md"]))
    _injections(root, entries)
    _reads(root / "logs" / "tools.jsonl", [("2026-07-21T00:00:00+00:00", "knowledge/x.md")])

    compact_access_log(root)

    live = (root / "state" / "knowledge_access.jsonl").read_text(encoding="utf-8").strip().splitlines()
    archive = (root / "state" / ACCESS_LOG_ARCHIVE).read_text(encoding="utf-8").strip().splitlines()
    assert len(live) + len(archive) == len(entries)


def test_compaction_is_idempotent(tmp_path):
    root = _agent(tmp_path)
    _injections(root, [
        ("2026-04-20T00:00:00+00:00", ["knowledge/old.md"]),
        ("2026-07-30T00:00:00+00:00", ["knowledge/current.md"]),
    ])
    _reads(root / "logs" / "tools.jsonl", [("2026-07-21T00:00:00+00:00", "knowledge/x.md")])

    assert compact_access_log(root) == 1
    assert compact_access_log(root) == 0


def test_compaction_does_nothing_without_reads_to_define_a_window(tmp_path):
    root = _agent(tmp_path)
    _injections(root, [("2026-04-20T00:00:00+00:00", ["knowledge/old.md"])])

    assert compact_access_log(root) == 0
    assert not (root / "state" / ACCESS_LOG_ARCHIVE).exists()


def test_a_torn_line_is_kept_where_a_human_can_find_it(tmp_path):
    """A half-written line has no timestamp to judge, and archiving it would hide it."""
    root = _agent(tmp_path)
    path = root / "state" / "knowledge_access.jsonl"
    path.write_text(
        json.dumps({"ts": "2026-04-20T00:00:00+00:00", "files": ["knowledge/old.md"]}) + "\n"
        + '{"ts": "2026-04-21T00:00:00+00:00", "fil\n'
        + json.dumps({"ts": "2026-07-30T00:00:00+00:00", "files": ["knowledge/current.md"]}) + "\n",
        encoding="utf-8",
    )
    _reads(root / "logs" / "tools.jsonl", [("2026-07-21T00:00:00+00:00", "knowledge/x.md")])

    compact_access_log(root)

    live = path.read_text(encoding="utf-8")
    assert '"fil' in live
    assert _build_access_counts(root)  # and the counter still reads the file


def test_the_archive_is_not_counted(tmp_path):
    """It sits in the same directory; counting it would undo the whole point."""
    root = _agent(tmp_path)
    _injections(root, [
        ("2026-04-20T00:00:00+00:00", ["knowledge/old.md"]),
        ("2026-07-30T00:00:00+00:00", ["knowledge/current.md"]),
    ])
    _reads(root / "logs" / "tools.jsonl", [("2026-07-21T00:00:00+00:00", "knowledge/x.md")])
    compact_access_log(root)

    counts = _build_access_counts(root)

    assert "knowledge/old.md" not in counts.injections_by_key
