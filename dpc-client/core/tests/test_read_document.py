"""What a document gave up, and what it did not.

The tool's whole claim is that a page says honestly what came out of it: text,
images the text route never looked at, and mathematics that a symbol font
turned into Latin letters. These tests hold that claim from both ends — the
flag fires where the poison is, and stays silent on prose.

Two real documents are used where a synthetic one would prove nothing: no
hand-written PDF carries a Computer Modern ToUnicode map that lies. Those
tests skip when the files are absent rather than pretending to pass.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.tools import document as D

MATH_PAPER = Path(os.path.expanduser("~/Downloads/2510.13406v1.pdf"))
SCANNED = Path(os.path.expanduser("~/Downloads/0001202607260003.pdf"))

# A two-page PDF written by hand: page 1 carries text, page 2 carries nothing.
# pdfium rebuilds the missing xref, which is what makes this short enough to
# read. Nothing here has a font whose map lies — that is what the real papers
# are for.
TINY_PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
4 0 obj << /Length 48 >> stream
BT /F1 12 Tf 20 100 Td (Hello document) Tj ET
endstream endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
6 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj
trailer << /Root 1 0 R /Size 7 >>
%%EOF"""


class _Ctx:
    """The tool only ever asks the context to resolve a path."""


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(D, "_resolve_file_path", lambda ctx, p, require_write=False: Path(p))
    return _Ctx()


@pytest.fixture
def tiny(tmp_path):
    p = tmp_path / "tiny.pdf"
    p.write_bytes(TINY_PDF)
    return p


def _read(ctx, path, pages=None):
    out = D.read_document(ctx, str(path), pages)
    assert not out.startswith("⚠️"), out
    return json.loads(out)


# --------------------------------------------------------------------- ranges

@pytest.mark.parametrize(
    "spec, expected",
    [
        ("2", [2]),
        ("1-2", [1, 2]),
        ("2,1", [1, 2]),          # ordered and deduplicated
        ("1-1", [1]),
    ],
)
def test_a_range_selects_exactly_those_pages(ctx, tiny, spec, expected):
    assert _read(ctx, tiny, spec)["pages_read"] == expected


def test_a_backwards_range_is_refused_out_loud(ctx, tiny):
    out = _read(ctx, tiny, "2-1")
    assert out["pages_read"] == []
    assert any("backwards" in w for w in out["warnings"])


def test_pages_the_document_does_not_have_are_named(ctx, tiny):
    """Silently returning fewer pages than asked for is the failure here."""
    out = _read(ctx, tiny, "1,7")
    assert out["pages_read"] == [1]
    assert any("outside this document" in w and "2" in w for w in out["warnings"])


def test_the_cap_says_what_it_dropped(ctx, tiny, monkeypatch):
    monkeypatch.setattr(D, "MAX_PAGES_PER_CALL", 1)
    out = _read(ctx, tiny, "1-2")
    assert out["pages_read"] == [1]
    assert any("call again" in w.lower() for w in out["warnings"])
    assert "continue_hint" in out


def test_no_range_reads_the_start_and_says_so(ctx, tiny, monkeypatch):
    monkeypatch.setattr(D, "DEFAULT_PAGES", 1)
    out = _read(ctx, tiny)
    assert out["pages_read"] == [1]
    assert any("no page range" in w for w in out["warnings"])


# ---------------------------------------------------------------- the reading

def test_text_comes_back_as_text(ctx, tiny):
    page = _read(ctx, tiny, "1")["per_page"][0]
    assert page["route"] == "text"
    assert "Hello document" in page["text"]
    assert page["chars"] == len(page["text"])


def test_a_page_with_no_text_is_marked_rather_than_returned_empty(ctx, tiny):
    out = _read(ctx, tiny, "2")
    page = out["per_page"][0]
    assert page["route"] == "no_text_layer"
    assert 2 in out["unreadable_pages"]
    assert "blank" in page["note"]


def test_the_envelope_says_the_content_is_untrusted(ctx, tiny):
    """A document can ask the agent for anything; the answer is that a document
    is data. Nothing else in the repository carries this field yet."""
    assert _read(ctx, tiny, "1")["untrusted_content"] is True


def test_a_broken_page_does_not_take_the_range_with_it(ctx):
    """The contract is partial failure: page 2 is damaged, pages 1 and 3 are not."""

    class _Doc:
        def __getitem__(self, index):
            if index == 1:
                raise RuntimeError("malformed page")
            raise AssertionError("only page 2 should have been asked for here")

    entry = D._read_page(_Doc(), 2)
    assert entry["route"] == "failed"
    assert "malformed page" in entry["error"]


def test_a_file_that_is_not_a_pdf_is_refused_by_name(ctx, tmp_path):
    other = tmp_path / "notes.docx"
    other.write_bytes(b"PK\x03\x04")
    assert "not supported yet" in D.read_document(ctx, str(other))


def test_a_denied_path_is_reported_not_raised(monkeypatch, tiny):
    def deny(ctx, p, require_write=False):
        raise PermissionError("Sandbox violation: nope")

    monkeypatch.setattr(D, "_resolve_file_path", deny)
    assert "Access denied" in D.read_document(_Ctx(), str(tiny))


# ------------------------------------------------------------- the detector

