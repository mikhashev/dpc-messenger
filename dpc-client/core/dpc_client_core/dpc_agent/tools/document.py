"""
DPC Agent — document reading (PDF).

`read_file` decodes every path as UTF-8 with errors="replace", so a paper
handed to an agent came back as replacement characters — and came back as a
*string*, so nothing reported a failure. This tool reads the text layer of a
PDF instead, page by page, and says per page what it got and what it did not.

What it deliberately does **not** do:

- **repair mathematics.** Measured on four TeX documents 2026-08-13: the
  characters a math font puts in the text layer are wrong in several font
  families at once (CMSY in four documents out of four, CMEX, MSBM, TeX-matha),
  the same font is right in one place and wrong in another, and one document
  emits 27 NUL bytes that carry nothing to repair. A substitution table would
  therefore be both endless and a lie on prose (`P` really is `P` most of the
  time). It detects and flags instead: an honest gap beats a plausible repair.
- **render or call a model.** No vision, no GPU, no network in this tool. The
  scan route and the arXiv-HTML route are separate steps, and the second one
  is a separate tool because a file reader that quietly reaches the network
  would misrepresent what the firewall panel promises about it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .core import _resolve_file_path
from .registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

# One call reads at most this many pages, whatever the range says. The range is
# the interface; this is the backstop that keeps a 2000-page file from being
# pulled whole by a loop nobody is watching.
MAX_PAGES_PER_CALL = 20
DEFAULT_PAGES = 10

# Fonts whose glyphs have no Latin meaning at all: symbol, extension, blackboard
# and the mathabx family. A Latin letter or an ASCII punctuation mark coming out
# of one of these is a lie by construction — the font's ToUnicode map is wrong.
# The letter fonts (CMMI math italic, CMR roman) are deliberately absent: a math
# italic x extracting as "x" is correct, and counting those flagged 295
# characters on one page that were never broken.
SYMBOL_FONT_MARKERS = (
    "CMSY", "CMEX", "MSAM", "MSBM", "TeX-math", "wasy", "stmary",
    "rsfs", "eufm", "esint", "mathx", "matha",
)

# Below this codepoint a character from a symbol font is suspect. Everything a
# real mathematical symbol needs lives above it.
SUSPECT_BELOW = 0x2000


def _parse_pages(spec: Optional[str], total: int) -> Tuple[List[int], List[str]]:
    """Turn "2", "1-5", "2,7,9-12" into page numbers, with what was dropped.

    Returns 1-based page numbers, clamped to the document, deduplicated and
    ordered. The second element is the list of complaints — a range that names
    pages the document does not have is worth saying out loud rather than
    silently returning fewer pages than asked for.
    """
    notes: List[str] = []
    if not spec or not spec.strip():
        wanted = list(range(1, min(DEFAULT_PAGES, total) + 1))
        if total > len(wanted):
            notes.append(
                f"no page range given: read the first {len(wanted)} of {total} pages"
            )
        return wanted, notes

    out: List[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        try:
            if "-" in part.lstrip("-"):
                lo_s, hi_s = part.split("-", 1)
                lo, hi = int(lo_s), int(hi_s)
                if lo > hi:
                    notes.append(f"range '{part}' runs backwards and was skipped")
                    continue
                out.extend(range(lo, hi + 1))
            else:
                out.append(int(part))
        except ValueError:
            notes.append(f"'{part}' is not a page or a range and was skipped")

    kept = sorted({p for p in out if 1 <= p <= total})
    dropped = sorted({p for p in out if p < 1 or p > total})
    if dropped:
        notes.append(
            f"pages {dropped} are outside this document, which has {total}"
        )
    if len(kept) > MAX_PAGES_PER_CALL:
        notes.append(
            f"{len(kept)} pages requested; reading the first {MAX_PAGES_PER_CALL}. "
            f"Call again with the rest."
        )
        kept = kept[:MAX_PAGES_PER_CALL]
    return kept, notes


def _file_digest(path: Path) -> str:
    """SHA-256 read a megabyte at a time.

    The whole point of the page range is that a 2000-page document is never
    pulled in whole; reading it whole to hash it would give that back at the
    one moment nobody is looking.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _char_fonts(textpage) -> Optional[List[str]]:
    """Font name per character, or None if this pdfium cannot say.

    pdfium has renamed its font functions across major versions, and a reader
    that stops working because the detector cannot run would be a worse trade
    than a reader with no detector. So the caller degrades instead: the text
    still comes back, and the response says the detector was unavailable.
    """
    try:
        import ctypes

        import pypdfium2.raw as C

        raw = textpage.raw
        count = C.FPDFText_CountChars(raw)
        buf = ctypes.create_string_buffer(256)
        flags = ctypes.c_int()
        names = []
        for i in range(count):
            n = C.FPDFText_GetFontInfo(raw, i, buf, 256, ctypes.byref(flags))
            names.append(buf.raw[: max(0, n)].decode("utf-8", "ignore").rstrip("\x00"))
        return names
    except Exception as exc:  # pragma: no cover - depends on the pdfium build
        log.debug("per-character font attribution unavailable: %s", exc)
        return None


