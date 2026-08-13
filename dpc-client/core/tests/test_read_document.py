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

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.tools import document as D

def _sample(name: str) -> Path:
    """Where a reference document might be on a node that is not this one.

    The papers live in one developer's Downloads folder, and the other nodes
    get them the way this project moves files at all — over a DPC chat, which
    lands them under the conversation's files directory. Looking in both
    places is the difference between these tests running on Linux and being
    skipped there forever.
    """
    home = Path(os.path.expanduser("~"))
    candidates = [home / "Downloads" / name, home / ".dpc" / "documents" / name]
    candidates += sorted((home / ".dpc" / "conversations").glob(f"*/files/{name}"))
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


MATH_PAPER = _sample("2510.13406v1.pdf")
SCANNED = _sample("0001202607260003.pdf")

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


def _read(ctx, path, pages=None, **kw):
    out = asyncio.run(D.read_document(ctx, str(path), pages, **kw))
    assert not out.startswith("⚠️"), out
    return json.loads(out)


class _Vision:
    """A stand-in for the local model: counts calls, answers what it is told to."""

    def __init__(self, answer="Транскрипция страницы", fail=False):
        self.answer, self.fail, self.calls = answer, fail, []

    async def query(self, prompt=None, provider_alias=None, images=None, return_metadata=False, **kw):
        self.calls.append({"alias": provider_alias, "images": len(images or [])})
        if self.fail:
            raise RuntimeError("model is on fire")
        return {"response": self.answer, "model": provider_alias or "test-vl"}


def _ctx_with_vision(ctx, vision, tmp_path):
    ctx.dpc_service = SimpleNamespace(llm_manager=vision)
    ctx.agent_root = tmp_path / "agent"
    return ctx


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
    assert "not supported yet" in asyncio.run(D.read_document(ctx, str(other)))


def test_a_denied_path_is_reported_not_raised(monkeypatch, tiny):
    def deny(ctx, p, require_write=False):
        raise PermissionError("Sandbox violation: nope")

    monkeypatch.setattr(D, "_resolve_file_path", deny)
    assert "Access denied" in asyncio.run(D.read_document(_Ctx(), str(tiny)))


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


def test_a_path_that_is_not_ascii_still_opens(ctx, tmp_path):
    """The Windows hazard, kept as a guard for every platform: a C library
    handed a path through a byte-oriented API loses non-ASCII names. The three
    nodes this runs on are Windows, Linux and macOS, and only one of them is
    ever tested by hand."""
    folder = tmp_path / "документы"
    folder.mkdir()
    target = folder / "статья с пробелом.pdf"
    target.write_bytes(TINY_PDF)
    assert "Hello document" in _read(ctx, target, "1")["per_page"][0]["text"]


