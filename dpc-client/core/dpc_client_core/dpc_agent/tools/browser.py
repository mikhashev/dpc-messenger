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
from dataclasses import dataclass
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


def _browse_with_camoufox(url: str) -> Optional[str]:
    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        return None

    try:
        with Camoufox(headless=True, **_camoufox_launch_kwargs()) as browser:
            page = browser.new_page()
            _attach_page_diagnostics(page)
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
    "fab_chlg_",            # example.com marketplace marker
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
            refs[ref] = {"role": role, "name": name}
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


_LLM_EXTRACT_WITH_TASK = (
    "You are a content extractor for a browser automation agent.\n\n"
    "The user's task is: {user_task}\n\n"
    "Given the following page snapshot (accessibility tree representation), "
    "extract and summarize the most relevant information for completing "
    "this task. Focus on:\n"
    "1. Interactive elements (buttons, links, inputs) that might be needed\n"
    "2. Text content relevant to the task "
    "(prices, descriptions, headings, important info)\n"
    "3. Navigation structure if relevant\n\n"
    "Keep ref IDs (like @e5) for interactive elements so the agent "
    "can use them.\n\n"
    "Page Snapshot:\n{snapshot}\n\n"
    "Provide a concise summary that preserves actionable information "
    "and relevant content."
)

