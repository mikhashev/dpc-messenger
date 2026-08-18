"""The knowledge index sits ahead of the whole history, so its bytes must move
only when the knowledge does.

Two separate defects are pinned here. The first is time: the index bucketed its
files with `datetime.now()`, so the same knowledge base rendered differently as
the clock passed midnight or as a "last: N days" label counted up — a changed
prefix with no changed knowledge. The second is frequency: every *read* of a
knowledge file regenerated the file, so an agent that read three documents inside
one turn rewrote the index three times and reordered it under itself.
"""
import json
import pathlib

import pytest

from dpc_client_core.dpc_agent import memory as memory_mod
from dpc_client_core.dpc_agent.memory import (
    generate_smart_index,
    update_access,
    record_write,
)


def _meta(**kw):
    base = {"summary": "", "last_accessed": "", "last_written": "",
            "access_count": 0, "write_count": 0}
    base.update(kw)
    return base


@pytest.fixture
def knowledge_dir(tmp_path):
    d = tmp_path / "knowledge"
    d.mkdir()
    for name in ("fresh.md", "another_fresh.md", "week_old.md", "ancient.md"):
        (d / name).write_text("# " + name, encoding="utf-8")
    (d / "_meta.json").write_text(json.dumps({
        "fresh.md": _meta(summary="today's note", last_written="2026-08-19T09:00:00+00:00"),
        "another_fresh.md": _meta(summary="also today", last_written="2026-08-19T08:00:00+00:00"),
        "week_old.md": _meta(summary="a few days back", last_written="2026-08-14T09:00:00+00:00"),
        "ancient.md": _meta(summary="long ago", last_written="2026-05-01T09:00:00+00:00"),
    }, indent=2), encoding="utf-8")
    return d


class _FarFutureDatetime:
    """Stands in for `datetime` so that any use of the wall clock changes the output."""

    @staticmethod
    def now(tz=None):
        from datetime import datetime as _dt, timezone as _tz
        return _dt(2027, 12, 31, 23, 59, tzinfo=tz or _tz.utc)

    @staticmethod
    def fromisoformat(s):
        from datetime import datetime as _dt
        return _dt.fromisoformat(s)


class TestTheIndexDoesNotMoveWithTheClock:
    def test_two_generations_a_year_apart_are_byte_identical(self, knowledge_dir, monkeypatch):
        first = generate_smart_index(knowledge_dir)
        monkeypatch.setattr(memory_mod, "datetime", _FarFutureDatetime, raising=False)
        second = generate_smart_index(knowledge_dir)
        assert second == first
        assert (knowledge_dir / "_index.md").read_text(encoding="utf-8") == first

    def test_no_line_carries_a_day_count(self, knowledge_dir):
        """The section titles are fixed strings; what must not appear is a counter."""
        import re
        content = generate_smart_index(knowledge_dir)
        moving = re.search(r"last:\s*\d+\s*days", content)
        assert moving is None, "a relative day count moves without the knowledge moving"

    def test_an_old_file_is_dated_absolutely(self, knowledge_dir):
        content = generate_smart_index(knowledge_dir)
        assert "2026-05-01" in content


class TestReadingDoesNotRewriteTheIndex:
    def test_reading_a_document_leaves_the_index_alone(self, knowledge_dir):
        before = generate_smart_index(knowledge_dir)
        update_access(knowledge_dir, "ancient.md")
        after = (knowledge_dir / "_index.md").read_text(encoding="utf-8")
        assert after == before

    def test_reading_still_records_the_access(self, knowledge_dir):
        generate_smart_index(knowledge_dir)
        update_access(knowledge_dir, "ancient.md")
        meta = json.loads((knowledge_dir / "_meta.json").read_text(encoding="utf-8"))
        assert meta["ancient.md"]["access_count"] == 1
        assert meta["ancient.md"]["last_accessed"]

    def test_a_write_does_refresh_the_index(self, knowledge_dir):
        """A write is a real change to the knowledge, so the index may follow it."""
        before = generate_smart_index(knowledge_dir)
        (knowledge_dir / "brand_new.md").write_text("# new", encoding="utf-8")
        record_write(knowledge_dir, "brand_new.md")
        after = (knowledge_dir / "_index.md").read_text(encoding="utf-8")
        assert after != before
        assert "Brand New" in after


class TestTheOrderIsOwnedByTheIndex:
    def test_key_order_in_meta_does_not_change_the_index(self, knowledge_dir):
        before = generate_smart_index(knowledge_dir)
        meta = json.loads((knowledge_dir / "_meta.json").read_text(encoding="utf-8"))
        reversed_meta = dict(reversed(list(meta.items())))
        (knowledge_dir / "_meta.json").write_text(
            json.dumps(reversed_meta, indent=2), encoding="utf-8"
        )
        assert generate_smart_index(knowledge_dir) == before


class TestTheResidualCostIsKnown:
    """A read still reaches the index — later, through `last_touched`.

    Johnny's reading, checked and confirmed: `update_access` moves `last_accessed`,
    `last_touched` is the max of the two stamps, so the read is deferred into the
    next rebuild rather than removed. This test states that plainly, so the day
    someone decides the index should bucket on `last_written` alone, it fails and
    says why instead of quietly changing what agents see.
    """

    def test_a_read_changes_the_index_at_the_next_rebuild(self, knowledge_dir):
        before = generate_smart_index(knowledge_dir)
        update_access(knowledge_dir, "ancient.md")
        assert (knowledge_dir / "_index.md").read_text(encoding="utf-8") == before

        after = generate_smart_index(knowledge_dir)
        assert after != before, (
            "a read is expected to re-bucket the file at the next rebuild; if this "
            "passes, the index now ignores reads and the memory contract changed"
        )
        assert "Ancient" in after.split("## Active (today)")[1].split("##")[0]