def test_the_attribution_api_answers_on_this_build(tiny):
    """The detector's input comes out of the pdfium binary, not out of our
    code, and pdfium has renamed its font functions across majors before. This
    pins the call itself on whatever platform runs the suite — without it, a
    build where attribution silently stopped working would still be green,
    because every page would come back `detector: unavailable` and the flags
    would simply never fire.
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(tiny)
    textpage = doc[0].get_textpage()
    text = textpage.get_text_range()
    fonts = D._char_fonts(textpage)

    assert fonts is not None, "per-character font attribution is unavailable on this build"
    assert len(fonts) == len(text)
    assert set(fonts) == {"Helvetica"}


# ---------------------------------------------------------------- the eye

def test_the_model_is_never_called_for_a_page_that_has_text(ctx, tiny, tmp_path):
    """Auto means auto: a page with a text layer is read from the text layer,
    and a GPU shared with everything else on the machine stays free."""
    vision = _Vision()
    out = _read(_ctx_with_vision(ctx, vision, tmp_path), tiny, "1", mode="auto")
    assert vision.calls == []
    assert out["vision_pages"] == [] and out["vision_seconds"] == 0.0


def test_text_mode_refuses_the_eye_even_when_the_page_needs_it(ctx, tiny, tmp_path):
    vision = _Vision()
    out = _read(_ctx_with_vision(ctx, vision, tmp_path), tiny, "2", mode="text")
    assert vision.calls == []
    assert out["unreadable_pages"] == [2]


def test_a_page_read_by_the_model_says_it_was_a_transcription(ctx, tiny, tmp_path):
    vision = _Vision(answer="Статья 2. Цели настоящего Федерального закона")
    out = _read(_ctx_with_vision(ctx, vision, tmp_path), tiny, "2", mode="vision")
    page = out["per_page"][0]
    assert len(vision.calls) == 1 and vision.calls[0]["images"] == 1
    assert page["route"] == "vision"
    assert "Статья 2" in page["text"]
    assert "transcribed by a vision model" in page["note"]
    assert out["vision_pages"] == [2] and out["vision_seconds"] >= 0


def test_the_same_page_is_not_read_twice(ctx, tiny, tmp_path):
    """A 22-page scan is ten minutes of a shared GPU. Paying that twice for the
    same file is the cost this cache exists to refuse."""
    vision = _Vision()
    c = _ctx_with_vision(ctx, vision, tmp_path)
    first = _read(c, tiny, "2", mode="vision")["per_page"][0]
    second = _read(c, tiny, "2", mode="vision")["per_page"][0]
    assert len(vision.calls) == 1, "the second read went to the model again"
    assert second["cached"] is True and second["text"] == first["text"]


def test_the_cache_is_keyed_by_content_not_by_name(ctx, tiny, tmp_path):
    """The same document under two names is one document."""
    vision = _Vision()
    c = _ctx_with_vision(ctx, vision, tmp_path)
    _read(c, tiny, "2", mode="vision")
    twin = tmp_path / "renamed.pdf"
    twin.write_bytes(tiny.read_bytes())
    assert _read(c, twin, "2", mode="vision")["per_page"][0]["cached"] is True
    assert len(vision.calls) == 1


def test_over_the_cap_nothing_is_spent_and_the_price_is_quoted(ctx, tiny, tmp_path):
    """The refusal has to arrive before the cost, not after it — which is why
    every page is inventoried first."""
    vision = _Vision()
    out = _read(_ctx_with_vision(ctx, vision, tmp_path), tiny, "1-2", mode="vision",
                max_vision_pages=1)
    assert vision.calls == [], "pages were read despite the cap"
    assert out["vision_pages"] == []
    assert out["vision_pages_refused"] == [1, 2]
    assert any("would take roughly" in w or "not looked at" in w for w in out["warnings"])


def test_a_model_that_answers_with_nothing_leaves_the_page_unread(ctx, tiny, tmp_path):
    """The provider stopped passing reasoning off as an answer; the page must
    not now be reported as read with an empty body."""
    vision = _Vision(answer="   ")
    out = _read(_ctx_with_vision(ctx, vision, tmp_path), tiny, "2", mode="vision")
    page = out["per_page"][0]
    assert page["route"] != "vision"
    assert "unread, not empty" in page["note"]
    assert 2 in out["unreadable_pages"]


def test_a_model_that_raises_is_reported_and_the_call_survives(ctx, tiny, tmp_path):
    out = _read(_ctx_with_vision(ctx, _Vision(fail=True), tmp_path), tiny, "1-2", mode="vision")
    assert any("model is on fire" in (p.get("note") or "") for p in out["per_page"])
    assert "Hello document" in out["per_page"][0]["text"], "page 1's text survived"


def test_no_model_in_reach_is_said_rather_than_crashed(ctx, tiny):
    out = _read(ctx, tiny, "2", mode="vision")
    assert "no model is reachable" in out["per_page"][0]["note"]


def test_a_page_too_large_to_render_is_refused_before_it_is_rendered(ctx, tmp_path):
    """A MediaBox is whatever the document says it is. 200000 points square is
    173,611 megapixels at 150 dpi — the guard reads the size, not the bitmap."""
    huge = tmp_path / "huge.pdf"
    huge.write_bytes(TINY_PDF.replace(b"/MediaBox [0 0 200 200]", b"/MediaBox [0 0 200000 200000]"))
    vision = _Vision()
    out = _read(_ctx_with_vision(ctx, vision, tmp_path), huge, "2", mode="vision")
    assert vision.calls == [], "the model was handed a bitmap that should not exist"
    assert "megapixels" in out["per_page"][0]["note"]


def test_an_unknown_mode_reads_text_and_says_so(ctx, tiny, tmp_path):
    vision = _Vision()
    out = _read(_ctx_with_vision(ctx, vision, tmp_path), tiny, "2", mode="clairvoyance")
    assert vision.calls == []
    assert any("not one of auto/text/vision" in w for w in out["warnings"])


@pytest.mark.skipif(not SCANNED.exists(), reason="the reference scan is not on this machine")
def test_a_real_scan_routes_itself_to_the_eye(ctx, tmp_path):
    """The auto rule end to end on the document it was written for: no text
    layer plus an image is the page a model has to read."""
    vision = _Vision()
    out = _read(_ctx_with_vision(ctx, vision, tmp_path), SCANNED, "2", mode="auto")
    assert len(vision.calls) == 1
    assert out["vision_pages"] == [2]


def test_the_transcription_is_asked_for_at_temperature_zero(ctx, tiny, tmp_path):
    """Scenario Б is "copy it exactly". The alias this runs through carries the
    temperature its owner picked for describing pictures — 0.7 here — and a
    copy has no business being creative."""

    class _Recording(_Vision):
        async def query(self, prompt=None, provider_alias=None, images=None,
                        return_metadata=False, **kw):
            self.kwargs = kw
            return await super().query(prompt=prompt, provider_alias=provider_alias,
                                       images=images, return_metadata=return_metadata)

    vision = _Recording()
    _read(_ctx_with_vision(ctx, vision, tmp_path), tiny, "2", mode="vision")
    assert vision.kwargs.get("temperature") == 0


# ------------------------------------------------- fitting the answer's budget

def test_a_range_too_long_to_return_says_where_it_stopped(ctx, tmp_path, monkeypatch):
    """A tool result is cut at 15 000 characters by the agent loop, and the cut
    lands mid-JSON — the caller gets something unparseable. Observed 2026-08-14:
    an agent asked for twenty pages of an eighty-page document, the answer was
    20 364 characters, and five rounds went into working around the wreckage."""
    monkeypatch.setattr(D, "MAX_TEXT_CHARS", 5)
    out = _read(ctx, tiny_two_page(tmp_path), "1-2")

    assert out["pages_read"] == [1, 2]
    assert [p["page"] for p in out["per_page"]] == [1], "the budget was not applied"
    assert out["pages_omitted_for_size"] == [2]
    assert any("left out of this answer" in w for w in out["warnings"])
    assert len(json.dumps(out)) < 15000


def test_the_first_page_is_always_returned_however_long_it_is(ctx, tmp_path, monkeypatch):
    """Otherwise a document with one long page answers with nothing at all."""
    monkeypatch.setattr(D, "MAX_TEXT_CHARS", 1)
    out = _read(ctx, tiny_two_page(tmp_path), "1")
    assert [p["page"] for p in out["per_page"]] == [1]
    assert out["pages_omitted_for_size"] == []


def tiny_two_page(tmp_path) -> Path:
    p = tmp_path / "two.pdf"
    p.write_bytes(TINY_PDF)
    return p


# ------------------------------------------------------------- writing it out

def test_save_to_writes_the_pages_and_keeps_them_out_of_the_answer(ctx, tmp_path):
    """Eighty pages cannot be read into a conversation however it is split: the
    document has to land somewhere and be read from there."""
    target = tmp_path / "out" / "doc.md"
    out = _read(ctx, tiny_two_page(tmp_path), "1-2", save_to=str(target))

    assert out["saved_to"] == str(target)
    assert target.exists()
    written = target.read_text(encoding="utf-8")
    assert "Hello document" in written
    assert "## Page 1" in written and "## Page 2" in written
    assert all("text" not in p for p in out["per_page"]), "the text came back as well"
    assert out["per_page"][0]["chars"] == len("Hello document")


def test_save_to_appends_so_a_long_document_arrives_in_pieces(ctx, tmp_path):
    target = tmp_path / "doc.md"
    _read(ctx, tiny_two_page(tmp_path), "1", save_to=str(target))
    _read(ctx, tiny_two_page(tmp_path), "2", save_to=str(target))
    written = target.read_text(encoding="utf-8")
    assert written.count("## Page 1") == 1 and written.count("## Page 2") == 1


def test_a_refused_write_is_reported_and_the_reading_still_returns(ctx, tmp_path, monkeypatch):
    def deny(ctx_, path, require_write=False):
        if require_write:
            raise PermissionError("Sandbox violation: not writable")
        return Path(path)

    monkeypatch.setattr(D, "_resolve_file_path", deny)
    out = _read(ctx, tiny_two_page(tmp_path), "1", save_to="/etc/passwd")
    assert out["saved_to"] is None
    assert any("could not write" in w for w in out["warnings"])
    assert "Hello document" in out["per_page"][0]["text"], "the pages were lost with the write"


def test_the_answer_says_where_it_is_in_the_document(ctx, tmp_path):
    """Same shape read_file uses: what you have, out of how much, and the call
    that continues. An agent that cannot see the edge of what it received has no
    way to know it is missing anything."""
    out = _read(ctx, tiny_two_page(tmp_path), "1")
    assert out["position"].startswith("[Pages 1-1 of 2")
    assert "1 pages not read" in out["position"]
    assert "continue: pages='2-2'" in out["position"]

    whole = _read(ctx, tiny_two_page(tmp_path), "1-2")
    assert "whole document" in whole["position"]
    assert "continue" not in whole["position"]


def test_the_tool_says_out_loud_that_it_can_look_at_a_page():
    """Forge read an eighty-page PDF without ever learning the tool has eyes.
    The routes have to be in the first sentence, not the fourth."""
    (entry,) = D.get_tools()
    description = entry.schema["description"]
    head = description[:200].lower()
    assert "vision" in head, "an agent skimming the first line cannot tell"
    assert "save_to" in description
    assert "position" in description


def test_the_returned_range_has_no_hole_in_it(ctx, tmp_path, monkeypatch):
    """Filling the budget with whatever fits next produced pages 1-13 and 16 on
    the eighty-page document, plus a continue hint pointing at 14 — a range a
    reader cannot act on. It stops at the first page that does not fit."""
    monkeypatch.setattr(D, "MAX_TEXT_CHARS", 5)
    out = _read(ctx, tiny_two_page(tmp_path), "1-2")
    returned = [p["page"] for p in out["per_page"]]
    assert returned == list(range(returned[0], returned[-1] + 1))
    assert min(out["pages_omitted_for_size"]) > max(returned)


def test_what_was_written_is_what_the_position_reports(ctx, tmp_path):
    """With save_to the pages leave the answer, and the count of characters must
    follow them out — it read 0 chars while writing 16 KB in the first version."""
    target = tmp_path / "doc.md"
    out = _read(ctx, tiny_two_page(tmp_path), "1-2", save_to=str(target))
    assert "| 14 chars" in out["position"]


def _pages(*lengths):
    return [{"page": i + 1, "text": "x" * n} for i, n in enumerate(lengths)]


def test_the_budget_stops_at_the_first_page_that_does_not_fit(monkeypatch):
    """The distinguishing case the two-page fixture cannot show: a long page in
    the middle with a short one behind it. Skipping would return 1 and 3."""
    monkeypatch.setattr(D, "MAX_TEXT_CHARS", 100)
    kept, omitted, spent = D._fit_to_budget(_pages(50, 500, 10))
    assert [p["page"] for p in kept] == [1]
    assert omitted == [2, 3]
    assert spent == 50


def test_everything_that_fits_is_returned():
    kept, omitted, spent = D._fit_to_budget(_pages(10, 20, 30))
    assert [p["page"] for p in kept] == [1, 2, 3] and omitted == [] and spent == 60


def test_one_page_longer_than_the_whole_budget_still_comes_back(monkeypatch):
    monkeypatch.setattr(D, "MAX_TEXT_CHARS", 10)
    kept, omitted, _ = D._fit_to_budget(_pages(5000, 1))
    assert [p["page"] for p in kept] == [1] and omitted == [2]


def test_every_argument_the_tool_takes_is_one_the_model_can_pass():
    """The description promised save_to for two commits while the schema did not
    list it: prose the model can read, a parameter it cannot send. Same shape as
    the defects this file keeps finding — the capability exists and the consumer
    cannot reach it."""
    import inspect

    (entry,) = D.get_tools()
    advertised = set(entry.schema["parameters"]["properties"])
    accepted = {
        name
        for name, param in inspect.signature(entry.handler).parameters.items()
        if name != "ctx" and param.kind is not param.VAR_KEYWORD
    }
    assert accepted == advertised, (
        f"only in the signature: {accepted - advertised}; "
        f"only in the schema: {advertised - accepted}"
    )