def _suspect_characters(
    text: str, fonts: Optional[List[str]]
) -> Tuple[str, int, List[str]]:
    """What the check found, as (status, count, fonts).

    Three statuses, and the third one is the point. `ran` means the page was
    checked. `unavailable` means this pdfium could not attribute characters at
    all. `attribution_mismatch` means it could, but the two sequences do not
    line up — pdfium counted characters the extracted string does not carry (a
    hyphenation artefact, a generated space), and since attribution is per
    index, every index past the first mismatch points at the wrong glyph.

    A count of zero would be the natural thing to return for the last two, and
    it is exactly wrong: zero suspect characters reads as "checked, clean" in
    the one case where nothing was checked at all.
    """
    if fonts is None:
        return "unavailable", 0, []
    if len(fonts) != len(text):
        return "attribution_mismatch", 0, []
    bad: Dict[str, int] = {}
    for ch, font in zip(text, fonts):
        cp = ord(ch)
        if cp < 0x20 and ch not in "\t\n\r":
            bad[font or "?"] = bad.get(font or "?", 0) + 1
            continue
        if cp >= SUSPECT_BELOW or ch.isspace():
            continue
        if any(m in font for m in SYMBOL_FONT_MARKERS):
            bad[font] = bad.get(font, 0) + 1
    return "ran", sum(bad.values()), sorted(bad)


def _page_fonts_and_images(page) -> Tuple[Set[str], Optional[int]]:
    """Font base names and the image count, or None when nothing could be counted.

    None rather than 0 for the same reason the check above has three states: a
    page reported as carrying no images, when in truth nobody could look, is
    how "there is no figure here" gets said about a figure.
    """
    fonts: Set[str] = set()
    images = 0
    try:
        objects = list(page.get_objects() or [])
    except Exception as exc:  # pragma: no cover
        log.debug("page object inventory unavailable: %s", exc)
        return fonts, None
    for obj in objects:
        try:
            if int(obj.type) == 3:  # FPDF_PAGEOBJ_IMAGE
                images += 1
                continue
            font = getattr(obj, "get_font", None)
            if font is None:
                continue
            f = obj.get_font()
            if f is not None:
                fonts.add(f.get_base_name())
        except Exception:
            continue
    return fonts, images


def _read_page(doc, number: int) -> Dict[str, Any]:
    """One page, and never an exception: a broken page is a marked page.

    A malformed page in the middle of a range must not take the pages around
    it with it — the caller asked for a range, and returning nothing because
    page 3 is damaged loses pages 2 and 4 for no reason.
    """
    entry: Dict[str, Any] = {"page": number, "route": "text"}
    try:
        page = doc[number - 1]
        textpage = page.get_textpage()
        text = textpage.get_text_range() or ""
        fonts, images = _page_fonts_and_images(page)
        char_fonts = _char_fonts(textpage)
        detector, suspect, suspect_fonts = _suspect_characters(text, char_fonts)

        entry.update(
            chars=len(text),
            images=images,
            fonts=sorted(fonts)[:12],
            suspect_chars=suspect if detector == "ran" else None,
            suspect_fonts=suspect_fonts,
            detector=detector,
            text=text,
        )
        if not text.strip():
            entry["route"] = "no_text_layer"
            if images is None:
                tail = "; and this page's objects could not be counted, so whether it carries an image is unknown"
            elif images:
                tail = f"; it carries {images} image object(s), so it is a scan and needs an eye"
            else:
                tail = "; and no image either — the page is genuinely blank"
            entry["note"] = "no text layer on this page" + tail
        elif detector == "attribution_mismatch":
            entry["note"] = (
                "the mathematics check did not run on this page: pdfium counted "
                f"{len(char_fonts or [])} characters where the text carries {len(text)}, "
                "so per-character font attribution could not be trusted. Unreliable "
                "formulas here would not have been flagged"
            )
        elif suspect:
            entry["note"] = (
                f"{suspect} characters came out of {', '.join(suspect_fonts)}, whose "
                f"glyphs have no Latin meaning — the formulas on this page are unreliable "
                f"and are returned as extracted, unrepaired"
            )
    except Exception as exc:
        entry.update(route="failed", error=f"{type(exc).__name__}: {exc}")
    return entry


