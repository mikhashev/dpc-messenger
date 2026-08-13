"""
DPC Agent — document reading (PDF).

`read_file` decodes every path as UTF-8 with errors="replace", so a paper
handed to an agent came back as replacement characters — and came back as a
*string*, so nothing reported a failure. This tool reads the text layer of a
PDF instead, page by page, and says per page what it got and what it did not.

A page with no text layer is a scan, and the only local way to read one is to
render it and look. That route exists here, and every part of it is bounded:
it never starts without being asked or without the page proving it has no
text, it never runs more pages than the caller allowed, it refuses in advance
with the price rather than spending it, and a page it has read once is not
read again. The GPU is shared with everything else on the machine, which is
what all of that is protecting.

What it deliberately does **not** do:

- **repair mathematics.** Measured on four TeX documents 2026-08-13: the
  characters a math font puts in the text layer are wrong in several font
  families at once (CMSY in four documents out of four, CMEX, MSBM, TeX-matha),
  the same font is right in one place and wrong in another, and one document
  emits 27 NUL bytes that carry nothing to repair. A substitution table would
  therefore be both endless and a lie on prose (`P` really is `P` most of the
  time). It detects and flags instead: an honest gap beats a plausible repair.
- **fetch anything.** This tool opens no URL of its own; the arXiv-HTML route,
  which is the only one that can return a paper's real formulas, is a separate
  tool, because a file reader that quietly reached out would misrepresent what
  the firewall panel promises about it. Note what that does *not* say: the
  vision provider it hands a page to may itself be a peer's model reached over
  P2P, which is how a node with no GPU reads a scan at all.
"""

from __future__ import annotations

import base64
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

# What a tool may hand back before the agent loop cuts it off: 15 000 characters
# (`dpc_agent/loop.py:_truncate_tool_result`), and the cut lands mid-JSON, so
# what arrives is not even parseable. Pages are the wrong unit to bound a
# response by — twenty pages of a real paper are 60 000 characters and can never
# fit. So the text is filled to a budget and the response says which pages it
# stopped at, which is the difference between a short answer and a broken one.
MAX_TEXT_CHARS = 11000

# Small formatting pieces for the written-out form, kept here so the writer
# below reads as one statement rather than a wall of escapes.
NL = chr(10)
NL2 = NL * 2
PAGE_HEADING = "## Page {n}" + NL2
FILE_HEADING = "# {name}" + NL2 + "pages {first}-{last} of {total}" + NL2


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

# The vision route, and every number here is a bound rather than a preference.
VISION_DPI = 150                    # 2162 image tokens for an A4 page, measured
DEFAULT_MAX_VISION_PAGES = 2        # per call; the caller raises it deliberately
# Measured 2026-08-13: 15 s on a prose scan, 40 s on a page of dense
# mathematics. The refusal quotes the pessimistic end — a price that turns out
# lower is a good surprise, and `vision_seconds` reports what it actually was.
VISION_SECONDS_PER_PAGE = 40
MAX_RENDER_MEGAPIXELS = 40          # a hostile MediaBox renders into whatever it likes
VISION_PROMPT = (
    "Transcribe this page exactly as printed, in reading order. Write any "
    "mathematics as LaTeX. Output only the transcription."
)
VISION_TEMPERATURE = 0
# Bumped when the prompt changes, because a cached transcription made by a
# different instruction is a different answer to a different question.
VISION_PROMPT_VERSION = 1


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


def _fit_to_budget(
    pages: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[int], int]:
    """Split pages into what fits in one answer and what does not.

    Stops at the first page that would overflow rather than skipping it: filling
    the remaining room with whatever comes next leaves a hole in the middle of
    the range and a continue hint pointing at a page already returned — the run
    on the eighty-page document came back as pages 1-13 and 16, which is not a
    thing a reader can act on. The first page is always kept, however long it
    is, because an answer with no pages at all is worse than a long one.
    """
    kept: List[Dict[str, Any]] = []
    spent = 0
    for index, entry in enumerate(pages):
        text = entry.get("text") or ""
        if kept and spent + len(text) > MAX_TEXT_CHARS:
            return kept, [e["page"] for e in pages[index:]], spent
        spent += len(text)
        kept.append(entry)
    return kept, [], spent


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


