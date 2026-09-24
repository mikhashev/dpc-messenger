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
from dpc_client_core.dpc_agent.tools.process import SupervisedRun

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


# `_run_djvu` goes through `run_supervised`; the three tests above measure its
# own-failure paths against a real process. These fake `run_supervised` itself
# to cover the translation offline, including the one path a real subprocess
# cannot exercise without actually filling memory: the ceiling kill.

def test_run_djvu_names_read_document_as_its_launcher(monkeypatch):
    seen = {}

    def fake(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return SupervisedRun(returncode=0, stdout="1\n", stderr="")

    monkeypatch.setattr(D, "run_supervised", fake)
    D._run_djvu("djvused", ["book.djvu", "-e", "n"])
    assert seen["cmd"] == ["djvused", "book.djvu", "-e", "n"]
    assert seen["kwargs"]["launcher"] == "read_document"


def test_a_timed_out_run_is_reported_the_same_way_as_before(monkeypatch):
    monkeypatch.setattr(D, "DJVU_CALL_SECONDS", 30)
    monkeypatch.setattr(
        D, "run_supervised",
        lambda *a, **kw: SupervisedRun(timed_out=True, killed="the command and its process group were killed"),
    )
    rc, out, err = D._run_djvu("djvutxt", ["--page=1", "book.djvu"])
    assert rc == -1
    assert "djvutxt did not finish within 30 s" in err


def test_a_binary_run_supervised_cannot_spawn_is_reported_not_raised(monkeypatch):
    def boom(*a, **kw):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(D, "run_supervised", boom)
    rc, out, err = D._run_djvu("does-not-exist", ["-e", "n"])
    assert rc == -1 and "could not be run" in err


def test_a_tree_killed_by_the_memory_ceiling_names_the_ceiling_and_the_usage(monkeypatch):
    monkeypatch.setattr(D, "_MEMORY_CEILING_MB", 8192)
    monkeypatch.setattr(
        D, "run_supervised",
        lambda *a, **kw: SupervisedRun(
            exceeded_mb=8300, killed="the command and its descendants were killed"
        ),
    )
    rc, out, err = D._run_djvu("ddjvu", ["-format=pnm"])
    assert rc == -1
    assert "ddjvu killed by DPC memory ceiling (8192 MB)" in err
    assert "8300 MB" in err
    assert "descendants were killed" in err


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


# ------------------------------------------ a layer that is only a header

def test_a_header_only_layer_beside_raster_chunks_is_named_a_facsimile(ctx, book, libre):
    """The DjVu half of the PDF case: a running header over a scanned page."""
    header = "В ШТАБАХ ПОБЕДЫ\n"
    libre(_DjVuLibre(pages=3, text={1: header}, raster={1: 10}))
    out = _read(ctx, book, "1", mode="text")
    page = out["per_page"][0]

    assert page["route"] == "text", "the routing must not change"
    assert out["thin_layer_pages"] == [1]
    assert f"only {len(header)} characters of text layer beside 10 raster chunks" in page["note"]
    assert "likely a scan or facsimile" in page["note"]
    assert "mode='vision' reads it" in page["note"]
    assert any("facsimile" in w for w in out["warnings"])


def test_a_thin_djvu_page_with_no_raster_is_not_a_facsimile(ctx, book, libre):
    libre(_DjVuLibre(pages=2, text={1: "заголовок"}, raster={1: 0}))
    out = _read(ctx, book, "1", mode="text")
    assert out["thin_layer_pages"] == []
    assert "facsimile" not in (out["per_page"][0].get("note") or "")


def test_a_full_djvu_page_beside_raster_chunks_is_not_a_facsimile(ctx, book, libre):
    libre(_DjVuLibre(pages=2, text={1: "полный текст страницы " * 40}, raster={1: 10}))
    out = _read(ctx, book, "1", mode="text")
    assert out["per_page"][0]["chars"] >= D.THIN_TEXT_CHARS
    assert out["thin_layer_pages"] == []


def test_a_djvu_page_with_text_read_on_request_does_not_claim_it_had_none(
    ctx, book, libre, tmp_path
):
    layer = "заголовок тома"
    libre(_DjVuLibre(pages=2, text={1: layer}))
    vision = _Vision()
    ctx.dpc_service = SimpleNamespace(llm_manager=vision)
    ctx.agent_root = tmp_path / "agent"

    page = _read(ctx, book, "1", mode="vision")["per_page"][0]
    assert page["route"] == "vision"
    assert "has no text layer" not in page["note"]
    assert "on request (mode='vision')" in page["note"]
    assert f"its own text layer holds {len(layer)} characters" in page["note"]


# ------------------------------- a path DjVuLibre on Windows cannot open
#
# Tested live 2026-09-22 (ADR-043 R1a): DjVuLibre 3.5.29 on Windows opens no
# path with a non-ASCII component, and 356 of the 374 DjVu files in the
# owner's library sit under one. The repair is a hard link under an ASCII
# name, made for Windows only — so these tests say which platform they are
# about instead of asking the one they run on.


@pytest.fixture
def cyrillic_book(tmp_path):
    """The library's ordinary case: a Cyrillic directory and a Cyrillic name."""
    folder = tmp_path / "кириллица"
    folder.mkdir()
    path = folder / "Данилов Ставка.djvu"
    path.write_bytes(b"AT&TFORM\x00\x00\x00\x10DJVM")
    return path


@pytest.fixture
def staging_root(tmp_path, monkeypatch):
    """Where the link is put, said out loud rather than read off this machine:
    a test that trusted $TEMP to be ASCII would pass for the wrong reason."""
    root = tmp_path / "ascii-tmp"
    monkeypatch.setenv(D.ASCII_TMP_ENV, str(root))
    return root


def _documents_handed_over(calls):
    """Every path the binaries were given the document under."""
    return [
        arg
        for _name, args in calls
        for arg in args
        if arg.lower().endswith(D.DJVU_SUFFIXES)
    ]


def _watching(fake, seen):
    """The same DjVuLibre, recording what each path held while it ran — the
    staged file is gone by the time the assertions run, which is the point."""

    def run(binary, args):
        for arg in args:
            if arg.lower().endswith(D.DJVU_SUFFIXES):
                seen[arg] = Path(arg).read_bytes() if Path(arg).exists() else None
        return fake(binary, args)

    return run


def test_on_windows_a_cyrillic_path_reaches_the_binaries_as_ascii(
    ctx, cyrillic_book, libre, staging_root, monkeypatch
):
    monkeypatch.setattr(D.sys, "platform", "win32")
    fake = _DjVuLibre(pages=2, text={1: "ГОСУДАРСТВЕННЫЙ КОМИТЕТ ОБОРОНЫ"})
    libre(fake)

    out = _read(ctx, cyrillic_book, "1", mode="text")

    given = _documents_handed_over(fake.calls)
    assert given, "no binary was handed the document at all"
    assert all(path.isascii() for path in given), given
    assert str(cyrillic_book) not in given
    # What the caller reads is the path it asked about, not the link.
    assert out["path"] == str(cyrillic_book)
    assert "КОМИТЕТ" in out["per_page"][0]["text"]


def test_the_link_carries_the_document_and_is_gone_when_the_call_ends(
    ctx, cyrillic_book, libre, staging_root, monkeypatch
):
    monkeypatch.setattr(D.sys, "platform", "win32")
    seen = {}
    fake = _DjVuLibre(pages=2, text={1: "x"})
    libre(_watching(fake, seen))

    _read(ctx, cyrillic_book, "1", mode="text")

    (staged,) = set(_documents_handed_over(fake.calls))
    assert seen[staged] == cyrillic_book.read_bytes(), "the link held other bytes"
    assert not Path(staged).exists(), "the staged file outlived the call"
    assert list(staging_root.rglob("*")) == []
    assert cyrillic_book.exists(), "the source was touched"


def test_the_ascii_name_is_the_documents_own_digest(
    ctx, cyrillic_book, libre, staging_root, monkeypatch
):
    """The name is the content, so two calls on one document stage one link and
    the vision cache keyed by the same digest stays keyed by content."""
    monkeypatch.setattr(D.sys, "platform", "win32")
    fake = _DjVuLibre(pages=2, text={1: "x"})
    libre(fake)

    _read(ctx, cyrillic_book, "1", mode="text")

    (staged,) = set(_documents_handed_over(fake.calls))
    assert Path(staged).name == f"{D._file_digest(cyrillic_book)[:16]}.djvu"


def test_a_hard_link_that_cannot_be_made_becomes_a_copy(
    ctx, cyrillic_book, libre, staging_root, monkeypatch
):
    """A different volume, a filesystem with no links, a permission that allows
    reading and not linking: slower, and still the document."""
    monkeypatch.setattr(D.sys, "platform", "win32")
    attempts = []

    def no_links(source, target):
        attempts.append((source, target))
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(D.os, "link", no_links)
    seen = {}
    fake = _DjVuLibre(pages=2, text={1: "x"})
    libre(_watching(fake, seen))

    out = _read(ctx, cyrillic_book, "1", mode="text")

    assert len(attempts) == 1, "the link was not tried before the copy"
    (staged,) = set(_documents_handed_over(fake.calls))
    assert seen[staged] == cyrillic_book.read_bytes()
    assert not Path(staged).exists()
    assert out["per_page"][0]["text"] == "x"


def test_an_ascii_path_is_handed_over_untouched_on_windows(
    ctx, book, libre, staging_root, monkeypatch
):
    monkeypatch.setattr(D.sys, "platform", "win32")
    fake = _DjVuLibre(pages=2, text={1: "x"})
    libre(fake)

    _read(ctx, book, "1", mode="text")

    assert set(_documents_handed_over(fake.calls)) == {str(book)}
    assert not staging_root.exists(), "an ASCII path was staged anyway"


def test_off_windows_a_cyrillic_path_is_never_staged(
    ctx, cyrillic_book, libre, staging_root, monkeypatch
):
    """Linux and macOS open these paths today, and the repair must not change
    what they read or what they measure."""
    monkeypatch.setattr(D.sys, "platform", "linux")
    fake = _DjVuLibre(pages=2, text={1: "полный текст"})
    libre(fake)

    out = _read(ctx, cyrillic_book, "1", mode="text")

    assert set(_documents_handed_over(fake.calls)) == {str(cyrillic_book)}
    assert not staging_root.exists()
    assert out["per_page"][0]["text"] == "полный текст"


def test_a_page_read_through_the_link_is_cached_against_the_document(
    ctx, cyrillic_book, libre, staging_root, tmp_path, monkeypatch
):
    """The cache key is the content, not the path the binaries were given: a
    page read once on Windows is not read again on the same file elsewhere."""
    libre(_DjVuLibre(pages=2))
    vision = _Vision()
    ctx.dpc_service = SimpleNamespace(llm_manager=vision)
    ctx.agent_root = tmp_path / "agent"

    monkeypatch.setattr(D.sys, "platform", "win32")
    first = _read(ctx, cyrillic_book, "1", mode="vision")["per_page"][0]
    monkeypatch.setattr(D.sys, "platform", "linux")
    second = _read(ctx, cyrillic_book, "1", mode="vision")["per_page"][0]

    assert len(vision.calls) == 1, "the staged path made a second cache entry"
    assert second["cached"] is True and second["text"] == first["text"]


def test_with_no_ascii_directory_anywhere_the_answer_names_the_setting(
    ctx, cyrillic_book, libre, tmp_path, monkeypatch
):
    """A user whose profile is not ASCII has no temp directory that is; the
    answer has to name the one setting that repairs it."""
    monkeypatch.setattr(D.sys, "platform", "win32")
    libre(_DjVuLibre(pages=2, text={1: "x"}))
    monkeypatch.setenv(D.ASCII_TMP_ENV, str(tmp_path / "настройка"))
    monkeypatch.setattr(D.tempfile, "gettempdir", lambda: str(tmp_path / "время"))
    monkeypatch.setattr(D.Path, "home", staticmethod(lambda: tmp_path / "пользователь"))

    out = asyncio.run(D.read_document(ctx, str(cyrillic_book)))

    assert out.startswith("⚠️")
    assert D.ASCII_TMP_ENV in out
    assert "PDF reading is unaffected" in out
