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
import json
import logging
import os
import platform
import re
import ssl
import time
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
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
    when syncing the close-time `storage_state` back to vault."""
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
            idle = now - session._last_activity
            if idle > IDLE_TIMEOUT_SECONDS:
                log.info(
                    "Closing idle %s for %s (idle %.0fs)", label, agent_id, idle
                )
                try:
                    await _run_in_session(session, "close")
                except Exception as e:
                    log.warning("Error closing idle %s %s: %s", label, agent_id, e)
                registry.pop(agent_id, None)
                closed += 1
    return closed


_session_locks: dict[str, asyncio.Lock] = {}

_pending_auth_approvals: dict[str, dict] = {}


def get_pending_auth_approvals() -> dict[str, dict]:
    return _pending_auth_approvals


# How long a headless auth request waits for a human before it is refused.
_HEADLESS_APPROVAL_TIMEOUT_SEC = 120


class _CrossLoopSignal:
    """One-shot signal set from any loop, awaited on the loop that made it.

    The waiter is a tool handler, which the registry runs on a loop of its
    own; the setter is a WebSocket command handler on the main loop. A
    `threading.Event` bridges them, but only by parking a pool worker for
    the whole wait — and pool workers are joined at interpreter exit, so a
    shutdown during an approval waited out the full timeout before the
    process could leave.
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()

    def set(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            # Waiter's loop is already gone: the tool call it belonged to
            # has returned, so there is nobody left to signal. Say so —
            # swallowing this is how a dead mechanism looks healthy.
            log.warning(
                "Approval signalled after its waiter's loop closed — "
                "the call it belonged to has already returned"
            )

    async def wait(self) -> None:
        await self._event.wait()


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
  function getValue(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      return (el.value || '').toString().slice(0, 200);
    }
    return '';
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
    const value = getValue(el);
    const children = [];
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
    if (!role && !name && children.length === 0) {
      let directText = '';
      for (const node of el.childNodes) {
        if (node.nodeType === 3) {
          const t = node.textContent.trim();
          if (t) directText += (directText ? ' ' : '') + t;
        }
      }
      if (directText) return {role: 'generic', name: directText.slice(0, 200), value: '', hidden: false, children: [], el: elId};
      return null;
    }
    return {
      role: role || 'generic',
      name: name,
      value: value,
      hidden: false,
      children: children,
      el: elId,
    };
  }
  return walk(document.body) || {role: 'generic', name: '', children: [], el: ''};
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
        if value and role in _A11Y_VALUE_ROLES:
            line += f' = "{value}"'
        line += ref_tag
        lines.append(line)
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
        result.append(
            f"\n[... {remaining} more lines truncated, "
            "use browser_snapshot for full content]"
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
        return _rejoin_with_actionable(
            extracted or _truncate_snapshot(prose, max_chars), actionable,
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

    Domain restriction is currently enforced at `navigate()` against
    `self._etld1s`. ADR-029 Task 003 replaces that check with a
    Playwright route handler that intercepts EVERY request (including
    redirects, XHR, etc.) — see 003-domain-restriction.md.
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
        self._etld1s = {web_auth.resolve_etld1(d) for d in self._domains}
        # Backward-compat single-domain alias used by ADR-028 callers.
        self._domain = self._domains[0] if self._domains else None
        self._etld1 = next(iter(self._etld1s), None)
        self._cookies_loaded = False
        self._cm = None
        self._browser = None
        self._context = None
        self._page = None
        self._domain_blocks = 0
        self._disconnected = False
        self._last_refs: dict[str, dict] = {}
        # Scopes the `data-dpc-el` marks to one snapshot, so a mark left on an
        # element this walk no longer reaches cannot answer a current ref.
        self._snapshot_serial: int = 0
        self._executor: Optional["_PinnedThread"] = None
        self._last_activity: float = time.monotonic()
        # PIDs of the Camoufox/Firefox subprocess tree spawned by this
        # browser, captured at launch. Used only as a last-resort kill when
        # close() times out at shutdown (dead Playwright driver) — otherwise
        # the subprocess orphans and survives Python exit. See
        # _force_kill_process.
        self._browser_pids: set[int] = set()

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
        vault entries (used at session-open where storage_state may cover
        the gap); default `False` keeps the strict re-login surface for
        any explicit single-domain call.
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

        state_path = self._state_path()
        context_kwargs: dict = {}
        if self._anonymous:
            log.debug(
                "anonymous browser for agent=%s — no saved login loaded",
                self._agent_id,
            )
        elif state_path.exists():
            try:
                state_data = json.loads(state_path.read_text(encoding="utf-8"))
                # Strip origins (localStorage/sessionStorage) — they cause
                # Firefox to briefly visit each origin on context creation,
                # which makes previous URLs flash in the address bar.
                # Cookies are the only cross-session state we need.
                if state_data.get("origins"):
                    state_data["origins"] = []
                    tmp = state_path.with_suffix(".json.tmp")
                    tmp.write_text(
                        json.dumps(state_data), encoding="utf-8"
                    )
                    context_kwargs["storage_state"] = str(tmp)
                else:
                    context_kwargs["storage_state"] = str(state_path)
            except (json.JSONDecodeError, OSError) as e:
                log.warning(
                    "storage_state parse error at %s, falling back to vault: %s",
                    state_path, e,
                )

        self._context = self._browser.new_context(**context_kwargs)
        self._install_domain_route_handler()

        # Vault is the canonical source for cookies; storage_state is kept
        # only for localStorage/sessionStorage and as a starting point for
        # cookies the browser may have rotated mid-session. After loading
        # storage_state, always overlay vault cookies for every configured
        # domain: add_cookies() replaces by name+domain+path, so vault
        # entries win on conflict and storage_state-only cookies survive.
        # skip_missing=True keeps the open path tolerant of domains whose
        # vault entries are absent/expired — storage_state still covers
        # them, and any genuine re-login need surfaces on the first
        # request that hits a protected resource.
        if self._domains:
            self._inject_vault_cookies(
                domains=list(self._domains), skip_missing=True
            )

        self._page = self._context.new_page()
        _attach_page_diagnostics(self._page, agent_id=self._agent_id)
        _active_camoufox_browsers.add(self)

    def _state_path(self) -> Path:
        home = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))
        return home / "agents" / self._agent_id / "browser_state.json"

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

    def _sync_cookies_to_vault(self, cookies: list[dict]) -> None:
        if not cookies or not self._etld1s:
            return
        from dpc_client_core import web_auth

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

        snake = {d: _from_playwright_cookies(items) for d, items in by_etld1.items()}
        for domain, items in snake.items():
            web_auth.save_cookies(self._agent_id, domain, items)

    def _save_storage_state(self) -> None:
        if self._context is None:
            return
        if self._anonymous:
            # It never held the agent's login, so it has nothing to
            # contribute — and writing here would overwrite the file the
            # interactive session owns with a session that knows nothing.
            return
        if self._disconnected:
            # Browser already detached (e.g. user closed the window): the
            # context is dead, so storage_state() would only raise and the
            # cookies are unreadable. Skip quietly instead of WARNING-spamming.
            log.debug(
                "storage_state save skipped for agent=%s (browser already closed)",
                self._agent_id,
            )
            return
        try:
            state_path = self._state_path()
            state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = state_path.with_suffix(".json.tmp")
            # Cookies only, deliberately. `storage_state()` also collects
            # localStorage, and Firefox reads it by *opening a window on each
            # origin* — measured: a save with two origins peaked at two extra
            # visible windows, appearing and vanishing within a second. This
            # runs after every navigate and at close, which is the flicker of
            # windows opening and closing that the user kept seeing.
            #
            # Nothing is lost: the load path strips origins before handing the
            # state to new_context, for the same reason in reverse ("they
            # cause Firefox to briefly visit each origin on context creation").
            # So the localStorage we paid those windows to collect was written
            # to disk and then discarded on the next open.
            state = {"cookies": self._context.cookies(), "origins": []}
            tmp_path.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp_path, state_path)
            if os.name == "posix":
                try:
                    os.chmod(state_path, 0o600)
                except OSError as chmod_err:
                    log.warning(
                        "storage_state chmod failed for agent=%s: %s",
                        self._agent_id, chmod_err,
                    )
            if state is not None:
                self._sync_cookies_to_vault(state.get("cookies", []))
        except Exception as e:
            if self._disconnected:
                # Disconnect fired *during* storage_state() (manual close
                # race): expected, not a real failure — keep it at DEBUG.
                log.debug(
                    "storage_state save skipped for agent=%s "
                    "(browser closed mid-save): %s",
                    self._agent_id, e,
                )
            else:
                log.warning(
                    "storage_state save failed for agent=%s: %s",
                    self._agent_id, e,
                )

    def _install_domain_route_handler(self) -> None:
        if self._context is None:
            return
        if not self._domains:
            return
        self._context.route("**/*", self._domain_route_gate)

    def _domain_route_gate(self, route) -> None:
        try:
            url = route.request.url
        except Exception:
            # Unknown Route shape — fail-closed rather than let request through.
            try:
                route.abort()
            except Exception:
                pass
            return

        if not url.startswith(("http://", "https://")):
            try:
                route.continue_()
            except Exception:
                pass
            return

        if not self._etld1s:
            self._on_domain_blocked(url, "")
            try:
                route.abort()
            except Exception:
                pass
            return

        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        for allowed in self._etld1s:
            if _domain_matches(url, allowed):
                try:
                    route.continue_()
                except Exception:
                    pass
                return

        self._on_domain_blocked(url, host)
        try:
            route.abort()
        except Exception:
            pass

    def _on_domain_blocked(self, url: str, etld1: str) -> None:
        # `etld1` here is the BLOCKED domain, not an auth domain.
        self._domain_blocks += 1
        try:
            from dpc_client_core import web_auth
            web_auth.log_browser_action(
                agent_id=self._agent_id,
                domain=etld1,
                action="domain_blocked",
                url=url,
                result="denied",
            )
        except Exception as exc:
            log.warning("audit emit failed (domain_blocked %s): %s", url, exc)

    def _current_etld1(self) -> str:
        if self._page is not None:
            try:
                from dpc_client_core import web_auth
                return web_auth.resolve_etld1(self._page.url)
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
        if not self._etld1s:
            return  # session opened without any auth domain (rare; tests)
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
            self._save_storage_state()
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

    def click(self, ref_or_selector: str, timeout: int = 30000) -> None:
        """Click an element. Accepts a `@eN` ref from the last
        `a11y_snapshot()` or a CSS selector (fallback)."""
        self._require_open()
        url = self._page.url
        mode = "ref" if ref_or_selector.startswith("@e") else "css"
        try:
            self._resolve_ref(ref_or_selector).click(timeout=timeout)
        except Exception as exc:
            self._audit_action(
                "click", url, "failed",
                selector=ref_or_selector, mode=mode,
                **_audit_error(exc),
            )
            raise
        self._audit_action(
            "click", url, "ok", selector=ref_or_selector, mode=mode,
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

        for _ in range(max_scrolls + 1):
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
                    break

            try:
                self.scroll("down", 800)
            except Exception:
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
        try:
            if not self._disconnected:
                url = ""
                if self._page is not None:
                    try:
                        url = self._page.url
                    except Exception:
                        pass
                self._audit_action("close", url, "ok")
                if self._context is not None:
                    try:
                        self._save_storage_state()
                    except Exception as exc:
                        log.warning(
                            "storage_state save during close failed for agent=%s: %s",
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

    def _on_browser_disconnected(self, *args: Any) -> None:
        """Fired by Playwright when the browser process detaches."""
        if self._disconnected:
            return
        self._disconnected = True
        log.info(
            "Camoufox browser disconnected (agent=%s) — removed from active set",
            self._agent_id,
        )
        _active_camoufox_browsers.discard(self)
        self._deregister()
        self._shutdown_executor()


def _get_or_create_session(
    agent_id: str, domains: list[str], headed: bool
) -> AuthBrowser:
    """Return an existing AuthBrowser for this agent or create one.

    Ark's D2 duplicate-open guard: a second `browser_*` tool call on
    the same agent reuses the live session instead of opening a second
    Camoufox subprocess. Domains/headed args apply only when a NEW
    session is created — switching modes mid-flight requires explicit
    `browser_close` first.

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


_SESSION_CALL_TIMEOUT = 120  # seconds — prevents hung executor from blocking the event loop forever


async def _run_in_session(
    session: "AuthBrowser", method_name: str, *args: Any, **kwargs: Any,
) -> Any:
    """Invoke a sync AuthBrowser method on the session's dedicated
    single-worker thread. Required because Playwright sync API objects
    (Page, Context, Browser) are thread-affine; every call must come
    from the thread that owns the connection."""
    session._last_activity = time.monotonic()
    loop = asyncio.get_running_loop()
    method = getattr(session, method_name)
    return await asyncio.wait_for(
        loop.run_in_executor(
            session._get_executor(), lambda: method(*args, **kwargs),
        ),
        timeout=_SESSION_CALL_TIMEOUT,
    )


_session_create_locks: dict[str, asyncio.Lock] = {}


async def _get_or_create_session_async(
    agent_id: str, domains: list[str], headed: bool,
) -> "AuthBrowser":
    """Async wrapper around `_get_or_create_session` that pins the
    initial `start()` to the new session's executor, so every later
    `_run_in_session` call lands on the same thread.

    Uses a per-agent asyncio.Lock to prevent duplicate browser launches
    when the LLM emits parallel browse_page tool calls in one round."""
    if agent_id not in _session_create_locks:
        _session_create_locks[agent_id] = asyncio.Lock()
    async with _session_create_locks[agent_id]:
        existing = _active_browser_sessions.get(agent_id)
        if existing is not None and existing._page is not None:
            try:
                if existing._page.is_closed():
                    log.info("Stale session for %s (page closed) — recreating", agent_id)
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


async def browse_page(
    ctx: ToolContext,
    url: str,
    size: str = "m",
    use_auth: Optional[str] = None,
    keep_open: bool = False,
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
        from dpc_client_core import web_auth as _web_auth_mod

        # ADR-029 Task 008: per-request approval for headless auth.
        # Headed (keep_open=True) needs no gate — human sees the browser.
        # Headless (keep_open=False) broadcasts approval request to UI.
        if not keep_open:
            local_api = getattr(dpc_service, "local_api", None) if dpc_service else None
            if local_api is not None and not getattr(local_api, "has_clients", True):
                # broadcast_event drops the request when nobody is connected,
                # so the wait below could only ever time out. Two minutes of
                # silence per call, and the agent is told "not approved" as
                # though a human had refused. Say what actually happened.
                _web_auth_mod.audit_append(
                    agent_id, use_auth, url, status="headless_no_ui",
                )
                return (
                    f"⚠️ Headless access to '{use_auth}' needs approval, but no "
                    f"UI client is connected to approve it. Use keep_open=true "
                    f"for a headed browser."
                )
            if local_api is not None:
                import uuid as _uuid
                approval_id = _uuid.uuid4().hex[:12]
                approval_event = _CrossLoopSignal()
                _pending_auth_approvals[approval_id] = {
                    "event": approval_event,
                    "agent_id": agent_id,
                    "domain": use_auth,
                    "url": url,
                    "approved": False,
                }
                await local_api.broadcast_event(
                    "web_auth_headless_approval_request",
                    {
                        "request_id": approval_id,
                        "agent_id": agent_id,
                        "domain": use_auth,
                        "url": url,
                    },
                )
                try:
                    await asyncio.wait_for(
                        approval_event.wait(),
                        timeout=_HEADLESS_APPROVAL_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    pass
                entry = _pending_auth_approvals.pop(approval_id, {})
                if not entry.get("approved", False):
                    _web_auth_mod.audit_append(
                        agent_id, use_auth, url, status="headless_rejected",
                    )
                    return (
                        f"⚠️ Headless access to '{use_auth}' was not approved. "
                        f"Use keep_open=true for headed browser login."
                    )
                _web_auth_mod.audit_append(
                    agent_id, use_auth, url, status="headless_approved",
                )

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
                # headed=False: the gate above asked the user to approve
                # *headless* access and the audit records it as such
                # (`headless_approved` / `headless_rejected`). Passing a
                # headed browser here opened a visible window per call
                # while telling the user it would not.
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
        max_chars = _SIZE_PRESETS.get(size, _SIZE_PRESETS["m"])
        total = len(text)
        if max_chars and total > max_chars:
            text = text[:max_chars] + f"\n\n... (truncated, {total} total chars, use size='l' or 'f' for more)"
        return f"Content from {url} (markdown, auth={use_auth}, {total} chars):\n\n{text}"

    if keep_open:
        agent_id = ctx.agent_root.name if hasattr(ctx, 'agent_root') else "anonymous"
        try:
            session = await _get_or_create_session_async(agent_id, [], True)
            session = await _navigate_with_recovery(session, agent_id, url, [])
            html = await _run_in_session(session, "get_page_html")
        except Exception as e:
            return f"⚠️ Camoufox browser failed: {e}"
        text = _html_to_markdown(html)
        max_chars = _SIZE_PRESETS.get(size, _SIZE_PRESETS["m"])
        total = len(text)
        if max_chars and total > max_chars:
            text = text[:max_chars] + f"\n\n... (truncated, {total} total chars, use size='l' or 'f' for more)"
        return f"Content from {url} (markdown, headed, {total} chars):\n\n{text}"

    result = await asyncio.to_thread(_browse_sync, url)

    if not result.get("success", False) and "error" in result:
        return f"⚠️ Failed to fetch page: {result['error']}"

    text = result.get("text", "")

    if result.get("needs_js"):
        js_text = await _fetch_js_text(
            url, getattr(getattr(ctx, "agent_root", None), "name", None),
        )
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
            await _run_in_session(session, "click", ref_or_selector, timeout)
        except ValueError as e:
            return f"⚠️ {e}"
        except Exception as e:
            log.warning(
                "click failed (agent=%s): %s: %s",
                agent_id, type(e).__name__, str(e).split(chr(10))[0],
            )
            return f"⚠️ Click failed: {type(e).__name__}: {e}"
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


async def browser_extract(ctx: ToolContext) -> str:
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
    return html


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


async def browser_collect(
    ctx: ToolContext,
    container: str,
    item_selector: str,
    extract: list[str] | None = None,
    max_scrolls: int = 30,
    scroll_pause_ms: int = 1000,
    dedup_by: str = "text",
) -> str:
    """Scroll a container and collect all matching items via CSS selectors.
    Returns a JSON summary with all extracted items, deduped and guarded."""
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
    import json as _json
    if isinstance(result, dict) and result.get("error"):
        return f"⚠️ {result['error']}"
    total = result.get("total", 0)
    scrolls = result.get("scrolls_done", 0)
    header = f"Collected {total} items ({scrolls} scrolls)"
    if result.get("warning"):
        header += f"\n⚠️ {result['warning']}"
    items_json = _json.dumps(result.get("items", []), ensure_ascii=False, indent=2)
    return f"{header}\n\n{items_json}"


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
    return "Browser session closed"


def get_tools() -> List[ToolEntry]:
    """Export browser tools for registry."""
    return [
        ToolEntry(
            name="browse_page",
            schema={
                "name": "browse_page",
                "description": "Fetch a web page and extract content as structured markdown. Preserves headings, lists, tables, and links. Use size presets to control output length: s=5K, m=10K (default), l=25K, f=full. Set use_auth=<domain> to fetch authenticated content using stored cookies (requires prior login via the web-auth UI). Set keep_open=true to leave the headed Camoufox window open after returning (works for both anonymous and use_auth fetches) — useful for visual debugging and Task 002 stateful interactive flows.",
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
                            "description": "Optional auth domain (eg 'example.com'). When set, the page is fetched authenticated using cookies from the agent's encrypted vault. The URL must be within the same eTLD+1 as use_auth (subdomains allowed). Returns a re-login prompt if cookies are missing or expired."
                        },
                        "keep_open": {
                            "type": "boolean",
                            "description": "When true, leave the headed Camoufox window open after the fetch returns. Works on both the anonymous and use_auth paths: either way a headed Camoufox session is opened and reused on subsequent keep_open browse_page calls for the same agent, so opening one site and then another navigates the same window. Window stays open until DPC restart, an explicit close_browser call, or the next keep_open fetch that reuses it. Use for visual debugging or as the foundation for Task 002 interactive flows.",
                            "default": False
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
            timeout_sec=15,
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
                    "properties": {},
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
                "description": "Scroll a container and collect all matching items. Use CSS selectors for container and items (discover them from browser_snapshot's scrollable-container hints or browser_extract's raw HTML). Scrolls the container, extracts items matching item_selector, deduplicates, and repeats until no new items appear or max_scrolls is reached. Returns JSON array of all collected items. Ideal for infinite-scroll lists (orders, search results, product catalogs).",
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
