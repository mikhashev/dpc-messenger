"""Tests for text extraction (ADR-010, MEM-3.2)."""

import json

from dpc_client_core.dpc_agent.text_extract import extract_text, is_binary


def test_markdown_extraction(tmp_path):
    f = tmp_path / "topic.md"
    f.write_text("# Title\n\nContent here", encoding="utf-8")
    assert extract_text(f) == "# Title\n\nContent here"


def test_json_value_extraction(tmp_path):
    f = tmp_path / "data.json"
    f.write_text(json.dumps({"name": "Alice", "nested": {"desc": "Engineer"}}), encoding="utf-8")
    result = extract_text(f)
    assert "Alice" in result
    assert "Engineer" in result


def test_binary_skip(tmp_path):
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG\r\n")
    assert extract_text(f) is None


def test_is_binary():
    from pathlib import Path
    assert is_binary(Path("file.png"))
    assert is_binary(Path("model.faiss"))
    assert not is_binary(Path("doc.md"))
    assert not is_binary(Path("config.ini"))


def test_max_chars_truncation(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("A" * 200, encoding="utf-8")
    result = extract_text(f, max_chars=100)
    assert len(result) == 100


def test_txt_extraction(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("Plain text content", encoding="utf-8")
    assert extract_text(f) == "Plain text content"
