"""Smoke tests for sleep_pipeline helpers (idempotency / stable naming / hash compute)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.sleep_pipeline import (
    _compute_archive_hash,
    _migrate_legacy_results,
    _result_filename,
)


def test_result_filename_one_to_one_strips_json():
    name = _result_filename("2026-05-13T17-28-25_reset_session.json")
    assert name == "result_2026-05-13T17-28-25_reset_session.json"


def test_result_filename_group_archive_sanitizes_colons():
    # `:` → `--` because `:` is the Windows drive separator and `_` is
    # already used inside archive timestamps + group ids may contain `_`.
    name = _result_filename(
        "group_archive:group-b88b65076b85-dpc-project:2026-05-09T18-19-41_reset_session.json"
    )
    assert ":" not in name
    assert name.startswith("result_group_archive--group-b88b65076b85-dpc-project--")
    assert name.endswith("_reset_session.json")


def test_result_filename_empty_input():
    # Edge case — empty archive_file should still produce a well-formed name.
    name = _result_filename("")
    assert name == "result_.json"


def test_compute_archive_hash_one_to_one(tmp_path: Path):
    conv = tmp_path / "agent_001"
    archive_dir = conv / "archive" / "2026" / "05"
    archive_dir.mkdir(parents=True)
    archive = archive_dir / "2026-05-13T12-00-00_reset_session.json"
    payload = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    archive.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    digest = {
        "archive_file": "2026-05-13T12-00-00_reset_session.json",
        "source": "one_to_one",
    }
    assert _compute_archive_hash(digest, conv) == expected


def test_compute_archive_hash_group_archive(tmp_path: Path):
    archive_path = tmp_path / "group-xxx-dpc" / "archive" / "2026-05-10T09-50-29_reset_session.json"
    archive_path.parent.mkdir(parents=True)
    payload = b'{"messages": [{"role": "user", "content": "hello"}]}'
    archive_path.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    digest = {
        "archive_file": f"group_archive:group-xxx-dpc:{archive_path.name}",
        "source": "group_archive",
        "_archive_path": str(archive_path),
    }
    # conversation_dir is irrelevant for group_archive — hash uses _archive_path.
    assert _compute_archive_hash(digest, tmp_path / "irrelevant") == expected


def test_compute_archive_hash_missing_returns_empty(tmp_path: Path):
    conv = tmp_path / "agent_001"
    (conv / "archive").mkdir(parents=True)
    digest = {
        "archive_file": "nonexistent_reset_session.json",
        "source": "one_to_one",
    }
    assert _compute_archive_hash(digest, conv) == ""


def test_compute_archive_hash_different_content_different_hash(tmp_path: Path):
    conv = tmp_path / "agent_001"
    archive_dir = conv / "archive" / "2026" / "05"
    archive_dir.mkdir(parents=True)
    a = archive_dir / "a_reset_session.json"
    b = archive_dir / "b_reset_session.json"
    a.write_bytes(b'{"version": 1}')
    b.write_bytes(b'{"version": 2}')

    da = {"archive_file": "a_reset_session.json", "source": "one_to_one"}
    db = {"archive_file": "b_reset_session.json", "source": "one_to_one"}
    assert _compute_archive_hash(da, conv) != _compute_archive_hash(db, conv)


def test_migrate_legacy_results_renames_session_files(tmp_path: Path):
    results_dir = tmp_path / "sleep_results"
    results_dir.mkdir()
    legacy = results_dir / "session_5.json"
    legacy.write_text(json.dumps({
        "archive_file": "2026-05-13T17-28-25_reset_session.json",
        "summary": "test",
    }), encoding="utf-8")

    count = _migrate_legacy_results(results_dir)
    assert count == 1
    assert not legacy.exists()
    assert (results_dir / "result_2026-05-13T17-28-25_reset_session.json").exists()


def test_migrate_legacy_results_idempotent(tmp_path: Path):
    results_dir = tmp_path / "sleep_results"
    results_dir.mkdir()
    (results_dir / "session_0.json").write_text(json.dumps({
        "archive_file": "x_reset_session.json",
    }), encoding="utf-8")

    # First call renames.
    assert _migrate_legacy_results(results_dir) == 1
    # Second call has nothing to do — no `session_*.json` files left.
    assert _migrate_legacy_results(results_dir) == 0
    # New file in place.
    assert (results_dir / "result_x_reset_session.json").exists()


def test_migrate_legacy_results_skips_missing_archive_field(tmp_path: Path):
    # Legacy file without `archive_file` key — can't derive new name, skip.
    results_dir = tmp_path / "sleep_results"
    results_dir.mkdir()
    legacy = results_dir / "session_0.json"
    legacy.write_text(json.dumps({"summary": "broken"}), encoding="utf-8")

    assert _migrate_legacy_results(results_dir) == 0
    assert legacy.exists()  # untouched


def test_migrate_legacy_results_deletes_when_target_exists(tmp_path: Path):
    # Legacy file points to archive that already has a `result_*.json` file —
    # delete the legacy duplicate, don't overwrite the new one.
    results_dir = tmp_path / "sleep_results"
    results_dir.mkdir()
    target = results_dir / "result_dup_reset_session.json"
    target.write_text(json.dumps({"archive_file": "dup_reset_session.json", "summary": "newer"}), encoding="utf-8")
    legacy = results_dir / "session_3.json"
    legacy.write_text(json.dumps({"archive_file": "dup_reset_session.json", "summary": "older"}), encoding="utf-8")

    count = _migrate_legacy_results(results_dir)
    assert count == 1
    assert not legacy.exists()
    # Target preserved (not overwritten with older content).
    assert json.loads(target.read_text(encoding="utf-8"))["summary"] == "newer"


def test_migrate_legacy_results_ignores_non_legacy_files(tmp_path: Path):
    # `session_NOT_DIGIT.json` is not a legacy file — don't touch.
    results_dir = tmp_path / "sleep_results"
    results_dir.mkdir()
    (results_dir / "session_foo.json").write_text("{}", encoding="utf-8")
    (results_dir / "result_x_reset_session.json").write_text(
        json.dumps({"archive_file": "x_reset_session.json"}), encoding="utf-8"
    )

    assert _migrate_legacy_results(results_dir) == 0
    assert (results_dir / "session_foo.json").exists()