def _cache_path(ctx: ToolContext, digest: str, page: int, model: str, dpi: int) -> Path:
    """Where a page already read by the model is kept.

    Keyed by the file's own hash rather than its name, so the same document
    read from two places is read once, and by model, dpi and prompt version,
    because changing any of those changes the answer.
    """
    root = getattr(ctx, "agent_root", None) or Path.home() / ".dpc"
    folder = Path(root) / "state" / "doc_cache"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = f"{digest[:16]}-p{page}-{model}-{dpi}-v{VISION_PROMPT_VERSION}"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in stamp)
    return folder / f"{safe}.json"


def _render_page(doc, number: int, dpi: int) -> Tuple[Optional[bytes], Optional[str]]:
    """A page as PNG bytes, or the reason it was not rendered.

    The size is asked for before anything is allocated: a page's MediaBox is
    whatever the document says it is, and a hostile one asks for a bitmap the
    size of the machine's memory. 40 megapixels is far above any real page —
    A4 at 150 dpi is 2.2 — and far below anything that hurts.
    """
    try:
        page = doc[number - 1]
        width_pt, height_pt = page.get_size()
        megapixels = (width_pt * dpi / 72) * (height_pt * dpi / 72) / 1_000_000
        if megapixels > MAX_RENDER_MEGAPIXELS:
            return None, (
                f"page {number} would render to {megapixels:.0f} megapixels at "
                f"{dpi} dpi, over the {MAX_RENDER_MEGAPIXELS} limit; not rendered"
            )
        import io

        image = page.render(scale=dpi / 72).to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), None
    except Exception as exc:
        return None, f"page {number} could not be rendered: {type(exc).__name__}: {exc}"


