"""A hash of digits only is valid YAML for an integer, and the parser obliges.

Two of 265 knowledge commits carry a 16-character content_hash with no letters in it.
The frontmatter parser hands those back as `int`, the check compared `str` against `int`,
and both files were reported as "Markdown content manually edited" on every startup —
with their content untouched since the day they were written.

That was fixed once, in the client's own reader. The same comparison lived here, in the
protocol library, and this is the one the integrity report actually runs: four warnings
per startup about two files nobody had edited.

A false alarm about integrity is not a harmless one. It is the alarm nobody believes the
next time it fires, and the next time may be real.
"""
from __future__ import annotations

import pytest

from dpc_protocol.commit_integrity import (
    compute_content_hash,
    parse_markdown_with_frontmatter,
    verify_markdown_integrity,
)

# The value from the live file, digits only — this is not a hypothetical.
ALL_DIGIT_HASH_SAMPLE = "4541343283619917"


def _write_commit(tmp_path, content: str, content_hash: str):
    path = tmp_path / "topic_commit-099fd77535e5d332.md"
    path.write_text(
        "---\n"
        "commit_id: commit-099fd77535e5d332\n"
        f"content_hash: {content_hash}\n"
        "---\n"
        f"{content}",
        encoding="utf-8",
    )
    return path


def test_yaml_really_does_turn_such_a_hash_into_a_number():
    assert ALL_DIGIT_HASH_SAMPLE != int(ALL_DIGIT_HASH_SAMPLE)
    assert ALL_DIGIT_HASH_SAMPLE == str(int(ALL_DIGIT_HASH_SAMPLE))


def test_untouched_content_with_an_all_digit_hash_is_not_reported_as_edited(tmp_path):
    content = "# Topic\n\nSomething written once and never touched.\n"
    path = _write_commit(tmp_path, content, "placeholder")
    # Hash what the parser hands the checker, not what we wrote — the body it
    # returns is the thing under comparison.
    _, parsed_body = parse_markdown_with_frontmatter(path)
    path = _write_commit(tmp_path, content, compute_content_hash(parsed_body))

    result = verify_markdown_integrity(path)
    tampering = [w for w in result["warnings"] if w["type"] == "content_tampered"]
    assert not tampering, f"unedited content reported as edited: {tampering}"


def test_a_hash_the_parser_turns_into_an_integer_still_verifies(tmp_path, monkeypatch):
    """The live case: the stored hash is all digits, so yaml yields an int."""
    content = "# Topic\n\nUntouched.\n"
    path = _write_commit(tmp_path, content, ALL_DIGIT_HASH_SAMPLE)

    # Make the computed hash equal the stored one, so the only thing that can
    # make them differ is the type the parser chose.
    monkeypatch.setattr(
        "dpc_protocol.commit_integrity.compute_content_hash",
        lambda _content: ALL_DIGIT_HASH_SAMPLE,
    )

    result = verify_markdown_integrity(path)
    tampering = [w for w in result["warnings"] if w["type"] == "content_tampered"]
    assert not tampering, f"an all-digit hash was read as tampering: {tampering}"
    assert result["content_hash_valid"] is True


def test_genuinely_changed_content_is_still_caught(tmp_path):
    """The fix must not turn the check off."""
    path = _write_commit(tmp_path, "# Topic\n\nEdited after the fact.\n", ALL_DIGIT_HASH_SAMPLE)

    result = verify_markdown_integrity(path)
    tampering = [w for w in result["warnings"] if w["type"] == "content_tampered"]
    assert tampering, "edited content passed the integrity check"
