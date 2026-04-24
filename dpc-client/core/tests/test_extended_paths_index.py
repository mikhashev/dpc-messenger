"""Tests for Extended Paths indexing (ADR-010, MEM-3.10)."""

from dpc_client_core.dpc_agent.extended_paths_index import (
    collect_extended_files, check_mtime_changes, _is_text_file,
)
from pathlib import Path


def test_collect_text_files(tmp_path):
    (tmp_path / "doc.md").write_text("content", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    (tmp_path / "code.py").write_text("print(1)", encoding="utf-8")

    files = collect_extended_files({"read_only": [str(tmp_path)]})
    names = {f.name for f in files}
    assert "doc.md" in names
    assert "code.py" in names
    assert "image.png" not in names


def test_collect_empty_paths():
    assert collect_extended_files({"read_only": [], "read_write": []}) == []


def test_mtime_detects_changes(tmp_path):
    import os, time
    f = tmp_path / "doc.md"
    f.write_text("v1", encoding="utf-8")
    files = [f]

    changed, cache = check_mtime_changes(files, {})
    assert len(changed) == 1

    changed2, cache2 = check_mtime_changes(files, cache)
    assert len(changed2) == 0

    time.sleep(0.05)
    f.write_text("v2", encoding="utf-8")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 1))
    changed3, _ = check_mtime_changes(files, cache2)
    assert len(changed3) == 1


def test_is_text_file():
    assert _is_text_file(Path("doc.md"))
    assert _is_text_file(Path("config.yaml"))
    assert not _is_text_file(Path("image.png"))
    assert not _is_text_file(Path("model.faiss"))
