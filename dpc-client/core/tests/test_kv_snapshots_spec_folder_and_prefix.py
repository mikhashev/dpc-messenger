"""Spec tests for the KV snapshot folder preparation and the prefix digest.
Written from ADR-044 (amendment 2026-11-13, points 5 and 6; "A missing disk")
and the slice-1 plan (section 2, P1) without sight of the implementation.
"""

import pytest

from dpc_client_core.managers import kv_snapshots as kv


# --------------------------------------------------------------- prepare_folder

def test_a_missing_parent_returns_the_off_reason_and_creates_nothing(tmp_path):
    parent = tmp_path / "unplugged-usb"
    folder = parent / "kv"

    reason = kv.prepare_folder(folder)

    assert isinstance(reason, str) and reason
    assert "missing" in reason.lower()
    assert not parent.exists()
    assert not folder.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_missing_parent_a_few_levels_up_also_creates_nothing(tmp_path):
    folder = tmp_path / "gone" / "deeper" / "kv"
    assert kv.prepare_folder(folder)
    assert list(tmp_path.iterdir()) == []


def test_an_existing_folder_is_ready(tmp_path):
    assert kv.prepare_folder(tmp_path) is None


def test_a_folder_whose_parent_exists_is_made(tmp_path):
    folder = tmp_path / "kv"
    assert kv.prepare_folder(folder) is None
    assert folder.is_dir()


def test_a_file_in_the_way_on_an_existing_parent_is_an_oserror_not_an_off_reason(tmp_path):
    blocker = tmp_path / "kv"
    blocker.write_bytes(b"i am a file")
    with pytest.raises(OSError):
        kv.prepare_folder(blocker)


# ----------------------------------------------------------------- prefix digest

MSGS = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi"},
    {"role": "user", "content": "and now?"},
]


def test_the_digest_is_a_sha256_hex_and_the_message_count():
    digest, k = kv.prefix_digest(MSGS)
    assert isinstance(digest, str) and len(digest) == 64
    int(digest, 16)
    assert k == len(MSGS)


def test_the_digest_is_deterministic():
    assert kv.prefix_digest(MSGS) == kv.prefix_digest(list(MSGS))


def test_a_shorter_prefix_has_its_own_digest_and_length():
    full, k_full = kv.prefix_digest(MSGS)
    part, k_part = kv.prefix_digest(MSGS, 2)
    assert k_part == 2
    assert part != full
    assert kv.prefix_digest(MSGS[:2]) == (part, 2)


def test_a_changed_message_changes_the_digest():
    other = [dict(m) for m in MSGS]
    other[1]["content"] = "hello!"
    assert kv.prefix_digest(other)[0] != kv.prefix_digest(MSGS)[0]


def _entry_for(msgs, k=None):
    digest, n = kv.prefix_digest(msgs, k)
    return {"prefix_sha256": digest, "prefix_messages": n}


def test_a_request_that_extends_the_saved_prefix_matches():
    saved = _entry_for(MSGS[:2])
    longer_request = MSGS + [{"role": "assistant", "content": "more"}]
    assert kv.matches(saved, longer_request)


def test_a_request_that_rewrote_the_saved_prefix_does_not_match():
    saved = _entry_for(MSGS[:2])
    rewritten = [dict(m) for m in MSGS]
    rewritten[0]["content"] = "be verbose"      # a compaction or a cut moved it
    assert not kv.matches(saved, rewritten)


def test_a_request_shorter_than_the_saved_prefix_does_not_match():
    saved = _entry_for(MSGS)
    assert not kv.matches(saved, MSGS[:2])