_LLM_EXTRACT_NO_TASK = (
    "Summarize this page snapshot, preserving:\n"
    "1. All interactive elements with their ref IDs (like @e5)\n"
    "2. Key text content and headings\n"
    "3. Important information visible on the page\n\n"
    "Page Snapshot:\n{snapshot}\n\n"
    "Provide a concise summary focused on interactive elements and "
    "key content."
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
    None, when the auxiliary call raises, or when the model returns an
    empty string. No-op when `snapshot_text` already fits under
    `max_chars`."""
    if len(snapshot_text) <= max_chars:
        return snapshot_text
    if llm_manager is None:
        return _truncate_snapshot(snapshot_text, max_chars)
    if user_task:
        prompt = _LLM_EXTRACT_WITH_TASK.format(
            user_task=user_task, snapshot=snapshot_text,
        )
    else:
        prompt = _LLM_EXTRACT_NO_TASK.format(snapshot=snapshot_text)
    try:
        response = await llm_manager.query(
            prompt, provider_alias=provider_alias,
        )
        extracted = (response or "").strip()
        return extracted or _truncate_snapshot(snapshot_text, max_chars)
    except Exception:
        return _truncate_snapshot(snapshot_text, max_chars)


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
    ):
        from dpc_client_core import web_auth

        self._agent_id = agent_id
        self._headed = headed
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
            if web_auth.is_expired(cookies):
                if skip_missing:
                    log.debug("vault: cookies for %s expired, skipping", d)
                    continue
                raise AuthExpiredError(
                    f"Cookies for {d} expired (agent={self._agent_id}) — re-login required"
                )
            all_cookies.extend(cookies)
        self._cookies_loaded = True
        return all_cookies

    def _open(self) -> None:
        from camoufox.sync_api import Camoufox

        self._cm = Camoufox(headless=not self._headed, **_camoufox_launch_kwargs())
        self._browser = self._cm.__enter__()
        try:
            self._browser.on("disconnected", self._on_browser_disconnected)
        except Exception as e:
            log.debug(
                "attach disconnect listener failed (agent=%s): %s",
                self._agent_id, e,
            )

        state_path = self._state_path()
        context_kwargs: dict = {}
        if state_path.exists():
            try:
                json.loads(state_path.read_text(encoding="utf-8"))
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
        try:
            state_path = self._state_path()
            state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = state_path.with_suffix(".json.tmp")
            state = self._context.storage_state(path=str(tmp_path))
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
            log.warning(
                "storage_state save failed for agent=%s: %s",
                self._agent_id, e,
            )

    def _install_domain_route_handler(self) -> None:
        if self._context is None:
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
            self._page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as exc:
            self._audit_action(
                "navigate", url, "failed",
                from_url=from_url, error=type(exc).__name__,
            )
            raise
        snapshot_text = ""
        snapshot_audit: dict[str, Any] = {"from_url": from_url}
        try:
            snapshot_text, refs = self.a11y_snapshot()
            snapshot_audit["snapshot_node_count"] = len(refs)
            snapshot_audit["snapshot_char_count"] = len(snapshot_text)
        except Exception as exc:
            snapshot_audit["snapshot_error"] = type(exc).__name__
        self._audit_action("navigate", url, "ok", **snapshot_audit)
        return snapshot_text

    def goto(self, url: str) -> str:
        """ADR-028 back-compat alias for `navigate()`. Single-shot
        consumers (_browse_with_camoufox / _auth_browse_html) call
        goto() and discard the return; proper wrapper (not class-level
        assignment) so subclass overrides of navigate() are honored."""
        return self.navigate(url)

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
        """Scroll vertically. direction: 'up' or 'down'."""
        self._require_open()
        url = self._page.url
        delta = -amount if direction == "up" else amount
        try:
            self._page.mouse.wheel(0, delta)
        except Exception as exc:
            self._audit_action(
                "scroll", url, "failed",
                direction=direction, amount=amount,
                error=type(exc).__name__,
            )
            raise
        self._audit_action(
            "scroll", url, "ok", direction=direction, amount=amount,
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
                error=type(exc).__name__,
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
                text_length=text_length, error=type(exc).__name__,
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
                full_page=full_page, error=type(exc).__name__,
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
                "extract", url, "failed", error=type(exc).__name__,
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
                error=type(exc).__name__,
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
            raw = self._page.accessibility.snapshot()
            tree_text, refs = _build_a11y_tree(raw) if raw else ("", {})
            self._last_refs = refs
        except Exception as exc:
            self._audit_action(
                "snapshot", url, "failed", error=type(exc).__name__,
            )
            raise
        self._audit_action(
            "snapshot", url, "ok",
            node_count=len(refs), char_count=len(tree_text),
        )
        return tree_text, refs

    def _resolve_ref(self, ref_or_selector: str):
        """Map a `@eN` ref against the last snapshot to a Playwright
        locator; fall back to treating the string as a CSS selector.

        Raises ValueError for `@eN` refs missing from `_last_refs` so
        the caller can prompt the agent to take a new snapshot."""
        self._require_open()
        if ref_or_selector.startswith("@e"):
            node = self._last_refs.get(ref_or_selector)
            if node is None:
                raise ValueError(
                    f"unknown ref {ref_or_selector!r} — "
                    "call a11y_snapshot() to refresh"
                )
            role = node.get("role", "")
            name = node.get("name", "")
            if name:
                return self._page.get_by_role(role, name=name)
            return self._page.get_by_role(role)
        return self._page.locator(ref_or_selector)

    def close(self) -> None:
        """Release browser resources. Safe to call multiple times."""
        # Audit before teardown nulls _page. Skip when already disconnected.
        if not self._disconnected:
            url = ""
            if self._page is not None:
                try:
                    url = self._page.url
                except Exception:
                    pass
            self._audit_action("close", url, "ok")

        if self._disconnected:
            self._cm = None
            self._browser = None
            self._context = None
            self._page = None
            _active_camoufox_browsers.discard(self)
            _active_browser_sessions.pop(self._agent_id, None)
            return
        if self._context is not None:
            self._save_storage_state()
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            finally:
                self._cm = None
                self._browser = None
                self._context = None
                self._page = None
        _active_camoufox_browsers.discard(self)
        _active_browser_sessions.pop(self._agent_id, None)

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
        _active_browser_sessions.pop(self._agent_id, None)


def _get_or_create_session(
    agent_id: str, domains: list[str], headed: bool
) -> AuthBrowser:
    """Return an existing AuthBrowser for this agent or create one.

    Ark's D2 duplicate-open guard: a second `browser_*` tool call on
    the same agent reuses the live session instead of opening a second
    Camoufox subprocess. Domains/headed args apply only when a NEW
    session is created — switching modes mid-flight requires explicit
    `browser_close` first."""
    existing = _active_browser_sessions.get(agent_id)
    if existing is not None and existing._page is not None:
        return existing
    session = AuthBrowser(agent_id=agent_id, domains=domains, headed=headed)
    session.start()
    _active_browser_sessions[agent_id] = session
    return session


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
    """Sync helper returning RAW HTML. T9 needs the pre-conversion HTML
    for `looks_like_challenge()` detection."""
    with AuthBrowser(agent_id=agent_id, domain=domain, headed=headed) as ab:
        ab.goto(url)
        return ab.get_page_html()


def _auth_browse(
    agent_id: str, domain: str, url: str, headed: bool = True
) -> str:
    """Wrapper around `_auth_browse_html` + `_html_to_markdown`. Kept so
    existing tests that patch `_auth_browse` directly continue to work."""
    return _html_to_markdown(_auth_browse_html(agent_id, domain, url, headed))


# ─────────────────────────────────────────────────────────────
# ADR-028 T9 — Popup fallback (caller side)
# ─────────────────────────────────────────────────────────────

@dataclass
class PendingPopupRequest:
    """One in-flight popup-fallback request. The WS handler for
    `web_auth_popup_complete` reads `expected_url` / `expected_etld1`
    to enforce the Q2 URL-safety check (Ark softer version) before
    resolving the future with the popup-extracted HTML.
    """
    future: asyncio.Future
    expected_url: str
    expected_etld1: str
    agent_id: str = ""
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
) -> str:
    """Ask the frontend to open a Tauri WebView popup so the user can
    solve a challenge (example.com fab_chlg, Cloudflare) or view JS-rendered
    content (example.org orders) for `url`. Awaits the user closing the
    popup; the backend WS handler `web_auth_popup_complete` resolves
    the future with the extracted HTML.

    `reason` is forwarded to the frontend so the popup-request panel
    can render context-appropriate copy:
      - "anti_bot_challenge" — Camoufox got a challenge stub
        (`looks_like_challenge` triggered)
      - "always_popup" — domain is on the agent's `always_popup`
        whitelist (example.org class — JS-render-only sites)

    Returns the popup-extracted HTML. The caller converts to markdown
    via `_html_to_markdown`. Raises `AuthRequiredError` on timeout,
    missing dpc_service, or popup_error reported by the frontend.
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

    request_id = uuid.uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _pending_popup_requests[request_id] = PendingPopupRequest(
        future=future,
        expected_url=url,
        expected_etld1=_wa.resolve_etld1(domain),
        agent_id=agent_id,
        opened_at=time.monotonic(),
    )

    try:
        await local_api.broadcast_event(
            "web_auth_popup_request",
            {
                "request_id": request_id,
                "agent_id": agent_id,
                "domain": domain,
                "url": url,
                "reason": reason,
            },
        )
        return await asyncio.wait_for(future, timeout=_POPUP_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        # T10-FRONTEND-CLEANUP-ON-TIMEOUT: tell the frontend to dismiss
        # the modal and close the popup window. Backend-side leak in
        # `_pending_popup_requests` is closed by the finally below; this
        # event closes the matching frontend-side state.
        try:
            await local_api.broadcast_event(
                "web_auth_popup_force_close",
                {"request_id": request_id, "reason": "timeout"},
            )
        except Exception:
            pass  # best-effort; do not mask the AuthRequiredError below
        raise AuthRequiredError(
            f"Popup fallback timeout ({_POPUP_TIMEOUT_S}s) for {url} — "
            f"user did not complete"
        ) from e
    finally:
        _pending_popup_requests.pop(request_id, None)


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

        # T9 always_popup (example.org variant C): skip Camoufox entirely
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
                    html = await _request_popup_fallback(
                        ctx, agent_id, use_auth, url,
                        reason="always_popup",
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
                return f"Content from {url} (markdown, auth={use_auth}, {total} chars):\n\n{text}"

        try:
            if keep_open:
                # ADR-029 Task 002: stateful headed session — page stays
                # open after returning, agent calls browser_* tools for
                # subsequent actions. Cookies for every allowed domain
                # load up-front (D1 multi-domain context).
                allowed = (
                    firewall.get_agent_web_auth_domains(agent_id)
                    if firewall is not None
                    else [use_auth]
                )
                session = await asyncio.to_thread(
                    _get_or_create_session, agent_id, list(allowed), True
                )
                await asyncio.to_thread(session.navigate, url)
                html = await asyncio.to_thread(session.get_page_html)
            else:
                html = await asyncio.to_thread(
                    _auth_browse_html, agent_id, use_auth, url, True
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

        # T9 challenge detection: if Camoufox got back an anti-bot stub,
        # hand off to the Tauri WebView popup (T2 cookie jar / fingerprint
        # the user already trusted at login). User solves challenge or
        # views JS-rendered content, popup closes, backend gets the
        # rendered HTML back via WS `web_auth_popup_complete`.
        if looks_like_challenge(html):
            log.info(
                "Anti-bot challenge detected for %s (agent=%s) — "
                "requesting popup fallback", url, agent_id,
            )
            try:
                html = await _request_popup_fallback(
                    ctx, agent_id, use_auth, url,
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
        return f"Content from {url} (markdown, auth={use_auth}, {total} chars):\n\n{text}"

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
                "description": "Fetch a web page and extract content as structured markdown. Preserves headings, lists, tables, and links. Use size presets to control output length: s=5K, m=10K (default), l=25K, f=full. Set use_auth=<domain> to fetch authenticated content using stored cookies (requires prior login via the web-auth UI). Set keep_open=true to leave the headed Camoufox window open after returning (auth path only) — useful for visual debugging and Task 002 stateful interactive flows.",
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
                            "description": "When true, leave the headed Camoufox window open after the fetch returns (only effective on the use_auth path; ignored for anonymous fetches). The session is reused on subsequent browse_page calls for the same agent, so opening one site and then another navigates the same window. Window stays open until DPC restart or until the next browse_page call replaces it. Use for visual debugging or as the foundation for Task 002 interactive flows.",
                            "default": False
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