async def _read_page_with_vision(
    ctx: ToolContext, doc, entry: Dict[str, Any], digest: str, model: Optional[str], dpi: int
) -> None:
    """Fill a page's text in by looking at it. Mutates `entry` in place."""
    number = entry["page"]
    alias = model or "default"
    cache = _cache_path(ctx, digest, number, alias, dpi)
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            entry.update(
                route="vision", text=cached["text"], chars=len(cached["text"]),
                model=cached.get("model"), cached=True, seconds=0.0,
                note=f"page {number} was read by a vision model earlier and is served from cache",
            )
            return
        except Exception as exc:
            log.debug("unreadable cache entry %s: %s", cache, exc)

    llm = getattr(getattr(ctx, "dpc_service", None), "llm_manager", None)
    if llm is None:
        entry["note"] = f"page {number} needs an eye and no model is reachable from here"
        return

    png, problem = _render_page(doc, number, dpi)
    if png is None:
        entry["note"] = problem
        return

    started = time.perf_counter()
    try:
        meta = await llm.query(
            prompt=VISION_PROMPT,
            provider_alias=model,  # None → the configured vision provider
            images=[{"base64": base64.b64encode(png).decode("ascii"), "mime_type": "image/png"}],
            return_metadata=True,
            # A copy has no business being creative. The alias this runs through
            # carries whatever temperature its owner chose for describing
            # pictures — 0.7 on this machine — and that is the wrong setting for
            # transcribing one. Per-call wins over the alias: kwargs reach
            # `_build_options` through `LLMManager.query`.
            temperature=VISION_TEMPERATURE,
        )
    except Exception as exc:
        entry["note"] = f"page {number}: the vision model failed — {type(exc).__name__}: {exc}"
        entry["seconds"] = round(time.perf_counter() - started, 1)
        return
    seconds = round(time.perf_counter() - started, 1)

    text = (meta.get("response", "") if isinstance(meta, dict) else str(meta)) or ""
    used = (meta.get("model") if isinstance(meta, dict) else None) or alias
    if not text.strip():
        # The provider stopped substituting reasoning for an empty answer, so an
        # empty answer is what it is: the page was not read, and saying so is the
        # whole point of the exercise.
        entry.update(seconds=seconds, model=used)
        entry["note"] = (
            f"page {number}: the vision model returned nothing after {seconds} s — "
            f"the page is unread, not empty"
        )
        return

    entry.update(
        route="vision", text=text, chars=len(text), model=used, cached=False,
        seconds=seconds,
        note=(
            f"page {number} has no text layer and was transcribed by a vision model "
            f"in {seconds} s — a transcription, not the document's own characters"
        ),
    )
    try:
        cache.write_text(
            json.dumps({"text": text, "model": used, "seconds": seconds}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("could not cache page %s of %s: %s", number, digest[:8], exc)


async def read_document(
    ctx: ToolContext,
    path: str,
    pages: Optional[str] = None,
    mode: str = "auto",
    max_vision_pages: int = DEFAULT_MAX_VISION_PAGES,
    vision_model: Optional[str] = None,
    save_to: Optional[str] = None,
) -> str:
    """
    Read the text of a PDF, page by page, and report what each page gave up.

    Args:
        ctx: Tool context
        path: Relative (sandbox) or absolute (firewall-checked) path to a PDF
        pages: "3", "1-5", "2,7,9-12". Omitted reads the first 10. Twenty pages
            per call at most; call again for the rest.
        mode: "auto" sends only pages with no text layer to the vision model,
            "text" never does, "vision" sends every requested page.
        max_vision_pages: how many pages this call may spend on the model. Over
            it, nothing is spent and the answer says what it would have cost.
        vision_model: provider alias for the vision route. None uses the
            configured vision provider.
        save_to: write the pages to this file instead of returning them. The
            answer then carries the per-page metadata and the path, and the
            document never passes through the conversation — which is how a
            long one is read at all.

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

    # The eye, and only where it was asked for or proved necessary. Deciding
    # this after every page has been inventoried — which costs about 5 ms a
    # page — is what lets the refusal below quote a real number instead of
    # discovering the cost halfway through spending it.
    if mode not in ("auto", "text", "vision"):
        notes.append(f"mode '{mode}' is not one of auto/text/vision; read as text only")
        mode = "text"
    if mode == "vision":
        candidates = [p for p in per_page if p["route"] != "failed"]
    elif mode == "auto":
        candidates = [p for p in per_page if p["route"] == "no_text_layer" and p.get("images")]
    else:
        candidates = []

    # The digest identifies the document in the answer and keys the page cache.
    # It streams a megabyte at a time, and tools run in an executor thread
    # (`dpc_agent/loop.py:378`), so neither memory nor the service's event loop
    # pays for a large file — only the read itself, once per call.
    digest = _file_digest(source)
    refused_pages: List[int] = []
    if len(candidates) > max(0, max_vision_pages):
        refused_pages = [p["page"] for p in candidates]
        notes.append(
            f"{len(candidates)} pages were put to the vision model and this call allows "
            f"{max_vision_pages}: pages {refused_pages} were not looked at. That would "
            f"take roughly {len(candidates) * VISION_SECONDS_PER_PAGE} s on a GPU shared "
            f"with everything else here. Call again with a narrower range, or raise "
            f"max_vision_pages deliberately."
        )
    else:
        for entry in candidates:
            await _read_page_with_vision(ctx, doc, entry, digest, vision_model, VISION_DPI)

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

    # Written out instead of returned: the pages go to a file, the answer keeps
    # the metadata. A tool result is capped at 15 000 characters, so an eighty
    # page document cannot be read into a conversation however many calls it is
    # split into — it has to land somewhere and be read from there.
    saved_to = None
    if save_to:
        try:
            target = _resolve_file_path(ctx, save_to, require_write=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            body = NL2.join(
                (PAGE_HEADING.format(n=e['page']) + (e.get('text') or '')).rstrip()
                for e in per_page
            )
            header = FILE_HEADING.format(
                name=source.name, first=wanted[0], last=wanted[-1], total=total
            )
            existing = target.read_text(encoding='utf-8') if target.exists() else ''
            target.write_text(
                (existing + NL2 if existing else header) + body + NL,
                encoding='utf-8',
            )
            saved_to = str(target)
        except PermissionError as exc:
            warnings.append(f"could not write to '{save_to}': {exc}")
        except OSError as exc:
            warnings.append(f"could not write to '{save_to}': {type(exc).__name__}: {exc}")

    # Fill to the budget, then stop and say so. A page is kept whole or not at
    # all: half a page of text with no marker is the failure this is preventing.
    kept: List[Dict[str, Any]] = []
    omitted: List[int] = []
    spent = 0
    if saved_to:
        spent = sum(len(e.get("text") or "") for e in per_page)
        kept = [{k: v for k, v in e.items() if k != "text"} for e in per_page]
        per_page = []
    if per_page:
        kept, omitted, spent = _fit_to_budget(per_page)
    if omitted:
        warnings.append(
            f"pages {omitted[0]}-{omitted[-1]} were read and left out of this answer "
            f"to stay under the {MAX_TEXT_CHARS}-character limit a tool result has. "
            f"Continue with pages='{omitted[0]}-{omitted[-1]}', or pass save_to to "
            f"write the whole range to a file instead of returning it."
        )

    payload = {
        "path": str(source),
        "sha256": digest,
        "pages_total": total,
        "pages_read": wanted,
        "per_page": kept,
        "pages_omitted_for_size": omitted,
        "saved_to": saved_to,
        "unreadable_pages": unreadable,
        "pages_with_unreliable_math": wants_eye,
        "figures_not_seen": figures_unseen,
        "vision_pages": [p["page"] for p in per_page if p["route"] == "vision"],
        "vision_pages_refused": refused_pages,
        # What this document cost, so the next decision about the cap is a
        # number rather than a taste.
        "vision_seconds": round(sum(p.get("seconds") or 0 for p in per_page), 1),
        # The document is data, not instruction. Nothing in this repository has
        # carried this field before; a page that asks the agent to do something
        # is a page quoting itself, and the tool gates decide what asking can
        # achieve.
        "untrusted_content": True,
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "warnings": warnings,
    }
    returned = [p["page"] for p in kept]
    unread = [n for n in range(1, total + 1) if n not in returned]
    # Where this answer sits in the document, in the same shape read_file uses —
    # what you have, out of how much, and the call that continues. An agent that
    # cannot see the edges of what it received has no way to know it is missing
    # anything, which is how twenty pages of an eighty-page document get treated
    # as the whole thing.
    payload["position"] = (
        f"[Pages {returned[0]}-{returned[-1]} of {total} | {spent:,} chars"
        + (f" | {len(unread)} pages not read" if unread else " | whole document")
        + (f" | continue: pages='{unread[0]}-{min(unread[0] + DEFAULT_PAGES - 1, total)}'"
           if unread else "")
        + "]"
        if returned
        else f"[No pages returned of {total}]"
    )
    if unread:
        payload["continue_hint"] = (
            f"{len(unread)} pages not read; the next is page {unread[0]}"
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
                    "Read a PDF — the text layer where there is one, and the local "
                    "vision model where there is not. A page with no text is a scan: "
                    "it is rendered and transcribed automatically (bounded by "
                    "max_vision_pages, cached by file hash, ~40 s a page on a shared "
                    "GPU), and mode='vision' transcribes pages that do have text, for "
                    "when the text layer is wrong rather than missing. Returns each "
                    "page plus what it cost: characters, images the text route did not "
                    "look at, and whether the page's mathematics is unreliable because "
                    "a symbol font mapped its glyphs into Latin letters — never "
                    "repaired, so a flagged page comes back exactly as extracted. The "
                    "answer carries a `position` line saying which pages of how many "
                    "you have and how to continue. For a document too long to read "
                    "into a conversation, pass save_to: the pages are written to that "
                    "file and only the metadata comes back. Local only: no network."
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
                        "mode": {
                            "type": "string",
                            "enum": ["auto", "text", "vision"],
                            "description": (
                                "'auto' (default) reads the text layer and sends only "
                                "pages that have none — scans — to the vision provider. "
                                "'text' never uses a model. 'vision' transcribes "
                                "every requested page, which is slow and worth it only "
                                "when the text layer is wrong rather than missing."
                            ),
                        },
                        "max_vision_pages": {
                            "type": "integer",
                            "description": (
                                "How many pages this call may spend on the vision model "
                                "(default 2, about 25 s each on a shared GPU). If more "
                                "pages need it, none are read and the answer says which "
                                "and what it would have cost."
                            ),
                        },
                        "vision_model": {
                            "type": "string",
                            "description": (
                                "Provider alias for the vision route. Omit to use the "
                                "configured vision provider."
                            ),
                        },
                    },
                    "required": ["path"],
                },
            },
            handler=read_document,
            timeout_sec=600,  # two vision pages at the measured tail, plus margin
            is_core=False,
            # Reading a document is reading a file the agent was pointed at, and
            # the path gates decide which files those are — but a new tool is off
            # until somebody turns it on, the same posture as web_fetch.
            default_enabled=False,
        ),
    ]
