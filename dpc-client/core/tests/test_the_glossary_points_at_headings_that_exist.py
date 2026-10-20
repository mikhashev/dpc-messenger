"""Every «Defined in» link in docs/GLOSSARY.md lands on a file and a heading that exist.

The glossary points, it does not define: a row whose link resolves to nothing is a
claim of a source that is not there. build.py --check already reports such a row — but
only where backlog.md is, and that file is gitignored, so no clone and no CI run could
ever see the warning (Linus, 2026-09-05: «если переструктурируем документы, ссылки в
глоссарии сломаются?» — they would, silently). This test is the half that travels.

The walk is imported from `tools/backlog/glossary_check.py`, the way the commit-hook
test imports `tools/git-hooks/`: nothing else imports `tools/`, so this is its coverage.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "backlog"))

from glossary_check import check_glossary, slug  # noqa: E402

GLOSSARY = ROOT / "docs" / "GLOSSARY.md"
FIXTURE = ROOT / "tools" / "backlog" / "adr_fixture" / "docs" / "GLOSSARY.md"


def axes_declared_by_build_py():
    """The axis vocabulary is build.py's (`AXES = (...)`, one tuple); read it from the
    source rather than copying it — a second list here would be the drift the glossary
    exists to prevent. When ADR-039's amendment moves the list to VISION, move this."""
    src = (ROOT / "tools" / "backlog" / "build.py").read_text(encoding="utf-8")
    m = re.search(r'^AXES\s*=\s*\((.*?)\)', src, re.M)
    assert m, "build.py no longer declares AXES on one line — update this reader"
    return tuple(re.findall(r'"([a-z]+)"', m.group(1)))


class TestTheProjectsGlossary:
    def test_every_defined_in_link_resolves(self):
        terms, links, bad, _ = check_glossary(GLOSSARY, ())
        assert links > 0, "a glossary with no links has nothing to check"
        assert bad == [], "\n".join(bad)

    def test_every_axis_token_has_a_row(self):
        axes = axes_declared_by_build_py()
        assert len(axes) == 5, axes
        _, _, _, no_row = check_glossary(GLOSSARY, axes)
        assert no_row == [], "\n".join(no_row)


class TestTheWalkItself:
    def test_the_fixture_glossary_warns_exactly_twice(self):
        """One link to no file, one to no heading — the two shapes the check knows."""
        terms, links, bad, no_row = check_glossary(FIXTURE, axes_declared_by_build_py())
        assert len(terms) == 7
        assert [w.split("\n")[0].split("  ")[-1] for w in bad] == ["dangling", "wrong anchor"]
        assert no_row == []

    def test_a_link_to_a_missing_file_names_the_file(self, tmp_path):
        g = tmp_path / "GLOSSARY.md"
        g.write_text("| **word** | meaning | [gone](gone.md) |\n", encoding="utf-8")
        _, links, bad, _ = check_glossary(g)
        assert links == 1 and len(bad) == 1
        assert "GLOSSARY.md:1" in bad[0] and "gone.md" in bad[0]

    def test_a_link_to_a_heading_that_moved_names_the_anchor(self, tmp_path):
        (tmp_path / "owner.md").write_text("# Owner\n\n## Old name\n", encoding="utf-8")
        g = tmp_path / "GLOSSARY.md"
        g.write_text("| **word** | meaning | [owner](owner.md#new-name) |\n", encoding="utf-8")
        _, _, bad, _ = check_glossary(g)
        assert len(bad) == 1 and "#new-name" in bad[0]

    def test_a_link_that_lands_is_silent_and_a_url_is_not_followed(self, tmp_path):
        (tmp_path / "owner.md").write_text("## 4a. The one field — `axis:`\n", encoding="utf-8")
        g = tmp_path / "GLOSSARY.md"
        g.write_text("| **axis** | meaning | [owner](owner.md#4a-the-one-field--axis) "
                     "and [spec](https://example.com/spec#x) |\n", encoding="utf-8")
        _, links, bad, _ = check_glossary(g)
        assert links == 1 and bad == []

    def test_an_axis_token_without_a_row_is_reported(self, tmp_path):
        g = tmp_path / "GLOSSARY.md"
        g.write_text("| **reach** | meaning | [g](GLOSSARY.md) |\n", encoding="utf-8")
        _, _, _, no_row = check_glossary(g, ("reach", "honesty"))
        assert len(no_row) == 1 and "honesty" in no_row[0]

    def test_the_anchor_rule_is_githubs(self):
        assert slug("4a. The one field that was added anyway — `axis:`") == \
            "4a-the-one-field-that-was-added-anyway--axis"
        assert slug("Vocabulary — read the project's words before naming anything") == \
            "vocabulary--read-the-projects-words-before-naming-anything"


class TestThePreimageRowMatchesTheSigner:
    """The «message signing / preimage» row names the preimage the signer builds.

    It said ten fields tagged `dptp-msg-v1` for two versions after v1 was gone. The
    count is read from the bytes `message_preimage` returns rather than copied from its
    source, so the row goes red on the day a v4 appends a field.
    """

    NUMBER_WORDS = {9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen",
                    14: "fourteen", 15: "fifteen"}

    @staticmethod
    def _row():
        rows = [line for line in GLOSSARY.read_text(encoding="utf-8").splitlines()
                if line.startswith("| **message signing / preimage** |")]
        assert len(rows) == 1, f"expected one preimage row, found {len(rows)}"
        return rows[0]

    @staticmethod
    def _field_count(preimage: bytes) -> int:
        """Fields of a `len:bytes` concatenation (message_signing._length_prefixed)."""
        count, pos = 0, 0
        while pos < len(preimage):
            colon = preimage.index(b":", pos)
            pos = colon + 1 + int(preimage[pos:colon])
            count += 1
        assert pos == len(preimage), "the preimage is not length-prefixed fields end to end"
        return count

    def test_the_row_names_the_current_version_and_its_field_count(self):
        from dpc_protocol.message_signing import (
            LEGACY_PREIMAGE_VERSIONS, PREIMAGE_VERSION, message_preimage,
        )
        row = self._row()
        count = self._field_count(message_preimage(
            conversation_id="g", message_id="m", sender_node_id="n"))
        assert f"`{PREIMAGE_VERSION}`" in row, f"the row does not name {PREIMAGE_VERSION}: {row}"
        assert f"{self.NUMBER_WORDS[count]} fields" in row, (
            f"the signer builds {count} fields; the row says otherwise: {row}")
        for legacy in LEGACY_PREIMAGE_VERSIONS:
            assert f"`{legacy}`" in row, f"the row no longer says {legacy} still verifies"
