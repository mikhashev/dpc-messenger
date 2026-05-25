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
import html as html_module
import logging
import re
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from io import StringIO
from typing import Any, Dict, List, Optional

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
    result["needs_js"] = not is_clean_text and len(text or "") < 200
    return result


def _browse_with_camoufox(url: str) -> Optional[str]:
    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        return None

    try:
        with Camoufox(headless=True) as browser:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
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
# ADR-028 T9 — Anti-bot challenge detection
# ─────────────────────────────────────────────────────────────

# Markers of common anti-bot challenge / interstitial pages. The lowercase
# check below makes these case-insensitive. Add new markers conservatively
# — every entry is a potential false-positive surface for legitimate pages
# that happen to mention the marker string in body text.
ANTI_BOT_PATTERNS: tuple[str, ...] = (
    "fab_chlg_",            # Ozon
    "__cf_chl_",            # Cloudflare challenge
    "g-recaptcha",          # Google reCAPTCHA widget
    "_incapsula_resource",  # Imperva / Incapsula
    "px-captcha",           # PerimeterX
)


def looks_like_challenge(html: str) -> bool:
    """Heuristic: does this HTML look like an anti-bot challenge page?

    Challenge pages share two traits we can cheaply test for:
      1. They are small — typically a stub that bootstraps a JS challenge,
         not a full content page. We cap at 50 KB to avoid scanning real
         page bodies that just happen to mention a challenge framework
         in passing (e.g. a blog post about reCAPTCHA).
      2. They contain a known vendor marker near the top of the document.

    Returns True only when both conditions hold on the first 10 KB.

    False positives are possible — a small page mentioning ``g-recaptcha``
    in body text would trigger. Acceptable for MVP per ADR-028 T9; the
    fallback is a popup the user actively closes, so a false trigger
    costs one human interaction, not a wrong result.
    """
    if not html or len(html) > 50_000:
        return False
    sample = html[:10_000].lower()
    return any(p in sample for p in ANTI_BOT_PATTERNS)


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


