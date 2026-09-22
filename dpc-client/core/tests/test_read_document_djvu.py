"""What a DjVu page gave up, in the same words a PDF page uses.

The DjVu route is three command-line binaries, so the thing under test is the
seam between this tool and them: what it asks them for, what it makes of what
comes back, and what it says when they are not there at all. `_run_djvu` is
replaced by a DjVuLibre that lives in this process, which is why these tests
run on a node with no DjVuLibre installed — the shape of the answer is the
claim, not the vendor's parser.

The two things not faked are the ones that would otherwise be assumed: the
timeout is measured against a real subprocess, and the render goes through a
real PNM and a real Pillow, because a page handed to a vision model as a
mislabelled bitmap is exactly the failure the media type is there to prevent.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.tools import document as D

# A 4x4 grey square, which is all Pillow needs to prove the conversion ran.
TINY_PNM = b"P5\n4 4\n255\n" + bytes(16)


class _Ctx:
    """The tool only ever asks the context to resolve a path."""


class _DjVuLibre:
    """DjVuLibre in this process: answers the three binaries, counts the calls."""

    def __init__(self, pages=3, text=None, raster=None, dpi=600, size=(2808, 4386),
                 failures=None):
        self.pages = pages
        self.text = text or {}
        self.raster = raster or {}
        self.dpi = dpi
        self.size = size
        self.failures = failures or {}      # binary name -> (rc, stderr)
        self.calls = []

    def __call__(self, binary, args):
        name = Path(binary).stem
        self.calls.append((name, list(args)))
        if name in self.failures:
            rc, err = self.failures[name]
            return rc, "", err
        if name == "djvused":
            return self._djvused(args)
        if name == "djvutxt":
            page = int(next(a for a in args if a.startswith("--page=")).split("=")[1])
            return 0, self.text.get(page, ""), ""
        if name == "ddjvu":
            Path(args[-1]).write_bytes(TINY_PNM)
            return 0, "", ""
        raise AssertionError(f"unexpected binary {name}")

    def _djvused(self, args):
        script = args[args.index("-e") + 1].strip()
        if script == "n":
            return 0, f"{self.pages}\n", ""
        page = int(re.match(r"select (\d+); dump", script).group(1))
        width, height = self.size
        lines = [
            "  FORM:DJVU [8239] ",
            f"    INFO [10]         DjVu {width}x{height}, v25, {self.dpi} dpi, gamma=2.2",
        ]
        lines += ["    Sjbz [7815]      JB2 bilevel data"] * self.raster.get(page, 1)
        if self.text.get(page):
            lines.append("    TXTz [367]      Hidden text (text, etc.)")
        return 0, "\n".join(lines) + "\n", ""


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(D, "_resolve_file_path", lambda ctx, p, require_write=False: Path(p))
    return _Ctx()


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "book.djvu"
    path.write_bytes(b"AT&TFORM\x00\x00\x00\x10DJVM")
    return path


@pytest.fixture
def libre(monkeypatch):
    """A DjVuLibre that is found and answers in process."""
    monkeypatch.setattr(
        D, "_djvulibre",
        lambda: {name: f"/usr/bin/{name}" for name in D.DJVU_TOOLS},
    )

    def install(fake):
        monkeypatch.setattr(D, "_run_djvu", fake)
        return fake

    return install


def _read(ctx, path, pages=None, **kw):
    out = asyncio.run(D.read_document(ctx, str(path), pages, **kw))
    assert not out.startswith("⚠️"), out
    return json.loads(out)


class _Vision:
    """A stand-in for the local model: records what it was handed."""

    def __init__(self, answer="Транскрипция страницы"):
        self.answer, self.calls = answer, []

    async def query(self, prompt=None, provider_alias=None, images=None,
                    return_metadata=False, **kw):
        self.calls.append(images)
        return {"response": self.answer, "model": provider_alias or "test-vl"}


# ------------------------------------------------------- finding the binaries

def test_without_djvulibre_the_answer_is_the_install_line(ctx, book, monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda name: None)
    monkeypatch.setattr(D, "WINDOWS_DJVULIBRE_DIRS", ())
    monkeypatch.delenv(D.DJVULIBRE_DIR_ENV, raising=False)

    out = asyncio.run(D.read_document(ctx, str(book)))
    assert "winget install DjVuLibre.DjView" in out
    assert "apt install djvulibre-bin" in out
    assert "brew install djvulibre" in out
    assert "PDF reading is unaffected" in out


def test_two_binaries_out_of_three_is_not_an_installation(monkeypatch, tmp_path):
    """Half a DjVuLibre fails in the middle of a page range instead of at the
    door, which is the failure this refuses to start."""
    monkeypatch.setattr(D.shutil, "which", lambda name: None)
    monkeypatch.setattr(D, "WINDOWS_DJVULIBRE_DIRS", ())
    for name in ("djvutxt", "djvused"):
        (tmp_path / name).write_text("#!/bin/sh\n")
    monkeypatch.setenv(D.DJVULIBRE_DIR_ENV, str(tmp_path))
    assert D._djvulibre() is None


def test_the_env_var_points_at_an_install_that_is_not_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(D.shutil, "which", lambda name: None)
    monkeypatch.setattr(D, "WINDOWS_DJVULIBRE_DIRS", ())
    for name in D.DJVU_TOOLS:
        (tmp_path / name).write_text("#!/bin/sh\n")
    monkeypatch.setenv(D.DJVULIBRE_DIR_ENV, str(tmp_path))

    found = D._djvulibre()
    assert set(found) == set(D.DJVU_TOOLS)
    assert all(str(tmp_path) in path for path in found.values())


def test_the_env_var_wins_over_path(monkeypatch, tmp_path):
    """An explicit setting beats discovery: an old djvulibre on PATH and a
    newer one installed elsewhere is exactly the machine this is for."""
    monkeypatch.setattr(D.shutil, "which", lambda name: f"/usr/bin/{name}")
    for name in D.DJVU_TOOLS:
        (tmp_path / name).write_text("#!/bin/sh\n")
    monkeypatch.setenv(D.DJVULIBRE_DIR_ENV, str(tmp_path))

    found = D._djvulibre()
    assert all(str(tmp_path) in path for path in found.values())
    assert "/usr/bin" not in found["ddjvu"]


# ----------------------------------------------------------- the subprocesses

def test_a_binary_that_never_returns_is_cut_off_and_says_so(monkeypatch):
    """The tool runs in an executor thread; a djvu binary that hangs would hold
    it for the life of the process."""
    monkeypatch.setattr(D, "DJVU_CALL_SECONDS", 1)
    rc, out, err = D._run_djvu(sys.executable, ["-c", "import time; time.sleep(30)"])
    assert rc == -1
    assert "did not finish within 1 s" in err


def test_a_binary_that_is_not_there_is_reported_not_raised(monkeypatch):
    rc, out, err = D._run_djvu(str(Path("does-not-exist-here")), ["-e", "n"])
    assert rc == -1 and "could not be run" in err


def test_output_that_is_not_utf8_is_replaced_not_raised():
    rc, out, err = D._run_djvu(
        sys.executable,
        ["-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe text')"],
    )
    assert rc == 0 and "text" in out and "�" in out


# ------------------------------------------------------------- the text route

def test_a_page_with_a_text_layer_and_one_without_are_told_apart(ctx, book, libre):
    libre(_DjVuLibre(pages=3, text={1: "ГОСУДАРСТВЕННЫЙ КОМИТЕТ ОБОРОНЫ\n"}))
    out = _read(ctx, book, "1-2", mode="text")

    first, second = out["per_page"]
    assert first["route"] == "text"
    assert first["chars"] == len("ГОСУДАРСТВЕННЫЙ КОМИТЕТ ОБОРОНЫ\n")
    assert "КОМИТЕТ" in first["text"]
    assert second["route"] == "no_text_layer"
    assert second["chars"] == 0
    assert "scan" in second["note"]
    assert out["unreadable_pages"] == [2]
    assert out["format"] == "djvu"


def test_a_page_with_neither_text_nor_raster_is_called_blank(ctx, book, libre):
    libre(_DjVuLibre(pages=2, raster={1: 0}))
    page = _read(ctx, book, "1")["per_page"][0]
    assert page["route"] == "no_text_layer"
    assert page["images"] == 0
    assert "genuinely blank" in page["note"]


def test_a_page_whose_structure_could_not_be_read_says_unknown_not_zero(ctx, book, libre, monkeypatch):
    fake = _DjVuLibre(pages=2)
    original = fake.__call__

    def broken(binary, args):
        if Path(binary).stem == "djvused" and "dump" in args[-1]:
            return 1, "", "*** [1-11711] page directory is damaged\n"
        return original(binary, args)

    libre(broken)
    page = _read(ctx, book, "1")["per_page"][0]
    assert page["images"] is None
    assert "unknown" in page["note"]


def test_the_page_count_comes_from_djvused(ctx, book, libre):
    fake = libre(_DjVuLibre(pages=434, text={1: "x"}))
    out = _read(ctx, book, "1")
    assert out["pages_total"] == 434
    assert ("djvused", [str(book), "-e", "n"]) in fake.calls


def test_a_file_djvused_cannot_open_is_refused_by_name(ctx, book, libre):
    libre(_DjVuLibre(failures={"djvused": (10, "*** Failed to open: bad file.\n")}))
    out = asyncio.run(D.read_document(ctx, str(book)))
    assert out.startswith("⚠️ Could not open 'book.djvu'")
    assert "Failed to open" in out


def test_a_page_djvutxt_fails_on_does_not_take_the_range_with_it(ctx, book, libre):
    fake = _DjVuLibre(pages=3, text={1: "page one", 3: "page three"})
    original = fake.__call__

    def broken(binary, args):
        if Path(binary).stem == "djvutxt" and "--page=2" in args:
            return 1, "", "*** [1-11711] JB2 data is corrupt\n"
        return original(binary, args)

    libre(broken)
    out = _read(ctx, book, "1-3")
    routes = {p["page"]: p["route"] for p in out["per_page"]}
    assert routes == {1: "text", 2: "failed", 3: "text"}
    assert "JB2 data is corrupt" in out["per_page"][1]["error"]


def test_the_mathematics_check_is_reported_as_not_run_rather_than_clean(ctx, book, libre):
    """Zero suspect characters reads as "checked, clean" in the one case where
    nothing was checked at all — a DjVu text layer has no fonts to check."""
    libre(_DjVuLibre(pages=2, text={1: "prose"}))
    out = _read(ctx, book, "1")
    page = out["per_page"][0]
    assert page["detector"] == "no_font_information"
    assert page["suspect_chars"] is None
    assert out["pages_with_unreliable_math"] == []
    assert any("no font information" in w for w in out["warnings"])


# --------------------------------------------------- ranges, budget, save_to

@pytest.mark.parametrize("spec, expected", [("2", [2]), ("1-3", [1, 2, 3]), ("3,1", [1, 3])])
def test_a_range_selects_exactly_those_pages(ctx, book, libre, spec, expected):
    libre(_DjVuLibre(pages=5, text={n: f"page {n}" for n in range(1, 6)}))
    assert _read(ctx, book, spec)["pages_read"] == expected


def test_a_range_beyond_the_document_is_clamped_and_said(ctx, book, libre):
    libre(_DjVuLibre(pages=3, text={3: "last"}))
    out = _read(ctx, book, "2-9")
    assert out["pages_read"] == [2, 3]
    assert any("outside this document" in w for w in out["warnings"])


def test_no_range_reads_the_first_pages_and_says_how_many_are_left(ctx, book, libre):
    libre(_DjVuLibre(pages=434, text={n: f"page {n}" for n in range(1, 40)}))
    out = _read(ctx, book)
    assert out["pages_read"] == list(range(1, D.DEFAULT_PAGES + 1))
    assert "424 pages not read" in out["position"]
    assert out["continue_hint"].endswith("page 11")


def test_over_twenty_pages_is_cut_to_twenty(ctx, book, libre):
    libre(_DjVuLibre(pages=434, text={n: "t" for n in range(1, 40)}))
    out = _read(ctx, book, "1-30")
    assert len(out["pages_read"]) == D.MAX_PAGES_PER_CALL
    assert any("Call again with the rest" in w for w in out["warnings"])


def test_a_range_too_long_to_return_says_where_it_stopped(ctx, book, libre, monkeypatch):
    monkeypatch.setattr(D, "MAX_TEXT_CHARS", 5)
    libre(_DjVuLibre(pages=3, text={1: "a" * 20, 2: "b" * 20}))
    out = _read(ctx, book, "1-2")
    assert out["pages_read"] == [1, 2]
    assert [p["page"] for p in out["per_page"]] == [1]
    assert out["pages_omitted_for_size"] == [2]
    assert any("left out of this answer" in w for w in out["warnings"])


def test_save_to_writes_the_pages_and_keeps_them_out_of_the_answer(ctx, book, libre, tmp_path):
    libre(_DjVuLibre(pages=3, text={1: "первая страница", 2: "вторая страница"}))
    target = tmp_path / "out" / "book.md"
    out = _read(ctx, book, "1-2", save_to=str(target))

    assert out["saved_to"] == str(target)
    written = target.read_text(encoding="utf-8")
    assert "первая страница" in written and "вторая страница" in written
    assert all("text" not in page for page in out["per_page"])


# --------------------------------------------------------------- the eye

def test_a_scanned_page_is_rendered_through_ddjvu_and_handed_over_as_png(
    ctx, book, libre, tmp_path
):
    """The bytes reach the model as a PNG with PNG's media type on it: a page
    labelled as something it is not is the failure this route has to avoid."""
    fake = libre(_DjVuLibre(pages=2))
    vision = _Vision()
    ctx.dpc_service = SimpleNamespace(llm_manager=vision)
    ctx.agent_root = tmp_path / "agent"

    out = _read(ctx, book, "1", mode="auto")

    assert out["vision_pages"] == [1]
    assert out["per_page"][0]["text"] == "Транскрипция страницы"
    image = vision.calls[0][0]
    assert image["mime_type"] == "image/png"
    assert __import__("base64").b64decode(image["base64"]).startswith(b"\x89PNG")
    ddjvu = [args for name, args in fake.calls if name == "ddjvu"][0]
    assert "-format=pnm" in ddjvu and "--page=1" not in ddjvu
    assert f"-scale={D.VISION_DPI}" in ddjvu and "-page=1" in ddjvu


def test_a_page_too_large_to_render_is_refused_before_ddjvu_runs(ctx, book, libre, tmp_path):
    """ddjvu's -scale is a dpi against the page's own resolution, so the size
    is known before any bitmap exists."""
    fake = libre(_DjVuLibre(pages=2, dpi=600, size=(100_000, 100_000)))
    vision = _Vision()
    ctx.dpc_service = SimpleNamespace(llm_manager=vision)
    ctx.agent_root = tmp_path / "agent"

    out = _read(ctx, book, "1", mode="vision")
    assert vision.calls == []
    assert [name for name, _ in fake.calls if name == "ddjvu"] == []
    assert "megapixels" in out["per_page"][0]["note"]


def test_a_page_read_once_is_not_read_again(ctx, book, libre, tmp_path):
    libre(_DjVuLibre(pages=2))
    vision = _Vision()
    ctx.dpc_service = SimpleNamespace(llm_manager=vision)
    ctx.agent_root = tmp_path / "agent"

    first = _read(ctx, book, "1", mode="vision")["per_page"][0]
    second = _read(ctx, book, "1", mode="vision")["per_page"][0]
    assert len(vision.calls) == 1
    assert second["cached"] is True and second["text"] == first["text"]


def test_over_the_cap_nothing_is_spent_and_the_price_is_quoted(ctx, book, libre, tmp_path):
    fake = libre(_DjVuLibre(pages=3))
    vision = _Vision()
    ctx.dpc_service = SimpleNamespace(llm_manager=vision)
    ctx.agent_root = tmp_path / "agent"

    out = _read(ctx, book, "1-3", mode="auto", max_vision_pages=1)
    assert vision.calls == []
    assert [name for name, _ in fake.calls if name == "ddjvu"] == []
    assert out["vision_pages_refused"] == [1, 2, 3]


def test_mode_text_never_renders_a_scan(ctx, book, libre, tmp_path):
    fake = libre(_DjVuLibre(pages=2))
    vision = _Vision()
    ctx.dpc_service = SimpleNamespace(llm_manager=vision)
    ctx.agent_root = tmp_path / "agent"

    out = _read(ctx, book, "1", mode="text")
    assert vision.calls == []
    assert [name for name, _ in fake.calls if name == "ddjvu"] == []
    assert out["per_page"][0]["route"] == "no_text_layer"


# ------------------------------------------------------------ the other suffixes

def test_a_djvu_no_longer_hits_the_unsupported_branch(ctx, book, libre):
    libre(_DjVuLibre(pages=1, text={1: "x"}))
    assert "not supported yet" not in asyncio.run(D.read_document(ctx, str(book)))


def test_the_single_page_djv_suffix_is_read_too(ctx, tmp_path, libre):
    libre(_DjVuLibre(pages=1, text={1: "x"}))
    single = tmp_path / "page.djv"
    single.write_bytes(b"AT&TFORM")
    assert _read(ctx, single, "1")["format"] == "djvu"


def test_a_file_that_is_neither_pdf_nor_djvu_is_still_refused_by_name(ctx, tmp_path):
    other = tmp_path / "notes.docx"
    other.write_bytes(b"PK\x03\x04")
    out = asyncio.run(D.read_document(ctx, str(other)))
    assert "not supported yet" in out
    assert "PDF and DjVu" in out


def test_no_pdf_reader_is_imported_for_a_djvu(ctx, book, libre, monkeypatch):
    """pypdfium2 is an optional extra; a DjVu node need not have it."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def refuse(name, *args, **kw):
        if name.startswith("pypdfium2"):
            raise AssertionError("the PDF reader was imported for a DjVu file")
        return real_import(name, *args, **kw)

    libre(_DjVuLibre(pages=2, text={1: "x"}))
    monkeypatch.setattr("builtins.__import__", refuse)
    assert _read(ctx, book, "1")["format"] == "djvu"