@pytest.mark.skipif(not MATH_PAPER.exists(), reason="the reference paper is not on this machine")
def test_the_detector_fires_on_the_page_whose_formulas_are_wrong(ctx):
    page = _read(ctx, MATH_PAPER, "3")["per_page"][0]
    assert page["suspect_chars"] > 0
    assert any(f.startswith(("CMSY", "TeX-matha")) for f in page["suspect_fonts"])
    assert "unreliable" in page["note"]


@pytest.mark.skipif(not MATH_PAPER.exists(), reason="the reference paper is not on this machine")
def test_the_detector_is_silent_on_prose(ctx):
    """The false-positive side, and the reason letter fonts are excluded: an
    earlier rule counted CMMI and reported 295 broken characters on a page
    where every one of them was a correctly extracted italic variable."""
    page = _read(ctx, MATH_PAPER, "1")["per_page"][0]
    assert page["suspect_chars"] == 0
    assert page["suspect_fonts"] == []


@pytest.mark.skipif(not MATH_PAPER.exists(), reason="the reference paper is not on this machine")
def test_nothing_is_repaired(ctx):
    """The design decision, held by a test: a flagged page comes back exactly
    as extracted. If anyone adds a substitution table, this goes red."""
    page = _read(ctx, MATH_PAPER, "3")["per_page"][0]
    assert "QJQ" in page["text"].replace(" ", "")
    assert "Q^\\top Q" not in page["text"]


@pytest.mark.skipif(not MATH_PAPER.exists(), reason="the reference paper is not on this machine")
def test_a_figure_on_a_readable_page_is_still_reported(ctx):
    """A page with good text and a load-bearing figure passes the text route
    silently otherwise, and the agent never learns it did not see the figure."""
    out = _read(ctx, MATH_PAPER, "7")
    assert out["figures_not_seen"] and out["figures_not_seen"][0]["page"] == 7
    assert out["per_page"][0]["images"] > 0


@pytest.mark.skipif(not SCANNED.exists(), reason="the reference scan is not on this machine")
def test_a_scan_is_reported_as_a_scan(ctx):
    out = _read(ctx, SCANNED, "2")
    page = out["per_page"][0]
    assert page["route"] == "no_text_layer"
    assert page["images"] >= 1
    assert "needs an eye" in page["note"]
    assert out["unreadable_pages"] == [2]


def test_the_detector_says_when_it_could_not_run(ctx, tiny, monkeypatch):
    """A pdfium that renames its font functions must cost the flags, not the
    reading — and the response has to admit the check did not happen."""
    monkeypatch.setattr(D, "_char_fonts", lambda tp: None)
    out = _read(ctx, tiny, "1")
    assert out["per_page"][0]["detector"] == "unavailable"
    assert any("did not run" in w for w in out["warnings"])
    assert "Hello document" in out["per_page"][0]["text"]


def test_font_attribution_that_does_not_line_up_says_so(ctx, tiny, monkeypatch):
    """Attribution is per index. If pdfium counts characters the extracted
    string does not carry, every index after the first mismatch points at the
    wrong glyph — and a count of zero would then read as "checked, clean" in
    the one case where nothing was checked."""
    status, count, fonts = D._suspect_characters("abc", ["CMSY10", "CMSY10"])
    assert status == "attribution_mismatch" and count == 0 and fonts == []

    monkeypatch.setattr(D, "_char_fonts", lambda tp: ["CMSY10"])
    page = _read(ctx, tiny, "1")["per_page"][0]
    assert page["detector"] == "attribution_mismatch"
    assert page["suspect_chars"] is None, "a page nobody checked is not a clean page"
    assert "did not run on this page" in page["note"]


def test_a_symbol_font_producing_a_latin_letter_is_the_thing_we_flag():
    status, n, fonts = D._suspect_characters("P∈x", ["CMSY10", "CMSY10", "CMR10"])
    assert status == "ran" and n == 1 and fonts == ["CMSY10"]


def test_a_letter_font_producing_a_letter_is_not():
    assert D._suspect_characters("xyz", ["CMMI10"] * 3) == ("ran", 0, [])


def test_an_uncountable_page_does_not_claim_there_is_no_figure(ctx, tiny, monkeypatch):
    """The same rule as the detector, one field over: nobody looked is not
    the same statement as nothing is there."""
    monkeypatch.setattr(D, "_page_fonts_and_images", lambda page: (set(), None))
    page = _read(ctx, tiny, "2")["per_page"][0]
    assert page["images"] is None
    assert "unknown" in page["note"]


def test_the_envelope_is_the_same_shape_when_no_page_could_be_read(ctx, tiny):
    """An agent that has to test for the presence of untrusted_content before
    trusting it has no guarantee at all."""
    out = _read(ctx, tiny, "9-8")
    assert out["pages_read"] == [] and out["per_page"] == []
    for field in ("sha256", "untrusted_content", "per_page", "unreadable_pages",
                  "pages_with_unreliable_math", "figures_not_seen", "elapsed_sec"):
        assert field in out, field


def test_the_digest_matches_the_file(tiny):
    import hashlib
    assert D._file_digest(tiny) == hashlib.sha256(tiny.read_bytes()).hexdigest()


def test_an_inventory_that_raises_returns_no_count_rather_than_zero():
    """The half the monkeypatched test above cannot see: what the inventory
    itself does when pdfium refuses to enumerate a page's objects."""

    class _Page:
        def get_objects(self):
            raise RuntimeError("pdfium says no")

    fonts, images = D._page_fonts_and_images(_Page())
    assert images is None and fonts == set()