def _domain_matches(url: str, etld1: str) -> bool:
    """Check whether URL host is the same eTLD+1 as `etld1` (or a
    subdomain of it). Prevents leaking cookies to unrelated hosts that
    happen to embed the auth-domain string in their URL (path / query
    params / fragments)."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname
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


class AuthBrowser:
    """Restricted Camoufox wrapper for authenticated reads (ADR-028 T4).

    Exposes ONLY navigation + content reading — no `click`, `fill`,
    `evaluate`, or any other interactive method. This is tool-level
    enforcement of READ-only access per ADR-028 Phase 1; WRITE
    permissions are explicitly out of scope until Phase 3.

    Cookies are loaded from the agent's encrypted vault (T3
    `web_auth.py`) at construction time. Two failure modes:

      AuthRequiredError — no cookies for the domain. User must log in
        via the Tauri WebView popup (T2) before this works.
      AuthExpiredError — cookies present but expired. Same fix as
        AuthRequiredError — user re-logs in.

    Use as a context manager so the Camoufox browser is always closed:

        with AuthBrowser(agent_id="agent_001", domain="ozon.ru") as ab:
            ab.goto("https://ozon.ru/my/orders")
            html = ab.get_page_content()
    """

    def __init__(self, agent_id: str, domain: str):
        from dpc_client_core import web_auth

        self._agent_id = agent_id
        self._domain = domain.lower()
        self._etld1 = web_auth.resolve_etld1(self._domain)
        cookies = web_auth.load_cookies(agent_id, domain)
        if cookies is None:
            raise AuthRequiredError(
                f"No cookies for {domain} (agent={agent_id}) — re-login required"
            )
        if web_auth.is_expired(cookies):
            raise AuthExpiredError(
                f"Cookies for {domain} expired (agent={agent_id}) — re-login required"
            )
        self._cookies = cookies
        self._cm = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    @property
    def domain(self) -> str:
        return self._domain

    def _open(self) -> None:
        from camoufox.sync_api import Camoufox

        self._cm = Camoufox(headless=True)
        self._browser = self._cm.__enter__()
        self._context = self._browser.new_context()
        self._context.add_cookies(_to_playwright_cookies(self._cookies))
        self._page = self._context.new_page()
        # S144 SHUTDOWN-PIPE-DRAIN: register the live instance so
        # CoreService.shutdown can close any orphaned Camoufox
        # subprocesses before the asyncio loop tears down.
        _active_camoufox_browsers.add(self)

    def goto(self, url: str) -> None:
        """Navigate to URL. URL eTLD+1 must match the auth domain or be
        a subdomain — otherwise raises ValueError without making any
        network request. This prevents the agent from accidentally (or
        adversarially) leaking auth cookies to an unrelated origin."""
        if self._page is None:
            raise RuntimeError("AuthBrowser not opened — use as context manager")
        if not _domain_matches(url, self._etld1):
            raise ValueError(
                f"URL {url!r} is outside auth domain {self._etld1!r}"
            )
        self._page.goto(url, wait_until="networkidle", timeout=30000)

    def get_page_html(self) -> str:
        """Return the raw HTML of the current page. Used by T9 challenge
        detection before trafilatura conversion — markdown extraction
        strips the very script/meta tokens (`fab_chlg_`, `g-recaptcha`,
        etc.) the heuristic looks for.

        Note: `page.content()` is called without an explicit timeout —
        the framework-level tool timeout (see registry.py — currently
        360s for browse_page to accommodate T9 popup interaction) is
        the backstop."""
        if self._page is None:
            raise RuntimeError("AuthBrowser not opened — use as context manager")
        return self._page.content()

    def get_page_content(self) -> str:
        """Return the current page as markdown via trafilatura. Same
        extraction pipeline as the anonymous browse path."""
        return _html_to_markdown(self.get_page_html())

    def close(self) -> None:
        """Release browser resources. Safe to call multiple times."""
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            finally:
                self._cm = None
                self._browser = None
                self._context = None
                self._page = None
        # Deregister even when already closed — discard is a no-op
        # for missing entries, so double-close stays cheap.
        _active_camoufox_browsers.discard(self)


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


def _auth_browse_html(agent_id: str, domain: str, url: str) -> str:
    """Sync helper returning RAW HTML (no trafilatura). T9 needs the
    pre-conversion HTML for `looks_like_challenge()` detection."""
    with AuthBrowser(agent_id=agent_id, domain=domain) as ab:
        ab.goto(url)
        return ab.get_page_html()


def _auth_browse(agent_id: str, domain: str, url: str) -> str:
    """Sync helper used from the async browse_page via asyncio.to_thread.

    Kept as a thin wrapper around `_auth_browse_html` + `_html_to_markdown`
    so existing tests that patch `_auth_browse` directly continue to work
    even after T9 split the raw-HTML and markdown stages."""
    return _html_to_markdown(_auth_browse_html(agent_id, domain, url))


# ─────────────────────────────────────────────────────────────
# ADR-028 T9 — Popup fallback (caller side)
# ─────────────────────────────────────────────────────────────

@dataclass
class PendingPopupRequest:
    """One in-flight popup-fallback request. The WS handler for
    `web_auth_popup_complete` reads `expected_url` / `expected_etld1`
    to enforce the Q2 URL-safety check (Ark softer version) before
    resolving the future with the popup-extracted HTML.

    T10 extension (S143): `keep_open=True` keeps the entry alive after
    the initial extraction so the agent can call follow-up tools
    (`popup_extract_now`, `popup_navigate`, `popup_close`) against the
    same `request_id`. `agent_id` is needed to gate concurrency in
    Step 4. `opened_at` is the monotonic-clock timestamp at session
    open; Step 4 uses it for the 30-minute hard cap (Q2).
    """
    future: asyncio.Future
    expected_url: str
    expected_etld1: str
    agent_id: str = ""
    keep_open: bool = False
    opened_at: float = 0.0


# Pending popup requests keyed by request_id. The future is resolved by
# the local-api WS handler for `web_auth_popup_complete` (Step 3) when
# the frontend reports back with the popup-extracted HTML.
# Ephemeral by design (Q3 decision S142): backend restart loses pending
# requests, frontend popups become orphaned, user retries.
#
# Thread-safety: the dict is mutated from coroutines on the CoreService
# event loop and from the WS handler running on the same loop. DPC has
# a single event loop; if a future deployment multiplexes loops, this
# dict needs an asyncio.Lock or per-loop registries.
_pending_popup_requests: dict[str, PendingPopupRequest] = {}

# 5-minute popup timeout per ADR-028 T9 Q4 (Mike + Ark agreed S142).
_POPUP_TIMEOUT_S = 300

# T10 (S143): per-call timeout for popup_extract_now and friends. The
# popup is already open and authenticated, so each extraction is just
# a JS round-trip — 30s is generous. popup_navigate ALSO uses this for
# the "issue location.href" round-trip; the agent passes wait_seconds
# separately for the post-navigation settle.
_POPUP_OPERATION_TIMEOUT_S = 30

# T10 Q2 (S143): hard cap on how long a single keep_open session can
# stay alive before the framework forces a cleanup. Picked at 30 min
# to match the popup-prompt UX expectation (long but not "forever") —
# prevents an agent stuck in a loop from leaving the popup window
# open indefinitely and confusing the user. Tools that look up an
# expired session raise `AuthRequiredError("session expired")`.
_POPUP_SESSION_MAX_AGE_S = 30 * 60


def get_pending_popup_requests() -> dict[str, PendingPopupRequest]:
    """Accessor used by the Step 3 WS handler to resolve futures by id.
    Module-level dict is the source of truth; this returns the live ref
    (mutation is intentional)."""
    return _pending_popup_requests


async def _request_popup_fallback(
    ctx: ToolContext,
    agent_id: str,
    domain: str,
    url: str,
    reason: str = "anti_bot_challenge",
    keep_open: bool = False,
    wait_seconds: int = 3,
) -> tuple[str, str]:
    """Ask the frontend to open a Tauri WebView popup so the user can
    solve a challenge (Ozon fab_chlg, Cloudflare) or view JS-rendered
    content (YarchePlus orders) for `url`. Awaits the user closing the
    popup; the backend WS handler `web_auth_popup_complete` (Step 3)
    resolves the future with the extracted HTML.

    `reason` is forwarded to the frontend so the popup-request panel
    can render context-appropriate copy:
      - "anti_bot_challenge" — Camoufox got a challenge stub
        (`looks_like_challenge` triggered)
      - "always_popup" — domain is on the agent's `always_popup`
        whitelist (YarchePlus class — JS-render-only sites)

    `keep_open=True` (T10) switches semantics so the popup stays alive
    after the initial extraction. The frontend auto-triggers a JS
    `__dpc_t9_emit_html__()` after `wait_seconds` instead of waiting
    for the user to close the window (S144 fix — Path A semantics for
    a Path B session was the root cause of the 300s timeout that
    surfaced in Mike's manual test). After auto-extract the agent can
    drive follow-up tools (`popup_extract_now`, `popup_navigate`,
    `popup_close`) against the same `request_id`.

    `wait_seconds` (Q5 spec) is the post-page-load settle delay before
    the frontend auto-extracts on the keep_open=True path. Ignored on
    keep_open=False (which still waits for the user to close the
    popup, same as the original T9 contract). Capped at 60.

    Returns `(html, request_id)`. The caller converts HTML to markdown
    via `_html_to_markdown` and surfaces `request_id` only on the
    `keep_open=True` path so the agent can address follow-up calls.
    Raises `AuthRequiredError` on timeout, missing dpc_service, or
    popup_error reported by the frontend.
    """
    dpc_service = getattr(ctx, "dpc_service", None)
    local_api = getattr(dpc_service, "local_api", None) if dpc_service else None
    if local_api is None:
        raise AuthRequiredError(
            "Popup fallback requires DPC service — unavailable in this context"
        )

    import time
    import uuid

    # eTLD+1 used by the Q2 URL-safety check in `web_auth_popup_complete`.
    # Lazy import to keep this module importable when web_auth (which
    # owns the ETLD1_MAP / resolver) isn't wired in unit tests.
    from dpc_client_core import web_auth as _wa

    # T10 Q7 concurrency: only one keep_open session may be active
    # at a time, system-wide. Reject before allocating a request_id
    # or broadcasting so the existing session is undisturbed.
    if keep_open:
        for existing_id, existing in list(_pending_popup_requests.items()):
            if existing.keep_open:
                age = time.monotonic() - existing.opened_at
                if age <= _POPUP_SESSION_MAX_AGE_S:
                    raise AuthRequiredError(
                        f"another popup session is already active "
                        f"(request_id={existing_id}, age={int(age)}s) — "
                        f"close it with popup_close before opening a new one"
                    )
                # Expired sister entry — clean it up and proceed.
                if existing.future and not existing.future.done():
                    existing.future.cancel()
                _pending_popup_requests.pop(existing_id, None)

    request_id = uuid.uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _pending_popup_requests[request_id] = PendingPopupRequest(
        future=future,
        expected_url=url,
        expected_etld1=_wa.resolve_etld1(domain),
        agent_id=agent_id,
        keep_open=keep_open,
        opened_at=time.monotonic(),
    )

    # S144 fix: keep_open=True uses a shorter timeout because the frontend
    # auto-extracts after wait_seconds (no waiting on the user to close).
    # ~60s buffers the wait_seconds (max 60) + page-load + JS round-trip
    # without making the agent wait 5 min when the frontend silently fails.
    wait_seconds = max(0, min(int(wait_seconds), 60))
    initial_timeout = (wait_seconds + 30) if keep_open else _POPUP_TIMEOUT_S

    # Bug 7 (S143 manual-test): track whether the initial extraction
    # actually succeeded. Only successful keep_open sessions stay in
    # the pending-map; any error path pops the entry so the next
    # browse_page(keep_open=True) is not blocked by Q7 concurrency.
    session_alive = False
    try:
        await local_api.broadcast_event(
            "web_auth_popup_request",
            {
                "request_id": request_id,
                "agent_id": agent_id,
                "domain": domain,
                "url": url,
                "reason": reason,
                # T10 Q4: forward keep_open so the popup window title can
                # advertise "Agent active — close to abort" for multi-page
                # sessions vs the plain "DPC — {url}" for single-shot.
                "keep_open": keep_open,
                # S144 T10 fix: frontend schedules an auto-extract after
                # wait_seconds when keep_open=true. Ignored on the
                # single-shot path (existing close-to-extract semantics).
                "wait_seconds": wait_seconds,
            },
        )
        html = await asyncio.wait_for(future, timeout=initial_timeout)
        session_alive = True
        return html, request_id
    except asyncio.TimeoutError as e:
        # S144 fix #2 (T10-FRONTEND-CLEANUP-ON-TIMEOUT): tell the frontend
        # to dismiss the modal and close the popup window. Bug 7 (S143)
        # closed the backend-side leak in `_pending_popup_requests`, but
        # the frontend still had the modal + popup window stuck because
        # no cleanup event was broadcast on error paths.
        try:
            await local_api.broadcast_event(
                "web_auth_popup_force_close",
                {"request_id": request_id, "reason": "timeout"},
            )
        except Exception:
            pass  # best-effort; do not mask the AuthRequiredError below
        raise AuthRequiredError(
            f"Popup fallback timeout ({initial_timeout}s) for {url} — "
            f"user did not complete"
            if not keep_open
            else f"Popup auto-extract timeout ({initial_timeout}s) for "
            f"{url} — frontend did not produce HTML within "
            f"wait_seconds={wait_seconds}s + 30s grace"
        ) from e
    finally:
        # T10 + Bug 7: keep only successful keep_open sessions. The
        # single-shot path (keep_open=False) always pops here, exactly
        # like the original T9 finally. The keep_open=True path used to
        # *always* skip the pop — but on error paths (user_cancelled,
        # popup_close_timeout, AuthRequiredError raised by the WS
        # handler) the agent received an error string with no
        # request_id, leaving a zombie entry that Q7 concurrency then
        # rejected on the next browse_page(keep_open=True). Track
        # `session_alive` instead: only the success-return branch
        # flips it, so every error path falls through to the pop.
        if not (keep_open and session_alive):
            _pending_popup_requests.pop(request_id, None)


# ─────────────────────────────────────────────────────────────
# T10 — Agent-orchestrated multi-page popup tools (S143)
# ─────────────────────────────────────────────────────────────
#
# Three sibling tools to browse_page(keep_open=True). Each looks up
# the session by request_id, broadcasts a request event that the
# frontend forwards to a Tauri command, then either awaits the
# extracted HTML (popup_extract_now) or returns immediately
# (popup_navigate, popup_close).
#
# Decisions locked S143 — see
# tasks/adr-028-agent-web-auth/010-agent-popup-orchestration.md
# (Q4 surfaces, Q5 wait, Q6 errors). Q3 eTLD+1 enforcement and Q7
# concurrency rejection arrive in Step 4.


def _get_local_api(ctx: ToolContext):
    """Resolve the local-api broadcaster from the agent context.

    Raises AuthRequiredError so the agent sees a uniform Q6 error
    surface across the four T9/T10 popup tools.
    """
    dpc_service = getattr(ctx, "dpc_service", None)
    local_api = getattr(dpc_service, "local_api", None) if dpc_service else None
    if local_api is None:
        raise AuthRequiredError(
            "Popup tools require DPC service — unavailable in this context"
        )
    return local_api


def _get_popup_session(request_id: str) -> PendingPopupRequest:
    """Resolve a live keep_open=True session by request_id.

    Raises AuthRequiredError for the missing-entry case (popup closed
    by user, backend restart, never opened) — Q6 unified error model.

    Q2 enforcement: if the session has been alive longer than
    `_POPUP_SESSION_MAX_AGE_S` (30 min), pop the entry, cancel any
    in-flight future, and raise so the agent stops extending the
    session. The actual popup window is closed by the frontend on
    the next manual close or by the user — we don't drive close
    from here because the framework Promise we'd need is awkward to
    spin up off a sync helper.
    """
    import time

    entry = _pending_popup_requests.get(request_id)
    if entry is None:
        raise AuthRequiredError(
            f"popup session not found (request_id={request_id}) — "
            f"closed by user, expired, or never opened"
        )
    if not entry.keep_open:
        raise AuthRequiredError(
            f"popup session is single-shot (request_id={request_id}) — "
            f"use browse_page(keep_open=True) to open a multi-page session"
        )
    age = time.monotonic() - entry.opened_at
    if age > _POPUP_SESSION_MAX_AGE_S:
        # Eject the expired session synchronously so subsequent tool
        # calls (and the orchestrator) see a clean state. Cancelling
        # the future unblocks any awaiter so it raises CancelledError
        # rather than waiting out _POPUP_OPERATION_TIMEOUT_S.
        if entry.future and not entry.future.done():
            entry.future.cancel()
        _pending_popup_requests.pop(request_id, None)
        raise AuthRequiredError(
            f"popup session expired (request_id={request_id}, "
            f"age={int(age)}s, max={_POPUP_SESSION_MAX_AGE_S}s) — "
            f"open a new session via browse_page(keep_open=True)"
        )
    return entry


async def popup_extract_now(ctx: ToolContext, request_id: str) -> str:
    """T10: extract HTML from the current popup page without closing it.

    Triggers an explicit eval of the injected `__dpc_t9_emit_html__()`
    inside the live popup. Avoids the WebView2 tear-down race that
    affects the extract-on-close path (T9 Bug 5) because the popup is
    still alive when JS runs.

    Returns the page HTML rendered to markdown. Raises
    `AuthRequiredError` if the popup is gone (user closed, expired,
    or never opened).
    """
    local_api = _get_local_api(ctx)
    entry = _get_popup_session(request_id)

    loop = asyncio.get_running_loop()
    new_future: asyncio.Future = loop.create_future()
    entry.future = new_future

    await local_api.broadcast_event(
        "web_auth_popup_extract_request",
        {"request_id": request_id},
    )
    try:
        html = await asyncio.wait_for(new_future, timeout=_POPUP_OPERATION_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        raise AuthRequiredError(
            f"popup_extract_now timeout ({_POPUP_OPERATION_TIMEOUT_S}s) "
            f"for request_id={request_id} — popup may have been closed "
            f"or the frontend extract listener is unresponsive"
        ) from e

    text = _html_to_markdown(html)
    return f"Content from popup (markdown, {len(text)} chars, request_id={request_id}):\n\n{text}"


async def popup_navigate(
    ctx: ToolContext,
    request_id: str,
    url: str,
    wait_seconds: int = 3,
) -> str:
    """T10: navigate the popup to a new URL within the same session.

    The popup stays open across navigation (same WebView, same cookie
    jar, init_script `__dpc_t9_emit_html__` survives). The frontend
    forwards to the `web_auth_popup_navigate` Tauri command which
    issues a JS `window.location.href = url` assignment.

    `wait_seconds` (Q5) is the post-navigation settle delay before
    returning, giving JS-heavy SPAs (YarchePlus orders) time to
    finish rendering before the next `popup_extract_now`.

    Step 4 will harden this with an eTLD+1 check (Q3) that rejects
    cross-origin navigation. For Step 2 the check is baseline: the
    Rust command already rejects non-http(s) schemes.
    """
    local_api = _get_local_api(ctx)
    entry = _get_popup_session(request_id)

    # Q3 same-eTLD+1 enforcement: the popup is sitting on top of the
    # user's authenticated cookie jar for `entry.expected_etld1`. An
    # agent that navigates to evil.com would carry no auth there, but
    # could exfiltrate authenticated content to evil.com by abusing
    # link rels or similar. Cheap defence: reject any URL whose
    # eTLD+1 doesn't match the originating session.
    from urllib.parse import urlparse
    from dpc_client_core import web_auth as _wa

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise AuthRequiredError(
            f"popup_navigate rejected url={url!r} — only http/https schemes allowed"
        )
    if not parsed.hostname:
        raise AuthRequiredError(
            f"popup_navigate rejected url={url!r} — missing host"
        )
    target_etld1 = _wa.resolve_etld1(parsed.hostname)
    if not target_etld1 or target_etld1 != entry.expected_etld1:
        raise AuthRequiredError(
            f"popup_navigate cross-origin rejected "
            f"(target={target_etld1 or parsed.hostname}, "
            f"session={entry.expected_etld1}) — agent may only navigate "
            f"within the same site that opened the popup"
        )

    # Track the latest URL so the existing Q2 (Ark softer) URL-safety
    # check in web_auth_popup_complete uses the most recent navigation
    # target as the expected_url for the next extract round.
    entry.expected_url = url

    await local_api.broadcast_event(
        "web_auth_popup_navigate_request",
        {"request_id": request_id, "url": url},
    )

    # Q5: settle delay for SPA rendering. Bounded — the agent must not
    # block forever on a wait. 60s upper limit catches a stuck agent
    # without making the tool slow for the common 3s case.
    wait_seconds = max(0, min(int(wait_seconds), 60))
    if wait_seconds:
        await asyncio.sleep(wait_seconds)

    return f"Navigated popup to {url} (request_id={request_id}, waited {wait_seconds}s)"


async def popup_scroll(
    ctx: ToolContext,
    request_id: str,
    direction: str = "down",
    distance_px: int = 1000,
    settle_seconds: int = 1,
) -> str:
    """T10 Step 5: scroll the popup so JS-paginated lists load more content.

    YarchePlus orders class — the initial DOM only contains ~4 visible
    orders, scrolling to the bottom triggers a JS XHR that appends
    older ones. `popup_extract_now` only sees the currently-rendered
    DOM, so an agent that wants the full list must scroll first.

    `direction`:
      - `"down"` / `"up"` — relative scroll by `distance_px` pixels
      - `"top"` / `"bottom"` — absolute scroll to start/end of document
        (`distance_px` ignored)

    `settle_seconds` is the post-scroll wait — gives the site time to
    fetch + render newly-appended content before the next extract.
    Capped at 30s. Default 1s covers the common XHR case.
    """
    local_api = _get_local_api(ctx)
    entry = _get_popup_session(request_id)

    if direction not in ("down", "up", "top", "bottom"):
        raise AuthRequiredError(
            f"popup_scroll: invalid direction {direction!r} "
            f"(allowed: down, up, top, bottom)"
        )

    distance_px = max(0, int(distance_px))
    await local_api.broadcast_event(
        "web_auth_popup_scroll_request",
        {
            "request_id": request_id,
            "direction": direction,
            "distance_px": distance_px,
        },
    )

    settle_seconds = max(0, min(int(settle_seconds), 30))
    if settle_seconds:
        await asyncio.sleep(settle_seconds)

    # entry still alive; popup_extract_now after this call sees the
    # updated DOM. Touch entry to silence "unused" lint warnings —
    # session lifetime check already happened in _get_popup_session.
    _ = entry
    return (
        f"Scrolled popup {direction} "
        f"(request_id={request_id}, distance={distance_px}px, "
        f"settled {settle_seconds}s) — call popup_extract_now next "
        f"to read the updated DOM"
    )


async def popup_close(ctx: ToolContext, request_id: str) -> str:
    """T10: close the popup programmatically.

    Triggers the existing CloseRequested handler — `web_auth_popup_closing`
    fires, the vault re-sync extracts cookies via the main window jar
    (Bug 4 hook preserved), and the modal-watchdog timer arms. From the
    agent's perspective the session is over after this call returns;
    follow-up `popup_extract_now`/`popup_navigate` will raise
    AuthRequiredError because the entry is gone.
    """
    local_api = _get_local_api(ctx)
    entry = _get_popup_session(request_id)

    # Cancel any awaiter (e.g. an in-flight popup_extract_now that
    # raced against this close) so its `asyncio.wait_for` gets a
    # CancelledError instead of timing out at _POPUP_OPERATION_TIMEOUT_S.
    if entry.future and not entry.future.done():
        entry.future.cancel()

    await local_api.broadcast_event(
        "web_auth_popup_close_request",
        {"request_id": request_id},
    )

    # Pop the entry immediately so subsequent tool calls get a clean
    # AuthRequiredError. The actual popup window closes asynchronously
    # via the Tauri command — the CloseRequested handler still runs
    # vault re-sync and the popup_closing/watchdog flow.
    _pending_popup_requests.pop(request_id, None)

    return f"Closed popup session request_id={request_id}"


async def browse_page(
    ctx: ToolContext,
    url: str,
    size: str = "m",
    use_auth: Optional[str] = None,
    keep_open: bool = False,
    wait_seconds: int = 3,
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
        use_auth: If set, fetch the page authenticated for this domain.
            Routes through restricted AuthBrowser with cookies from the
            agent's encrypted vault (ADR-028). The URL must be within
            the same eTLD+1 as use_auth (subdomains allowed). Returns a
            re-login prompt if cookies are missing or expired.
        keep_open: When true, leave the popup open after the initial
            fetch so subsequent popup_extract_now / popup_navigate /
            popup_close tools can drive it. The response includes a
            `request_id=...` hint.
        wait_seconds: Post-page-load settle delay before the frontend
            auto-extracts on the keep_open=True popup path. Default 3
            covers most SPAs; raise for slow JS render. Ignored when
            keep_open=False (Path A waits for user close). Capped at 60.

    Returns:
        Page content as markdown
    """
    if use_auth:
        # Contract: ctx.agent_root is ~/.dpc/agents/{agent_id}/ (see
        # dpc_agent.utils.get_agent_root). Last path component IS the
        # agent_id. If the agent storage layout changes, this derivation
        # must move to a helper there — track via grep on `agent_root.name`.
        agent_id = ctx.agent_root.name

        # ADR-028 T5: per-agent + per-domain auth gate. Reject before
        # opening Camoufox if the agent's web_auth.allowed_domains
        # whitelist doesn't permit this domain, or if no cookies are in
        # the vault. firewall is None in pure-unit-test contexts (no
        # dpc_service wired) — those tests bypass the gate by design.
        firewall = None
        dpc_service = getattr(ctx, "dpc_service", None)
        if dpc_service is not None:
            firewall = getattr(dpc_service, "firewall", None)
        # ADR-028 T6 audit hook: import lazily so the tool module stays
        # importable even when web_auth.py is unavailable in tests.
        from dpc_client_core import web_auth as _web_auth_mod

        if firewall is not None and not firewall.is_auth_domain_allowed(agent_id, use_auth):
            # Per-reason guidance instead of one generic message — Mike
            # S142 caught the old phrasing conflating "add to whitelist"
            # and "re-login" when the actual fix depends on which check
            # failed. get_auth_denial_reason mirrors the conditions in
            # is_auth_domain_allowed so the advice always matches reality.
            reason = firewall.get_auth_denial_reason(agent_id, use_auth)
            _web_auth_mod.audit_append(
                agent_id, use_auth, url,
                status=f"firewall_denied:{reason or 'unknown'}",
            )
            if reason == "not_in_whitelist":
                return (
                    f"⚠️ Domain '{use_auth}' is not in agent '{agent_id}''s "
                    f"authorized list. Add it to "
                    f"agent_profiles.{agent_id}.web_auth.allowed_domains in "
                    f"privacy_rules.json (UI: AgentPermissionsPanel → Web "
                    f"Authentication → '+ Add'), then log in via the popup."
                )
            if reason == "cookies_missing":
                return (
                    f"⚠️ Agent '{agent_id}' has no saved login for "
                    f"'{use_auth}'. Open AgentPermissionsPanel → Web "
                    f"Authentication and click 'Login' next to '{use_auth}' "
                    f"to authenticate."
                )
            if reason == "cookies_expired":
                return (
                    f"⚠️ Saved login for '{use_auth}' has expired for "
                    f"agent '{agent_id}'. Open AgentPermissionsPanel → Web "
                    f"Authentication and click 'Re-login' next to "
                    f"'{use_auth}' to refresh cookies."
                )
            # Belt-and-suspenders: keep the original generic phrasing for
            # any future denial reason we forget to map here.
            return (
                f"⚠️ Domain '{use_auth}' is not authorized for agent "
                f"'{agent_id}'. Check the Web Authentication settings."
            )

        # T9 always_popup (YarchePlus variant C): skip Camoufox entirely
        # for sites whose interesting content only renders under a real
        # browser (JS-only order pages, client-rendered SPAs). The user
        # already authorized this exact domain via allowed_domains, so
        # the firewall check above is the security boundary; this just
        # decides headless vs popup at fetch time.
        if firewall is not None:
            use_auth_etld1 = _web_auth_mod.resolve_etld1(use_auth)
            if use_auth_etld1 in firewall.get_agent_always_popup_domains(agent_id):
                log.info(
                    "always_popup whitelist hit for %s (agent=%s) — "
                    "skipping headless fetch", url, agent_id,
                )
                try:
                    html, popup_request_id = await _request_popup_fallback(
                        ctx, agent_id, use_auth, url,
                        reason="always_popup", keep_open=keep_open,
                        wait_seconds=wait_seconds,
                    )
                except AuthRequiredError as e:
                    _web_auth_mod.audit_append(
                        agent_id, use_auth, url, status="popup_timeout"
                    )
                    return f"⚠️ {e}"
                text = _html_to_markdown(html)
                _web_auth_mod.audit_append(
                    agent_id, use_auth, url, status=200, bytes_size=len(text)
                )
                max_chars = _SIZE_PRESETS.get(size, _SIZE_PRESETS["m"])
                total = len(text)
                if max_chars and total > max_chars:
                    text = text[:max_chars] + f"\n\n... (truncated, {total} total chars, use size='l' or 'f' for more)"
                # T10: when keep_open=True surface the session request_id
                # so the agent can call popup_extract_now / popup_navigate /
                # popup_close against this popup.
                session_hint = (
                    f" — session request_id={popup_request_id} (open; close with popup_close)"
                    if keep_open
                    else ""
                )
                return f"Content from {url} (markdown, auth={use_auth}, {total} chars{session_hint}):\n\n{text}"

        try:
            html = await asyncio.to_thread(_auth_browse_html, agent_id, use_auth, url)
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

        # T9 challenge detection: if Camoufox got back an anti-bot stub,
        # hand off to the Tauri WebView popup (T2 cookie jar / fingerprint
        # the user already trusted at login). User solves challenge or
        # views JS-rendered content, popup closes, backend gets the
        # rendered HTML back via WS `web_auth_popup_complete` (Step 3).
        challenge_request_id: Optional[str] = None
        if looks_like_challenge(html):
            log.info(
                "Anti-bot challenge detected for %s (agent=%s) — "
                "requesting popup fallback", url, agent_id,
            )
            try:
                html, challenge_request_id = await _request_popup_fallback(
                    ctx, agent_id, use_auth, url, keep_open=keep_open,
                    wait_seconds=wait_seconds,
                )
            except AuthRequiredError as e:
                _web_auth_mod.audit_append(
                    agent_id, use_auth, url, status="popup_timeout"
                )
                return f"⚠️ {e}"

        text = _html_to_markdown(html)
        # Success path — record byte size for cost / quota tracking.
        _web_auth_mod.audit_append(
            agent_id, use_auth, url, status=200, bytes_size=len(text)
        )
        max_chars = _SIZE_PRESETS.get(size, _SIZE_PRESETS["m"])
        total = len(text)
        if max_chars and total > max_chars:
            text = text[:max_chars] + f"\n\n... (truncated, {total} total chars, use size='l' or 'f' for more)"
        # T10: surface request_id on the challenge popup-fallback path
        # too when keep_open=True so the agent can drive follow-up tools.
        session_hint = (
            f" — session request_id={challenge_request_id} (open; close with popup_close)"
            if keep_open and challenge_request_id
            else ""
        )
        return f"Content from {url} (markdown, auth={use_auth}, {total} chars{session_hint}):\n\n{text}"

    result = await asyncio.to_thread(_browse_sync, url)

    if not result.get("success", False) and "error" in result:
        return f"⚠️ Failed to fetch page: {result['error']}"

    text = result.get("text", "")

    if result.get("needs_js"):
        js_text = await asyncio.to_thread(_browse_with_camoufox, url)
        if js_text and len(js_text) > len(text or ""):
            text = js_text
    max_chars = _SIZE_PRESETS.get(size, _SIZE_PRESETS["m"])
    total = len(text)
    if max_chars and total > max_chars:
        text = text[:max_chars] + f"\n\n... (truncated, {total} total chars, use size='l' or 'f' for more)"

    return f"Content from {url} (markdown, {total} chars):\n\n{text}"


def fetch_json(ctx: ToolContext, url: str) -> str:
    """
    Fetch JSON data from a URL.

    Args:
        ctx: Tool context (unused)
        url: URL to fetch

    Returns:
        JSON content formatted for reading
    """
    import json

    result = _fetch_url(url)

    if not result["success"]:
        return f"⚠️ Failed to fetch JSON: {result['error']}"

    try:
        data = json.loads(result["content"])
        formatted = json.dumps(data, indent=2, ensure_ascii=False)

        if len(formatted) > 10000:
            formatted = formatted[:10000] + f"\n\n... (truncated)"

        return f"JSON from {url}:\n\n{formatted}"

    except json.JSONDecodeError as e:
        return f"⚠️ Invalid JSON: {e}"



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


def get_tools() -> List[ToolEntry]:
    """Export browser tools for registry."""
    return [
        ToolEntry(
            name="browse_page",
            schema={
                "name": "browse_page",
                "description": "Fetch a web page and extract content as structured markdown. Preserves headings, lists, tables, and links. Use size presets to control output length: s=5K, m=10K (default), l=25K, f=full. Set use_auth=<domain> to fetch authenticated content using stored cookies (requires prior login via the web-auth UI). Set keep_open=true to leave the popup window open after the initial fetch — the response includes a 'request_id=...' hint you can then pass to popup_extract_now, popup_navigate, or popup_close for multi-page workflows (e.g. drilling from an order list into individual orders without reopening the popup).",
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
                            "description": "Optional auth domain (eg 'ozon.ru'). When set, the page is fetched authenticated using cookies from the agent's encrypted vault. The URL must be within the same eTLD+1 as use_auth (subdomains allowed). Returns a re-login prompt if cookies are missing or expired."
                        },
                        "keep_open": {
                            "type": "boolean",
                            "description": "When true, keep the popup window open after the initial fetch so you can call popup_navigate/popup_extract_now/popup_close against the same session. Use for multi-page authenticated workflows. Default false (single-shot, popup closes automatically).",
                            "default": False
                        },
                        "wait_seconds": {
                            "type": "integer",
                            "description": "Post-page-load settle delay before the popup auto-extracts on the keep_open=true path. 3s is right for most SPAs; raise for slow JS render. Ignored when keep_open=false. Capped at 60.",
                            "default": 3,
                            "minimum": 0,
                            "maximum": 60
                        }
                    },
                    "required": ["url"]
                }
            },
            handler=browse_page,
            # 60s is too tight for the use_auth path: that goes through
            # Camoufox launch + goto (30s wait_until=networkidle) + optional
            # T9 popup-fallback (5min user-interaction timeout per Q4) +
            # trafilatura conversion. Worst case: ~5min popup + 30s
            # Camoufox + small overhead. 360s gives a 30s buffer over the
            # 5-min popup deadline so the user always has the full 5 min.
            # Anonymous browse_page (without use_auth) returns in <10s so
            # the higher cap doesn't slow that path down.
            timeout_sec=360,
            default_enabled=True,
        ),

        # T10: multi-page popup orchestration siblings. All three look up
        # the session via request_id obtained from browse_page(keep_open=true).
        ToolEntry(
            name="popup_extract_now",
            schema={
                "name": "popup_extract_now",
                "description": "Extract HTML from the currently-open popup session without closing it. Returns the page rendered to markdown. Use after popup_navigate to read the new page, or to re-read the same page after dynamic content updates. Requires a session previously opened via browse_page(keep_open=true).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_id": {
                            "type": "string",
                            "description": "Session id returned from browse_page(keep_open=true)"
                        }
                    },
                    "required": ["request_id"]
                }
            },
            handler=popup_extract_now,
            timeout_sec=45,
            default_enabled=True,
        ),

        ToolEntry(
            name="popup_navigate",
            schema={
                "name": "popup_navigate",
                "description": "Navigate the popup to a new URL within the same authenticated session. The popup window stays open across navigations (same cookies, same JS state). Pass wait_seconds (default 3) to let JS-heavy SPAs settle before the next popup_extract_now. Must stay within the same eTLD+1 as the original popup session.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_id": {
                            "type": "string",
                            "description": "Session id returned from browse_page(keep_open=true)"
                        },
                        "url": {
                            "type": "string",
                            "description": "New URL to load (must match the session's eTLD+1)"
                        },
                        "wait_seconds": {
                            "type": "integer",
                            "description": "Post-navigation settle delay before returning. 3 seconds is right for most sites; raise for slow SPAs. Capped at 60.",
                            "default": 3,
                            "minimum": 0,
                            "maximum": 60
                        }
                    },
                    "required": ["request_id", "url"]
                }
            },
            handler=popup_navigate,
            timeout_sec=90,
            default_enabled=True,
        ),

        ToolEntry(
            name="popup_close",
            schema={
                "name": "popup_close",
                "description": "Close the popup session opened by browse_page(keep_open=true). The popup window goes through its normal close flow — cookies are re-synced to the vault and the session is cleaned up. Call this when you are done with the multi-page workflow.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_id": {
                            "type": "string",
                            "description": "Session id returned from browse_page(keep_open=true)"
                        }
                    },
                    "required": ["request_id"]
                }
            },
            handler=popup_close,
            timeout_sec=15,
            default_enabled=True,
        ),

        ToolEntry(
            name="popup_scroll",
            schema={
                "name": "popup_scroll",
                "description": "Scroll the popup window programmatically so JS-paginated lists load more content. Many sites only render a few items initially and fetch more via XHR on scroll-to-bottom (YarchePlus orders list is a typical example). Use 'bottom' to trigger 'load more', then call popup_extract_now to read the updated DOM. Repeat as needed until extract returns no new items.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_id": {
                            "type": "string",
                            "description": "Session id returned from browse_page(keep_open=true)"
                        },
                        "direction": {
                            "type": "string",
                            "description": "Scroll direction. 'down'/'up' = relative by distance_px. 'top'/'bottom' = absolute to start/end of document (distance_px ignored).",
                            "enum": ["down", "up", "top", "bottom"],
                            "default": "down"
                        },
                        "distance_px": {
                            "type": "integer",
                            "description": "Pixels to scroll for 'down'/'up'. Ignored for 'top'/'bottom'.",
                            "default": 1000,
                            "minimum": 0
                        },
                        "settle_seconds": {
                            "type": "integer",
                            "description": "Post-scroll wait in seconds — gives XHR-loaded content time to render before the next popup_extract_now. Capped at 30.",
                            "default": 1,
                            "minimum": 0,
                            "maximum": 30
                        }
                    },
                    "required": ["request_id"]
                }
            },
            handler=popup_scroll,
            timeout_sec=45,
            default_enabled=True,
        ),

        ToolEntry(
            name="fetch_json",
            schema={
                "name": "fetch_json",
                "description": "Fetch JSON data from a URL API endpoint",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to fetch JSON from"
                        }
                    },
                    "required": ["url"]
                }
            },
            handler=fetch_json,
            timeout_sec=30,
            default_enabled=True,
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
            default_enabled=True,
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
            default_enabled=True,
        ),
    ]