def read_document(ctx: ToolContext, path: str, pages: Optional[str] = None) -> str:
    """
    Read the text of a PDF, page by page, and report what each page gave up.

    Args:
        ctx: Tool context
        path: Relative (sandbox) or absolute (firewall-checked) path to a PDF
        pages: "3", "1-5", "2,7,9-12". Omitted reads the first 10. Twenty pages
            per call at most; call again for the rest.

    Returns:
        JSON with per-page routes, character counts, image counts, the pages
        nothing could be read from, and the pages whose mathematics is unreliable.
    """
    started = time.perf_counter()
    try:
        source = _resolve_file_path(ctx, path, require_write=False)
    except PermissionError as e:
        return f"⚠️ Access denied: {e}"

    if not source.exists():
        return f"⚠️ File not found: {path}"
    if not source.is_file():
        return f"⚠️ Not a file: {path}"
    if source.suffix.lower() != ".pdf":
        return (
            f"⚠️ read_document reads PDF; '{source.suffix or 'no extension'}' is not "
            f"supported yet. Text and markdown already read correctly through read_file."
        )

    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover - dependency is declared
        return "⚠️ pypdfium2 is not installed; document reading is unavailable."

    try:
        doc = pdfium.PdfDocument(source)
        total = len(doc)
    except Exception as exc:
        low = str(exc).lower()
        if "password" in low or "encrypt" in low:
            return (
                f"⚠️ '{source.name}' is encrypted and this reader has no password for it."
            )
        return f"⚠️ Could not open '{source.name}': {type(exc).__name__}: {exc}"

    wanted, notes = _parse_pages(pages, total)
    if not wanted:
        notes = notes or ["no readable page numbers in the range"]

    per_page = [_read_page(doc, n) for n in wanted]

    unreadable = [p["page"] for p in per_page if p["route"] in ("no_text_layer", "failed")]
    wants_eye = [p["page"] for p in per_page if p.get("suspect_chars")]
    figures_unseen = [
        {"page": p["page"], "images": p["images"]}
        for p in per_page
        if p.get("images") and p["route"] == "text"
    ]

    warnings = list(notes)
    warnings.extend(p["note"] for p in per_page if p.get("note"))
    if any(p.get("detector") == "unavailable" for p in per_page):
        warnings.append(
            "per-character font attribution was unavailable on this build, so the "
            "unreliable-mathematics check did not run"
        )

    payload = {
        "path": str(source),
        "sha256": _file_digest(source),
        "pages_total": total,
        "pages_read": wanted,
        "per_page": per_page,
        "unreadable_pages": unreadable,
        "pages_with_unreliable_math": wants_eye,
        "figures_not_seen": figures_unseen,
        # The document is data, not instruction. Nothing in this repository has
        # carried this field before; a page that asks the agent to do something
        # is a page quoting itself, and the tool gates decide what asking can
        # achieve.
        "untrusted_content": True,
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "warnings": warnings,
    }
    if len(wanted) < total:
        remaining = [n for n in range(1, total + 1) if n not in wanted]
        payload["continue_hint"] = (
            f"{len(remaining)} pages not read; the next is page {remaining[0]}"
        )
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool Registry Export
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    """Export document tools for registry."""
    return [
        ToolEntry(
            name="read_document",
            schema={
                "name": "read_document",
                "description": (
                    "Read the text of a PDF page by page. Returns the text of each "
                    "page plus what that page gave up: how many characters, how many "
                    "images the text route did not look at, and whether the page's "
                    "mathematics is unreliable because a symbol font mapped its glyphs "
                    "into Latin letters. Mathematics is never repaired — a page flagged "
                    "as unreliable is returned as extracted, and the honest way to read "
                    "its formulas is the paper's HTML source or an eye on the page. "
                    "Local only: no network, no vision model, no GPU."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path to the PDF. Relative paths resolve to the agent "
                                "sandbox; absolute paths require extended-path read "
                                "access in the firewall."
                            ),
                        },
                        "pages": {
                            "type": "string",
                            "description": (
                                "Which pages: '3', '1-5', '2,7,9-12'. Omitted reads the "
                                "first 10. At most 20 pages per call."
                            ),
                        },
                    },
                    "required": ["path"],
                },
            },
            handler=read_document,
            timeout_sec=120,
            is_core=False,
            # Reading a document is reading a file the agent was pointed at, and
            # the path gates decide which files those are — but a new tool is off
            # until somebody turns it on, the same posture as web_fetch.
            default_enabled=False,
        ),
    ]
